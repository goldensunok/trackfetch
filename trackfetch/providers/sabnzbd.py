"""SABnzbd, through its documented HTTP API.

Every call is ``GET /api`` (or ``POST /api`` when uploading a file) with
``apikey``, ``mode`` and ``output=json``.

Modes used: ``version``, ``queue``, ``history``, ``addurl``, ``addfile``, and
``queue`` with ``name=pause``, ``name=resume`` or ``name=delete``.
"""

from __future__ import annotations

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

PROVIDER_ID = "sabnzbd"
SERVICE = "SABnzbd"

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
    workflow="Send an NZB file to SABnzbd and watch it here.",
    requires_app="SABnzbd",
)

STATUS_MAP = {
    "Queued": TransferStatus.QUEUED,
    "Grabbing": TransferStatus.QUEUED,
    "Fetching": TransferStatus.DOWNLOADING,
    "Downloading": TransferStatus.DOWNLOADING,
    "Verifying": TransferStatus.DOWNLOADING,
    "Repairing": TransferStatus.DOWNLOADING,
    "Extracting": TransferStatus.DOWNLOADING,
    "Moving": TransferStatus.DOWNLOADING,
    "Running": TransferStatus.DOWNLOADING,
    "Paused": TransferStatus.PAUSED,
    "Completed": TransferStatus.COMPLETE,
    "Failed": TransferStatus.FAILED,
}


class SabnzbdProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._api: HttpApi | None = None

    # -- plumbing ----------------------------------------------------------

    @property
    def api(self) -> HttpApi:
        if self._api is None:
            base = normalise_base_url(self.ctx.config("base_url", "http://localhost:8080"))
            self._api = HttpApi(
                base,
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
        return bool(self.ctx.config("base_url")) and bool(self.ctx.secret("api_key"))

    def call(self, mode: str, params: dict[str, Any] | None = None, files=None) -> Any:
        query = {"mode": mode, "output": "json", "apikey": self.ctx.secret("api_key")}
        query.update(params or {})
        method = "POST" if files else "GET"
        if files:
            response = self.api.request(
                method, "/api", params=query, files=files, raise_for_status=False
            )
        else:
            response = self.api.request(method, "/api", params=query, raise_for_status=False)

        if response.status_code in (401, 403):
            raise AuthenticationFailed(
                "SABnzbd rejected the API key.",
                "Open Settings, choose SABnzbd and paste the key from its Config, "
                "General page.",
                technical=f"HTTP {response.status_code}",
                provider_id=PROVIDER_ID,
            )
        if response.status_code >= 400:
            from trackfetch.providers._http import translate_status

            raise translate_status(response, SERVICE, PROVIDER_ID)

        text = (response.text or "").strip()
        if text.lower().startswith("api key incorrect") or "api key required" in text.lower():
            raise AuthenticationFailed(
                "SABnzbd rejected the API key.",
                "Open Settings, choose SABnzbd and paste the key from its Config, "
                "General page.",
                provider_id=PROVIDER_ID,
            )
        payload = self.api.parse_json(response)
        node = as_mapping(payload)
        if node.get("status") is False and node.get("error"):
            raise RemoteServiceError(
                "SABnzbd refused that request.",
                "Open SABnzbd to see what it says, then try again.",
                technical=coerce_str(node.get("error")),
                provider_id=PROVIDER_ID,
            )
        return payload

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
            payload = as_mapping(self.call("version"))
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
        return self._status_ok(
            f"Connected to SABnzbd {coerce_str(payload.get('version'))}.".strip()
        )

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
            job.error = "There is no NZB for SABnzbd to fetch."
            job.remedy = "Add the NZB file again."
            return job

        if source.lower().startswith(("http://", "https://")):
            payload = as_mapping(self.call("addurl", {"name": source}))
        else:
            path = Path(source)
            if not path.exists():
                job.status = TransferStatus.FAILED
                job.error = "Trackfetch could not find that NZB file."
                job.remedy = "Check it has not been moved, then add it again."
                return job
            files = {"nzbfile": (path.name, path.read_bytes(), "application/x-nzb")}
            payload = as_mapping(self.call("addfile", {"nzbname": path.stem}, files=files))
            job.label = path.name

        ids = as_list(payload.get("nzo_ids"))
        if not ids:
            job.status = TransferStatus.FAILED
            job.error = "SABnzbd did not accept that NZB file."
            job.remedy = "Check the file is a valid NZB, then try again."
            job.touch()
            return job

        job.remote_id = coerce_str(ids[0])
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
            queue = as_mapping(as_mapping(self.call("queue")).get("queue"))
            history = as_mapping(as_mapping(self.call("history")).get("history"))
        except Exception as exc:
            log.debug("could not read the SABnzbd queue: %r", exc)
            return []

        slots = {
            coerce_str(as_mapping(s).get("nzo_id")): dict(as_mapping(s))
            for s in as_list(queue.get("slots"))
        }
        finished = {
            coerce_str(as_mapping(s).get("nzo_id")): dict(as_mapping(s))
            for s in as_list(history.get("slots"))
        }

        changed: list[TransferJob] = []
        for job in relevant:
            record = slots.get(job.remote_id)
            if record is not None:
                if self._apply_queue(job, record):
                    changed.append(job)
                continue
            done = finished.get(job.remote_id)
            if done is not None and self._apply_history(job, done):
                changed.append(job)
        return changed

    @staticmethod
    def _apply_queue(job: TransferJob, record: dict[str, Any]) -> bool:
        before = (job.status, job.downloaded_bytes, job.total_bytes)
        job.status = STATUS_MAP.get(
            coerce_str(record.get("status")), TransferStatus.QUEUED
        )
        total_mb = _float(record.get("mb"))
        left_mb = _float(record.get("mbleft"))
        if total_mb is not None:
            job.total_bytes = int(total_mb * 1024 * 1024)
        if total_mb is not None and left_mb is not None:
            job.downloaded_bytes = int(max(0.0, total_mb - left_mb) * 1024 * 1024)
        name = coerce_str(record.get("filename"))
        if name:
            job.label = name
        job.eta_seconds = _timeleft_seconds(coerce_str(record.get("timeleft")))
        job.touch()
        return before != (job.status, job.downloaded_bytes, job.total_bytes)

    @staticmethod
    def _apply_history(job: TransferJob, record: dict[str, Any]) -> bool:
        before = job.status
        status = coerce_str(record.get("status"))
        job.status = STATUS_MAP.get(status, TransferStatus.COMPLETE)
        if job.status is TransferStatus.FAILED:
            job.error = "SABnzbd could not finish that download."
            job.remedy = coerce_str(record.get("fail_message")) or (
                "Open SABnzbd to see the detail. The posting may be incomplete."
            )
        folder = coerce_str(record.get("storage") or record.get("path"))
        if folder:
            job.destination = folder
        if job.status is TransferStatus.COMPLETE:
            job.downloaded_bytes = job.total_bytes or job.downloaded_bytes
        job.touch()
        return before != job.status

    # -- controls ----------------------------------------------------------

    def pause(self, job: TransferJob) -> bool:
        if not job.remote_id:
            return False
        try:
            self.call("queue", {"name": "pause", "value": job.remote_id})
        except TrackfetchError:
            return False
        job.status = TransferStatus.PAUSED
        job.touch()
        return True

    def resume(self, job: TransferJob) -> bool:
        if not job.remote_id:
            return False
        try:
            self.call("queue", {"name": "resume", "value": job.remote_id})
        except TrackfetchError:
            return False
        job.status = TransferStatus.QUEUED
        job.touch()
        return True

    def cancel(self, job: TransferJob) -> bool:
        if job.remote_id:
            try:
                self.call("queue", {"name": "delete", "value": job.remote_id})
            except TrackfetchError:
                pass
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timeleft_seconds(text: str) -> int | None:
    if not text or text.count(":") < 1:
        return None
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return None


def create(ctx: ProviderContext) -> SabnzbdProvider:
    return SabnzbdProvider(ctx)
