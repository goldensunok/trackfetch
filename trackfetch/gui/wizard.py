"""The first-run setup guide.

The aim is that a person who has never used a terminal can get from installing
Trackfetch to "Soulseek connected" without touching anything technical.

The Soulseek page does all of this on its own: find the helper, download it if
it is missing, write its configuration, start it, wait for it, sign in, and
check that Soulseek itself is connected. Each step is reported in plain words as
it happens.
"""

from __future__ import annotations

import logging
import secrets as pysecrets
import string
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from trackfetch import __app_name__
from trackfetch.app import TrackfetchApp
from trackfetch.config.secrets import SecretStore
from trackfetch.gui.dialogs import show_message
from trackfetch.gui.help_text import SLSKD_EXPLANATION
from trackfetch.gui.workers import BackgroundRunner
from trackfetch.models.common import ConnectionState
from trackfetch.services.slskd_setup import SetupFailure

log = logging.getLogger(__name__)


def _random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(pysecrets.choice(alphabet) for _ in range(length))


class FirstRunWizard(QWizard):
    """Welcome, Soulseek, download folder, done."""

    def __init__(self, app: TrackfetchApp, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(f"Welcome to {__app_name__}")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.resize(760, 620)

        self.addPage(WelcomePage())
        self.addPage(SoulseekPage(app))
        self.addPage(FolderPage(app))
        self.addPage(FinishPage(app))

    def done(self, result: int) -> None:
        if result == QWizard.DialogCode.Accepted:
            self.app.settings.first_run_complete = True
            self.app.save_settings()
        super().done(result)


class WelcomePage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Welcome to Trackfetch")
        self.setSubTitle(
            "This guide sets up Soulseek for you. It takes about a minute."
        )
        layout = QVBoxLayout(self)
        text = QLabel(
            "Trackfetch finds and downloads files from several different places, "
            "and keeps them all in one list.\n\n"
            "The main one is Soulseek, where people share files with each other. "
            "Trackfetch searches it, shows you what came back, and downloads only "
            "what you tick.\n\n"
            "You can set up the other services later, from Settings. Nothing else "
            "is needed to get going.\n\n"
            "Press Next to set up Soulseek."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        layout.addStretch(1)


class SoulseekPage(QWizardPage):
    """Finds, installs, configures, starts and checks the Soulseek helper."""

    step_reported = Signal(str)
    progress_reported = Signal(float)
    setup_finished = Signal(object)

    def __init__(self, app: TrackfetchApp) -> None:
        super().__init__()
        self.app = app
        self.runner = BackgroundRunner(max_threads=2)
        self._connected = False

        self.setTitle("Soulseek")
        self.setSubTitle("Enter your Soulseek details and Trackfetch does the rest.")

        layout = QVBoxLayout(self)

        explain = QLabel(SLSKD_EXPLANATION)
        explain.setWordWrap(True)
        explain.setObjectName("Hint")
        layout.addWidget(explain)

        form = QFormLayout()
        self.username = QLineEdit()
        self.username.setPlaceholderText("your Soulseek username")
        form.addRow("Soulseek username", self.username)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Soulseek password", self.password)

        self.auto_start = QCheckBox("Let Trackfetch start and stop the helper for me")
        self.auto_start.setChecked(True)
        form.addRow("", self.auto_start)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.setup_button = QPushButton("Set up Soulseek now")
        self.setup_button.setObjectName("PrimaryAction")
        self.setup_button.clicked.connect(self.start_setup)
        buttons.addWidget(self.setup_button)
        self.skip_button = QPushButton("Skip for now")
        self.skip_button.clicked.connect(self._skip)
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.steps = QListWidget()
        self.steps.addItem("Progress will appear here once you press the button above.")
        self.steps.setEnabled(False)
        layout.addWidget(self.steps, 1)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        layout.addWidget(self.result)

        self.step_reported.connect(self._add_step)
        self.progress_reported.connect(self._set_progress)
        self.setup_finished.connect(self._on_finished)

        self._load_existing()

    # -- helpers -----------------------------------------------------------

    def _load_existing(self) -> None:
        config = self.app.settings.provider("slskd").config
        self.username.setText(str(config.get("soulseek_username", "")))
        stored = self.app.secrets.get(SecretStore.key_for("slskd", "soulseek_password"))
        if stored:
            self.password.setText(stored)
        self.auto_start.setChecked(bool(config.get("manage_slskd", True)))

    def _add_step(self, text: str) -> None:
        self.steps.addItem(text)
        self.steps.scrollToBottom()

    def _set_progress(self, fraction: float) -> None:
        self.progress.setVisible(True)
        self.progress.setValue(int(max(0.0, min(1.0, fraction)) * 100))

    def isComplete(self) -> bool:  # noqa: N802 - Qt naming
        return True  # the user may always continue and finish setup later

    # -- the work ----------------------------------------------------------

    def _skip(self) -> None:
        self.result.setText(
            "Skipped. You can set Soulseek up later from Settings, Connections."
        )
        self.wizard().next()

    def start_setup(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            show_message(
                self,
                "Soulseek details needed",
                "Please enter your Soulseek username and password.",
                "These are the same details you use in the ordinary Soulseek "
                "program. If you would rather not share an account between the two, "
                "make a second Soulseek account and use that here.",
            )
            return

        self.setup_button.setEnabled(False)
        self.steps.setEnabled(True)
        self.steps.clear()
        self.result.setText("")
        self.progress.setVisible(True)
        self.progress.setValue(0)

        self.runner.run(
            self._do_setup,
            username,
            password,
            self.auto_start.isChecked(),
            on_result=self.setup_finished.emit,
            on_error=self.setup_finished.emit,
        )

    def _do_setup(self, username: str, password: str, manage: bool):
        """Runs on a background thread. Returns a ConnectionStatus or a failure."""
        from trackfetch.services.slskd_setup import ensure_soulseek_ready

        return ensure_soulseek_ready(
            self.app,
            username,
            password,
            manage_helper=manage,
            report=self.step_reported.emit,
            progress=self.progress_reported.emit,
        )

    def _on_finished(self, outcome) -> None:
        self.setup_button.setEnabled(True)
        self.progress.setVisible(False)

        if isinstance(outcome, SetupFailure):
            self.result.setText(f"{outcome.message}\n{outcome.remedy}")
            self.result.setStyleSheet("color: #b3261e;")
            return
        if isinstance(outcome, Exception):
            self.result.setText(
                "Trackfetch could not finish setting up Soulseek. "
                "Press Set up Soulseek now to try again."
            )
            self.result.setStyleSheet("color: #b3261e;")
            log.debug("wizard setup failed: %r", outcome)
            return

        status = outcome
        if getattr(status, "state", None) is ConnectionState.CONNECTED:
            self._connected = True
            self.result.setText("Soulseek connected ✓")
            self.result.setStyleSheet("color: #1a7f37; font-weight: 700; font-size: 17px;")
            self._add_step("Soulseek connected")
        else:
            detail = getattr(status, "detail", "Soulseek is not connected.")
            remedy = getattr(status, "remedy", "")
            self.result.setText(f"{detail}\n{remedy}".strip())
            self.result.setStyleSheet("color: #b3261e;")
        self.completeChanged.emit()


class FolderPage(QWizardPage):
    def __init__(self, app: TrackfetchApp) -> None:
        super().__init__()
        self.app = app
        self.setTitle("Where should finished files go?")
        self.setSubTitle("You can change this later in Settings.")

        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.folder = QLineEdit(str(app.settings.resolved_download_dir()))
        row.addWidget(self.folder, 1)
        browse = QPushButton("Choose folder")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)

        note = QLabel(
            "Trackfetch will create this folder if it does not exist. Finished "
            "files appear here, and there is an Open download folder button in the "
            "main window."
        )
        note.setWordWrap(True)
        note.setObjectName("Hint")
        layout.addWidget(note)
        layout.addStretch(1)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose your download folder", self.folder.text()
        )
        if chosen:
            self.folder.setText(chosen)

    def validatePage(self) -> bool:  # noqa: N802 - Qt naming
        path = self.folder.text().strip()
        if not path:
            return False
        try:
            Path(path).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError:
            show_message(
                self,
                "That folder cannot be used",
                "Trackfetch could not create that folder.",
                "Please choose somewhere else, such as a folder inside your "
                "Documents or Downloads.",
            )
            return False
        self.app.set_download_dir(path)
        return True


class FinishPage(QWizardPage):
    def __init__(self, app: TrackfetchApp) -> None:
        super().__init__()
        self.app = app
        self.setTitle("You are ready")
        layout = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802 - Qt naming
        folder = self.app.settings.resolved_download_dir()
        self.summary.setText(
            "Here is how to use Trackfetch:\n\n"
            "1. Paste or import your list on the first tab. Any words at all work "
            "as a search.\n"
            "2. Press SEARCH ALL.\n"
            "3. Look at the Results tab, use the filters, and tick what you want.\n"
            "4. Press DOWNLOAD SELECTED.\n"
            "5. Watch the Downloads tab.\n\n"
            f"Finished files will appear in:\n{folder}\n\n"
            "You can set up qBittorrent, Transmission, Deluge, JDownloader, aria2, "
            "SFTP, WebDAV, Syncthing, NZBGet and SABnzbd at any time from Settings."
        )


def _port_from(url: str, default: int = 5030) -> int:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        return parsed.port or default
    except ValueError:
        return default
