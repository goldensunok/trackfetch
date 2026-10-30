"""Ordinary web downloads over HTTP and HTTPS.

Workflow: URL in, resolve, download. Trackfetch fetches the bytes itself here,
rather than handing the job to another program.

Handles redirects, works out a sensible filename, reports progress, retries a
failed transfer with a backoff, resumes a part-finished file when the server
supports byte ranges, applies sensible timeouts and checks a supplied checksum.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlparse

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
from trackfetch.providers._http import translate_http_error, translate_status
from trackfetch.providers.base import Provider, ProviderContext, ProgressCallback
from trackfetch.providers.errors import (
    DestinationError,
    RemoteServiceError,
    TrackfetchError,
)
from trackfetch.services.destinations import (
    ensure_parent,
    sanitise_filename,
    verify_checksum,
)

log = logging.getLogger(__name__)

PROVIDER_ID = "http"
CHUNK = 262144
MAX_ATTEMPTS = 3

INFO = ProviderInfo(
    provider_id=PROVIDER_ID,
    display_name="Web download",
    capabilities=frozenset(
        {
            Capability.RESOLVE,
            Capability.DOWNLOAD,
            Capability.PROGRESS,
            Capability.PAUSE,
            Capability.RESUME,
            Capability.CANCEL,
            Capability.CONNECTION_TEST,
        }
    ),
    workflow="Paste a direct link and Trackfetch downloads the file.",
)

_FILENAME_STAR = re.compile(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", re.IGNORECASE)
_FILENAME_PLAIN = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


class HttpProvider(Provider):
    """Downloads a direct link, with resume and retries."""

    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._cancelled: set[str] = set()
        self._paused: set[str] = set()
        self._lock = threading.RLock()

    # -- configuration -----------------------------------------------------

    def is_configured(self) -> bool:
        return True  # nothing to set up

    def _client(self, timeout: float | None = None) -> httpx.Client:
        read_timeout = timeout or float(self.ctx.config("timeout", 60) or 60)
        return httpx.Client(
            follow_redirects=True,
            verify=bool(self.ctx.config("verify_tls", True)),
            timeout=httpx.Timeout(read_timeout, connect=10.0),
            headers={"User-Agent": self._user_agent()},
        )

    @staticmethod
    def _user_agent() -> str:
        from trackfetch import __version__

        return f"Trackfetch/{__version__}"

    def test_connection(self) -> ConnectionStatus:
        if not self.enabled:
            return self._set_status(
                ConnectionStatus(
                    provider_id=PROVIDER_ID,
                    state=ConnectionState.DISABLED,
                    detail="Web downloads are switched off.",
                    remedy="Open Settings and switch them on.",
                )
            )
        return self._status_ok("Web downloads are ready. No setup needed.")

    # -- resolve -----------------------------------------------------------

    def resolve(self, target: str) -> SearchCandidate:
        """Look at a link and describe the file behind it."""
        url = target.strip()
        if not url:
            raise RemoteServiceError(
                "That link is empty.",
                "Paste a full web address starting with http or https.",
                provider_id=PROVIDER_ID,
            )
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url

        filename = filename_from_url(url)
        size: int | None = None
        content_type = ""
        final_url = url
        try:
            with self._client(timeout=20) as client:
                response = client.head(url)
                if response.status_code in (403, 405, 501) or response.status_code >= 400:
                    # Some servers do not answer HEAD. Ask for one byte instead.
                    response = client.get(url, headers={"Range": "bytes=0-0"})
                if response.status_code >= 400:
                    raise translate_status(response, "That website", PROVIDER_ID)
                final_url = str(response.url)
                filename = (
                    filename_from_headers(dict(response.headers))
                    or filename_from_url(final_url)
                    or filename
                )
                size = size_from_headers(dict(response.headers))
                content_type = coerce_str(response.headers.get("content-type")).split(";")[0]
        except TrackfetchError:
            raise
        except httpx.HTTPError as exc:
            raise translate_http_error(exc, "That website", PROVIDER_ID) from exc

        extension = Path(filename).suffix.lstrip(".").lower()
        candidate = SearchCandidate(
            provider_id=PROVIDER_ID,
            filename=filename,
            directory=urlparse(final_url).netloc,
            extension=extension,
            size=size,
            codec=content_type,
            relevance=1.0,
            raw={"url": final_url, "content_type": content_type},
        )
        candidate.payload = {"url": final_url, "filename": filename}
        return candidate

    # -- downloading -------------------------------------------------------

    def build_job(
        self, candidate: SearchCandidate, destination_dir: Path, label: str = ""
    ) -> TransferJob:
        job = super().build_job(candidate, destination_dir, label)
        job.payload.setdefault("url", candidate.raw.get("url", ""))
        job.payload.setdefault("filename", candidate.filename)
        job.label = label or candidate.filename or job.payload.get("url", "")
        return job

    def download(
        self,
        job: TransferJob,
        on_progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> TransferJob:
        url = coerce_str(job.payload.get("url"))
        if not url:
            job.status = TransferStatus.FAILED
            job.error = "That download has no web address."
            job.remedy = "Remove it from the list and add the link again."
            return job

        target = Path(job.payload.get("target_path") or "")
        if not target or str(target) == ".":
            filename = sanitise_filename(
                coerce_str(job.payload.get("filename")) or filename_from_url(url)
            )
            target = Path(job.destination or str(self.ctx.download_dir())) / filename
            job.payload["target_path"] = str(target)

        part = target.with_name(target.name + ".part")
        try:
            ensure_parent(target)
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = DestinationError.default_message
            job.remedy = DestinationError.default_remedy
            log.warning("cannot create %s: %s", target.parent, exc)
            return job

        # Clear any stale pause or cancel flag left over from a previous run.
        # Cancelling a job that was only waiting sets the flag with nothing
        # running to consume it, which would otherwise kill the next attempt
        # the instant it started.
        with self._lock:
            self._cancelled.discard(job.id)
            self._paused.discard(job.id)

        job.status = TransferStatus.DOWNLOADING
        job.error = ""
        job.remedy = ""
        job.touch()

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            job.attempts = attempt
            try:
                self._stream_to_file(job, url, part, on_progress, cancel)
            except _Cancelled:
                job.status = TransferStatus.CANCELLED
                job.touch()
                return job
            except _Paused:
                job.status = TransferStatus.PAUSED
                job.touch()
                return job
            except TrackfetchError as exc:
                last_error = exc
                if not _is_retryable(exc) or attempt == MAX_ATTEMPTS:
                    break
                time.sleep(min(8.0, 1.5**attempt))
                continue
            except Exception as exc:  # pragma: no cover - defensive
                last_error = exc
                break
            else:
                last_error = None
                break

        if last_error is not None:
            job.status = TransferStatus.FAILED
            if isinstance(last_error, TrackfetchError):
                job.error = last_error.message
                job.remedy = last_error.remedy
            else:
                job.error = "The download did not finish."
                job.remedy = "Check the link still works, then click Retry."
            job.touch()
            return job

        checksum = coerce_str(job.payload.get("checksum"))
        if checksum:
            try:
                if not verify_checksum(part, checksum):
                    part.unlink(missing_ok=True)
                    job.status = TransferStatus.FAILED
                    job.error = "The downloaded file did not match the checksum supplied."
                    job.remedy = "The file may be damaged. Try downloading it again."
                    job.touch()
                    return job
            except OSError as exc:  # pragma: no cover - defensive
                log.warning("checksum check failed: %s", exc)

        try:
            if target.exists():
                target.unlink()
            part.replace(target)
        except OSError as exc:
            job.status = TransferStatus.FAILED
            job.error = "Trackfetch could not save the finished file."
            job.remedy = "Check the download folder still exists and is writable."
            log.warning("could not move %s to %s: %s", part, target, exc)
            job.touch()
            return job

        job.status = TransferStatus.COMPLETE
        job.destination = str(target)
        job.downloaded_bytes = job.total_bytes or target.stat().st_size
        job.total_bytes = job.total_bytes or job.downloaded_bytes
        job.completed_at = time.time()
        job.speed_bps = None
        job.eta_seconds = None
        job.touch()
        return job

    def _stream_to_file(
        self,
        job: TransferJob,
        url: str,
        part: Path,
        on_progress: ProgressCallback | None,
        cancel: threading.Event | None,
    ) -> None:
        existing = part.stat().st_size if part.exists() else 0
        headers: dict[str, str] = {}
        if existing:
            headers["Range"] = f"bytes={existing}-"

        with self._client() as client:
            try:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 416:
                        # The part file is already the whole file.
                        return
                    if response.status_code >= 400:
                        raise translate_status(response, "That website", PROVIDER_ID)

                    resuming = response.status_code == 206 and existing > 0
                    if existing and not resuming:
                        # The server ignored the range, so start again cleanly.
                        existing = 0

                    headers = dict(response.headers)
                    total = size_from_headers(headers)
                    if total is not None:
                        # Content-Range already carries the whole file's length,
                        # so only a bare Content-Length has to have the bytes
                        # already on disk added to it.
                        has_range = any(
                            k.lower() == "content-range" for k in headers
                        )
                        job.total_bytes = (
                            total if has_range or not resuming else total + existing
                        )
                    job.downloaded_bytes = existing

                    mode = "ab" if resuming else "wb"
                    started = time.time()
                    written = existing
                    last_report = 0.0

                    with open(part, mode) as handle:
                        for chunk in response.iter_bytes(chunk_size=CHUNK):
                            if cancel is not None and cancel.is_set():
                                raise _Cancelled()
                            with self._lock:
                                if job.id in self._cancelled:
                                    self._cancelled.discard(job.id)
                                    raise _Cancelled()
                                if job.id in self._paused:
                                    self._paused.discard(job.id)
                                    raise _Paused()
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
                    if on_progress:
                        on_progress(written, job.total_bytes, job.speed_bps or 0.0)
            except (_Cancelled, _Paused, TrackfetchError):
                raise
            except httpx.HTTPError as exc:
                raise translate_http_error(exc, "That website", PROVIDER_ID) from exc
            except OSError as exc:
                raise DestinationError(
                    "Trackfetch could not write to the download folder.",
                    "Check the folder exists and there is space on the disk.",
                    technical=repr(exc),
                    provider_id=PROVIDER_ID,
                ) from exc

    # -- controls ----------------------------------------------------------

    def pause(self, job: TransferJob) -> bool:
        with self._lock:
            self._paused.add(job.id)
        return True

    def resume(self, job: TransferJob) -> bool:
        with self._lock:
            self._paused.discard(job.id)
        job.status = TransferStatus.WAITING
        job.touch()
        return True

    def cancel(self, job: TransferJob) -> bool:
        with self._lock:
            self._cancelled.add(job.id)
        target = coerce_str(job.payload.get("target_path"))
        if target:
            Path(target + ".part").unlink(missing_ok=True)
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        return []  # progress is reported live while downloading


class _Cancelled(Exception):
    pass


class _Paused(Exception):
    pass


def _is_retryable(exc: TrackfetchError) -> bool:
    from trackfetch.providers.errors import (
        AuthenticationFailed,
        ConnectionFailed as CF,
        OperationTimedOut,
        RemoteServiceError as RSE,
    )

    if isinstance(exc, AuthenticationFailed):
        return False
    if isinstance(exc, (CF, OperationTimedOut)):
        return True
    if isinstance(exc, RSE):
        return "HTTP 5" in (exc.technical or "")
    return False


# ---------------------------------------------------------------------------
# header parsing
# ---------------------------------------------------------------------------


def filename_from_headers(headers: dict[str, str]) -> str:
    disposition = ""
    for key, value in headers.items():
        if key.lower() == "content-disposition":
            disposition = value
            break
    if not disposition:
        return ""
    match = _FILENAME_STAR.search(disposition)
    if match:
        charset = match.group(1) or "utf-8"
        try:
            return sanitise_filename(unquote(match.group(2), encoding=charset))
        except LookupError:
            return sanitise_filename(unquote(match.group(2)))
    match = _FILENAME_PLAIN.search(disposition)
    if match:
        return sanitise_filename(match.group(1).strip())
    return ""


def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return sanitise_filename(name, fallback="download")


def size_from_headers(headers: dict[str, str]) -> int | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    content_range = lowered.get("content-range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    return coerce_int(lowered.get("content-length"))


def modified_from_headers(headers: dict[str, str]) -> float | None:
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get("last-modified")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None


def create(ctx: ProviderContext) -> HttpProvider:
    return HttpProvider(ctx)
