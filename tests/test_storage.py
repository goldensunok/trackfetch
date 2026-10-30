"""Tests for the local store: persistence, resume and concurrent access."""

from __future__ import annotations

import random
import threading
import time

import pytest

from trackfetch.models.common import (
    BatchItem,
    InputKind,
    SearchCandidate,
    SearchQuery,
    TransferJob,
    TransferStatus,
)
from trackfetch.storage.db import Database
from trackfetch.storage.repositories import Repositories


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_a_batch_survives_a_restart(tmp_path):
    path = tmp_path / "t.sqlite3"
    db = Database(path)
    repos = Repositories(db)
    batch_id = repos.batches.create_batch("my list")
    repos.batches.add_items(
        batch_id,
        [
            BatchItem(raw_text="one", kind=InputKind.QUERY),
            BatchItem(raw_text="https://example.com/x.zip", kind=InputKind.URL),
        ],
    )
    db.close()

    db2 = Database(path)
    repos2 = Repositories(db2)
    items = repos2.batches.items(batch_id)
    assert [i.raw_text for i in items] == ["one", "https://example.com/x.zip"]
    assert items[1].kind is InputKind.URL
    assert repos2.batches.latest_batch_id() == batch_id
    db2.close()


def test_candidates_and_selection_are_persisted(repos):
    batch_id = repos.batches.create_batch()
    item = BatchItem(raw_text="query")
    repos.batches.add_items(batch_id, [item])
    query = SearchQuery(batch_item_id=item.id, provider_id="slskd", text="query")
    repos.queries.save(query)

    candidates = [
        SearchCandidate(
            query_id=query.id,
            provider_id="slskd",
            filename=f"file{i}.mp3",
            size=1000 + i,
            bitrate=320,
            relevance=0.5 + i / 100,
            payload={"username": "peer", "remote_filename": f"C:\\a\\file{i}.mp3"},
        )
        for i in range(3)
    ]
    repos.candidates.save_many(candidates)

    stored = repos.candidates.for_query(query.id)
    assert len(stored) == 3
    assert stored[0].payload["username"] == "peer"

    repos.candidates.set_selected(candidates[1].id, True)
    selected = repos.candidates.selected_for_batch(batch_id)
    assert len(selected) == 1
    assert selected[0].filename == "file1.mp3"


def test_clearing_selection_for_an_item(repos):
    batch_id = repos.batches.create_batch()
    item = BatchItem(raw_text="q")
    repos.batches.add_items(batch_id, [item])
    query = SearchQuery(batch_item_id=item.id, provider_id="slskd")
    repos.queries.save(query)
    candidate = SearchCandidate(query_id=query.id, filename="a.mp3", selected=True)
    repos.candidates.save_many([candidate])
    repos.candidates.clear_selection_for_item(item.id)
    assert repos.candidates.selected_for_batch(batch_id) == []


def test_transfer_round_trip(repos):
    job = TransferJob(
        provider_id="slskd",
        label="thing.flac",
        status=TransferStatus.DOWNLOADING,
        total_bytes=1000,
        downloaded_bytes=250,
        payload={"username": "peer"},
    )
    repos.transfers.save(job)
    loaded = repos.transfers.get(job.id)
    assert loaded is not None
    assert loaded.label == "thing.flac"
    assert loaded.status is TransferStatus.DOWNLOADING
    assert loaded.payload["username"] == "peer"
    assert loaded.percent == 25.0


def test_completions_prevent_repeat_downloads(repos):
    repos.completions.record("abc", "slskd", "song.flac", "/tmp/song.flac", 100)
    assert repos.completions.has("abc") is True
    assert repos.completions.has("other") is False
    repos.completions.forget("abc")
    assert repos.completions.has("abc") is False


def test_events_are_capped(repos):
    for i in range(60):
        repos.events.add(f"event {i}", "info", "slskd")
    recent = repos.events.recent(limit=10)
    assert len(recent) == 10
    assert recent[0]["message"] == "event 59"


def test_bad_json_in_a_payload_does_not_crash_loading(repos, db):
    job = TransferJob(provider_id="http", label="x")
    repos.transfers.save(job)
    db.execute("UPDATE transfers SET payload_json = ? WHERE id = ?", ("not json", job.id))
    loaded = repos.transfers.get(job.id)
    assert loaded is not None
    assert loaded.payload == {}


def test_unknown_status_falls_back_safely(repos, db):
    job = TransferJob(provider_id="http", label="x")
    repos.transfers.save(job)
    db.execute("UPDATE transfers SET status = ? WHERE id = ?", ("Nonsense", job.id))
    assert repos.transfers.get(job.id).status is TransferStatus.WAITING


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_many_threads_can_write_at_once(tmp_path):
    db = Database(tmp_path / "concurrent.sqlite3")
    repos = Repositories(db)
    errors: list[Exception] = []
    written: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            for n in range(25):
                job = TransferJob(
                    provider_id="slskd",
                    label=f"worker{index}-{n}",
                    total_bytes=n * 10,
                )
                repos.transfers.save(job)
                with lock:
                    written.append(job.id)
                # Interleave reads with writes, which is what the real
                # application does while the queue is polling.
                repos.transfers.active()
                time.sleep(random.uniform(0, 0.001))
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert len(written) == 200
    assert len(repos.transfers.all()) == 200
    db.close()


def test_concurrent_updates_to_the_same_row_are_serialised(tmp_path):
    db = Database(tmp_path / "same-row.sqlite3")
    repos = Repositories(db)
    job = TransferJob(provider_id="http", label="shared", total_bytes=1000)
    repos.transfers.save(job)
    errors: list[Exception] = []

    def bump() -> None:
        try:
            for n in range(50):
                job.downloaded_bytes = n * 10
                repos.transfers.save(job)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=bump) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert repos.transfers.get(job.id) is not None
    db.close()


def test_reads_and_writes_from_different_threads_use_separate_connections(tmp_path):
    db = Database(tmp_path / "threads.sqlite3")
    seen: list[int] = []

    def reader() -> None:
        seen.append(len(db.query("SELECT * FROM transfers")))

    db.execute(
        "INSERT INTO transfers(id, provider_id, created_at, updated_at)"
        " VALUES('a', 'http', 1, 1)"
    )
    thread = threading.Thread(target=reader)
    thread.start()
    thread.join(timeout=10)
    assert seen == [1]
    db.close()


def test_an_in_memory_database_is_shared_between_threads():
    db = Database(":memory:")
    db.execute(
        "INSERT INTO transfers(id, provider_id, created_at, updated_at)"
        " VALUES('a', 'http', 1, 1)"
    )
    found: list[int] = []

    def reader() -> None:
        found.append(len(db.query("SELECT * FROM transfers")))

    thread = threading.Thread(target=reader)
    thread.start()
    thread.join(timeout=10)
    assert found == [1]
    db.close()


def test_app_state_key_value(db):
    db.set_state("last_batch", "abc")
    assert db.get_state("last_batch") == "abc"
    db.set_state("last_batch", "def")
    assert db.get_state("last_batch") == "def"
    assert db.get_state("missing", "fallback") == "fallback"
