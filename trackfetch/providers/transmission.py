"""Transmission, through its documented RPC interface.

All calls are ``POST`` to the RPC address with a JSON body of the form
``{"method": ..., "arguments": {...}}``. Transmission answers the first request
with ``409 Conflict`` and an ``X-Transmission-Session-Id`` header, which must be
echoed back on every later call. That handshake is handled automatically here.

Methods used: ``session-get``, ``torrent-add``, ``torrent-get``,
``torrent-start``, ``torrent-stop``, ``torrent-remove``.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Sequence

import httpx

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
from trackfetch.providers._http import HttpApi, normalise_base_url, translate_status
from trackfetch.providers.base import Provider, ProviderContext
from trackfetch.providers.errors import (
    AuthenticationFailed,
    InvalidResponse,
    RemoteServiceError,
    TrackfetchError,
)

log = logging.getLogger(__name__)

PROVIDER_ID = "transmission"
SERVICE = "Transmission"
SESSION_HEADER = "X-Transmission-Session-Id"

INFO = ProviderInfo(
    provider_id=PROVIDER_ID,
    display_name=SERVICE,
    capabilities=frozenset(
        {
            Capability.ADD_JOB,
            Capability.QUEUE,
            Capability.PROGRESS,
            Capability.PAUSE,
            Capability.RESUME,
            Capability.CANCEL,
            Capability.AUTH,
            Capability.CONNECTION_TEST,
        }
    ),
    workflow="Send a torrent or magnet link to Transmission and watch it here.",
    requires_app="Transmission",
)

FIELDS = [
    "id",
    "name",
    "status",
    "totalSize",
    "sizeWhenDone",
    "leftUntilDone",
    "downloadedEver",
    "rateDownload",
    "eta",
    "percentDone",
    "errorString",
    "error",
    "downloadDir",
    "hashString",
    "isFinished",
]

STATUS_MAP = {
    0: TransferStatus.PAUSED,
    1: TransferStatus.QUEUED,
    2: TransferStatus.DOWNLOADING,
    3: TransferStatus.QUEUED,
    4: TransferStatus.DOWNLOADING,
    5: TransferStatus.COMPLETE,
    6: TransferStatus.COMPLETE,
}


class TransmissionProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._api: HttpApi | None = None
        self._session_id = ""

    # -- plumbing ----------------------------------------------------------

    @property
    def api(self) -> HttpApi:
        if self._api is None:
            url = normalise_base_url(
                self.ctx.config("base_url", "http://localhost:9091/transmission/rpc")
            )
            username = coerce_str(self.ctx.config("username", ""))
            password = self.ctx.secret("password")
            auth = (username, password) if username or password else None
            self._api = HttpApi(
                url,
                service_name=SERVICE,
                provider_id=PROVIDER_ID,
                verify_tls=bool(self.ctx.config("verify_tls", True)),
                timeout=30,
                auth=auth,
            )
        return self._api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None

    def is_configured(self) -> bool:
        return bool(self.ctx.config("base_url"))

    def call(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"method": method, "arguments": arguments or {}}
        response = self._post(body)
        if response.status_code == 409:
            self._session_id = response.headers.get(SESSION_HEADER, "")
            response = self._post(body)
        if response.status_code in (401, 403):
            raise AuthenticationFailed(
                "Transmission rejected the login details.",
                "Open Settings, choose Transmission and check the username and "
                "password from its remote access options.",
                technical=f"HTTP {response.status_code}",
                provider_id=PROVIDER_ID,
            )
        if response.status_code >= 400:
            raise translate_status(response, SERVICE, PROVIDER_ID)

        payload = as_mapping(self.api.parse_json(response))
        result = coerce_str(payload.get("result"))
        if result and result != "success":
            raise RemoteServiceError(
                "Transmission refused that request.",
                "Open Transmission to see what it says, then try again.",
                technical=result,
                provider_id=PROVIDER_ID,
            )
        return dict(as_mapping(payload.get("arguments")))

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        headers = {SESSION_HEADER: self._session_id} if self._session_id else {}
        return self.api.request("POST", "", json=body, headers=headers, raise_for_status=False)

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
            return self._status_not_configured()
        try:
            session = self.call("session-get")
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
        version = coerce_str(session.get("version"))
        return self._status_ok(f"Connected to {SERVICE} {version}.".strip())

    # -- jobs --------------------------------------------------------------

    def build_job(
        self, candidate: SearchCandidate, destination_dir: Path, label: str = ""
    ) -> TransferJob:
        job = super().build_job(candidate, destination_dir, label)
        job.destination = str(destination_dir)
        return job

    def submit(self, job: TransferJob) -> TransferJob:
        source = coerce_str(job.payload.get("source"))
        if not source:
            job.status = TransferStatus.FAILED
            job.error = "There is nothing for Transmission to download."
            job.remedy = "Add the torrent or magnet link again."
            return job

        arguments: dict[str, Any] = {
            "download-dir": job.destination or str(self.ctx.download_dir())
        }
        if source.lower().startswith(("magnet:", "http://", "https://")):
            arguments["filename"] = source
        else:
            path = Path(source)
            if not path.exists():
                job.status = TransferStatus.FAILED
                job.error = "Trackfetch could not find that torrent file."
                job.remedy = "Check it has not been moved, then add it again."
                return job
            arguments["metainfo"] = base64.b64encode(path.read_bytes()).decode("ascii")

        result = self.call("torrent-add", arguments)
        added = as_mapping(result.get("torrent-added")) or as_mapping(
            result.get("torrent-duplicate")
        )
        if not added:
            raise InvalidResponse(
                "Transmission did not say whether it accepted that torrent.",
                "Open Transmission and check whether it appeared.",
                provider_id=PROVIDER_ID,
            )
        job.remote_id = coerce_str(added.get("id"))
        job.label = coerce_str(added.get("name")) or job.label
        job.status = TransferStatus.QUEUED
        job.error = ""
        job.remedy = ""
        job.attempts += 1
        job.touch()
        return job

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        relevant = [j for j in jobs if j.provider_id == PROVIDER_ID and not j.status.is_terminal]
        ids = [int(j.remote_id) for j in relevant if j.remote_id.isdigit()]
        if not ids:
            return []
        try:
            result = self.call("torrent-get", {"ids": ids, "fields": FIELDS})
        except Exception as exc:
            log.debug("could not read Transmission torrents: %r", exc)
            return []
        by_id = {
            coerce_str(as_mapping(t).get("id")): dict(as_mapping(t))
            for t in as_list(result.get("torrents"))
        }
        changed: list[TransferJob] = []
        for job in relevant:
            record = by_id.get(job.remote_id)
            if record is None:
                continue
            if self._apply(job, record):
                changed.append(job)
        return changed

    @staticmethod
    def _apply(job: TransferJob, record: dict[str, Any]) -> bool:
        before = (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)
        status_code = coerce_int(record.get("status"))
        job.status = STATUS_MAP.get(status_code, TransferStatus.QUEUED)

        total = coerce_int(record.get("sizeWhenDone")) or coerce_int(record.get("totalSize"))
        if total:
            job.total_bytes = total
        left = coerce_int(record.get("leftUntilDone"))
        if total is not None and left is not None:
            job.downloaded_bytes = max(0, total - left)
        else:
            job.downloaded_bytes = coerce_int(record.get("downloadedEver")) or job.downloaded_bytes

        rate = record.get("rateDownload")
        if isinstance(rate, (int, float)):
            job.speed_bps = float(rate)
        eta = coerce_int(record.get("eta"))
        job.eta_seconds = eta if eta and eta > 0 else None

        name = coerce_str(record.get("name"))
        if name:
            job.label = name
        folder = coerce_str(record.get("downloadDir"))
        if folder:
            job.destination = folder

        error_text = coerce_str(record.get("errorString"))
        if coerce_int(record.get("error")) and error_text:
            job.status = TransferStatus.FAILED
            job.error = "Transmission reported a problem with this torrent."
            job.remedy = "Open Transmission to see the detail, then click Retry here."
        job.touch()
        return before != (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)

    # -- controls ----------------------------------------------------------

    def _ids(self, job: TransferJob) -> list[int]:
        return [int(job.remote_id)] if job.remote_id.isdigit() else []

    def pause(self, job: TransferJob) -> bool:
        ids = self._ids(job)
        if not ids:
            return False
        try:
            self.call("torrent-stop", {"ids": ids})
        except TrackfetchError:
            return False
        job.status = TransferStatus.PAUSED
        job.touch()
        return True

    def resume(self, job: TransferJob) -> bool:
        ids = self._ids(job)
        if not ids:
            return False
        try:
            self.call("torrent-start", {"ids": ids})
        except TrackfetchError:
            return False
        job.status = TransferStatus.QUEUED
        job.touch()
        return True

    def cancel(self, job: TransferJob) -> bool:
        ids = self._ids(job)
        if ids:
            try:
                self.call("torrent-remove", {"ids": ids, "delete-local-data": False})
            except TrackfetchError:
                return False
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True


def create(ctx: ProviderContext) -> TransmissionProvider:
    return TransmissionProvider(ctx)
