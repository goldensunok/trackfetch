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


# ---------------------------------------------------------------------------
# The Soulseek helper must be stopped on exit
#
# This is the one that cost an afternoon. Trackfetch started slskd and never
# stopped it, so after Trackfetch was closed the helper kept running, kept hold
# of the Soulseek account, and knocked the user's ordinary Soulseek program off
# within a second of it signing in.
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(self, pid=4242):
        self.pid = pid
        self.terminated = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def _supervisor(tmp_path):
    from trackfetch.services.slskd_supervisor import SlskdSupervisor

    return SlskdSupervisor(managed_dir=tmp_path / "slskd")


def test_stop_terminates_a_helper_trackfetch_started(tmp_path):
    supervisor = _supervisor(tmp_path)
    process = _FakeProcess()
    supervisor._process = process

    assert supervisor.stop() is True
    assert process.terminated is True


def test_stop_leaves_alone_a_helper_the_user_started(tmp_path):
    """An slskd the user runs themselves is never touched. That was promised."""
    supervisor = _supervisor(tmp_path)
    supervisor.adopted_existing = True
    process = _FakeProcess()
    supervisor._process = process

    assert supervisor.stop() is False
    assert process.terminated is False


def test_start_records_the_pid_so_a_later_run_can_stop_it(tmp_path, monkeypatch):
    from trackfetch.services import slskd_supervisor as sup
    from trackfetch.services.slskd_supervisor import SlskdInstallation

    supervisor = _supervisor(tmp_path)
    executable = tmp_path / "bin" / "slskd"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(supervisor, "is_running", lambda base_url=None: False)
    monkeypatch.setattr(sup.subprocess, "Popen", lambda *a, **k: _FakeProcess(pid=777))

    assert supervisor.start(SlskdInstallation(executable, "managed")) is True
    assert supervisor.pid_file.exists()

    # A brand new supervisor, as a later run of Trackfetch would have, still
    # knows which process to stop.
    fresh = _supervisor(tmp_path)
    monkeypatch.setattr(sup, "_pid_is_slskd", lambda pid: pid == 777)
    assert fresh._recorded_pid() == 777
    assert fresh.started_by_trackfetch is True

    stopped = {}
    monkeypatch.setattr(sup, "_terminate_pid", lambda pid, timeout=10.0: stopped.setdefault(pid, True))
    assert fresh.stop() is True
    assert stopped == {777: True}
    assert not fresh.pid_file.exists()


def test_a_recycled_pid_is_never_signalled(tmp_path, monkeypatch):
    """If the recorded number now belongs to something else, leave it alone."""
    from trackfetch.services import slskd_supervisor as sup

    supervisor = _supervisor(tmp_path)
    supervisor._record_pid(31337, tmp_path / "slskd")
    monkeypatch.setattr(sup, "_pid_is_slskd", lambda pid: False)

    killed = []
    monkeypatch.setattr(sup, "_terminate_pid", lambda pid, timeout=10.0: killed.append(pid))
    assert supervisor.stop() is False
    assert killed == []


def test_shutdown_stops_the_helper(tmp_path, settings, secrets):
    """The app's own shutdown path must reach the supervisor."""
    from trackfetch.app import TrackfetchApp
    from trackfetch.config.settings import SettingsStore
    from trackfetch.storage.db import Database

    class _Store(SettingsStore):
        def __init__(self):
            pass

        def load(self):
            return settings

        def save(self, value):
            return None

    app = TrackfetchApp(
        settings_store=_Store(),
        secrets=secrets,
        database=Database(tmp_path / "app.sqlite3"),
        configure_logging=False,
    )
    provider = app.registry.get("slskd")
    assert provider is not None
    supervisor = _supervisor(tmp_path)
    process = _FakeProcess()
    supervisor._process = process
    provider._supervisor = supervisor

    assert app.soulseek_helper_stoppable() is True
    app.shutdown()
    assert process.terminated is True


def test_shutdown_does_not_build_a_provider_just_to_ask(tmp_path, settings, secrets):
    """Creating providers during shutdown would be the wrong thing to do."""
    from trackfetch.app import TrackfetchApp
    from trackfetch.config.settings import SettingsStore
    from trackfetch.storage.db import Database

    class _Store(SettingsStore):
        def __init__(self):
            pass

        def load(self):
            return settings

        def save(self, value):
            return None

    app = TrackfetchApp(
        settings_store=_Store(),
        secrets=secrets,
        database=Database(tmp_path / "app.sqlite3"),
        configure_logging=False,
    )
    assert app.registry.existing("slskd") is None
    assert app.soulseek_helper_stoppable() is False
    assert app.registry.existing("slskd") is None
    app.shutdown()


def test_the_written_config_is_patient_about_timeouts(tmp_path):
    """Short timeouts were showing up in slskd's log as disconnections."""
    from trackfetch.services.slskd_supervisor import (
        CONNECT_TIMEOUT_MS,
        INACTIVITY_TIMEOUT_MS,
    )

    supervisor = _supervisor(tmp_path)
    path = supervisor.write_config(
        soulseek_username="tobias",
        soulseek_password="secret",
        web_username="slskd",
        web_password="helper",
        downloads_dir=tmp_path / "downloads",
    )
    text = path.read_text(encoding="utf-8")
    assert f"connect: {CONNECT_TIMEOUT_MS}" in text
    assert f"inactivity: {INACTIVITY_TIMEOUT_MS}" in text
    assert CONNECT_TIMEOUT_MS >= 10000
    # The password is in the file, but never in a log or a diagnostic report.
    assert "secret" in text


# ---------------------------------------------------------------------------
# A background status check must look, not nudge
#
# The 60 second status timer was calling PUT /server every single time, which
# cancelled slskd's own patient reconnection schedule and forced a fresh login
# attempt once a minute for hours.
# ---------------------------------------------------------------------------


@respx.mock
def test_background_status_check_never_asks_slskd_to_reconnect(slskd):
    _login()
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_DISCONNECTED)
    )
    reconnects = respx.put(f"{API}/server").mock(return_value=httpx.Response(204))

    slskd.test_connection()
    assert reconnects.call_count == 0


@respx.mock
def test_pressing_the_button_is_allowed_to_nudge(slskd):
    _login()
    respx.get(f"{API}/server").mock(
        return_value=httpx.Response(200, json=fx.SERVER_STATE_DISCONNECTED)
    )
    reconnects = respx.put(f"{API}/server").mock(return_value=httpx.Response(204))

    slskd.check_connection(interactive=True)
    assert reconnects.call_count == 1


def test_every_provider_has_the_read_only_check():
    """check_connection must exist everywhere, so the timer is always safe."""
    from trackfetch.providers.registry import PROVIDER_MODULES

    assert PROVIDER_MODULES
    from trackfetch.providers.base import Provider as Base

    assert hasattr(Base, "check_connection")


# ---------------------------------------------------------------------------
# A lost connection must stop the batch, not burn through it
#
# 92 items were marked failed in about 30 seconds against a dead connection,
# which lost the user's place in the list for no gain.
# ---------------------------------------------------------------------------


class _DeadProvider(Provider):
    info = ProviderInfo(
        provider_id="dead",
        display_name="Dead service",
        capabilities=frozenset({Capability.SEARCH}),
    )

    def __init__(self, ctx, fail_after=0):
        super().__init__(ctx)
        self.calls = 0
        self.fail_after = fail_after

    def test_connection(self):
        return self._status_ok()

    def search(self, text, options=None, cancel=None):
        from trackfetch.providers.errors import SoulseekNotConnected

        self.calls += 1
        if self.calls > self.fail_after:
            raise SoulseekNotConnected(provider_id="dead")
        return []


def _search_service(repos, settings, provider):
    from trackfetch.services.search_service import SearchService

    class _Registry:
        def get(self, provider_id):
            return provider

    return SearchService(_Registry(), repos, settings)


def _query_items(count, repos=None):
    from trackfetch.models.common import BatchItem, InputKind

    items = [BatchItem(raw_text=f"item {n}", kind=InputKind.QUERY) for n in range(count)]
    if repos is not None:
        # The searches are recorded against their batch item, so the items have
        # to exist in the database exactly as they do in real use.
        batch_id = repos.batches.create_batch(source="test")
        repos.batches.add_items(batch_id, items)
    return items


def test_a_dropped_connection_halts_the_batch(repos, settings, slskd_ctx):
    settings.search_concurrency = 1
    provider = _DeadProvider(slskd_ctx)
    service = _search_service(repos, settings, provider)

    outcomes = service.search_all(_query_items(50, repos), "dead")

    assert service.halted_reason
    assert service.halted_remedy
    # It stops shortly after the failures start, instead of chewing through all
    # fifty and reporting every one as failed.
    assert len(outcomes) < 10
    assert provider.calls < 10


def test_one_timeout_on_a_healthy_connection_does_not_halt(repos, settings, slskd_ctx):
    """A single failure is normal. Only a run of them means the service is gone."""
    from trackfetch.providers.errors import OperationTimedOut

    class _Flaky(_DeadProvider):
        def search(self, text, options=None, cancel=None):
            self.calls += 1
            if self.calls == 2:
                raise OperationTimedOut(provider_id="dead")
            return []

    settings.search_concurrency = 1
    provider = _Flaky(slskd_ctx)
    service = _search_service(repos, settings, provider)

    outcomes = service.search_all(_query_items(6, repos), "dead")

    assert service.halted_reason == ""
    assert len(outcomes) == 6


def test_halting_is_reported_on_the_progress(repos, settings, slskd_ctx):
    settings.search_concurrency = 1
    provider = _DeadProvider(slskd_ctx)
    service = _search_service(repos, settings, provider)

    seen = []
    service.search_all(_query_items(20, repos), "dead", on_progress=seen.append)

    assert seen
    assert seen[-1].halted
    assert "Stopped after" in seen[-1].text


def test_the_gap_between_searches_is_honoured(repos, settings, slskd_ctx):
    """Firing a hundred searches off with no gap is what gets you dropped."""
    settings.search_concurrency = 1
    settings.search_gap_seconds = 0.2
    provider = _DeadProvider(slskd_ctx, fail_after=99)
    service = _search_service(repos, settings, provider)

    started = time.monotonic()
    service.search_all(_query_items(4, repos), "dead")
    elapsed = time.monotonic() - started

    # Three gaps between four searches, allowing plenty of slack.
    assert elapsed >= 0.45


def test_a_gap_of_zero_means_no_waiting(repos, settings, slskd_ctx):
    settings.search_concurrency = 1
    settings.search_gap_seconds = 0.0
    provider = _DeadProvider(slskd_ctx, fail_after=99)
    service = _search_service(repos, settings, provider)

    started = time.monotonic()
    service.search_all(_query_items(5, repos), "dead")
    assert time.monotonic() - started < 1.0


def test_the_gap_setting_survives_being_saved_and_loaded():
    from trackfetch.config.settings import Settings

    settings = Settings()
    settings.search_gap_seconds = 3.5
    assert Settings.from_dict(settings.to_dict()).search_gap_seconds == 3.5
    # Nonsense is replaced by the default rather than crashing.
    assert Settings.from_dict({"search_gap_seconds": "soon"}).search_gap_seconds == 2.0
    assert Settings.from_dict({"search_gap_seconds": -5}).search_gap_seconds == 0.0


# ---------------------------------------------------------------------------
# The Connections strip must not delete the chip under the mouse pointer
#
# set_providers() rebuilt every chip on each 60 second refresh, and a click
# landing on a chip Qt had already deleted took the whole program down with
# "Internal C++ object (ConnectionChip) already deleted".
# ---------------------------------------------------------------------------


def test_the_connection_chips_are_not_rebuilt_on_every_refresh(qt_app):
    from trackfetch.gui.widgets.connections_panel import ConnectionsPanel

    panel = ConnectionsPanel()
    providers = [("slskd", "Soulseek"), ("http", "Web download")]
    panel.set_providers(providers)
    first = panel._chips["slskd"]

    panel.set_providers(list(providers))
    assert panel._chips["slskd"] is first

    panel.set_providers([("slskd", "Soulseek")])
    assert "http" not in panel._chips


def test_a_click_on_a_chip_being_deleted_does_not_crash(qt_app):
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QMouseEvent

    from trackfetch.gui.widgets.connections_panel import ConnectionChip

    chip = ConnectionChip("slskd", "Soulseek")

    def explode(_event):
        raise RuntimeError("Internal C++ object (ConnectionChip) already deleted.")

    chip.clicked.connect(lambda _pid: explode(None))
    event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPoint(1, 1),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    # Must not raise.
    chip.mouseReleaseEvent(event)


def test_a_status_for_a_chip_that_has_gone_is_survived(qt_app):
    from trackfetch.gui.widgets.connections_panel import ConnectionsPanel
    from trackfetch.models.common import ConnectionState, ConnectionStatus

    panel = ConnectionsPanel()
    panel.set_providers([("slskd", "Soulseek")])

    class _Gone:
        def set_status(self, status):
            raise RuntimeError("already deleted")

    panel._chips["slskd"] = _Gone()
    panel.set_status(
        "slskd", ConnectionStatus(provider_id="slskd", state=ConnectionState.CONNECTED)
    )
    assert "slskd" not in panel._chips


# ---------------------------------------------------------------------------
# Closing the database must not pull the rug from under a working thread
#
# Found by the test suite segfaulting. close() closed every connection,
# including one a transfer worker was in the middle of using, and sqlite takes
# the whole process down when that happens. No message, no log line, nothing.
# ---------------------------------------------------------------------------


def test_close_waits_for_work_already_running(tmp_path):
    from trackfetch.storage.db import Database

    db = Database(tmp_path / "busy.sqlite3")
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def slow_write():
        with db.write() as conn:
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES('k','v') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            entered.set()
            release.wait(5)
        finished.set()

    worker = threading.Thread(target=slow_write)
    worker.start()
    assert entered.wait(5)

    closer = threading.Thread(target=lambda: db.close(grace=5.0))
    closer.start()
    time.sleep(0.2)
    # The close is still waiting, because the write has not finished.
    assert not finished.is_set()

    release.set()
    worker.join(5)
    closer.join(5)
    assert finished.is_set()
    assert db.closed


def test_using_a_closed_database_raises_instead_of_crashing(tmp_path):
    from trackfetch.storage.db import Database, DatabaseClosed

    db = Database(tmp_path / "gone.sqlite3")
    db.close()

    with pytest.raises(DatabaseClosed):
        db.set_state("k", "v")
    with pytest.raises(DatabaseClosed):
        db.get_state("k")
    with pytest.raises(DatabaseClosed):
        with db.write():
            pass


def test_hammering_the_database_while_it_closes(tmp_path):
    """The shape of the original crash: many threads writing as close lands."""
    from trackfetch.storage.db import Database, DatabaseClosed

    db = Database(tmp_path / "race.sqlite3")
    stop = threading.Event()
    errors: list[Exception] = []

    def hammer(n):
        while not stop.is_set():
            try:
                db.set_state(f"k{n}", "v")
            except DatabaseClosed:
                return
            except Exception as exc:  # anything else is a real failure
                errors.append(exc)
                return

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    db.close(grace=5.0)
    stop.set()
    for t in threads:
        t.join(5)

    assert errors == []
    assert db.closed


def test_stopping_transfers_lets_workers_finish_first(repos, settings, slskd_ctx):
    """stop() used to abandon running workers, then the database closed."""
    from trackfetch.services.transfer_manager import TransferManager

    class _Registry:
        def get(self, provider_id):
            return None

        def require(self, provider_id):
            raise LookupError(provider_id)

    manager = TransferManager(_Registry(), repos, settings)
    manager.start()
    started = threading.Event()
    done = threading.Event()

    def slow():
        started.set()
        time.sleep(0.6)
        done.set()

    manager._pool.submit(slow)
    assert started.wait(5)
    manager.stop()
    # stop() returns only once the worker has finished, so nothing is still
    # writing when the database is closed a moment later.
    assert done.is_set()


# ---------------------------------------------------------------------------
# Every single Soulseek search came back empty
#
# slskd's documentation calls the search timeout a value in seconds. The
# running code reads it as milliseconds (slskd issue 1820), so Trackfetch was
# asking for a fifteen millisecond search: over before a single peer on the
# network could answer. 149 real searches in one day, every one with zero
# responses, and nothing in any log to say why.
# ---------------------------------------------------------------------------


@respx.mock
def test_the_search_timeout_is_sent_in_milliseconds(slskd):
    import json as _json

    _login()
    route = respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    slskd.client.search_start("Aphex Twin Xtal", timeout_seconds=15)
    body = _json.loads(route.calls[0].request.content)
    assert body["searchTimeout"] == 15000
    # A search measured in thousandths of a second is the bug, so guard the
    # magnitude rather than only the arithmetic.
    assert body["searchTimeout"] >= 1000


@respx.mock
def test_tabs_in_a_pasted_line_do_not_reach_the_network(slskd):
    """Lists pasted from other programs arrive as title, tab, artist."""
    import json as _json

    _login()
    route = respx.post(f"{API}/searches").mock(
        return_value=httpx.Response(200, json=fx.SEARCH_STATE_IN_PROGRESS)
    )
    slskd.client.search_start("A Song for You\tDonny Hathaway")
    body = _json.loads(route.calls[0].request.content)
    assert body["searchText"] == "A Song for You Donny Hathaway"
    assert "\t" not in body["searchText"]


def test_search_text_tidying():
    from trackfetch.providers.slskd_client import clean_search_text

    assert clean_search_text("  Les Fleurs\tMinnie Riperton  ") == "Les Fleurs Minnie Riperton"
    assert clean_search_text("two  spaces") == "two spaces"
    assert clean_search_text("a\nb") == "a b"
