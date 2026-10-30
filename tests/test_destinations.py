"""Tests for safe destination paths, duplicate handling and checksums."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trackfetch.models.common import DuplicatePolicy
from trackfetch.services.destinations import (
    DuplicateAction,
    decide_destination,
    fingerprint,
    safe_join,
    sanitise_filename,
    sanitise_relative_dir,
    unique_path,
    verify_checksum,
)


# ---------------------------------------------------------------------------
# Filename safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("normal.mp3", "normal.mp3"),
        ("C:\\shared\\music\\track.flac", "track.flac"),
        ("@@abc\\Music\\Album\\01 Xtal.flac", "01 Xtal.flac"),
        ("/home/someone/file.zip", "file.zip"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.dll", "evil.dll"),
        ("bad:name?.mp3", "bad_name_.mp3"),
        ("trailing dots...", "trailing dots"),
        ("", "download"),
        ("   ", "download"),
        ("..", "download"),
    ],
)
def test_filenames_are_made_safe(given, expected):
    assert sanitise_filename(given) == expected


def test_windows_reserved_names_are_escaped():
    assert sanitise_filename("CON.txt") == "_CON.txt"
    assert sanitise_filename("nul") == "_nul"


def test_absurdly_long_names_are_shortened_but_keep_the_extension():
    name = "x" * 500 + ".flac"
    result = sanitise_filename(name)
    assert len(result) <= 180
    assert result.endswith(".flac")


def test_control_characters_are_removed():
    assert "\n" not in sanitise_filename("bad\nname.mp3")
    assert "\x00" not in sanitise_filename("bad\x00name.mp3")


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


def test_relative_directories_are_stripped_of_escapes():
    assert sanitise_relative_dir("a/../../b") == "a/b"
    assert sanitise_relative_dir("C:\\Music\\Album") == "Music/Album"
    assert sanitise_relative_dir("") == ""


def test_safe_join_keeps_everything_under_the_download_folder(tmp_path):
    result = safe_join(tmp_path, "Artist/Album")
    assert result == tmp_path / "Artist" / "Album"


def test_safe_join_refuses_to_escape(tmp_path):
    # The escaping components are stripped rather than followed.
    result = safe_join(tmp_path, "../../../etc")
    assert str(result).startswith(str(tmp_path))
    assert "etc" in str(result)


def test_safe_join_handles_absolute_looking_input(tmp_path):
    result = safe_join(tmp_path, "/etc/passwd")
    assert str(result).startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_a_new_file_is_simply_written(tmp_path):
    decision = decide_destination(tmp_path, "song.mp3")
    assert decision.action is DuplicateAction.WRITE
    assert decision.path == tmp_path / "song.mp3"


def test_an_existing_file_is_never_silently_overwritten(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"already here")
    decision = decide_destination(tmp_path, "song.mp3", DuplicatePolicy.ASK)
    assert decision.action is DuplicateAction.ASK
    assert decision.existing == tmp_path / "song.mp3"


def test_skip_policy(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"x")
    decision = decide_destination(tmp_path, "song.mp3", DuplicatePolicy.SKIP)
    assert decision.action is DuplicateAction.SKIP


def test_rename_policy_produces_a_new_name(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"x")
    decision = decide_destination(tmp_path, "song.mp3", DuplicatePolicy.RENAME)
    assert decision.action is DuplicateAction.RENAME
    assert decision.path.name == "song (2).mp3"


def test_rename_keeps_counting(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"x")
    (tmp_path / "song (2).mp3").write_bytes(b"x")
    assert unique_path(tmp_path / "song.mp3").name == "song (3).mp3"


def test_replace_policy_targets_the_same_path(tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"x")
    decision = decide_destination(tmp_path, "song.mp3", DuplicatePolicy.REPLACE)
    assert decision.action is DuplicateAction.REPLACE
    assert decision.path == tmp_path / "song.mp3"


def test_part_file_naming(tmp_path):
    decision = decide_destination(tmp_path, "song.mp3")
    assert decision.part_path.name == "song.mp3.part"


# ---------------------------------------------------------------------------
# Fingerprints and checksums
# ---------------------------------------------------------------------------


def test_fingerprints_are_stable_and_specific():
    a = fingerprint("slskd", "C:\\music\\x.mp3", 100)
    b = fingerprint("slskd", "c:\\MUSIC\\X.mp3", 100)
    c = fingerprint("slskd", "C:\\music\\x.mp3", 101)
    d = fingerprint("http", "C:\\music\\x.mp3", 100)
    assert a == b
    assert a != c
    assert a != d


def test_checksum_verification(tmp_path):
    path = tmp_path / "f.bin"
    payload = b"trackfetch"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert verify_checksum(path, f"sha256:{digest}") is True
    assert verify_checksum(path, digest) is True
    assert verify_checksum(path, "sha256:" + "0" * 64) is False


def test_no_checksum_means_nothing_to_check(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"x")
    assert verify_checksum(path, "") is True
    assert verify_checksum(path, "not-a-digest") is True
