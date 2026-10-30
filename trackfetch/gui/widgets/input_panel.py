"""The input area: paste a list, or import one.

Any text at all is a valid search. The panel says so plainly, because there is
no requirement for the list to describe music, and a person should not have to
guess what format is expected.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from trackfetch.models.common import BatchItem, InputKind
from trackfetch.services import input_parser

PLACEHOLDER = (
    "Paste your list here, one item per line.\n\n"
    "Each line can be:\n"
    "  anything you want to search for, for example  Aphex Twin - Xtal\n"
    "  a web address, for example  https://example.com/file.zip\n"
    "  a magnet link\n"
    "  the location of a torrent or NZB file\n\n"
    "Searches do not have to be music. Any words at all will do."
)


class InputPanel(QWidget):
    """Paste box plus the four Add buttons."""

    items_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        heading = QLabel("What do you want to find?")
        heading.setObjectName("SubHeading")
        layout.addWidget(heading)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(PLACEHOLDER)
        self.editor.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.editor, 1)

        buttons = QHBoxLayout()
        self.import_button = QPushButton("Import TXT or CSV")
        self.import_button.setToolTip("Open a list you have saved as a text or spreadsheet file")
        self.import_button.clicked.connect(self.import_list)
        buttons.addWidget(self.import_button)

        self.url_button = QPushButton("Add web link")
        self.url_button.setToolTip("Add a direct download address")
        self.url_button.clicked.connect(self.add_url)
        buttons.addWidget(self.url_button)

        self.torrent_button = QPushButton("Add torrent or magnet")
        self.torrent_button.clicked.connect(self.add_torrent)
        buttons.addWidget(self.torrent_button)

        self.nzb_button = QPushButton("Add NZB")
        self.nzb_button.clicked.connect(self.add_nzb)
        buttons.addWidget(self.nzb_button)

        buttons.addStretch(1)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        buttons.addWidget(self.clear_button)
        layout.addLayout(buttons)

        self.summary = QLabel("Nothing added yet.")
        self.summary.setObjectName("Hint")
        layout.addWidget(self.summary)

    # -- content -----------------------------------------------------------

    def items(self) -> list[BatchItem]:
        return input_parser.parse_lines(self.editor.toPlainText())

    def set_text(self, text: str) -> None:
        self.editor.setPlainText(text)

    def append_lines(self, lines: list[str]) -> None:
        if not lines:
            return
        existing = self.editor.toPlainText().rstrip()
        block = "\n".join(lines)
        self.editor.setPlainText(f"{existing}\n{block}".strip())

    def clear(self) -> None:
        self.editor.clear()

    def _on_text_changed(self) -> None:
        items = self.items()
        self.summary.setText(self._describe(items))
        self.items_changed.emit(len(items))

    @staticmethod
    def _describe(items: list[BatchItem]) -> str:
        if not items:
            return "Nothing added yet."
        counts = input_parser.summarise(items)
        parts: list[str] = [f"{counts['TOTAL']} item{'s' if counts['TOTAL'] != 1 else ''}"]
        labels = {
            str(InputKind.QUERY): "to search for",
            str(InputKind.URL): "web link",
            str(InputKind.MAGNET): "magnet link",
            str(InputKind.TORRENT_FILE): "torrent file",
            str(InputKind.NZB_FILE): "NZB file",
            str(InputKind.LOCAL_PATH): "file on this computer",
        }
        detail = [
            f"{counts[kind]} {label}"
            for kind, label in labels.items()
            if counts.get(kind)
        ]
        if detail:
            parts.append("(" + ", ".join(detail) + ")")
        return " ".join(parts)

    # -- add buttons -------------------------------------------------------

    def import_list(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose a list to import",
            str(Path.home()),
            "Lists (*.txt *.csv);;Text files (*.txt);;Spreadsheets (*.csv);;All files (*)",
        )
        if not paths:
            return
        items = input_parser.parse_files(paths)
        self.append_lines([i.raw_text for i in items])

    def add_url(self) -> None:
        text, ok = QInputDialog.getText(
            self,
            "Add a web link",
            "Paste the address of the file you want:",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if ok and text.strip():
            self.append_lines([text.strip()])

    def add_torrent(self) -> None:
        choice = QInputDialog.getItem(
            self,
            "Add torrent or magnet",
            "What would you like to add?",
            ["A torrent file on this computer", "A magnet link"],
            0,
            False,
        )
        label, ok = choice
        if not ok:
            return
        if label.startswith("A magnet"):
            text, ok = QInputDialog.getText(
                self, "Add a magnet link", "Paste the magnet link:"
            )
            if ok and text.strip():
                self.append_lines([text.strip()])
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose torrent files", str(Path.home()), "Torrent files (*.torrent)"
        )
        if paths:
            self.append_lines(paths)

    def add_nzb(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose NZB files", str(Path.home()), "NZB files (*.nzb)"
        )
        if paths:
            self.append_lines(paths)
