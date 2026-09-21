# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A small Windows tool (CustomTkinter GUI, same file doubles as a CLI) that downloads a clip
from a Zoetermeer municipal council (gemeenteraad) meeting video hosted on iBabs /
CompanyWebcast. Given an iBabs agenda URL it drives headless Chromium via Playwright to
intercept the signed HLS playlist URL, then downloads the segments in parallel and
remuxes them through FFmpeg into an MP4.

Dependencies live in the local `venv/` (Python 3.14): see `requirements.txt` and
`requirements-dev.txt`. FFmpeg comes from `imageio-ffmpeg`, no system install needed.

## Commands

Prefix with `venv\Scripts\` or activate the venv first (`venv\Scripts\activate`).

```powershell
# Fresh setup
python -m venv venv
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m playwright install chromium

# Tests (pure unit tests, no network)
venv\Scripts\python -m pytest
venv\Scripts\python -m pytest tests/test_hls.py -k select_segments

# Lint (only pyflakes is set up)
venv\Scripts\python -m pyflakes hls.py scraper.py main.py tests

# GUI
venv\Scripts\python main.py

# CLI (any argument switches to CLI mode). --url accepts an agenda page or a .m3u8 URL.
venv\Scripts\python main.py --url "https://zoetermeer.bestuurlijkeinformatie.nl/Agenda/Index/<guid>" --output clip.mp4 --start 00:15:00 --duration 00:05:00
venv\Scripts\python main.py --url <agenda-url> --output x.mp4 --print-m3u8   # only intercept the playlist URL

# Scraper smoke test against the live site (prints the signed playlist URL)
venv\Scripts\python scraper.py [agenda-url]
$env:RAAD_DEBUG=1; venv\Scripts\python scraper.py     # with per-request logging

# Windows build (output in dist/079RaadDownloader/)
venv\Scripts\pyinstaller main.spec
```

There is no integration test suite; anything touching the network is verified by hand
against a live agenda page. Signed playlist URLs expire (CloudFront `Policy` has a
`DateLessThan`), so a captured `.m3u8` URL is only good for a day or so.

## Architecture

Three modules, one pipeline: `scraper.get_m3u8_url` -> `hls.HLSDownloader` -> MP4.

**`scraper.py`** loads the agenda page in headless Chromium (browser-like user agent, the
CompanyWebcast player refuses to start otherwise), listens passively with
`page.on("request")` for the first URL containing `.m3u8`, keeps the `.cwc` player
scrolled into view and clicks the player's **"Start now"** button inside its iframe. The
player only requests the playlist in response to that click, so the click must land in
a *child* frame: the iBabs page's own search button has an accessible name that also
matches "start", which is why `_click_start` skips `page.frames[0]`. Whole run is 5-7 s.
A `.m3u8` URL passed in is returned unchanged. If Chromium is missing it is installed
through the bundled Node driver (works from the frozen exe too, where
`python -m playwright` does not exist). Set `RAAD_DEBUG=1` to log every request URL,
scroll and click to stderr.

**`hls.py`** is the downloader. `load_playlist` fetches the playlist (following a master
playlist to its highest-bandwidth variant), `parse_media_playlist` yields `Segment`s with
cumulative start times, key URI/IV and optional fMP4 init segment, `select_segments`
picks the segments overlapping the requested range and returns the offset into the first
one. `HLSDownloader.run` then starts FFmpeg reading MPEG-TS from stdin
(`-ss <offset> -t <clip> -c copy -bsf:a aac_adtstoasc -movflags +faststart`), fetches
segments with a `ThreadPoolExecutor` (16 connections by default, a window of 2x that in
flight), decrypts AES-128-CBC segments with `cryptography`, and writes them to FFmpeg's
stdin strictly in playlist order from a single writer thread. Progress is measured in
video seconds downloaded, so it is accurate even for "until the end" downloads. On
cancel or error the stdin pipe is closed (not killed) so FFmpeg writes the moov atom and
the partial file stays playable.

Measured on a 2h51m meeting (1030 x 10 s segments, 2.75 GB): 30 s end to end, ~340x
realtime; plain `ffmpeg -i <m3u8>` does ~90x because it fetches segments serially.

**`main.py`** hosts both front ends. `run_gui` runs scrape + download on one worker
thread and marshals every UI update through `App.ui` (`after(0, ...)`); Tk is not
thread-safe and all downloader callbacks fire on worker threads. `App.busy` guards
against late callbacks after a cancel. `run_cli` is used whenever `sys.argv` has
arguments; exit codes are 0 ok, 1 error, 2 bad arguments, 130 interrupted.

Segment URLs in these playlists are root-relative (`/<id>/<n>.ts`) and are fetched
*without* the signed query string; only the playlist itself is signed. CloudFront blocks
the default `curl` user agent but accepts browsers and FFmpeg's `Lavf`.

## Packaging notes

- `main.spec` is tracked (the `*.spec` gitignore line is commented out). It uses
  `collect_all` for customtkinter, playwright and imageio_ffmpeg so the Playwright
  driver and the FFmpeg binary ship in the bundle. Chromium does not; first run
  downloads it to `%LOCALAPPDATA%\ms-playwright`.
- `console=True` on purpose: the exe is also the CLI.
- Python 3.14.7 ships Tcl/Tk 9 with its library embedded in the DLLs (zipfs). PyInstaller
  6.19 fails at exe startup with "Tcl data directory _tcl_data not found"; 6.22+ works.
  `dist/main/` and `build/main/` are leftovers from the pre-rewrite build (gitignored).

## Reference files (not code)

`agendavideo.js`, `cwc_client.js` (and `_utf8` copies), `stream_page.html` and
`screenshot.png` are captures from the iBabs portal kept for reverse engineering the
player. `cwc_client_utf8.js` is actually a CloudFront 403 page. Nothing imports them.
