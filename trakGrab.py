#!/usr/bin/env python3
# trakGrab.py
# Original by Daniel Guilbert, Nawid Salehie (12.11.19 - 07.08.24)
# Modernized 2026 for the current traktrain.com markup (v2.2)
#
# Downloads the free preview MP3s of every published track on a
# traktrain.com producer profile, following all pagination pages.

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from lxml import html as lxml_html
from lxml.etree import ParserError

TRAKTRAIN = "https://traktrain.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
PAGE_CAP = 100  # safety cap for pagination
CHUNK = 64 * 1024  # download chunk size
MAX_NAME_LEN = 180  # keep filenames well under Windows' 255-char limit
DOWNLOAD_RETRIES = 3  # attempts per file

BASE_URL_RE = re.compile(r"var\s+AWS_BASE_URL\s*=\s*'([^']+)'")
PROFILE_TRACKS_RE = re.compile(r"/profile-tracks/(\d+)")
PROFILE_URL_RE = re.compile(r"traktrain\.com/([^/?#\s]+)", re.IGNORECASE)
# Tab is deliberately excluded so the whitespace collapse below can turn it
# into a space instead of gluing words together.
ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x08\x0b-\x1f]')
# Reserved Windows device names, case-insensitive: CON, PRN, AUX, COM1-9, LPT1-9.
RESERVED_NAMES_RE = re.compile(r"(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$")


class ScrapeError(Exception):
    """A scraping problem worth reporting without a traceback."""


# One httpx client for the whole run: connection reuse against the site and
# the CDN, HTTP/2 where CloudFront offers it, browser-like default headers.
# The CDN refuses requests without the Referer, so it is set globally.
_client: httpx.Client | None = None


def get_client() -> httpx.Client:
    """Return the shared client, creating it on first use."""
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Referer": TRAKTRAIN},
            timeout=60,
            follow_redirects=True,
            http2=True,
        )
    return _client


def close_client() -> None:
    """Close the shared client; safe to call more than once."""
    global _client
    if _client is not None:
        _client.close()
        _client = None


def fetch_text(url: str, *, ajax: bool = False) -> str:
    """GET a URL and return the decoded body, raising on HTTP errors."""
    headers = {"X-Requested-With": "XMLHttpRequest"} if ajax else None
    resp = get_client().get(url, headers=headers)
    resp.raise_for_status()
    return resp.text


def parse_tracks(html: str) -> list[dict[str, Any]]:
    """Extract every track's JSON payload from data-player-info attributes.

    Empty or whitespace-only input raises lxml's ParserError; callers rely on
    getting an empty list back for that instead (pagination feeds it blank
    pages as a matter of course).
    """
    if not html or not html.strip():
        return []
    try:
        tree = lxml_html.fromstring(html)
    except ParserError:
        return []
    tracks: list[dict[str, Any]] = []
    for node in tree.xpath("//*[@data-player-info]"):
        raw = node.get("data-player-info")
        try:
            info = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue  # attribute misformatted; skip rather than crash
        if not (isinstance(info, dict) and info.get("src")):
            continue
        # The play-button div also carries data-name="<artist> - <track>".
        # The JSON payload itself has no artist field, so this is where the
        # display name comes from (e.g. data-name="mel - candycrush").
        data_name = node.get("data-name")
        if isinstance(data_name, str) and " - " in data_name:
            artist, _, rest = data_name.partition(" - ")
            artist, rest = artist.strip(), rest.strip()
            if artist:
                info.setdefault("artist", artist)
            if not info.get("name") and rest:
                info["name"] = rest
        tracks.append(info)
    return tracks


def dedupe(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate tracks (same id or same src), keeping first occurrence."""
    seen: set[Any] = set()
    unique: list[dict[str, Any]] = []
    for track in tracks:
        key = track.get("id", track.get("src"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(track)
    return unique


def paginate(first_page_html: str) -> list[dict[str, Any]]:
    """Follow the ?page=N pagination of a producer profile, if it has one."""
    endpoint = PROFILE_TRACKS_RE.search(first_page_html)
    if not endpoint:
        return []
    base = TRAKTRAIN.rstrip("/")
    tracks: list[dict[str, Any]] = []
    for page in range(2, PAGE_CAP + 1):
        url = f"{base}{endpoint.group(0)}?page={page}"
        try:
            payload = json.loads(fetch_text(url, ajax=True))
            content = payload.get("content", "") if isinstance(payload, dict) else ""
        except (json.JSONDecodeError, httpx.HTTPError):
            break
        page_tracks = parse_tracks(content)
        if not page_tracks:
            break
        tracks.extend(page_tracks)
        print(f"  page {page}: {len(page_tracks)} more track(s)")
    return tracks


def extract_base_url(html: str) -> str:
    """Read the CDN base URL (var AWS_BASE_URL) from the profile page."""
    match = BASE_URL_RE.search(html)
    if not match:
        raise ScrapeError("Could not find the CDN base URL on the profile page.")
    return match.group(1)


def scrape_artist(artist: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (CDN base URL, deduplicated track list) for a producer.

    Raises httpx.HTTPStatusError (404 = unknown artist) or ScrapeError when
    the page structure is not what this scraper understands.
    """
    # httpx.URL percent-encodes the path, so spaces and unicode in an artist
    # slug work without importing urllib.parse. The combined path must keep
    # its leading slash or httpx rejects it.
    url = httpx.URL(TRAKTRAIN).copy_with(path=f"/{artist.strip('/')}")
    html = fetch_text(str(url))
    base_url = extract_base_url(html)
    tracks = parse_tracks(html) + paginate(html)
    return base_url, dedupe(tracks)


def display_name(track: dict[str, Any]) -> str:
    """Return 'artist - title' when the artist is known, else the bare title."""
    name = str(track.get("name", "?")).strip()
    artist = str(track.get("artist", "")).strip()
    if artist and not name.casefold().startswith(f"{artist} - ".casefold()):
        return f"{artist} - {name}"
    return name


def pick_song(tracks: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """Case-insensitive match on title or 'artist - title': exact, then substring."""
    wanted_low = wanted.casefold().strip()
    if not wanted_low:
        return None
    for track in tracks:
        titles = {str(track.get("name", "")).strip().casefold(), display_name(track).casefold()}
        if wanted_low in titles:
            return track
    for track in tracks:
        if wanted_low in str(track.get("name", "")).casefold() or wanted_low in display_name(track).casefold():
            return track
    return None


def sanitize(name: str) -> str:
    """Make a track name safe as a Windows filename without mangling it."""
    cleaned = ILLEGAL_CHARS_RE.sub("", str(name))
    # Trailing dots/spaces are dropped by Windows itself, so 'name.' and
    # 'name' would be the same file; stripping them here keeps that explicit.
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(". ")
    return cleaned[:MAX_NAME_LEN] or "untitled"


def unique_path(directory: Path, filename: str) -> Path:
    """Return a path that does not collide, adding (1), (2), ... if needed."""
    path = directory / filename
    stem, ext = os.path.splitext(filename)
    counter = 1
    while path.exists():
        path = directory / f"{stem} ({counter}){ext}"
        counter += 1
    return path


def safe_filename(directory: Path, label: str, ext: str) -> Path:
    """Sanitize a track label into a collision-free path inside directory.

    Windows silently reserves names like CON or COM1 in every directory, so
    those get an underscore suffix rather than a confusing failure later.
    """
    name = sanitize(label)
    if RESERVED_NAMES_RE.match(name):
        name = f"_{name}"
    filename = name + ext
    dest = directory / filename
    if dest.exists():
        dest = unique_path(directory, filename)
    return dest


def download(url: str, dest: Path) -> bool:
    """Stream a file to disk with a progress readout; retry, then clean up.

    Partial files are removed immediately -- on network errors *and* on
    Ctrl+C -- so a broken download can never linger and be mistaken for a
    complete one on the next run.
    """
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            with get_client().stream("GET", url, timeout=120) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with dest.open("wb") as out:
                    for chunk in resp.iter_bytes(CHUNK):
                        out.write(chunk)
                        done += len(chunk)
                        if total:
                            print(f"\r    {done * 100 // total:3d}% of {total} bytes", end="", flush=True)
            print()
            return True
        except (httpx.HTTPError, OSError) as exc:
            print(f"\n    attempt {attempt}/{DOWNLOAD_RETRIES} failed: {exc}")
            dest.unlink(missing_ok=True)
            if attempt < DOWNLOAD_RETRIES:
                time.sleep(attempt)
    return False


def grab(base_url: str, track: dict[str, Any], out_dir: Path, *, skip_existing: bool) -> str:
    """Download one track. Returns 'downloaded', 'skipped' or 'failed'.

    Ctrl+C during the transfer removes the partial file and re-raises, so an
    interrupted run never leaves a broken file behind that a later run would
    treat as already downloaded.
    """
    src = str(track.get("src", "")).strip()
    if not src:
        return "failed"
    url = src if src.startswith("http") else base_url + src.lstrip("/")

    # File names read like 'mel - 6Figures.mp3' when the artist is known.
    label = display_name(track) if track.get("artist") else str(track.get("name") or track.get("id") or "untitled")
    ext = os.path.splitext(src)[1] or ".mp3"
    dest = out_dir / (sanitize(label) + ext)

    if dest.exists():
        if skip_existing:
            # The file may belong to a *different* track that merely shares
            # the title. The bookkeeping marker distinguishes them: without
            # one (pre-marker file, or hand-placed), skip conservatively.
            if _same_track(dest, track.get("id"), src):
                print("    already exists, skipped")
                return "skipped"
            print("    same name, different track - saving with a suffix")
            dest = safe_filename(out_dir, label, ext)
        else:
            dest = safe_filename(out_dir, label, ext)
            print(f"    exists, saving as {dest.name}")

    print(f"    {url}")
    try:
        if download(url, dest):
            _remember_track(dest, track.get("id"), src)
            return "downloaded"
    except KeyboardInterrupt:
        dest.unlink(missing_ok=True)
        raise
    dest.unlink(missing_ok=True)  # drop partial file
    return "failed"


def _marker_path(dest: Path) -> Path:
    """Path of the invisible bookkeeping file for a downloaded track."""
    return dest.with_name(f".{dest.name}.trakid")


def _same_track(dest: Path, track_id: Any, src: str) -> bool:
    """True when the file at dest was downloaded from this exact track."""
    marker = _marker_path(dest)
    if not marker.exists():
        # No marker: predates bookkeeping, or hand-placed. Skipping is the
        # conservative choice -- it preserves the never-overwrite promise.
        return True
    try:
        return marker.read_text(encoding="utf-8").strip() == f"{track_id}|{src}"
    except (OSError, UnicodeDecodeError):
        # Unreadable marker: treat the file as its own thing rather than
        # overwriting something we cannot vouch for.
        return True


def _remember_track(dest: Path, track_id: Any, src: str) -> None:
    """Record which track a downloaded file came from (best effort)."""
    try:
        _marker_path(dest).write_text(f"{track_id}|{src}", encoding="utf-8")
    except OSError:
        pass  # bookkeeping must never break a successful download


def resolve_artist_input(raw: str) -> str:
    """Accept either a bare slug or a full profile URL; return the slug."""
    url_match = PROFILE_URL_RE.search(raw.strip())
    if url_match:
        return url_match.group(1)
    return raw.strip().strip("/")


def banner() -> str:
    """The startup line. Deliberately version-free, like the sibling repos.

    semantic-release owns the version in pyproject.toml; printing it here
    would only create a second copy that every release silently drifts.
    """
    return "trakGrab - downloads free previews from traktrain.com\n"


def main() -> None:
    try:
        print(banner())
        artist = resolve_artist_input(input("What is the artist name? traktrain.com/"))
        if not artist:
            sys.exit("No artist given.")
        wanted = input("Which song? (* for all) ").strip() or "*"

        print("\nConnecting...")
        try:
            base_url, tracks = scrape_artist(artist)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                sys.exit(f"Artist '{artist}' not found on traktrain.com.")
            sys.exit(f"traktrain returned HTTP {exc.response.status_code}.")
        except httpx.HTTPError as exc:
            sys.exit(f"Connection failed: {exc}")
        except ScrapeError as exc:
            sys.exit(str(exc))

        if not tracks:
            sys.exit(f"No tracks found for '{artist}'.")

        print(f"Connected! Found {len(tracks)} track(s).\n")

        # The artist string came off the network (profile URL slug) or the
        # keyboard, so it gets the same filename treatment as track names:
        # '..' would otherwise escape songs/, and a reserved name would fail
        # oddly at mkdir time.
        out_dir = Path(__file__).resolve().parent / "songs" / sanitize(artist)
        out_dir.mkdir(parents=True, exist_ok=True)

        if wanted != "*":
            track = pick_song(tracks, wanted)
            if track is None:
                print(f"Song '{wanted}' not found. Available tracks:")
                for t in tracks:
                    print("  -", display_name(t))
                sys.exit(1)
            grab(base_url, track, out_dir, skip_existing=False)
        else:
            stats = {"downloaded": 0, "skipped": 0, "failed": 0}
            for i, track in enumerate(tracks, 1):
                print(f"[{i}/{len(tracks)}] {display_name(track)}")
                stats[grab(base_url, track, out_dir, skip_existing=True)] += 1
            print(
                f"\nDone! {stats['downloaded']} downloaded, {stats['skipped']} already existed,"
                f" {stats['failed']} failed."
            )
        print(f"Saved to: {out_dir}")
    finally:
        close_client()


if __name__ == "__main__":  # pragma: no cover - exercised by running the script
    try:
        main()
    except KeyboardInterrupt:
        # A Ctrl+C mid-download raises inside download() too; its handler
        # already removed the partial file there. Here the message is all
        # that is left to do.
        print("\nAborted.")
        sys.exit(130)
