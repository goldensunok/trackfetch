"""Regression tests for defects found in review and since fixed.

Each test names the fault it guards against, so a future change that
reintroduces it fails here rather than in front of the user.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import pytest
import respx

from tests.fixtures import slskd_payloads as fx
from trackfetch.models.common import (
    Capability,
    ProviderInfo,
    SearchCandidate,
    TransferJob,
    TransferStatus,
)
from trackfetch.providers.base import Provider, ProviderContext
from trackfetch.providers.slskd import SlskdProvider
from trackfetch.services.transfer_manager import TransferManager

BASE = "http://localhost:5030"
API = f"{BASE}/api/v0"


# ---------------------------------------------------------------------------
# Soulseek: a transfer slskd forgets must not strand the job
# ---------------------------------------------------------------------------


class _NoSupervisor:
    def find_installation(self):
        from trackfetch.services.slskd_supervisor import SlskdInstallation

        return SlskdInstallation(None, "none")

    def start(self, installation=None, use_managed_config=True):
        return False

    def wait_until_ready(self, base_url=None, timeout=45, on_progress=None):
        return False

    def desktop_soulseek_running(self):
        return False

    def describe(self):
        return {}


@pytest.fixture
def slskd(slskd_ctx):
    provider = SlskdProvider(slskd_ctx, supervisor=_NoSupervisor())
    yield provider
    provider.close()


def _login():
    respx.post(f"{API}/session").mock(return_value=httpx.Response(200, json=fx.SESSION_LOGIN))
    respx.get(f"{API}/session/enabled").mock(return_value=httpx.Response(200, json=True))


def _queued_job(tmp_path, submitted_at=None):
    job = TransferJob(
        provider_id="slskd",
        label="01 Xtal.flac",
        status=TransferStatus.QUEUED,
        destination=str(tmp_path),
        payload={
            "username": "peer_one",
            "remote_filename": "C:\\music\\01 Xtal.flac",
            "size": 100,
        },
    )
    if submitted_at is not None:
        job.payload["submitted_at"] = submitted_at
    return job


@respx.mock
def test_a_transfer_slskd_has_forgotten_eventually_fails_rather_than_hanging(
    slskd, tmp_path
):
    """Was: the job sat in Queued for ever with no button that could rescue it."""
    _login()
    respx.get(f"{API}/transfers/downloads").mock(return_value=httpx.Response(200, json=[]))

    stale = time.time() - (SlskdProvider.MISSING_TRANSFER_TIMEOUT + 5)
    job = _queued_job(tmp_path, submitted_at=stale)
    changed = slskd.poll([job])

    assert changed == [job]
    assert job.status is TransferStatus.FAILED
    assert "no longer has a record" in job.error
    assert "Retry" in job.remedy


@respx.mock
def test_a_transfer_that_has_only_just_been_asked_for_is_left_alone(slskd, tmp_path):
    _login()
    respx.get(f"{API}/transfers/downloads").mock(return_value=httpx.Response(200, json=[]))
    job = _queued_job(tmp_path, submitted_at=time.time())
    assert slskd.poll([job]) == []
    assert job.status is TransferStatus.QUEUED


@respx.mock
def test_a_job_with_no_submission_time_gets_one_rather_than_failing(slskd, tmp_path):
    _login()
    respx.get(f"{API}/transfers/downloads").mock(return_value=httpx.Response(200, json=[]))
    job = _queued_job(tmp_path)
    assert slskd.poll([job]) == []
    assert job.status is TransferStatus.QUEUED
    assert isinstance(job.payload.get("submitted_at"), float)


@respx.mock
def test_a_stale_record_does_not_overwrite_a_retried_job(slskd, tmp_path):
    """Was: a retried job matched the old errored record and failed again."""
    _login()
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "username": "peer_one",
                    "directories": [
                        {
                            "directory": "C:\\music",
                            "files": [
                                {
                                    "id": "old-attempt",
                                    "username": "peer_one",
                                    "filename": "C:\\music\\01 Xtal.flac",
                                    "size": 100,
                                    "state": "Completed, Errored",
                                    "exception": "rejected",
                                },
                                {
                                    "id": "new-attempt",
                                    "username": "peer_one",
                                    "filename": "C:\\music\\01 Xtal.flac",
                                    "size": 100,
                                    "state": "InProgress",
                                    "bytesTransferred": 50,
                                },
                            ],
                        }
                    ],
                }
            ],
        )
    )
    job = _queued_job(tmp_path, submitted_at=time.time())
    job.payload["transfer_id"] = "new-attempt"
    slskd.poll([job])
    assert job.status is TransferStatus.DOWNLOADING
    assert job.downloaded_bytes == 50


@respx.mock
def test_two_jobs_for_the_same_file_do_not_share_one_record(slskd, tmp_path):
    _login()
    respx.get(f"{API}/transfers/downloads").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "username": "peer_one",
                    "directories": [
                        {
                            "directory": "C:\\music",
                            "files": [
                                {
                                    "id": "only-one",
                                    "username": "peer_one",
                                    "filename": "C:\\music\\01 Xtal.flac",
                                    "size": 100,
                                    "state": "InProgress",
                                    "bytesTransferred": 25,
                                }
                            ],
                        }
                    ],
                }
            ],
        )
    )
    first = _queued_job(tmp_path, submitted_at=time.time())
    second = _queued_job(tmp_path, submitted_at=time.time())
    slskd.poll([first, second])
    assert first.payload.get("transfer_id") == "only-one"
    assert second.payload.get("transfer_id") != "only-one"


@respx.mock
def test_submitting_stamps_the_time_and_clears_the_old_transfer_id(slskd, tmp_path):
    _login()
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_CONNECTED)
    )
    respx.post(f"{API}/transfers/downloads/batches").mock(return_value=httpx.Response(201))
    job = _queued_job(tmp_path)
    job.payload["transfer_id"] = "gone"
    slskd.submit(job)
    assert "transfer_id" not in job.payload
    assert isinstance(job.payload.get("submitted_at"), float)


# ---------------------------------------------------------------------------
# The queue: removing a row must be final
# ---------------------------------------------------------------------------


class SlowLocalProvider(Provider):
    info = ProviderInfo(
        provider_id="local",
        display_name="Local test provider",
        capabilities=frozenset({Capability.DOWNLOAD, Capability.PROGRESS, Capability.CANCEL}),
    )

    def __init__(self, ctx):
        super().__init__(ctx)
        self.release = threading.Event()

    def test_connection(self):
        return self._status_ok()

    def download(self, job, on_progress=None, cancel=None):
        self.release.wait(timeout=10)
        job.status = TransferStatus.COMPLETE
        job.total_bytes = 10
        job.downloaded_bytes = 10
        return job


class StubRegistry:
    def __init__(self, providers):
        self._providers = {p.provider_id: p for p in providers}

    def get(self, provider_id):
        return self._providers.get(provider_id)

    def all(self):
        return list(self._providers.values())

    def failed(self):
        return {}


def _ctx(settings, secrets, provider_id):
    return ProviderContext(
        provider_id=provider_id,
        settings=settings,
        provider_settings=settings.provider(provider_id),
        secrets=secrets,
    )


def _wait(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_a_removed_transfer_does_not_come_back(repos, settings, secrets, tmp_path):
    """Was: the worker's finally block re-inserted the row after deletion."""
    provider = SlowLocalProvider(_ctx(settings, secrets, "local"))
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    candidate = SearchCandidate(provider_id="local", filename="slow.bin", size=10)
    jobs, _ = manager.add_candidates([candidate], tmp_path)
    job_id = jobs[0].id

    manager.start()
    try:
        assert _wait(lambda: manager.job(job_id).status is TransferStatus.DOWNLOADING)
        manager.remove(job_id)
        assert repos.transfers.get(job_id) is None

        provider.release.set()
        time.sleep(1.0)
        assert repos.transfers.get(job_id) is None
        assert manager.job(job_id) is None
    finally:
        provider.release.set()
        manager.stop()


class StubbornProvider(Provider):
    """Pretends to pause, then refuses to resume, like an unreachable client."""

    info = ProviderInfo(
        provider_id="external",
        display_name="External test provider",
        capabilities=frozenset(
            {
                Capability.ADD_JOB,
                Capability.QUEUE,
                Capability.PROGRESS,
                Capability.PAUSE,
                Capability.RESUME,
            }
        ),
    )

    def __init__(self, ctx):
        super().__init__(ctx)
        self.submits = 0

    def test_connection(self):
        return self._status_ok()

    def submit(self, job):
        self.submits += 1
        job.status = TransferStatus.QUEUED
        return job

    def pause(self, job):
        job.status = TransferStatus.PAUSED
        return True

    def resume(self, job):
        return False  # the client is unreachable

    def poll(self, jobs):
        return []


def test_a_failed_resume_does_not_submit_the_job_twice(
    repos, settings, secrets, tmp_path
):
    """Was: resume flipped to Waiting regardless, so the queue resubmitted."""
    provider = StubbornProvider(_ctx(settings, secrets, "external"))
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    candidate = SearchCandidate(
        provider_id="external", filename="thing.nzb", payload={"source": "thing.nzb"}
    )
    jobs, _ = manager.add_candidates([candidate], tmp_path)
    job_id = jobs[0].id

    manager.start()
    try:
        assert _wait(lambda: manager.job(job_id).status is TransferStatus.QUEUED)
        assert provider.submits == 1
        manager.pause(job_id)
        assert manager.job(job_id).status is TransferStatus.PAUSED

        assert manager.resume(job_id) is False
        time.sleep(1.0)
        assert manager.job(job_id).status is TransferStatus.PAUSED
        assert provider.submits == 1
    finally:
        manager.stop()


def test_resume_is_refused_by_a_provider_that_cannot_do_it(
    repos, settings, secrets, tmp_path
):
    provider = SlowLocalProvider(_ctx(settings, secrets, "local"))
    manager = TransferManager(StubRegistry([provider]), repos, settings)
    job = TransferJob(provider_id="local", destination=str(tmp_path))
    manager.add_job(job)
    assert manager.resume(job.id) is False


# ---------------------------------------------------------------------------
# Web downloads
# ---------------------------------------------------------------------------


@pytest.fixture
def http_provider(settings, secrets):
    from trackfetch.providers.http import HttpProvider

    provider = HttpProvider(_ctx(settings, secrets, "http"))
    yield provider
    provider.close()


@respx.mock
def test_a_resumed_download_does_not_double_count_its_size(http_provider, tmp_path):
    """Was: Content-Range total plus the bytes on disk, so 4096 became 5096."""
    part = tmp_path / "a.bin.part"
    part.write_bytes(b"y" * 1000)
    respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(
            206,
            content=b"z" * 3096,
            headers={"Content-Range": "bytes 1000-4095/4096", "Content-Length": "3096"},
        )
    )
    job = TransferJob(
        provider_id="http",
        destination=str(tmp_path),
        payload={"url": "https://example.com/a.bin", "filename": "a.bin"},
    )
    http_provider.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert job.total_bytes == 4096
    assert job.downloaded_bytes == 4096
    assert (tmp_path / "a.bin").stat().st_size == 4096


@respx.mock
def test_cancelling_a_waiting_download_does_not_break_the_next_attempt(
    http_provider, tmp_path
):
    """Was: the cancel flag stuck around and killed the first retry."""
    respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(200, content=b"hello")
    )
    job = TransferJob(
        provider_id="http",
        destination=str(tmp_path),
        payload={"url": "https://example.com/a.bin", "filename": "a.bin"},
    )
    # Cancelled while it was only waiting, so nothing was running to notice.
    http_provider.cancel(job)
    assert job.status is TransferStatus.CANCELLED

    http_provider.retry(job)
    http_provider.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert (tmp_path / "a.bin").read_bytes() == b"hello"


# ---------------------------------------------------------------------------
# Files without an extension
# ---------------------------------------------------------------------------


@respx.mock
def test_a_file_with_no_extension_is_not_turned_into_a_folder(settings, secrets, tmp_path):
    """Was: CHANGELOG was saved as CHANGELOG/CHANGELOG."""
    from trackfetch.providers.webdav import WebdavProvider

    settings.provider("webdav").config.update({"base_url": "https://dav.example.com"})
    provider = WebdavProvider(_ctx(settings, secrets, "webdav"))
    respx.get("https://dav.example.com/CHANGELOG").mock(
        return_value=httpx.Response(200, content=b"notes")
    )
    job = TransferJob(
        provider_id="webdav",
        destination=str(tmp_path),
        payload={
            "remote_path": "CHANGELOG",
            "target_path": str(tmp_path / "CHANGELOG"),
        },
    )
    provider.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert (tmp_path / "CHANGELOG").is_file()
    assert not (tmp_path / "CHANGELOG" / "CHANGELOG").exists()
    provider.close()


def test_the_same_holds_for_syncthing(settings, secrets, tmp_path):
    from trackfetch.providers.syncthing import SyncthingProvider

    source = tmp_path / "synced"
    source.mkdir()
    (source / "CHANGELOG").write_bytes(b"notes")
    target_dir = tmp_path / "out"
    target_dir.mkdir()

    provider = SyncthingProvider(_ctx(settings, secrets, "syncthing"))
    job = TransferJob(
        provider_id="syncthing",
        destination=str(target_dir),
        payload={
            "local_path": str(source / "CHANGELOG"),
            "target_path": str(target_dir / "CHANGELOG"),
        },
    )
    provider.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert (target_dir / "CHANGELOG").is_file()
    provider.close()
