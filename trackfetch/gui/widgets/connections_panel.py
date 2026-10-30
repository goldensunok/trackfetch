"""The Connections strip.

Shows, at a glance, which services are working. Soulseek always comes first
because it is the reason the application exists. Services that are switched off
are not shown at all, so normal use is not cluttered by things the user does not
have.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from trackfetch.gui.style import connection_colour, connection_mark
from trackfetch.models.common import ConnectionState, ConnectionStatus


class ConnectionChip(QFrame):
    """One service and its state, clickable to open its settings."""

    clicked = Signal(str)

    def __init__(self, provider_id: str, display_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.provider_id = provider_id
        self.display_name = display_name
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 5, 10, 5)
        layout.setSpacing(8)

        self.mark = QLabel("–")
        self.mark.setFixedWidth(14)
        self.text = QLabel(display_name)
        layout.addWidget(self.mark)
        layout.addWidget(self.text)

        self.setToolTip(f"Click to set up {display_name}")
        # Start as "Checking" rather than "Not configured", because at this point
        # Trackfetch has not looked yet and saying otherwise is misleading.
        self.set_status(
            ConnectionStatus(
                provider_id=provider_id,
                state=ConnectionState.CONNECTING,
                detail=f"Checking {display_name}...",
            )
        )

    def set_status(self, status: ConnectionStatus) -> None:
        colour = connection_colour(status.state)
        self.mark.setText(connection_mark(status.state))
        self.mark.setStyleSheet(f"color: {colour}; font-weight: 700;")
        self.text.setText(f"{self.display_name}: {status.summary()}")
        self.text.setStyleSheet(f"color: {colour};")
        tip = status.detail or f"Click to set up {self.display_name}"
        if status.remedy:
            tip = f"{tip}\n\n{status.remedy}"
        self.setToolTip(tip)
        self.setStyleSheet(f"QFrame {{ border: 1px solid {colour}; border-radius: 13px; }}")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # A click can land on a chip in the instant between Qt scheduling its
        # deletion and actually deleting it. Touching anything on the widget
        # then raises a RuntimeError from the Qt bindings and takes the whole
        # program down, so this guards rather than assuming.
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                self.clicked.emit(self.provider_id)
            super().mouseReleaseEvent(event)
        except RuntimeError:  # pragma: no cover - the race this exists to survive
            return


class ConnectionsPanel(QWidget):
    """The row of service chips, with a Check now button."""

    configure_requested = Signal(str)
    test_requested = Signal()
    diagnostics_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._chips: dict[str, ConnectionChip] = {}
        #: What the strip is currently showing, so a refresh that changes nothing
        #: does nothing.
        self._shown: list[tuple[str, str]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel("Connections")
        title.setObjectName("SubHeading")
        row.addWidget(title)

        self._chip_row = QHBoxLayout()
        self._chip_row.setSpacing(8)
        row.addLayout(self._chip_row)
        row.addStretch(1)

        self.check_button = QPushButton("Check now")
        self.check_button.setToolTip("Check every service again")
        self.check_button.clicked.connect(self.test_requested.emit)
        row.addWidget(self.check_button)

        # Kept in plain sight rather than buried in a menu, because it is the
        # first thing to reach for when something is not working.
        self.diagnostics_button = QPushButton("Copy Diagnostic Information")
        self.diagnostics_button.setToolTip(
            "Copy a report you can paste into a message. It contains no passwords."
        )
        self.diagnostics_button.clicked.connect(self.diagnostics_requested.emit)
        row.addWidget(self.diagnostics_button)

        outer.addLayout(row)

        self.hint = QLabel("")
        self.hint.setObjectName("Hint")
        self.hint.setWordWrap(True)
        outer.addWidget(self.hint)

    # -- content -----------------------------------------------------------

    def set_providers(self, providers: list[tuple[str, str]]) -> None:
        """Make the strip show exactly these services, in this order.

        Called on every automatic refresh. Rebuilding the chips each time threw
        away perfectly good widgets, lost the state already showing on them, and
        left a deleted chip under the mouse pointer, which crashed the program if
        the user happened to click at that moment. So nothing is touched unless
        the list of services has genuinely changed.
        """
        wanted = list(providers)
        if [(pid, name) for pid, name in wanted] == self._shown:
            return

        for chip in self._chips.values():
            self._chip_row.removeWidget(chip)
            try:
                chip.clicked.disconnect()
            except (RuntimeError, TypeError):  # pragma: no cover - nothing connected
                pass
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()

        for provider_id, name in wanted:
            chip = ConnectionChip(provider_id, name)
            chip.clicked.connect(self.configure_requested.emit)
            self._chip_row.addWidget(chip)
            self._chips[provider_id] = chip
        self._shown = [(pid, name) for pid, name in wanted]

    def set_status(self, provider_id: str, status: ConnectionStatus) -> None:
        chip = self._chips.get(provider_id)
        if chip is None:
            return
        try:
            chip.set_status(status)
        except RuntimeError:  # pragma: no cover - chip removed while a check was in flight
            self._chips.pop(provider_id, None)

    def set_statuses(self, statuses: dict[str, ConnectionStatus]) -> None:
        for provider_id, status in statuses.items():
            self.set_status(provider_id, status)
        self._update_hint(statuses)

    def _update_hint(self, statuses: dict[str, ConnectionStatus]) -> None:
        """Show the single most useful piece of advice, not a wall of text."""
        soulseek = statuses.get("slskd")
        if soulseek is not None and soulseek.state is not ConnectionState.CONNECTED:
            if soulseek.remedy:
                self.hint.setText(f"{soulseek.detail} {soulseek.remedy}")
                self.hint.setStyleSheet(f"color: {connection_colour(soulseek.state)};")
                return
        broken = [
            s
            for s in statuses.values()
            if s.state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR)
        ]
        if broken:
            first = broken[0]
            self.hint.setText(f"{first.detail} {first.remedy}".strip())
            self.hint.setStyleSheet(f"color: {connection_colour(first.state)};")
        else:
            self.hint.setText("")
            self.hint.setStyleSheet("")
