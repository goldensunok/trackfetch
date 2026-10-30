"""The whole Soulseek journey, end to end, with slskd mocked at its HTTP boundary.

This is the test that mirrors the success criterion:

    paste a list -> SEARCH ALL -> inspect and filter -> choose -> DOWNLOAD
    SELECTED -> watch progress -> the file is recorded as finished

Everything except the slskd HTTP traffic is the real application: the real
registry, the real search service, the real queue, the real database. It is
still not a live Soulseek test.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from tests.fixtures import slskd_payloads as fx
from trackfetch.config.secrets import InMemorySecretStore
from trackfetch.models.common import InputKind, TransferStatus
from trackfetch.services import input_parser
from trackfetch.services.filters import ResultFilter

BASE = "http://localhost:5030"
API = f"{BASE}/api/v0"


@pytest.fixture
def app(scratch_home, tmp_path):
    from trackfetch.app import TrackfetchApp
    from trackfetch.storage.db import Database

    secrets = InMemorySecretStore()
    application = TrackfetchApp(
        secrets=secrets,
        database=Database(tmp_path / "e2e.sqlite3"),
        configure_logging=False,
    )
    application.settings.download_dir = str(tmp_path / "downloads")
    application.settings.search_timeout = 1
    application.settings.first_run_complete = True

    slskd = application.settings.provider("slskd")
    slskd.enabled = True
    slskd.config.update(
        {"base_url": BASE, "username": "slskd", "manage_slskd": False, "timeout": 5}
    )
    secrets.set("slskd.password", "helperpass")

    application.start()
    yield application
    application.shutdown()


def _mock_slskd(responses=None, downloads=None):
    respx.get(f"{API}/session/enabled").mock(return_value=httpx.Response(200, json=True))
    respx.post(f"{API}/session").mock(
        return_value=httpx.Response(200, json=fx.SESSION_LOGIN)
    )
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_CONNECTED)
    )
    respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_COMPLETE)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}/responses").mock(
        return_value=httpx.Response(200, json=responses if responses is not None else fx.SEARCH_RESPONSES)
    )
    respx.delete(f"{API}/searches/{fx.SEARCH_ID}").mock(return_value=httpx.Response(204))
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(200, json=downloads if downloads is not None else [])
    )


def _wait(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@respx.mock
def test_the_whole_soulseek_journey(app, tmp_path):
    _mock_slskd()
    enqueue = respx.post(f"{API}/transfers/downloads/batches").mock(
        return_value=httpx.Response(201)
    )

    # 1. Soulseek reports as connected.
    status = app.registry.require("slskd").test_connection()
    assert status.ok

    # 2. The user pastes a list.
    items = input_parser.parse_lines("Aphex Twin - Xtal\nBurial - Archangel")
    batch_id = app.repos.batches.create_batch(source="pasted")
    app.repos.batches.add_items(batch_id, items)
    assert all(i.kind is InputKind.QUERY for i in items)

    # 3. SEARCH ALL.
    progress_seen: list[str] = []
    outcomes = app.search_service.search_all(
        items, "slskd", on_progress=lambda p: progress_seen.append(p.text)
    )
    assert len(outcomes) == 2
    assert progress_seen[-1] == "Searching 2 / 2"
    assert all(len(o.candidates) == 4 for o in outcomes)

    # Nothing has been downloaded merely because a result scored highly.
    assert app.transfers.jobs() == []
    assert not any(app.settings.resolved_download_dir().glob("*"))

    # 4. The user filters, then picks one result deliberately.
    stored = app.repos.candidates.for_item(items[0].id)
    assert len(stored) == 4

    flac_only = ResultFilter(extensions=["flac"]).apply(stored)
    assert len(flac_only) == 2

    top = max(stored, key=lambda c: c.relevance)
    # The user picks the FLAC, which scores lower on the file name than the
    # plainly named MP3. Trackfetch must follow the person, not the score.
    chosen = next(c for c in stored if c.filename == "01 Xtal.flac")
    assert chosen.id != top.id
    assert chosen.relevance < top.relevance
    app.repos.candidates.set_selected(chosen.id, True)

    selected = app.repos.candidates.selected_for_batch(batch_id)
    assert len(selected) == 1

    # 5. DOWNLOAD SELECTED.
    queued, skipped = app.transfers.add_candidates(selected, tmp_path / "downloads")
    assert skipped == []
    assert len(queued) == 1
    job = queued[0]

    assert _wait(lambda: enqueue.called, timeout=15)
    body = json.loads(enqueue.calls[0].request.content)
    assert body["username"] == "peer_one"
    assert body["files"] == [
        {
            "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac",
            "size": 28311552,
        }
    ]

    # 6. Progress is reported.
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "username": "peer_one",
                    "directories": [
                        {
                            "directory": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92",
                            "files": [
                                {
                                    "id": "t1",
                                    "username": "peer_one",
                                    "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac",
                                    "size": 28311552,
                                    "state": "Queued, Remotely",
                                    "bytesTransferred": 0,
                                    "placeInQueue": 7,
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    )
    assert _wait(lambda: app.transfers.job(job.id).payload.get("place_in_queue") == 7)

    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "username": "peer_one",
                    "directories": [
                        {
                            "directory": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92",
                            "files": [
                                {
                                    "id": "t1",
                                    "username": "peer_one",
                                    "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac",
                                    "size": 28311552,
                                    "state": "InProgress",
                                    "bytesTransferred": 14155776,
                                    "averageSpeed": 200000.0,
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    )
    assert _wait(lambda: app.transfers.job(job.id).status is TransferStatus.DOWNLOADING)
    assert round(app.transfers.job(job.id).percent) == 50

    # 7. Completion is recorded, so it is not fetched again.
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "username": "peer_one",
                    "directories": [
                        {
                            "directory": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92",
                            "files": [
                                {
                                    "id": "t1",
                                    "username": "peer_one",
                                    "filename": "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac",
                                    "size": 28311552,
                                    "state": "Completed, Succeeded",
                                    "bytesTransferred": 28311552,
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    )
    assert _wait(lambda: app.transfers.job(job.id).status is TransferStatus.COMPLETE)
    assert app.repos.completions.count() == 1

    again, skipped_again = app.transfers.add_candidates([chosen], tmp_path / "downloads")
    assert again == []
    assert "Already downloaded" in skipped_again[0][1]


@respx.mock
def test_the_list_and_the_queue_survive_a_restart(app, tmp_path):
    _mock_slskd()
    respx.post(f"{API}/transfers/downloads/batches").mock(return_value=httpx.Response(201))

    items = input_parser.parse_lines("Aphex Twin - Xtal")
    batch_id = app.repos.batches.create_batch()
    app.repos.batches.add_items(batch_id, items)
    app.db.set_state("last_batch", batch_id)
    app.search_service.search_all(items, "slskd")

    stored = app.repos.candidates.for_item(items[0].id)
    app.repos.candidates.set_selected(stored[0].id, True)
    app.transfers.add_candidates(
        app.repos.candidates.selected_for_batch(batch_id), tmp_path / "downloads"
    )
    app.transfers.stop()

    # Reopen against the same database, as closing and reopening the program does.
    from trackfetch.app import TrackfetchApp

    reopened = TrackfetchApp(
        secrets=InMemorySecretStore(), database=app.db, configure_logging=False
    )
    reopened.settings = app.settings
    restored_jobs = reopened.transfers.load_existing()
    assert len(restored_jobs) == 1

    last_batch = reopened.db.get_state("last_batch")
    assert last_batch == batch_id
    restored_items = reopened.repos.batches.items(last_batch)
    assert [i.raw_text for i in restored_items] == ["Aphex Twin - Xtal"]
    assert len(reopened.repos.candidates.for_item(restored_items[0].id)) == 4
    assert reopened.repos.candidates.selected_for_batch(last_batch)


@respx.mock
def test_one_failing_search_does_not_stop_the_others(app):
    _mock_slskd()
    respx.get(f"{API}/searches/{fx.SEARCH_ID}/responses").mock(
        side_effect=[
            httpx.Response(200, json=fx.SEARCH_RESPONSES),
            httpx.Response(500, text="server error"),
            httpx.Response(200, json=fx.SEARCH_RESPONSES),
        ]
    )
    items = input_parser.parse_lines("one\ntwo\nthree")
    batch_id = app.repos.batches.create_batch()
    app.repos.batches.add_items(batch_id, items)

    app.settings.search_concurrency = 1
    outcomes = app.search_service.search_all(items, "slskd")
    assert len(outcomes) == 3
    # The middle search returns nothing, the others still return results.
    assert sum(1 for o in outcomes if o.candidates) == 2


@respx.mock
def test_a_disconnected_soulseek_produces_readable_failures_for_every_item(app):
    _mock_slskd()
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_DISCONNECTED)
    )
    items = input_parser.parse_lines("one\ntwo")
    batch_id = app.repos.batches.create_batch()
    app.repos.batches.add_items(batch_id, items)

    outcomes = app.search_service.search_all(items, "slskd")
    assert len(outcomes) == 2
    for outcome in outcomes:
        assert outcome.candidates == []
        assert "not signed in" in outcome.error.lower()
        assert outcome.remedy
        assert "Traceback" not in outcome.error
