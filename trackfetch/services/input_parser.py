"""Turn whatever the user pastes or imports into a list of batch items.

Accepts a pasted block of lines, a TXT file, a CSV file, direct links, magnet
links, torrent files and NZB files, in any mixture, and works out what each line
is. A line that is not recognisably a link is treated as a search query, which
is exactly what a Soulseek user wants: any text at all is a valid search, and
nothing here assumes the text describes music.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path
from typing import Iterable, Sequence

from trackfetch.models.common import BatchItem, InputKind

log = logging.getLogger(__name__)

MAX_ITEMS = 10000
MAX_LINE_LENGTH = 2000

_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", re.IGNORECASE)
_BARE_DOMAIN = re.compile(
    r"^(?:www\.)?[a-z0-9][a-z0-9\-]{0,62}(?:\.[a-z0-9][a-z0-9\-]{0,62})+"
    r"(?::\d{1,5})?(?:/\S*)?$",
    re.IGNORECASE,
)
_COMMENT = re.compile(r"^\s*(#|//)")

#: Column names a person is likely to use in a spreadsheet, in priority order.
_PREFERRED_COLUMNS = (
    "query", "search", "search query", "term", "title", "track", "name",
    "item", "url", "link", "magnet",
)


def classify(text: str) -> InputKind:
    """Decide what a single line is."""
    value = text.strip()
    if not value:
        return InputKind.QUERY
    lowered = value.lower()

    if lowered.startswith("magnet:"):
        return InputKind.MAGNET
    if lowered.startswith(("http://", "https://", "ftp://", "ftps://")):
        return InputKind.URL

    looks_like_path = (
        "/" in value or "\\" in value or re.match(r"^[A-Za-z]:", value) is not None
    )
    if lowered.endswith(".torrent") and looks_like_path:
        return InputKind.TORRENT_FILE
    if lowered.endswith(".nzb") and looks_like_path:
        return InputKind.NZB_FILE

    if _URL.match(value):
        return InputKind.URL
    # A bare domain with a path is almost certainly a link, but a plain domain
    # like "aphex twin.com" is not, so a space anywhere rules it out.
    if " " not in value and _BARE_DOMAIN.match(value):
        return InputKind.URL

    if looks_like_path and Path(value).expanduser().exists():
        suffix = Path(value).suffix.lower()
        if suffix == ".torrent":
            return InputKind.TORRENT_FILE
        if suffix == ".nzb":
            return InputKind.NZB_FILE
        return InputKind.LOCAL_PATH

    return InputKind.QUERY


def parse_lines(
    text: str, source: str = "pasted", limit: int = MAX_ITEMS
) -> list[BatchItem]:
    """Parse a pasted block. Blank lines and # comments are ignored."""
    items: list[BatchItem] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip().strip("﻿")
        if not line or _COMMENT.match(line):
            continue
        if len(line) > MAX_LINE_LENGTH:
            line = line[:MAX_LINE_LENGTH]
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            BatchItem(
                raw_text=line,
                kind=classify(line),
                source=source,
                position=len(items),
            )
        )
        if len(items) >= limit:
            log.warning("input truncated at %s items", limit)
            break
    return items


def parse_txt_file(path: Path | str, limit: int = MAX_ITEMS) -> list[BatchItem]:
    file_path = Path(path).expanduser()
    text = _read_text(file_path)
    return parse_lines(text, source=file_path.name, limit=limit)


def parse_csv_text(
    text: str, source: str = "pasted.csv", limit: int = MAX_ITEMS
) -> list[BatchItem]:
    """Parse CSV content, picking the most likely column automatically."""
    if not text.strip():
        return []
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader if any((cell or "").strip() for cell in row)]
    if not rows:
        return []

    headers: list[str] = []
    if _looks_like_header(rows[0]):
        headers = [(cell or "").strip() for cell in rows[0]]
        rows = rows[1:]

    column = _choose_column(headers)
    items: list[BatchItem] = []
    seen: set[str] = set()
    for row in rows:
        if column >= len(row):
            value = next((c for c in row if (c or "").strip()), "")
        else:
            value = row[column]
        value = (value or "").strip().strip("﻿")
        if not value or _COMMENT.match(value):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        extra: dict[str, str] = {}
        for index, cell in enumerate(row):
            if index == column:
                continue
            name = headers[index] if index < len(headers) and headers[index] else f"column{index+1}"
            if (cell or "").strip():
                extra[name] = cell.strip()
        items.append(
            BatchItem(
                raw_text=value[:MAX_LINE_LENGTH],
                kind=classify(value),
                extra=extra,
                source=source,
                position=len(items),
            )
        )
        if len(items) >= limit:
            log.warning("input truncated at %s items", limit)
            break
    return items


def parse_csv_file(path: Path | str, limit: int = MAX_ITEMS) -> list[BatchItem]:
    file_path = Path(path).expanduser()
    return parse_csv_text(_read_text(file_path), source=file_path.name, limit=limit)


def parse_file(path: Path | str, limit: int = MAX_ITEMS) -> list[BatchItem]:
    """Import any supported file, choosing the right reader by extension."""
    file_path = Path(path).expanduser()
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        return parse_csv_file(file_path, limit)
    if suffix in {".torrent", ".nzb"}:
        kind = InputKind.TORRENT_FILE if suffix == ".torrent" else InputKind.NZB_FILE
        return [
            BatchItem(
                raw_text=str(file_path),
                kind=kind,
                source=file_path.name,
                position=0,
            )
        ]
    return parse_txt_file(file_path, limit)


def parse_files(paths: Iterable[Path | str], limit: int = MAX_ITEMS) -> list[BatchItem]:
    items: list[BatchItem] = []
    for path in paths:
        for item in parse_file(path, limit):
            item.position = len(items)
            items.append(item)
            if len(items) >= limit:
                return items
    return items


def merge(*groups: Sequence[BatchItem], limit: int = MAX_ITEMS) -> list[BatchItem]:
    """Combine several sets of items, dropping repeats and renumbering."""
    out: list[BatchItem] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            key = item.raw_text.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            item.position = len(out)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


def summarise(items: Sequence[BatchItem]) -> dict[str, int]:
    """Counts by kind, for the "what did I just import" line in the interface."""
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.kind)] = counts.get(str(item.kind), 0) + 1
    counts["TOTAL"] = len(items)
    return counts


def _looks_like_header(row: Sequence[str]) -> bool:
    """Treat the first row as a header only when a cell is plainly a column name.

    Guessing wrongly is costly in both directions: a missed header adds a junk
    search, a false header silently drops a real one. An exact match against the
    names people actually use is the reliable signal.
    """
    cells = [(cell or "").strip().lower() for cell in row]
    return any(cell in _PREFERRED_COLUMNS for cell in cells if cell)


def _choose_column(headers: list[str]) -> int:
    if not headers:
        return 0
    lowered = [h.strip().lower() for h in headers]
    for preferred in _PREFERRED_COLUMNS:
        if preferred in lowered:
            return lowered.index(preferred)
    for index, name in enumerate(lowered):
        if any(preferred in name for preferred in _PREFERRED_COLUMNS):
            return index
    return 0


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")  # pragma: no cover - last resort
