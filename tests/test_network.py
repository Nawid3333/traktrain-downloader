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
