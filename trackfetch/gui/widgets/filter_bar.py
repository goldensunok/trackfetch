"""The filter controls above the results.

Everything starts at Any. Nothing is pre-selected, nothing is preferred, and the
current state is always written out in words underneath so it is never a mystery
why a result is missing.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from trackfetch.services.filters import COMMON_EXTENSIONS, ResultFilter

ANY = "Any"
MB = 1024 * 1024


class FilterBar(QWidget):
    """User-controlled filters. Emits the whole filter whenever it changes."""

    filter_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        box = QGroupBox("Filters (everything starts at Any)")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)

        row = 0
        grid.addWidget(QLabel("File type"), row, 0)
        self.extension = QComboBox()
        self.extension.addItem(ANY)
        for ext in COMMON_EXTENSIONS:
            self.extension.addItem(ext.upper(), ext)
        self.extension.setToolTip("Show only one kind of file, or Any for all of them")
        grid.addWidget(self.extension, row, 1)

        # The bitrate needs two controls, so they share one cell and the grid
        # stays as four tidy label-and-control pairs on every row.
        grid.addWidget(QLabel("Bitrate"), row, 2)
        bitrate_cell = QWidget()
        bitrate_layout = QHBoxLayout(bitrate_cell)
        bitrate_layout.setContentsMargins(0, 0, 0, 0)
        bitrate_layout.setSpacing(6)
        self.bitrate_mode = QComboBox()
        self.bitrate_mode.addItems([ANY, "At least", "Exactly"])
        bitrate_layout.addWidget(self.bitrate_mode, 1)
        self.bitrate_value = QSpinBox()
        self.bitrate_value.setRange(0, 10000)
        self.bitrate_value.setSingleStep(32)
        self.bitrate_value.setSuffix(" kbps")
        self.bitrate_value.setEnabled(False)
        bitrate_layout.addWidget(self.bitrate_value, 1)
        grid.addWidget(bitrate_cell, row, 3)

        grid.addWidget(QLabel("Sample rate"), row, 4)
        self.sample_rate = QComboBox()
        self.sample_rate.addItem(ANY)
        for rate in (44100, 48000, 88200, 96000, 176400, 192000):
            self.sample_rate.addItem(f"{rate / 1000:g} kHz", rate)
        grid.addWidget(self.sample_rate, row, 5)

        grid.addWidget(QLabel("Bit depth"), row, 6)
        self.bit_depth = QComboBox()
        self.bit_depth.addItem(ANY)
        for depth in (8, 16, 24, 32):
            self.bit_depth.addItem(f"{depth}-bit", depth)
        grid.addWidget(self.bit_depth, row, 7)

        row = 1
        grid.addWidget(QLabel("Size from"), row, 0)
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 1_000_000)
        self.min_size.setSuffix(" MB")
        self.min_size.setSpecialValueText(ANY)
        grid.addWidget(self.min_size, row, 1)

        grid.addWidget(QLabel("Size to"), row, 2)
        self.max_size = QSpinBox()
        self.max_size.setRange(0, 1_000_000)
        self.max_size.setSuffix(" MB")
        self.max_size.setSpecialValueText(ANY)
        grid.addWidget(self.max_size, row, 3)

        grid.addWidget(QLabel("Length from"), row, 4)
        self.min_duration = QSpinBox()
        self.min_duration.setRange(0, 100000)
        self.min_duration.setSuffix(" s")
        self.min_duration.setSpecialValueText(ANY)
        grid.addWidget(self.min_duration, row, 5)

        grid.addWidget(QLabel("Length to"), row, 6)
        self.max_duration = QSpinBox()
        self.max_duration.setRange(0, 100000)
        self.max_duration.setSuffix(" s")
        self.max_duration.setSpecialValueText(ANY)
        grid.addWidget(self.max_duration, row, 7)

        row = 2
        grid.addWidget(QLabel("Name contains"), row, 0)
        self.text_contains = QLineEdit()
        self.text_contains.setPlaceholderText("any words")
        grid.addWidget(self.text_contains, row, 1)

        grid.addWidget(QLabel("Match at least"), row, 2)
        self.min_relevance = QSpinBox()
        self.min_relevance.setRange(0, 100)
        self.min_relevance.setSuffix(" %")
        self.min_relevance.setSpecialValueText(ANY)
        self.min_relevance.setToolTip(
            "How closely the file name has to match what you asked for"
        )
        grid.addWidget(self.min_relevance, row, 3)

        self.only_free = QCheckBox("Only people with a free slot")
        grid.addWidget(self.only_free, row, 4, 1, 2)

        self.hide_locked = QCheckBox("Hide locked files")
        self.hide_locked.setToolTip(
            "Some people only share with others who share enough themselves"
        )
        grid.addWidget(self.hide_locked, row, 6, 1, 2)

        row = 3
        self.keep_unknown = QCheckBox("Keep results that do not report their details")
        self.keep_unknown.setChecked(True)
        self.keep_unknown.setToolTip(
            "Many people on Soulseek do not report a bitrate or length. Leave this "
            "ticked so those files are still shown."
        )
        grid.addWidget(self.keep_unknown, row, 0, 1, 5)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.reset_button = QPushButton("Reset to Any")
        self.reset_button.clicked.connect(self.reset)
        buttons.addWidget(self.reset_button)
        grid.addLayout(buttons, row, 6, 1, 2)

        # Labels stay their natural width; the controls share the space evenly.
        for column in (0, 2, 4, 6):
            grid.setColumnStretch(column, 0)
        for column in (1, 3, 5, 7):
            grid.setColumnStretch(column, 1)

        outer.addWidget(box)

        self.description = QLabel("No filters (showing everything)")
        self.description.setObjectName("Hint")
        outer.addWidget(self.description)

        self._connect_signals()

    # -- signals -----------------------------------------------------------

    def _connect_signals(self) -> None:
        self.extension.currentIndexChanged.connect(self._emit)
        self.bitrate_mode.currentIndexChanged.connect(self._on_bitrate_mode)
        self.bitrate_value.valueChanged.connect(self._emit)
        self.sample_rate.currentIndexChanged.connect(self._emit)
        self.bit_depth.currentIndexChanged.connect(self._emit)
        self.min_size.valueChanged.connect(self._emit)
        self.max_size.valueChanged.connect(self._emit)
        self.min_duration.valueChanged.connect(self._emit)
        self.max_duration.valueChanged.connect(self._emit)
        self.text_contains.textChanged.connect(self._emit)
        self.min_relevance.valueChanged.connect(self._emit)
        self.only_free.toggled.connect(self._emit)
        self.hide_locked.toggled.connect(self._emit)
        self.keep_unknown.toggled.connect(self._emit)

    def _on_bitrate_mode(self) -> None:
        self.bitrate_value.setEnabled(self.bitrate_mode.currentIndex() != 0)
        self._emit()

    def _emit(self) -> None:
        if self._loading:
            return
        current = self.current_filter()
        self.description.setText(current.describe())
        self.filter_changed.emit(current)

    # -- state -------------------------------------------------------------

    def current_filter(self) -> ResultFilter:
        extensions: list[str] = []
        ext = self.extension.currentData()
        if ext:
            extensions = [str(ext)]

        min_bitrate = None
        exact_bitrate = None
        mode = self.bitrate_mode.currentIndex()
        value = self.bitrate_value.value()
        if mode == 1 and value:
            min_bitrate = value
        elif mode == 2 and value:
            exact_bitrate = value

        sample_rates = []
        rate = self.sample_rate.currentData()
        if rate:
            sample_rates = [int(rate)]

        bit_depths = []
        depth = self.bit_depth.currentData()
        if depth:
            bit_depths = [int(depth)]

        return ResultFilter(
            extensions=extensions,
            min_bitrate=min_bitrate,
            exact_bitrate=exact_bitrate,
            min_size=self.min_size.value() * MB or None,
            max_size=self.max_size.value() * MB or None,
            min_duration=self.min_duration.value() or None,
            max_duration=self.max_duration.value() or None,
            sample_rates=sample_rates,
            bit_depths=bit_depths,
            text_contains=self.text_contains.text().strip(),
            min_relevance=self.min_relevance.value() / 100.0,
            only_free_slots=self.only_free.isChecked(),
            hide_locked=self.hide_locked.isChecked(),
            keep_unknown=self.keep_unknown.isChecked(),
        )

    def load_filter(self, value: ResultFilter) -> None:
        self._loading = True
        try:
            self._select_data(self.extension, value.extensions[0] if value.extensions else None)
            if value.exact_bitrate:
                self.bitrate_mode.setCurrentIndex(2)
                self.bitrate_value.setValue(value.exact_bitrate)
            elif value.min_bitrate:
                self.bitrate_mode.setCurrentIndex(1)
                self.bitrate_value.setValue(value.min_bitrate)
            else:
                self.bitrate_mode.setCurrentIndex(0)
                self.bitrate_value.setValue(0)
            self.bitrate_value.setEnabled(self.bitrate_mode.currentIndex() != 0)
            self._select_data(
                self.sample_rate, value.sample_rates[0] if value.sample_rates else None
            )
            self._select_data(self.bit_depth, value.bit_depths[0] if value.bit_depths else None)
            self.min_size.setValue(int((value.min_size or 0) / MB))
            self.max_size.setValue(int((value.max_size or 0) / MB))
            self.min_duration.setValue(value.min_duration or 0)
            self.max_duration.setValue(value.max_duration or 0)
            self.text_contains.setText(value.text_contains)
            self.min_relevance.setValue(int(value.min_relevance * 100))
            self.only_free.setChecked(value.only_free_slots)
            self.hide_locked.setChecked(value.hide_locked)
            self.keep_unknown.setChecked(value.keep_unknown)
        finally:
            self._loading = False
        self._emit()

    def reset(self) -> None:
        self.load_filter(ResultFilter())

    @staticmethod
    def _select_data(combo: QComboBox, value) -> None:
        if value is None:
            combo.setCurrentIndex(0)
            return
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)
