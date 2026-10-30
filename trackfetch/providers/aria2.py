"""aria2, through its documented JSON-RPC interface.

aria2 handles web addresses, magnet links and torrent files with one small,
fast program. Calls are ``POST`` to the RPC address with a JSON-RPC 2.0 body. If
aria2 was started with ``--rpc-secret``, the token is passed as the first
parameter in the documented ``token:<secret>`` form.

Methods used: ``aria2.getVersion``, ``aria2.addUri``, ``aria2.addTorrent``,
``aria2.tellStatus``, ``aria2.pause``, ``aria2.unpause``, ``aria2.remove``.
"""

from __future__ import annotations

import base64
import itertools
import logging
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
from trackfetch.providers.base import Provider, ProviderContext
from trackfetch.providers.errors import (
    AuthenticationFailed,
    RemoteServiceError,
    TrackfetchError,
)

log = logging.getLogger(__name__)

PROVIDER_ID = "aria2"
SERVICE = "aria2"

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
            Capability.CONNECTION_TEST,
        }
    ),
    workflow="Hand a link, magnet or torrent to aria2 and watch it here.",
    requires_app="aria2",
)

STATUS_MAP = {
    "active": TransferStatus.DOWNLOADING,
    "waiting": TransferStatus.QUEUED,
    "paused": TransferStatus.PAUSED,
    "error": TransferStatus.FAILED,
    "complete": TransferStatus.COMPLETE,
    "removed": TransferStatus.CANCELLED,
}

STATUS_FIELDS = [
    "gid",
    "status",
    "totalLength",
    "completedLength",
    "downloadSpeed",
    "errorCode",
    "errorMessage",
    "dir",
    "files",
]


class Aria2Provider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._api: HttpApi | None = None
        self._ids = itertools.count(1)

    # -- plumbing ----------------------------------------------------------

    @property
    def api(self) -> HttpApi:
        if self._api is None:
            url = normalise_base_url(
                self.ctx.config("base_url", "http://localhost:6800/jsonrpc")
            )
            self._api = HttpApi(
                url,
                service_name=SERVICE,
                provider_id=PROVIDER_ID,
                verify_tls=bool(self.ctx.config("verify_tls", True)),
                timeout=30,
            )
        return self._api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None

    def is_configured(self) -> bool:
        return bool(self.ctx.config("base_url"))

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        secret = self.ctx.secret("secret")
        full_params: list[Any] = []
        if secret:
            full_params.append(f"token:{secret}")
        full_params.extend(params or [])
        body = {
            "jsonrpc": "2.0",
            "id": str(next(self._ids)),
            "method": method,
            "params": full_params,
        }
        payload = as_mapping(self.api.json("POST", "", json=body))
        error = as_mapping(payload.get("error"))
        if error:
            message = coerce_str(error.get("message"))
            if "unauthorized" in message.lower() or coerce_int(error.get("code")) == 1:
                raise AuthenticationFailed(
                    "aria2 rejected the secret token.",
                    "Open Settings, choose aria2 and check the secret token matches "
                    "the one aria2 was started with.",
                    technical=message,
                    provider_id=PROVIDER_ID,
                )
            raise RemoteServiceError(
                "aria2 refused that request.",
                "Check aria2 is running with its remote control switched on.",
                technical=message,
                provider_id=PROVIDER_ID,
            )
        return payload.get("result")

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
            result = as_mapping(self.call("aria2.getVersion"))
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
        return self._status_ok(f"Connected to aria2 {coerce_str(result.get('version'))}.")

    # -- jobs --------------------------------------------------------------

    def build_job(
        self, candidate: SearchCandidate, destination_dir: Path, label: str = ""
    ) -> TransferJob:
        job = super().build_job(candidate, destination_dir, label)
        job.destination = str(destination_dir)
        if not job.payload.get("source") and candidate.payload.get("url"):
            job.payload["source"] = candidate.payload["url"]
        return job

    def submit(self, job: TransferJob) -> TransferJob:
        source = coerce_str(job.payload.get("source")) or coerce_str(job.payload.get("url"))
        if not source:
            job.status = TransferStatus.FAILED
            job.error = "There is nothing for aria2 to download."
            job.remedy = "Add the link again."
            return job

        options = {"dir": job.destination or str(self.ctx.download_dir())}
        if source.lower().endswith(".torrent") and Path(source).exists():
            encoded = base64.b64encode(Path(source).read_bytes()).decode("ascii")
            gid = self.call("aria2.addTorrent", [encoded, [], options])
        else:
            gid = self.call("aria2.addUri", [[source], options])

        job.remote_id = coerce_str(gid)
        job.status = TransferStatus.QUEUED
        job.error = ""
        job.remedy = ""
        job.attempts += 1
        job.touch()
        return job

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        relevant = [j for j in jobs if j.provider_id == PROVIDER_ID and not j.status.is_terminal]
        changed: list[TransferJob] = []
        for job in relevant:
            if not job.remote_id:
                continue
            try:
                record = as_mapping(
                    self.call("aria2.tellStatus", [job.remote_id, STATUS_FIELDS])
                )
            except Exception as exc:
                log.debug("could not read aria2 status: %r", exc)
                continue
            if record and self._apply(job, dict(record)):
                changed.append(job)
        return changed

    @staticmethod
    def _apply(job: TransferJob, record: dict[str, Any]) -> bool:
        before = (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)
        job.status = STATUS_MAP.get(
            coerce_str(record.get("status")).lower(), TransferStatus.QUEUED
        )
        job.total_bytes = coerce_int(record.get("totalLength")) or job.total_bytes
        job.downloaded_bytes = (
            coerce_int(record.get("completedLength")) or job.downloaded_bytes
        )
        speed = coerce_int(record.get("downloadSpeed"))
        if speed is not None:
            job.speed_bps = float(speed)
            if speed > 0 and job.total_bytes:
                job.eta_seconds = int(
                    max(0, (job.total_bytes - job.downloaded_bytes) / speed)
                )
        files = as_list(record.get("files"))
        if files:
            path = coerce_str(as_mapping(files[0]).get("path"))
            if path:
                job.destination = path
                job.label = Path(path).name or job.label
        if job.status is TransferStatus.FAILED:
            job.error = "aria2 could not finish that download."
            job.remedy = "Check the link still works, then click Retry."
        job.touch()
        return before != (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)

    # -- controls ----------------------------------------------------------

    def pause(self, job: TransferJob) -> bool:
        if not job.remote_id:
            return False
        try:
            self.call("aria2.pause", [job.remote_id])
        except TrackfetchError:
            return False
        job.status = TransferStatus.PAUSED
        job.touch()
        return True

    def resume(self, job: TransferJob) -> bool:
        if not job.remote_id:
            return False
        try:
            self.call("aria2.unpause", [job.remote_id])
        except TrackfetchError:
            return False
        job.status = TransferStatus.QUEUED
        job.touch()
        return True

    def cancel(self, job: TransferJob) -> bool:
        if job.remote_id:
            try:
                self.call("aria2.remove", [job.remote_id])
            except TrackfetchError:
                pass
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True


def create(ctx: ProviderContext) -> Aria2Provider:
    return Aria2Provider(ctx)
