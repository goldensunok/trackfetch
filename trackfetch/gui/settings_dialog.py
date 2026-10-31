"""The Settings window.

Four pages: Connections, Downloads, Search and Default filters. Each service
gets the same three controls: switch it on or off, Configure, Test Connection.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from trackfetch.app import TrackfetchApp
from trackfetch.config.secrets import SecretStore
from trackfetch.gui.dialogs import show_message, show_problem
from trackfetch.gui.provider_forms import FormField, ProviderForm, form_for
from trackfetch.gui.style import connection_colour, connection_mark
from trackfetch.gui.widgets.filter_bar import FilterBar
from trackfetch.gui.workers import BackgroundRunner
from trackfetch.models.common import DuplicatePolicy
from trackfetch.services.filters import ResultFilter

log = logging.getLogger(__name__)


class ProviderConfigDialog(QDialog):
    """A form built from the provider's declared fields."""

    def __init__(self, app: TrackfetchApp, form: ProviderForm, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self.form = form
        self.widgets: dict[str, QWidget] = {}
        self.setWindowTitle(f"Set up {form.title}")
        self.setMinimumWidth(660)
        self.setMinimumHeight(560)

        layout = QVBoxLayout(self)

        description = QLabel(form.description)
        description.setWordWrap(True)
        layout.addWidget(description)

        if form.where_to_get:
            where = QLabel(f"Where to find these details: {form.where_to_get}")
            where.setWordWrap(True)
            where.setObjectName("Hint")
            layout.addWidget(where)

        if form.multi_server:
            self._build_server_list(layout)
        else:
            self._build_fields(layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        self.test_button = QPushButton("Test Connection")
        self.test_button.clicked.connect(self._test)
        buttons.addButton(self.test_button, QDialogButtonBox.ButtonRole.ActionRole)
        layout.addWidget(buttons)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        self.runner = BackgroundRunner(max_threads=2)

    # -- single-server form ------------------------------------------------

    def _build_fields(self, layout: QVBoxLayout) -> None:
        """Lay the fields out on a grid.

        A grid rather than a form layout, because the explanatory notes wrap
        onto several lines and a form layout sizes wrapped labels badly, which
        makes them overlap the boxes above and below.
        """
        settings = self.app.settings.provider(self.form.provider_id)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)

        main_fields = [f for f in self.form.fields if not f.advanced]
        advanced_fields = [f for f in self.form.fields if f.advanced]

        if main_fields:
            box = QGroupBox("Details")
            self._fill_grid(QGridLayout(box), main_fields, settings.config)
            container_layout.addWidget(box)

        if advanced_fields:
            # Collapsed by default: an ordinary setup never needs to open it.
            advanced_box = QGroupBox("Advanced settings (you can usually ignore these)")
            advanced_box.setCheckable(True)
            advanced_box.setChecked(False)
            inner = QWidget()
            self._fill_grid(QGridLayout(inner), advanced_fields, settings.config)
            inner.setVisible(False)
            advanced_layout = QVBoxLayout(advanced_box)
            advanced_layout.addWidget(inner)
            advanced_box.toggled.connect(inner.setVisible)
            container_layout.addWidget(advanced_box)

        container_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

    def _fill_grid(self, grid: QGridLayout, fields: list[FormField], config: dict) -> None:
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        row = 0
        for field in fields:
            widget = self._make_widget(field)
            self.widgets[field.key] = widget
            label = QLabel(field.label + (" *" if field.required else ""))
            label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            grid.addWidget(label, row, 0)
            grid.addWidget(widget, row, 1)
            row += 1
            if field.hint:
                hint = QLabel(field.hint)
                hint.setObjectName("Hint")
                hint.setWordWrap(True)
                hint.setMinimumWidth(300)
                hint.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
                )
                grid.addWidget(hint, row, 1)
                row += 1
            self._load_value(field, widget, config)

    def _make_widget(self, field: FormField) -> QWidget:
        if field.kind == "bool":
            return QCheckBox()
        if field.kind == "int":
            spin = QSpinBox()
            spin.setRange(field.minimum, field.maximum)
            return spin
        if field.kind == "choice":
            combo = QComboBox()
            for value, label in field.choices:
                combo.addItem(label, value)
            return combo
        if field.kind == "folder":
            return _FolderPicker()
        line = QLineEdit()
        if field.kind == "password":
            line.setEchoMode(QLineEdit.EchoMode.Password)
        return line

    def _load_value(self, field: FormField, widget: QWidget, config: dict) -> None:
        if field.secret:
            value = self.app.secrets.get(
                SecretStore.key_for(self.form.provider_id, field.key)
            )
        else:
            value = config.get(field.key, field.default)

        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value if value != "" else field.default))
        elif isinstance(widget, QSpinBox):
            try:
                widget.setValue(int(value or field.default or 0))
            except (TypeError, ValueError):
                widget.setValue(int(field.default or 0))
        elif isinstance(widget, QComboBox):
            index = widget.findData(value or field.default)
            widget.setCurrentIndex(max(0, index))
        elif isinstance(widget, _FolderPicker):
            widget.set_value(str(value or field.default or ""))
        elif isinstance(widget, QLineEdit):
            widget.setText(str(value or field.default or ""))

    def _collect(self) -> tuple[dict, dict]:
        config: dict = {}
        secrets: dict = {}
        for field in self.form.fields:
            widget = self.widgets.get(field.key)
            if widget is None:
                continue
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            elif isinstance(widget, QComboBox):
                value = widget.currentData()
            elif isinstance(widget, _FolderPicker):
                value = widget.value()
            elif isinstance(widget, QLineEdit):
                value = widget.text().strip()
            else:  # pragma: no cover - defensive
                continue
            if field.secret:
                secrets[field.key] = value
            else:
                config[field.key] = value
        return config, secrets

    # -- multi-server form -------------------------------------------------

    def _build_server_list(self, layout: QVBoxLayout) -> None:
        box = QGroupBox("Saved servers")
        inner = QVBoxLayout(box)
        self.server_list = QListWidget()
        inner.addWidget(self.server_list)

        row = QHBoxLayout()
        add = QPushButton("Add")
        add.clicked.connect(lambda: self._edit_server(None))
        row.addWidget(add)
        edit = QPushButton("Edit")
        edit.clicked.connect(self._edit_selected_server)
        row.addWidget(edit)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_selected_server)
        row.addWidget(remove)
        row.addStretch(1)
        inner.addLayout(row)
        layout.addWidget(box)
        self._reload_servers()

    def _servers(self) -> list[dict]:
        settings = self.app.settings.provider(self.form.provider_id)
        servers = settings.config.get("servers")
        return servers if isinstance(servers, list) else []

    def _reload_servers(self) -> None:
        self.server_list.clear()
        for server in self._servers():
            label = f"{server.get('name', 'server')}  ({server.get('username', '')}@{server.get('host', '')})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, server.get("name"))
            self.server_list.addItem(item)

    def _edit_selected_server(self) -> None:
        item = self.server_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        existing = next((s for s in self._servers() if s.get("name") == name), None)
        self._edit_server(existing)

    def _edit_server(self, existing: dict | None) -> None:
        dialog = _ServerDialog(self.app, self.form, existing, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        server = dialog.values()
        settings = self.app.settings.provider(self.form.provider_id)
        servers = [s for s in self._servers() if s.get("name") != server.get("name")]
        if existing is not None and existing.get("name") != server.get("name"):
            servers = [s for s in servers if s.get("name") != existing.get("name")]
        servers.append(server)
        settings.config["servers"] = servers
        self.app.save_settings()
        self._reload_servers()

    def _remove_selected_server(self) -> None:
        item = self.server_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        settings = self.app.settings.provider(self.form.provider_id)
        settings.config["servers"] = [s for s in self._servers() if s.get("name") != name]
        self.app.secrets.delete(
            SecretStore.key_for(self.form.provider_id, f"{name}.password")
        )
        self.app.save_settings()
        self._reload_servers()

    # -- saving and testing ------------------------------------------------

    def accept(self) -> None:
        if not self.form.multi_server:
            config, secrets = self._collect()
            missing = [
                f.label
                for f in self.form.fields
                if f.required
                and not (secrets.get(f.key) if f.secret else config.get(f.key))
            ]
            if missing:
                show_message(
                    self,
                    "Something is missing",
                    "Please fill in: " + ", ".join(missing),
                    "These are needed before Trackfetch can connect.",
                )
                return
            settings = self.app.settings.provider(self.form.provider_id)
            settings.config.update(config)
            for key, value in secrets.items():
                self.app.secrets.set(
                    SecretStore.key_for(self.form.provider_id, key), str(value)
                )
        self.app.save_settings()
        self.app.refresh_provider(self.form.provider_id)
        super().accept()

    def _test(self) -> None:
        if not self.form.multi_server:
            config, secrets = self._collect()
            settings = self.app.settings.provider(self.form.provider_id)
            settings.config.update(config)
            for key, value in secrets.items():
                self.app.secrets.set(
                    SecretStore.key_for(self.form.provider_id, key), str(value)
                )
            self.app.save_settings()
        self.app.refresh_provider(self.form.provider_id)

        self.test_button.setEnabled(False)
        self.result_label.setText("Checking...")

        provider = self.app.registry.get(self.form.provider_id)
        if provider is None:
            self.result_label.setText("That service is not available in this build.")
            self.test_button.setEnabled(True)
            return

        if self.form.provider_id == "slskd":
            # Soulseek is not just a connection test: the helper may need
            # installing, configuring and starting first. Do the whole thing.
            self.runner.run(
                self._prepare_soulseek,
                on_result=self._show_test_result,
                on_error=lambda exc: (
                    show_problem(self, exc, "Test Connection"),
                    self.test_button.setEnabled(True),
                ),
            )
            return

        self.runner.run(
            # interactive: the user pressed the button and is waiting, so a
            # provider may repair itself rather than only report.
            lambda p=provider: p.check_connection(interactive=True),
            on_result=self._show_test_result,
            on_error=lambda exc: (
                show_problem(self, exc, "Test Connection"),
                self.test_button.setEnabled(True),
            ),
        )

    def _prepare_soulseek(self):
        from trackfetch.services.slskd_setup import ensure_soulseek_ready

        config = self.app.settings.provider("slskd").config
        return ensure_soulseek_ready(
            self.app,
            coerce_text(config.get("soulseek_username")),
            self.app.secrets.get(SecretStore.key_for("slskd", "soulseek_password")),
            manage_helper=bool(config.get("manage_slskd", True)),
        )

    def _show_test_result(self, status) -> None:
        from trackfetch.services.slskd_setup import SetupFailure

        self.test_button.setEnabled(True)
        if isinstance(status, SetupFailure):
            self.result_label.setText(f"{status.message}\n{status.remedy}")
            self.result_label.setStyleSheet("color: #b3261e;")
            return
        colour = connection_colour(status.state)
        mark = connection_mark(status.state)
        text = f"{mark}  {status.detail or status.summary()}"
        if status.remedy and not status.ok:
            text += f"\n{status.remedy}"
        self.result_label.setText(text)
        self.result_label.setStyleSheet(f"color: {colour};")


class _ServerDialog(QDialog):
    """Add or edit one saved server."""

    def __init__(
        self,
        app: TrackfetchApp,
        form: ProviderForm,
        existing: dict | None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.app = app
        self.form = form
        self.existing = existing or {}
        self.widgets: dict[str, QWidget] = {}
        self.setWindowTitle("Server details")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        for field in form.fields:
            widget = self._make_widget(field)
            self.widgets[field.key] = widget
            form_layout.addRow(field.label + (" *" if field.required else ""), widget)
            if field.hint:
                hint = QLabel(field.hint)
                hint.setObjectName("Hint")
                hint.setWordWrap(True)
                hint.setMinimumWidth(340)
                hint.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
                )
                form_layout.addRow("", hint)
        layout.addLayout(form_layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _make_widget(self, field: FormField) -> QWidget:
        if field.kind == "int":
            spin = QSpinBox()
            spin.setRange(field.minimum, field.maximum)
            spin.setValue(int(self.existing.get(field.key, field.default) or 0))
            return spin
        line = QLineEdit()
        if field.kind == "password":
            line.setEchoMode(QLineEdit.EchoMode.Password)
            name = self.existing.get("name")
            if name:
                line.setText(
                    self.app.secrets.get(
                        SecretStore.key_for(self.form.provider_id, f"{name}.password")
                    )
                )
        else:
            line.setText(str(self.existing.get(field.key, field.default) or ""))
        return line

    def values(self) -> dict:
        out: dict = {}
        password = ""
        for field in self.form.fields:
            widget = self.widgets[field.key]
            if isinstance(widget, QSpinBox):
                out[field.key] = widget.value()
            elif isinstance(widget, QLineEdit):
                if field.secret:
                    password = widget.text()
                else:
                    out[field.key] = widget.text().strip()
        name = out.get("name") or out.get("host") or "server"
        out["name"] = name
        if password:
            self.app.secrets.set(
                SecretStore.key_for(self.form.provider_id, f"{name}.password"), password
            )
        return out

    def accept(self) -> None:
        for field in self.form.fields:
            if not field.required:
                continue
            widget = self.widgets[field.key]
            if isinstance(widget, QLineEdit) and not widget.text().strip():
                show_message(
                    self, "Something is missing", f"Please fill in {field.label}."
                )
                return
        super().accept()


class _FolderPicker(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.line = QLineEdit()
        layout.addWidget(self.line, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", self.line.text())
        if folder:
            self.line.setText(folder)

    def value(self) -> str:
        return self.line.text().strip()

    def set_value(self, value: str) -> None:
        self.line.setText(value)


class SettingsDialog(QDialog):
    """The whole Settings window."""

    def __init__(
        self, app: TrackfetchApp, parent: QWidget | None = None, focus_provider: str = ""
    ):
        super().__init__(parent)
        self.app = app
        self.runner = BackgroundRunner(max_threads=4)
        self.setWindowTitle("Trackfetch Settings")
        self.resize(920, 680)

        layout = QHBoxLayout(self)

        self.sections = QListWidget()
        self.sections.setFixedWidth(180)
        for name in ("Connections", "Downloads", "Search", "Default filters"):
            self.sections.addItem(name)
        layout.addWidget(self.sections)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_connections_page())
        self.pages.addWidget(self._build_downloads_page())
        self.pages.addWidget(self._build_search_page())
        self.pages.addWidget(self._build_filters_page())
        layout.addWidget(self.pages, 1)

        self.sections.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.sections.setCurrentRow(0)

        if focus_provider:
            self.sections.setCurrentRow(0)
            self._configure(focus_provider)

    # -- pages -------------------------------------------------------------

    def _build_connections_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        heading = QLabel("Services")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        note = QLabel(
            "Switch on only the services you actually use. Everything switched off "
            "stays out of your way."
        )
        note.setObjectName("Hint")
        note.setWordWrap(True)
        layout.addWidget(note)

        # There are a dozen services, so the list scrolls rather than running off
        # the bottom of a small screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 8, 0)

        self._provider_rows: dict[str, tuple[QCheckBox, QLabel]] = {}
        for provider in self.app.registry.all():
            holder_layout.addWidget(self._build_provider_row(provider))
        holder_layout.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        test_all = QPushButton("Test every connection")
        test_all.clicked.connect(self._test_all)
        buttons.addWidget(test_all)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        return page

    def _build_provider_row(self, provider) -> QWidget:
        row = QGroupBox(provider.display_name)
        inner = QHBoxLayout(row)

        enabled = QCheckBox("Switched on")
        enabled.setChecked(provider.enabled)
        enabled.toggled.connect(
            lambda checked, pid=provider.provider_id: self._set_enabled(pid, checked)
        )
        inner.addWidget(enabled)

        status = QLabel(provider.last_status.summary())
        status.setMinimumWidth(220)
        inner.addWidget(status, 1)

        configure = QPushButton("Configure")
        configure.clicked.connect(
            lambda _, pid=provider.provider_id: self._configure(pid)
        )
        configure.setEnabled(form_for(provider.provider_id) is not None)
        inner.addWidget(configure)

        test = QPushButton("Test Connection")
        test.clicked.connect(lambda _, pid=provider.provider_id: self._test_one(pid))
        inner.addWidget(test)

        self._provider_rows[provider.provider_id] = (enabled, status)
        return row

    def _build_downloads_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Downloads")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        box = QGroupBox("Where and how")
        form = QFormLayout(box)

        self.folder_picker = _FolderPicker()
        self.folder_picker.set_value(str(self.app.settings.resolved_download_dir()))
        self.folder_picker.line.textChanged.connect(self._save_folder)
        form.addRow("Save finished files to", self.folder_picker)

        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 20)
        self.concurrency.setValue(self.app.settings.max_concurrent_transfers)
        self.concurrency.valueChanged.connect(self._save_downloads)
        form.addRow("How many downloads at once", self.concurrency)

        self.duplicates = QComboBox()
        for policy, label in (
            (DuplicatePolicy.ASK, "Ask me each time"),
            (DuplicatePolicy.RENAME, "Keep both, add a number"),
            (DuplicatePolicy.SKIP, "Skip it"),
            (DuplicatePolicy.REPLACE, "Replace the old one"),
        ):
            self.duplicates.addItem(label, str(policy))
        index = self.duplicates.findData(str(self.app.settings.duplicate_policy))
        self.duplicates.setCurrentIndex(max(0, index))
        self.duplicates.currentIndexChanged.connect(self._save_downloads)
        form.addRow("If the file is already there", self.duplicates)

        self.confirm = QCheckBox("Ask before starting downloads")
        self.confirm.setChecked(self.app.settings.confirm_before_download)
        self.confirm.toggled.connect(self._save_downloads)
        form.addRow("", self.confirm)

        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_search_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Search")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        box = QGroupBox("How searching behaves")
        form = QFormLayout(box)

        self.search_timeout = QSpinBox()
        self.search_timeout.setRange(5, 300)
        self.search_timeout.setSuffix(" seconds")
        self.search_timeout.setValue(self.app.settings.search_timeout)
        self.search_timeout.valueChanged.connect(self._save_search)
        form.addRow("How long to wait for answers", self.search_timeout)
        form.addRow(
            "", _hint("Soulseek answers arrive gradually. 15 to 30 seconds is usual.")
        )

        self.search_concurrency = QSpinBox()
        self.search_concurrency.setRange(1, 16)
        self.search_concurrency.setValue(self.app.settings.search_concurrency)
        self.search_concurrency.valueChanged.connect(self._save_search)
        form.addRow("How many searches at once", self.search_concurrency)

        self.search_gap = QDoubleSpinBox()
        self.search_gap.setRange(0.0, 60.0)
        self.search_gap.setSingleStep(0.5)
        self.search_gap.setDecimals(1)
        self.search_gap.setSuffix(" seconds")
        self.search_gap.setValue(float(self.app.settings.search_gap_seconds))
        self.search_gap.valueChanged.connect(self._save_search)
        form.addRow("Pause between searches", self.search_gap)

        self.search_rate_limit = QSpinBox()
        self.search_rate_limit.setRange(0, 1000)
        self.search_rate_limit.setValue(int(self.app.settings.search_rate_limit))
        self.search_rate_limit.valueChanged.connect(self._save_search)
        form.addRow("Most searches in a burst", self.search_rate_limit)

        self.search_rate_window = QDoubleSpinBox()
        self.search_rate_window.setRange(1.0, 3600.0)
        self.search_rate_window.setDecimals(0)
        self.search_rate_window.setSingleStep(10.0)
        self.search_rate_window.setSuffix(" seconds")
        self.search_rate_window.setValue(float(self.app.settings.search_rate_window_seconds))
        self.search_rate_window.valueChanged.connect(self._save_search)
        form.addRow("...measured over", self.search_rate_window)
        form.addRow(
            "",
            _hint(
                "Soulseek blocks an account for about half an hour if it sees too "
                "many searches too quickly, and the block looks just like being "
                "disconnected. These three settings keep you under that line. The "
                "defaults, 34 searches every 220 seconds, are what established "
                "Soulseek tools settle on. Raising them risks the block; 0 for the "
                "burst limit switches the ceiling off entirely."
            ),
        )

        self.result_limit = QSpinBox()
        self.result_limit.setRange(1, 5000)
        self.result_limit.setValue(self.app.settings.result_limit)
        self.result_limit.valueChanged.connect(self._save_search)
        form.addRow("Most results to collect per search", self.result_limit)

        self.min_files = QSpinBox()
        self.min_files.setRange(1, 100)
        self.min_files.setValue(self.app.settings.minimum_response_file_count)
        self.min_files.valueChanged.connect(self._save_search)
        form.addRow("Ignore answers with fewer files than", self.min_files)

        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _build_filters_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Default filters")
        heading.setObjectName("Heading")
        layout.addWidget(heading)

        note = QLabel(
            "These are the filters the Results tab starts with. Everything is Any "
            "unless you change it, and Trackfetch never prefers a format, bitrate "
            "or version on your behalf."
        )
        note.setWordWrap(True)
        note.setObjectName("Hint")
        layout.addWidget(note)

        self.filter_bar = FilterBar()
        self.filter_bar.load_filter(ResultFilter.from_defaults(self.app.settings.filters))
        self.filter_bar.filter_changed.connect(self._save_filters)
        layout.addWidget(self.filter_bar)
        layout.addStretch(1)
        return page

    # -- actions -----------------------------------------------------------

    def _set_enabled(self, provider_id: str, enabled: bool) -> None:
        self.app.settings.provider(provider_id).enabled = enabled
        self.app.save_settings()
        self.app.refresh_provider(provider_id)

    def _configure(self, provider_id: str) -> None:
        form = form_for(provider_id)
        if form is None:
            show_message(
                self, "Trackfetch", f"{provider_id} does not need any setup."
            )
            return
        dialog = ProviderConfigDialog(self.app, form, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.app.settings.provider(provider_id).enabled = True
            widgets = self._provider_rows.get(provider_id)
            if widgets:
                widgets[0].setChecked(True)
            self.app.save_settings()
            self._test_one(provider_id)

    def _test_one(self, provider_id: str) -> None:
        provider = self.app.registry.get(provider_id)
        widgets = self._provider_rows.get(provider_id)
        if provider is None or widgets is None:
            return
        widgets[1].setText("Checking...")
        # Soulseek may need its helper installed and started, not merely pinged.
        if provider_id == "slskd":
            work = self._prepare_soulseek
        else:
            work = lambda p=provider: p.check_connection(interactive=True)  # noqa: E731
        self.runner.run(
            work,
            on_result=lambda status, pid=provider_id: self._apply_status(pid, status),
            on_error=lambda exc, pid=provider_id: self._apply_error(pid, exc),
        )

    def _test_all(self) -> None:
        for provider_id in list(self._provider_rows):
            self._test_one(provider_id)

    def _prepare_soulseek(self):
        from trackfetch.services.slskd_setup import ensure_soulseek_ready

        config = self.app.settings.provider("slskd").config
        return ensure_soulseek_ready(
            self.app,
            coerce_text(config.get("soulseek_username")),
            self.app.secrets.get(SecretStore.key_for("slskd", "soulseek_password")),
            manage_helper=bool(config.get("manage_slskd", True)),
        )

    def _apply_status(self, provider_id: str, status) -> None:
        from trackfetch.services.slskd_setup import SetupFailure

        widgets = self._provider_rows.get(provider_id)
        if not widgets:
            return
        if isinstance(status, SetupFailure):
            widgets[1].setText(status.message)
            widgets[1].setStyleSheet("color: #b3261e;")
            widgets[1].setToolTip(status.remedy)
            return
        text = status.detail or status.summary()
        widgets[1].setText(f"{connection_mark(status.state)}  {text}")
        widgets[1].setStyleSheet(f"color: {connection_colour(status.state)};")
        widgets[1].setToolTip(status.remedy)

    def _apply_error(self, provider_id: str, exc: Exception) -> None:
        widgets = self._provider_rows.get(provider_id)
        if widgets:
            widgets[1].setText("Could not check that service.")
        log.debug("connection test error for %s: %r", provider_id, exc)

    # -- saving ------------------------------------------------------------

    def _save_folder(self) -> None:
        value = self.folder_picker.value()
        if value:
            self.app.settings.download_dir = value
            self.app.save_settings()

    def _save_downloads(self) -> None:
        self.app.settings.max_concurrent_transfers = self.concurrency.value()
        self.app.settings.duplicate_policy = DuplicatePolicy(self.duplicates.currentData())
        self.app.settings.confirm_before_download = self.confirm.isChecked()
        self.app.save_settings()

    def _save_search(self) -> None:
        self.app.settings.search_timeout = self.search_timeout.value()
        self.app.settings.search_concurrency = self.search_concurrency.value()
        self.app.settings.result_limit = self.result_limit.value()
        self.app.settings.minimum_response_file_count = self.min_files.value()
        self.app.settings.search_gap_seconds = float(self.search_gap.value())
        self.app.settings.search_rate_limit = int(self.search_rate_limit.value())
        self.app.settings.search_rate_window_seconds = float(self.search_rate_window.value())
        self.app.save_settings()

    def _save_filters(self, value: ResultFilter) -> None:
        self.app.settings.filters = value.to_defaults()
        self.app.save_settings()


def coerce_text(value) -> str:
    return "" if value is None else str(value)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Hint")
    label.setWordWrap(True)
    return label
