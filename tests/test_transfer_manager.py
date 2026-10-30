"""Tests for the unified queue: concurrency, resume, duplicates and failures."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from trackfetch.models.common import (
    Capability,
    DuplicatePolicy,
    ProviderInfo,
    SearchCandidate,
    TransferJob,
    TransferStatus,
)
from trackfetch.providers.base import Provider, ProviderContext
from trackfetch.services.destinations import DuplicateAction
from trackfetch.services.transfer_manager import TransferManager


class StubRegistry:
    def __init__(self, providers):
        self._providers = {p.provider_id: p for p in providers}

    def get(self, provider_id):
        return self._providers.get(provider_id)

    def all(self):
        return list(self._providers.values())

    def failed(self):
        return {}


def make_ctx(settings, secrets, provider_id):
    return ProviderContext(
        provider_id=provider_id,
        settings=settings,
        provider_settings=settings.provider(provider_id),
        secrets=secrets,
    )


class LocalProvider(Provider):
    """Downloads bytes itself, like the web and SFTP providers do."""

    info = ProviderInfo(
        provider_id="local",
        display_name="Local test provider",
        capabilities=frozenset(
            {Capability.DOWNLOAD, Capability.PROGRESS, Capability.CANCEL, Capability.PAUSE}
        ),
    )

    def __init__(self, ctx, fail_labels=(), delay=0.01):
        super().__init__(ctx)
        self.fail_labels = set(fail_labels)
        self.delay = delay
        self.started: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def test_connection(self):
        return self._status_ok()

    def download(self, job, on_progress=None, cancel=None):
        with self._lock:
            self.started.append(job.label)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if job.label in self.fail_labels:
                from trackfetch.providers.errors import ConnectionFailed

                raise ConnectionFailed("That did not work.", "Try again.")
            total = 1000
            for step in range(1, 6):
                if cancel is not None and cancel.is_set():
                    job.status = TransferStatus.CANCELLED
                    return job
                time.sleep(self.delay)
                if on_progress:
                    on_progress(step * 200, total, 1000.0)
            target = Path(job.payload.get("target_path") or job.destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * total)
            job.total_bytes = total
            job.downloaded_bytes = total
            job.status = TransferStatus.COMPLETE
            return job
        finally:
            with self._lock:
                self.concurrent -= 1


class ExternalProvider(Provider):
    """Hands the work to another program, like slskd or qBittorrent do."""

    info = ProviderInfo(
        provider_id="external",
        display_name="External test provider",
        capabilities=frozenset(
            {Capability.ADD_JOB, Capability.QUEUE, Capability.PROGRESS, Capability.CANCEL}
        ),
    )

    def __init__(self, ctx, fail_submit=()):
        super().__init__(ctx)
        self.submitted: list[str] = []
        self.fail_submit = set(fail_submit)
        self.progress_steps: dict[str, int] = {}

    def test_connection(self):
        return self._status_ok()

    def submit(self, job):
        if job.label in self.fail_submit:
            from trackfetch.providers.errors import ConnectionFailed

            raise ConnectionFailed("Not running.", "Start it and try again.")
        self.submitted.append(job.label)
        job.status = TransferStatus.QUEUED
        job.remote_id = f"remote-{job.label}"
        return job

    def poll(self, jobs):
        changed = []
        for job in jobs:
            step = self.progress_steps.get(job.id, 0) + 1
            self.progress_steps[job.id] = step
            job.total_bytes = 1000
            job.downloaded_bytes = min(1000, step * 500)
            job.status = (
                TransferStatus.COMPLETE
                if job.downloaded_bytes >= 1000
                else TransferStatus.DOWNLOADING
            )
            changed.append(job)
        return changed


@pytest.fixture
def local_provider(settings, secrets):
    return LocalProvider(make_ctx(settings, secrets, "local"))


@pytest.fixture
def external_provider(settings, secrets):
    return ExternalProvider(make_ctx(settings, secrets, "external"))


def candidate(name, provider_id="local", size=1000):
    c = SearchCandidate(
        provider_id=provider_id,
        filename=name,
        size=size,
        payload={"remote_filename": f"remote/{name}"},
    )
    return c


def wait_for(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# Queueing and downloading
# ---------------------------------------------------------------------------


def test_selected_items_become_queue_entries(repos, settings, local_provider, tmp_path):
    registry = StubRegistry([local_provider])
    manager = TransferManager(registry, repos, settings)
    jobs, skipped = manager.add_candidates([candidate("a.mp3"), candidate("b.mp3")], tmp_path)
    assert len(jobs) == 2
    assert skipped == []
    assert all(j.status is TransferStatus.WAITING for j in jobs)
    assert repos.transfers.get(jobs[0].id) is not None


def test_downloads_run_and_complete(repos, settings, local_provider, tmp_path):
    registry = StubRegistry([local_provider])
    manager = TransferManager(registry, repos, settings)
    manager.add_candidates([candidate("a.mp3")], tmp_path)
    manager.start()
    try:
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.COMPLETE)
    finally:
        manager.stop()
    assert (tmp_path / "a.mp3").exists()
    assert manager.jobs()[0].percent == 100.0


def test_simultaneous_transfer_limit_is_respected(repos, settings, secrets, tmp_path):
    settings.max_concurrent_transfers = 2
    provider = LocalProvider(make_ctx(settings, secrets, "local"), delay=0.05)
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    manager.add_candidates([candidate(f"f{i}.mp3") for i in range(6)], tmp_path)
    manager.start()
    try:
        assert wait_for(
            lambda: all(j.status.is_terminal for j in manager.jobs()), timeout=30
        )
    finally:
        manager.stop()
    assert provider.max_concurrent <= 2
    assert len(provider.started) == 6


def test_one_failure_does_not_stop_the_batch(repos, settings, secrets, tmp_path):
    provider = LocalProvider(make_ctx(settings, secrets, "local"), fail_labels={"bad.mp3"})
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    manager.add_candidates(
        [candidate("good1.mp3"), candidate("bad.mp3"), candidate("good2.mp3")], tmp_path
    )
    manager.start()
    try:
        assert wait_for(lambda: all(j.status.is_terminal for j in manager.jobs()), timeout=30)
    finally:
        manager.stop()
    statuses = {j.label: j.status for j in manager.jobs()}
    assert statuses["good1.mp3"] is TransferStatus.COMPLETE
    assert statuses["good2.mp3"] is TransferStatus.COMPLETE
    assert statuses["bad.mp3"] is TransferStatus.FAILED
    failed = next(j for j in manager.jobs() if j.status is TransferStatus.FAILED)
    assert failed.error == "That did not work."
    assert failed.remedy == "Try again."


def test_a_failed_transfer_can_be_retried(repos, settings, secrets, tmp_path):
    provider = LocalProvider(make_ctx(settings, secrets, "local"), fail_labels={"x.mp3"})
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    jobs, _ = manager.add_candidates([candidate("x.mp3")], tmp_path)
    manager.start()
    try:
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.FAILED)
        provider.fail_labels.clear()
        manager.retry(jobs[0].id)
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.COMPLETE)
    finally:
        manager.stop()


def test_a_transfer_can_be_cancelled(repos, settings, secrets, tmp_path):
    provider = LocalProvider(make_ctx(settings, secrets, "local"), delay=0.3)
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    jobs, _ = manager.add_candidates([candidate("slow.mp3")], tmp_path)
    manager.start()
    try:
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.DOWNLOADING)
        manager.cancel(jobs[0].id)
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.CANCELLED)
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# External providers
# ---------------------------------------------------------------------------


def test_external_jobs_are_submitted_and_polled(repos, settings, external_provider, tmp_path):
    manager = TransferManager(StubRegistry([external_provider]), repos, settings)
    manager.add_candidates([candidate("t.iso", provider_id="external")], tmp_path)
    manager.start()
    try:
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.COMPLETE, timeout=30)
    finally:
        manager.stop()
    assert external_provider.submitted == ["t.iso"]


def test_a_failing_external_client_produces_a_readable_error(
    repos, settings, secrets, tmp_path
):
    provider = ExternalProvider(make_ctx(settings, secrets, "external"), fail_submit={"t.iso"})
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    manager.add_candidates([candidate("t.iso", provider_id="external")], tmp_path)
    manager.start()
    try:
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.FAILED)
    finally:
        manager.stop()
    assert manager.jobs()[0].error == "Not running."


def test_a_missing_provider_fails_the_job_readably(repos, settings, tmp_path):
    manager = TransferManager(StubRegistry([]), repos, settings)
    job = TransferJob(provider_id="nowhere", label="x", destination=str(tmp_path))
    manager.add_job(job)
    manager.start()
    try:
        assert wait_for(lambda: manager.jobs()[0].status is TransferStatus.FAILED)
    finally:
        manager.stop()
    assert "not available" in manager.jobs()[0].error


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_an_already_completed_item_is_not_downloaded_again(
    repos, settings, local_provider, tmp_path
):
    manager = TransferManager(StubRegistry([local_provider]), repos, settings)
    first = candidate("a.mp3")
    jobs, _ = manager.add_candidates([first], tmp_path)
    repos.completions.record(
        jobs[0].payload["fingerprint"], "local", "a.mp3", str(tmp_path / "a.mp3"), 1000
    )
    again, skipped = manager.add_candidates([candidate("a.mp3")], tmp_path)
    assert again == []
    assert len(skipped) == 1
    assert "Already downloaded" in skipped[0][1]


def test_an_existing_file_is_renamed_rather_than_overwritten(
    repos, settings, local_provider, tmp_path
):
    (tmp_path / "a.mp3").write_bytes(b"original")
    settings.duplicate_policy = DuplicatePolicy.RENAME
    manager = TransferManager(StubRegistry([local_provider]), repos, settings)
    jobs, _ = manager.add_candidates([candidate("a.mp3")], tmp_path)
    assert Path(jobs[0].payload["target_path"]).name == "a (2).mp3"
    assert (tmp_path / "a.mp3").read_bytes() == b"original"


def test_skip_policy_leaves_the_existing_file_alone(
    repos, settings, local_provider, tmp_path
):
    (tmp_path / "a.mp3").write_bytes(b"original")
    settings.duplicate_policy = DuplicatePolicy.SKIP
    manager = TransferManager(StubRegistry([local_provider]), repos, settings)
    jobs, skipped = manager.add_candidates([candidate("a.mp3")], tmp_path)
    assert jobs == []
    assert len(skipped) == 1


def test_the_user_can_be_asked_what_to_do(repos, settings, local_provider, tmp_path):
    (tmp_path / "a.mp3").write_bytes(b"original")
    settings.duplicate_policy = DuplicatePolicy.ASK
    asked: list[str] = []

    def resolver(job, path):
        asked.append(path.name)
        return DuplicateAction.SKIP

    manager = TransferManager(
        StubRegistry([local_provider]), repos, settings, duplicate_resolver=resolver
    )
    jobs, skipped = manager.add_candidates([candidate("a.mp3")], tmp_path)
    assert asked == ["a.mp3"]
    assert jobs == []
    assert len(skipped) == 1


# ---------------------------------------------------------------------------
# Resume after closing the application
# ---------------------------------------------------------------------------


def test_the_queue_is_restored_after_a_restart(repos, settings, local_provider, tmp_path):
    manager = TransferManager(StubRegistry([local_provider]), repos, settings)
    manager.add_candidates([candidate("a.mp3"), candidate("b.mp3")], tmp_path)

    # A fresh manager, as if the program had been closed and reopened.
    reopened = TransferManager(StubRegistry([local_provider]), repos, settings)
    restored = reopened.load_existing()
    assert len(restored) == 2
    assert {j.label for j in restored} == {"a.mp3", "b.mp3"}


def test_a_transfer_left_mid_download_goes_back_to_waiting(
    repos, settings, local_provider, tmp_path
):
    job = TransferJob(
        provider_id="local",
        label="half.mp3",
        status=TransferStatus.DOWNLOADING,
        downloaded_bytes=500,
        total_bytes=1000,
        destination=str(tmp_path / "half.mp3"),
    )
    repos.transfers.save(job)
    reopened = TransferManager(StubRegistry([local_provider]), repos, settings)
    restored = reopened.load_existing()
    assert restored[0].status is TransferStatus.WAITING


def test_finished_entries_can_be_cleared(repos, settings, local_provider, tmp_path):
    manager = TransferManager(StubRegistry([local_provider]), repos, settings)
    jobs, _ = manager.add_candidates([candidate("a.mp3")], tmp_path)
    jobs[0].status = TransferStatus.COMPLETE
    repos.transfers.save(jobs[0])
    manager._jobs[jobs[0].id] = jobs[0]
    manager.clear_finished()
    assert manager.jobs() == []


def test_summary_counts_by_status(repos, settings, local_provider, tmp_path):
    manager = TransferManager(StubRegistry([local_provider]), repos, settings)
    manager.add_candidates([candidate("a.mp3"), candidate("b.mp3")], tmp_path)
    summary = manager.summary()
    assert summary["Waiting"] == 2
    assert summary["Complete"] == 0
