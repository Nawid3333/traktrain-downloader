## trakGrab

trakGrab is a Python 3 script that downloads the free preview MP3s of
every published track on a traktrain.com producer profile.
Works on Windows, macOS and Linux.

__DEPENDENCIES__

- Python 3.10+ (uses stdlib + BeautifulSoup only)
- BeautifulSoup 4 — install via `python -m pip install beautifulsoup4`

__USAGE__

Simply run the script from the command line with `py trakGrab.py`.
The script prompts the user for an artist (their traktrain URL) and for
a beat to download. The wildcard character (`*`) can be used to download
all of an artist's available beats. A full profile URL like
`https://traktrain.com/uq` is accepted as artist input as well.

Beats are downloaded to `$PWD\songs\artistName\songName.mp3` where `$PWD`
is the location of the script. Existing files are never overwritten:
re-runs skip tracks that are already downloaded ("modded against double
naming" behavior kept and improved).

__WHAT CHANGED IN v2.0__

- traktrain.com moved its track metadata into `data-player-info` JSON
  attributes whose first key is no longer `name`; the old regex- and
  string-splitting parsing failed on it. trakGrab now parses the
  attribute as real JSON via BeautifulSoup.
- Switched from `http://www.traktrain.com` to `https://traktrain.com`.
- Follows profile pagination (`/profile-tracks/<id>?page=N`) so artists
  with more tracks than fit on the first page are fully scraped.
- The CDN (`*.cloudfront.net`) only serves files with a `Referer` header
  of `https://traktrain.com/`; this is set on every request.
- Updated the User-Agent to a current Chrome version.
- Downloads stream in 64 KB chunks with a progress readout and are
  retried up to 3 times on network errors; partial files are removed.
- Track names are sanitized for Windows-illegal characters and long
  names are truncated, keeping the (1)/(2) collision suffixes.

No rights reserved.

##### -dg 12.11.19 / -ns 07.08.24 / modernized 09.2026
