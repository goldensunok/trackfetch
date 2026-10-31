"""SEARCH ALL: run every query in a batch against a search-capable provider.

Searching and downloading are separate operations. Nothing here starts a
transfer. It collects results and records them, and the user decides afterwards
what, if anything, to download.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Sequence

from trackfetch.config.settings import Settings
from trackfetch.models.common import (
    BatchItem,
    Capability,
    InputKind,
    SearchCandidate,
    SearchQuery,
)
from trackfetch.providers.base import SearchOptions
from trackfetch.providers.errors import (
    AuthenticationFailed,
    ConnectionFailed,
    OperationTimedOut,
    SlskdUnavailable,
    SoulseekNotConnected,
    TrackfetchError,
)
from trackfetch.providers.registry import ProviderRegistry
from trackfetch.storage.repositories import Repositories

log = logging.getLogger(__name__)

#: Failures that mean "the service itself is not there", as opposed to "that
#: particular search found nothing" or "that one search went wrong".
CONNECTION_FAILURES = (
    SlskdUnavailable,
    SoulseekNotConnected,
    ConnectionFailed,
    AuthenticationFailed,
    OperationTimedOut,
)

#: How many connection failures in a row before Trackfetch stops the batch.
#: One is not enough: a single search can time out on a healthy connection.
#: Three in a row means the service has gone, and carrying on would burn through
#: the rest of the list in seconds and report every item as failed.
HALT_AFTER_CONNECTION_FAILURES = 3


@dataclass(slots=True)
class SearchProgress:
    """What the interface shows while a batch is running."""

    done: int = 0
    total: int = 0
    current: str = ""
    found: int = 0
    failed: int = 0
    #: Set when the batch stopped early because the service went away. Written in
    #: plain words, for showing to the user as-is.
    halted: str = ""

    @property
    def text(self) -> str:
        if self.halted:
            return f"Stopped after {self.done} / {self.total}"
        return f"Searching {self.done} / {self.total}"


@dataclass(slots=True)
class SearchOutcome:
    """The result of one query."""

    item: BatchItem
    query: SearchQuery
    candidates: list[SearchCandidate]
    error: str = ""
    remedy: str = ""


ProgressHandler = Callable[[SearchProgress], None]
ResultHandler = Callable[[SearchOutcome], None]


class SearchService:
    """Runs a batch of searches, in parallel, with progress and cancellation."""

    def __init__(
        self,
        registry: ProviderRegistry,
        repos: Repositories,
        settings: Settings,
    ):
        self.registry = registry
        self.repos = repos
        self.settings = settings
        self._cancel = threading.Event()
        self._running = False
        self._lock = threading.RLock()
        self._halted_reason = ""
        self._halted_remedy = ""
        self._consecutive_connection_failures = 0
        #: Guards the pacing, so the limit applies across all the worker
        #: threads rather than per thread.
        self._pace_lock = threading.Lock()
        self._last_search_started = 0.0
        #: When each recent search was started, for the rolling rate limit.
        self._recent_starts: deque[float] = deque()

    # -- control -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def halted_reason(self) -> str:
        """Why the last batch stopped early, in plain words. Empty if it did not."""
        return self._halted_reason

    @property
    def halted_remedy(self) -> str:
        return self._halted_remedy

    def cancel(self) -> None:
        self._cancel.set()

    # -- pacing ------------------------------------------------------------

    def _wait_for_turn(self) -> None:
        """Hold the next search back until the network will tolerate it.

        The Soulseek server blocks an account for about half an hour if it
        sends too many searches too quickly, and the block looks from the
        outside like an ordinary disconnection, which makes it baffling. Two
        limits keep Trackfetch the right side of it: a minimum gap between one
        search and the next, and a ceiling on how many may start within a
        rolling window. Both are settings, so a patient user can go slower and
        an impatient one can take the risk knowingly.
        """
        while not self._cancel.is_set():
            gap = max(0.0, float(getattr(self.settings, "search_gap_seconds", 0.0) or 0.0))
            ceiling = max(0, int(getattr(self.settings, "search_rate_limit", 0) or 0))
            window = max(
                1.0, float(getattr(self.settings, "search_rate_window_seconds", 220.0) or 220.0)
            )

            with self._pace_lock:
                now = time.monotonic()
                while self._recent_starts and now - self._recent_starts[0] >= window:
                    self._recent_starts.popleft()

                wait_for = 0.0
                if gap > 0 and self._last_search_started:
                    wait_for = max(wait_for, gap - (now - self._last_search_started))
                if ceiling and len(self._recent_starts) >= ceiling:
                    wait_for = max(wait_for, window - (now - self._recent_starts[0]))

                if wait_for <= 0:
                    self._last_search_started = now
                    self._recent_starts.append(now)
                    return

            # Wake often enough to notice a cancel, however long the wait.
            self._cancel.wait(min(wait_for, 0.5))

    # -- the batch ---------------------------------------------------------

    def search_all(
        self,
        items: Sequence[BatchItem],
        provider_id: str,
        on_progress: ProgressHandler | None = None,
        on_result: ResultHandler | None = None,
    ) -> list[SearchOutcome]:
        """Search every query item. Never raises: failures land on the item."""
        provider = self.registry.get(provider_id)
        if provider is None or not provider.supports(Capability.SEARCH):
            raise TrackfetchError(
                "That service cannot search.",
                "Choose a service that supports searching, such as Soulseek.",
                provider_id=provider_id,
            )

        searchable = [i for i in items if i.kind is InputKind.QUERY and not i.skipped]
        progress = SearchProgress(total=len(searchable))
        outcomes: list[SearchOutcome] = []
        if not searchable:
            if on_progress:
                on_progress(progress)
            return outcomes

        self._cancel.clear()
        self._running = True
        self._halted_reason = ""
        self._halted_remedy = ""
        self._consecutive_connection_failures = 0
        self._last_search_started = 0.0
        self._recent_starts.clear()
        options = SearchOptions(
            timeout=self.settings.search_timeout,
            response_limit=self.settings.result_limit,
            minimum_response_file_count=self.settings.minimum_response_file_count,
        )
        workers = max(1, min(self.settings.search_concurrency, len(searchable)))

        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tf-search") as pool:
                futures: dict[Future, BatchItem] = {
                    pool.submit(self._run_one, provider, item, options): item
                    for item in searchable
                }
                for future in _as_completed(futures):
                    item = futures[future]
                    try:
                        outcome = future.result()
                    except Exception:  # pragma: no cover - defensive
                        log.exception("search worker failed")
                        outcome = SearchOutcome(
                            item=item,
                            query=SearchQuery(batch_item_id=item.id, provider_id=provider_id,
                                              text=item.raw_text, state="failed"),
                            candidates=[],
                            error="Trackfetch could not run that search.",
                            remedy="Try that item again on its own.",
                        )
                    outcomes.append(outcome)
                    with self._lock:
                        progress.done += 1
                        progress.current = item.raw_text
                        progress.found += len(outcome.candidates)
                        if outcome.error:
                            progress.failed += 1
                        progress.halted = self._halted_reason
                    if on_result:
                        on_result(outcome)
                    if on_progress:
                        on_progress(
                            SearchProgress(
                                done=progress.done,
                                total=progress.total,
                                current=progress.current,
                                found=progress.found,
                                failed=progress.failed,
                                halted=progress.halted,
                            )
                        )
                    if self._halted_reason:
                        # The service has gone. Stop the batch rather than
                        # marking every remaining item as failed in a few
                        # seconds, which loses the user's place in the list.
                        self._cancel.set()
                        for pending in futures:
                            pending.cancel()
                        log.warning("Search batch halted: %s", self._halted_reason)
                        break
        finally:
            self._running = False
        return outcomes

    def _note_failure(self, exc: Exception) -> None:
        """Count connection failures, and halt the batch once they pile up."""
        with self._lock:
            if isinstance(exc, CONNECTION_FAILURES):
                self._consecutive_connection_failures += 1
            else:
                self._consecutive_connection_failures = 0
                return
            if self._halted_reason:
                return
            if self._consecutive_connection_failures >= HALT_AFTER_CONNECTION_FAILURES:
                message = getattr(exc, "message", "") or str(exc)
                self._halted_reason = (
                    f"Searching stopped because the connection was lost. {message}".strip()
                )
                self._halted_remedy = (
                    "Soulseek blocks an account for around half an hour when it "
                    "sees too many searches too quickly, and that looks exactly "
                    "like this. Leave it a while, then press SEARCH ALL again: "
                    "everything found so far is kept. If it keeps happening, "
                    "lower the search settings under Settings, Search."
                )

    def _note_success(self) -> None:
        with self._lock:
            self._consecutive_connection_failures = 0

    def _run_one(self, provider, item: BatchItem, options: SearchOptions) -> SearchOutcome:
        query = SearchQuery(
            batch_item_id=item.id,
            provider_id=provider.provider_id,
            text=item.raw_text,
            state="running",
        )
        self.repos.queries.save(query)

        if self._cancel.is_set():
            query.state = "cancelled"
            query.completed_at = time.time()
            self.repos.queries.save(query)
            return SearchOutcome(item, query, [], error="Search was stopped.")

        self._wait_for_turn()
        if self._cancel.is_set():
            query.state = "cancelled"
            query.completed_at = time.time()
            self.repos.queries.save(query)
            return SearchOutcome(item, query, [], error="Search was stopped.")

        try:
            candidates = provider.search(item.raw_text, options, cancel=self._cancel)
        except TrackfetchError as exc:
            self._note_failure(exc)
            query.state = "failed"
            query.error = exc.message
            query.completed_at = time.time()
            self.repos.queries.save(query)
            self.repos.events.add(
                f"Search failed for '{item.raw_text}': {exc.message}",
                "error",
                provider.provider_id,
            )
            return SearchOutcome(item, query, [], error=exc.message, remedy=exc.remedy)
        except Exception:  # pragma: no cover - defensive
            log.exception("unexpected search failure")
            query.state = "failed"
            query.error = "Trackfetch could not run that search."
            query.completed_at = time.time()
            self.repos.queries.save(query)
            return SearchOutcome(
                item,
                query,
                [],
                error="Trackfetch could not run that search.",
                remedy="Try that item again on its own.",
            )

        self._note_success()
        for candidate in candidates:
            candidate.query_id = query.id
        self.repos.candidates.save_many(candidates)

        query.state = "complete"
        query.result_count = len(candidates)
        query.completed_at = time.time()
        self.repos.queries.save(query)
        return SearchOutcome(item, query, candidates)


def _as_completed(futures):
    from concurrent.futures import as_completed

    return as_completed(list(futures.keys()))
