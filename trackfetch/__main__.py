"""Starting Trackfetch.

This is what the desktop shortcut runs. It sets up logging, builds the
application core, shows the setup guide the first time, then opens the main
window. Anything that goes wrong before the window appears is shown as a plain
message rather than a console traceback, because on Windows there is no console
to read.
"""

from __future__ import annotations

import logging
import sys
import traceback

from trackfetch import __app_name__, __version__

log = logging.getLogger(__name__)


def _fatal(message: str, detail: str = "") -> None:
    """Report a start-up failure in a way a person can actually see."""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        # The reference has to be kept while the box is on screen.
        _qt = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
        box = QMessageBox()
        box.setWindowTitle(__app_name__)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText(message)
        box.setInformativeText(
            "Trackfetch could not start. If this keeps happening, send this "
            "message on along with the details below."
        )
        if detail:
            box.setDetailedText(detail)
        box.exec()
    except Exception:  # pragma: no cover - last resort
        print(f"{message}\n{detail}", file=sys.stderr)


def main() -> int:
    from trackfetch.diagnostics.logging_setup import setup_logging

    setup_logging()
    log.info("Starting %s %s", __app_name__, __version__)

    try:
        from PySide6.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover - broken install
        _fatal(
            "Trackfetch is missing part of itself.",
            f"{exc}\n\n{traceback.format_exc()}",
        )
        return 1

    QApplication.setApplicationName(__app_name__)
    QApplication.setApplicationVersion(__version__)
    QApplication.setOrganizationName(__app_name__)
    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(True)
    try:
        from PySide6.QtGui import QIcon

        from trackfetch.gui.style import APP_STYLESHEET
        from trackfetch.config.paths import resource_path

        # Applied at application level so every window and dialog matches.
        qt_app.setStyleSheet(APP_STYLESHEET)
        icon_file = resource_path("trackfetch.ico")
        if icon_file.exists():
            qt_app.setWindowIcon(QIcon(str(icon_file)))
    except Exception:  # pragma: no cover - cosmetic only
        log.debug("could not apply the appearance settings", exc_info=True)

    try:
        from trackfetch.app import TrackfetchApp

        app = TrackfetchApp(configure_logging=False)
        app.start()
    except Exception as exc:
        log.exception("could not start the application core")
        _fatal(
            "Trackfetch could not open its settings or its list of downloads.",
            f"{exc}\n\n{traceback.format_exc()}",
        )
        return 1

    def handle_uncaught(exc_type, exc_value, exc_tb):
        """Never let an unexpected error close the window silently."""
        if issubclass(exc_type, KeyboardInterrupt):  # pragma: no cover
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.error(
            "unexpected error", exc_info=(exc_type, exc_value, exc_tb)
        )
        try:
            from trackfetch.gui.dialogs import show_message

            show_message(
                None,
                __app_name__,
                "Something unexpected happened inside Trackfetch.",
                "The rest of the program is still running. If this keeps "
                "happening, use Help then Copy Diagnostic Information.",
                "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            )
        except Exception:  # pragma: no cover - defensive
            pass

    sys.excepthook = handle_uncaught

    try:
        from trackfetch.gui.main_window import MainWindow
        from trackfetch.gui.wizard import FirstRunWizard

        window = MainWindow(app)

        if not app.settings.first_run_complete:
            wizard = FirstRunWizard(app, window)
            wizard.exec()
            app.settings.first_run_complete = True
            app.save_settings()
            window.refresh_connections()

        window.show()
        code = qt_app.exec()
    except Exception as exc:
        log.exception("the interface failed to start")
        _fatal(
            "Trackfetch could not open its main window.",
            f"{exc}\n\n{traceback.format_exc()}",
        )
        code = 1
    finally:
        try:
            app.shutdown()
        except Exception:  # pragma: no cover - defensive
            log.debug("shutdown problem", exc_info=True)

    return int(code)


if __name__ == "__main__":
    sys.exit(main())
