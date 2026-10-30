"""Logging and the diagnostic report."""

from trackfetch.diagnostics.logging_setup import RecentLogBuffer, setup_logging
from trackfetch.diagnostics.collect import build_report

__all__ = ["RecentLogBuffer", "setup_logging", "build_report"]
