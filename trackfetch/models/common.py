"""Core value types used across providers, services, storage and the GUI.

Nothing in this module may import a provider, the GUI or the database. It is the
shared vocabulary, deliberately dependency free so it can be unit tested on its
own and reused everywhere else.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


def new_id() -> str:
    """Return a fresh opaque identifier for internal records."""
    return uuid.uuid4().hex


class Capability(StrEnum):
    """What a provider is actually able to do.

    Providers advertise these and the interface adapts. There is deliberately no
    attempt to make every provider look identical, because they genuinely are
    not: Soulseek searches a network, qBittorrent accepts a job, SFTP browses a
    tree.
    """

    SEARCH = "SEARCH"
    BROWSE = "BROWSE"
    RESOLVE = "RESOLVE"
    ADD_JOB = "ADD_JOB"
    DOWNLOAD = "DOWNLOAD"
    QUEUE = "QUEUE"
    PROGRESS = "PROGRESS"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    CANCEL = "CANCEL"
    AUTH = "AUTH"
    CONNECTION_TEST = "CONNECTION_TEST"


class ConnectionState(StrEnum):
    """Status of a provider's link to whatever it talks to."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    DISABLED = "DISABLED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


@dataclass(slots=True)
class ConnectionStatus:
    """A provider status expressed in language an ordinary person can read.

    ``detail`` is a short plain sentence. ``remedy`` is what the user should do
    about it, also in plain language. ``technical`` is kept for the log and the
    diagnostics report and is never shown as the primary message.
    """

    provider_id: str
    state: ConnectionState = ConnectionState.NOT_CONFIGURED
    detail: str = ""
    remedy: str = ""
    technical: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    def summary(self) -> str:
        """One-line summary suitable for the connections panel."""
        labels = {
            ConnectionState.CONNECTED: "Connected",
            ConnectionState.DISCONNECTED: "Disconnected",
            ConnectionState.NOT_CONFIGURED: "Not configured",
            ConnectionState.DISABLED: "Disabled",
            ConnectionState.CONNECTING: "Checking",
            ConnectionState.ERROR: "Problem",
        }
        return labels[self.state]


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Static description of a provider, used to build the interface."""

    provider_id: str
    display_name: str
    capabilities: frozenset[Capability]
    #: Short plain-English description of the workflow this provider offers.
    workflow: str = ""
    #: Name of the external application this provider talks to, if any.
    requires_app: str | None = None

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities


class InputKind(StrEnum):
    """How a line of user input should be interpreted."""

    QUERY = "QUERY"
    URL = "URL"
    MAGNET = "MAGNET"
    TORRENT_FILE = "TORRENT_FILE"
    NZB_FILE = "NZB_FILE"
    LOCAL_PATH = "LOCAL_PATH"


@dataclass(slots=True)
class BatchItem:
    """One line of what the user pasted or imported."""

    id: str = field(default_factory=new_id)
    batch_id: str = ""
    raw_text: str = ""
    kind: InputKind = InputKind.QUERY
    #: Optional extra columns picked up from a CSV import.
    extra: dict[str, str] = field(default_factory=dict)
    #: Where the item came from, for the user's benefit ("pasted", "list.txt").
    source: str = "pasted"
    position: int = 0
    skipped: bool = False


@dataclass(slots=True)
class SearchQuery:
    """A search issued to a provider on behalf of a batch item."""

    id: str = field(default_factory=new_id)
    batch_item_id: str = ""
    provider_id: str = ""
    text: str = ""
    remote_id: str | None = None
    state: str = "pending"
    error: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    result_count: int = 0


@dataclass(slots=True)
class SearchCandidate:
    """One selectable result returned by a search-capable provider.

    Every field beyond ``filename`` is optional because providers genuinely
    differ in what they report. The interface shows what exists and leaves the
    rest blank rather than inventing values.
    """

    id: str = field(default_factory=new_id)
    query_id: str = ""
    provider_id: str = ""

    filename: str = ""
    directory: str = ""
    extension: str = ""
    size: int | None = None

    bitrate: int | None = None
    is_vbr: bool | None = None
    duration: int | None = None
    sample_rate: int | None = None
    bit_depth: int | None = None
    codec: str = ""

    remote_user: str = ""
    queue_length: int | None = None
    has_free_slot: bool | None = None
    upload_speed: int | None = None
    locked: bool = False

    relevance: float = 0.0
    selected: bool = False

    #: Everything the provider needs handed back to start the transfer.
    payload: dict[str, Any] = field(default_factory=dict)
    #: The untouched provider response, kept for diagnostics.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def display_path(self) -> str:
        if self.directory and self.filename:
            sep = "\\" if "\\" in self.directory else "/"
            trimmed = self.directory.rstrip("/\\")
            return trimmed + sep + self.filename
        return self.filename or self.directory

    @property
    def quality_summary(self) -> str:
        """Short human summary of the audio/technical quality, if known."""
        bits: list[str] = []
        if self.bitrate:
            bits.append(f"{self.bitrate} kbps{' VBR' if self.is_vbr else ''}")
        if self.sample_rate:
            khz = self.sample_rate / 1000.0
            bits.append(f"{khz:g} kHz")
        if self.bit_depth:
            bits.append(f"{self.bit_depth}-bit")
        return ", ".join(bits)


class TransferStatus(StrEnum):
    """Unified status vocabulary across every provider."""

    WAITING = "Waiting"
    QUEUED = "Queued"
    DOWNLOADING = "Downloading"
    PAUSED = "Paused"
    COMPLETE = "Complete"
    FAILED = "Failed"
    CANCELLED = "Cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TransferStatus.COMPLETE,
            TransferStatus.FAILED,
            TransferStatus.CANCELLED,
        )

    @property
    def is_active(self) -> bool:
        return self in (
            TransferStatus.WAITING,
            TransferStatus.QUEUED,
            TransferStatus.DOWNLOADING,
        )


@dataclass(slots=True)
class TransferJob:
    """One row of the unified download queue."""

    id: str = field(default_factory=new_id)
    provider_id: str = ""
    label: str = ""
    status: TransferStatus = TransferStatus.WAITING
    total_bytes: int | None = None
    downloaded_bytes: int = 0
    speed_bps: float | None = None
    eta_seconds: int | None = None
    destination: str = ""
    error: str = ""
    remedy: str = ""
    #: Identifier the provider itself uses, so we can poll it back.
    remote_id: str = ""
    #: Everything the provider needs to start, resume or retry this job.
    payload: dict[str, Any] = field(default_factory=dict)
    batch_item_id: str = ""
    candidate_id: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    attempts: int = 0

    @property
    def percent(self) -> float:
        if self.status is TransferStatus.COMPLETE:
            return 100.0
        if not self.total_bytes:
            return 0.0
        return max(0.0, min(100.0, self.downloaded_bytes * 100.0 / self.total_bytes))

    def touch(self) -> None:
        self.updated_at = time.time()


class DuplicatePolicy(StrEnum):
    """What to do when the destination file already exists."""

    ASK = "ASK"
    SKIP = "SKIP"
    RENAME = "RENAME"
    REPLACE = "REPLACE"


# ---------------------------------------------------------------------------
# Small formatting helpers, used by the GUI and by log messages.
# ---------------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(value: int | float | None) -> str:
    """Format a byte count the way a file manager would."""
    if value is None:
        return ""
    amount = float(value)
    if amount < 0:
        return ""
    for unit in _UNITS:
        if amount < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} PB"


def human_speed(value: float | None) -> str:
    if not value:
        return ""
    return f"{human_bytes(value)}/s"


def human_duration(seconds: int | float | None) -> str:
    """Format a number of seconds as h:mm:ss or m:ss."""
    if seconds is None:
        return ""
    total = int(seconds)
    if total < 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion for values coming from external APIs."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return int(float(text))
        except ValueError:
            return None
    return None


def coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "1", "on"}:
            return True
        if low in {"false", "no", "0", "off"}:
            return False
    return None


def coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def as_mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` if it is a mapping, otherwise an empty mapping.

    External APIs occasionally return ``null`` or a bare string where an object
    is documented. Everything that parses a remote payload goes through helpers
    like this one so a surprising response produces an empty result rather than
    an exception in the middle of the interface.
    """
    if isinstance(value, Mapping):
        return value
    return {}


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []
