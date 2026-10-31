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
        self.follow_download_dir_in_helper()

    def follow_download_dir_in_helper(self) -> bool:
        """Point the Soulseek helper at the folder the user just chose.

        Soulseek downloads are written by the helper, not by Trackfetch, so the
        helper has its own copy of the download folder, set when Soulseek was
        configured. Change the folder here and say nothing, and the files keep
        going to the old one while the screen insists otherwise. Returns True
        when the helper's settings were rewritten, which takes effect the next
        time it starts.
        """
        getter = getattr(self.registry, "existing", None)
        provider = getter("slskd") if callable(getter) else None
        if provider is None:
            return False
        try:
            supervisor = provider.supervisor
            if not supervisor.has_managed_config():
                # An slskd the user set up themselves. Not ours to rewrite.
                return False
            from trackfetch.config.secrets import SecretStore

            config = self.settings.provider("slskd").config
            soulseek_user = str(config.get("soulseek_username") or "")
            soulseek_password = self.secrets.get(
                SecretStore.key_for("slskd", "soulseek_password")
            )
            helper_password = self.secrets.get(SecretStore.key_for("slskd", "password"))
            if not (soulseek_user and soulseek_password and helper_password):
                # Rewriting with anything missing would empty the helper's own
                # credentials and break Soulseek completely. Far worse than
                # leaving the old folder in place.
                log.info(
                    "Leaving the helper's settings alone: its credentials are not all "
                    "available to rewrite them safely"
                )
                return False
            supervisor.write_config(
                soulseek_username=soulseek_user,
                soulseek_password=soulseek_password,
                web_username=str(config.get("username") or "slskd"),
                web_password=helper_password,
                downloads_dir=self.settings.resolved_download_dir(),
            )
        except Exception:  # pragma: no cover - never block the user's choice
            log.debug("could not update the helper's download folder", exc_info=True)
            return False
        log.info("The Soulseek helper will use the new download folder when it next starts")
        return True

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
