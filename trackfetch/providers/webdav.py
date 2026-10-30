"""WebDAV servers, such as Nextcloud, ownCloud and generic WebDAV shares.

Workflow: connect, browse, choose, download. Trackfetch fetches the bytes
itself, using the standard ``PROPFIND`` method to list folders and ordinary
``GET`` with a byte range to download and resume.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

import httpx

from trackfetch.models.common import (
    Capability,
    ConnectionState,
    ConnectionStatus,
    ProviderInfo,
    SearchCandidate,
    TransferJob,
    TransferStatus,
    coerce_int,
    coerce_str,
)
from trackfetch.providers._http import (
    HttpApi,
    normalise_base_url,
    translate_http_error,
    translate_status,
)
from trackfetch.providers.base import BrowseEntry, ProgressCallback, Provider, ProviderContext
from trackfetch.providers.errors import (
    DestinationError,
    InvalidResponse,
    TrackfetchError,
)
from trackfetch.services.destinations import ensure_parent, sanitise_filename

log = logging.getLogger(__name__)

PROVIDER_ID = "webdav"
SERVICE = "the WebDAV server"
DAV_NS = "{DAV:}"
CHUNK = 262144

INFO = ProviderInfo(
    provider_id=PROVIDER_ID,
    display_name="WebDAV",
    capabilities=frozenset(
        {
            Capability.BROWSE,
            Capability.DOWNLOAD,
            Capability.PROGRESS,
            Capability.RESUME,
            Capability.CANCEL,
            Capability.AUTH,
            Capability.CONNECTION_TEST,
        }
    ),
    workflow="Connect to a WebDAV server, browse it, choose files and download them.",
)

PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:displayname/><d:getcontentlength/><d:getlastmodified/>"
    "<d:resourcetype/><d:getcontenttype/>"
    "</d:prop></d:propfind>"
)


class WebdavProvider(Provider):
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
            base = normalise_base_url(coerce_str(self.ctx.config("base_url", "")))
            username = coerce_str(self.ctx.config("username", ""))
            password = self.ctx.secret("password")
            self._api = HttpApi(
                base,
                service_name=SERVICE,
                provider_id=PROVIDER_ID,
                verify_tls=bool(self.ctx.config("verify_tls", True)),
                timeout=60,
                auth=(username, password) if username or password else None,
            )
        return self._api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None

    def is_configured(self) -> bool:
        return bool(self.ctx.config("base_url"))

    # -- connection --------------------------------------------------------

    def test_connection(self) -> ConnectionStatus:
        if not self.enabled:
            return self._set_status(
                ConnectionStatus(
                    provider_id=PROVIDER_ID,
                    state=ConnectionState.DISABLED,
                    detail="WebDAV is switched off.",
                    remedy="Open Settings and switch it on.",
                )
            )
        if not self.is_configured():
            return self._status_not_configured()
        try:
            entries = self.browse("")
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
        return self._status_ok(
            f"Connected. The top folder has {len(entries)} item"
            f"{'s' if len(entries) != 1 else ''}."
        )

    # -- browsing ----------------------------------------------------------

    def browse(self, path: str = "") -> list[BrowseEntry]:
        target = path.strip("/")
        try:
            response = self.api.request(
                "PROPFIND",
                target,
                headers={"Depth": "1", "Content-Type": "application/xml"},
                content=PROPFIND_BODY.encode("utf-8"),
                raise_for_status=False,
            )
        except httpx.HTTPError as exc:
            raise translate_http_error(exc, SERVICE, PROVIDER_ID) from exc
        if response.status_code >= 400:
            raise translate_status(response, SERVICE, PROVIDER_ID)

        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise InvalidResponse(
                "The WebDAV server sent a reply Trackfetch could not read.",
                "Check the address is the WebDAV address, not the web page address.",
                technical=repr(exc),
                provider_id=PROVIDER_ID,
            ) from exc

        base_path = urlparse(self.api.base_url).path.rstrip("/")
        entries: list[BrowseEntry] = []
        for element in root.findall(f"{DAV_NS}response"):
            href = _text(element, f"{DAV_NS}href")
            if not href:
                continue
            remote = unquote(urlparse(href).path)
            if base_path and remote.startswith(base_path):
                remote = remote[len(base_path) :]
            remote = remote.strip("/")
            if remote.rstrip("/") == target.rstrip("/"):
                continue  # the folder itself

            propstat = element.find(f"{DAV_NS}propstat")
            prop = propstat.find(f"{DAV_NS}prop") if propstat is not None else None
            is_dir = False
            size = None
            modified = None
            name = PurePosixPath(remote).name
            if prop is not None:
                resource_type = prop.find(f"{DAV_NS}resourcetype")
                is_dir = (
                    resource_type is not None
                    and resource_type.find(f"{DAV_NS}collection") is not None
                )
                size = coerce_int(_text(prop, f"{DAV_NS}getcontentlength"))
                display = _text(prop, f"{DAV_NS}displayname")
                if display:
                    name = display
                modified = _http_date(_text(prop, f"{DAV_NS}getlastmodified"))
            entries.append(
                BrowseEntry(
                    name=name,
                    path=remote,
                    is_dir=is_dir,
                    size=size,
                    modified=modified,
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def candidate_from_entry(self, entry: BrowseEntry) -> SearchCandidate:
        candidate = SearchCandidate(
            provider_id=PROVIDER_ID,
            filename=entry.name,
            directory=str(PurePosixPath(entry.path).parent),
            extension=Path(entry.name).suffix.lstrip(".").lower(),
            size=entry.size,
            relevance=1.0,
        )
        candidate.payload = {"remote_path": entry.path}
        return candidate

    # -- downloading -------------------------------------------------------

    def build_job(
        self, candidate: SearchCandidate, destination_dir: Path, label: str = ""
    ) -> TransferJob:
        job = super().build_job(candidate, destination_dir, label)
        job.payload.setdefault("remote_path", candidate.payload.get("remote_path", ""))
        return job

    def download(
        self,
        job: TransferJob,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> TransferJob:
        remote = coerce_str(job.payload.get("remote_path"))
        if not remote:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch does not know which file to fetch."
            job.remedy = "Browse the server again and choose the file."
            return job

        target = _target_path(job, PurePosixPath(remote).name)
        part = target.with_name(target.name + ".part")

        # Drop any stale cancel flag from an earlier attempt.
        with self._guard:
            self._cancelled.discard(job.id)

        try:
            ensure_parent(target)
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = DestinationError.default_message
            job.remedy = DestinationError.default_remedy
            log.warning("cannot create %s: %s", target.parent, exc)
            return job

        existing = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}

        job.status = TransferStatus.DOWNLOADING
        started = time.time()
        written = existing
        last_report = 0.0

        try:
            with self.api.client.stream(
                "GET", self.api.url_for(remote), headers=headers
            ) as response:
                if response.status_code == 416:
                    pass  # already complete
                elif response.status_code >= 400:
                    raise translate_status(response, SERVICE, PROVIDER_ID)
                else:
                    resuming = response.status_code == 206 and existing > 0
                    if existing and not resuming:
                        existing = 0
                        written = 0
                    length = coerce_int(response.headers.get("content-length"))
                    if length is not None:
                        job.total_bytes = length + existing if resuming else length
                    with open(part, "ab" if resuming else "wb") as handle:
                        for chunk in response.iter_bytes(chunk_size=CHUNK):
                            if cancel is not None and cancel.is_set():
                                job.status = TransferStatus.CANCELLED
                                return job
                            with self._guard:
                                if job.id in self._cancelled:
                                    self._cancelled.discard(job.id)
                                    job.status = TransferStatus.CANCELLED
                                    return job
                            handle.write(chunk)
                            written += len(chunk)
                            job.downloaded_bytes = written
                            elapsed = max(1e-6, time.time() - started)
                            speed = (written - existing) / elapsed
                            job.speed_bps = speed
                            if job.total_bytes and speed > 0:
                                job.eta_seconds = int(
                                    max(0, (job.total_bytes - written) / speed)
                                )
                            now = time.time()
                            if on_progress and now - last_report > 0.25:
                                last_report = now
                                on_progress(written, job.total_bytes, speed)
        except TrackfetchError as exc:
            job.status = TransferStatus.FAILED
            job.error = exc.message
            job.remedy = exc.remedy
            return job
        except httpx.HTTPError as exc:
            translated = translate_http_error(exc, SERVICE, PROVIDER_ID)
            job.status = TransferStatus.FAILED
            job.error = getattr(translated, "message", "The download was interrupted.")
            job.remedy = "Click Retry. Trackfetch will carry on from where it stopped."
            return job
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch could not write to the download folder."
            job.remedy = "Check there is space on the disk and the folder still exists."
            log.warning("webdav write failed: %s", exc)
            return job

        try:
            if target.exists():
                target.unlink()
            part.replace(target)
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch could not save the finished file."
            job.remedy = "Check the download folder still exists and is writable."
            log.warning("could not move %s: %s", part, exc)
            return job

        job.status = TransferStatus.COMPLETE
        job.destination = str(target)
        job.downloaded_bytes = job.total_bytes or target.stat().st_size
        job.completed_at = time.time()
        job.speed_bps = None
        job.eta_seconds = None
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


def _text(element, tag: str) -> str:
    if element is None:
        return ""
    found = element.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _http_date(value: str) -> float | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


def create(ctx: ProviderContext) -> WebdavProvider:
    return WebdavProvider(ctx)
