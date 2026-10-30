"""Tests for the promises Trackfetch makes about secrets and diagnostics.

The rule being tested: a password, an API key or a token must never reach the
log file, the settings file, the database or the diagnostic report.
"""

from __future__ import annotations

import json
import logging

import pytest

from trackfetch.config.secrets import InMemorySecretStore, SecretStore
from trackfetch.config.settings import Settings, SettingsStore
from trackfetch.diagnostics.collect import build_report
from trackfetch.diagnostics.logging_setup import RecentLogBuffer, redact, setup_logging

SECRET = "SuperSecretPassword123"
API_KEY = "abcdef0123456789abcdef0123456789"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f'{{"password": "{SECRET}"}}',
        f"password={SECRET}",
        f"api_key: {SECRET}",
        f"apikey={SECRET}",
        f"Authorization: Bearer {SECRET}",
        f"X-API-Key: {SECRET}",
        f'{{"token":"{SECRET}"}}',
        f"secret = {SECRET}",
        f"https://user:{SECRET}@example.com/path",
    ],
)
def test_credentials_are_stripped_from_log_lines(line):
    cleaned = redact(line)
    assert SECRET not in cleaned
    assert "***" in cleaned


def test_ordinary_text_is_left_alone():
    line = "Finished downloading 01 Xtal.flac from peer_one"
    assert redact(line) == line


def test_the_log_file_never_contains_a_password(scratch_home):
    buffer = setup_logging(level=logging.DEBUG)
    log = logging.getLogger("trackfetch.test")
    log.info("connecting with password=%s", SECRET)
    log.warning('{"api_key": "%s"}', API_KEY)
    for handler in logging.getLogger().handlers:
        handler.flush()

    from trackfetch.config.paths import app_paths

    contents = app_paths().log_file.read_text(encoding="utf-8")
    assert SECRET not in contents
    assert API_KEY not in contents
    assert "***" in contents

    assert all(SECRET not in line for line in buffer.recent())
    logging.getLogger().handlers.clear()


def test_the_recent_buffer_keeps_warnings_separately():
    buffer = RecentLogBuffer()
    buffer.setFormatter(logging.Formatter("%(message)s"))
    for level, message in [
        (logging.INFO, "ordinary thing"),
        (logging.WARNING, "something odd"),
        (logging.ERROR, "something broke"),
    ]:
        buffer.emit(
            logging.LogRecord("t", level, "f", 1, message, None, None)
        )
    assert len(buffer.recent()) == 3
    assert len(buffer.recent_errors()) == 2


# ---------------------------------------------------------------------------
# Storage of secrets
# ---------------------------------------------------------------------------


def test_secrets_never_go_into_the_settings_file(scratch_home):
    from trackfetch.config.paths import app_paths

    settings = Settings()
    settings.provider("qbittorrent").config.update(
        {"base_url": "http://localhost:8080", "username": "admin"}
    )
    store = SettingsStore(app_paths().settings_file)
    store.save(settings)

    secrets = InMemorySecretStore()
    secrets.set(SecretStore.key_for("qbittorrent", "password"), SECRET)

    written = app_paths().settings_file.read_text(encoding="utf-8")
    assert SECRET not in written
    assert "password" not in json.loads(written)["providers"]["qbittorrent"]["config"]


def test_the_secret_store_round_trips_and_forgets():
    store = InMemorySecretStore()
    key = SecretStore.key_for("slskd", "password")
    assert store.get(key) == ""
    store.set(key, SECRET)
    assert store.get(key) == SECRET
    assert store.has(key) is True
    store.set(key, "")
    assert store.get(key) == ""


def test_the_file_fallback_does_not_store_plain_text(tmp_path):
    store = SecretStore(fallback_file=tmp_path / "creds.dat")
    store._keyring = None  # force the fallback path
    store.set("slskd.password", SECRET)
    raw = (tmp_path / "creds.dat").read_bytes()
    assert SECRET.encode() not in raw
    assert store.get("slskd.password") == SECRET


def test_a_damaged_credential_file_is_not_fatal(tmp_path):
    path = tmp_path / "creds.dat"
    path.write_bytes(b"this is not valid")
    store = SecretStore(fallback_file=path)
    store._keyring = None
    assert store.get("anything") == ""


# ---------------------------------------------------------------------------
# The diagnostic report
# ---------------------------------------------------------------------------


def test_the_diagnostic_report_contains_no_secrets(scratch_home):
    settings = Settings()
    settings.provider("qbittorrent").enabled = True
    settings.provider("qbittorrent").config.update(
        {
            "base_url": f"http://admin:{SECRET}@localhost:8080",
            "username": "admin",
            "password": SECRET,
            "api_key": API_KEY,
        }
    )
    report = build_report(settings)
    assert SECRET not in report
    assert API_KEY not in report
    assert "(hidden)" in report


def test_the_diagnostic_report_says_what_it_should(scratch_home):
    settings = Settings()
    report = build_report(settings)
    assert "TRACKFETCH DIAGNOSTIC INFORMATION" in report
    assert "no passwords, keys or tokens" in report
    assert "Trackfetch version:" in report
    assert "System:" in report
    assert "Download folder:" in report
    assert "[slskd]" in report


def test_the_diagnostic_report_includes_provider_status_and_events(
    scratch_home, secrets, repos
):
    from trackfetch.providers.registry import ProviderRegistry

    settings = Settings()
    settings.provider("http").enabled = True
    registry = ProviderRegistry(settings, secrets)
    repos.events.add("Search failed for 'thing'", "error", "slskd")

    report = build_report(settings, registry, repos)
    assert "Service status" in report
    assert "Recent activity" in report
    assert "Search failed" in report
    assert "Queue summary" in report
    registry.close()


def test_the_report_survives_a_provider_that_will_not_answer(scratch_home, secrets):
    from trackfetch.providers.registry import ProviderRegistry

    class Broken:
        def all(self):
            raise RuntimeError("no")

        def failed(self):
            return {"nonsense": "module not found"}

    settings = Settings()
    report = build_report(settings, Broken())
    assert "could not read service status" in report


def test_a_url_with_a_password_in_it_is_cleaned(scratch_home):
    settings = Settings()
    settings.provider("nzbget").config["base_url"] = f"http://user:{SECRET}@box:6789"
    report = build_report(settings)
    assert SECRET not in report
    assert "box:6789" in report
