"""Tests for pasted lists, TXT import, CSV import and link recognition."""

from __future__ import annotations

from trackfetch.models.common import InputKind
from trackfetch.services import input_parser


def test_pasted_list_becomes_search_queries():
    items = input_parser.parse_lines(
        "Aphex Twin - Xtal\nBurial - Archangel\n\n  Boards of Canada - Roygbiv  "
    )
    assert [i.raw_text for i in items] == [
        "Aphex Twin - Xtal",
        "Burial - Archangel",
        "Boards of Canada - Roygbiv",
    ]
    assert all(i.kind is InputKind.QUERY for i in items)
    assert [i.position for i in items] == [0, 1, 2]


def test_queries_are_not_assumed_to_be_music():
    items = input_parser.parse_lines(
        "linux mint iso\nannual report 2025 pdf\nsomeone's holiday photos"
    )
    assert all(i.kind is InputKind.QUERY for i in items)
    assert len(items) == 3


def test_comments_and_blank_lines_are_ignored():
    items = input_parser.parse_lines("# a note\n\nreal query\n// another note")
    assert len(items) == 1
    assert items[0].raw_text == "real query"


def test_repeated_lines_are_dropped():
    items = input_parser.parse_lines("same thing\nSAME THING\nsame thing\nother")
    assert len(items) == 2


def test_links_magnets_and_files_are_recognised():
    text = "\n".join(
        [
            "https://example.com/files/album.zip",
            "http://example.com/a.mp3",
            "magnet:?xt=urn:btih:abcdef0123456789",
            "C:\\Users\\tobias\\Downloads\\thing.torrent",
            "/home/tobias/news.nzb",
            "example.com/download/file.bin",
            "just a search",
        ]
    )
    kinds = [i.kind for i in input_parser.parse_lines(text)]
    assert kinds == [
        InputKind.URL,
        InputKind.URL,
        InputKind.MAGNET,
        InputKind.TORRENT_FILE,
        InputKind.NZB_FILE,
        InputKind.URL,
        InputKind.QUERY,
    ]


def test_a_query_containing_a_dot_is_still_a_query():
    assert input_parser.classify("Mr. Scruff - Get A Move On") is InputKind.QUERY
    assert input_parser.classify("St. Germain") is InputKind.QUERY


def test_txt_import(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    items = input_parser.parse_file(path)
    assert len(items) == 3
    assert items[0].source == "list.txt"


def test_txt_import_handles_a_byte_order_mark_and_windows_line_endings(tmp_path):
    path = tmp_path / "list.txt"
    path.write_bytes("\ufeffone\r\ntwo\r\n".encode("utf-8"))
    items = input_parser.parse_file(path)
    assert [i.raw_text for i in items] == ["one", "two"]


def test_txt_import_handles_a_non_utf8_file(tmp_path):
    path = tmp_path / "list.txt"
    path.write_bytes("Bj\xf6rk - Hyperballad\n".encode("cp1252"))
    items = input_parser.parse_file(path)
    assert items[0].raw_text.startswith("Bj")


def test_csv_import_picks_the_obvious_column(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text(
        "Artist,Query,Notes\nAphex Twin,Aphex Twin - Xtal,favourite\nBurial,Burial - Archangel,\n",
        encoding="utf-8",
    )
    items = input_parser.parse_file(path)
    assert [i.raw_text for i in items] == ["Aphex Twin - Xtal", "Burial - Archangel"]
    assert items[0].extra["Artist"] == "Aphex Twin"
    assert items[0].extra["Notes"] == "favourite"


def test_csv_without_a_header_uses_the_first_column(tmp_path):
    path = tmp_path / "plain.csv"
    path.write_text("one thing,extra\ntwo thing,extra2\n", encoding="utf-8")
    items = input_parser.parse_file(path)
    assert [i.raw_text for i in items] == ["one thing", "two thing"]


def test_csv_with_semicolons_is_handled(tmp_path):
    path = tmp_path / "euro.csv"
    path.write_text("query;note\nfirst item;a\nsecond item;b\n", encoding="utf-8")
    items = input_parser.parse_file(path)
    assert [i.raw_text for i in items] == ["first item", "second item"]


def test_csv_of_urls_is_classified_correctly(tmp_path):
    path = tmp_path / "urls.csv"
    path.write_text("url\nhttps://example.com/a.zip\nhttps://example.com/b.zip\n", encoding="utf-8")
    items = input_parser.parse_file(path)
    assert all(i.kind is InputKind.URL for i in items)


def test_torrent_and_nzb_files_import_as_single_items(tmp_path):
    torrent = tmp_path / "thing.torrent"
    torrent.write_bytes(b"d8:announce")
    items = input_parser.parse_file(torrent)
    assert len(items) == 1
    assert items[0].kind is InputKind.TORRENT_FILE


def test_hundreds_of_items_are_accepted():
    text = "\n".join(f"query number {i}" for i in range(500))
    items = input_parser.parse_lines(text)
    assert len(items) == 500
    summary = input_parser.summarise(items)
    assert summary["TOTAL"] == 500
    assert summary["QUERY"] == 500


def test_the_item_limit_is_respected():
    text = "\n".join(f"item {i}" for i in range(50))
    items = input_parser.parse_lines(text, limit=10)
    assert len(items) == 10


def test_merge_combines_sources_without_repeats():
    a = input_parser.parse_lines("one\ntwo")
    b = input_parser.parse_lines("two\nthree")
    merged = input_parser.merge(a, b)
    assert [i.raw_text for i in merged] == ["one", "two", "three"]
    assert [i.position for i in merged] == [0, 1, 2]


def test_very_long_lines_are_trimmed():
    items = input_parser.parse_lines("x" * 5000)
    assert len(items[0].raw_text) == input_parser.MAX_LINE_LENGTH
