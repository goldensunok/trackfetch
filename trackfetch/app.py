"""The application core.

Everything except the windows lives here, so the whole program can be driven and
tested without starting a graphical interface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from trackfetch.config.paths import app_paths
from trackfetch.config.secrets import SecretStore
from trackfetch.config.settings import Settings, SettingsStore
from trackfetch.diagnostics.collect import build_report
from trackfetch.diagnostics.logging_setup import setup_logging
from trackfetch.models.common import TransferJob
from trackfetch.providers.registry import ProviderRegistry
from trackfetch.services.search_service import SearchService
from trackfetch.services.transfer_manager import TransferManager
from trackfetch.storage.db import Database
from trackfetch.storage.repositories import Repositories

log = logging.getLogger(__name__)


class TrackfetchApp:
    """Wires the settings, the store, the providers and the services together."""

    def __init__(
        self,
        settings_store: SettingsStore | None = None,
        secrets: SecretStore | None = None,
        database: Database | None = None,
        configure_logging: bool = True,
    ):
        if configure_logging:
            setup_logging()
        self.paths = app_paths()
        self.settings_store = settings_store or SettingsStore()
        self.settings: Settings = self.settings_store.load()
        self.secrets = secrets or SecretStore()
        self.db = database or Database(self.paths.database_file)
        self.repos = Repositories(self.db)
        self.registry = ProviderRegistry(self.settings, self.secrets, self._record_event)
        self.search_service = SearchService(self.registry, self.repos, self.settings)
        self.transfers = TransferManager(
            self.registry, self.repos, self.settings, on_update=self._on_transfer_update
        )
        self._transfer_listeners: list[Callable[[TransferJob], None]] = []
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self.settings.resolved_download_dir().mkdir(parents=True, exist_ok=True)
        self.transfers.load_existing()
        self.transfers.start()
        self._started = True
        log.info("Trackfetch started")

    def shutdown(self) -> None:
        log.info("Trackfetch shutting down")
        try:
            self.transfers.stop()
        finally:
            self.stop_soulseek_helper()
            self.registry.close()
            self.repos.close()
        self._started = False

    def soulseek_helper_stoppable(self) -> bool:
        """Whether there is a Soulseek helper that Trackfetch may stop."""
        supervisor = self._soulseek_supervisor()
        if supervisor is None:
            return False
        try:
            return bool(supervisor.stoppable())
        except Exception:  # pragma: no cover - defensive
            return False

    def stop_soulseek_helper(self) -> bool:
        """Stop the slskd helper, if Trackfetch is the one that started it.

        This matters more than it looks. Soulseek allows one login per account,
        so a helper left running after Trackfetch closes keeps hold of the
        account and knocks the user's ordinary Soulseek program straight back
        off. A helper the user runs themselves is never touched.
        """
        supervisor = self._soulseek_supervisor()
        if supervisor is None:
            return False
        try:
            return bool(supervisor.stop())
        except Exception:  # pragma: no cover - shutdown must never raise
            log.debug("could not stop the Soulseek helper", exc_info=True)
            return False

    def _soulseek_supervisor(self):
        """The slskd supervisor, without creating a provider that does not exist."""
        getter = getattr(self.registry, "existing", None)
        provider = getter("slskd") if callable(getter) else None
        if provider is None:
            return None
        try:
            return provider.supervisor
        except Exception:  # pragma: no cover - defensive
            return None

    # -- settings ----------------------------------------------------------

    def save_settings(self) -> None:
        self.settings_store.save(self.settings)

    def set_download_dir(self, path: Path | str) -> None:
        self.settings.download_dir = str(path)
        Path(path).expanduser().mkdir(parents=True, exist_ok=True)
        self.save_settings()

    def refresh_provider(self, provider_id: str) -> None:
        """Recreate a provider after its settings changed."""
        self.registry.refresh(provider_id)

    # -- events ------------------------------------------------------------

    def _record_event(self, message: str, level: str, provider_id: str) -> None:
        try:
            self.repos.events.add(message, level, provider_id)
        except Exception:  # pragma: no cover - diagnostics must never break work
            log.debug("could not record event", exc_info=True)

    def add_transfer_listener(self, listener: Callable[[TransferJob], None]) -> None:
        self._transfer_listeners.append(listener)

    def _on_transfer_update(self, job: TransferJob) -> None:
        for listener in list(self._transfer_listeners):
            try:
                listener(job)
            except Exception:  # pragma: no cover - defensive
                log.debug("transfer listener failed", exc_info=True)

    # -- diagnostics -------------------------------------------------------

    def diagnostic_report(self) -> str:
        return build_report(self.settings, self.registry, self.repos)
