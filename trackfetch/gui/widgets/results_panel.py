"""The results table.

Results are grouped under the item they came from, so it is always obvious which
candidate belongs to which request. Nothing is ticked automatically. A file is
downloaded only when the person ticks it and presses DOWNLOAD SELECTED.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from trackfetch.gui.widgets.filter_bar import FilterBar
from trackfetch.models.common import (
    BatchItem,
    SearchCandidate,
    human_bytes,
    human_duration,
    human_speed,
)
from trackfetch.services.filters import ResultFilter

COLUMNS = [
    "Requested item / Candidate",
    "Provider",
    "Type",
    "Quality",
    "Size",
    "Length",
    "From",
    "Queue",
    "Match",
]

ROLE_KIND = Qt.ItemDataRole.UserRole
ROLE_ID = Qt.ItemDataRole.UserRole + 1


class ResultsPanel(QWidget):
    """Grouped results, filters, and manual selection."""

    selection_changed = Signal(int)
    research_requested = Signal(str, str)  # batch item id, new text
    skip_requested = Signal(str, bool)  # batch item id, skipped

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._groups: dict[str, QTreeWidgetItem] = {}
        self._items: dict[str, BatchItem] = {}
        self._candidates: dict[str, list[SearchCandidate]] = {}
        self._by_id: dict[str, SearchCandidate] = {}
        self._filter = ResultFilter()
        self._updating = False
        #: Internal provider ids to the names a person recognises.
        self._provider_names: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.filter_bar = FilterBar()
        self.filter_bar.filter_changed.connect(self._on_filter_changed)
        layout.addWidget(self.filter_bar)

        controls = QHBoxLayout()
        self.summary = QLabel("No results yet.")
        self.summary.setObjectName("Hint")
        controls.addWidget(self.summary)
        controls.addStretch(1)

        self.expand_button = QPushButton("Expand all")
        self.expand_button.clicked.connect(self._toggle_expand)
        controls.addWidget(self.expand_button)

        self.clear_selection_button = QPushButton("Untick everything")
        self.clear_selection_button.clicked.connect(self.clear_selection)
        controls.addWidget(self.clear_selection_button)
        layout.addLayout(controls)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        self.tree.itemChanged.connect(self._on_item_changed)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index in range(1, len(COLUMNS)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)

        hint = QLabel(
            "Tick the files you want, then press DOWNLOAD SELECTED. "
            "Right click an item to change its search words, search it again, or skip it."
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    # -- content -----------------------------------------------------------

    def set_provider_names(self, names: dict[str, str]) -> None:
        self._provider_names = dict(names)

    def reset(self) -> None:
        self.tree.clear()
        self._groups.clear()
        self._items.clear()
        self._candidates.clear()
        self._by_id.clear()
        self._update_summary()

    def add_group(self, item: BatchItem, candidates: list[SearchCandidate], error: str = "") -> None:
        """Add or replace the results for one requested item."""
        self._items[item.id] = item
        self._candidates[item.id] = list(candidates)
        for candidate in candidates:
            self._by_id[candidate.id] = candidate

        existing = self._groups.get(item.id)
        if existing is not None:
            index = self.tree.indexOfTopLevelItem(existing)
            self.tree.takeTopLevelItem(index)

        group = QTreeWidgetItem(self.tree)
        group.setData(0, ROLE_KIND, "group")
        group.setData(0, ROLE_ID, item.id)
        group.setFirstColumnSpanned(False)
        group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        self._groups[item.id] = group
        self._populate_group(item.id)
        self._update_summary()
        # Results restored from a previous session, or picked while browsing a
        # server, can arrive already ticked. Tell the window, so DOWNLOAD
        # SELECTED is available straight away.
        self.selection_changed.emit(self.selected_count())

    def set_group_error(self, item: BatchItem, message: str, remedy: str = "") -> None:
        self.add_group(item, [], error=message)
        group = self._groups.get(item.id)
        if group is not None:
            group.setText(0, f"{item.raw_text}  —  {message}")
            group.setToolTip(0, f"{message}\n\n{remedy}".strip())
            group.setForeground(0, QBrush(QColor("#b3261e")))

    def _populate_group(self, item_id: str) -> None:
        group = self._groups.get(item_id)
        item = self._items.get(item_id)
        if group is None or item is None:
            return

        self._updating = True
        try:
            group.takeChildren()
            candidates = self._candidates.get(item_id, [])
            visible = self._filter.apply(candidates)
            visible.sort(key=lambda c: c.relevance, reverse=True)

            for candidate in visible:
                child = QTreeWidgetItem(group)
                child.setData(0, ROLE_KIND, "candidate")
                child.setData(0, ROLE_ID, candidate.id)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(
                    0,
                    Qt.CheckState.Checked if candidate.selected else Qt.CheckState.Unchecked,
                )
                self._fill_row(child, candidate)
                child.setText(
                    1,
                    self._provider_names.get(candidate.provider_id, candidate.provider_id),
                )

            selected = sum(1 for c in candidates if c.selected)
            hidden = len(candidates) - len(visible)
            label = f"{item.raw_text}  —  {len(candidates)} result"
            if len(candidates) != 1:
                label += "s"
            if hidden:
                label += f", {hidden} hidden by filters"
            if selected:
                label += f", {selected} ticked"
            if item.skipped:
                label = f"[skipped]  {label}"
            group.setText(0, label)
            group.setForeground(
                0, QBrush(QColor("#8a8a8a" if item.skipped else "#000000"))
            )
            group.setExpanded(bool(visible))
        finally:
            self._updating = False

    @staticmethod
    def _fill_row(row: QTreeWidgetItem, candidate: SearchCandidate) -> None:
        name = candidate.filename or candidate.display_path
        if candidate.locked:
            name = f"{name}  (locked)"
        row.setText(0, name)
        row.setToolTip(0, candidate.display_path)
        row.setText(1, candidate.provider_id)
        row.setText(2, (candidate.extension or "").upper())
        row.setText(3, candidate.quality_summary)
        row.setText(4, human_bytes(candidate.size))
        row.setText(5, human_duration(candidate.duration))
        row.setText(6, candidate.remote_user)
        queue = ""
        if candidate.has_free_slot is True:
            queue = "free slot"
        elif candidate.queue_length is not None:
            queue = f"{candidate.queue_length} waiting"
        if candidate.upload_speed:
            queue = f"{queue}, {human_speed(candidate.upload_speed)}".strip(", ")
        row.setText(7, queue)
        row.setText(8, f"{int(candidate.relevance * 100)}%")

    # -- filters -----------------------------------------------------------

    def set_filter(self, value: ResultFilter) -> None:
        self.filter_bar.load_filter(value)

    def _on_filter_changed(self, value: ResultFilter) -> None:
        self._filter = value
        for item_id in list(self._groups):
            self._populate_group(item_id)
        self._update_summary()

    # -- selection ---------------------------------------------------------

    def _on_item_changed(self, row: QTreeWidgetItem, column: int) -> None:
        if self._updating or column != 0:
            return
        if row.data(0, ROLE_KIND) != "candidate":
            return
        candidate = self._by_id.get(row.data(0, ROLE_ID))
        if candidate is None:
            return
        candidate.selected = row.checkState(0) == Qt.CheckState.Checked
        self._refresh_group_label(candidate)
        self._update_summary()
        self.selection_changed.emit(self.selected_count())

    def _refresh_group_label(self, candidate: SearchCandidate) -> None:
        for item_id, candidates in self._candidates.items():
            if any(c.id == candidate.id for c in candidates):
                group = self._groups.get(item_id)
                item = self._items.get(item_id)
                if group is None or item is None:
                    return
                selected = sum(1 for c in candidates if c.selected)
                base = group.text(0).split("  —  ")[0]
                total = len(candidates)
                label = f"{base}  —  {total} result{'s' if total != 1 else ''}"
                if selected:
                    label += f", {selected} ticked"
                group.setText(0, label)
                return

    def selected_candidates(self) -> list[SearchCandidate]:
        return [c for c in self._by_id.values() if c.selected]

    def selected_count(self) -> int:
        return len(self.selected_candidates())

    def clear_selection(self) -> None:
        for candidate in self._by_id.values():
            candidate.selected = False
        for item_id in list(self._groups):
            self._populate_group(item_id)
        self._update_summary()
        self.selection_changed.emit(0)

    def batch_item_for_candidate(self, candidate_id: str) -> str:
        for item_id, candidates in self._candidates.items():
            if any(c.id == candidate_id for c in candidates):
                return item_id
        return ""

    def _update_summary(self) -> None:
        total = sum(len(v) for v in self._candidates.values())
        shown = sum(len(self._filter.apply(v)) for v in self._candidates.values())
        selected = self.selected_count()
        if not total:
            self.summary.setText("No results yet.")
            return
        text = f"{total} result{'s' if total != 1 else ''} found"
        if shown != total:
            text += f", {shown} shown after filtering"
        text += f", {selected} ticked for download"
        self.summary.setText(text)

    def _toggle_expand(self) -> None:
        expanded = self.expand_button.text() == "Expand all"
        for index in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(index).setExpanded(expanded)
        self.expand_button.setText("Collapse all" if expanded else "Expand all")

    # -- context menu ------------------------------------------------------

    def _show_menu(self, position) -> None:
        row = self.tree.itemAt(position)
        if row is None:
            return
        kind = row.data(0, ROLE_KIND)
        menu = QMenu(self)

        if kind == "group":
            item_id = row.data(0, ROLE_ID)
            item = self._items.get(item_id)
            if item is None:
                return
            edit = QAction("Change the search words and search again", self)
            edit.triggered.connect(lambda: self._edit_query(item))
            menu.addAction(edit)

            again = QAction("Search this item again", self)
            again.triggered.connect(
                lambda: self.research_requested.emit(item.id, item.raw_text)
            )
            menu.addAction(again)

            menu.addSeparator()
            skip = QAction("Skip this item" if not item.skipped else "Stop skipping", self)
            skip.triggered.connect(
                lambda: self.skip_requested.emit(item.id, not item.skipped)
            )
            menu.addAction(skip)

            menu.addSeparator()
            untick = QAction("Untick everything for this item", self)
            untick.triggered.connect(lambda: self._untick_group(item_id))
            menu.addAction(untick)
        else:
            tick = QAction("Tick the highlighted files", self)
            tick.triggered.connect(lambda: self._set_checked_for_selection(True))
            menu.addAction(tick)
            untick = QAction("Untick the highlighted files", self)
            untick.triggered.connect(lambda: self._set_checked_for_selection(False))
            menu.addAction(untick)
            menu.addSeparator()
            copy = QAction("Copy the full path", self)
            copy.triggered.connect(lambda: self._copy_path(row))
            menu.addAction(copy)

        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _edit_query(self, item: BatchItem) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, ok = QInputDialog.getText(
            self, "Change the search", "Search for:", text=item.raw_text
        )
        if ok and text.strip():
            self.research_requested.emit(item.id, text.strip())

    def _untick_group(self, item_id: str) -> None:
        for candidate in self._candidates.get(item_id, []):
            candidate.selected = False
        self._populate_group(item_id)
        self._update_summary()
        self.selection_changed.emit(self.selected_count())

    def _set_checked_for_selection(self, checked: bool) -> None:
        for row in self.tree.selectedItems():
            if row.data(0, ROLE_KIND) != "candidate":
                continue
            row.setCheckState(
                0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )

    def _copy_path(self, row: QTreeWidgetItem) -> None:
        from PySide6.QtWidgets import QApplication

        candidate = self._by_id.get(row.data(0, ROLE_ID))
        if candidate is not None:
            QApplication.clipboard().setText(candidate.display_path)
