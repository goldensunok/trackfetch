"""Tests for the JDownloader provider.

The My.JDownloader protocol is signed and encrypted, so the mock server here
performs the same derivation the real one does. If Trackfetch signed or
encrypted anything incorrectly, these tests would fail to decrypt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest
import respx

from trackfetch.models.common import ConnectionState, TransferJob, TransferStatus
from trackfetch.providers.base import ProviderContext
from trackfetch.providers.jdownloader import JDownloaderProvider

EMAIL = "tobias@example.com"
PASSWORD = "hunter2"
API = "https://api.jdownloader.org"
SESSION_TOKEN = "aabbccdd" * 8  # 32 bytes of hex
DEVICE_ID = "device-1"


def secret(domain: str) -> bytes:
    digest = hashlib.sha256()
    digest.update(EMAIL.lower().encode() + PASSWORD.encode() + domain.encode())
    return digest.digest()


def derived(base: bytes) -> bytes:
    digest = hashlib.sha256()
    digest.update(base + bytearray.fromhex(SESSION_TOKEN))
    return digest.digest()


def encrypt(token: bytes, payload: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv, key = token[: len(token) // 2], token[len(token) // 2 :]
    data = payload.encode()
    pad = 16 - (len(data) % 16)
    data += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()


def decrypt(token: bytes, payload: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv, key = token[: len(token) // 2], token[len(token) // 2 :]
    raw = base64.b64decode(payload)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(raw) + decryptor.finalize()
    return plain[: -plain[-1]].decode()


@pytest.fixture
def provider(settings, secrets):
    provider_settings = settings.provider("jdownloader")
    provider_settings.enabled = True
    provider_settings.config.update({"email": EMAIL})
    ctx = ProviderContext(
        provider_id="jdownloader",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    ctx.set_secret("password", PASSWORD)
    p = JDownloaderProvider(ctx)
    yield p
    p.close()


class FakeMyJD:
    """A stand-in for api.jdownloader.org that checks the signatures."""

    def __init__(self, device_response=None):
        self.device_calls: list[dict] = []
        self.device_response = device_response or {}
        self.signature_failures = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path
        params = dict(parse_qsl(url.query))

        if path.startswith("/t_"):
            return self._device(request, path)

        # The signature covers the raw query string exactly as it was sent, so
        # the check has to use the raw text rather than re-encoding it.
        raw_query, _, signature = url.query.partition("&signature=")
        query = f"{path}?{raw_query}"
        token = secret("server") if path == "/my/connect" else derived(secret("server"))
        expected = hmac.new(token, query.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            self.signature_failures += 1
            return httpx.Response(403, text="bad signature")

        if path == "/my/connect":
            body = {"sessiontoken": SESSION_TOKEN, "regaintoken": SESSION_TOKEN}
            return httpx.Response(200, text=encrypt(secret("server"), json.dumps(body)))
        if path == "/my/listdevices":
            body = {"list": [{"id": DEVICE_ID, "name": "Tobias PC", "type": "jd"}]}
            return httpx.Response(
                200, text=encrypt(derived(secret("server")), json.dumps(body))
            )
        if path == "/my/disconnect":
            return httpx.Response(
                200, text=encrypt(derived(secret("server")), json.dumps({}))
            )
        return httpx.Response(404)

    def _device(self, request: httpx.Request, path: str) -> httpx.Response:
        token = derived(secret("device"))
        payload = json.loads(decrypt(token, request.content.decode()))
        self.device_calls.append(payload)
        action = path.split(DEVICE_ID, 1)[-1]
        data = self.device_response.get(action, [])
        return httpx.Response(
            200, text=encrypt(token, json.dumps({"data": data, "rid": payload["rid"]}))
        )


@respx.mock
def test_jdownloader_signs_in_and_finds_the_device(provider):
    server = FakeMyJD()
    respx.route(host="api.jdownloader.org").mock(side_effect=server.handle)
    status = provider.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "Tobias PC" in status.detail
    assert server.signature_failures == 0


@respx.mock
def test_jdownloader_wrong_password_is_reported_plainly(settings, secrets):
    provider_settings = settings.provider("jdownloader")
    provider_settings.enabled = True
    provider_settings.config.update({"email": EMAIL})
    ctx = ProviderContext(
        provider_id="jdownloader",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    ctx.set_secret("password", "the wrong one")
    p = JDownloaderProvider(ctx)

    server = FakeMyJD()
    respx.route(host="api.jdownloader.org").mock(side_effect=server.handle)
    status = p.test_connection()
    assert status.state is ConnectionState.DISCONNECTED
    assert "rejected" in status.detail.lower()
    assert "Settings" in status.remedy
    p.close()


@respx.mock
def test_jdownloader_reports_no_devices_helpfully(provider):
    class Empty(FakeMyJD):
        def _list(self):
            return []

    server = FakeMyJD()

    def handle(request):
        url = urlparse(str(request.url))
        if url.path == "/my/listdevices":
            body = {"list": []}
            return httpx.Response(
                200, text=encrypt(derived(secret("server")), json.dumps(body))
            )
        return server.handle(request)

    respx.route(host="api.jdownloader.org").mock(side_effect=handle)
    status = provider.test_connection()
    assert "does not show any computers" in status.detail
    assert "Start JDownloader" in status.remedy


@respx.mock
def test_jdownloader_adds_a_link(provider, tmp_path):
    server = FakeMyJD(device_response={"/downloadsV2/queryLinks": []})
    respx.route(host="api.jdownloader.org").mock(side_effect=server.handle)

    job = TransferJob(
        provider_id="jdownloader",
        label="thing",
        destination=str(tmp_path),
        payload={"source": "https://filehost.example.com/file/abc"},
    )
    provider.submit(job)
    assert job.status is TransferStatus.QUEUED

    add_calls = [c for c in server.device_calls if c["url"] == "/linkgrabberv2/addLinks"]
    assert len(add_calls) == 1
    params = json.loads(add_calls[0]["params"][0])
    assert params["links"] == "https://filehost.example.com/file/abc"
    assert params["destinationFolder"] == str(tmp_path)
    assert params["autostart"] is True


@respx.mock
def test_jdownloader_reports_progress_and_completion(provider, tmp_path):
    links = [
        {
            "uuid": 1234,
            "name": "file.zip",
            "url": "https://filehost.example.com/file/abc",
            "bytesTotal": 1000,
            "bytesLoaded": 400,
            "speed": 250,
            "finished": False,
            "enabled": True,
        }
    ]
    server = FakeMyJD(device_response={"/downloadsV2/queryLinks": links})
    respx.route(host="api.jdownloader.org").mock(side_effect=server.handle)

    job = TransferJob(
        provider_id="jdownloader",
        destination=str(tmp_path),
        status=TransferStatus.QUEUED,
        payload={"source": "https://filehost.example.com/file/abc"},
    )
    provider.poll([job])
    assert job.status is TransferStatus.DOWNLOADING
    assert job.downloaded_bytes == 400
    assert job.label == "file.zip"
    assert job.remote_id == "1234"

    links[0].update({"finished": True, "bytesLoaded": 1000, "speed": 0})
    provider.poll([job])
    assert job.status is TransferStatus.COMPLETE


@respx.mock
def test_jdownloader_reports_an_offline_link(provider, tmp_path):
    links = [
        {
            "uuid": 9,
            "name": "gone.zip",
            "url": "https://filehost.example.com/gone",
            "status": "File not found (offline)",
            "finished": False,
        }
    ]
    server = FakeMyJD(device_response={"/downloadsV2/queryLinks": links})
    respx.route(host="api.jdownloader.org").mock(side_effect=server.handle)
    job = TransferJob(
        provider_id="jdownloader",
        destination=str(tmp_path),
        status=TransferStatus.QUEUED,
        payload={"source": "https://filehost.example.com/gone"},
    )
    provider.poll([job])
    assert job.status is TransferStatus.FAILED
    assert job.remedy


@respx.mock
def test_jdownloader_cancel_removes_the_link(provider, tmp_path):
    server = FakeMyJD(device_response={"/downloadsV2/removeLinks": True})
    respx.route(host="api.jdownloader.org").mock(side_effect=server.handle)
    job = TransferJob(provider_id="jdownloader", remote_id="1234", destination=str(tmp_path))
    assert provider.cancel(job) is True
    assert job.status is TransferStatus.CANCELLED
    removed = [c for c in server.device_calls if c["url"] == "/downloadsV2/removeLinks"]
    assert removed


def test_jdownloader_without_details_is_not_configured(settings, secrets):
    provider_settings = settings.provider("jdownloader")
    provider_settings.enabled = True
    ctx = ProviderContext(
        provider_id="jdownloader",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    p = JDownloaderProvider(ctx)
    assert p.is_configured() is False
    assert p.test_connection().state is ConnectionState.NOT_CONFIGURED
    p.close()
