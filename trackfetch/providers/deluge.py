"""Deluge, through the documented JSON-RPC interface of its web interface.

Every call is a ``POST`` to ``/json`` with ``{"method": ..., "params": [...],
"id": n}``. Deluge keeps a session cookie after ``auth.login``.

Methods used: ``auth.login``, ``web.connected``, ``web.get_hosts``,
``web.connect``, ``daemon.get_version``, ``core.add_torrent_magnet``,
``core.add_torrent_file``, ``core.get_torrents_status``, ``core.pause_torrent``
(or ``core.pause_torrents`` on Deluge 2), ``core.resume_torrent``,
``core.remove_torrent``.
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

PROVIDER_ID = "deluge"
SERVICE = "Deluge"

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
    workflow="Send a torrent or magnet link to Deluge and watch it here.",
    requires_app="Deluge (deluge-web)",
)

STATUS_KEYS = [
    "name",
    "state",
    "progress",
    "total_wanted",
    "total_done",
    "download_payload_rate",
    "eta",
    "save_path",
    "message",
    "is_finished",
]

STATE_MAP = {
    "Queued": TransferStatus.QUEUED,
    "Checking": TransferStatus.DOWNLOADING,
    "Downloading": TransferStatus.DOWNLOADING,
    "Allocating": TransferStatus.DOWNLOADING,
    "Moving": TransferStatus.DOWNLOADING,
    "Seeding": TransferStatus.COMPLETE,
    "Paused": TransferStatus.PAUSED,
    "Error": TransferStatus.FAILED,
}


class DelugeProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._api: HttpApi | None = None
        self._ids = itertools.count(1)
        self._authenticated = False
        self._plural_controls: bool | None = None

    # -- plumbing ----------------------------------------------------------

    @property
    def api(self) -> HttpApi:
        if self._api is None:
            base = normalise_base_url(self.ctx.config("base_url", "http://localhost:8112"))
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
        self._authenticated = False

    def is_configured(self) -> bool:
        return bool(self.ctx.config("base_url")) and bool(self.ctx.secret("password"))

    def call(self, method: str, params: list[Any] | None = None, authenticate: bool = True) -> Any:
        if authenticate:
            self.login()
        body = {"method": method, "params": params or [], "id": next(self._ids)}
        payload = as_mapping(self.api.json("POST", "/json", json=body))
        error = payload.get("error")
        if error:
            detail = as_mapping(error)
            message = coerce_str(detail.get("message")) or coerce_str(error)
            if "not authenticated" in message.lower():
                self._authenticated = False
                raise AuthenticationFailed(
                    "Deluge rejected the password.",
                    "Open Settings, choose Deluge and enter the web interface "
                    "password again.",
                    technical=message,
                    provider_id=PROVIDER_ID,
                )
            raise RemoteServiceError(
                "Deluge refused that request.",
                "Open Deluge to see what it says, then try again.",
                technical=message,
                provider_id=PROVIDER_ID,
            )
        return payload.get("result")

    def login(self, force: bool = False) -> None:
        if self._authenticated and not force:
            return
        password = self.ctx.secret("password")
        result = self.call("auth.login", [password], authenticate=False)
        if result is not True:
            raise AuthenticationFailed(
                "Deluge rejected the password.",
                "Open Settings, choose Deluge and enter the web interface password "
                "again.",
                provider_id=PROVIDER_ID,
            )
        self._authenticated = True
        self._ensure_daemon()

    def _ensure_daemon(self) -> None:
        """The web interface can be running while the Deluge daemon is not."""
        try:
            connected = self.call("web.connected", [], authenticate=False)
        except TrackfetchError:
            return
        if connected:
            return
        hosts = as_list(self.call("web.get_hosts", [], authenticate=False))
        if not hosts:
            raise RemoteServiceError(
                "Deluge's web interface is running but it is not connected to "
                "Deluge itself.",
                "Open the Deluge web page once and connect it to the daemon.",
                provider_id=PROVIDER_ID,
            )
        host_id = coerce_str(as_list(hosts[0])[0] if isinstance(hosts[0], list) else hosts[0])
        self.call("web.connect", [host_id], authenticate=False)

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
            self.login(force=True)
            version = coerce_str(self.call("daemon.get_version", []))
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
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
            job.error = "There is nothing for Deluge to download."
            job.remedy = "Add the torrent or magnet link again."
            return job

        options = {"download_location": job.destination or str(self.ctx.download_dir())}
        if source.lower().startswith("magnet:"):
            torrent_id = self.call("core.add_torrent_magnet", [source, options])
        else:
            path = Path(source)
            if not path.exists():
                job.status = TransferStatus.FAILED
                job.error = "Trackfetch could not find that torrent file."
                job.remedy = "Check it has not been moved, then add it again."
                return job
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            torrent_id = self.call("core.add_torrent_file", [path.name, encoded, options])

        job.remote_id = coerce_str(torrent_id)
        job.status = TransferStatus.QUEUED
        job.error = ""
        job.remedy = ""
        job.attempts += 1
        job.touch()
        return job

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        relevant = [j for j in jobs if j.provider_id == PROVIDER_ID and not j.status.is_terminal]
        ids = [j.remote_id for j in relevant if j.remote_id]
        if not ids:
            return []
        try:
            result = as_mapping(
                self.call("core.get_torrents_status", [{"id": ids}, STATUS_KEYS])
            )
        except Exception as exc:
            log.debug("could not read Deluge torrents: %r", exc)
            return []
        changed: list[TransferJob] = []
        for job in relevant:
            record = as_mapping(result.get(job.remote_id))
            if not record:
                continue
            if self._apply(job, dict(record)):
                changed.append(job)
        return changed

    @staticmethod
    def _apply(job: TransferJob, record: dict[str, Any]) -> bool:
        before = (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)
        state = coerce_str(record.get("state"))
        job.status = STATE_MAP.get(state, TransferStatus.QUEUED)
        if record.get("is_finished"):
            job.status = TransferStatus.COMPLETE

        job.total_bytes = coerce_int(record.get("total_wanted")) or job.total_bytes
        job.downloaded_bytes = coerce_int(record.get("total_done")) or job.downloaded_bytes
        rate = record.get("download_payload_rate")
        if isinstance(rate, (int, float)):
            job.speed_bps = float(rate)
        eta = coerce_int(record.get("eta"))
        job.eta_seconds = eta if eta and eta > 0 else None
        name = coerce_str(record.get("name"))
        if name:
            job.label = name
        folder = coerce_str(record.get("save_path"))
        if folder:
            job.destination = folder
        if job.status is TransferStatus.FAILED:
            job.error = "Deluge reported a problem with this torrent."
            job.remedy = "Open Deluge to see the detail, then click Retry here."
        job.touch()
        return before != (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)

    # -- controls ----------------------------------------------------------

    def _control(self, singular: str, plural: str, job: TransferJob) -> bool:
        if not job.remote_id:
            return False
        attempts = (
            [(plural, [[job.remote_id]]), (singular, [job.remote_id])]
            if self._plural_controls is not False
            else [(singular, [job.remote_id]), (plural, [[job.remote_id]])]
        )
        for index, (method, params) in enumerate(attempts):
            try:
                self.call(method, params)
            except TrackfetchError:
                if index == 0:
                    continue
                return False
            self._plural_controls = method == plural
            return True
        return False

    def pause(self, job: TransferJob) -> bool:
        ok = self._control("core.pause_torrent", "core.pause_torrents", job)
        if ok:
            job.status = TransferStatus.PAUSED
            job.touch()
        return ok

    def resume(self, job: TransferJob) -> bool:
        ok = self._control("core.resume_torrent", "core.resume_torrents", job)
        if ok:
            job.status = TransferStatus.QUEUED
            job.touch()
        return ok

    def cancel(self, job: TransferJob) -> bool:
        if job.remote_id:
            try:
                self.call("core.remove_torrent", [job.remote_id, False])
            except TrackfetchError:
                return False
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True


def create(ctx: ProviderContext) -> DelugeProvider:
    return DelugeProvider(ctx)
