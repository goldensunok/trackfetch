"""Safe destination paths and duplicate handling.

Filenames arriving from a Soulseek peer, an HTTP header or a torrent are
untrusted input. Everything that turns one into a path on disk goes through
here, so a remote name can never escape the download folder, overwrite
something silently, or produce a name the operating system refuses.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath

from trackfetch.models.common import DuplicatePolicy

#: Characters no mainstream filesystem accepts, plus control characters.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TRAILING = re.compile(r"[ .]+$")

#: Names Windows reserves whatever the extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_NAME_LENGTH = 180


class DuplicateAction(StrEnum):
    WRITE = "WRITE"
    SKIP = "SKIP"
    RENAME = "RENAME"
    REPLACE = "REPLACE"
    ASK = "ASK"


@dataclass(slots=True)
class Destination:
    """The decided destination for one transfer."""

    path: Path
    action: DuplicateAction
    existing: Path | None = None

    @property
    def part_path(self) -> Path:
        return self.path.with_name(self.path.name + ".part")


def sanitise_filename(name: str, fallback: str = "download") -> str:
    """Turn an arbitrary remote name into a safe single filename component."""
    text = (name or "").strip()
    # A remote name may be a whole path in either style. Keep only the last part.
    if text:
        text = PureWindowsPath(text.replace("/", "\\")).name or PurePosixPath(text).name
    text = unicodedata.normalize("NFC", text)
    text = _ILLEGAL.sub("_", text)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    text = _TRAILING.sub("", text)
    if not text or text in {".", ".."}:
        return fallback

    stem, dot, suffix = text.rpartition(".")
    if dot and stem and stem.upper() in _RESERVED:
        stem = f"_{stem}"
        text = f"{stem}.{suffix}"
    elif text.upper() in _RESERVED:
        text = f"_{text}"

    if len(text) > MAX_NAME_LENGTH:
        stem, dot, suffix = text.rpartition(".")
        if dot and len(suffix) <= 8:
            keep = MAX_NAME_LENGTH - len(suffix) - 1
            text = f"{stem[:keep]}.{suffix}"
        else:
            text = text[:MAX_NAME_LENGTH]
    return text or fallback


def sanitise_relative_dir(path: str) -> str:
    """Keep a remote folder structure but strip anything that could escape."""
    if not path:
        return ""
    normalised = path.replace("\\", "/")
    parts: list[str] = []
    for part in normalised.split("/"):
        part = part.strip()
        if not part or part in {".", ".."}:
            continue
        if re.match(r"^[A-Za-z]:$", part):  # a drive letter
            continue
        parts.append(sanitise_filename(part, fallback="folder"))
    return "/".join(parts)


def safe_join(base_dir: Path, *parts: str) -> Path:
    """Join under ``base_dir`` and refuse to leave it."""
    base = Path(base_dir).expanduser()
    candidate = base
    for part in parts:
        for piece in sanitise_relative_dir(part).split("/"):
            if piece:
                candidate = candidate / piece
    base_resolved = _resolve_for_comparison(base)
    candidate_resolved = _resolve_for_comparison(candidate)
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes the download folder: {candidate}") from exc
    return candidate


def _resolve_for_comparison(path: Path) -> Path:
    """Resolve without requiring the path to exist."""
    return Path(os.path.normpath(str(path.absolute())))


def unique_path(path: Path) -> Path:
    """Return a path that does not exist, adding " (2)", " (3)" and so on."""
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    index = 2
    while True:
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1
        if index > 9999:  # pragma: no cover - absurd case
            return parent / f"{stem}-{os.getpid()}{suffix}"


def decide_destination(
    base_dir: Path,
    filename: str,
    policy: DuplicatePolicy = DuplicatePolicy.ASK,
    subfolder: str = "",
) -> Destination:
    """Work out where a file should go and what to do if it is already there."""
    safe_name = sanitise_filename(filename)
    folder = safe_join(base_dir, subfolder) if subfolder else Path(base_dir).expanduser()
    target = folder / safe_name

    if not target.exists():
        return Destination(target, DuplicateAction.WRITE)

    if policy is DuplicatePolicy.SKIP:
        return Destination(target, DuplicateAction.SKIP, existing=target)
    if policy is DuplicatePolicy.REPLACE:
        return Destination(target, DuplicateAction.REPLACE, existing=target)
    if policy is DuplicatePolicy.RENAME:
        return Destination(unique_path(target), DuplicateAction.RENAME, existing=target)
    return Destination(target, DuplicateAction.ASK, existing=target)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def fingerprint(provider_id: str, identity: str, size: int | None = None) -> str:
    """A stable key for "this exact item from this exact provider".

    Used to avoid downloading something already recorded as complete.
    """
    raw = f"{provider_id}|{identity.strip().lower()}|{size or ''}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def verify_checksum(path: Path, expected: str) -> bool:
    """Check a file against a supplied checksum.

    ``expected`` may be "sha256:abcd...", "md5:abcd..." or a bare hex digest,
    in which case the algorithm is guessed from its length.
    """
    if not expected:
        return True
    text = expected.strip().lower()
    algorithm = ""
    if ":" in text:
        algorithm, _, digest = text.partition(":")
    else:
        digest = text
        algorithm = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}.get(len(digest), "")
    if not algorithm or algorithm not in hashlib.algorithms_available:
        return True  # nothing usable was supplied, so nothing to fail
    hasher = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == digest
