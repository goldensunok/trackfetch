"""Browsing a server: SFTP, WebDAV and Syncthing.

Those three do not search. They give you access to somewhere you already have an
account or a folder, so their workflow is: connect, look around, pick files,
download. This window is that workflow.

Anything ticked here is added to the Results tab as its own group, so the same
DOWNLOAD SELECTED button starts it, and it lands in the same queue as everything
else.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from trackfetch.app import TrackfetchApp
from trackfetch.gui.dialogs import show_message, show_problem
from trackfetch.gui.workers import BackgroundRunner
from trackfetch.models.common import Capability, SearchCandidate, human_bytes

log = logging.getLogger(__name__)

ROLE_ENTRY = Qt.ItemDataRole.UserRole


class BrowseDialog(QDialog):
    """Look around a server and tick the files you want."""

    #: Emitted with the chosen candidates when the user presses Add.
    files_chosen = Signal(object)

    def __init__(self, app: TrackfetchApp, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self.runner = BackgroundRunner(max_threads=2)
        self._path_stack: list[str] = []
        self._current_path = ""

        self.setWindowTitle("Browse a server")
        self.resize(900, 620)

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Service"))
        self.provider_selector = QComboBox()
        for provider in self.app.registry.with_capability(Capability.BROWSE):
            self.provider_selector.addItem(provider.display_name, provider.provider_id)
        top.addWidget(self.provider_selector, 1)

        self.open_button = QPushButton("Connect")
        self.open_button.clicked.connect(self.go_home)
        top.addWidget(self.open_button)
        layout.addLayout(top)

        nav = QHBoxLayout()
        self.up_button = QPushButton("Back")
        self.up_button.setEnabled(False)
        self.up_button.clicked.connect(self.go_back)
        nav.addWidget(self.up_button)
        self.location = QLabel("Not connected yet.")
        self.location.setObjectName("Hint")
        nav.addWidget(self.location, 1)
        layout.addLayout(nav)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Size", "Kind"])
        self.tree.setAlternatingRowColors(True)
        self.tree.itemDoubleClicked.connect(self._open_item)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)

        hint = QLabel(
            "Double click a folder to open it. Tick the files you want, then "
            "press Add to my list."
        )
        hint.setObjectName("Hint")
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.add_button = QPushButton("Add to my list")
        self.add_button.setObjectName("AccentAction")
        self.add_button.clicked.connect(self.add_selected)
        buttons.addWidget(self.add_button)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        if self.provider_selector.count() == 0:
            self.provider_selector.addItem("No service that can browse is switched on", "")
            self.open_button.setEnabled(False)
            self.add_button.setEnabled(False)
            self.location.setText(
                "Open Settings and switch on SFTP, WebDAV or Syncthing first."
            )

    # -- navigation --------------------------------------------------------

    @property
    def provider_id(self) -> str:
        return str(self.provider_selector.currentData() or "")

    def go_home(self) -> None:
        self._path_stack.clear()
        self._load("")

    def go_back(self) -> None:
        if not self._path_stack:
            return
        self._load(self._path_stack.pop(), remember=False)

    def _open_item(self, item: QTreeWidgetItem, column: int) -> None:
        entry = item.data(0, ROLE_ENTRY)
        if entry is None or not entry.is_dir:
            return
        self._load(entry.path)

    def _load(self, path: str, remember: bool = True) -> None:
        provider = self.app.registry.get(self.provider_id)
        if provider is None:
            return
        if remember and path != self._current_path:
            self._path_stack.append(self._current_path)
        self._current_path = path
        self.up_button.setEnabled(bool(self._path_stack))
        self.location.setText(f"Looking in: {path or 'the top level'}")
        self.tree.clear()
        self.tree.setEnabled(False)
        self.open_button.setEnabled(False)

        self.runner.run(
            provider.browse,
            path,
            on_result=self._show_entries,
            on_error=self._show_error,
        )

    def _show_entries(self, entries) -> None:
        self.tree.setEnabled(True)
        self.open_button.setEnabled(True)
        for entry in entries:
            row = QTreeWidgetItem(self.tree)
            row.setData(0, ROLE_ENTRY, entry)
            row.setText(0, entry.name)
            row.setText(1, "" if entry.is_dir else human_bytes(entry.size))
            row.setText(2, "Folder" if entry.is_dir else "File")
            if not entry.is_dir:
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(0, Qt.CheckState.Unchecked)
        if not entries:
            empty = QTreeWidgetItem(self.tree)
            empty.setText(0, "This folder is empty.")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)

    def _show_error(self, exc: Exception) -> None:
        self.tree.setEnabled(True)
        self.open_button.setEnabled(True)
        show_problem(self, exc, "Browse")

    # -- choosing ----------------------------------------------------------

    def add_selected(self) -> None:
        provider = self.app.registry.get(self.provider_id)
        if provider is None:
            return
        chosen: list[SearchCandidate] = []
        for index in range(self.tree.topLevelItemCount()):
            row = self.tree.topLevelItem(index)
            entry = row.data(0, ROLE_ENTRY)
            if entry is None or entry.is_dir:
                continue
            if row.checkState(0) != Qt.CheckState.Checked:
                continue
            try:
                candidate = provider.candidate_from_entry(entry)
            except Exception as exc:
                log.debug("could not turn %s into a download: %r", entry.path, exc)
                continue
            candidate.selected = True
            chosen.append(candidate)

        if not chosen:
            show_message(
                self,
                "Nothing ticked",
                "No files are ticked yet.",
                "Tick the box next to each file you want, then press Add to my list.",
            )
            return

        self.files_chosen.emit(chosen)
        show_message(
            self,
            "Added",
            f"{len(chosen)} file{'s' if len(chosen) != 1 else ''} added to your list.",
            "They are on the Results tab, already ticked. Press DOWNLOAD SELECTED "
            "when you are ready.",
        )
