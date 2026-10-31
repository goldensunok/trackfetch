"""Logging setup.

Two destinations: a rotating file the user never has to look at, and a small
in-memory buffer of recent messages that the diagnostic report can include.

A redaction filter runs over every record. Passwords, tokens and API keys must
never reach the log, so anything that looks like one is replaced before the
record is written, in addition to the code simply not logging secrets.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import threading
from collections import deque
from pathlib import Path

from trackfetch.config.paths import app_paths

log = logging.getLogger(__name__)

_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(password\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
    re.compile(r"(?i)(token\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)(x-api-key:\s*)(\S+)"),
    re.compile(r"(?i)(secret\"?\s*[:=]\s*\"?)([^\s\",}]+)"),
    re.compile(r"(?i)(://[^:/@\s]+:)([^@/\s]+)(@)"),
]

MAX_RECENT = 300


def redact(text: str) -> str:
    """Replace anything that looks like a credential with asterisks."""
    result = text
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups >= 3:
            result = pattern.sub(r"\1***\3", result)
        else:
            result = pattern.sub(r"\1***", result)
    return result


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


class RecentLogBuffer(logging.Handler):
    """Keeps the last few hundred log lines in memory for the report."""

    def __init__(self, capacity: int = MAX_RECENT):
        super().__init__()
        self._records: deque[str] = deque(maxlen=capacity)
        self._errors: deque[str] = deque(maxlen=60)
        self._lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover - defensive
            return
        line = redact(line)
        with self._lock:
            self._records.append(line)
            if record.levelno >= logging.WARNING:
                self._errors.append(line)

    def recent(self, limit: int = 100) -> list[str]:
        with self._lock:
            return list(self._records)[-limit:]

    def recent_errors(self, limit: int = 30) -> list[str]:
        with self._lock:
            return list(self._errors)[-limit:]


_buffer: RecentLogBuffer | None = None


def get_buffer() -> RecentLogBuffer:
    global _buffer
    if _buffer is None:
        _buffer = RecentLogBuffer()
    return _buffer


def setup_logging(level: int = logging.INFO, log_file: Path | None = None) -> RecentLogBuffer:
    """Configure logging once, at start-up."""
    target = log_file or app_paths().log_file
    target.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    redaction = RedactionFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        target, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(redaction)
    root.addHandler(console)

    buffer = get_buffer()
    buffer.setLevel(logging.DEBUG)
    buffer.setFormatter(formatter)
    buffer.addFilter(redaction)
    root.addHandler(buffer)

    # These libraries are chatty at debug level and their output is not useful.
    for noisy in ("httpx", "httpcore", "paramiko", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    enable_crash_report()
    return buffer


#: Kept open for the life of the process. The crash handler writes straight to
#: the file descriptor, so it must not be closed or garbage collected.
_crash_file = None


def crash_report_path() -> Path:
    return app_paths().log_dir / "crash.txt"


def enable_crash_report() -> Path | None:
    """Make a hard crash leave a note behind.

    A segmentation fault or a fault inside Qt kills the process outright:
    nothing reaches the log, and the window simply disappears with no message.
    Python can still write the stack of every thread as it goes down, which
    turns "it just closed" into something a person can send on and someone can
    read. Appended to, never truncated, so an overnight run keeps every
    occurrence.
    """
    global _crash_file
    if _crash_file is not None:
        return crash_report_path()
    try:
        import faulthandler
        import time as _time

        path = crash_report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        handle.write(f"\n=== Trackfetch started {_time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        faulthandler.enable(file=handle, all_threads=True)
        _crash_file = handle
        return path
    except Exception:  # pragma: no cover - never let this stop the program
        log.debug("could not enable the crash report", exc_info=True)
        return None
