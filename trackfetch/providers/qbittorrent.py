"""qBittorrent, through its documented Web UI API (version 2).

Trackfetch does not implement BitTorrent. It hands torrent files and magnet
links to qBittorrent, which the user already runs, and reports their progress.

Endpoints used, all under ``/api/v2``:

    POST /auth/login                 username, password (form encoded)
    GET  /app/version
    GET  /app/webapiVersion
    POST /torrents/add               urls or torrents (multipart), savepath
    GET  /torrents/info              hashes, filter
    POST /torrents/stop  (or /pause on older builds)
    POST /torrents/start (or /resume on older builds)
    POST /torrents/delete            hashes, deleteFiles
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

PROVIDER_ID = "qbittorrent"
SERVICE = "qBittorrent"

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
    workflow="Send a torrent or magnet link to qBittorrent and watch it here.",
    requires_app="qBittorrent",
)

#: qBittorrent state strings mapped onto Trackfetch's vocabulary.
STATE_MAP = {
    "error": TransferStatus.FAILED,
    "missingFiles": TransferStatus.FAILED,
    "unknown": TransferStatus.FAILED,
    "uploading": TransferStatus.COMPLETE,
    "stalledUP": TransferStatus.COMPLETE,
    "queuedUP": TransferStatus.COMPLETE,
    "checkingUP": TransferStatus.COMPLETE,
    "forcedUP": TransferStatus.COMPLETE,
    "pausedUP": TransferStatus.COMPLETE,
    "stoppedUP": TransferStatus.COMPLETE,
    "downloading": TransferStatus.DOWNLOADING,
    "forcedDL": TransferStatus.DOWNLOADING,
    "metaDL": TransferStatus.DOWNLOADING,
    "checkingDL": TransferStatus.DOWNLOADING,
    "allocating": TransferStatus.DOWNLOADING,
    "moving": TransferStatus.DOWNLOADING,
    "stalledDL": TransferStatus.QUEUED,
    "queuedDL": TransferStatus.QUEUED,
    "checkingResumeData": TransferStatus.QUEUED,
    "pausedDL": TransferStatus.PAUSED,
    "stoppedDL": TransferStatus.PAUSED,
}


class QBittorrentProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._api: HttpApi | None = None
        self._logged_in = False
        self._modern_controls: bool | None = None

    # -- plumbing ----------------------------------------------------------

    @property
    def api(self) -> HttpApi:
        if self._api is None:
            base = normalise_base_url(self.ctx.config("base_url", "http://localhost:8080"))
            self._api = HttpApi(
                f"{base}/api/v2",
                service_name=SERVICE,
                provider_id=PROVIDER_ID,
                verify_tls=bool(self.ctx.config("verify_tls", True)),
                timeout=30,
                headers={"Referer": base},
            )
        return self._api

    def close(self) -> None:
        if self._api is not None:
            self._api.close()
            self._api = None
        self._logged_in = False

    def is_configured(self) -> bool:
        return bool(self.ctx.config("base_url"))

    def login(self, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        username = coerce_str(self.ctx.config("username", ""))
        password = self.ctx.secret("password")
        if not username and not password:
            # qBittorrent can be set to allow local connections without a login.
            self._logged_in = True
            return
        response = self.api.request(
            "POST",
            "/auth/login",
            data={"username": username, "password": password},
            raise_for_status=False,
        )
        text = (response.text or "").strip()
        if response.status_code == 403 or text.lower().startswith("fail"):
            raise AuthenticationFailed(
                "qBittorrent rejected the login details.",
                "Open Settings, choose qBittorrent and check the username and "
                "password from its Web UI options.",
                technical=f"HTTP {response.status_code}: {text[:120]}",
                provider_id=PROVIDER_ID,
            )
        if response.status_code >= 400:
            from trackfetch.providers._http import translate_status

            raise translate_status(response, SERVICE, PROVIDER_ID)
        self._logged_in = True

    def _call(self, method: str, path: str, **kwargs: Any):
        self.login()
        response = self.api.request(method, path, raise_for_status=False, **kwargs)
        if response.status_code == 403:
            self.login(force=True)
            response = self.api.request(method, path, raise_for_status=False, **kwargs)
        if response.status_code >= 400:
            from trackfetch.providers._http import translate_status

            raise translate_status(response, SERVICE, PROVIDER_ID)
        return response

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
            version = self._call("GET", "/app/version").text.strip()
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
        return self._status_ok(f"Connected to {SERVICE} {version}.")

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
            job.error = "There is nothing for qBittorrent to download."
            job.remedy = "Add the torrent or magnet link again."
            return job

        data = {"savepath": job.destination or str(self.ctx.download_dir())}
        files = None
        if source.lower().startswith("magnet:") or source.lower().startswith(
            ("http://", "https://")
        ):
            data["urls"] = source
        else:
            path = Path(source)
            if not path.exists():
                job.status = TransferStatus.FAILED
                job.error = "Trackfetch could not find that torrent file."
                job.remedy = "Check it has not been moved, then add it again."
                return job
            files = {"torrents": (path.name, path.read_bytes(), "application/x-bittorrent")}

        before = {t["hash"] for t in self._torrents()}
        self._call("POST", "/torrents/add", data=data, files=files)

        job.status = TransferStatus.QUEUED
        job.error = ""
        job.remedy = ""
        job.attempts += 1
        job.remote_id = _hash_from_magnet(source) or ""
        if not job.remote_id:
            after = self._torrents()
            new = [t for t in after if t.get("hash") not in before]
            if new:
                job.remote_id = coerce_str(new[0].get("hash"))
                job.label = coerce_str(new[0].get("name")) or job.label
        job.touch()
        return job

    def _torrents(self, hashes: Sequence[str] | None = None) -> list[dict[str, Any]]:
        params = {}
        if hashes:
            params["hashes"] = "|".join(hashes)
        response = self._call("GET", "/torrents/info", params=params)
        return [dict(as_mapping(t)) for t in as_list(self.api.parse_json(response))]

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        relevant = [j for j in jobs if j.provider_id == PROVIDER_ID and not j.status.is_terminal]
        if not relevant:
            return []
        try:
            torrents = self._torrents()
        except Exception as exc:
            log.debug("could not read qBittorrent torrents: %r", exc)
            return []
        by_hash = {coerce_str(t.get("hash")).lower(): t for t in torrents}

        changed: list[TransferJob] = []
        for job in relevant:
            record = by_hash.get(job.remote_id.lower()) if job.remote_id else None
            if record is None:
                continue
            if self._apply(job, record):
                changed.append(job)
        return changed

    @staticmethod
    def _apply(job: TransferJob, record: dict[str, Any]) -> bool:
        before = (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)
        state = coerce_str(record.get("state"))
        job.status = STATE_MAP.get(state, TransferStatus.QUEUED)
        job.total_bytes = coerce_int(record.get("size")) or job.total_bytes
        job.downloaded_bytes = coerce_int(record.get("completed")) or job.downloaded_bytes
        speed = record.get("dlspeed")
        if isinstance(speed, (int, float)):
            job.speed_bps = float(speed)
        eta = coerce_int(record.get("eta"))
        job.eta_seconds = eta if eta and eta < 8640000 else None
        name = coerce_str(record.get("name"))
        if name:
            job.label = name
        save_path = coerce_str(record.get("content_path") or record.get("save_path"))
        if save_path:
            job.destination = save_path
        if job.status is TransferStatus.FAILED:
            job.error = "qBittorrent reported a problem with this torrent."
            job.remedy = "Open qBittorrent to see the detail, then click Retry here."
        job.touch()
        return before != (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)

    # -- controls ----------------------------------------------------------

    def _control(self, modern: str, legacy: str, job: TransferJob) -> bool:
        if not job.remote_id:
            return False
        data = {"hashes": job.remote_id}
        order = [modern, legacy] if self._modern_controls is not False else [legacy, modern]
        for index, path in enumerate(order):
            try:
                self._call("POST", path, data=data)
            except RemoteServiceError as exc:
                if index == 0 and "404" in (exc.technical or ""):
                    self._modern_controls = False
                    continue
                return False
            except TrackfetchError:
                return False
            self._modern_controls = path == modern
            return True
        return False

    def pause(self, job: TransferJob) -> bool:
        ok = self._control("/torrents/stop", "/torrents/pause", job)
        if ok:
            job.status = TransferStatus.PAUSED
            job.touch()
        return ok

    def resume(self, job: TransferJob) -> bool:
        ok = self._control("/torrents/start", "/torrents/resume", job)
        if ok:
            job.status = TransferStatus.QUEUED
            job.touch()
        return ok

    def cancel(self, job: TransferJob) -> bool:
        if not job.remote_id:
            job.status = TransferStatus.CANCELLED
            return True
        try:
            self._call(
                "POST",
                "/torrents/delete",
                data={"hashes": job.remote_id, "deleteFiles": "false"},
            )
        except TrackfetchError:
            return False
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True


def _hash_from_magnet(source: str) -> str:
    """Pull the info hash out of a magnet link so the torrent can be found again."""
    text = source.strip()
    if not text.lower().startswith("magnet:"):
        return ""
    marker = "xt=urn:btih:"
    lowered = text.lower()
    index = lowered.find(marker)
    if index < 0:
        return ""
    value = text[index + len(marker) :]
    for separator in ("&", "/"):
        if separator in value:
            value = value.split(separator)[0]
    return value.strip().lower()


def create(ctx: ProviderContext) -> QBittorrentProvider:
    return QBittorrentProvider(ctx)
