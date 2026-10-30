"""Tests for ordinary web downloads: redirects, metadata, progress, resume."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import httpx
import pytest
import respx

from trackfetch.models.common import TransferJob, TransferStatus
from trackfetch.providers.base import ProviderContext
from trackfetch.providers.errors import RemoteServiceError
from trackfetch.providers.http import (
    HttpProvider,
    filename_from_headers,
    filename_from_url,
    size_from_headers,
)


@pytest.fixture
def provider(settings, secrets):
    ctx = ProviderContext(
        provider_id="http",
        settings=settings,
        provider_settings=settings.provider("http"),
        secrets=secrets,
    )
    p = HttpProvider(ctx)
    yield p
    p.close()


def job_for(url: str, tmp_path: Path, **payload) -> TransferJob:
    job = TransferJob(provider_id="http", label="file", destination=str(tmp_path))
    job.payload = {"url": url, **payload}
    return job


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def test_filename_from_content_disposition():
    assert filename_from_headers({"Content-Disposition": 'attachment; filename="a b.zip"'}) == "a b.zip"
    assert filename_from_headers({"content-disposition": "attachment; filename=plain.txt"}) == "plain.txt"


def test_filename_from_rfc5987_content_disposition():
    header = {"Content-Disposition": "attachment; filename*=UTF-8''caf%C3%A9%20song.mp3"}
    assert filename_from_headers(header) == "café song.mp3"


def test_filename_from_url_when_there_is_no_header():
    assert filename_from_url("https://example.com/files/my%20album.zip") == "my album.zip"
    assert filename_from_url("https://example.com/") == "download"


def test_filename_from_a_hostile_content_disposition_is_made_safe():
    header = {"Content-Disposition": 'attachment; filename="../../etc/passwd"'}
    assert filename_from_headers(header) == "passwd"


def test_size_from_headers_handles_ranges():
    assert size_from_headers({"Content-Length": "1234"}) == 1234
    assert size_from_headers({"Content-Range": "bytes 0-0/9999"}) == 9999
    assert size_from_headers({}) is None


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


@respx.mock
def test_resolve_reads_the_name_and_size(provider):
    respx.head("https://example.com/a.zip").mock(
        return_value=httpx.Response(
            200,
            headers={
                "Content-Length": "5000",
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="album.zip"',
            },
        )
    )
    candidate = provider.resolve("https://example.com/a.zip")
    assert candidate.filename == "album.zip"
    assert candidate.size == 5000
    assert candidate.extension == "zip"
    assert candidate.payload["url"] == "https://example.com/a.zip"


@respx.mock
def test_resolve_follows_a_redirect(provider):
    respx.head("https://example.com/go").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.example.com/real.bin"})
    )
    respx.head("https://cdn.example.com/real.bin").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "10"})
    )
    candidate = provider.resolve("https://example.com/go")
    assert candidate.filename == "real.bin"
    assert candidate.directory == "cdn.example.com"


@respx.mock
def test_resolve_falls_back_to_a_range_request_when_head_is_refused(provider):
    respx.head("https://example.com/a.bin").mock(return_value=httpx.Response(405))
    respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(206, headers={"Content-Range": "bytes 0-0/4096"})
    )
    candidate = provider.resolve("https://example.com/a.bin")
    assert candidate.size == 4096


@respx.mock
def test_resolve_adds_https_when_the_scheme_is_missing(provider):
    route = respx.head("https://example.com/x").mock(
        return_value=httpx.Response(200, headers={"Content-Length": "1"})
    )
    provider.resolve("example.com/x")
    assert route.called


def test_resolve_refuses_an_empty_link(provider):
    with pytest.raises(RemoteServiceError):
        provider.resolve("   ")


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


@respx.mock
def test_a_file_downloads_and_progress_is_reported(provider, tmp_path):
    payload = b"x" * 4096
    respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(200, content=payload, headers={"Content-Length": "4096"})
    )
    seen: list[tuple[int, int | None]] = []
    job = job_for("https://example.com/a.bin", tmp_path, filename="a.bin")
    provider.download(job, on_progress=lambda done, total, speed: seen.append((done, total)))

    assert job.status is TransferStatus.COMPLETE
    assert (tmp_path / "a.bin").read_bytes() == payload
    assert job.total_bytes == 4096
    assert job.percent == 100.0
    assert seen and seen[-1][0] == 4096


@respx.mock
def test_a_part_finished_file_resumes_from_where_it_stopped(provider, tmp_path):
    part = tmp_path / "a.bin.part"
    part.write_bytes(b"y" * 1000)

    def handler(request):
        assert request.headers["Range"] == "bytes=1000-"
        return httpx.Response(
            206,
            content=b"z" * 3096,
            headers={"Content-Range": "bytes 1000-4095/4096", "Content-Length": "3096"},
        )

    respx.get("https://example.com/a.bin").mock(side_effect=handler)
    job = job_for("https://example.com/a.bin", tmp_path, filename="a.bin")
    provider.download(job)

    assert job.status is TransferStatus.COMPLETE
    data = (tmp_path / "a.bin").read_bytes()
    assert len(data) == 4096
    assert data.startswith(b"y" * 1000)


@respx.mock
def test_a_server_that_ignores_the_range_starts_again_cleanly(provider, tmp_path):
    part = tmp_path / "a.bin.part"
    part.write_bytes(b"old data")
    respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(200, content=b"new content")
    )
    job = job_for("https://example.com/a.bin", tmp_path, filename="a.bin")
    provider.download(job)
    assert (tmp_path / "a.bin").read_bytes() == b"new content"


@respx.mock
def test_a_transient_failure_is_retried(provider, tmp_path):
    respx.get("https://example.com/a.bin").mock(
        side_effect=[
            httpx.ConnectError("dropped"),
            httpx.Response(200, content=b"finally"),
        ]
    )
    job = job_for("https://example.com/a.bin", tmp_path, filename="a.bin")
    provider.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert job.attempts == 2


@respx.mock
def test_a_permanent_failure_is_not_retried_and_reads_plainly(provider, tmp_path):
    route = respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(404, text="nope")
    )
    job = job_for("https://example.com/a.bin", tmp_path, filename="a.bin")
    provider.download(job)
    assert job.status is TransferStatus.FAILED
    assert len(route.calls) == 1
    assert "nope" not in job.error
    assert job.remedy


@respx.mock
def test_a_missing_url_fails_cleanly(provider, tmp_path):
    job = TransferJob(provider_id="http", destination=str(tmp_path), payload={})
    provider.download(job)
    assert job.status is TransferStatus.FAILED
    assert "no web address" in job.error


@respx.mock
def test_a_supplied_checksum_is_verified(provider, tmp_path):
    payload = b"trackfetch"
    digest = hashlib.sha256(payload).hexdigest()
    respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(200, content=payload)
    )
    job = job_for(
        "https://example.com/a.bin", tmp_path, filename="a.bin", checksum=f"sha256:{digest}"
    )
    provider.download(job)
    assert job.status is TransferStatus.COMPLETE


@respx.mock
def test_a_wrong_checksum_fails_and_removes_the_file(provider, tmp_path):
    respx.get("https://example.com/a.bin").mock(
        return_value=httpx.Response(200, content=b"trackfetch")
    )
    job = job_for(
        "https://example.com/a.bin", tmp_path, filename="a.bin", checksum="sha256:" + "0" * 64
    )
    provider.download(job)
    assert job.status is TransferStatus.FAILED
    assert "checksum" in job.error
    assert not (tmp_path / "a.bin").exists()
    assert not (tmp_path / "a.bin.part").exists()


@respx.mock
def test_a_download_can_be_cancelled(provider, tmp_path):
    respx.get("https://example.com/big.bin").mock(
        return_value=httpx.Response(200, content=b"x" * (1024 * 1024))
    )
    cancel = threading.Event()
    cancel.set()
    job = job_for("https://example.com/big.bin", tmp_path, filename="big.bin")
    provider.download(job, cancel=cancel)
    assert job.status is TransferStatus.CANCELLED


@respx.mock
def test_a_hostile_filename_cannot_escape_the_download_folder(provider, tmp_path):
    respx.get("https://example.com/evil").mock(
        return_value=httpx.Response(
            200,
            content=b"data",
            headers={"Content-Disposition": 'attachment; filename="../../../evil.sh"'},
        )
    )
    job = job_for("https://example.com/evil", tmp_path, filename="../../../evil.sh")
    provider.download(job)
    assert job.status is TransferStatus.COMPLETE
    assert Path(job.destination).parent == tmp_path
    assert Path(job.destination).name == "evil.sh"


def test_the_provider_needs_no_setup(provider):
    status = provider.test_connection()
    assert status.ok
    assert "No setup" in status.detail or "ready" in status.detail.lower()
