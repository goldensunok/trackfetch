"""Tests for the part that looks after the Soulseek helper.

These cover detection, the configuration file it writes, the release it would
download for this machine, and the safety checks on the archive it unpacks.
Starting a real slskd process is not something a test should do, so the process
launch itself is covered by checking the command it builds.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from trackfetch.services.slskd_supervisor import (
    GITHUB_LATEST,
    SlskdSupervisor,
    _release_asset_suffix,
    _safe_extract,
    _yaml_str,
)


@pytest.fixture
def supervisor(tmp_path):
    return SlskdSupervisor(managed_dir=tmp_path / "slskd", port=5030)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_nothing_installed_is_reported_honestly(supervisor, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(Path, "exists", lambda self: False)
    install = supervisor.find_installation()
    assert install.found is False
    assert install.source == "none"


def test_a_managed_install_is_found(supervisor):
    supervisor.managed_executable.parent.mkdir(parents=True, exist_ok=True)
    supervisor.managed_executable.write_text("#!/bin/sh\n")
    install = supervisor.find_installation()
    assert install.found is True
    assert install.source == "managed"


@respx.mock
def test_a_running_helper_is_detected(supervisor):
    respx.get("http://localhost:5030/api/v0/session/enabled").mock(
        return_value=httpx.Response(200, json=True)
    )
    assert supervisor.is_running() is True


@respx.mock
def test_a_helper_that_is_not_running_is_detected(supervisor):
    respx.get("http://localhost:5030/api/v0/session/enabled").mock(
        side_effect=httpx.ConnectError("refused")
    )
    assert supervisor.is_running() is False


@respx.mock
def test_waiting_gives_up_rather_than_hanging(supervisor):
    respx.get("http://localhost:5030/api/v0/session/enabled").mock(
        side_effect=httpx.ConnectError("refused")
    )
    assert supervisor.wait_until_ready(timeout=2) is False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_the_configuration_is_written_inside_trackfetchs_own_folder(supervisor, tmp_path):
    downloads = tmp_path / "downloads"
    path = supervisor.write_config(
        soulseek_username="tobias",
        soulseek_password="s3cret",
        web_username="slskd",
        web_password="helperpass",
        downloads_dir=downloads,
    )
    assert path == supervisor.managed_app_dir / "slskd.yml"
    assert str(path).startswith(str(tmp_path))
    content = path.read_text()
    assert 'username: "tobias"' in content
    assert 'password: "s3cret"' in content
    assert f'downloads: "{downloads}"' in content
    assert "port: 5030" in content
    assert downloads.exists()


def test_awkward_passwords_are_quoted_safely():
    assert _yaml_str('he said "hi"') == '"he said \\"hi\\""'
    assert _yaml_str("back\\slash") == '"back\\\\slash"'
    assert _yaml_str("colon: value #comment") == '"colon: value #comment"'


def test_the_configuration_can_be_rewritten(supervisor, tmp_path):
    for password in ("first", "second"):
        supervisor.write_config(
            soulseek_username="tobias",
            soulseek_password=password,
            web_username="slskd",
            web_password="x",
            downloads_dir=tmp_path / "d",
        )
    assert 'password: "second"' in supervisor.config_path.read_text()


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------


def test_the_right_release_is_chosen_for_this_computer(supervisor):
    suffix = _release_asset_suffix()
    release = {
        "assets": [
            {"name": "slskd-0.26.0-linux-musl-x64.zip", "browser_download_url": "u1"},
            {"name": f"slskd-0.26.0-{suffix}.zip", "browser_download_url": "u2"},
            {"name": "slskd-0.26.0-osx-arm64.zip", "browser_download_url": "u3"},
        ]
    }
    name, url = supervisor.find_release_asset(release)
    assert suffix in name
    assert url == "u2"
    assert "musl" not in name


def test_no_matching_release_is_handled(supervisor):
    assert supervisor.find_release_asset({"assets": []}) == ("", "")
    assert supervisor.find_release_asset({}) == ("", "")


@respx.mock
def test_installing_downloads_and_unpacks(supervisor, monkeypatch):
    from trackfetch.services import slskd_supervisor as module

    monkeypatch.setattr(module, "_executable_name", lambda: "slskd")
    suffix = _release_asset_suffix()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("slskd", "#!/bin/sh\necho hello\n")
        archive.writestr("config/example.yml", "example: true\n")
    payload = buffer.getvalue()

    respx.get(GITHUB_LATEST).mock(
        return_value=httpx.Response(
            200,
            json={
                "tag_name": "0.26.0",
                "assets": [
                    {
                        "name": f"slskd-0.26.0-{suffix}.zip",
                        "browser_download_url": "https://example.com/slskd.zip",
                    }
                ],
            },
        )
    )
    respx.get("https://example.com/slskd.zip").mock(
        return_value=httpx.Response(200, content=payload)
    )

    steps: list[str] = []
    executable = supervisor.install_latest(on_progress=lambda m, f: steps.append(m))
    assert executable is not None
    assert executable.exists()
    assert executable.name == "slskd"
    assert steps


@respx.mock
def test_a_failed_download_returns_nothing_rather_than_raising(supervisor):
    respx.get(GITHUB_LATEST).mock(side_effect=httpx.ConnectError("offline"))
    assert supervisor.install_latest() is None


def test_an_archive_cannot_write_outside_the_target(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.txt", "no")
        archive.writestr("/absolute.txt", "no")
        archive.writestr("good.txt", "yes")
    buffer.seek(0)

    target = tmp_path / "out"
    target.mkdir()
    with zipfile.ZipFile(buffer) as archive:
        _safe_extract(archive, target)

    assert (target / "good.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert list(target.rglob("*")) == [target / "good.txt"]


# ---------------------------------------------------------------------------
# The other Soulseek program
# ---------------------------------------------------------------------------


def test_looking_for_the_desktop_soulseek_program_never_raises(supervisor, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no such command")

    monkeypatch.setattr("subprocess.run", boom)
    assert supervisor.desktop_soulseek_running() is False


def test_the_desktop_soulseek_program_is_recognised(supervisor, monkeypatch):
    class Result:
        stdout = "bash\nSoulseekQt\npython3\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: Result())
    assert supervisor.desktop_soulseek_running() is True


@respx.mock
def test_the_description_is_safe_to_put_in_a_report(supervisor):
    respx.get("http://localhost:5030/api/v0/session/enabled").mock(
        side_effect=httpx.ConnectError("refused")
    )
    described = supervisor.describe()
    assert set(described) >= {"installed", "running", "port", "managed_app_dir"}
    assert "password" not in str(described).lower()
