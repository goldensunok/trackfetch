"""Background work for the interface.

Nothing slow ever runs on the thread that draws the window. Searches, connection
tests and downloads all run here and report back through Qt signals, which is
what keeps the application responsive instead of appearing frozen.
"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    finished = Signal()
    result = Signal(object)
    error = Signal(object)
    progress = Signal(object)
    message = Signal(str)


class Worker(QRunnable):
    """Runs one callable on a background thread and reports the outcome.

    The callable may accept a ``progress`` keyword, which it can call with any
    object to update the interface while it works.
    """

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self._pass_progress = kwargs.pop("_pass_progress", False)
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # pragma: no cover - exercised through the interface
        try:
            if self._pass_progress:
                self.kwargs["progress"] = self.signals.progress.emit
            outcome = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            log.debug("worker failed: %s", traceback.format_exc())
            self.signals.error.emit(exc)
        else:
            self.signals.result.emit(outcome)
        finally:
            self.signals.finished.emit()


class BackgroundRunner:
    """A small facade over the thread pool, so windows do not manage threads."""

    def __init__(self, max_threads: int = 8):
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(max_threads)
        self._cancel_events: dict[str, threading.Event] = {}

    def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_progress: Callable[[Any], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        pass_progress: bool = False,
        **kwargs: Any,
    ) -> Worker:
        worker = Worker(fn, *args, _pass_progress=pass_progress, **kwargs)
        if on_result:
            worker.signals.result.connect(on_result)
        if on_error:
            worker.signals.error.connect(on_error)
        if on_progress:
            worker.signals.progress.connect(on_progress)
        if on_finished:
            worker.signals.finished.connect(on_finished)
        self.pool.start(worker)
        return worker

    def wait(self, msecs: int = 5000) -> bool:
        return self.pool.waitForDone(msecs)
