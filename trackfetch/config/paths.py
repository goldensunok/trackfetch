"""Where Trackfetch keeps its own files.

Everything Trackfetch writes lives under one folder per operating system
convention, so an ordinary user never has to know where it is, and an uninstall
leaves nothing scattered around.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_NAME = "Trackfetch"
APP_AUTHOR = "Trackfetch"


def _platformdirs():
    try:
        import platformdirs

        return platformdirs
    except ImportError:  # pragma: no cover - only on a stripped environment
        return None


def _fallback_data_dir() -> Path:
    home = Path.home()
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return base / APP_NAME
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    return home / ".local" / "share" / APP_NAME.lower()


def _default_downloads_dir() -> Path:
    pd = _platformdirs()
    if pd is not None and hasattr(pd, "user_downloads_dir"):
        try:
            return Path(pd.user_downloads_dir()) / APP_NAME
        except Exception:  # pragma: no cover - defensive
            pass
    return Path.home() / "Downloads" / APP_NAME


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved locations for configuration, database, logs and downloads."""

    data_dir: Path
    config_dir: Path
    log_dir: Path
    cache_dir: Path
    default_download_dir: Path

    @property
    def database_file(self) -> Path:
        return self.data_dir / "trackfetch.sqlite3"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "settings.json"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "trackfetch.log"

    @property
    def slskd_dir(self) -> Path:
        """Where Trackfetch keeps a slskd install it manages itself."""
        return self.data_dir / "slskd"

    def ensure(self) -> "AppPaths":
        for path in (self.data_dir, self.config_dir, self.log_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)
        return self


@lru_cache(maxsize=1)
def app_paths() -> AppPaths:
    """Return the application paths, honouring TRACKFETCH_HOME if set.

    ``TRACKFETCH_HOME`` exists so the test suite (and a portable install) can
    redirect everything into a scratch folder. Ordinary users never set it.
    """
    override = os.environ.get("TRACKFETCH_HOME")
    if override:
        root = Path(override).expanduser().resolve()
        return AppPaths(
            data_dir=root,
            config_dir=root / "config",
            log_dir=root / "logs",
            cache_dir=root / "cache",
            default_download_dir=root / "downloads",
        ).ensure()

    pd = _platformdirs()
    if pd is not None:
        data_dir = Path(pd.user_data_dir(APP_NAME, APP_AUTHOR, roaming=False))
        config_dir = Path(pd.user_config_dir(APP_NAME, APP_AUTHOR, roaming=False))
        log_dir = Path(pd.user_log_dir(APP_NAME, APP_AUTHOR))
        cache_dir = Path(pd.user_cache_dir(APP_NAME, APP_AUTHOR))
    else:  # pragma: no cover - fallback path
        data_dir = _fallback_data_dir()
        config_dir = data_dir / "config"
        log_dir = data_dir / "logs"
        cache_dir = data_dir / "cache"

    return AppPaths(
        data_dir=data_dir,
        config_dir=config_dir,
        log_dir=log_dir,
        cache_dir=cache_dir,
        default_download_dir=_default_downloads_dir(),
    ).ensure()


def resource_path(name: str) -> Path:
    """Find a bundled resource, whether running from source or from the .exe.

    PyInstaller unpacks bundled files into a temporary folder and points
    ``sys._MEIPASS`` at it, so both cases have to be handled.
    """
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / "trackfetch" / "resources" / name
        if candidate.exists():
            return candidate
        candidate = Path(bundled) / "resources" / name
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent.parent / "resources" / name


def reset_app_paths_cache() -> None:
    """Clear the cached paths. Used by tests that move TRACKFETCH_HOME."""
    app_paths.cache_clear()
