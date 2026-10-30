"""Look and feel.

Deliberately plain: large readable type, clear buttons, obvious primary actions.
The two buttons that matter (SEARCH ALL and DOWNLOAD SELECTED) are visually
distinct from everything else so there is never any doubt about what to press.
"""

from __future__ import annotations

from trackfetch.models.common import ConnectionState, TransferStatus

FONT_SIZE = 14

COLOURS = {
    "connected": "#1a7f37",
    "disconnected": "#b3261e",
    "not_configured": "#6b6b6b",
    "disabled": "#8a8a8a",
    "working": "#8a5a00",
    "primary": "#0b57d0",
    "primary_hover": "#0a4bb5",
    "accent": "#1a7f37",
    "accent_hover": "#166b2e",
}

STATE_COLOURS = {
    ConnectionState.CONNECTED: COLOURS["connected"],
    ConnectionState.DISCONNECTED: COLOURS["disconnected"],
    ConnectionState.ERROR: COLOURS["disconnected"],
    ConnectionState.NOT_CONFIGURED: COLOURS["not_configured"],
    ConnectionState.DISABLED: COLOURS["disabled"],
    ConnectionState.CONNECTING: COLOURS["working"],
}

STATE_MARKS = {
    ConnectionState.CONNECTED: "✓",       # tick
    ConnectionState.DISCONNECTED: "✗",    # cross
    ConnectionState.ERROR: "!",
    ConnectionState.NOT_CONFIGURED: "–",  # dash
    ConnectionState.DISABLED: "–",
    ConnectionState.CONNECTING: "…",      # ellipsis
}

STATUS_COLOURS = {
    TransferStatus.WAITING: "#6b6b6b",
    TransferStatus.QUEUED: "#8a5a00",
    TransferStatus.DOWNLOADING: "#0b57d0",
    TransferStatus.PAUSED: "#6b6b6b",
    TransferStatus.COMPLETE: "#1a7f37",
    TransferStatus.FAILED: "#b3261e",
    TransferStatus.CANCELLED: "#6b6b6b",
}


APP_STYLESHEET = f"""
QWidget {{
    font-size: {FONT_SIZE}px;
}}
QLabel#Heading {{
    font-size: 20px;
    font-weight: 600;
}}
QLabel#SubHeading {{
    font-size: 15px;
    font-weight: 600;
}}
QLabel#Hint {{
    color: #5a5a5a;
}}
QPushButton {{
    padding: 7px 14px;
    border-radius: 6px;
}}
QPushButton#PrimaryAction {{
    background-color: {COLOURS["primary"]};
    color: white;
    font-size: 16px;
    font-weight: 700;
    padding: 12px 22px;
    border: none;
    border-radius: 8px;
}}
QPushButton#PrimaryAction:hover {{ background-color: {COLOURS["primary_hover"]}; }}
QPushButton#PrimaryAction:disabled {{ background-color: #b8c4d9; color: #f0f0f0; }}
QPushButton#AccentAction {{
    background-color: {COLOURS["accent"]};
    color: white;
    font-size: 16px;
    font-weight: 700;
    padding: 12px 22px;
    border: none;
    border-radius: 8px;
}}
QPushButton#AccentAction:hover {{ background-color: {COLOURS["accent_hover"]}; }}
QPushButton#AccentAction:disabled {{ background-color: #b6d4bf; color: #f0f0f0; }}
QGroupBox {{
    font-weight: 600;
    border: 1px solid #d0d0d0;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}
QTreeWidget, QTableWidget {{
    alternate-background-color: rgba(0, 0, 0, 12);
}}
QHeaderView::section {{
    padding: 5px;
    font-weight: 600;
}}
QTabBar::tab {{
    padding: 9px 18px;
    font-weight: 600;
}}
"""


def connection_mark(state: ConnectionState) -> str:
    return STATE_MARKS.get(state, "?")


def connection_colour(state: ConnectionState) -> str:
    return STATE_COLOURS.get(state, COLOURS["not_configured"])


def status_colour(status: TransferStatus) -> str:
    return STATUS_COLOURS.get(status, "#6b6b6b")
