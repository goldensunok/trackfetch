"""User-controlled filtering and sorting of search results.

Every setting here starts at "Any". Nothing is preferred by default: not a
format, not a bitrate, not a release, not a version. The filters do exactly what
the person sets and nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Iterable, Sequence

from trackfetch.config.settings import FilterDefaults
from trackfetch.models.common import SearchCandidate

#: Offered in the interface as a convenience. Choosing none of them means Any.
COMMON_EXTENSIONS = (
    "mp3", "flac", "wav", "aiff", "aif", "m4a", "aac", "ogg", "opus", "wma",
    "alac", "ape", "wv", "mp4", "mkv", "avi", "zip", "rar", "7z", "pdf", "iso",
)


class SortField(StrEnum):
    RELEVANCE = "Relevance"
    FILENAME = "Name"
    SIZE = "Size"
    BITRATE = "Bitrate"
    DURATION = "Duration"
    SAMPLE_RATE = "Sample rate"
    BIT_DEPTH = "Bit depth"
    USER = "User"
    QUEUE = "Queue"
    SPEED = "Speed"


@dataclass(slots=True)
class ResultFilter:
    """One set of filter settings. Empty or None always means "Any"."""

    extensions: list[str] = field(default_factory=list)
    min_bitrate: int | None = None
    exact_bitrate: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    min_duration: int | None = None
    max_duration: int | None = None
    codecs: list[str] = field(default_factory=list)
    sample_rates: list[int] = field(default_factory=list)
    bit_depths: list[int] = field(default_factory=list)

    text_contains: str = ""
    min_relevance: float = 0.0
    only_free_slots: bool = False
    hide_locked: bool = False
    users: list[str] = field(default_factory=list)

    #: When a candidate simply does not report a value, should it be kept?
    #: Keeping it is the default, because Soulseek peers often report nothing
    #: and silently discarding those results would hide real files.
    keep_unknown: bool = True

    # -- construction ------------------------------------------------------

    @classmethod
    def from_defaults(cls, defaults: FilterDefaults) -> "ResultFilter":
        return cls(
            extensions=list(defaults.extensions),
            min_bitrate=defaults.min_bitrate,
            exact_bitrate=defaults.exact_bitrate,
            min_size=defaults.min_size,
            max_size=defaults.max_size,
            min_duration=defaults.min_duration,
            max_duration=defaults.max_duration,
            codecs=list(defaults.codecs),
            sample_rates=list(defaults.sample_rates),
            bit_depths=list(defaults.bit_depths),
        )

    def to_defaults(self) -> FilterDefaults:
        return FilterDefaults(
            extensions=list(self.extensions),
            min_bitrate=self.min_bitrate,
            exact_bitrate=self.exact_bitrate,
            min_size=self.min_size,
            max_size=self.max_size,
            min_duration=self.min_duration,
            max_duration=self.max_duration,
            codecs=list(self.codecs),
            sample_rates=list(self.sample_rates),
            bit_depths=list(self.bit_depths),
        )

    def is_empty(self) -> bool:
        """True when nothing is being filtered out."""
        for f in fields(self):
            if f.name == "keep_unknown":
                continue
            value = getattr(self, f.name)
            if f.name == "min_relevance":
                if value:
                    return False
                continue
            if value:
                return False
        return True

    def describe(self) -> str:
        """Short description for the interface, so the state is never hidden."""
        if self.is_empty():
            return "No filters (showing everything)"
        parts: list[str] = []
        if self.extensions:
            parts.append("type: " + ", ".join(sorted(self.extensions)))
        if self.exact_bitrate:
            parts.append(f"bitrate exactly {self.exact_bitrate}")
        elif self.min_bitrate:
            parts.append(f"bitrate {self.min_bitrate}+")
        if self.min_size or self.max_size:
            low = _mb(self.min_size)
            high = _mb(self.max_size)
            parts.append(f"size {low or '0'} to {high or 'any'}")
        if self.min_duration or self.max_duration:
            parts.append(
                f"length {self.min_duration or 0}s to {self.max_duration or 'any'}"
            )
        if self.codecs:
            parts.append("codec: " + ", ".join(sorted(self.codecs)))
        if self.sample_rates:
            parts.append("sample rate: " + ", ".join(str(s) for s in sorted(self.sample_rates)))
        if self.bit_depths:
            parts.append("bit depth: " + ", ".join(str(b) for b in sorted(self.bit_depths)))
        if self.text_contains:
            parts.append(f'name contains "{self.text_contains}"')
        if self.only_free_slots:
            parts.append("free slot only")
        if self.hide_locked:
            parts.append("hiding locked files")
        if self.min_relevance:
            parts.append(f"match {int(self.min_relevance * 100)}%+")
        return "; ".join(parts)

    # -- matching ----------------------------------------------------------

    def matches(self, candidate: SearchCandidate) -> bool:
        if self.extensions:
            ext = (candidate.extension or "").lower().lstrip(".")
            if ext not in {e.lower().lstrip(".") for e in self.extensions}:
                return False

        if self.codecs:
            codec = (candidate.codec or candidate.extension or "").lower()
            if codec not in {c.lower() for c in self.codecs}:
                return False

        if not self._numeric_ok(candidate.bitrate, self.min_bitrate, None, self.exact_bitrate):
            return False
        if not self._numeric_ok(candidate.size, self.min_size, self.max_size, None):
            return False
        if not self._numeric_ok(candidate.duration, self.min_duration, self.max_duration, None):
            return False

        if self.sample_rates:
            if candidate.sample_rate is None:
                if not self.keep_unknown:
                    return False
            elif candidate.sample_rate not in self.sample_rates:
                return False

        if self.bit_depths:
            if candidate.bit_depth is None:
                if not self.keep_unknown:
                    return False
            elif candidate.bit_depth not in self.bit_depths:
                return False

        if self.text_contains:
            needle = self.text_contains.lower()
            haystack = f"{candidate.filename} {candidate.directory}".lower()
            if needle not in haystack:
                return False

        if self.users:
            if (candidate.remote_user or "").lower() not in {u.lower() for u in self.users}:
                return False

        if self.only_free_slots and candidate.has_free_slot is False:
            return False

        if self.hide_locked and candidate.locked:
            return False

        if self.min_relevance and candidate.relevance < self.min_relevance:
            return False

        return True

    def _numeric_ok(
        self,
        value: int | None,
        minimum: int | None,
        maximum: int | None,
        exact: int | None,
    ) -> bool:
        if exact is None and minimum is None and maximum is None:
            return True
        if value is None:
            return self.keep_unknown
        if exact is not None:
            return value == exact
        if minimum is not None and value < minimum:
            return False
        if maximum is not None and value > maximum:
            return False
        return True

    def apply(self, candidates: Iterable[SearchCandidate]) -> list[SearchCandidate]:
        return [c for c in candidates if self.matches(c)]


def sort_candidates(
    candidates: Sequence[SearchCandidate],
    field_name: SortField = SortField.RELEVANCE,
    descending: bool = True,
) -> list[SearchCandidate]:
    """Sort results by whichever column the user clicked."""

    def key(c: SearchCandidate):
        if field_name is SortField.RELEVANCE:
            return (c.relevance, c.size or 0)
        if field_name is SortField.FILENAME:
            return (c.filename.lower(),)
        if field_name is SortField.SIZE:
            return (c.size or 0,)
        if field_name is SortField.BITRATE:
            return (c.bitrate or 0,)
        if field_name is SortField.DURATION:
            return (c.duration or 0,)
        if field_name is SortField.SAMPLE_RATE:
            return (c.sample_rate or 0,)
        if field_name is SortField.BIT_DEPTH:
            return (c.bit_depth or 0,)
        if field_name is SortField.USER:
            return (c.remote_user.lower(),)
        if field_name is SortField.QUEUE:
            # An unknown queue sorts as if it were long, so known-free peers
            # rise to the top when the user asks for it.
            return (c.queue_length if c.queue_length is not None else 10**6,)
        if field_name is SortField.SPEED:
            return (c.upload_speed or 0,)
        return (c.relevance,)

    reverse = descending
    if field_name in (SortField.FILENAME, SortField.USER, SortField.QUEUE):
        reverse = not descending
    return sorted(candidates, key=key, reverse=reverse)


def available_values(candidates: Sequence[SearchCandidate]) -> dict[str, list]:
    """What the current results actually contain, to populate the filter menus."""
    extensions = sorted({(c.extension or "").lower() for c in candidates if c.extension})
    bitrates = sorted({c.bitrate for c in candidates if c.bitrate})
    sample_rates = sorted({c.sample_rate for c in candidates if c.sample_rate})
    bit_depths = sorted({c.bit_depth for c in candidates if c.bit_depth})
    users = sorted({c.remote_user for c in candidates if c.remote_user})
    return {
        "extensions": extensions,
        "bitrates": bitrates,
        "sample_rates": sample_rates,
        "bit_depths": bit_depths,
        "users": users,
    }


def _mb(value: int | None) -> str:
    if not value:
        return ""
    return f"{value / (1024 * 1024):.0f} MB"
