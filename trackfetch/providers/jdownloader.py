"""JDownloader, through the documented My.JDownloader control API.

JDownloader already knows how to deal with a very large number of file hosting
sites. Trackfetch does not reimplement any of that: it hands the address over
and reports back what JDownloader does with it.

The My.JDownloader protocol is used exactly as documented:

* ``loginSecret  = SHA256(email + password + "server")``
* ``deviceSecret = SHA256(email + password + "device")``
* every request carries ``rid`` and a hex ``HMAC-SHA256`` signature of the
  query string, keyed with the current token;
* payloads are AES-CBC encrypted, with the first half of the token as the
  initialisation vector and the second half as the key, then base64 encoded.

Calls used on the device: ``/linkgrabberv2/addLinks``,
``/downloadsV2/queryLinks``, ``/downloadsV2/removeLinks``,
``/downloadcontroller/start``, ``/downloadcontroller/pause``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
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
    coerce_bool,
    coerce_int,
    coerce_str,
)
from trackfetch.providers._http import HttpApi
from trackfetch.providers.base import Provider, ProviderContext
from trackfetch.providers.errors import (
    AuthenticationFailed,
    InvalidResponse,
    RemoteServiceError,
    TrackfetchError,
)

log = logging.getLogger(__name__)

PROVIDER_ID = "jdownloader"
SERVICE = "JDownloader"
API_URL = "https://api.jdownloader.org"
APP_KEY = "trackfetch"
API_VERSION = 1

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
    workflow="Hand a web address to JDownloader and watch it here.",
    requires_app="JDownloader",
)

QUERY_FIELDS = {
    "bytesLoaded": True,
    "bytesTotal": True,
    "speed": True,
    "status": True,
    "finished": True,
    "enabled": True,
    "running": True,
    "url": True,
    "host": True,
    "packageUUIDs": True,
}


def _aes_available() -> bool:
    try:
        import cryptography.hazmat.primitives.ciphers  # noqa: F401

        return True
    except ImportError:  # pragma: no cover - depends on the build
        return False


class MyJDownloaderClient:
    """A small, complete client for the My.JDownloader API."""

    def __init__(self, email: str, password: str, api_url: str = API_URL, verify_tls: bool = True):
        self.email = email.strip().lower()
        self.password = password
        self._login_secret = self._secret("server")
        self._device_secret = self._secret("device")
        self._session_token = ""
        self._regain_token = ""
        self._server_token = b""
        self._device_token = b""
        self._rid = int(time.time() * 1000)
        self._http = HttpApi(
            api_url,
            service_name=SERVICE,
            provider_id=PROVIDER_ID,
            verify_tls=verify_tls,
            timeout=45,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )

    # -- primitives --------------------------------------------------------

    def _secret(self, domain: str) -> bytes:
        digest = hashlib.sha256()
        digest.update(
            self.email.encode("utf-8") + self.password.encode("utf-8") + domain.encode("utf-8")
        )
        return digest.digest()

    def _next_rid(self) -> int:
        self._rid += 1
        return self._rid

    @staticmethod
    def _sign(key: bytes, data: str) -> str:
        return hmac.new(key, data.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def _encrypt(token: bytes, payload: str) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        iv, key = token[: len(token) // 2], token[len(token) // 2 :]
        data = payload.encode("utf-8")
        pad = 16 - (len(data) % 16)
        data += bytes([pad]) * pad
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode("ascii")

    @staticmethod
    def _decrypt(token: bytes, payload: str) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        iv, key = token[: len(token) // 2], token[len(token) // 2 :]
        raw = base64.b64decode(payload)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(raw) + decryptor.finalize()
        if plain:
            pad = plain[-1]
            if 1 <= pad <= 16:
                plain = plain[:-pad]
        return plain.decode("utf-8", errors="replace")

    def _update_tokens(self) -> None:
        session_bytes = bytearray.fromhex(self._session_token)
        server = hashlib.sha256()
        server.update(self._login_secret + session_bytes)
        self._server_token = server.digest()
        device = hashlib.sha256()
        device.update(self._device_secret + session_bytes)
        self._device_token = device.digest()

    # -- server calls ------------------------------------------------------

    def _server_call(self, path: str, params: list[str], token: bytes) -> dict[str, Any]:
        rid = self._next_rid()
        query = f"{path}?{'&'.join(params)}&rid={rid}" if params else f"{path}?rid={rid}"
        signature = self._sign(token, query)
        response = self._http.request(
            "GET", f"{query}&signature={signature}", raise_for_status=False
        )
        if response.status_code == 403:
            raise AuthenticationFailed(
                "My.JDownloader rejected the email address or password.",
                "Open Settings, choose JDownloader and check the details you use "
                "to sign in to My.JDownloader.",
                technical=f"HTTP {response.status_code}",
                provider_id=PROVIDER_ID,
            )
        if response.status_code >= 400:
            from trackfetch.providers._http import translate_status

            raise translate_status(response, SERVICE, PROVIDER_ID)
        try:
            plain = self._decrypt(token, response.text)
            payload = json.loads(plain)
        except Exception as exc:
            raise InvalidResponse(
                "Trackfetch could not read My.JDownloader's reply.",
                "Check the email address and password, then try again.",
                technical=repr(exc),
                provider_id=PROVIDER_ID,
            ) from exc
        return dict(as_mapping(payload))

    def connect(self) -> None:
        from urllib.parse import quote

        payload = self._server_call(
            "/my/connect",
            [f"email={quote(self.email)}", f"appkey={APP_KEY}"],
            self._login_secret,
        )
        self._session_token = coerce_str(payload.get("sessiontoken"))
        self._regain_token = coerce_str(payload.get("regaintoken"))
        if not self._session_token:
            raise AuthenticationFailed(
                "My.JDownloader did not accept the sign in.",
                "Check the email address and password you use for My.JDownloader.",
                provider_id=PROVIDER_ID,
            )
        self._update_tokens()

    def disconnect(self) -> None:
        if not self._session_token:
            return
        try:
            self._server_call(
                "/my/disconnect",
                [f"sessiontoken={self._session_token}"],
                self._server_token,
            )
        except Exception:  # pragma: no cover - best effort
            pass
        self._session_token = ""

    def devices(self) -> list[dict[str, Any]]:
        payload = self._server_call(
            "/my/listdevices",
            [f"sessiontoken={self._session_token}"],
            self._server_token,
        )
        return [dict(as_mapping(d)) for d in as_list(payload.get("list"))]

    # -- device calls ------------------------------------------------------

    def device_action(
        self, device_id: str, path: str, params: list[Any] | None = None
    ) -> Any:
        rid = self._next_rid()
        body = {
            "apiVer": API_VERSION,
            "url": path,
            "params": [json.dumps(p) if not isinstance(p, str) else p for p in (params or [])],
            "rid": rid,
        }
        encrypted = self._encrypt(self._device_token, json.dumps(body))
        url = f"/t_{self._session_token}_{device_id}{path}"
        response = self._http.request(
            "POST",
            url,
            content=encrypted.encode("ascii"),
            headers={"Content-Type": "application/aesjson-jd; charset=utf-8"},
            raise_for_status=False,
        )
        if response.status_code == 403:
            raise AuthenticationFailed(
                "My.JDownloader would not let Trackfetch talk to that computer.",
                "Check JDownloader is running and signed in to My.JDownloader.",
                provider_id=PROVIDER_ID,
            )
        if response.status_code >= 400:
            from trackfetch.providers._http import translate_status

            raise translate_status(response, SERVICE, PROVIDER_ID)
        try:
            plain = self._decrypt(self._device_token, response.text)
            payload = json.loads(plain)
        except Exception as exc:
            raise InvalidResponse(
                "Trackfetch could not read JDownloader's reply.",
                "Check JDownloader is running and up to date.",
                technical=repr(exc),
                provider_id=PROVIDER_ID,
            ) from exc
        return as_mapping(payload).get("data")

    def close(self) -> None:
        self._http.close()


class JDownloaderProvider(Provider):
    info = INFO

    def __init__(self, ctx: ProviderContext):
        super().__init__(ctx)
        self._client: MyJDownloaderClient | None = None
        self._device_id = ""

    # -- plumbing ----------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.ctx.config("email")) and bool(self.ctx.secret("password"))

    def close(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.close()
            self._client = None
        self._device_id = ""

    def client(self) -> MyJDownloaderClient:
        if not _aes_available():
            raise RemoteServiceError(
                "JDownloader support is missing part of itself in this build.",
                "Use Help then Copy Diagnostic Information and send the result on.",
                technical="the cryptography package is not available",
                provider_id=PROVIDER_ID,
            )
        if self._client is None:
            self.require_configured()
            self._client = MyJDownloaderClient(
                coerce_str(self.ctx.config("email")),
                self.ctx.secret("password"),
                verify_tls=bool(self.ctx.config("verify_tls", True)),
            )
            self._client.connect()
            self._device_id = self._choose_device(self._client)
        return self._client

    def _choose_device(self, client: MyJDownloaderClient) -> str:
        devices = client.devices()
        if not devices:
            raise RemoteServiceError(
                "My.JDownloader does not show any computers running JDownloader.",
                "Start JDownloader and make sure it is signed in to My.JDownloader, "
                "then click Test Connection.",
                provider_id=PROVIDER_ID,
            )
        wanted = coerce_str(self.ctx.config("device_name"))
        if wanted:
            for device in devices:
                if coerce_str(device.get("name")) == wanted:
                    return coerce_str(device.get("id"))
        return coerce_str(devices[0].get("id"))

    def _action(self, path: str, params: list[Any] | None = None) -> Any:
        client = self.client()
        return client.device_action(self._device_id, path, params)

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
                "JDownloader has not been set up yet.",
                "Open Settings, choose JDownloader and enter your My.JDownloader "
                "email address and password.",
            )
        try:
            self.close()
            client = self.client()
            devices = client.devices()
        except TrackfetchError as exc:
            return self._status_from_error(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return self._status_from_error(exc)
        names = ", ".join(coerce_str(d.get("name")) for d in devices)
        return self._status_ok(f"Connected to JDownloader on: {names}.")

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
            job.error = "There is nothing for JDownloader to fetch."
            job.remedy = "Add the link again."
            return job

        before = {coerce_str(l.get("uuid")) for l in self._links()}
        self._action(
            "/linkgrabberv2/addLinks",
            [
                {
                    "links": source,
                    "autostart": True,
                    "destinationFolder": job.destination or str(self.ctx.download_dir()),
                    "packageName": job.label or None,
                    "overwritePackagizerRules": False,
                }
            ],
        )
        job.status = TransferStatus.QUEUED
        job.error = ""
        job.remedy = ""
        job.attempts += 1
        job.payload["submitted_at"] = time.time()
        job.payload["known_links"] = sorted(before)
        job.touch()
        return job

    def _links(self) -> list[dict[str, Any]]:
        try:
            data = self._action("/downloadsV2/queryLinks", [QUERY_FIELDS])
        except TrackfetchError:
            raise
        return [dict(as_mapping(link)) for link in as_list(data)]

    def poll(self, jobs: Sequence[TransferJob]) -> list[TransferJob]:
        relevant = [j for j in jobs if j.provider_id == PROVIDER_ID and not j.status.is_terminal]
        if not relevant:
            return []
        try:
            links = self._links()
        except Exception as exc:
            log.debug("could not read the JDownloader list: %r", exc)
            return []

        by_uuid = {coerce_str(link.get("uuid")): link for link in links}
        by_url = {coerce_str(link.get("url")): link for link in links}

        changed: list[TransferJob] = []
        for job in relevant:
            record = by_uuid.get(job.remote_id) if job.remote_id else None
            if record is None:
                source = coerce_str(job.payload.get("source"))
                record = by_url.get(source)
                if record is None:
                    # Match anything new that appeared after this job was added.
                    known = set(job.payload.get("known_links") or [])
                    fresh = [
                        link
                        for uuid, link in by_uuid.items()
                        if uuid and uuid not in known
                    ]
                    record = fresh[0] if len(fresh) == 1 else None
                if record is not None:
                    job.remote_id = coerce_str(record.get("uuid"))
            if record is None:
                continue
            if self._apply(job, record):
                changed.append(job)
        return changed

    @staticmethod
    def _apply(job: TransferJob, record: dict[str, Any]) -> bool:
        before = (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)
        total = coerce_int(record.get("bytesTotal"))
        loaded = coerce_int(record.get("bytesLoaded"))
        speed = coerce_int(record.get("speed"))
        status_text = coerce_str(record.get("status"))
        finished = coerce_bool(record.get("finished"))

        if total:
            job.total_bytes = total
        if loaded is not None:
            job.downloaded_bytes = loaded
        if speed is not None:
            job.speed_bps = float(speed)
            if speed > 0 and job.total_bytes:
                job.eta_seconds = int(
                    max(0, (job.total_bytes - job.downloaded_bytes) / speed)
                )

        lowered = status_text.lower()
        if finished:
            job.status = TransferStatus.COMPLETE
        elif "error" in lowered or "offline" in lowered or "failed" in lowered:
            job.status = TransferStatus.FAILED
            job.error = "JDownloader could not fetch that link."
            job.remedy = status_text or "Open JDownloader to see what it says."
        elif speed:
            job.status = TransferStatus.DOWNLOADING
        elif record.get("enabled") is False:
            job.status = TransferStatus.PAUSED
        else:
            job.status = TransferStatus.QUEUED

        name = coerce_str(record.get("name"))
        if name:
            job.label = name
        job.touch()
        return before != (job.status, job.downloaded_bytes, job.total_bytes, job.speed_bps)

    # -- controls ----------------------------------------------------------

    def pause(self, job: TransferJob) -> bool:
        try:
            self._action("/downloadcontroller/pause", [True])
        except TrackfetchError:
            return False
        job.status = TransferStatus.PAUSED
        job.touch()
        return True

    def resume(self, job: TransferJob) -> bool:
        try:
            self._action("/downloadcontroller/pause", [False])
            self._action("/downloadcontroller/start", [])
        except TrackfetchError:
            return False
        job.status = TransferStatus.QUEUED
        job.touch()
        return True

    def cancel(self, job: TransferJob) -> bool:
        if job.remote_id:
            try:
                self._action("/downloadsV2/removeLinks", [[job.remote_id], []])
            except TrackfetchError:
                pass
        job.status = TransferStatus.CANCELLED
        job.touch()
        return True

    def diagnostics(self) -> dict[str, Any]:
        data = super().diagnostics()
        data["client"] = {
            "device_id_known": bool(self._device_id),
            "aes_available": _aes_available(),
        }
        return data


def create(ctx: ProviderContext) -> JDownloaderProvider:
    return JDownloaderProvider(ctx)
