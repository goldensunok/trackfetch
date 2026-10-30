"""Tests for the two providers Trackfetch downloads from directly.

SFTP is tested against a stand-in for Paramiko, so connection, authentication
failure, an interrupted transfer and a completed download are all covered
without needing a real server. WebDAV is tested against its real PROPFIND and
GET traffic, mocked at the HTTP boundary.
"""

from __future__ import annotations

import stat
from pathlib import Path

import httpx
import pytest
import respx

from trackfetch.models.common import ConnectionState, TransferJob, TransferStatus
from trackfetch.providers.base import ProviderContext


# ===========================================================================
# SFTP
# ===========================================================================


class FakeAttr:
    def __init__(self, filename, size=0, is_dir=False, mtime=1700000000):
        self.filename = filename
        self.st_size = size
        self.st_mtime = mtime
        self.st_mode = (stat.S_IFDIR | 0o755) if is_dir else (stat.S_IFREG | 0o644)


class FakeFile:
    def __init__(self, data: bytes, fail_after: int | None = None):
        self._data = data
        self._pos = 0
        self._fail_after = fail_after
        self._read_count = 0

    def prefetch(self, size=None):
        return None

    def seek(self, position):
        self._pos = position

    def read(self, size):
        self._read_count += 1
        if self._fail_after is not None and self._read_count > self._fail_after:
            raise OSError("connection lost")
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSftp:
    def __init__(self, files: dict[str, bytes], fail_after: int | None = None):
        self.files = files
        self.fail_after = fail_after
        self.closed = False

    def listdir(self, path="."):
        return [Path(p).name for p in self.files]

    def listdir_attr(self, path="."):
        out = [FakeAttr("subfolder", is_dir=True)]
        for name, data in self.files.items():
            out.append(FakeAttr(Path(name).name, size=len(data)))
        return out

    def stat(self, path):
        return FakeAttr(Path(path).name, size=len(self.files[path]))

    def open(self, path, mode="rb"):
        return FakeFile(self.files[path], self.fail_after)

    def close(self):
        self.closed = True


class FakeSSHClient:
    """Stands in for paramiko.SSHClient."""

    behaviour = "ok"
    files: dict[str, bytes] = {}
    fail_after: int | None = None

    def __init__(self):
        self._sftp = None

    def load_system_host_keys(self):
        return None

    def set_missing_host_key_policy(self, policy):
        return None

    def connect(self, **kwargs):
        if FakeSSHClient.behaviour == "auth":
            raise _FakeAuthError("Authentication failed.")
        if FakeSSHClient.behaviour == "refused":
            raise OSError("Connection refused")

    def open_sftp(self):
        self._sftp = FakeSftp(FakeSSHClient.files, FakeSSHClient.fail_after)
        return self._sftp

    def close(self):
        return None


class _FakeAuthError(Exception):
    pass


@pytest.fixture
def sftp_provider(settings, secrets, monkeypatch, tmp_path):
    import paramiko

    from trackfetch.providers.sftp import SftpProvider

    monkeypatch.setattr(paramiko, "SSHClient", FakeSSHClient)
    FakeSSHClient.behaviour = "ok"
    FakeSSHClient.files = {"/remote/track.flac": b"F" * 5000}
    FakeSSHClient.fail_after = None

    provider_settings = settings.provider("sftp")
    provider_settings.enabled = True
    provider_settings.config["servers"] = [
        {
            "name": "My server",
            "host": "files.example.com",
            "port": 22,
            "username": "tobias",
            "start_path": "/remote",
        }
    ]
    ctx = ProviderContext(
        provider_id="sftp",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    ctx.set_secret("My server.password", "hunter2")
    p = SftpProvider(ctx)
    yield p
    p.close()


def test_sftp_with_no_servers_is_not_configured(settings, secrets):
    from trackfetch.providers.sftp import SftpProvider

    provider_settings = settings.provider("sftp")
    provider_settings.enabled = True
    ctx = ProviderContext(
        provider_id="sftp",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    p = SftpProvider(ctx)
    assert p.is_configured() is False
    assert p.test_connection().state is ConnectionState.NOT_CONFIGURED
    p.close()


def test_sftp_connects(sftp_provider):
    status = sftp_provider.test_connection()
    assert status.state is ConnectionState.CONNECTED
    assert "1 server" in status.detail


def test_sftp_authentication_failure_reads_plainly(sftp_provider):
    FakeSSHClient.behaviour = "auth"
    status = sftp_provider.test_connection()
    assert status.state is ConnectionState.DISCONNECTED
    assert "rejected your SFTP login" in status.detail
    assert "username and password" in status.detail


def test_sftp_unreachable_server_reads_plainly(sftp_provider):
    FakeSSHClient.behaviour = "refused"
    status = sftp_provider.test_connection()
    assert "could not reach" in status.detail.lower()
    assert "Connection refused" not in status.detail


def test_sftp_browsing_lists_servers_then_files(sftp_provider):
    servers = sftp_provider.browse("")
    assert servers[0].name == "My server"
    assert servers[0].is_dir is True

    entries = sftp_provider.browse("My server:/remote")
    names = [e.name for e in entries]
    assert "subfolder" in names
    assert "track.flac" in names
    assert entries[0].is_dir is True  # folders sort first


def test_sftp_downloads_a_file(sftp_provider, tmp_path):
    entry = next(e for e in sftp_provider.browse("My server:/remote") if not e.is_dir)
    entry.path = "My server:/remote/track.flac"
    candidate = sftp_provider.candidate_from_entry(entry)
    job = sftp_provider.build_job(candidate, tmp_path)
    job.payload["target_path"] = str(tmp_path / "track.flac")

    seen = []
    sftp_provider.download(job, on_progress=lambda d, t, s: seen.append(d))
    assert job.status is TransferStatus.COMPLETE
    assert (tmp_path / "track.flac").read_bytes() == b"F" * 5000
    assert seen


def test_sftp_interrupted_transfer_can_be_retried(sftp_provider, tmp_path):
    FakeSSHClient.files = {"/remote/big.bin": b"B" * (300 * 1024)}
    FakeSSHClient.fail_after = 1

    job = TransferJob(
        provider_id="sftp",
        destination=str(tmp_path),
        payload={
            "server": "My server",
            "remote_path": "/remote/big.bin",
            "target_path": str(tmp_path / "big.bin"),
        },
    )
    sftp_provider.download(job)
    assert job.status is TransferStatus.FAILED
    assert "interrupted" in job.error
    assert "carry on" in job.remedy
    assert (tmp_path / "big.bin.part").exists()

    # The second attempt succeeds and resumes from the part file.
    FakeSSHClient.fail_after = None
    sftp_provider.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert (tmp_path / "big.bin").stat().st_size == 300 * 1024


def test_sftp_without_a_chosen_file_fails_cleanly(sftp_provider, tmp_path):
    job = TransferJob(provider_id="sftp", destination=str(tmp_path), payload={})
    sftp_provider.download(job)
    assert job.status is TransferStatus.FAILED
    assert "which file" in job.error


# ===========================================================================
# WebDAV
# ===========================================================================

DAV = "https://cloud.example.com/remote.php/dav/files/tobias"

PROPFIND_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/tobias/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/tobias/Music/</d:href>
    <d:propstat><d:prop>
      <d:displayname>Music</d:displayname>
      <d:resourcetype><d:collection/></d:resourcetype>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/tobias/notes.txt</d:href>
    <d:propstat><d:prop>
      <d:displayname>notes.txt</d:displayname>
      <d:getcontentlength>2048</d:getcontentlength>
      <d:getlastmodified>Mon, 18 Aug 2026 10:00:00 GMT</d:getlastmodified>
      <d:resourcetype/>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>
"""


@pytest.fixture
def webdav(settings, secrets):
    from trackfetch.providers.webdav import WebdavProvider

    provider_settings = settings.provider("webdav")
    provider_settings.enabled = True
    provider_settings.config.update({"base_url": DAV, "username": "tobias"})
    ctx = ProviderContext(
        provider_id="webdav",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    ctx.set_secret("password", "hunter2")
    p = WebdavProvider(ctx)
    yield p
    p.close()


@respx.mock
def test_webdav_connects_and_lists(webdav):
    respx.request("PROPFIND", DAV).mock(
        return_value=httpx.Response(207, content=PROPFIND_XML.encode())
    )
    status = webdav.test_connection()
    assert status.state is ConnectionState.CONNECTED

    entries = webdav.browse("")
    names = {e.name: e for e in entries}
    assert names["Music"].is_dir is True
    assert names["notes.txt"].size == 2048
    assert names["notes.txt"].path == "notes.txt"


@respx.mock
def test_webdav_authentication_failure_reads_plainly(webdav):
    respx.request("PROPFIND", DAV).mock(return_value=httpx.Response(401, text="denied"))
    status = webdav.test_connection()
    assert status.state is ConnectionState.DISCONNECTED
    assert "rejected the login" in status.detail
    assert "denied" not in status.detail


@respx.mock
def test_webdav_unreadable_reply_is_reported(webdav):
    respx.request("PROPFIND", DAV).mock(
        return_value=httpx.Response(207, content=b"not xml at all")
    )
    status = webdav.test_connection()
    assert "could not read" in status.detail.lower()


@respx.mock
def test_webdav_downloads_a_file(webdav, tmp_path):
    respx.get(f"{DAV}/notes.txt").mock(
        return_value=httpx.Response(200, content=b"n" * 2048, headers={"Content-Length": "2048"})
    )
    job = TransferJob(
        provider_id="webdav",
        destination=str(tmp_path),
        payload={"remote_path": "notes.txt", "target_path": str(tmp_path / "notes.txt")},
    )
    webdav.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert (tmp_path / "notes.txt").read_bytes() == b"n" * 2048


@respx.mock
def test_webdav_resumes_a_part_finished_file(webdav, tmp_path):
    part = tmp_path / "notes.txt.part"
    part.write_bytes(b"a" * 1000)

    def handler(request):
        assert request.headers["Range"] == "bytes=1000-"
        return httpx.Response(
            206,
            content=b"b" * 1048,
            headers={"Content-Range": "bytes 1000-2047/2048", "Content-Length": "1048"},
        )

    respx.get(f"{DAV}/notes.txt").mock(side_effect=handler)
    job = TransferJob(
        provider_id="webdav",
        destination=str(tmp_path),
        payload={"remote_path": "notes.txt", "target_path": str(tmp_path / "notes.txt")},
    )
    webdav.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert (tmp_path / "notes.txt").stat().st_size == 2048


@respx.mock
def test_webdav_interrupted_transfer_reads_plainly(webdav, tmp_path):
    respx.get(f"{DAV}/notes.txt").mock(side_effect=httpx.ReadError("dropped"))
    job = TransferJob(
        provider_id="webdav",
        destination=str(tmp_path),
        payload={"remote_path": "notes.txt", "target_path": str(tmp_path / "notes.txt")},
    )
    webdav.download(job)
    assert job.status is TransferStatus.FAILED
    assert "carry on" in job.remedy


def test_webdav_without_an_address_is_not_configured(settings, secrets):
    from trackfetch.providers.webdav import WebdavProvider

    provider_settings = settings.provider("webdav")
    provider_settings.enabled = True
    ctx = ProviderContext(
        provider_id="webdav",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    p = WebdavProvider(ctx)
    assert p.test_connection().state is ConnectionState.NOT_CONFIGURED
    p.close()
