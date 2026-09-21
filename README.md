# trakGrab

A small, terminal-driven Python tool that downloads the free preview MP3s of
every published track on a [traktrain.com](https://traktrain.com) producer
profile — with pagination support, resumable re-runs and Windows-safe
filenames.

> **Status:** personal project, tested on Windows with Python 3.14 against
> traktrain.com's September 2026 markup. Contributions are welcome.

---

## What it does

- Scrapes a producer profile (`traktrain.com/<artist>`) and extracts every
  track's metadata from the page's embedded `data-player-info` JSON
- Follows profile pagination (`/profile-tracks/<id>?page=N`) so artists with
  more tracks than fit on one page are scraped completely
- Downloads every free preview MP3 from traktrain's CDN (`*.cloudfront.net`),
  which only serves files with a proper `Referer` header
- Never overwrites: re-runs skip tracks that are already downloaded, so an
  interrupted or repeated run simply picks up where it left off
- Alternatively downloads a single track by name (case-insensitive, with
  substring matching and a listing of available tracks on a miss)

## Quick start

### 1. Clone and install dependencies

```bash
git clone https://github.com/Nawid3333/traktrain-downloader
cd traktrain-downloader
python -m pip install -r requirements.txt
```

### 2. Run it

```bash
python trakGrab.py
```

The script prompts for an artist (the part after `traktrain.com/`, or a full
profile URL like `https://traktrain.com/uq`) and for a song. The wildcard
character (`*`) downloads all of the artist's available beats.

### 3. Optional: install it as a command

The project builds as a wheel and installs a `trakgrab` command:

```bash
pip install .
trakgrab
```

### Output

Beats land in `songs/<artist>/<artist> - <track name>.mp3` next to the
script — file names read like `mel - 6Figures.mp3`, exactly as traktrain
labels them on the profile page. Rare name collisions get `(1)`, `(2)`
suffixes.

## How it works

```
trakGrab.py ── GET https://traktrain.com/<artist> (browser User-Agent)
       │
       ├─ var AWS_BASE_URL  ──►  CDN base URL (cloudfront)
       ├─ div[data-player-info]  ──►  JSON: name, src, id, bpm, …
       └─ /profile-tracks/<id>?page=N  ──►  more pages of the same
                │
                ▼
     GET <AWS_BASE_URL>/<src>   Referer: https://traktrain.com/
                │
                ▼
     songs/<artist>/<artist> - <sanitized track name>.mp3
```

The parsing deliberately uses BeautifulSoup + `json.loads` on the
`data-player-info` attribute instead of string splitting: traktrain changed
the JSON's key order at some point (the first key is `prices` now, not
`name`), which silently broke regex-based scrapers. The artist display name
lives only in the same div's `data-name="<artist> - <track>"` attribute —
the JSON payload itself carries just the producer link — and is used to
prefix every file name.

## Project layout

```text
traktrain-downloader/
├── trakGrab.py          # The whole tool: scraping, pagination, downloads
├── pyproject.toml       # Packaging metadata; `pip install .` gives `trakgrab`
├── requirements.txt     # Runtime dependencies (beautifulsoup4)
├── LICENSE              # GPLv3
├── .gitignore
└── songs/               # Downloaded beats (ignored by git)
```

## License

This project is licensed under the **GNU General Public License v3.0 or
later** — the same license used for the rest of this repository's Python
tools. See [LICENSE](LICENSE) for the full text.

---

## Roadmap / known limitations

- Downloads the free 128 kbps preview streams, not the purchased files —
  bought tracks require an account and are intentionally out of scope.
- A producer's non-first pages are fetched one by one; there is no parallelism
  (deliberately gentle on traktrain's servers).
- Drum kits and other non-audio products are not scraped; only tracks with an
  MP3/preview `src` are listed.

---

Happy beat hunting! 🎧
