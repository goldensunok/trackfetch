"""Textual relevance between what the user asked for and what came back.

This measures similarity of text and nothing else.

It deliberately contains no opinion about studio versus live, original versus
remix, official versus bootleg, one release versus another, or any preferred
format or bitrate. Those are the user's decisions, made in the results table
with the filters and the sort controls. Encoding them here would quietly
override the person using the program, which is exactly what the specification
rules out.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

#: Characters that separate words in filenames but carry no meaning.
_SEPARATORS = re.compile(r"[_\-\.\+/\\|,;:!\?\*\"'`~\[\]\(\)\{\}<>@#$%^&=]+")
_WHITESPACE = re.compile(r"\s+")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


@lru_cache(maxsize=8192)
def normalise(text: str, strip_extension: bool = False) -> str:
    """Reduce a string to comparable words.

    Handles capitalisation, underscores, hyphens, punctuation, accented
    characters and repeated whitespace. Digits and letters survive untouched.
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKD", text)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    if strip_extension:
        value = _EXTENSION.sub("", value)
    value = value.replace("&", " and ")
    value = _SEPARATORS.sub(" ", value)
    value = _WHITESPACE.sub(" ", value)
    return value.strip().lower()


@lru_cache(maxsize=8192)
def tokens(text: str, strip_extension: bool = False) -> tuple[str, ...]:
    normalised = normalise(text, strip_extension)
    return tuple(t for t in normalised.split(" ") if t)


def _coverage(query_tokens: tuple[str, ...], candidate_tokens: tuple[str, ...]) -> float:
    """Fraction of the query's words that appear in the candidate."""
    if not query_tokens:
        return 0.0
    candidate_set = set(candidate_tokens)
    joined = " ".join(candidate_tokens)
    hits = 0
    for token in query_tokens:
        if token in candidate_set:
            hits += 1
        elif len(token) >= 4 and token in joined:
            # Allow a word that got glued to its neighbour in the filename.
            hits += 0.75
    return min(1.0, hits / len(query_tokens))


def _ordering(query_tokens: tuple[str, ...], candidate_tokens: tuple[str, ...]) -> float:
    """How much of the query's word order survives in the candidate."""
    if len(query_tokens) < 2:
        return 1.0
    positions: list[int] = []
    cursor = 0
    for token in query_tokens:
        try:
            index = candidate_tokens.index(token, cursor)
        except ValueError:
            continue
        positions.append(index)
        cursor = index + 1
    if len(positions) < 2:
        return 0.5 if positions else 0.0
    return len(positions) / len(query_tokens)


def _sequence(query: str, candidate: str) -> float:
    if not query or not candidate:
        return 0.0
    return SequenceMatcher(None, query, candidate).ratio()


def score(query: str, candidate_text: str, directory: str = "") -> float:
    """Return a textual similarity score between 0.0 and 1.0.

    ``candidate_text`` is normally the filename. ``directory`` is consulted as a
    weaker secondary signal, because the album or folder name often carries the
    artist while the filename carries only the track.
    """
    query_norm = normalise(query)
    if not query_norm:
        return 0.0

    file_norm = normalise(candidate_text, strip_extension=True)
    dir_norm = normalise(directory)

    query_tokens = tokens(query)
    file_tokens = tokens(candidate_text, strip_extension=True)
    combined_tokens = file_tokens + tokens(directory)

    coverage_file = _coverage(query_tokens, file_tokens)
    coverage_all = _coverage(query_tokens, combined_tokens)
    coverage_value = max(coverage_file, 0.85 * coverage_all)

    ordering_value = _ordering(query_tokens, file_tokens)
    sequence_value = max(
        _sequence(query_norm, file_norm),
        0.8 * _sequence(query_norm, f"{dir_norm} {file_norm}".strip()),
    )

    total = 0.6 * coverage_value + 0.15 * ordering_value + 0.25 * sequence_value
    return round(max(0.0, min(1.0, total)), 4)


def best_score(query: str, texts: list[str]) -> float:
    return max((score(query, t) for t in texts), default=0.0)


def explain(query: str, candidate_text: str, directory: str = "") -> dict[str, float]:
    """Component breakdown, shown in the interface as a tooltip."""
    query_tokens = tokens(query)
    file_tokens = tokens(candidate_text, strip_extension=True)
    return {
        "coverage": round(_coverage(query_tokens, file_tokens), 4),
        "ordering": round(_ordering(query_tokens, file_tokens), 4),
        "sequence": round(
            _sequence(normalise(query), normalise(candidate_text, strip_extension=True)), 4
        ),
        "total": score(query, candidate_text, directory),
    }
