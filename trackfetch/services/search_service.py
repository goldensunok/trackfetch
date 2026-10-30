"""SEARCH ALL: run every query in a batch against a search-capable provider.

Searching and downloading are separate operations. Nothing here starts a
transfer. It collects results and records them, and the user decides afterwards
what, if anything, to download.
"""

from __future__ import annotations

import logging
import threading
import time
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
from trackfetch.providers.errors import TrackfetchError
from trackfetch.providers.registry import ProviderRegistry
from trackfetch.storage.repositories import Repositories

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchProgress:
    """What the interface shows while a batch is running."""

    done: int = 0
    total: int = 0
    current: str = ""
    found: int = 0
    failed: int = 0

    @property
    def text(self) -> str:
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

    # -- control -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        self._cancel.set()

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
                            )
                        )
        finally:
            self._running = False
        return outcomes

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

        try:
            candidates = provider.search(item.raw_text, options, cancel=self._cancel)
        except TrackfetchError as exc:
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
