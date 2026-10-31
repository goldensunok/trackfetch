"""Shared test fixtures.

Every test runs against a scratch TRACKFETCH_HOME, an in-memory secret store and
a temporary database. Nothing here touches a real service, a real network or the
machine's own configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def scratch_home(tmp_path_factory, monkeypatch):
    """Point every application path at a throwaway folder."""
    home = tmp_path_factory.mktemp("trackfetch-home")
    monkeypatch.setenv("TRACKFETCH_HOME", str(home))
    from trackfetch.config import paths

    paths.reset_app_paths_cache()
    yield home
    paths.reset_app_paths_cache()


@pytest.fixture
def secrets():
    from trackfetch.config.secrets import InMemorySecretStore

    return InMemorySecretStore()


@pytest.fixture
def settings():
    from trackfetch.config.settings import Settings

    value = Settings()
    value.first_run_complete = True
    # No pacing in tests: the gap is for being kind to the real Soulseek
    # network, and waiting for it here would only make the suite slow. The
    # pacing itself has its own tests, which set it deliberately.
    value.search_gap_seconds = 0.0
    return value


@pytest.fixture(scope="session")
def qt_app():
    """One offscreen Qt application for the whole session.

    Qt refuses to build a second QApplication, and widgets cannot be created
    without one. Nothing is shown: QT_QPA_PLATFORM=offscreen keeps it headless.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication

        application = QApplication.instance() or QApplication([])
    except Exception as exc:
        # A bare machine often has PySide6 installed but not the system
        # libraries it loads at import time, which raises ImportError rather
        # than ModuleNotFoundError and so slips past importorskip. Skipping is
        # right here: these tests check interface behaviour, and a machine with
        # no graphics stack cannot answer the question either way.
        pytest.skip(f"Qt cannot start on this machine: {exc}")
    yield application


@pytest.fixture
def db(tmp_path):
    from trackfetch.storage.db import Database

    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


@pytest.fixture
def repos(db):
    from trackfetch.storage.repositories import Repositories

    return Repositories(db)


@pytest.fixture
def slskd_ctx(settings, secrets):
    """A provider context wired for slskd, with credentials already stored."""
    from trackfetch.providers.base import ProviderContext

    provider_settings = settings.provider("slskd")
    provider_settings.enabled = True
    provider_settings.config.update(
        {
            "base_url": "http://localhost:5030",
            "username": "slskd",
            "manage_slskd": False,
            "verify_tls": True,
            "timeout": 5,
        }
    )
    ctx = ProviderContext(
        provider_id="slskd",
        settings=settings,
        provider_settings=provider_settings,
        secrets=secrets,
    )
    ctx.set_secret("password", "hunter2")
    return ctx


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
