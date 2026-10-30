"""Build the text produced by "Copy Diagnostic Information".

The point of this is that when something breaks, one button gives a person
everything a helper needs and nothing that would embarrass them.

Included: version, operating system, which services are switched on, what state
each is in, recent errors, and non-secret configuration such as addresses and
port numbers.

Never included: passwords, API keys, tokens, or the contents of the credential
store. Addresses are printed, but any user information inside a URL is stripped.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from typing import Any

from trackfetch import __version__
from trackfetch.config.paths import app_paths
from trackfetch.config.settings import Settings
from trackfetch.diagnostics.logging_setup import get_buffer, redact
from trackfetch.providers._http import redact_url

SECRET_KEY_HINTS = (
    "password", "pass", "secret", "token", "apikey", "api_key", "key", "auth",
    "credential", "cookie",
)


def _clean_config(config: dict[str, Any]) -> dict[str, Any]:
    """Copy a provider config with anything sensitive removed."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        lowered = key.lower()
        if any(hint in lowered for hint in SECRET_KEY_HINTS):
            out[key] = "(hidden)" if value else "(not set)"
            continue
        if isinstance(value, str) and "://" in value:
            out[key] = redact_url(value)
        elif isinstance(value, list):
            out[key] = f"{len(value)} entries"
        elif isinstance(value, dict):
            out[key] = _clean_config(value)
        else:
            out[key] = value
    return out


def build_report(
    settings: Settings,
    registry: Any | None = None,
    repos: Any | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Return the full diagnostic text, ready to paste into a message."""
    lines: list[str] = []

    def section(title: str) -> None:
        lines.append("")
        lines.append(title)
        lines.append("-" * len(title))

    lines.append("TRACKFETCH DIAGNOSTIC INFORMATION")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("This report contains no passwords, keys or tokens.")

    section("Application")
    lines.append(f"Trackfetch version: {__version__}")
    lines.append(f"Python: {sys.version.split()[0]}")
    lines.append(f"Frozen build: {getattr(sys, 'frozen', False)}")

    section("Computer")
    lines.append(f"System: {platform.system()} {platform.release()}")
    lines.append(f"Version: {platform.version()}")
    lines.append(f"Machine: {platform.machine()}")

    section("Folders")
    paths = app_paths()
    lines.append(f"Settings folder: {paths.config_dir}")
    lines.append(f"Data folder: {paths.data_dir}")
    lines.append(f"Log folder: {paths.log_dir}")
    lines.append(f"Download folder: {settings.resolved_download_dir()}")

    section("General settings")
    lines.append(f"First run completed: {settings.first_run_complete}")
    lines.append(f"Simultaneous transfers: {settings.max_concurrent_transfers}")
    lines.append(f"Duplicate handling: {settings.duplicate_policy}")
    lines.append(f"Search timeout: {settings.search_timeout}s")
    lines.append(f"Search concurrency: {settings.search_concurrency}")
    lines.append(f"Result limit: {settings.result_limit}")
    filters = settings.filters
    lines.append(
        "Saved filters: "
        + ("none (everything set to Any)" if filters.is_empty() else json.dumps(
            {
                "extensions": filters.extensions,
                "min_bitrate": filters.min_bitrate,
                "exact_bitrate": filters.exact_bitrate,
                "min_size": filters.min_size,
                "max_size": filters.max_size,
                "min_duration": filters.min_duration,
                "max_duration": filters.max_duration,
                "codecs": filters.codecs,
                "sample_rates": filters.sample_rates,
                "bit_depths": filters.bit_depths,
            }
        ))
    )

    section("Services")
    for provider_id, provider_settings in settings.providers.items():
        state = "on" if provider_settings.enabled else "off"
        lines.append(f"[{provider_id}] enabled: {state}")
        cleaned = _clean_config(provider_settings.config)
        for key, value in sorted(cleaned.items()):
            lines.append(f"    {key}: {value}")

    if registry is not None:
        section("Service status")
        try:
            for provider in registry.all():
                data = provider.diagnostics()
                lines.append(
                    f"{data.get('name', provider.provider_id)}: "
                    f"{data.get('status', 'unknown')} "
                    f"({'configured' if data.get('configured') else 'not configured'}, "
                    f"{'on' if data.get('enabled') else 'off'})"
                )
                detail = data.get("status_detail")
                if detail:
                    lines.append(f"    {detail}")
                for key in ("slskd", "helper", "client"):
                    if key in data and data[key]:
                        lines.append(f"    {key}: {_clean_config(dict(data[key]))}")
        except Exception as exc:  # pragma: no cover - defensive
            lines.append(f"(could not read service status: {exc})")

        failed = registry.failed()
        if failed:
            lines.append("")
            lines.append("Services that could not load:")
            for provider_id, reason in failed.items():
                lines.append(f"    {provider_id}: {reason}")

    if repos is not None:
        section("Recent activity")
        try:
            for row in repos.events.recent(limit=40):
                stamp = time.strftime("%H:%M:%S", time.localtime(row["ts"]))
                lines.append(f"{stamp} [{row['level']}] {row['provider_id']} {row['message']}")
        except Exception as exc:  # pragma: no cover - defensive
            lines.append(f"(could not read recent activity: {exc})")

        section("Queue summary")
        try:
            jobs = repos.transfers.all(limit=2000)
            counts: dict[str, int] = {}
            for job in jobs:
                counts[str(job.status)] = counts.get(str(job.status), 0) + 1
            for status, count in sorted(counts.items()):
                lines.append(f"{status}: {count}")
            if not counts:
                lines.append("The queue is empty.")
        except Exception as exc:  # pragma: no cover - defensive
            lines.append(f"(could not read the queue: {exc})")

    section("Recent warnings and errors")
    errors = get_buffer().recent_errors(limit=40)
    if errors:
        lines.extend(errors)
    else:
        lines.append("None recorded.")

    if extra:
        section("Extra")
        for key, value in extra.items():
            lines.append(f"{key}: {value}")

    return redact("\n".join(lines))
