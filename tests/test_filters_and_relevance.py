"""Tests for relevance scoring and the user-controlled filters.

The scoring tests deliberately check that Trackfetch has no opinion about
versions or releases. Preferring a studio take over a live one, or an original
over a remix, is the user's decision and must not be encoded in the software.
"""

from __future__ import annotations

import pytest

from trackfetch.models.common import SearchCandidate
from trackfetch.services import relevance
from trackfetch.services.filters import (
    ResultFilter,
    SortField,
    available_values,
    sort_candidates,
)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("Aphex Twin - Xtal", "aphex twin xtal"),
        ("aphex_twin__xtal", "aphex twin xtal"),
        ("APHEX-TWIN...XTAL", "aphex twin xtal"),
        ("Aphex   Twin    Xtal", "aphex twin xtal"),
        ("Björk", "bjork"),
        ("Simon & Garfunkel", "simon and garfunkel"),
        ("[2019] Album (Deluxe)", "2019 album deluxe"),
    ],
)
def test_normalisation_removes_formatting_noise(given, expected):
    assert relevance.normalise(given) == expected


def test_extension_can_be_stripped():
    assert relevance.normalise("track.flac", strip_extension=True) == "track"
    assert relevance.normalise("track.flac") == "track flac"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_an_exact_match_scores_highest():
    exact = relevance.score("Aphex Twin - Xtal", "Aphex Twin - Xtal.flac")
    partial = relevance.score("Aphex Twin - Xtal", "Aphex Twin - Tha.flac")
    unrelated = relevance.score("Aphex Twin - Xtal", "Britney Spears - Toxic.mp3")
    assert exact > partial > unrelated
    assert exact > 0.85


def test_formatting_differences_do_not_matter():
    a = relevance.score("Aphex Twin - Xtal", "aphex_twin_xtal.mp3")
    b = relevance.score("Aphex Twin - Xtal", "Aphex Twin - Xtal.mp3")
    assert abs(a - b) < 0.2
    assert a > 0.7


def test_the_folder_name_helps_when_the_filename_is_bare():
    with_folder = relevance.score(
        "Aphex Twin - Xtal", "01 Xtal.flac", "Aphex Twin/Selected Ambient Works"
    )
    without_folder = relevance.score("Aphex Twin - Xtal", "01 Xtal.flac")
    assert with_folder > without_folder


def test_live_remix_and_bootleg_are_not_penalised():
    """No version is preferred. Only textual similarity counts."""
    query = "Radiohead - Idioteque"
    studio = relevance.score(query, "Radiohead - Idioteque.flac")
    live = relevance.score(query, "Radiohead - Idioteque (Live).flac")
    remix = relevance.score(query, "Radiohead - Idioteque (Remix).flac")
    bootleg = relevance.score(query, "Radiohead - Idioteque [bootleg].flac")
    # The extra word lowers similarity slightly for all three equally; none is
    # singled out, and none is pushed below the others by a rule.
    assert abs(live - remix) < 0.05
    assert abs(remix - bootleg) < 0.06
    assert studio >= live


def test_asking_for_a_live_version_scores_the_live_file_higher():
    query = "Radiohead - Idioteque live"
    live = relevance.score(query, "Radiohead - Idioteque (Live).flac")
    studio = relevance.score(query, "Radiohead - Idioteque.flac")
    assert live > studio


def test_format_and_bitrate_do_not_affect_the_score():
    a = relevance.score("some track", "some track.mp3")
    b = relevance.score("some track", "some track.flac")
    assert abs(a - b) < 0.05


def test_scores_stay_in_range():
    for candidate in ["", "x", "a very long unrelated filename indeed.mp3"]:
        value = relevance.score("query text", candidate)
        assert 0.0 <= value <= 1.0
    assert relevance.score("", "anything") == 0.0


def test_explain_returns_components():
    parts = relevance.explain("Aphex Twin - Xtal", "Aphex Twin - Xtal.flac")
    assert set(parts) == {"coverage", "ordering", "sequence", "total"}
    assert parts["coverage"] == 1.0


def test_non_music_queries_score_normally():
    assert relevance.score("linux mint iso", "linux-mint-22-cinnamon.iso") > 0.4
    assert relevance.score("annual report 2025", "Annual Report 2025.pdf") > 0.8


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def make(**kwargs) -> SearchCandidate:
    base = dict(
        filename="track.mp3",
        extension="mp3",
        size=5_000_000,
        bitrate=320,
        duration=200,
        sample_rate=44100,
        bit_depth=None,
        codec="mp3",
        remote_user="peer",
        has_free_slot=True,
        relevance=0.9,
    )
    base.update(kwargs)
    return SearchCandidate(**base)


def test_an_empty_filter_keeps_everything():
    f = ResultFilter()
    assert f.is_empty() is True
    assert f.describe() == "No filters (showing everything)"
    items = [make(), make(extension="flac"), make(bitrate=None)]
    assert len(f.apply(items)) == 3


def test_file_type_filter():
    f = ResultFilter(extensions=["flac"])
    items = [make(extension="mp3"), make(extension="flac"), make(extension="FLAC")]
    assert len(f.apply(items)) == 2


def test_minimum_bitrate_filter():
    f = ResultFilter(min_bitrate=256)
    items = [make(bitrate=128), make(bitrate=320), make(bitrate=256)]
    assert len(f.apply(items)) == 2


def test_exact_bitrate_filter():
    f = ResultFilter(exact_bitrate=320)
    items = [make(bitrate=320), make(bitrate=321), make(bitrate=192)]
    assert len(f.apply(items)) == 1


def test_size_range_filter():
    f = ResultFilter(min_size=1_000_000, max_size=10_000_000)
    items = [make(size=500_000), make(size=5_000_000), make(size=50_000_000)]
    assert len(f.apply(items)) == 1


def test_duration_range_filter():
    f = ResultFilter(min_duration=100, max_duration=300)
    items = [make(duration=60), make(duration=200), make(duration=600)]
    assert len(f.apply(items)) == 1


def test_sample_rate_and_bit_depth_filters():
    f = ResultFilter(sample_rates=[44100], bit_depths=[16])
    items = [
        make(sample_rate=44100, bit_depth=16),
        make(sample_rate=48000, bit_depth=16),
        make(sample_rate=44100, bit_depth=24),
    ]
    assert len(f.apply(items)) == 1


def test_results_with_no_metadata_are_kept_by_default():
    """Soulseek peers often report nothing. Hiding those would hide real files."""
    f = ResultFilter(min_bitrate=320)
    items = [make(bitrate=None), make(bitrate=320), make(bitrate=128)]
    assert len(f.apply(items)) == 2


def test_results_with_no_metadata_can_be_excluded_on_request():
    f = ResultFilter(min_bitrate=320, keep_unknown=False)
    items = [make(bitrate=None), make(bitrate=320)]
    assert len(f.apply(items)) == 1


def test_free_slot_and_locked_filters():
    items = [
        make(has_free_slot=True),
        make(has_free_slot=False),
        make(locked=True),
    ]
    assert len(ResultFilter(only_free_slots=True).apply(items)) == 2
    assert len(ResultFilter(hide_locked=True).apply(items)) == 2


def test_text_and_relevance_filters():
    items = [make(filename="Xtal.flac", relevance=0.9), make(filename="Tha.flac", relevance=0.2)]
    assert len(ResultFilter(text_contains="xtal").apply(items)) == 1
    assert len(ResultFilter(min_relevance=0.5).apply(items)) == 1


def test_filter_description_is_readable():
    f = ResultFilter(extensions=["flac"], min_bitrate=900, only_free_slots=True)
    text = f.describe()
    assert "flac" in text
    assert "900+" in text
    assert "free slot" in text


def test_filters_round_trip_through_saved_defaults():
    from trackfetch.config.settings import FilterDefaults

    original = ResultFilter(extensions=["flac", "wav"], min_bitrate=500, max_size=100)
    defaults: FilterDefaults = original.to_defaults()
    restored = ResultFilter.from_defaults(defaults)
    assert restored.extensions == ["flac", "wav"]
    assert restored.min_bitrate == 500
    assert restored.max_size == 100


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------


def test_sorting_by_each_column():
    items = [
        make(filename="b.mp3", size=200, bitrate=128, relevance=0.5, queue_length=5),
        make(filename="a.mp3", size=100, bitrate=320, relevance=0.9, queue_length=0),
    ]
    assert sort_candidates(items, SortField.RELEVANCE)[0].relevance == 0.9
    assert sort_candidates(items, SortField.SIZE)[0].size == 200
    assert sort_candidates(items, SortField.BITRATE)[0].bitrate == 320
    assert sort_candidates(items, SortField.FILENAME)[0].filename == "a.mp3"
    assert sort_candidates(items, SortField.QUEUE)[0].queue_length == 0


def test_available_values_populates_the_filter_menus():
    items = [make(extension="flac", bitrate=1000), make(extension="mp3", bitrate=320)]
    values = available_values(items)
    assert values["extensions"] == ["flac", "mp3"]
    assert values["bitrates"] == [320, 1000]
