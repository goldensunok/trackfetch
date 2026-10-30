"""The main Trackfetch window.

Layout, top to bottom:

    Connections            which services are working, at a glance
    SEARCH ALL / DOWNLOAD SELECTED     the two actions that matter
    Items / Results / Downloads        three tabs, in the order they are used
    Status line                        what is happening right now

The two big buttons are always visible, whichever tab is showing, so there is
never any hunting for them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from trackfetch import __app_name__, __version__
from trackfetch.app import TrackfetchApp
from trackfetch.gui.dialogs import ask_choice, ask_yes_no, show_message, show_problem
from trackfetch.gui.style import APP_STYLESHEET
from trackfetch.gui.widgets.connections_panel import ConnectionsPanel
from trackfetch.gui.widgets.input_panel import InputPanel
from trackfetch.gui.widgets.queue_panel import QueuePanel
from trackfetch.gui.widgets.results_panel import ResultsPanel
from trackfetch.gui.workers import BackgroundRunner
from trackfetch.models.common import (
    BatchItem,
    Capability,
    InputKind,
    TransferJob,
)
from trackfetch.services.destinations import DuplicateAction
from trackfetch.services.filters import ResultFilter

log = logging.getLogger(__name__)

TAB_ITEMS = 0
TAB_RESULTS = 1
TAB_DOWNLOADS = 2


class MainWindow(QMainWindow):
    """Everything the user normally needs, in one window."""

    #: Emitted from worker threads; Qt delivers them on the interface thread.
    search_progress = Signal(object)
    search_result = Signal(object)
    search_finished = Signal(object)
    statuses_ready = Signal(object)
    transfer_updated = Signal(object)
    link_job_ready = Signal(object)
    link_job_failed = Signal(object)

    def __init__(self, app: TrackfetchApp):
        super().__init__()
        self.app = app
        self.runner = BackgroundRunner()
        self._batch_id: str = ""
        self._items_by_id: dict[str, BatchItem] = {}
        self._searching = False
        self._search_total = 0

        self.setWindowTitle(f"{__app_name__} {__version__}")
        self.resize(1280, 860)
        self.setStyleSheet(APP_STYLESHEET)

        self._build_ui()
        self._connect_signals()

        self.app.transfers.duplicate_resolver = self._ask_about_duplicate
        self.app.add_transfer_listener(self.transfer_updated.emit)

        self._load_saved_filters()
        self._restore_previous_session()
        self.refresh_connections()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(lambda: self.refresh_connections(quiet=True))
        self._status_timer.start(60000)

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)

        self.connections = ConnectionsPanel()
        layout.addWidget(self.connections)

        layout.addLayout(self._build_action_bar())

        self.tabs = QTabWidget()
        self.input_panel = InputPanel()
        self.results_panel = ResultsPanel()
        self.queue_panel = QueuePanel()
        self.tabs.addTab(self.input_panel, "1. Your list")
        self.tabs.addTab(self.results_panel, "2. Results")
        self.tabs.addTab(self.queue_panel, "3. Downloads")
        layout.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self._build_menu()

        self.status_label = QLabel("Ready.")
        self.statusBar().addWidget(self.status_label, 1)
        self.folder_label = QLabel("")
        self.statusBar().addPermanentWidget(self.folder_label)
        self._update_folder_label()

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(10)

        self.search_button = QPushButton("SEARCH ALL")
        self.search_button.setObjectName("PrimaryAction")
        self.search_button.setToolTip(
            "Search for every item in your list. This does not download anything."
        )
        bar.addWidget(self.search_button)

        self.provider_selector = QComboBox()
        self.provider_selector.setToolTip("Which service to search")
        bar.addWidget(self.provider_selector)

        self.stop_button = QPushButton("Stop searching")
        self.stop_button.setEnabled(False)
        bar.addWidget(self.stop_button)

        bar.addSpacing(20)

        self.download_button = QPushButton("DOWNLOAD SELECTED")
        self.download_button.setObjectName("AccentAction")
        self.download_button.setEnabled(False)
        self.download_button.setToolTip("Download only the files you have ticked")
        bar.addWidget(self.download_button)

        bar.addStretch(1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(240)
        self.progress.setVisible(False)
        bar.addWidget(self.progress)

        self.progress_label = QLabel("")
        bar.addWidget(self.progress_label)

        return bar

    def _build_menu(self) -> None:
        menu = self.menuBar()

        file_menu = menu.addMenu("&File")
        choose_folder = QAction("Choose download folder", self)
        choose_folder.triggered.connect(self.choose_download_folder)
        file_menu.addAction(choose_folder)

        open_folder = QAction("Open download folder", self)
        open_folder.triggered.connect(self.queue_panel._open_download_folder)
        file_menu.addAction(open_folder)

        file_menu.addSeparator()
        browse_action = QAction("Browse a server (SFTP, WebDAV, Syncthing)", self)
        browse_action.triggered.connect(self.open_browser)
        file_menu.addAction(browse_action)

        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        tools = menu.addMenu("&Settings")
        settings_action = QAction("Settings", self)
        settings_action.triggered.connect(self.open_settings)
        tools.addAction(settings_action)

        check_action = QAction("Check all connections", self)
        check_action.triggered.connect(lambda: self.refresh_connections())
        tools.addAction(check_action)

        wizard_action = QAction("Run the setup guide again", self)
        wizard_action.triggered.connect(self.run_wizard)
        tools.addAction(wizard_action)

        tools.addSeparator()
        stop_helper = QAction("Stop the Soulseek helper", self)
        stop_helper.setToolTip(
            "Shuts down the background helper so your ordinary Soulseek program "
            "can sign in. Trackfetch does this automatically when it closes."
        )
        stop_helper.triggered.connect(self.stop_soulseek_helper)
        tools.addAction(stop_helper)

        help_menu = menu.addMenu("&Help")
        diagnostics = QAction("Copy Diagnostic Information", self)
        diagnostics.triggered.connect(self.copy_diagnostics)
        help_menu.addAction(diagnostics)

        instructions = QAction("How to use Trackfetch", self)
        instructions.triggered.connect(self.show_instructions)
        help_menu.addAction(instructions)

        about = QAction("About Trackfetch", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)

    def _connect_signals(self) -> None:
        self.search_button.clicked.connect(self.start_search_all)
        self.stop_button.clicked.connect(self.stop_search)
        self.download_button.clicked.connect(self.download_selected)

        self.connections.configure_requested.connect(self.open_settings_for)
        self.connections.test_requested.connect(lambda: self.refresh_connections())
        self.connections.diagnostics_requested.connect(self.copy_diagnostics)

        self.input_panel.items_changed.connect(self._on_items_changed)
        self.results_panel.selection_changed.connect(self._on_selection_changed)
        self.results_panel.research_requested.connect(self.research_item)
        self.results_panel.skip_requested.connect(self.set_item_skipped)

        # Every one of these can talk to an external program over the network,
        # so none of them runs on the thread that draws the window.
        self.queue_panel.pause_requested.connect(
            lambda job_id: self._queue_action(self.app.transfers.pause, job_id)
        )
        self.queue_panel.resume_requested.connect(
            lambda job_id: self._queue_action(self.app.transfers.resume, job_id)
        )
        self.queue_panel.retry_requested.connect(
            lambda job_id: self._queue_action(self.app.transfers.retry, job_id)
        )
        self.queue_panel.cancel_requested.connect(
            lambda job_id: self._queue_action(self.app.transfers.cancel, job_id)
        )
        self.queue_panel.remove_requested.connect(self._remove_job)
        self.queue_panel.clear_finished_requested.connect(self._clear_finished)
        self.queue_panel.retry_all_failed_requested.connect(
            lambda: self._queue_action(self.app.transfers.retry_all_failed)
        )

        self.search_progress.connect(self._on_search_progress)
        self.search_result.connect(self._on_search_result)
        self.search_finished.connect(self._on_search_finished)
        self.statuses_ready.connect(self._on_statuses_ready)
        self.transfer_updated.connect(self.queue_panel.upsert)
        self.link_job_ready.connect(self.queue_panel.upsert)
        self.link_job_failed.connect(lambda exc: show_problem(self, exc, "Add download"))

    # -- connections -------------------------------------------------------

    def refresh_connections(self, quiet: bool = False) -> None:
        providers = [
            (p.provider_id, p.display_name)
            for p in self.app.registry.all()
            if p.enabled or p.provider_id == "slskd"
        ]
        self.connections.set_providers(providers)
        names = {p.provider_id: p.display_name for p in self.app.registry.all()}
        self.results_panel.set_provider_names(names)
        self.queue_panel.set_provider_names(names)
        self._refresh_provider_selector()
        if not quiet:
            self.set_status("Checking connections...")
        self.runner.run(
            self.app.registry.test_all,
            on_result=self.statuses_ready.emit,
            on_error=lambda exc: log.debug("connection check failed: %r", exc),
        )

    def _on_statuses_ready(self, statuses: dict) -> None:
        self.connections.set_statuses(statuses)
        soulseek = statuses.get("slskd")
        if soulseek is not None and soulseek.ok:
            self.set_status("Soulseek: Connected.")
        else:
            self.set_status("Ready.")

    def _refresh_provider_selector(self) -> None:
        current = self.provider_selector.currentData()
        self.provider_selector.blockSignals(True)
        self.provider_selector.clear()
        for provider in self.app.registry.with_capability(Capability.SEARCH):
            self.provider_selector.addItem(provider.display_name, provider.provider_id)
        if self.provider_selector.count() == 0:
            self.provider_selector.addItem("No search service is switched on", "")
        index = self.provider_selector.findData(current)
        self.provider_selector.setCurrentIndex(max(0, index))
        self.provider_selector.blockSignals(False)

    # -- searching ---------------------------------------------------------

    def start_search_all(self) -> None:
        if self._searching:
            return
        provider_id = self.provider_selector.currentData()
        if not provider_id:
            show_message(
                self,
                "Trackfetch",
                "No service that can search is switched on.",
                "Open Settings, choose Soulseek and set it up. Soulseek is the "
                "service that searches other people's shared files.",
            )
            return

        items = self.input_panel.items()
        queries = [i for i in items if i.kind is InputKind.QUERY]
        others = [i for i in items if i.kind is not InputKind.QUERY]

        if not items:
            show_message(
                self,
                "Trackfetch",
                "There is nothing in your list yet.",
                "Type or paste what you are looking for on the first tab, one item "
                "per line.",
            )
            self.tabs.setCurrentIndex(TAB_ITEMS)
            return

        self._batch_id = self.app.repos.batches.create_batch(source="pasted")
        self.app.repos.batches.add_items(self._batch_id, items)
        self._items_by_id = {i.id: i for i in items}
        self.app.db.set_state("last_batch", self._batch_id)

        self.results_panel.reset()
        for item in others:
            self.results_panel.add_group(item, [])
            self.results_panel.set_group_error(
                item,
                "This is a link, not a search. Press DOWNLOAD SELECTED to fetch it.",
            )

        if not queries:
            show_message(
                self,
                "Trackfetch",
                "Your list has no searches in it, only links.",
                "Press DOWNLOAD SELECTED to fetch the links.",
            )
            self._on_selection_changed(0)
            self.download_button.setEnabled(True)
            self.tabs.setCurrentIndex(TAB_RESULTS)
            return

        self._set_searching(True, len(queries))
        self.tabs.setCurrentIndex(TAB_RESULTS)

        def work() -> object:
            return self.app.search_service.search_all(
                queries,
                provider_id,
                on_progress=self.search_progress.emit,
                on_result=self.search_result.emit,
            )

        self.runner.run(
            work,
            on_result=self.search_finished.emit,
            on_error=self._on_search_error,
        )

    def stop_search(self) -> None:
        self.app.search_service.cancel()
        self.set_status("Stopping the search...")

    def research_item(self, item_id: str, text: str) -> None:
        item = self._items_by_id.get(item_id)
        if item is None:
            return
        provider_id = self.provider_selector.currentData()
        if not provider_id:
            return
        item.raw_text = text
        item.skipped = False
        self.app.repos.batches.update_text(item_id, text)
        self.set_status(f"Searching again for {text}")

        def work() -> object:
            return self.app.search_service.search_all(
                [item],
                provider_id,
                on_result=self.search_result.emit,
            )

        self.runner.run(work, on_error=self._on_search_error)

    def set_item_skipped(self, item_id: str, skipped: bool) -> None:
        item = self._items_by_id.get(item_id)
        if item is None:
            return
        item.skipped = skipped
        self.app.repos.batches.set_skipped(item_id, skipped)
        self.results_panel.add_group(item, self.results_panel._candidates.get(item_id, []))

    def _set_searching(self, active: bool, total: int = 0) -> None:
        self._searching = active
        if active:
            self._search_total = total
        self.search_button.setEnabled(not active)
        self.stop_button.setEnabled(active)
        self.progress.setVisible(active)
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(0)
        if active:
            self.progress_label.setText(f"Searching 0 / {total}")
        else:
            self.progress_label.setText("")

    def _on_search_progress(self, progress) -> None:
        self.progress.setMaximum(max(1, progress.total))
        self.progress.setValue(progress.done)
        self.progress_label.setText(progress.text)
        if progress.current:
            self.set_status(f"Searched: {progress.current}")

    def _on_search_result(self, outcome) -> None:
        self._items_by_id[outcome.item.id] = outcome.item
        if outcome.error:
            self.results_panel.set_group_error(outcome.item, outcome.error, outcome.remedy)
        else:
            self.results_panel.add_group(outcome.item, outcome.candidates)

    def _on_search_finished(self, outcomes) -> None:
        self._set_searching(False)
        total = sum(len(o.candidates) for o in outcomes or [])
        failed = sum(1 for o in outcomes or [] if o.error)
        halted = self.app.search_service.halted_reason
        if halted:
            # Stopping early is the right outcome, but the user must be told, or
            # a batch of 92 that stopped at 7 just looks like it finished.
            done = len(outcomes or [])
            self.set_status(f"{halted} Stopped after {done} of {self._search_total}.")
            self.connections.hint.setText(
                f"{halted} {self.app.search_service.halted_remedy}".strip()
            )
            show_message(
                self,
                "Searching stopped",
                halted,
                f"{done} of {self._search_total} items were searched, and those "
                f"results are kept. {self.app.search_service.halted_remedy}",
            )
            self.refresh_connections()
            self.download_button.setEnabled(self.results_panel.selected_count() > 0)
            return
        message = f"Search finished. {total} result{'s' if total != 1 else ''} found."
        if failed:
            message += f" {failed} item{'s' if failed != 1 else ''} could not be searched."
        self.set_status(message)
        self.download_button.setEnabled(self.results_panel.selected_count() > 0)

    # -- the Soulseek helper -----------------------------------------------

    def stop_soulseek_helper(self) -> None:
        """Shut the helper down from the menu, so Task Manager is never needed."""
        if not self.app.soulseek_helper_stoppable():
            show_message(
                self,
                "Stop the Soulseek helper",
                "There is no helper for Trackfetch to stop.",
                "Either it is not running, or it is one you started yourself, in "
                "which case Trackfetch leaves it alone.",
            )
            return
        stopped = self.app.stop_soulseek_helper()
        if stopped:
            self.set_status("The Soulseek helper has been stopped.")
            show_message(
                self,
                "Stop the Soulseek helper",
                "The Soulseek helper has been stopped.",
                "Your ordinary Soulseek program can sign in again now. Trackfetch "
                "will start the helper again next time you search.",
            )
        else:
            show_message(
                self,
                "Stop the Soulseek helper",
                "Trackfetch could not stop the helper.",
                "Use Copy Diagnostic Information and send the result on.",
            )
        self.refresh_connections()

    def _on_search_error(self, exc: Exception) -> None:
        self._set_searching(False)
        show_problem(self, exc, "Search")

    # -- downloading -------------------------------------------------------

    def _on_selection_changed(self, count: int) -> None:
        self.download_button.setEnabled(count > 0 or self._has_direct_links())
        if count:
            self.download_button.setText(f"DOWNLOAD SELECTED ({count})")
        else:
            self.download_button.setText("DOWNLOAD SELECTED")

    def _has_direct_links(self) -> bool:
        return any(
            i.kind is not InputKind.QUERY and not i.skipped
            for i in self._items_by_id.values()
        )

    def download_selected(self) -> None:
        chosen = self.results_panel.selected_candidates()
        links = [
            i
            for i in self._items_by_id.values()
            if i.kind is not InputKind.QUERY and not i.skipped
        ]
        if not chosen and not links:
            show_message(
                self,
                "Trackfetch",
                "Nothing is ticked yet.",
                "Go to the Results tab and tick the files you want.",
            )
            return

        if self.app.settings.confirm_before_download:
            total = len(chosen) + len(links)
            if not ask_yes_no(
                self,
                "Start downloading",
                f"Start downloading {total} item{'s' if total != 1 else ''}?",
            ):
                return

        destination = self.app.settings.resolved_download_dir()
        item_ids = {
            c.id: self.results_panel.batch_item_for_candidate(c.id) for c in chosen
        }
        queued, skipped = self.app.transfers.add_candidates(chosen, destination, item_ids)

        # Choosing which program handles each link is instant and may need a
        # message box, so it happens here. Looking the links up is slow, so that
        # happens on a worker thread and the rows appear as each one resolves.
        pairs = []
        for item in links:
            provider = self._provider_for_link(item)
            if provider is not None:
                pairs.append((item, provider))
        links = [item for item, _ in pairs]
        if pairs:
            self.runner.run(self._queue_direct_links, pairs, destination)

        self.queue_panel.set_download_dir(destination)
        for job in queued:
            self.queue_panel.upsert(job)
        self.tabs.setCurrentIndex(TAB_DOWNLOADS)

        total = len(queued) + len(links)
        message = f"{total} item{'s' if total != 1 else ''} added to the queue."
        if skipped:
            message += f" {len(skipped)} skipped."
        self.set_status(message)
        if skipped:
            detail = "\n".join(f"{c.filename}: {reason}" for c, reason in skipped[:20])
            show_message(
                self,
                "Some items were skipped",
                f"{len(skipped)} item(s) were not added.",
                "The most common reason is that the file is already in your download "
                "folder or has been downloaded before.",
                detail,
            )

    def _queue_direct_links(self, pairs, destination: Path) -> None:
        """Turn URLs, magnets, torrents and NZBs into queue entries.

        Runs on a worker thread. Anything a URL needs looking up on the network
        happens here, and each finished row is handed back through a signal.
        """
        for item, provider in pairs:
            try:
                job = _job_for_link(provider, item, destination)
            except Exception as exc:
                log.debug("could not queue %s: %r", item.raw_text, exc)
                self.link_job_failed.emit(exc)
                continue
            job.batch_item_id = item.id
            self.app.transfers.add_job(job)
            self.link_job_ready.emit(job)

    def _provider_for_link(self, item: BatchItem):
        registry = self.app.registry
        if item.kind is InputKind.URL:
            for provider_id in ("http", "jdownloader", "aria2"):
                provider = registry.get(provider_id)
                if provider is not None and provider.enabled:
                    return provider
        if item.kind in (InputKind.MAGNET, InputKind.TORRENT_FILE):
            for provider_id in ("qbittorrent", "transmission", "deluge", "aria2"):
                provider = registry.get(provider_id)
                if provider is not None and provider.enabled:
                    return provider
            show_message(
                self,
                "No torrent program is set up",
                "Trackfetch has nowhere to send that torrent.",
                "Open Settings and set up qBittorrent, Transmission or Deluge.",
            )
            return None
        if item.kind is InputKind.NZB_FILE:
            for provider_id in ("nzbget", "sabnzbd"):
                provider = registry.get(provider_id)
                if provider is not None and provider.enabled:
                    return provider
            show_message(
                self,
                "No Usenet program is set up",
                "Trackfetch has nowhere to send that NZB file.",
                "Open Settings and set up NZBGet or SABnzbd.",
            )
            return None
        return None

    def _ask_about_duplicate(self, job: TransferJob, path: Path) -> DuplicateAction:
        choice = ask_choice(
            self,
            "That file is already there",
            f"{path.name} is already in your download folder.",
            ["Keep both", "Skip this one", "Replace it"],
            "Trackfetch will never replace a file without asking.",
        )
        if choice == "Skip this one":
            return DuplicateAction.SKIP
        if choice == "Replace it":
            return DuplicateAction.REPLACE
        return DuplicateAction.RENAME

    def _queue_action(self, action, *args) -> None:
        """Run a queue action on a worker thread and never block the window."""
        self.runner.run(
            action,
            *args,
            on_error=lambda exc: show_problem(self, exc, "Downloads"),
        )

    def _remove_job(self, job_id: str) -> None:
        self.queue_panel.remove_row(job_id)
        self.runner.run(
            self.app.transfers.remove,
            job_id,
            on_error=lambda exc: log.debug("could not remove %s: %r", job_id, exc),
        )

    def _clear_finished(self) -> None:
        self.runner.run(
            self.app.transfers.clear_finished,
            on_finished=lambda: self.queue_panel.set_jobs(self.app.transfers.jobs()),
            on_error=lambda exc: show_problem(self, exc, "Downloads"),
        )

    # -- settings and helpers ---------------------------------------------

    def open_settings(self) -> None:
        self.open_settings_for("")

    def open_settings_for(self, provider_id: str) -> None:
        from trackfetch.gui.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self.app, self, focus_provider=provider_id)
        dialog.exec()
        self.app.save_settings()
        self._update_folder_label()
        self.queue_panel.set_download_dir(self.app.settings.resolved_download_dir())
        self._load_saved_filters()
        self.refresh_connections()

    def open_browser(self) -> None:
        """Open the browse window for the providers that offer it."""
        from trackfetch.gui.browse_dialog import BrowseDialog

        dialog = BrowseDialog(self.app, self)
        dialog.files_chosen.connect(self._add_browsed_files)
        dialog.exec()

    def _add_browsed_files(self, candidates) -> None:
        """Put files picked from a server into the results, already ticked."""
        if not candidates:
            return
        provider = self.app.registry.get(candidates[0].provider_id)
        label = provider.display_name if provider else "Server"

        item = BatchItem(raw_text=f"Files chosen from {label}", source="browse")
        if not self._batch_id:
            self._batch_id = self.app.repos.batches.create_batch(source="browse")
            self.app.db.set_state("last_batch", self._batch_id)
        self.app.repos.batches.add_items(self._batch_id, [item])

        from trackfetch.models.common import SearchQuery

        query = SearchQuery(
            batch_item_id=item.id,
            provider_id=candidates[0].provider_id,
            text=item.raw_text,
            state="complete",
            result_count=len(candidates),
        )
        self.app.repos.queries.save(query)
        for candidate in candidates:
            candidate.query_id = query.id
        self.app.repos.candidates.save_many(candidates)

        self._items_by_id[item.id] = item
        self.results_panel.add_group(item, list(candidates))
        self._on_selection_changed(self.results_panel.selected_count())
        self.tabs.setCurrentIndex(TAB_RESULTS)

    def run_wizard(self) -> None:
        from trackfetch.gui.wizard import FirstRunWizard

        wizard = FirstRunWizard(self.app, self)
        wizard.exec()
        self.app.save_settings()
        self._update_folder_label()
        self.refresh_connections()

    def choose_download_folder(self) -> None:
        current = str(self.app.settings.resolved_download_dir())
        folder = QFileDialog.getExistingDirectory(self, "Choose your download folder", current)
        if folder:
            self.app.set_download_dir(folder)
            self._update_folder_label()
            self.queue_panel.set_download_dir(folder)

    def copy_diagnostics(self) -> None:
        try:
            report = self.app.diagnostic_report()
        except Exception as exc:  # pragma: no cover - defensive
            show_problem(self, exc, "Diagnostics")
            return
        QApplication.clipboard().setText(report)
        show_message(
            self,
            "Diagnostic information copied",
            "The diagnostic information has been copied.",
            "Paste it into your message with Ctrl and V. It contains no passwords "
            "or keys.",
            report[:4000],
        )

    def show_instructions(self) -> None:
        from trackfetch.gui.help_text import INSTRUCTIONS

        show_message(self, "How to use Trackfetch", "Trackfetch in short", INSTRUCTIONS)

    def show_about(self) -> None:
        show_message(
            self,
            "About Trackfetch",
            f"{__app_name__} version {__version__}",
            "A search and download manager for Soulseek and other services.",
        )

    # -- state -------------------------------------------------------------

    def _load_saved_filters(self) -> None:
        self.results_panel.set_filter(
            ResultFilter.from_defaults(self.app.settings.filters)
        )

    def _restore_previous_session(self) -> None:
        """Put back the last list and the queue, so nothing is lost on a restart."""
        self.queue_panel.set_download_dir(self.app.settings.resolved_download_dir())
        self.queue_panel.set_jobs(self.app.transfers.jobs())

        batch_id = self.app.db.get_state("last_batch")
        if not batch_id:
            return
        items = self.app.repos.batches.items(batch_id)
        if not items:
            return
        self._batch_id = batch_id
        self._items_by_id = {i.id: i for i in items}
        self.input_panel.set_text("\n".join(i.raw_text for i in items))
        for item in items:
            candidates = self.app.repos.candidates.for_item(item.id)
            if candidates:
                self.results_panel.add_group(item, candidates)
        if any(self.app.repos.candidates.for_item(i.id) for i in items):
            self.set_status(
                "Your previous list and results were restored. "
                "Nothing already finished will be downloaded again."
            )

    def _on_items_changed(self, count: int) -> None:
        self.search_button.setEnabled(count > 0 and not self._searching)

    def _update_folder_label(self) -> None:
        self.folder_label.setText(
            f"Downloads go to: {self.app.settings.resolved_download_dir()}"
        )

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # -- shutdown ----------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        active = [j for j in self.app.transfers.jobs() if j.status.is_active]
        if active:
            if not ask_yes_no(
                self,
                "Close Trackfetch",
                f"{len(active)} download{'s are' if len(active) != 1 else ' is'} "
                "still going.",
                "You can close now and Trackfetch will pick them up again next "
                "time you open it. Close anyway?",
                "Close",
                "Keep it open",
            ):
                event.ignore()
                return
        self.app.search_service.cancel()
        self.runner.wait(2000)
        event.accept()


def _job_for_link(provider, item: BatchItem, destination: Path) -> TransferJob:
    """Build a queue entry for a link, magnet, torrent file or NZB file."""
    from trackfetch.models.common import SearchCandidate

    if item.kind is InputKind.URL and provider.supports(Capability.RESOLVE):
        candidate = provider.resolve(item.raw_text)
        return provider.build_job(candidate, destination)

    candidate = SearchCandidate(
        provider_id=provider.provider_id,
        filename=Path(item.raw_text).name if item.kind is not InputKind.MAGNET else item.raw_text[:80],
        payload={"source": item.raw_text, "kind": str(item.kind)},
    )
    job = provider.build_job(candidate, destination)
    job.payload.setdefault("source", item.raw_text)
    job.payload.setdefault("kind", str(item.kind))
    return job
