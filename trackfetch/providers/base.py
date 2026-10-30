"""The provider interface.

Every provider is an adapter. It converts one external mechanism (a Soulseek
daemon, a torrent client, an SFTP server) into the small vocabulary defined in
``trackfetch.models``. Nothing outside this package needs to know which is which.

Providers deliberately do not all implement the same methods. They advertise
capabilities and the rest of Trackfetch asks before it acts. That is what keeps
"search the Soulseek network" and "hand a magnet link to qBittorrent" in the
same application without pretending they are the same operation.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from trackfetch.config.secrets import SecretStore
from trackfetch.config.settings import ProviderSettings, Settings
from trackfetch.models.common import (
    Capability,
    ConnectionState,
    ConnectionStatus,
    ProviderInfo,
    SearchCandidate,
    TransferJob,
    TransferStatus,
)
from trackfetch.providers.errors import (
    ProviderNotConfigured,
    UnsupportedOperation,
)

log = logging.getLogger(__name__)

#: Called with (downloaded_bytes, total_bytes_or_None, speed_bytes_per_second).
ProgressCallback = Callable[[int, int | None, float], None]


@dataclass(slots=True)
class BrowseEntry:
    """One row of a browsable listing (SFTP, WebDAV, Syncthing folder)."""

    name: str
    path: str
    is_dir: bool = False
    size: int | None = None
    modified: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchOptions:
    """User-controlled search parameters. No preferences are baked in here."""

    timeout: int = 15
    response_limit: int = 250
    file_limit: int = 10000
    minimum_response_file_count: int = 1
    maximum_peer_queue_length: int | None = None
    minimum_peer_upload_speed: int | None = None


@dataclass
class ProviderContext:
    """Everything a provider is given when it is created."""

    provider_id: str
    settings: Settings
    provider_settings: ProviderSettings
    secrets: SecretStore
    #: Called with (message, level, provider_id) so notable events reach the
    #: diagnostics report without providers importing the database.
    on_event: Callable[[str, str, str], None] = lambda m, lvl, pid: None

    def secret(self, field_name: str) -> str:
        return self.secrets.get(SecretStore.key_for(self.provider_id, field_name))

    def set_secret(self, field_name: str, value: str) -> None:
        self.secrets.set(SecretStore.key_for(self.provider_id, field_name), value)

    def config(self, key: str, default: Any = None) -> Any:
        return self.provider_settings.config.get(key, default)

    def download_dir(self) -> Path:
        return self.settings.resolved_download_dir()

    def event(self, message: str, level: str = "info") -> None:
        try:
            self.on_event(message, level, self.provider_id)
        except Exception:  # pragma: no cover - diagnostics must never break work
            pass


class Provider(ABC):
    """Base class. Subclasses override only what they can actually do."""

    #: Filled in by each subclass.
    info: ProviderInfo

    def __init__(self, ctx: ProviderContext):
        self.ctx = ctx
        self._lock = threading.RLock()
        self._status = ConnectionStatus(provider_id=self.provider_id)

    # -- identity ----------------------------------------------------------

    @property
    def provider_id(self) -> str:
        return self.info.provider_id

    @property
    def display_name(self) -> str:
        return self.info.display_name

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.info.capabilities

    def supports(self, capability: Capability) -> bool:
        return capability in self.info.capabilities

    @property
    def enabled(self) -> bool:
        return bool(self.ctx.provider_settings.enabled)

    # -- configuration and connection --------------------------------------

    def is_configured(self) -> bool:
        """Whether the user has supplied enough detail to try connecting."""
        return True

    def require_configured(self) -> None:
        if not self.is_configured():
            raise ProviderNotConfigured(
                f"{self.display_name} has not been set up yet.",
                f"Open Settings, choose {self.display_name} and click Configure.",
                provider_id=self.provider_id,
            )

    @abstractmethod
    def test_connection(self) -> ConnectionStatus:
        """Check the service and return a status a person can read."""

    @property
    def last_status(self) -> ConnectionStatus:
        return self._status

    def _set_status(self, status: ConnectionStatus) -> ConnectionStatus:
        with self._lock:
            self._status = status
        return status

    def _status_not_configured(self, detail: str = "", remedy: str = "") -> ConnectionStatus:
        return self._set_status(
            ConnectionStatus(
                provider_id=self.provider_id,
                state=ConnectionState.NOT_CONFIGURED,
                detail=detail or f"{self.display_name} has not been set up yet.",
                remedy=remedy
                or f"Open Settings, choose {self.display_name} and click Configure.",
            )
        )

    def _status_ok(self, detail: str = "") -> ConnectionStatus:
        return self._set_status(
            ConnectionStatus(
                provider_id=self.provider_id,
                state=ConnectionState.CONNECTED,
                detail=detail or f"{self.display_name} is connected.",
            )
        )

    def _status_from_error(self, exc: Exception) -> ConnectionStatus:
        from trackfetch.providers.errors import TrackfetchError

        if isinstance(exc, TrackfetchError):
            message, remedy, technical = exc.message, exc.remedy, exc.technical
        else:
            message = f"Trackfetch could not reach {self.display_name}."
            remedy = f"Check {self.display_name} is running, then try again."
            technical = repr(exc)
        return self._set_status(
            ConnectionStatus(
                provider_id=self.provider_id,
                state=ConnectionState.DISCONNECTED,
                detail=message,
                remedy=remedy,
                technical=technical,
            )
        )

    # -- search ------------------------------------------------------------

    def search(
        self,
        text: str,
        options: SearchOptions | None = None,
        cancel: threading.Event | None = None,
    ) -> list[SearchCandidate]:
        """Run one search and return every candidate found.

        Blocking on purpose: Trackfetch runs it on a worker thread and reports
        batch progress from the caller. Providers with a genuinely asynchronous
        protocol (Soulseek) handle their own polling inside here.
        """
        raise UnsupportedOperation(
            f"{self.display_name} cannot search.",
            provider_id=self.provider_id,
        )

    # -- browse ------------------------------------------------------------

    def browse(self, path: str = "") -> list[BrowseEntry]:
        raise UnsupportedOperation(
            f"{self.display_name} cannot browse folders.",
            provider_id=self.provider_id,
        )

    def candidate_from_entry(self, entry: BrowseEntry) -> SearchCandidate:
        raise UnsupportedOperation(
            f"{self.display_name} cannot turn that into a download.",
            provider_id=self.provider_id,
        )

    # -- resolve -----------------------------------------------------------

    def resolve(self, target: str) -> SearchCandidate:
        """Inspect a URL or link and describe what it points at."""
        raise UnsupportedOperation(
            f"{self.display_name} cannot inspect that kind of link.",
            provider_id=self.provider_id,
        )

    # -- jobs --------------------------------------------------------------

    def build_job(
        self,
        candidate: SearchCandidate,
        destination_dir: Path,
        label: str = "",
    ) -> TransferJob:
        """Create the queue entry for a chosen candidate."""
        return TransferJob(
            provider_id=self.provider_id,
            label=label or candidate.filename or candidate.display_path,
            status=TransferStatus.WAITING,
            total_bytes=candidate.size,
            destination=str(destination_dir),
            candidate_id=candidate.id,
            payload=dict(candidate.payload),
        )

    def submit(self, job: TransferJob) -> TransferJob:
        """Hand the job to the external client. Sets ``job.remote_id``."""
        raise UnsupportedOperation(
            f"{self.display_name} cannot start downloads that way.",
            provider_id=self.provider_id,
        )

    def download(
        self,
        job: TransferJob,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> TransferJob:
        """Fetch the bytes ourselves. Only for providers with DOWNLOAD."""
        raise UnsupportedOperation(
            f"{self.display_name} does not download files directly.",
            provider_id=self.provider_id,
        )

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        """Refresh the given jobs from the external client. Returns changed jobs."""
        return []

    def pause(self, job: TransferJob) -> bool:
        return False

    def resume(self, job: TransferJob) -> bool:
        return False

    def cancel(self, job: TransferJob) -> bool:
        return False

    def retry(self, job: TransferJob) -> bool:
        """Default retry: put it back to Waiting so the queue picks it up again."""
        job.status = TransferStatus.WAITING
        job.error = ""
        job.remedy = ""
        job.touch()
        return True

    # -- housekeeping ------------------------------------------------------

    def close(self) -> None:
        """Release sockets and background resources."""

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret facts about this provider for the diagnostics report."""
        return {
            "id": self.provider_id,
            "name": self.display_name,
            "enabled": self.enabled,
            "configured": self.is_configured(),
            "capabilities": sorted(str(c) for c in self.capabilities),
            "status": self._status.summary(),
            "status_detail": self._status.detail,
        }


def capabilities(*items: Capability) -> frozenset[Capability]:
    return frozenset(items)


def filter_by_capability(
    providers: Iterable[Provider], capability: Capability
) -> list[Provider]:
    return [p for p in providers if p.supports(capability)]
