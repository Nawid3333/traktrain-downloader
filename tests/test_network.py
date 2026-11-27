"""Network-path tests: every HTTP code path, fully mocked.

`test_trakgrab.py` covers the pure helpers; this file covers everything that
talks HTTP -- scraping, pagination, downloading, and the `main()` flows --
via respx, so the whole module can reach 100% coverage while CI stays
offline. Fixtures model the real September 2026 traktrain markup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import respx
from mutagen import MutagenError
from mutagen.id3 import ID3, TENC

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trakGrab  # noqa: E402

TRACK_JSON = '{"prices":[],"producerLink":"/uq","id":1669133,"src":"78923/a.mp3","name":"candycrush"}'
TRACK_JSON_2 = '{"prices":[],"id":2,"src":"78923/b.mp3","name":"second"}'
TRACK_FIXTURE = {"id": 1669133, "src": "78923/a.mp3", "name": "hit", "artist": "mel"}
PROFILE_HTML = (
    "<html><script>var AWS_BASE_URL = 'https://cdn.example/';</script>"
    "<form data-endpoint='/profile-tracks/123'></form>"
    f"<div data-player-info='{TRACK_JSON}'></div></html>"
)
# A single-track page (/t/<id>), November 2026 shape: no data-player-info,
# the payload is the first argument of an inline FixedPlayer(...) call.
TRACK_PAGE_JSON = '{"prices":[],"producerLink":"/uq","id":1678104,"src":"78923/c.mp3","name":"hit ","brand":"mel"}'
TRACK_PAGE_HTML = (
    "<html><script>var AWS_BASE_URL = 'https://cdn.example/';</script>"
    f"<script>const fp1 = FixedPlayer({TRACK_PAGE_JSON}, true, null);</script></html>"
)
LINK_TRACK = {"id": 1678104, "src": "78923/c.mp3", "name": "hit ", "artist": "mel", "producerLink": "/uq"}


@pytest.fixture()
def _fresh_client():
    """A clean client before and after each network test."""
    trakGrab.close_client()
    yield
    trakGrab.close_client()


@pytest.fixture()
def respx_mock_scope():
    """Active respx router for one test (fixture form of @respx.mock)."""
    with respx.mock:
        yield respx.mock


class TestClientLifecycle:
    """The shared httpx client and its Referer-bearing default headers."""

    def test_get_client_creates_once(self, _fresh_client):
        first = trakGrab.get_client()
        assert trakGrab.get_client() is first
        assert first.headers["Referer"] == trakGrab.TRAKTRAIN

    def test_close_client_is_idempotent(self, _fresh_client):
        trakGrab.get_client()
        trakGrab.close_client()
        trakGrab.close_client()  # second call is a no-op, not a crash
        assert trakGrab._client is None


class TestFetchText:
    def test_a_plain_get_decodes_the_body(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://x.test/page").respond(200, text="hello")
        assert trakGrab.fetch_text("https://x.test/page") == "hello"

    def test_ajax_mode_sends_the_xhr_header(self, _fresh_client, respx_mock_scope):
        route = respx_mock_scope.get("https://x.test/api").respond(200, text="{}")
        trakGrab.fetch_text("https://x.test/api", ajax=True)
        assert route.calls.last.request.headers["X-Requested-With"] == "XMLHttpRequest"

    def test_an_http_error_raises(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://x.test/404").respond(404)
        with pytest.raises(httpx.HTTPStatusError):
            trakGrab.fetch_text("https://x.test/404")


class TestParseTracksAdditional:
    """A few parse paths only reachable with lxml-shaped input."""

    def test_a_data_name_without_separator_leaves_the_json_alone(self):
        # Built via concatenation to stay under the line-length limit.
        json_payload = '{"id":1,"src":"a.mp3","name":"t"}'
        html = f"<div data-name='no separator here' data-player-info='{json_payload}'></div>"
        (track,) = trakGrab.parse_tracks(html)
        assert "artist" not in track

    def test_an_empty_artist_side_is_ignored(self):
        html = '<div data-name=\' - title\' data-player-info=\'{"id":1,"src":"a.mp3","name":"t"}\'></div>'
        (track,) = trakGrab.parse_tracks(html)
        assert "artist" not in track

    def test_a_none_attribute_is_skipped(self):
        """lxml yields None for an attribute lxml itself could not expose."""
        html = '<div data-player-info=\'{"id":1,"src":"a.mp3"}\'></div>'
        (track,) = trakGrab.parse_tracks(html)
        assert track["src"] == "a.mp3"


class TestPaginate:
    PAGE_URL = "https://traktrain.com/profile-tracks/123"

    def test_no_endpoint_means_no_pagination(self):
        assert trakGrab.paginate("<html>no form here</html>") == []

    def test_one_extra_page_is_collected(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/profile-tracks/123?page=2").respond(
            200, json={"content": f"<div data-player-info='{TRACK_JSON_2}'></div>"}
        )
        respx_mock_scope.get("https://traktrain.com/profile-tracks/123?page=3").respond(
            200, json={"content": "<div class='empty-search'></div>"}
        )
        first = f"<form data-endpoint='/profile-tracks/123'></form><div data-player-info='{TRACK_JSON}'></div>"
        tracks = trakGrab.paginate(first)
        assert [t["name"] for t in tracks] == ["second"]

    def test_a_non_json_response_stops_pagination(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/profile-tracks/123?page=2").respond(
            200, text="<html>not json</html>"
        )
        first = "<form data-endpoint='/profile-tracks/123'></form>"
        assert trakGrab.paginate(first) == []

    def test_an_http_error_stops_pagination(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/profile-tracks/123?page=2").respond(500)
        first = "<form data-endpoint='/profile-tracks/123'></form>"
        assert trakGrab.paginate(first) == []

    def test_a_non_dict_payload_stops_pagination(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/profile-tracks/123?page=2").respond(200, json=[1, 2, 3])
        first = "<form data-endpoint='/profile-tracks/123'></form>"
        assert trakGrab.paginate(first) == []


class TestScrapeArtist:
    @pytest.fixture(autouse=True)
    def _page2_empty(self, respx_mock_scope):
        """scrape_artist always probes page 2; it comes back empty here."""
        respx_mock_scope.get(url="https://traktrain.com/profile-tracks/123", params={"page": "2"}).respond(
            200, json={"content": ""}
        )

    def test_scrape_returns_base_url_and_deduped_tracks(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/uq").respond(200, text=PROFILE_HTML)
        base_url, tracks = trakGrab.scrape_artist("uq")
        assert base_url == "https://cdn.example/"
        assert [t["name"] for t in tracks] == ["candycrush"]

    def test_the_artist_path_is_percent_encoded(self, _fresh_client, respx_mock_scope):
        route = respx_mock_scope.get("https://traktrain.com/mel%20beat").respond(200, text=PROFILE_HTML)
        trakGrab.scrape_artist("mel beat")
        assert route.called

    def test_a_404_propagates_to_the_caller(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/ghost").respond(404)
        with pytest.raises(httpx.HTTPStatusError):
            trakGrab.scrape_artist("ghost")


class TestScrapeTrack:
    def test_a_track_page_gives_base_url_and_payload(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/t/1678104").respond(200, text=TRACK_PAGE_HTML)
        base_url, track = trakGrab.scrape_track("1678104")
        assert base_url == "https://cdn.example/"
        assert track["src"] == "78923/c.mp3"
        assert track["artist"] == "mel"

    def test_a_404_propagates_to_the_caller(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get("https://traktrain.com/t/9").respond(404)
        with pytest.raises(httpx.HTTPStatusError):
            trakGrab.scrape_track("9")

    def test_a_page_without_a_player_raises_scrape_error(self, _fresh_client, respx_mock_scope):
        html = "<script>var AWS_BASE_URL = 'https://cdn.example/';</script>"
        respx_mock_scope.get("https://traktrain.com/t/5").respond(200, text=html)
        with pytest.raises(trakGrab.ScrapeError):
            trakGrab.scrape_track("5")


class TestGrabTrackLinks:
    """Downloading by link: each failure is reported, the rest still run."""

    @pytest.fixture(autouse=True)
    def _fake_download(self, monkeypatch):
        monkeypatch.setattr(trakGrab, "download", lambda _u, t: (t.write_bytes(b"x"), True)[1])

    def _scrape_by_id(self, monkeypatch, outcomes):
        def fake(track_id):
            outcome = outcomes[track_id]
            if isinstance(outcome, Exception):
                raise outcome
            return "https://cdn.example/", dict(outcome)

        monkeypatch.setattr(trakGrab, "scrape_track", fake)

    def test_a_track_lands_in_its_producer_folder(self, tmp_path, monkeypatch, capsys):
        self._scrape_by_id(monkeypatch, {"1": LINK_TRACK})
        (tmp_path / "uq").mkdir()
        (tmp_path / "uq" / ".mel - old.mp3.trakid").write_text("1|x.mp3")  # left by an older version
        trakGrab.grab_track_links(["1"], tmp_path)
        assert [p.name for p in (tmp_path / "uq").iterdir()] == ["mel - hit.mp3"]
        out = capsys.readouterr().out
        assert "[1/1] mel - hit" in out
        assert "1 downloaded, 0 already existed, 0 failed" in out
        assert out.count("Saved to:") == 1

    def test_a_second_run_skips_the_same_track(self, tmp_path, monkeypatch, capsys):
        self._scrape_by_id(monkeypatch, {"1": LINK_TRACK})
        trakGrab.grab_track_links(["1"], tmp_path)
        trakGrab.grab_track_links(["1"], tmp_path)
        assert "0 downloaded, 1 already existed" in capsys.readouterr().out
        assert not (tmp_path / "uq" / "mel - hit (1).mp3").exists()

    def test_every_failure_kind_is_counted_and_the_rest_continue(self, tmp_path, monkeypatch, capsys):
        request = httpx.Request("GET", "https://traktrain.com/t/x")
        self._scrape_by_id(
            monkeypatch,
            {
                "404": httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request)),
                "503": httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request)),
                "net": httpx.ConnectError("no route to host"),
                "kit": trakGrab.ScrapeError("no playable preview on the track page"),
                "ok": LINK_TRACK,
            },
        )
        trakGrab.grab_track_links(["404", "503", "net", "kit", "ok"], tmp_path)
        out = capsys.readouterr().out
        assert "Track 404: not found on traktrain.com." in out
        assert "Track 503: traktrain returned HTTP 503." in out
        assert "Track net: connection failed: no route to host" in out
        assert "Track kit: no playable preview on the track page." in out
        assert "1 downloaded, 0 already existed, 4 failed" in out
        assert (tmp_path / "uq" / "mel - hit.mp3").exists()

    def test_several_producers_get_one_saved_line_each(self, tmp_path, monkeypatch, capsys):
        other = {"id": 2, "src": "9/d.mp3", "name": "two", "artist": "zed", "producerLink": "/zed"}
        third = {**LINK_TRACK, "id": 3, "src": "78923/e.mp3", "name": "three"}
        self._scrape_by_id(monkeypatch, {"1": LINK_TRACK, "2": other, "3": third})
        trakGrab.grab_track_links(["1", "2", "3"], tmp_path)
        out = capsys.readouterr().out
        assert out.count("Saved to:") == 2
        assert (tmp_path / "zed" / "zed - two.mp3").exists()
        assert (tmp_path / "uq" / "mel - three.mp3").exists()


class TestReadMoreLinks:
    def _inputs(self, monkeypatch, *answers):
        it = iter(answers)

        def fake_input(_prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)

    def test_lines_are_read_until_an_empty_one(self, monkeypatch):
        self._inputs(monkeypatch, " t/2 ", "t/3", "", "never read")
        assert trakGrab.read_more_links() == ["t/2", "t/3"]

    def test_end_of_input_also_ends_the_list(self, monkeypatch):
        self._inputs(monkeypatch, "t/2")
        assert trakGrab.read_more_links() == ["t/2"]


class TestDownload:
    @pytest.fixture(autouse=True)
    def _router(self, respx_mock_scope):
        self.router = respx_mock_scope

    def _route(self, body: bytes, status: int = 200):
        return self.router.get("https://cdn.example/a.mp3").respond(status, content=body)

    def test_a_successful_download_writes_the_file(self, tmp_path, _fresh_client):
        self._route(b"mp3-bytes")
        dest = tmp_path / "a.mp3"
        assert trakGrab.download("https://cdn.example/a.mp3", dest) is True
        assert dest.read_bytes() == b"mp3-bytes"

    def test_an_http_error_retries_then_cleans_up(self, tmp_path, _fresh_client, monkeypatch):
        self._route(b"nope", status=500)
        monkeypatch.setattr(trakGrab.time, "sleep", lambda _s: None)
        dest = tmp_path / "a.mp3"
        assert trakGrab.download("https://cdn.example/a.mp3", dest) is False
        assert not dest.exists()  # partial file removed after every attempt

    def test_a_network_error_retries_then_gives_up(self, tmp_path, _fresh_client, monkeypatch):
        self.router.get("https://cdn.example/a.mp3").mock(side_effect=httpx.ConnectError("boom"))
        monkeypatch.setattr(trakGrab.time, "sleep", lambda _s: None)
        dest = tmp_path / "a.mp3"
        assert trakGrab.download("https://cdn.example/a.mp3", dest) is False
        assert not dest.exists()

    def test_a_failure_then_success_recovers(self, tmp_path, _fresh_client):
        route = self.router.get("https://cdn.example/a.mp3")
        dest = tmp_path / "a.mp3"

        calls = {"n": 0}

        def flaky(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, content=b"recovered")

        route.mock(side_effect=flaky)
        assert trakGrab.download("https://cdn.example/a.mp3", dest) is True
        assert dest.stat().st_size > 0

    def test_a_midstream_failure_removes_the_partial(self, tmp_path, _fresh_client, monkeypatch):
        def stream_breaker(request):
            # A body that raises mid-iteration simulates a dropped connection.
            def gen():
                yield b"first-half"
                raise httpx.ReadError("connection dropped")

            return httpx.Response(200, content=gen())

        self.router.get("https://cdn.example/a.mp3").mock(side_effect=stream_breaker)
        monkeypatch.setattr(trakGrab.time, "sleep", lambda _s: None)
        dest = tmp_path / "a.mp3"
        assert trakGrab.download("https://cdn.example/a.mp3", dest) is False
        assert not dest.exists()

    def test_progress_prints_for_a_known_length(self, tmp_path, _fresh_client, capsys):
        self._route(b"x" * 100)
        dest = tmp_path / "a.mp3"
        assert trakGrab.download("https://cdn.example/a.mp3", dest) is True
        assert "100%" in capsys.readouterr().out

    def test_no_progress_output_without_content_length(self, tmp_path, _fresh_client, capsys):
        self.router.get("https://cdn.example/a.mp3").respond(200, headers={"Content-Length": ""}, content=b"abc")
        dest = tmp_path / "a.mp3"
        assert trakGrab.download("https://cdn.example/a.mp3", dest) is True
        assert "%" not in capsys.readouterr().out


class TestGrab:
    TRACK = {"id": 1669133, "src": "78923/a.mp3", "name": "hit", "artist": "mel"}

    def test_an_empty_src_fails_without_network(self, tmp_path):
        assert trakGrab.grab("https://cdn/", {"id": 1}, tmp_path, skip_existing=True) == "failed"

    def test_an_absolute_src_is_used_verbatim(self, tmp_path, monkeypatch):
        seen = {}

        def fake_download(url, target):
            seen["url"] = url
            target.write_bytes(b"x")
            return True

        monkeypatch.setattr(trakGrab, "download", fake_download)
        track = {"id": 1, "src": "https://elsewhere.example/a.mp3", "name": "t"}
        assert trakGrab.grab("https://cdn/", track, tmp_path, skip_existing=False) == "downloaded"
        assert seen["url"] == "https://elsewhere.example/a.mp3"

    def test_a_relative_src_is_joined_to_the_base(self, tmp_path, monkeypatch):
        seen = {}

        def fake_download(url, target):
            seen["url"] = url
            target.write_bytes(b"x")
            return True

        monkeypatch.setattr(trakGrab, "download", fake_download)
        trakGrab.grab("https://cdn.example/", self.TRACK, tmp_path, skip_existing=False)
        assert seen["url"] == "https://cdn.example/78923/a.mp3"

    def test_a_failed_download_removes_the_partial(self, tmp_path, monkeypatch):
        def failing_download(_url, target):
            target.write_bytes(b"partial")
            return False

        monkeypatch.setattr(trakGrab, "download", failing_download)
        assert trakGrab.grab("https://cdn/", self.TRACK, tmp_path, skip_existing=False) == "failed"
        assert not (tmp_path / "mel - hit.mp3").exists()

    def test_a_src_without_extension_defaults_to_mp3(self, tmp_path, monkeypatch):
        seen = {}

        def fake_download(url, target):
            seen["ext"] = target.suffix
            target.write_bytes(b"x")
            return True

        monkeypatch.setattr(trakGrab, "download", fake_download)
        track = {"id": 1, "src": "78923/noext", "name": "t"}
        trakGrab.grab("https://cdn/", track, tmp_path, skip_existing=False)
        assert seen["ext"] == ".mp3"

    def test_a_track_without_an_artist_uses_the_bare_label(self, tmp_path, monkeypatch):
        seen = {}

        def fake_download(url, target):
            seen["name"] = target.name
            target.write_bytes(b"x")
            return True

        monkeypatch.setattr(trakGrab, "download", fake_download)
        trakGrab.grab("https://cdn/", {"id": 1, "src": "a.mp3", "name": "bare"}, tmp_path, skip_existing=False)
        assert seen["name"] == "bare.mp3"

    def test_single_mode_over_an_existing_file_gets_a_suffix(self, tmp_path, monkeypatch):
        (tmp_path / "mel - hit.mp3").write_bytes(b"existing")

        def fake_download(_url, target):
            target.write_bytes(b"x")
            return True

        monkeypatch.setattr(trakGrab, "download", fake_download)
        assert trakGrab.grab("https://cdn/", self.TRACK, tmp_path, skip_existing=False) == "downloaded"
        assert (tmp_path / "mel - hit (1).mp3").exists()


COVER_URL = "https://img.example/78923/cover.jpg"
COVER_BYTES = b"\xff\xd8\xff\xe0fake-jpeg"


class TestCoverUrl:
    def test_the_original_image_is_preferred(self):
        track = {"imageOriginal": COVER_URL, "image": "https://img.example/80x80/78923/cover.jpg"}
        assert trakGrab.cover_url(track) == COVER_URL

    def test_the_thumbnail_is_the_fallback(self):
        assert trakGrab.cover_url({"imageOriginal": "", "image": COVER_URL}) == COVER_URL

    def test_no_usable_image_gives_an_empty_string(self):
        assert trakGrab.cover_url({"image": "/relative.jpg"}) == ""
        assert trakGrab.cover_url({}) == ""


class TestFetchCover:
    def test_an_image_is_returned_with_its_mime_type(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get(COVER_URL).respond(
            200, content=COVER_BYTES, headers={"Content-Type": "image/JPEG; charset=binary"}
        )
        assert trakGrab.fetch_cover(COVER_URL) == (COVER_BYTES, "image/jpeg")

    def test_no_url_means_no_request(self):
        assert trakGrab.fetch_cover("") is None

    def test_an_http_error_gives_none(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get(COVER_URL).respond(404)
        assert trakGrab.fetch_cover(COVER_URL) is None

    def test_a_non_image_response_gives_none(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get(COVER_URL).respond(200, text="<html>error</html>")
        assert trakGrab.fetch_cover(COVER_URL) is None

    def test_an_empty_image_gives_none(self, _fresh_client, respx_mock_scope):
        respx_mock_scope.get(COVER_URL).respond(200, content=b"", headers={"Content-Type": "image/jpeg"})
        assert trakGrab.fetch_cover(COVER_URL) is None


class TestTagMp3:
    """Tags are written for real with mutagen, into small stand-in files."""

    TRACK = {"name": "hit ", "artist": "mel", "imageOriginal": COVER_URL}
    AUDIO = b"\xff\xfb\x90\x00" + b"\x00" * 64  # stand-in for MPEG frames

    @pytest.fixture(autouse=True)
    def _cover_route(self, _fresh_client, respx_mock_scope):
        self.route = respx_mock_scope.get(COVER_URL).respond(
            200, content=COVER_BYTES, headers={"Content-Type": "image/jpeg"}
        )

    def _mp3(self, tmp_path, data=None):
        dest = tmp_path / "mel - hit.mp3"
        dest.write_bytes(self.AUDIO if data is None else data)
        return dest

    def test_title_artist_and_cover_are_written(self, tmp_path):
        dest = self._mp3(tmp_path)
        assert trakGrab.tag_mp3(dest, self.TRACK) is True
        tags = ID3(dest)
        assert str(tags["TIT2"]) == "hit"
        assert str(tags["TPE1"]) == "mel"
        (apic,) = tags.getall("APIC")
        assert apic.data == COVER_BYTES
        assert apic.mime == "image/jpeg"
        assert apic.type == 3  # front cover
        assert dest.read_bytes().endswith(self.AUDIO)  # the audio is untouched

    def test_an_existing_tag_keeps_its_other_frames(self, tmp_path):
        dest = self._mp3(tmp_path)
        existing = ID3()
        existing.add(TENC(encoding=3, text="LAME in FL Studio 2025"))
        existing.save(dest)
        trakGrab.tag_mp3(dest, self.TRACK)
        tags = ID3(dest)
        assert str(tags["TENC"]) == "LAME in FL Studio 2025"
        assert len(tags.getall("APIC")) == 1

    def test_without_a_cover_title_and_artist_are_still_written(self, tmp_path):
        dest = self._mp3(tmp_path)
        assert trakGrab.tag_mp3(dest, {"name": "hit", "artist": "mel"}) is False
        tags = ID3(dest)
        assert str(tags["TIT2"]) == "hit"
        assert not tags.getall("APIC")

    def test_a_track_without_names_writes_no_text_frames(self, tmp_path):
        dest = self._mp3(tmp_path)
        assert trakGrab.tag_mp3(dest, {"imageOriginal": COVER_URL}) is True
        tags = ID3(dest)
        assert not tags.getall("TIT2")
        assert not tags.getall("TPE1")

    def test_other_formats_are_left_alone(self, tmp_path):
        dest = tmp_path / "mel - hit.wav"
        dest.write_bytes(b"RIFF....WAVE")
        assert trakGrab.tag_mp3(dest, self.TRACK) is False
        assert dest.read_bytes() == b"RIFF....WAVE"
        assert not self.route.called

    def test_a_tag_error_is_swallowed(self, tmp_path, monkeypatch):
        def broken(*_args):
            raise MutagenError("unreadable tag")

        monkeypatch.setattr(trakGrab, "ID3", broken)
        dest = self._mp3(tmp_path)
        assert trakGrab.tag_mp3(dest, self.TRACK) is False
        assert dest.read_bytes() == self.AUDIO


class TestGrabTagging:
    """How grab() reports tagging, and Ctrl+C while the tag is written."""

    TRACK = {"id": 1, "src": "78923/a.mp3", "name": "hit", "artist": "mel"}

    @pytest.fixture(autouse=True)
    def _fake_download(self, monkeypatch):
        monkeypatch.setattr(trakGrab, "download", lambda _u, t: (t.write_bytes(b"x"), True)[1])

    def test_a_cover_is_reported(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(trakGrab, "tag_mp3", lambda _d, _t: True)
        trakGrab.grab("https://cdn/", dict(self.TRACK), tmp_path, skip_existing=True)
        assert "cover art added" in capsys.readouterr().out

    def test_a_missing_cover_is_reported(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(trakGrab, "tag_mp3", lambda _d, _t: False)
        assert trakGrab.grab("https://cdn/", dict(self.TRACK), tmp_path, skip_existing=True) == "downloaded"
        assert "no cover art available" in capsys.readouterr().out

    def test_a_ctrl_c_while_tagging_removes_the_file(self, tmp_path, monkeypatch):
        def interrupted(_dest, _track):
            raise KeyboardInterrupt

        monkeypatch.setattr(trakGrab, "tag_mp3", interrupted)
        with pytest.raises(KeyboardInterrupt):
            trakGrab.grab("https://cdn/", dict(self.TRACK), tmp_path, skip_existing=True)
        assert not (tmp_path / "mel - hit.mp3").exists()


class TestMain:
    """The interactive entry point, scripted end to end.

    main() derives the output directory from ``trakGrab.__file__``, so the
    tests point that at tmp_path instead of trying to patch Path.parent
    (a read-only property).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(trakGrab, "__file__", str(tmp_path / "trakGrab.py"))
        monkeypatch.setattr(trakGrab, "download", lambda _u, t: (t.write_bytes(b"x"), True)[1])
        self.tmp_path = tmp_path

    def _inputs(self, monkeypatch, *answers):
        it = iter(answers)
        monkeypatch.setattr("builtins.input", lambda _p="": next(it))

    def test_full_run_downloads_everything(self, monkeypatch, capsys):
        self._inputs(monkeypatch, "uq", "*")
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: ("https://cdn.example/", [dict(TRACK_FIXTURE)]))

        trakGrab.main()

        out = capsys.readouterr().out
        assert "Found 1 track(s)" in out
        assert "1 downloaded, 0 already existed, 0 failed" in out
        assert (self.tmp_path / "songs" / "uq" / "mel - hit.mp3").exists()

    def test_a_single_song_flow(self, monkeypatch, capsys):
        self._inputs(monkeypatch, "uq", "hit")
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: ("https://cdn.example/", [dict(TRACK_FIXTURE)]))

        trakGrab.main()
        out = capsys.readouterr().out
        assert "Saved to:" in out
        assert (self.tmp_path / "songs" / "uq" / "mel - hit.mp3").exists()

    def test_a_single_song_by_full_label_also_matches(self, monkeypatch, capsys):
        self._inputs(monkeypatch, "uq", "mel - hit")
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: ("https://cdn.example/", [dict(TRACK_FIXTURE)]))

        trakGrab.main()
        assert "Saved to:" in capsys.readouterr().out

    def test_the_banner_prints_the_tool_name(self, monkeypatch, capsys):
        self._inputs(monkeypatch, "uq", "mel - hit")
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: ("https://cdn.example/", [dict(TRACK_FIXTURE)]))
        trakGrab.main()
        assert "trakGrab" in capsys.readouterr().out

    def test_an_unknown_song_lists_the_tracks(self, monkeypatch, capsys):
        self._inputs(monkeypatch, "uq", "nope")
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: ("https://cdn.example/", [dict(TRACK_FIXTURE)]))

        with pytest.raises(SystemExit) as exc:
            trakGrab.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "not found. Available tracks:" in out
        assert "mel - hit" in out

    def test_an_empty_artist_exits(self, monkeypatch):
        self._inputs(monkeypatch, "", "")
        with pytest.raises(SystemExit) as exc:
            trakGrab.main()
        assert "No artist given" in str(exc.value)

    def test_a_404_reports_the_artist_missing(self, monkeypatch):
        self._inputs(monkeypatch, "ghost", "*")

        def raise_404(_url, **_kw):
            request = httpx.Request("GET", "https://traktrain.com/ghost")
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

        monkeypatch.setattr(trakGrab, "scrape_artist", raise_404)

        with pytest.raises(SystemExit) as exc:
            trakGrab.main()
        assert "not found on traktrain.com" in str(exc.value)

    def test_another_http_error_reports_the_code(self, monkeypatch):
        self._inputs(monkeypatch, "uq", "*")

        def raise_503(_url, **_kw):
            request = httpx.Request("GET", "https://traktrain.com/uq")
            raise httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))

        monkeypatch.setattr(trakGrab, "scrape_artist", raise_503)

        with pytest.raises(SystemExit) as exc:
            trakGrab.main()
        assert "HTTP 503" in str(exc.value)

    def test_a_connection_error_reports_the_reason(self, monkeypatch):
        self._inputs(monkeypatch, "uq", "*")

        def raise_net(_url, **_kw):
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(trakGrab, "scrape_artist", raise_net)

        with pytest.raises(SystemExit) as exc:
            trakGrab.main()
        assert "Connection failed" in str(exc.value)

    def test_a_structural_scrape_error_is_reported_without_traceback(self, monkeypatch):
        self._inputs(monkeypatch, "weird", "*")

        def raise_scrape(_url):
            raise trakGrab.ScrapeError("page changed")

        monkeypatch.setattr(trakGrab, "scrape_artist", raise_scrape)

        with pytest.raises(SystemExit) as exc:
            trakGrab.main()
        assert "page changed" in str(exc.value)

    def test_an_artist_with_no_tracks_exits(self, monkeypatch):
        self._inputs(monkeypatch, "empty", "*")
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: ("https://cdn.example/", []))

        with pytest.raises(SystemExit) as exc:
            trakGrab.main()
        assert "No tracks found" in str(exc.value)

    def test_a_full_profile_url_is_accepted_as_input(self, monkeypatch, capsys):
        self._inputs(monkeypatch, "https://traktrain.com/uq", "*")
        seen = {}
        monkeypatch.setattr(
            trakGrab,
            "scrape_artist",
            lambda artist: (seen.__setitem__("artist", artist), ("https://cdn.example/", [dict(TRACK_FIXTURE)]))[1],
        )

        trakGrab.main()
        assert seen["artist"] == "uq"

    def test_track_links_skip_the_song_prompt(self, monkeypatch, capsys):
        """One line with links, more pasted lines, an empty line to start."""
        self._inputs(
            monkeypatch,
            "https://traktrain.com/t/1 uq",
            "https://traktrain.com/t/2",
            "https://traktrain.com/t/1",  # pasted twice: downloaded once
            "",
        )
        seen = []

        def fake_scrape(track_id):
            seen.append(track_id)
            track = {**LINK_TRACK, "id": int(track_id), "src": f"78923/{track_id}.mp3", "name": f"n{track_id}"}
            return "https://cdn.example/", track

        monkeypatch.setattr(trakGrab, "scrape_track", fake_scrape)
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: pytest.fail("artist mode must not run"))

        trakGrab.main()

        assert seen == ["1", "2"]
        out = capsys.readouterr().out
        assert "Not a track link, skipped: uq" in out
        assert "2 track link(s)" in out
        assert "2 downloaded" in out
        assert (self.tmp_path / "songs" / "uq" / "mel - n1.mp3").exists()
        assert (self.tmp_path / "songs" / "uq" / "mel - n2.mp3").exists()

    def test_a_whitespace_song_defaults_to_all(self, monkeypatch, capsys):
        """An empty song answer means '*', not 'match nothing'."""
        self._inputs(monkeypatch, "uq", "   ")
        monkeypatch.setattr(trakGrab, "scrape_artist", lambda _a: ("https://cdn.example/", [dict(TRACK_FIXTURE)]))

        trakGrab.main()
        assert "1 downloaded" in capsys.readouterr().out


class TestCtrlC:
    """The interrupt paths: mid-download cleanup and the __main__ guard."""

    def test_a_ctrl_c_during_download_removes_the_partial(self, tmp_path, monkeypatch):
        def interrupted_download(_url, target):
            target.write_bytes(b"partial")
            raise KeyboardInterrupt

        monkeypatch.setattr(trakGrab, "download", interrupted_download)
        track = {"id": 1, "src": "a.mp3", "name": "t"}
        with pytest.raises(KeyboardInterrupt):
            trakGrab.grab("https://cdn/", track, tmp_path, skip_existing=False)
        # The half-written file is gone; a later run will re-download.
        assert not (tmp_path / "t.mp3").exists()

    def test_the_entry_point_turns_ctrl_c_into_exit_130(self):
        """Running the real module: a Ctrl+C at the prompt becomes exit 130."""
        import subprocess

        repo = Path(__file__).resolve().parent.parent
        # builtins.input raises on the first prompt, simulating Ctrl+C there;
        # the __main__ guard is then what converts it into exit code 130.
        script = (
            "import builtins, sys, runpy\n"
            f"sys.path.insert(0, {str(repo)!r})\n"
            "def interrupt(prompt=''):\n"
            "    raise KeyboardInterrupt\n"
            "builtins.input = interrupt\n"
            "try:\n"
            f"    runpy.run_path({str(repo / 'trakGrab.py')!r}, run_name='__main__')\n"
            "except SystemExit as e:\n"
            "    print('EXIT', e.code)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=60,
        )
        assert "EXIT 130" in proc.stdout
