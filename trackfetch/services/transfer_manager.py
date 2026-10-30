"""The unified download queue.

One queue for every provider. Some providers hand the work to an external client
(slskd, qBittorrent, Transmission, JDownloader, NZBGet, SABnzbd, Deluge, aria2)
and Trackfetch watches their progress. Others (web downloads, SFTP, WebDAV)
Trackfetch performs itself. Both kinds appear as identical rows to the user.

Guarantees:

* a failure on one transfer never stops the rest of the batch;
* the queue survives closing and reopening the application;
* nothing already recorded as complete is fetched again unless asked;
* nothing is silently overwritten.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Sequence

from trackfetch.config.settings import Settings
from trackfetch.models.common import (
    Capability,
    DuplicatePolicy,
    SearchCandidate,
    TransferJob,
    TransferStatus,
)
from trackfetch.providers.base import Provider
from trackfetch.providers.errors import TrackfetchError
from trackfetch.providers.registry import ProviderRegistry
from trackfetch.services.destinations import (
    Destination,
    DuplicateAction,
    decide_destination,
    fingerprint,
)
from trackfetch.storage.repositories import Repositories

log = logging.getLogger(__name__)

UpdateHandler = Callable[[TransferJob], None]
DuplicateResolver = Callable[[TransferJob, Path], DuplicateAction]

POLL_SECONDS = 2.0
TICK_SECONDS = 0.5


class TransferManager:
    """Owns the queue, the workers and the polling loop."""

    def __init__(
        self,
        registry: ProviderRegistry,
        repos: Repositories,
        settings: Settings,
        on_update: UpdateHandler | None = None,
        duplicate_resolver: DuplicateResolver | None = None,
    ):
        self.registry = registry
        self.repos = repos
        self.settings = settings
        self.on_update = on_update or (lambda job: None)
        self.duplicate_resolver = duplicate_resolver

        self._jobs: dict[str, TransferJob] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._in_flight: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._last_poll = 0.0
        #: Jobs the user removed. A worker still finishing must not write them
        #: back into the database after the row has gone.
        self._removed: set[str] = set()
        self._pool_size = 1

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            workers = max(1, int(self.settings.max_concurrent_transfers))
            self._pool_size = workers
            self._pool = ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="tf-transfer"
            )
            self._thread = threading.Thread(
                target=self._loop, name="tf-queue", daemon=True
            )
            self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        for event in list(self._cancel_events.values()):
            event.set()
        thread = self._thread
        if thread is not None and wait:
            thread.join(timeout=8)
        self._thread = None
        pool = self._pool
        self._pool = None
        if pool is not None:
            # Give the workers a moment to notice the cancel and finish writing
            # their last row. Anything still running after that is left to the
            # database's own shutdown guard. Waiting without a limit would hang
            # the program on a slow remote server.
            closer = threading.Thread(
                target=lambda: pool.shutdown(wait=True, cancel_futures=True),
                name="tf-pool-close",
                daemon=True,
            )
            closer.start()
            if wait:
                closer.join(timeout=5)

    # -- loading and adding ------------------------------------------------

    def load_existing(self) -> list[TransferJob]:
        """Restore the queue from the database after a restart."""
        jobs = self.repos.transfers.all()
        with self._lock:
            for job in jobs:
                self._jobs[job.id] = job
                if job.status is TransferStatus.DOWNLOADING:
                    # Nothing can have been running while the program was shut,
                    # so put it back in the queue rather than lying about it.
                    job.status = TransferStatus.WAITING
                    job.speed_bps = None
                    self.repos.transfers.save(job)
        return jobs

    def jobs(self) -> list[TransferJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at)

    def job(self, job_id: str) -> TransferJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def add_candidates(
        self,
        candidates: Sequence[SearchCandidate],
        destination_dir: Path | None = None,
        batch_item_ids: dict[str, str] | None = None,
    ) -> tuple[list[TransferJob], list[tuple[SearchCandidate, str]]]:
        """Turn chosen results into queue entries.

        Returns (queued jobs, skipped items with the reason).
        """
        base_dir = Path(destination_dir or self.settings.resolved_download_dir())
        queued: list[TransferJob] = []
        skipped: list[tuple[SearchCandidate, str]] = []

        for candidate in candidates:
            provider = self.registry.get(candidate.provider_id)
            if provider is None:
                skipped.append((candidate, "That service is not available."))
                continue

            identity = candidate.payload.get("remote_filename") or candidate.display_path
            key = fingerprint(candidate.provider_id, str(identity), candidate.size)
            if self.repos.completions.has(key):
                skipped.append(
                    (candidate, "Already downloaded. Use Download Again to force it.")
                )
                continue

            try:
                job = provider.build_job(candidate, base_dir)
            except TrackfetchError as exc:
                skipped.append((candidate, exc.message))
                continue

            job.candidate_id = candidate.id
            if batch_item_ids:
                job.batch_item_id = batch_item_ids.get(candidate.id, "")
            job.payload["fingerprint"] = key

            decision = self._resolve_destination(job, base_dir, candidate.filename)
            if decision is None:
                skipped.append((candidate, "Already in the download folder."))
                continue
            job.payload["target_path"] = str(decision.path)
            if provider.supports(Capability.DOWNLOAD):
                job.destination = str(decision.path)
            else:
                job.destination = str(decision.path.parent)

            queued.append(job)

        self._register(queued)
        return queued, skipped

    def add_job(self, job: TransferJob) -> TransferJob:
        self._register([job])
        return job

    def _register(self, jobs: Iterable[TransferJob]) -> None:
        with self._lock:
            for job in jobs:
                job.status = TransferStatus.WAITING
                self._jobs[job.id] = job
                self.repos.transfers.save(job)
                self._emit(job)

    def _resolve_destination(
        self, job: TransferJob, base_dir: Path, filename: str
    ) -> Destination | None:
        policy = self.settings.duplicate_policy
        decision = decide_destination(base_dir, filename or job.label, policy)
        if decision.action is DuplicateAction.ASK:
            if self.duplicate_resolver is not None:
                chosen = self.duplicate_resolver(job, decision.path)
            else:
                chosen = DuplicateAction.RENAME
            if chosen is DuplicateAction.SKIP:
                return None
            if chosen is DuplicateAction.RENAME:
                decision = decide_destination(
                    base_dir, filename or job.label, DuplicatePolicy.RENAME
                )
            elif chosen is DuplicateAction.REPLACE:
                decision = decide_destination(
                    base_dir, filename or job.label, DuplicatePolicy.REPLACE
                )
        elif decision.action is DuplicateAction.SKIP:
            return None
        return decision

    # -- controls ----------------------------------------------------------

    def pause(self, job_id: str) -> bool:
        job = self.job(job_id)
        if job is None:
            return False
        provider = self.registry.get(job.provider_id)
        if provider is None or not provider.supports(Capability.PAUSE):
            return False
        ok = _safe(provider.pause, job)
        if ok and job.status is not TransferStatus.PAUSED:
            job.status = TransferStatus.PAUSED
        self._save(job)
        return bool(ok)

    def resume(self, job_id: str) -> bool:
        job = self.job(job_id)
        if job is None:
            return False
        provider = self.registry.get(job.provider_id)
        if provider is None or not provider.supports(Capability.RESUME):
            return False
        ok = _safe(provider.resume, job)
        # Only put it back in the queue if the client actually resumed it.
        # Flipping to Waiting after a failed resume would make the queue submit
        # the same job to the external client a second time.
        if ok and job.status is TransferStatus.PAUSED:
            job.status = TransferStatus.WAITING
        self._save(job)
        return bool(ok)

    def cancel(self, job_id: str) -> bool:
        job = self.job(job_id)
        if job is None:
            return False
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()
        provider = self.registry.get(job.provider_id)
        if provider is not None:
            _safe(provider.cancel, job)
        job.status = TransferStatus.CANCELLED
        self._save(job)
        return True

    def retry(self, job_id: str) -> bool:
        job = self.job(job_id)
        if job is None:
            return False
        provider = self.registry.get(job.provider_id)
        if provider is not None:
            _safe(provider.retry, job)
        job.status = TransferStatus.WAITING
        job.error = ""
        job.remedy = ""
        job.speed_bps = None
        self._save(job)
        return True

    def remove(self, job_id: str) -> bool:
        self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
            self._removed.add(job_id)
        self.repos.transfers.delete(job_id)
        return True

    def clear_finished(self) -> None:
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.status in (TransferStatus.COMPLETE, TransferStatus.CANCELLED):
                    self._jobs.pop(job_id, None)
        self.repos.transfers.clear_finished()

    def retry_all_failed(self) -> int:
        count = 0
        for job in self.jobs():
            if job.status is TransferStatus.FAILED:
                self.retry(job.id)
                count += 1
        return count

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # pragma: no cover - the loop must never die
                log.exception("transfer loop error")
            self._stop.wait(TICK_SECONDS)

    def _tick(self) -> None:
        self._start_waiting()
        now = time.time()
        if now - self._last_poll >= POLL_SECONDS:
            self._last_poll = now
            self._poll_external()

    def _start_waiting(self) -> None:
        with self._lock:
            waiting = [j for j in self._jobs.values() if j.status is TransferStatus.WAITING]
            # Never promise more slots than the thread pool actually has, or a
            # job would be marked Downloading while it sat in the pool's queue.
            allowed = min(
                max(1, int(self.settings.max_concurrent_transfers)), self._pool_size
            )
            capacity = allowed - len(self._in_flight)
        if not waiting:
            return

        for job in waiting:
            provider = self.registry.get(job.provider_id)
            if provider is None:
                job.status = TransferStatus.FAILED
                job.error = "That service is not available."
                job.remedy = "Open Settings and check the service is switched on."
                self._save(job)
                continue

            if provider.supports(Capability.DOWNLOAD):
                if capacity <= 0:
                    continue
                capacity -= 1
                self._begin_local_download(provider, job)
            elif provider.supports(Capability.ADD_JOB):
                self._submit_external(provider, job)
            else:
                job.status = TransferStatus.FAILED
                job.error = f"{provider.display_name} cannot download that."
                self._save(job)

    def _begin_local_download(self, provider: Provider, job: TransferJob) -> None:
        pool = self._pool
        if pool is None:
            return
        with self._lock:
            if job.id in self._in_flight:
                return
            self._in_flight.add(job.id)
        job.status = TransferStatus.DOWNLOADING
        self._save(job)

        cancel = threading.Event()
        self._cancel_events[job.id] = cancel

        def progress(done: int, total: int | None, speed: float) -> None:
            job.downloaded_bytes = done
            if total:
                job.total_bytes = total
            job.speed_bps = speed
            if total and speed > 0:
                job.eta_seconds = int(max(0, (total - done) / speed))
            job.touch()
            self._emit(job)

        def work() -> None:
            try:
                provider.download(job, progress, cancel)
            except TrackfetchError as exc:
                job.status = TransferStatus.FAILED
                job.error = exc.message
                job.remedy = exc.remedy
            except Exception:  # pragma: no cover - defensive
                log.exception("download failed")
                job.status = TransferStatus.FAILED
                job.error = "The download did not finish."
                job.remedy = "Click Retry to try again."
            finally:
                with self._lock:
                    self._in_flight.discard(job.id)
                self._cancel_events.pop(job.id, None)
                self._finish(job)

        pool.submit(work)

    def _submit_external(self, provider: Provider, job: TransferJob) -> None:
        try:
            provider.submit(job)
        except TrackfetchError as exc:
            job.status = TransferStatus.FAILED
            job.error = exc.message
            job.remedy = exc.remedy
        except Exception:  # pragma: no cover - defensive
            log.exception("submit failed")
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch could not hand that to the download program."
            job.remedy = "Check the program is running, then click Retry."
        self._save(job)

    def _poll_external(self) -> None:
        with self._lock:
            active = [
                j
                for j in self._jobs.values()
                if not j.status.is_terminal and j.status is not TransferStatus.PAUSED
            ]
        if not active:
            return
        by_provider: dict[str, list[TransferJob]] = {}
        for job in active:
            by_provider.setdefault(job.provider_id, []).append(job)

        for provider_id, jobs in by_provider.items():
            provider = self.registry.get(provider_id)
            if provider is None or not provider.supports(Capability.ADD_JOB):
                continue
            try:
                changed = provider.poll(jobs)
            except Exception:  # pragma: no cover - a provider must not stop the loop
                log.debug("polling %s failed", provider_id, exc_info=True)
                continue
            for job in changed:
                if job.status is TransferStatus.COMPLETE:
                    self._finish(job)
                else:
                    self._save(job)

    # -- bookkeeping -------------------------------------------------------

    def _finish(self, job: TransferJob) -> None:
        with self._lock:
            if job.id in self._removed:
                self._removed.discard(job.id)
                return
        if job.status is TransferStatus.COMPLETE:
            job.completed_at = job.completed_at or time.time()
            key = job.payload.get("fingerprint") or fingerprint(
                job.provider_id, job.label, job.total_bytes
            )
            try:
                self.repos.completions.record(
                    key, job.provider_id, job.label, job.destination, job.total_bytes
                )
            except Exception:  # pragma: no cover - defensive
                log.debug("could not record completion", exc_info=True)
            self._note(f"Finished {job.label}", "info", job.provider_id)
        elif job.status is TransferStatus.FAILED:
            self._note(f"Failed {job.label}: {job.error}", "error", job.provider_id)
        self._save(job)

    def _note(self, message: str, level: str, provider_id: str) -> None:
        """Record an event, but never let the note-keeping break the work.

        A worker can finish at the moment the program is closing, when the
        database is already shutting down.
        """
        try:
            self.repos.events.add(message, level, provider_id)
        except Exception:  # pragma: no cover - diagnostics must not break work
            log.debug("could not record an event", exc_info=True)

    def _save(self, job: TransferJob) -> None:
        with self._lock:
            if job.id in self._removed:
                # The user removed this row. A worker still winding down must
                # not write it back into the database.
                return
        job.touch()
        try:
            self.repos.transfers.save(job)
        except Exception:  # pragma: no cover - defensive
            log.debug("could not save transfer", exc_info=True)
        self._emit(job)

    def _emit(self, job: TransferJob) -> None:
        try:
            self.on_update(job)
        except Exception:  # pragma: no cover - the interface must not break the queue
            log.debug("transfer update handler failed", exc_info=True)

    # -- summary -----------------------------------------------------------

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {str(s): 0 for s in TransferStatus}
        for job in self.jobs():
            counts[str(job.status)] += 1
        return counts


def _safe(func, *args):
    try:
        return func(*args)
    except Exception:  # pragma: no cover - defensive
        log.debug("provider call failed", exc_info=True)
        return False
