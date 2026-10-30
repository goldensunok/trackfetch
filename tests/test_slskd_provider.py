"""Tests for the Soulseek provider (Trackfetch's slskd adapter).

The slskd HTTP boundary is mocked with realistic payloads. These tests cover the
whole user-facing path: connection reporting, searching, metadata, manual
selection, enqueueing exactly what was selected, progress, completion and
failure. They are not a live Soulseek test.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest
import respx

from tests.fixtures import slskd_payloads as fx
from trackfetch.models.common import ConnectionState, TransferStatus
from trackfetch.providers.base import SearchOptions
from trackfetch.providers.errors import SoulseekNotConnected
from trackfetch.providers.slskd import SlskdProvider

BASE = "http://localhost:5030"
API = f"{BASE}/api/v0"


class FakeSupervisor:
    """Stands in for the real slskd supervisor. Starts nothing, kills nothing."""

    def __init__(self, installed: bool = True, desktop_running: bool = False):
        self.installed = installed
        self.desktop_running = desktop_running
        self.start_calls = 0

    def find_installation(self):
        from trackfetch.services.slskd_supervisor import SlskdInstallation

        if self.installed:
            return SlskdInstallation(Path("/fake/slskd"), "managed")
        return SlskdInstallation(None, "none")

    def start(self, installation=None, use_managed_config=True):
        self.start_calls += 1
        return True

    def wait_until_ready(self, base_url=None, timeout=45, on_progress=None):
        return False

    def desktop_soulseek_running(self):
        return self.desktop_running

    def describe(self):
        return {"installed": self.installed, "running": False}


@pytest.fixture
def provider(slskd_ctx):
    p = SlskdProvider(slskd_ctx, supervisor=FakeSupervisor())
    yield p
    p.close()


def _mock_login():
    respx.post(f"{API}/session").mock(return_value=httpx.Response(200, json=fx.SESSION_LOGIN))
    respx.get(f"{API}/session/enabled").mock(return_value=httpx.Response(200, json=True))


def _mock_connected():
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_CONNECTED)
    )


# ---------------------------------------------------------------------------
# Configuration and connection reporting
# ---------------------------------------------------------------------------


def test_provider_advertises_the_right_capabilities(provider):
    from trackfetch.models.common import Capability

    assert provider.supports(Capability.SEARCH)
    assert provider.supports(Capability.ADD_JOB)
    assert provider.supports(Capability.PROGRESS)
    assert provider.supports(Capability.CANCEL)
    assert not provider.supports(Capability.BROWSE)


def test_missing_password_means_not_configured(slskd_ctx):
    slskd_ctx.set_secret("password", "")
    p = SlskdProvider(slskd_ctx, supervisor=FakeSupervisor())
    assert p.is_configured() is False
    status = p.test_connection()
    assert status.state is ConnectionState.NOT_CONFIGURED
    assert "Settings" in status.remedy
    p.close()


def test_disabled_provider_reports_disabled(slskd_ctx):
    slskd_ctx.provider_settings.enabled = False
    p = SlskdProvider(slskd_ctx, supervisor=FakeSupervisor())
    assert p.test_connection().state is ConnectionState.DISABLED
    p.close()


@respx.mock
def test_slskd_not_running_gives_a_plain_english_status(provider):
    respx.get(f"{API}/session/enabled").mock(side_effect=httpx.ConnectError("refused"))
    status = provider.test_connection()
    assert status.state is ConnectionState.DISCONNECTED
    assert "connect to Soulseek" in status.detail
    assert "Exception" not in status.detail
    assert "HTTPConnectionPool" not in status.detail
    assert status.remedy


@respx.mock
def test_connected_status_is_reported(provider):
    _mock_login()
    _mock_connected()
    status = provider.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert status.summary() == "Connected"


@respx.mock
def test_soulseek_signed_out_is_explained_not_dumped(provider):
    _mock_login()
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_DISCONNECTED)
    )
    respx.put(f"{API}/server").mock(return_value=httpx.Response(204))
    status = provider.test_connection()
    assert status.state is ConnectionState.DISCONNECTED
    assert "not signed in" in status.detail.lower()
    assert "Settings" in status.remedy or "Soulseek" in status.remedy


@respx.mock
def test_open_desktop_soulseek_produces_the_close_it_advice(slskd_ctx):
    p = SlskdProvider(slskd_ctx, supervisor=FakeSupervisor(desktop_running=True))
    _mock_login()
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_DISCONNECTED)
    )
    respx.put(f"{API}/server").mock(return_value=httpx.Response(204))
    status = p.test_connection()
    assert "close it" in status.remedy.lower()
    assert "one login per account" in status.remedy.lower()
    p.close()


@respx.mock
def test_provider_tries_to_start_slskd_when_it_is_not_answering(slskd_ctx):
    slskd_ctx.provider_settings.config["manage_slskd"] = True
    supervisor = FakeSupervisor()
    p = SlskdProvider(slskd_ctx, supervisor=supervisor)
    respx.get(f"{API}/session/enabled").mock(side_effect=httpx.ConnectError("refused"))
    p.check_connection(interactive=True)
    assert supervisor.start_calls == 1
    p.close()


@respx.mock
def test_background_check_does_not_start_slskd(slskd_ctx):
    """The automatic refresh looks and reports. It does not launch anything."""
    slskd_ctx.provider_settings.config["manage_slskd"] = True
    supervisor = FakeSupervisor()
    p = SlskdProvider(slskd_ctx, supervisor=supervisor)
    respx.get(f"{API}/session/enabled").mock(side_effect=httpx.ConnectError("refused"))
    status = p.test_connection()
    assert supervisor.start_calls == 0
    assert status.state is ConnectionState.DISCONNECTED
    p.close()


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


def _mock_full_search():
    respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_COMPLETE)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}/responses").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_RESPONSES)
    )
    respx.delete(f"{API}/searches/{fx.SEARCH_ID}").mock(return_value=httpx.Response(204))


@respx.mock
def test_search_returns_candidates_from_several_users(provider):
    _mock_login()
    _mock_connected()
    _mock_full_search()
    results = provider.search(
        "Aphex Twin - Xtal", SearchOptions(timeout=1, response_limit=50)
    )
    assert len(results) == 4
    assert {r.remote_user for r in results} == {"peer_one", "peer_two", "peer_three"}


@respx.mock
def test_search_exposes_every_available_metadata_field(provider):
    _mock_login()
    _mock_connected()
    _mock_full_search()
    results = provider.search("Aphex Twin - Xtal", SearchOptions(timeout=1))
    xtal = next(r for r in results if r.filename == "01 Xtal.flac")
    assert xtal.directory.endswith("Selected Ambient Works 85-92")
    assert xtal.extension == "flac"
    assert xtal.size == 28311552
    assert xtal.bitrate == 1004
    assert xtal.bit_depth == 16
    assert xtal.sample_rate == 44100
    assert xtal.duration == 293
    assert xtal.is_vbr is False
    assert xtal.remote_user == "peer_one"
    assert xtal.has_free_slot is True
    assert xtal.queue_length == 0
    assert xtal.upload_speed == 812345
    assert xtal.payload["remote_filename"].endswith("01 Xtal.flac")


@respx.mock
def test_search_marks_locked_results(provider):
    _mock_login()
    _mock_connected()
    _mock_full_search()
    results = provider.search("Aphex Twin - Xtal", SearchOptions(timeout=1))
    locked = [r for r in results if r.locked]
    assert len(locked) == 1
    assert locked[0].remote_user == "peer_three"


@respx.mock
def test_search_scores_relevance_without_preferring_a_version(provider):
    _mock_login()
    _mock_connected()
    _mock_full_search()
    results = provider.search("Aphex Twin - Xtal", SearchOptions(timeout=1))
    scored = {r.filename: r.relevance for r in results}
    # The live version is not penalised for being live; it scores on text alone
    # and it does mention both words of the query.
    live = scored["Aphex Twin - Xtal (live at Warehouse).mp3"]
    assert live > 0.5
    assert all(0.0 <= v <= 1.0 for v in scored.values())


@respx.mock
def test_search_when_soulseek_is_signed_out_raises_a_readable_error(provider):
    _mock_login()
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_DISCONNECTED)
    )
    with pytest.raises(SoulseekNotConnected) as exc:
        provider.search("anything", SearchOptions(timeout=1))
    assert "not signed in" in exc.value.message.lower()


@respx.mock
def test_search_survives_a_malformed_responses_payload(provider):
    _mock_login()
    _mock_connected()
    respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_COMPLETE)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}/responses").mock(
        return_value=httpx.Response(200, content=b"not json at all")
    )
    respx.delete(f"{API}/searches/{fx.SEARCH_ID}").mock(return_value=httpx.Response(204))
    assert provider.search("x", SearchOptions(timeout=1)) == []


@respx.mock
def test_search_can_be_cancelled(provider):
    _mock_login()
    _mock_connected()
    respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    respx.get(f"{API}/searches/{fx.SEARCH_ID}").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    respx.put(f"{API}/searches/{fx.SEARCH_ID}").mock(return_value=httpx.Response(204))
    respx.get(f"{API}/searches/{fx.SEARCH_ID}/responses").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.delete(f"{API}/searches/{fx.SEARCH_ID}").mock(return_value=httpx.Response(204))
    cancel = threading.Event()
    cancel.set()
    assert provider.search("x", SearchOptions(timeout=2), cancel=cancel) == []


# ---------------------------------------------------------------------------
# Selection and enqueueing
# ---------------------------------------------------------------------------


@respx.mock
def test_only_the_selected_file_is_enqueued(provider, tmp_path):
    _mock_login()
    _mock_connected()
    _mock_full_search()
    results = provider.search("Aphex Twin - Xtal", SearchOptions(timeout=1))

    # The user picks the mp3 from peer_two, not the highest scoring result.
    chosen = next(r for r in results if r.remote_user == "peer_two")
    job = provider.build_job(chosen, tmp_path)

    route = respx.post(f"{API}/transfers/downloads/batches").mock(
        return_value=httpx.Response(201)
    )
    provider.submit(job)

    body = json.loads(route.calls[0].request.content)
    assert body["username"] == "peer_two"
    assert body["files"] == [{"filename": "C:\\shared\\aphex_twin-xtal.mp3", "size": 7340032}]
    assert body["options"]["destination"] == str(tmp_path)
    assert job.status is TransferStatus.QUEUED
    assert len(route.calls) == 1


@respx.mock
def test_enqueue_failure_becomes_a_readable_job_error(provider, tmp_path):
    _mock_login()
    _mock_connected()
    _mock_full_search()
    results = provider.search("Aphex Twin - Xtal", SearchOptions(timeout=1))
    job = provider.build_job(results[0], tmp_path)

    respx.post(f"{API}/transfers/downloads/batches").mock(
        return_value=httpx.Response(500, text="boom")
    )
    provider.submit(job)
    assert job.status is TransferStatus.FAILED
    assert job.error
    assert "boom" not in job.error
    assert job.remedy


def test_job_without_enough_detail_fails_cleanly(provider, tmp_path):
    from trackfetch.models.common import TransferJob

    job = TransferJob(provider_id="slskd", destination=str(tmp_path), payload={})
    provider.submit(job)
    assert job.status is TransferStatus.FAILED
    assert "enough detail" in job.error


# ---------------------------------------------------------------------------
# Progress, completion and failure
# ---------------------------------------------------------------------------


def _queued_job(tmp_path, username="peer_one", filename=None):
    from trackfetch.models.common import TransferJob

    filename = filename or (
        "@@abcde\\Music\\Aphex Twin\\Selected Ambient Works 85-92\\01 Xtal.flac"
    )
    return TransferJob(
        provider_id="slskd",
        label="01 Xtal.flac",
        status=TransferStatus.QUEUED,
        destination=str(tmp_path),
        payload={"username": username, "remote_filename": filename, "size": 28311552},
    )


@respx.mock
def test_poll_reports_progress(provider, tmp_path):
    _mock_login()
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(200, json=fx.DOWNLOADS_GROUPED)
    )
    job = _queued_job(tmp_path)
    changed = provider.poll([job])
    assert changed == [job]
    assert job.status is TransferStatus.DOWNLOADING
    assert job.downloaded_bytes == 14155776
    assert job.total_bytes == 28311552
    assert round(job.percent) == 50
    assert job.speed_bps == 524288.0
    assert job.eta_seconds == 27


@respx.mock
def test_poll_reports_completion(provider, tmp_path):
    _mock_login()
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(200, json=fx.DOWNLOADS_COMPLETE)
    )
    job = _queued_job(tmp_path)
    provider.poll([job])
    assert job.status is TransferStatus.COMPLETE
    assert job.percent == 100.0
    assert job.completed_at is not None


@respx.mock
def test_poll_reports_failure_in_plain_english(provider, tmp_path):
    _mock_login()
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(200, json=fx.DOWNLOADS_FAILED)
    )
    job = _queued_job(tmp_path, "peer_two", "C:\\shared\\aphex_twin-xtal.mp3")
    provider.poll([job])
    assert job.status is TransferStatus.FAILED
    assert "no longer being shared" in job.error
    assert "different result" in job.remedy


@respx.mock
def test_poll_reports_remote_queue_position(provider, tmp_path):
    _mock_login()
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(200, json=fx.DOWNLOADS_QUEUED_REMOTELY)
    )
    job = _queued_job(tmp_path, "peer_two", "C:\\shared\\aphex_twin-xtal.mp3")
    provider.poll([job])
    assert job.status is TransferStatus.QUEUED
    assert job.payload["place_in_queue"] == 7


@respx.mock
def test_poll_survives_slskd_going_away(provider, tmp_path):
    _mock_login()
    respx.get(f"{API}/transfers/downloads").mock(side_effect=httpx.ConnectError("gone"))
    job = _queued_job(tmp_path)
    assert provider.poll([job]) == []
    assert job.status is TransferStatus.QUEUED


@respx.mock
def test_poll_ignores_jobs_from_other_providers(provider, tmp_path):
    from trackfetch.models.common import TransferJob

    other = TransferJob(provider_id="http", status=TransferStatus.DOWNLOADING)
    assert provider.poll([other]) == []


@respx.mock
def test_cancel_calls_slskd_and_marks_the_job(provider, tmp_path):
    _mock_login()
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(200, json=fx.DOWNLOADS_GROUPED)
    )
    job = _queued_job(tmp_path)
    provider.poll([job])
    route = respx.delete(
        f"{API}/transfers/downloads/peer_one/6f1d1f1c-1111-2222-3333-444455556666"
    ).mock(return_value=httpx.Response(204))
    assert provider.cancel(job) is True
    assert route.called
    assert job.status is TransferStatus.CANCELLED


def test_retry_puts_the_job_back_to_waiting(provider, tmp_path):
    job = _queued_job(tmp_path)
    job.status = TransferStatus.FAILED
    job.error = "gone"
    job.payload["transfer_id"] = "abc"
    provider.retry(job)
    assert job.status is TransferStatus.WAITING
    assert job.error == ""
    assert "transfer_id" not in job.payload
