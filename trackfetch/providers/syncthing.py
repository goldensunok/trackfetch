"""Syncthing, through its documented REST API.

Syncthing is not a way to search the internet, and Trackfetch does not pretend
otherwise. It is a way to reach files and devices the user has already set up
themselves. This provider lists the folders Syncthing is looking after, lets the
user browse inside them, and copies chosen files into the download folder.

Endpoints used (all with the ``X-API-Key`` header):

    GET /rest/system/status        this device's identity
    GET /rest/system/version       version information
    GET /rest/system/connections   which devices are connected
    GET /rest/config/folders       the folders being synchronised
    GET /rest/db/browse            the contents of one folder
    GET /rest/db/status            how far a folder has got
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from trackfetch.models.common import (
    Capability,
    ConnectionState,
    ConnectionStatus,
    ProviderInfo,
    SearchCandidate,
    TransferJob,
    TransferStatus,
    as_list,
    as_mapping,
    coerce_int,
    coerce_str,
)
from trackfetch.providers._http import HttpApi, normalise_base_url
from trackfetch.providers.base import BrowseEntry, ProgressCallback, Provider, ProviderContext
from trackfetch.providers.errors import ProviderNotConfigured, TrackfetchError
from trackfetch.services.destinations import ensure_parent, sanitise_filename

log = logging.getLogger(__name__)

PROVIDER_ID = "syncthing"
SERVICE = "Syncthing"
CHUNK = 1024 * 1024

INFO = ProviderInfo(
    provider_id=PROVIDER_ID,
    display_name=SERVICE,
    capabilities=frozenset(
        {
            Capability.BROWSE,
            Capability.DOWNLOAD,
            Capability.PROGRESS,
            Capability.AUTH,
            Capability.CONNECTION_TEST,
        }
    ),
    workflow="Browse the folders you already sync and copy files out of them.",
    requires_app="Syncthing",
)


class SyncthingProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._api: HttpApi | None = None
        self._cancelled: set[str] = set()
        self._guard = threading.RLock()

    # -- plumbing ----------------------------------------------------------

    @property
    def api(self) -> HttpApi:
        if self._api is None:
            base = normalise_base_url(self.ctx.config("base_url", "http://localhost:8384"))
            self._api = HttpApi(
                base,
                service_name=SERVICE,
                provider_id=PROVIDER_ID,
                verify_tls=bool(self.ctx.config("verify_tls", True)),
                timeout=30,
                headers={"X-API-Key": self.ctx.secret("api_key")},
            )
        return self._api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None

    def is_configured(self) -> bool:
        return bool(self.ctx.config("base_url")) and bool(self.ctx.secret("api_key"))

    # -- connection --------------------------------------------------------

    def test_connection(self) -> ConnectionStatus:
        if not self.enabled:
            return self._set_status(
                ConnectionStatus(
                    provider_id=PROVIDER_ID,
                    state=ConnectionState.DISABLED,
                    detail=f"{SERVICE} is switched off.",
                    remedy="Open Settings and switch it on.",
                )
            )
        if not self.is_configured():
            return self._status_not_configured(
                "Syncthing has not been set up yet.",
                "Open Settings, choose Syncthing and paste the API key from its "
                "own settings page.",
            )
        try:
            version = as_mapping(self.api.get_json("/rest/system/version"))
            folders = self.folders()
            connections = as_mapping(self.api.get_json("/rest/system/connections"))
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)

        connected = sum(
            1
            for entry in as_mapping(connections.get("connections")).values()
            if as_mapping(entry).get("connected")
        )
        return self._status_ok(
            f"Connected to Syncthing {coerce_str(version.get('version'))}. "
            f"{len(folders)} folder{'s' if len(folders) != 1 else ''}, "
            f"{connected} device{'s' if connected != 1 else ''} online."
        )

    # -- browsing ----------------------------------------------------------

    def folders(self) -> list[dict[str, Any]]:
        payload = self.api.get_json("/rest/config/folders")
        return [dict(as_mapping(f)) for f in as_list(payload)]

    def browse(self, path: str = "") -> list[BrowseEntry]:
        if not self.is_configured():
            raise ProviderNotConfigured(provider_id=PROVIDER_ID)

        if not path:
            entries: list[BrowseEntry] = []
            for folder in self.folders():
                folder_id = coerce_str(folder.get("id"))
                label = coerce_str(folder.get("label")) or folder_id
                entries.append(
                    BrowseEntry(
                        name=label,
                        path=f"{folder_id}:",
                        is_dir=True,
                        extra={"folder": folder_id, "local_path": coerce_str(folder.get("path"))},
                    )
                )
            return entries

        folder_id, _, prefix = path.partition(":")
        params: dict[str, Any] = {"folder": folder_id, "levels": 0}
        if prefix:
            params["prefix"] = prefix
        payload = self.api.get_json("/rest/db/browse", params=params)
        local_root = self._local_path(folder_id)

        entries = []
        for name, value in _iter_browse(payload):
            child_prefix = f"{prefix}/{name}".strip("/") if prefix else name
            is_dir = not isinstance(value, list)
            size = None
            modified = None
            if isinstance(value, list) and len(value) >= 2:
                modified = _timestamp(value[0])
                size = coerce_int(value[1])
            entries.append(
                BrowseEntry(
                    name=name,
                    path=f"{folder_id}:{child_prefix}",
                    is_dir=is_dir,
                    size=size,
                    modified=modified,
                    extra={
                        "folder": folder_id,
                        "local_path": str(Path(local_root) / child_prefix) if local_root else "",
                    },
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def _local_path(self, folder_id: str) -> str:
        for folder in self.folders():
            if coerce_str(folder.get("id")) == folder_id:
                return coerce_str(folder.get("path"))
        return ""

    def candidate_from_entry(self, entry: BrowseEntry) -> SearchCandidate:
        candidate = SearchCandidate(
            provider_id=PROVIDER_ID,
            filename=entry.name,
            directory=coerce_str(entry.extra.get("folder")),
            extension=Path(entry.name).suffix.lstrip(".").lower(),
            size=entry.size,
            relevance=1.0,
        )
        candidate.payload = {
            "folder": entry.extra.get("folder", ""),
            "local_path": entry.extra.get("local_path", ""),
        }
        return candidate

    # -- copying out -------------------------------------------------------

    def build_job(
        self, candidate: SearchCandidate, destination_dir: Path, label: str = ""
    ) -> TransferJob:
        job = super().build_job(candidate, destination_dir, label)
        job.payload.setdefault("local_path", candidate.payload.get("local_path", ""))
        return job

    def download(
        self,
        job: TransferJob,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> TransferJob:
        """Copy the file out of the synchronised folder.

        Syncthing has no endpoint that serves file contents, so the honest way
        to do this is to copy from the folder Syncthing is already keeping on
        this computer.
        """
        source = Path(coerce_str(job.payload.get("local_path")))
        if not source or not source.exists():
            job.status = TransferStatus.FAILED
            job.error = "That file is not on this computer yet."
            job.remedy = (
                "Syncthing has to finish synchronising it first. Check the folder "
                "in Syncthing, then click Retry."
            )
            return job
        if source.is_dir():
            job.status = TransferStatus.FAILED
            job.error = "That is a folder, not a file."
            job.remedy = "Open the folder and choose the files inside it."
            return job

        target = _target_path(job, source.name)
        part = target.with_name(target.name + ".part")

        # Drop any stale cancel flag from an earlier attempt.
        with self._guard:
            self._cancelled.discard(job.id)

        try:
            ensure_parent(target)
            total = source.stat().st_size
            job.total_bytes = total
            job.status = TransferStatus.DOWNLOADING
            started = time.time()
            written = 0
            with open(source, "rb") as reader, open(part, "wb") as writer:
                while True:
                    if cancel is not None and cancel.is_set():
                        job.status = TransferStatus.CANCELLED
                        return job
                    with self._guard:
                        if job.id in self._cancelled:
                            self._cancelled.discard(job.id)
                            job.status = TransferStatus.CANCELLED
                            return job
                    chunk = reader.read(CHUNK)
                    if not chunk:
                        break
                    writer.write(chunk)
                    written += len(chunk)
                    job.downloaded_bytes = written
                    elapsed = max(1e-6, time.time() - started)
                    job.speed_bps = written / elapsed
                    if on_progress:
                        on_progress(written, total, job.speed_bps)
            shutil.copystat(source, part, follow_symlinks=True)
            if target.exists():
                target.unlink()
            part.replace(target)
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch could not copy that file."
            job.remedy = "Check the file is not in use and there is space on the disk."
            log.warning("syncthing copy failed: %s", exc)
            return job

        job.status = TransferStatus.COMPLETE
        job.destination = str(target)
        job.downloaded_bytes = job.total_bytes or target.stat().st_size
        job.completed_at = time.time()
        job.speed_bps = None
        job.touch()
        return job

    def cancel(self, job: TransferJob) -> bool:
        with self._guard:
            self._cancelled.add(job.id)
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        return []


def _target_path(job: TransferJob, remote_name: str) -> Path:
    """Where the file should end up. The queue's choice is used as given."""
    chosen = coerce_str(job.payload.get("target_path"))
    if chosen:
        return Path(chosen)
    base = Path(job.destination)
    if base.is_dir() or not base.suffix:
        return base / sanitise_filename(remote_name)
    return base


def _iter_browse(payload: Any):
    """Yield (name, value) pairs from either shape Syncthing may return."""
    node = as_mapping(payload)
    if node:
        for name, value in node.items():
            yield coerce_str(name), value
        return
    for entry in as_list(payload):
        record = as_mapping(entry)
        name = coerce_str(record.get("name"))
        if not name:
            continue
        if coerce_str(record.get("type")).lower().startswith("file"):
            yield name, [record.get("modTime"), record.get("size")]
        else:
            yield name, {}


def _timestamp(value: Any) -> float | None:
    text = coerce_str(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(text.split("+")[0].rstrip("Z"), fmt))
        except ValueError:
            continue
    return None


def create(ctx: ProviderContext) -> SyncthingProvider:
    return SyncthingProvider(ctx)
