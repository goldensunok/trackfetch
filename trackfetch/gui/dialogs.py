"""Message boxes that speak English.

A person using Trackfetch should never see a stack trace or an exception class
name. Every problem is shown as: what happened, what to do about it, and an
optional "Technical details" section for anyone who wants it.
"""

from __future__ import annotations

import logging

from PySide6.QtWidgets import QMessageBox, QWidget

from trackfetch.providers.errors import TrackfetchError

log = logging.getLogger(__name__)


def humanise(exc: Exception) -> tuple[str, str, str]:
    """Return (message, remedy, technical) for any exception."""
    if isinstance(exc, TrackfetchError):
        return exc.message, exc.remedy, exc.technical or repr(exc)
    if isinstance(exc, PermissionError):
        return (
            "Trackfetch is not allowed to use that folder or file.",
            "Choose a different folder, or check the file is not open in another "
            "program.",
            repr(exc),
        )
    if isinstance(exc, FileNotFoundError):
        return (
            "Trackfetch could not find that file.",
            "Check it has not been moved or deleted, then try again.",
            repr(exc),
        )
    if isinstance(exc, OSError):
        return (
            "Trackfetch could not read or write that file.",
            "Check there is space on the disk and that the folder still exists.",
            repr(exc),
        )
    return (
        "Something went wrong inside Trackfetch.",
        "Try again. If it keeps happening, use Copy Diagnostic Information and "
        "send the result on.",
        repr(exc),
    )


def show_problem(parent: QWidget | None, exc: Exception, title: str = "Trackfetch") -> None:
    message, remedy, technical = humanise(exc)
    log.warning("%s | %s", message, technical)
    show_message(parent, title, message, remedy, technical, QMessageBox.Icon.Warning)


def show_message(
    parent: QWidget | None,
    title: str,
    message: str,
    remedy: str = "",
    technical: str = "",
    icon: QMessageBox.Icon = QMessageBox.Icon.Information,
) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(icon)
    box.setText(message)
    if remedy:
        box.setInformativeText(remedy)
    if technical:
        box.setDetailedText(technical)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()


def ask_yes_no(
    parent: QWidget | None,
    title: str,
    question: str,
    detail: str = "",
    yes_text: str = "Yes",
    no_text: str = "No",
) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(question)
    if detail:
        box.setInformativeText(detail)
    yes = box.addButton(yes_text, QMessageBox.ButtonRole.YesRole)
    box.addButton(no_text, QMessageBox.ButtonRole.NoRole)
    box.exec()
    return box.clickedButton() is yes


def ask_choice(
    parent: QWidget | None,
    title: str,
    question: str,
    options: list[str],
    detail: str = "",
) -> str:
    """Ask a small multiple-choice question. Returns the chosen label, or ""."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(question)
    if detail:
        box.setInformativeText(detail)
    buttons = {}
    for label in options:
        buttons[box.addButton(label, QMessageBox.ButtonRole.ActionRole)] = label
    box.exec()
    return buttons.get(box.clickedButton(), "")
