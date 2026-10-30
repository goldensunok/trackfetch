"""Application settings: load, validate, save.

Settings live in one JSON file. The file never contains a password, an API key
or a token; those go to the operating system credential store (see
``secrets.py``). Unknown keys in the file are preserved rather than discarded so
a newer settings file opened by an older build does not lose information.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from trackfetch.config.paths import app_paths
from trackfetch.models.common import DuplicatePolicy

log = logging.getLogger(__name__)

SETTINGS_VERSION = 1


@dataclass(slots=True)
class FilterDefaults:
    """Saved default file filters. Every one of them permits "Any"."""

    extensions: list[str] = field(default_factory=list)  # empty means Any
    min_bitrate: int | None = None
    exact_bitrate: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    min_duration: int | None = None
    max_duration: int | None = None
    codecs: list[str] = field(default_factory=list)
    sample_rates: list[int] = field(default_factory=list)
    bit_depths: list[int] = field(default_factory=list)

    def is_empty(self) -> bool:
        return all(not getattr(self, f.name) for f in fields(self))


@dataclass(slots=True)
class ProviderSettings:
    """Per-provider configuration. Contains no secrets."""

    enabled: bool = False
    config: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)


def _default_providers() -> dict[str, ProviderSettings]:
    """Sensible starting state.

    HTTP needs nothing configured, so it is on from the start. Soulseek is on
    because it is the point of the application, but it will report
    "Not configured" until the first-run wizard has set it up. Everything else
    stays off so it does not clutter normal operation.
    """
    return {
        "slskd": ProviderSettings(
            enabled=True,
            config={
                "base_url": "http://localhost:5030",
                "auth_mode": "password",  # "password" or "apikey"
                "username": "slskd",
                "manage_slskd": True,
                "soulseek_username": "",
                "verify_tls": True,
                "timeout": 30,
            },
        ),
        "http": ProviderSettings(enabled=True, config={"verify_tls": True, "timeout": 60}),
        "qbittorrent": ProviderSettings(
            enabled=False,
            config={"base_url": "http://localhost:8080", "username": "admin", "verify_tls": True},
        ),
        "transmission": ProviderSettings(
            enabled=False,
            config={"base_url": "http://localhost:9091/transmission/rpc", "username": "", "verify_tls": True},
        ),
        "jdownloader": ProviderSettings(
            enabled=False,
            config={"mode": "local", "base_url": "http://localhost:3128", "email": ""},
        ),
        "sftp": ProviderSettings(enabled=False, config={"servers": []}),
        "webdav": ProviderSettings(
            enabled=False,
            config={"base_url": "", "username": "", "verify_tls": True},
        ),
        "syncthing": ProviderSettings(
            enabled=False, config={"base_url": "http://localhost:8384", "verify_tls": True}
        ),
        "nzbget": ProviderSettings(
            enabled=False,
            config={"base_url": "http://localhost:6789", "username": "nzbget", "verify_tls": True},
        ),
        "deluge": ProviderSettings(
            enabled=False, config={"base_url": "http://localhost:8112", "verify_tls": True}
        ),
        "sabnzbd": ProviderSettings(
            enabled=False, config={"base_url": "http://localhost:8080", "verify_tls": True}
        ),
        "aria2": ProviderSettings(
            enabled=False, config={"base_url": "http://localhost:6800/jsonrpc", "verify_tls": True}
        ),
    }


@dataclass(slots=True)
class Settings:
    """Everything the user can change."""

    version: int = SETTINGS_VERSION
    first_run_complete: bool = False

    # Downloads
    download_dir: str = ""
    max_concurrent_transfers: int = 3
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.ASK

    # Search
    search_timeout: int = 15
    search_concurrency: int = 4
    result_limit: int = 250
    minimum_response_file_count: int = 1
    #: Seconds to leave between starting one search and the next. Soulseek
    #: searches go out across the whole network, and sending them off with no
    #: gap is a reliable way to be dropped. Zero means no gap at all.
    search_gap_seconds: float = 2.0

    # Interface
    theme: str = "system"
    confirm_before_download: bool = False

    filters: FilterDefaults = field(default_factory=FilterDefaults)
    providers: dict[str, ProviderSettings] = field(default_factory=_default_providers)

    #: Anything a future version wrote that this one does not understand.
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    # -- accessors ---------------------------------------------------------

    def provider(self, provider_id: str) -> ProviderSettings:
        if provider_id not in self.providers:
            self.providers[provider_id] = ProviderSettings()
        return self.providers[provider_id]

    def enabled_providers(self) -> list[str]:
        return [pid for pid, ps in self.providers.items() if ps.enabled]

    def resolved_download_dir(self) -> Path:
        if self.download_dir:
            return Path(self.download_dir).expanduser()
        return app_paths().default_download_dir

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(self._extra)
        data.update(
            {
                "version": self.version,
                "first_run_complete": self.first_run_complete,
                "download_dir": self.download_dir,
                "max_concurrent_transfers": self.max_concurrent_transfers,
                "duplicate_policy": str(self.duplicate_policy),
                "search_timeout": self.search_timeout,
                "search_concurrency": self.search_concurrency,
                "result_limit": self.result_limit,
                "minimum_response_file_count": self.minimum_response_file_count,
                "search_gap_seconds": self.search_gap_seconds,
                "theme": self.theme,
                "confirm_before_download": self.confirm_before_download,
                "filters": asdict(self.filters),
                "providers": {
                    pid: {"enabled": ps.enabled, "config": ps.config}
                    for pid, ps in self.providers.items()
                },
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {
            "version",
            "first_run_complete",
            "download_dir",
            "max_concurrent_transfers",
            "duplicate_policy",
            "search_timeout",
            "search_concurrency",
            "result_limit",
            "minimum_response_file_count",
            "search_gap_seconds",
            "theme",
            "confirm_before_download",
            "filters",
            "providers",
        }
        settings = cls()
        settings._extra = {k: v for k, v in data.items() if k not in known}

        settings.first_run_complete = bool(data.get("first_run_complete", False))
        settings.download_dir = str(data.get("download_dir") or "")
        settings.max_concurrent_transfers = _clamp(
            data.get("max_concurrent_transfers", 3), 1, 20, 3
        )
        try:
            settings.duplicate_policy = DuplicatePolicy(str(data.get("duplicate_policy", "ASK")))
        except ValueError:
            settings.duplicate_policy = DuplicatePolicy.ASK
        settings.search_timeout = _clamp(data.get("search_timeout", 15), 5, 300, 15)
        settings.search_concurrency = _clamp(data.get("search_concurrency", 4), 1, 16, 4)
        settings.result_limit = _clamp(data.get("result_limit", 250), 1, 5000, 250)
        settings.minimum_response_file_count = _clamp(
            data.get("minimum_response_file_count", 1), 1, 1000, 1
        )
        settings.search_gap_seconds = _clamp_float(
            data.get("search_gap_seconds", 2.0), 0.0, 60.0, 2.0
        )
        settings.theme = str(data.get("theme", "system"))
        settings.confirm_before_download = bool(data.get("confirm_before_download", False))

        raw_filters = data.get("filters") or {}
        if isinstance(raw_filters, dict):
            allowed = {f.name for f in fields(FilterDefaults)}
            settings.filters = FilterDefaults(
                **{k: v for k, v in raw_filters.items() if k in allowed}
            )

        providers = _default_providers()
        raw_providers = data.get("providers") or {}
        if isinstance(raw_providers, dict):
            for pid, raw in raw_providers.items():
                if not isinstance(raw, dict):
                    continue
                base = providers.get(pid, ProviderSettings())
                base.enabled = bool(raw.get("enabled", base.enabled))
                cfg = raw.get("config")
                if isinstance(cfg, dict):
                    merged = dict(base.config)
                    merged.update(cfg)
                    base.config = merged
                providers[pid] = base
        settings.providers = providers
        return settings


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _clamp_float(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


class SettingsStore:
    """Thread-safe load/save of the settings file, written atomically."""

    def __init__(self, path: Path | None = None):
        self._path = path or app_paths().settings_file
        self._lock = threading.RLock()
        self._settings: Settings | None = None

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Settings:
        with self._lock:
            if self._settings is not None:
                return self._settings
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._settings = Settings()
                return self._settings
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Settings file could not be read, starting fresh: %s", exc)
                self._backup_broken_file()
                self._settings = Settings()
                return self._settings
            if not isinstance(raw, dict):
                self._settings = Settings()
                return self._settings
            self._settings = Settings.from_dict(raw)
            return self._settings

    def save(self, settings: Settings | None = None) -> None:
        with self._lock:
            if settings is not None:
                self._settings = settings
            if self._settings is None:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._settings.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self._path)

    def reload(self) -> Settings:
        with self._lock:
            self._settings = None
            return self.load()

    def _backup_broken_file(self) -> None:
        try:
            if self._path.exists():
                self._path.replace(self._path.with_suffix(".json.broken"))
        except OSError:  # pragma: no cover - defensive
            pass
