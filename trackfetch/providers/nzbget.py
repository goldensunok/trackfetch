"""NZBGet, through its documented JSON-RPC interface.

Trackfetch does not speak NNTP. It hands NZB files to NZBGet, which does the
Usenet work, and reports the queue back.

Calls are ``POST /jsonrpc`` with ``{"method": ..., "params": [...]}``. NZBGet
only accepts positional parameters, and the order matters, which is why the
``append`` call below lists every argument explicitly.

Methods used: ``version``, ``status``, ``append``, ``listgroups``,
``editqueue``, ``history``.
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

PROVIDER_ID = "nzbget"
SERVICE = "NZBGet"

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
    workflow="Send an NZB file to NZBGet and watch it here.",
    requires_app="NZBGet",
)

STATUS_MAP = {
    "QUEUED": TransferStatus.QUEUED,
    "PAUSED": TransferStatus.PAUSED,
    "DOWNLOADING": TransferStatus.DOWNLOADING,
    "FETCHING": TransferStatus.DOWNLOADING,
    "PP_QUEUED": TransferStatus.DOWNLOADING,
    "LOADING_PARS": TransferStatus.DOWNLOADING,
    "VERIFYING_SOURCES": TransferStatus.DOWNLOADING,
    "REPAIRING": TransferStatus.DOWNLOADING,
    "VERIFYING_REPAIRED": TransferStatus.DOWNLOADING,
    "RENAMING": TransferStatus.DOWNLOADING,
    "UNPACKING": TransferStatus.DOWNLOADING,
    "MOVING": TransferStatus.DOWNLOADING,
    "EXECUTING_SCRIPT": TransferStatus.DOWNLOADING,
    "PP_FINISHED": TransferStatus.COMPLETE,
}


class NzbgetProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._api: HttpApi | None = None
        self._ids = itertools.count(1)

    # -- plumbing ----------------------------------------------------------

    @property
    def api(self) -> HttpApi:
        if self._api is None:
            base = normalise_base_url(self.ctx.config("base_url", "http://localhost:6789"))
            username = coerce_str(self.ctx.config("username", "nzbget"))
            password = self.ctx.secret("password")
            self._api = HttpApi(
                base,
                service_name=SERVICE,
                provider_id=PROVIDER_ID,
                verify_tls=bool(self.ctx.config("verify_tls", True)),
                timeout=30,
                auth=(username, password) if username or password else None,
            )
        return self._api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None

    def is_configured(self) -> bool:
        return bool(self.ctx.config("base_url"))

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        body = {"method": method, "params": params or [], "id": next(self._ids)}
        response = self.api.request("POST", "/jsonrpc", json=body, raise_for_status=False)
        if response.status_code in (401, 403):
            raise AuthenticationFailed(
                "NZBGet rejected the login details.",
                "Open Settings, choose NZBGet and check the control username and "
                "password from its own Security settings.",
                technical=f"HTTP {response.status_code}",
                provider_id=PROVIDER_ID,
            )
        if response.status_code >= 400:
            from trackfetch.providers._http import translate_status

            raise translate_status(response, SERVICE, PROVIDER_ID)
        payload = as_mapping(self.api.parse_json(response))
        error = as_mapping(payload.get("error"))
        if error:
            raise RemoteServiceError(
                "NZBGet refused that request.",
                "Open NZBGet to see what it says, then try again.",
                technical=coerce_str(error.get("message")),
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
            version = coerce_str(self.call("version"))
            status = as_mapping(self.call("status"))
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
        if status.get("ServerStandBy") is False:
            return self._status_ok(f"Connected to NZBGet {version}, downloading.")
        return self._status_ok(f"Connected to NZBGet {version}.")

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
            job.error = "There is no NZB file for NZBGet to fetch."
            job.remedy = "Add the NZB file again."
            return job

        if source.lower().startswith(("http://", "https://")):
            filename = Path(source.split("?")[0]).name or "download.nzb"
            content = source  # NZBGet accepts a URL in place of the file content
        else:
            path = Path(source)
            if not path.exists():
                job.status = TransferStatus.FAILED
                job.error = "Trackfetch could not find that NZB file."
                job.remedy = "Check it has not been moved, then add it again."
                return job
            filename = path.name
            content = base64.b64encode(path.read_bytes()).decode("ascii")

        # Parameter order is fixed by NZBGet's API and must not be rearranged.
        result = self.call(
            "append",
            [
                filename,      # NZBFilename
                content,       # Content: base64 of the file, or a URL
                "",            # Category
                0,             # Priority
                False,         # AddToTop
                False,         # AddPaused
                "",            # DupeKey
                0,             # DupeScore
                "SCORE",       # DupeMode
                [],            # PPParameters
            ],
        )
        nzb_id = coerce_int(result) or 0
        if nzb_id <= 0:
            job.status = TransferStatus.FAILED
            job.error = "NZBGet did not accept that NZB file."
            job.remedy = "Check the file is a valid NZB, then try again."
            job.touch()
            return job

        job.remote_id = str(nzb_id)
        job.label = filename
        job.status = TransferStatus.QUEUED
        job.error = ""
        job.remedy = ""
        job.attempts += 1
        job.touch()
        return job

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        relevant = [j for j in jobs if j.provider_id == PROVIDER_ID and not j.status.is_terminal]
        if not relevant:
            return []
        try:
            groups = [dict(as_mapping(g)) for g in as_list(self.call("listgroups", [0]))]
            history = [dict(as_mapping(h)) for h in as_list(self.call("history", [False]))]
        except Exception as exc:
            log.debug("could not read the NZBGet queue: %r", exc)
            return []

        by_id = {coerce_str(g.get("NZBID")): g for g in groups}
        history_by_id = {coerce_str(h.get("NZBID")): h for h in history}

        changed: list[TransferJob] = []
        for job in relevant:
            record = by_id.get(job.remote_id)
            if record is not None:
                if self._apply_group(job, record):
                    changed.append(job)
                continue
            finished = history_by_id.get(job.remote_id)
            if finished is not None and self._apply_history(job, finished):
                changed.append(job)
        return changed

    @staticmethod
    def _apply_group(job: TransferJob, record: dict[str, Any]) -> bool:
        before = (job.status, job.downloaded_bytes, job.total_bytes)
        job.status = STATUS_MAP.get(
            coerce_str(record.get("Status")).upper(), TransferStatus.QUEUED
        )
        total = _bytes64(record, "FileSize")
        remaining = _bytes64(record, "RemainingSize")
        if total:
            job.total_bytes = total
        if total and remaining is not None:
            job.downloaded_bytes = max(0, total - remaining)
        name = coerce_str(record.get("NZBName"))
        if name:
            job.label = name
        folder = coerce_str(record.get("DestDir") or record.get("FinalDir"))
        if folder:
            job.destination = folder
        job.touch()
        return before != (job.status, job.downloaded_bytes, job.total_bytes)

    @staticmethod
    def _apply_history(job: TransferJob, record: dict[str, Any]) -> bool:
        before = job.status
        status = coerce_str(record.get("Status")).upper()
        if status.startswith("SUCCESS"):
            job.status = TransferStatus.COMPLETE
            job.downloaded_bytes = job.total_bytes or job.downloaded_bytes
        elif status.startswith("DELETED"):
            job.status = TransferStatus.CANCELLED
        else:
            job.status = TransferStatus.FAILED
            job.error = "NZBGet could not finish that download."
            job.remedy = "Open NZBGet to see the detail. The posting may be incomplete."
        folder = coerce_str(record.get("FinalDir") or record.get("DestDir"))
        if folder:
            job.destination = folder
        job.touch()
        return before != job.status

    # -- controls ----------------------------------------------------------

    def _edit(self, command: str, job: TransferJob) -> bool:
        if not job.remote_id.isdigit():
            return False
        try:
            self.call("editqueue", [command, 0, "", [int(job.remote_id)]])
        except TrackfetchError:
            return False
        return True

    def pause(self, job: TransferJob) -> bool:
        if self._edit("GroupPause", job):
            job.status = TransferStatus.PAUSED
            job.touch()
            return True
        return False

    def resume(self, job: TransferJob) -> bool:
        if self._edit("GroupResume", job):
            job.status = TransferStatus.QUEUED
            job.touch()
            return True
        return False

    def cancel(self, job: TransferJob) -> bool:
        self._edit("GroupDelete", job)
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True


def _bytes64(record: dict[str, Any], prefix: str) -> int | None:
    """NZBGet reports 64-bit sizes as separate high and low 32-bit halves."""
    low = coerce_int(record.get(f"{prefix}Lo"))
    high = coerce_int(record.get(f"{prefix}Hi"))
    if low is None and high is None:
        megabytes = coerce_int(record.get(f"{prefix}MB"))
        return megabytes * 1024 * 1024 if megabytes is not None else None
    return (high or 0) * (2**32) + (low or 0)


def create(ctx: ProviderContext) -> NzbgetProvider:
    return NzbgetProvider(ctx)
