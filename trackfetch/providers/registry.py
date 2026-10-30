"""The provider registry.

The rest of Trackfetch asks the registry for providers and for capabilities. It
never imports a provider module. Adding a provider means adding one entry to
``PROVIDER_MODULES`` and writing the adapter.

Providers are imported lazily and one failing import never takes the
application down: a provider that cannot load is reported as unavailable and
everything else carries on.
"""

from __future__ import annotations

import importlib
import logging
import threading
from typing import Callable, Iterable

from trackfetch.config.secrets import SecretStore
from trackfetch.config.settings import Settings
from trackfetch.models.common import Capability, ConnectionStatus
from trackfetch.providers.base import Provider, ProviderContext

log = logging.getLogger(__name__)

#: provider id -> (module path, class name). Order is the order shown in the
#: Connections list, with Soulseek first because it is the point of the program.
PROVIDER_MODULES: dict[str, tuple[str, str]] = {
    "slskd": ("trackfetch.providers.slskd", "SlskdProvider"),
    "http": ("trackfetch.providers.http", "HttpProvider"),
    "qbittorrent": ("trackfetch.providers.qbittorrent", "QBittorrentProvider"),
    "transmission": ("trackfetch.providers.transmission", "TransmissionProvider"),
    "deluge": ("trackfetch.providers.deluge", "DelugeProvider"),
    "jdownloader": ("trackfetch.providers.jdownloader", "JDownloaderProvider"),
    "aria2": ("trackfetch.providers.aria2", "Aria2Provider"),
    "sftp": ("trackfetch.providers.sftp", "SftpProvider"),
    "webdav": ("trackfetch.providers.webdav", "WebdavProvider"),
    "syncthing": ("trackfetch.providers.syncthing", "SyncthingProvider"),
    "nzbget": ("trackfetch.providers.nzbget", "NzbgetProvider"),
    "sabnzbd": ("trackfetch.providers.sabnzbd", "SabnzbdProvider"),
}


class ProviderRegistry:
    """Creates, caches and hands out provider instances."""

    def __init__(
        self,
        settings: Settings,
        secrets: SecretStore,
        on_event: Callable[[str, str, str], None] | None = None,
    ):
        self._settings = settings
        self._secrets = secrets
        self._on_event = on_event or (lambda message, level, provider_id: None)
        self._instances: dict[str, Provider] = {}
        self._failed: dict[str, str] = {}
        self._lock = threading.RLock()

    # -- construction ------------------------------------------------------

    def _create(self, provider_id: str) -> Provider | None:
        entry = PROVIDER_MODULES.get(provider_id)
        if entry is None:
            return None
        module_path, class_name = entry
        try:
            module = importlib.import_module(module_path)
            klass = getattr(module, class_name)
        except Exception as exc:
            log.error("Provider %s could not be loaded: %r", provider_id, exc)
            self._failed[provider_id] = str(exc)
            return None
        ctx = ProviderContext(
            provider_id=provider_id,
            settings=self._settings,
            provider_settings=self._settings.provider(provider_id),
            secrets=self._secrets,
            on_event=self._on_event,
        )
        try:
            return klass(ctx)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("Provider %s could not start: %r", provider_id, exc)
            self._failed[provider_id] = str(exc)
            return None

    def get(self, provider_id: str) -> Provider | None:
        with self._lock:
            if provider_id in self._instances:
                return self._instances[provider_id]
            if provider_id in self._failed:
                return None
            provider = self._create(provider_id)
            if provider is not None:
                self._instances[provider_id] = provider
            return provider

    def require(self, provider_id: str) -> Provider:
        provider = self.get(provider_id)
        if provider is None:
            from trackfetch.providers.errors import ProviderNotConfigured

            raise ProviderNotConfigured(
                "That service is not available in this copy of Trackfetch.",
                "Use Copy Diagnostic Information and send it on, so the problem "
                "can be looked at.",
                technical=self._failed.get(provider_id, "unknown provider"),
                provider_id=provider_id,
            )
        return provider

    # -- listings ----------------------------------------------------------

    def all_ids(self) -> list[str]:
        return list(PROVIDER_MODULES.keys())

    def all(self) -> list[Provider]:
        return [p for p in (self.get(pid) for pid in self.all_ids()) if p is not None]

    def enabled(self) -> list[Provider]:
        return [p for p in self.all() if p.enabled]

    def with_capability(self, capability: Capability, enabled_only: bool = True) -> list[Provider]:
        source = self.enabled() if enabled_only else self.all()
        return [p for p in source if p.supports(capability)]

    def search_providers(self) -> list[Provider]:
        return self.with_capability(Capability.SEARCH)

    def failed(self) -> dict[str, str]:
        return dict(self._failed)

    # -- bulk operations ---------------------------------------------------

    def test_all(self, provider_ids: Iterable[str] | None = None) -> dict[str, ConnectionStatus]:
        """Test every enabled provider. Never raises."""
        results: dict[str, ConnectionStatus] = {}
        targets = list(provider_ids) if provider_ids else [p.provider_id for p in self.enabled()]
        for provider_id in targets:
            provider = self.get(provider_id)
            if provider is None:
                continue
            try:
                results[provider_id] = provider.test_connection()
            except Exception as exc:  # pragma: no cover - defensive
                results[provider_id] = provider._status_from_error(exc)
        return results

    def refresh(self, provider_id: str) -> Provider | None:
        """Drop and recreate a provider after its settings changed."""
        with self._lock:
            existing = self._instances.pop(provider_id, None)
            if existing is not None:
                try:
                    existing.close()
                except Exception:  # pragma: no cover - defensive
                    pass
            self._failed.pop(provider_id, None)
            return self.get(provider_id)

    def close(self) -> None:
        with self._lock:
            for provider in self._instances.values():
                try:
                    provider.close()
                except Exception:  # pragma: no cover - defensive
                    pass
            self._instances.clear()
