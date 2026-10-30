"""The unified download queue.

Every transfer appears here regardless of which program is doing the work, with
the same columns and the same buttons. Where a service cannot pause or resume,
the button is simply disabled rather than pretending and failing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from trackfetch.gui.style import status_colour
from trackfetch.models.common import (
    TransferJob,
    TransferStatus,
    human_bytes,
    human_duration,
    human_speed,
)

COLUMNS = [
    "Item",
    "Service",
    "Status",
    "Progress",
    "Size",
    "Downloaded",
    "Speed",
    "Time left",
    "Saved to",
    "Message",
]

COL_PROGRESS = 3
ROLE_ID = Qt.ItemDataRole.UserRole


class QueuePanel(QWidget):
    """Table of transfers with pause, resume, retry, cancel and remove."""

    pause_requested = Signal(str)
    resume_requested = Signal(str)
    retry_requested = Signal(str)
    cancel_requested = Signal(str)
    remove_requested = Signal(str)
    clear_finished_requested = Signal()
    retry_all_failed_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows: dict[str, int] = {}
        self._jobs: dict[str, TransferJob] = {}
        self._bars: dict[str, QProgressBar] = {}
        #: Internal provider ids to the names a person recognises.
        self._provider_names: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        self.summary = QLabel("Nothing in the queue.")
        self.summary.setObjectName("SubHeading")
        top.addWidget(self.summary)
        top.addStretch(1)

        self.open_folder_button = QPushButton("Open download folder")
        self.open_folder_button.clicked.connect(self._open_download_folder)
        top.addWidget(self.open_folder_button)
        layout.addLayout(top)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for index in range(1, len(COLUMNS) - 2):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        # The destination and the message can both be long. Give them a fixed
        # share rather than letting them push the item name off the screen.
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(9, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(8, 190)
        header.resizeSection(9, 320)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(lambda: self._emit(self.pause_requested))
        buttons.addWidget(self.pause_button)

        self.resume_button = QPushButton("Resume")
        self.resume_button.clicked.connect(lambda: self._emit(self.resume_requested))
        buttons.addWidget(self.resume_button)

        self.retry_button = QPushButton("Retry")
        self.retry_button.clicked.connect(lambda: self._emit(self.retry_requested))
        buttons.addWidget(self.retry_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(lambda: self._emit(self.cancel_requested))
        buttons.addWidget(self.cancel_button)

        self.remove_button = QPushButton("Remove from list")
        self.remove_button.clicked.connect(lambda: self._emit(self.remove_requested))
        buttons.addWidget(self.remove_button)

        buttons.addStretch(1)

        self.retry_failed_button = QPushButton("Retry everything that failed")
        self.retry_failed_button.clicked.connect(self.retry_all_failed_requested.emit)
        buttons.addWidget(self.retry_failed_button)

        self.clear_button = QPushButton("Clear finished")
        self.clear_button.clicked.connect(self.clear_finished_requested.emit)
        buttons.addWidget(self.clear_button)
        layout.addLayout(buttons)

        self._download_dir = Path.home()
        self._update_buttons()

    # -- content -----------------------------------------------------------

    def set_provider_names(self, names: dict[str, str]) -> None:
        self._provider_names = dict(names)

    def set_download_dir(self, path: Path | str) -> None:
        self._download_dir = Path(path)

    def set_jobs(self, jobs: list[TransferJob]) -> None:
        self.table.setRowCount(0)
        self._rows.clear()
        self._jobs.clear()
        self._bars.clear()
        for job in jobs:
            self.upsert(job)
        self._update_summary()

    def upsert(self, job: TransferJob) -> None:
        row = self._rows.get(job.id)
        if row is None:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self._rows[job.id] = row
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            self._bars[job.id] = bar
            self.table.setCellWidget(row, COL_PROGRESS, bar)
            for column in range(len(COLUMNS)):
                if column == COL_PROGRESS:
                    continue
                cell = QTableWidgetItem("")
                if column == 0:
                    cell.setData(ROLE_ID, job.id)
                self.table.setItem(row, column, cell)
        self._jobs[job.id] = job
        self._fill_row(row, job)
        self._update_summary()
        self._update_buttons()

    def _fill_row(self, row: int, job: TransferJob) -> None:
        values = [
            job.label,
            self._provider_names.get(job.provider_id, job.provider_id),
            str(job.status),
            "",
            human_bytes(job.total_bytes),
            human_bytes(job.downloaded_bytes),
            human_speed(job.speed_bps) if job.status is TransferStatus.DOWNLOADING else "",
            human_duration(job.eta_seconds) if job.status is TransferStatus.DOWNLOADING else "",
            job.destination,
            f"{job.error} {job.remedy}".strip(),
        ]
        for column, value in enumerate(values):
            if column == COL_PROGRESS:
                continue
            cell = self.table.item(row, column)
            if cell is None:
                cell = QTableWidgetItem("")
                self.table.setItem(row, column, cell)
            cell.setText(value)
            if column == 0:
                cell.setData(ROLE_ID, job.id)
                cell.setToolTip(job.destination or job.label)
            if column == 2:
                cell.setForeground(QBrush(QColor(status_colour(job.status))))
            if column == 9 and job.error:
                cell.setToolTip(f"{job.error}\n\n{job.remedy}".strip())
                cell.setForeground(QBrush(QColor(status_colour(TransferStatus.FAILED))))

        bar = self._bars.get(job.id)
        if bar is not None:
            if job.total_bytes:
                bar.setRange(0, 100)
                bar.setValue(int(job.percent))
                bar.setFormat("%p%")
            elif job.status is TransferStatus.DOWNLOADING:
                bar.setRange(0, 0)  # a busy indicator when the size is unknown
                bar.setFormat("")
            else:
                bar.setRange(0, 100)
                bar.setValue(100 if job.status is TransferStatus.COMPLETE else 0)
                bar.setFormat("%p%")

    def remove_row(self, job_id: str) -> None:
        row = self._rows.pop(job_id, None)
        self._jobs.pop(job_id, None)
        self._bars.pop(job_id, None)
        if row is None:
            return
        self.table.removeRow(row)
        self._rows = {
            jid: (r - 1 if r > row else r) for jid, r in self._rows.items()
        }
        self._update_summary()

    # -- selection ---------------------------------------------------------

    def selected_job_ids(self) -> list[str]:
        ids: list[str] = []
        for index in self.table.selectionModel().selectedRows():
            cell = self.table.item(index.row(), 0)
            if cell is not None:
                job_id = cell.data(ROLE_ID)
                if job_id:
                    ids.append(str(job_id))
        return ids

    def _emit(self, signal: Signal) -> None:
        for job_id in self.selected_job_ids():
            signal.emit(job_id)

    def _update_buttons(self) -> None:
        selected = [self._jobs.get(i) for i in self.selected_job_ids()]
        selected = [j for j in selected if j is not None]
        any_selected = bool(selected)
        self.pause_button.setEnabled(
            any_selected and any(j.status is TransferStatus.DOWNLOADING for j in selected)
        )
        self.resume_button.setEnabled(
            any_selected and any(j.status is TransferStatus.PAUSED for j in selected)
        )
        self.retry_button.setEnabled(
            any_selected
            and any(
                j.status in (TransferStatus.FAILED, TransferStatus.CANCELLED)
                for j in selected
            )
        )
        self.cancel_button.setEnabled(
            any_selected and any(j.status.is_active for j in selected)
        )
        self.remove_button.setEnabled(any_selected)
        self.retry_failed_button.setEnabled(
            any(j.status is TransferStatus.FAILED for j in self._jobs.values())
        )

    def _update_summary(self) -> None:
        if not self._jobs:
            self.summary.setText("Nothing in the queue.")
            return
        counts: dict[TransferStatus, int] = {}
        for job in self._jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        parts = [f"{count} {str(status).lower()}" for status, count in counts.items()]
        self.summary.setText("Downloads: " + ", ".join(parts))

    # -- helpers -----------------------------------------------------------

    def _open_download_folder(self) -> None:
        path = self._download_dir
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607
        except Exception:  # pragma: no cover - depends on the desktop
            from trackfetch.gui.dialogs import show_message

            show_message(
                self,
                "Trackfetch",
                "Trackfetch could not open that folder for you.",
                f"The files are in: {path}",
            )
