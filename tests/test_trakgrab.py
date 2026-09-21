"""Offline tests for trakGrab's pure helpers.

The suite deliberately never touches traktrain.com or the CDN: CI must pass
without network access, and the parsing logic is what silently broke when
traktrain changed its markup (the JSON's first key stopped being `name`).
These tests pin the behaviour that matters for that.

Fixtures in this file are modelled on real markup captured from
traktrain.com in September 2026.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trakGrab  # noqa: E402


class TestParseTracks:
    """Parsing of the data-player-info JSON, the part that broke in 2026."""

    # Real JSON payload shape from a September 2026 profile page: `prices`
    # first, `name` further down. No artist field inside the JSON.
    TRACK_JSON = (
        '{"prices":[["MP3 Lease",50]],"bpm":165,"producerLink":"/uq","id":1669133,'
        '"src":"78923/f4fa299f-8dbf-1521-ac16-6fde4adba0cb.mp3","name":"candycrush "}'
    )

    def test_parses_a_current_player_info_payload(self):
        html = f"<div data-player-info='{self.TRACK_JSON}'></div>"
        tracks = trakGrab.parse_tracks(html)
        assert len(tracks) == 1
        assert tracks[0]["name"] == "candycrush "
        assert tracks[0]["src"].endswith(".mp3")

    def test_name_is_not_the_first_key(self):
        """The 2026 break: scrapers assuming {"name": first found nothing."""
        html = f"<div data-player-info='{self.TRACK_JSON}'></div>"
        tracks = trakGrab.parse_tracks(html)
        assert tracks[0]["name"] == "candycrush "

    def test_data_name_supplies_the_artist(self):
        html = f"<div data-name=\"mel - candycrush  \" data-player-info='{self.TRACK_JSON}'></div>"
        (track,) = trakGrab.parse_tracks(html)
        assert track["artist"] == "mel"
        assert track["name"] == "candycrush "

    def test_data_name_fills_a_missing_title(self):
        json_no_name = '{"prices":[],"id":1,"src":"78923/x.mp3","producerLink":"/uq"}'
        html = f"<div data-name=\"mel - unlisted \" data-player-info='{json_no_name}'></div>"
        (track,) = trakGrab.parse_tracks(html)
        assert track["name"] == "unlisted"

    def test_a_broken_json_payload_is_skipped_not_fatal(self):
        html = "<div data-player-info='not json at all'></div>"
        assert trakGrab.parse_tracks(html) == []

    def test_payloads_without_a_src_are_skipped(self):
        html = "<div data-player-info='{\"prices\":[]}'></div>"
        assert trakGrab.parse_tracks(html) == []

    def test_several_tracks_are_returned_in_order(self):
        html = (
            f"<div data-player-info='{self.TRACK_JSON}'></div>"
            '<div data-player-info=\'{"id":2,"src":"a/b.mp3","name":"second"}\'></div>'
        )
        tracks = trakGrab.parse_tracks(html)
        assert [t["name"].strip() for t in tracks] == ["candycrush", "second"]

    def test_empty_input_returns_empty_list(self):
        """Pagination feeds blank pages; lxml would raise ParserError on them."""
        for blank in ("", "   ", "\n"):
            assert trakGrab.parse_tracks(blank) == []

    def test_malformed_html_returns_empty_list(self):
        assert trakGrab.parse_tracks("<<<not html at all") == []


class TestDisplayName:
    def test_artist_is_prefixed(self):
        assert trakGrab.display_name({"artist": "mel", "name": "6Figures"}) == "mel - 6Figures"

    def test_bare_title_when_no_artist(self):
        assert trakGrab.display_name({"name": "6Figures"}) == "6Figures"

    def test_no_double_prefix(self):
        """A title that already begins with 'artist - ' is left alone."""
        assert trakGrab.display_name({"artist": "mel", "name": "mel - 6Figures"}) == "mel - 6Figures"

    def test_case_insensitive_prefix_check(self):
        assert trakGrab.display_name({"artist": "mel", "name": "Mel - 6Figures"}) == "Mel - 6Figures"


class TestPickSong:
    TRACKS = [
        {"name": "candycrush", "artist": "mel", "src": "a.mp3"},
        {"name": "ends in tragedy", "artist": "mel", "src": "b.mp3"},
    ]

    def test_matches_the_bare_title(self):
        assert trakGrab.pick_song(self.TRACKS, "candycrush")["src"] == "a.mp3"

    def test_matches_the_artist_prefixed_label(self):
        assert trakGrab.pick_song(self.TRACKS, "mel - candycrush")["src"] == "a.mp3"

    def test_matching_is_case_insensitive(self):
        assert trakGrab.pick_song(self.TRACKS, "CANDYCRUSH")["src"] == "a.mp3"

    def test_substring_fallback(self):
        assert trakGrab.pick_song(self.TRACKS, "tragedy")["src"] == "b.mp3"

    def test_no_match_returns_none(self):
        assert trakGrab.pick_song(self.TRACKS, "nope") is None

    def test_an_empty_query_matches_nothing(self):
        assert trakGrab.pick_song(self.TRACKS, "") is None
        assert trakGrab.pick_song(self.TRACKS, "   ") is None


class TestSanitize:
    def test_windows_illegal_characters_are_removed(self):
        assert trakGrab.sanitize('Ken Carson Type Beat - "Zombie"') == "Ken Carson Type Beat - Zombie"

    def test_path_traversal_is_neutralized(self):
        for nasty in ("..\\..\\evil", "../../evil", "a/b", "a\\b"):
            cleaned = trakGrab.sanitize(nasty)
            assert "/" not in cleaned and "\\" not in cleaned
        assert "evil" in trakGrab.sanitize("..\\..\\evil")

    def test_trailing_dots_and_spaces_are_stripped(self):
        assert trakGrab.sanitize("name... ") == "name"

    def test_inner_whitespace_is_collapsed(self):
        assert trakGrab.sanitize("too   many\tspaces") == "too many spaces"

    def test_long_names_are_truncated(self):
        assert len(trakGrab.sanitize("x" * 500)) == trakGrab.MAX_NAME_LEN

    def test_an_empty_name_falls_back_to_untitled(self):
        assert trakGrab.sanitize("") == "untitled"

    def test_a_dot_only_name_falls_back_to_untitled(self):
        """Dots are stripped, so '..' has no usable stem left."""
        assert trakGrab.sanitize("..") == "untitled"

    def test_unicode_survives_sanitization(self):
        assert trakGrab.sanitize("café del mar") == "café del mar"


class TestSafeFilename:
    """The Windows reserved-name guard and collision handling in one place."""

    def test_a_normal_name_passes_through(self, tmp_path):
        assert trakGrab.safe_filename(tmp_path, "mel - 6Figures", ".mp3") == tmp_path / "mel - 6Figures.mp3"

    def test_reserved_device_names_get_an_underscore(self, tmp_path):
        assert trakGrab.safe_filename(tmp_path, "CON", ".mp3") == tmp_path / "_CON.mp3"
        assert trakGrab.safe_filename(tmp_path, "com1", ".mp3") == tmp_path / "_com1.mp3"
        assert trakGrab.safe_filename(tmp_path, "LPT1", ".mp3") == tmp_path / "_LPT1.mp3"

    def test_an_existing_file_gets_a_numbered_suffix(self, tmp_path):
        (tmp_path / "mel - 6Figures.mp3").write_bytes(b"x")
        assert trakGrab.safe_filename(tmp_path, "mel - 6Figures", ".mp3") == tmp_path / "mel - 6Figures (1).mp3"


class TestUniquePath:
    def test_no_collision_returns_the_path_itself(self, tmp_path):
        assert trakGrab.unique_path(tmp_path, "a.mp3") == tmp_path / "a.mp3"

    def test_collision_gets_a_numbered_suffix(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"x")
        assert trakGrab.unique_path(tmp_path, "a.mp3") == tmp_path / "a (1).mp3"

    def test_numbering_increments(self, tmp_path):
        (tmp_path / "a.mp3").write_bytes(b"x")
        (tmp_path / "a (1).mp3").write_bytes(b"x")
        assert trakGrab.unique_path(tmp_path, "a.mp3") == tmp_path / "a (2).mp3"


class TestTrackMarkers:
    """Bookkeeping that distinguishes two tracks sharing one title."""

    def test_a_fresh_download_writes_a_marker(self, tmp_path):
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(b"x")
        trakGrab._remember_track(dest, 1669133, "78923/a.mp3")
        marker = trakGrab._marker_path(dest)
        assert marker.exists()
        assert marker.read_text(encoding="utf-8") == "1669133|78923/a.mp3"

    def test_same_track_recognized_by_marker(self, tmp_path):
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(b"x")
        trakGrab._remember_track(dest, 1669133, "78923/a.mp3")
        assert trakGrab._same_track(dest, 1669133, "78923/a.mp3") is True

    def test_different_track_not_recognized(self, tmp_path):
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(b"x")
        trakGrab._remember_track(dest, 1669133, "78923/a.mp3")
        assert trakGrab._same_track(dest, 9999999, "78923/b.mp3") is False

    def test_a_markerless_file_is_treated_conservatively(self, tmp_path):
        """Pre-bookkeeping or hand-placed files are never overwritten."""
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(b"x")
        assert trakGrab._same_track(dest, 1, "x/y.mp3") is True

    def test_an_unreadable_marker_is_treated_conservatively(self, tmp_path):
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(b"x")
        marker = trakGrab._marker_path(dest)
        marker.write_bytes(b"\xff\xfe\x00bad")
        assert trakGrab._same_track(dest, 1, "x/y.mp3") is True

    def test_grab_skips_a_same_title_track_of_the_same_id(self, tmp_path):
        """Re-running with the marker present still counts as skipped."""
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(b"x")
        trakGrab._remember_track(dest, 1669133, "78923/a.mp3")
        track = {"id": 1669133, "src": "78923/a.mp3", "name": "hit", "artist": "mel"}
        assert trakGrab.grab("https://cdn/", track, tmp_path, skip_existing=True) == "skipped"

    def test_grab_renames_a_same_title_track_with_a_different_id(self, tmp_path, monkeypatch):
        """Two distinct tracks sharing a title must not collapse into one."""
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(b"old bytes")
        trakGrab._remember_track(dest, 111, "78923/old.mp3")

        track = {"id": 222, "src": "78923/new.mp3", "name": "hit", "artist": "mel"}

        # Signature matches trakGrab.download; the URL argument is unused.
        downloaded_to: list[str] = []

        def fake_download(url: str, target) -> bool:
            downloaded_to.append(url)
            target.write_bytes(b"new bytes")  # pretend the fetch worked
            return True

        monkeypatch.setattr(trakGrab, "download", fake_download)
        assert trakGrab.grab("https://cdn/", track, tmp_path, skip_existing=True) == "downloaded"
        assert downloaded_to  # the fetch was attempted for the renamed path

        # The original file is untouched; the new track went to a suffix path
        # with its own marker.
        assert dest.read_bytes() == b"old bytes"
        renamed = tmp_path / "mel - hit (1).mp3"
        assert renamed.read_bytes() == b"new bytes"
        assert trakGrab._same_track(renamed, 222, "78923/new.mp3") is True
        assert trakGrab._same_track(dest, 222, "78923/new.mp3") is False


class TestDedupe:
    def test_same_id_is_dropped(self):
        tracks = [{"id": 1, "src": "a.mp3"}, {"id": 1, "src": "a.mp3"}]
        assert len(trakGrab.dedupe(tracks)) == 1

    def test_distinct_tracks_survive(self):
        tracks = [{"id": 1, "src": "a.mp3"}, {"id": 2, "src": "b.mp3"}]
        assert len(trakGrab.dedupe(tracks)) == 2


class TestBaseUrl:
    def test_extracts_the_cdn_base(self):
        html = "<script>var AWS_BASE_URL = 'https://d2lvs3zi8kbddv.cloudfront.net/';</script>"
        assert trakGrab.extract_base_url(html) == "https://d2lvs3zi8kbddv.cloudfront.net/"

    def test_a_missing_base_url_raises_scrape_error(self):
        with pytest.raises(trakGrab.ScrapeError):
            trakGrab.extract_base_url("<html></html>")


class TestResolveArtistInput:
    def test_a_bare_slug_passes_through(self):
        assert trakGrab.resolve_artist_input("uq") == "uq"

    def test_a_full_profile_url_is_reduced_to_the_slug(self):
        assert trakGrab.resolve_artist_input("https://traktrain.com/uq") == "uq"

    def test_surrounding_slashes_are_trimmed(self):
        assert trakGrab.resolve_artist_input("/mel-beats/") == "mel-beats"

    def test_an_empty_input_stays_empty(self):
        assert trakGrab.resolve_artist_input("   ") == ""


class TestVersion:
    def test_the_banner_and_pyproject_do_not_drift(self):
        """The printed banner should agree with the packaged major.minor."""
        # tomllib is 3.11+; the 3.10 floor takes the backport.
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        version = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
        assert version.split(".")[:2] == ["2", "2"]
