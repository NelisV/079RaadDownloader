"""HLS playlist parsing and a parallel segment downloader that remuxes through FFmpeg.

Pipeline: fetch playlist -> select the segments covering [start, start+duration) ->
download those segments with N concurrent connections (decrypting AES-128 where the
playlist asks for it) -> write them *in order* to FFmpeg's stdin -> FFmpeg trims the
exact start offset / duration and stream-copies into an MP4.

Only the writer thread talks to FFmpeg, so segments arrive in order no matter how the
network reorders them. A bounded window of in-flight downloads keeps memory flat.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin

import imageio_ffmpeg
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.adapters import HTTPAdapter

# CloudFront in front of sdk.companywebcast.com rejects curl's user agent but accepts
# browsers and FFmpeg's default; we always identify as a normal desktop browser.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_WORKERS = 16
SEGMENT_RETRIES = 6
REQUEST_TIMEOUT = (10, 60)  # connect, read (seconds)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- time helpers


def parse_time(value: str | float | int) -> float:
    """Parse ``H:MM:SS(.fff)``, ``MM:SS``, ``SS`` or ``1h30m15s`` into seconds.

    Raises ``ValueError`` for anything else (including negative values).
    """
    if isinstance(value, (int, float)):
        secs = float(value)
    else:
        text = value.strip().lower()
        if not text:
            raise ValueError("empty time")
        if ":" in text:
            parts = text.split(":")
            if len(parts) > 3 or any(p == "" for p in parts):
                raise ValueError(f"invalid time: {value!r}")
            nums = [float(p) for p in parts]
            secs = 0.0
            for n in nums:
                secs = secs * 60 + n
        elif re.fullmatch(r"(\d+h)?(\d+m)?(\d+(\.\d+)?s?)?", text) and re.search(r"[hms]", text):
            secs = 0.0
            for amount, unit in re.findall(r"(\d+(?:\.\d+)?)([hms])", text):
                secs += float(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
        else:
            secs = float(text)
    if secs < 0:
        raise ValueError("time must not be negative")
    return secs


def format_time(secs: float) -> str:
    """Seconds -> ``H:MM:SS`` (hours not zero-padded, so 4h+ meetings render fine)."""
    secs = max(0, int(round(secs)))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


# --------------------------------------------------------------------------- playlist model


@dataclass
class Segment:
    uri: str
    duration: float
    start: float  # cumulative start time within the playlist (seconds)
    seq: int  # media sequence number (used as IV when the key tag has none)
    key_uri: Optional[str] = None
    iv: Optional[bytes] = None
    map_uri: Optional[str] = None  # EXT-X-MAP init segment (fMP4 streams)

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Playlist:
    url: str
    segments: list[Segment] = field(default_factory=list)
    is_vod: bool = False

    @property
    def total_duration(self) -> float:
        return self.segments[-1].end if self.segments else 0.0


class PlaylistError(Exception):
    pass


def _parse_attrs(attr_text: str) -> dict[str, str]:
    """Parse an HLS attribute list: ``KEY=VALUE,KEY="quoted, value"``."""
    attrs = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)', attr_text):
        key, val = m.group(1), m.group(2)
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        attrs[key] = val
    return attrs


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def select_variant(text: str, base_url: str) -> str:
    """Return the absolute URL of the highest-bandwidth variant in a master playlist."""
    best_bw, best_uri = -1, None
    lines = [ln.strip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            bw = int(attrs.get("AVERAGE-BANDWIDTH") or attrs.get("BANDWIDTH") or 0)
            # URI is the next non-empty, non-tag line
            for nxt in lines[i + 1 :]:
                if nxt and not nxt.startswith("#"):
                    if bw > best_bw:
                        best_bw, best_uri = bw, nxt
                    break
    if best_uri is None:
        raise PlaylistError("master playlist contains no variant streams")
    return urljoin(base_url, best_uri)


def parse_media_playlist(text: str, base_url: str) -> Playlist:
    if "#EXTM3U" not in text:
        raise PlaylistError("not an M3U8 playlist (missing #EXTM3U)")
    if is_master_playlist(text):
        raise PlaylistError("expected a media playlist but got a master playlist")

    pl = Playlist(url=base_url)
    seq = 0
    key_uri: Optional[str] = None
    key_iv: Optional[bytes] = None
    map_uri: Optional[str] = None
    pending_duration: Optional[float] = None
    clock = 0.0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            if method == "NONE":
                key_uri, key_iv = None, None
            elif method == "AES-128":
                key_uri = urljoin(base_url, attrs["URI"])
                iv_text = attrs.get("IV")
                key_iv = bytes.fromhex(iv_text[2:] if iv_text.lower().startswith("0x") else iv_text) if iv_text else None
            else:
                raise PlaylistError(f"unsupported encryption method {method} (only AES-128 is supported)")
        elif line.startswith("#EXT-X-MAP:"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            map_uri = urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXTINF:"):
            pending_duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line.startswith("#EXT-X-ENDLIST"):
            pl.is_vod = True
        elif line.startswith("#"):
            continue  # other tags are irrelevant for downloading
        else:
            if pending_duration is None:
                raise PlaylistError(f"segment without #EXTINF: {line}")
            pl.segments.append(
                Segment(
                    uri=urljoin(base_url, line),
                    duration=pending_duration,
                    start=clock,
                    seq=seq,
                    key_uri=key_uri,
                    iv=key_iv,
                    map_uri=map_uri,
                )
            )
            clock += pending_duration
            seq += 1
            pending_duration = None

    if not pl.segments:
        raise PlaylistError("playlist contains no segments")
    return pl


def select_segments(playlist: Playlist, start_secs: float, duration_secs: Optional[float]) -> tuple[list[Segment], float, Optional[float]]:
    """Pick the segments overlapping ``[start, start+duration)``.

    Returns ``(segments, offset_into_first_segment, clip_duration)``. ``duration_secs``
    of ``None``/``0`` means "until the end". ``clip_duration`` is the exact length to
    hand to FFmpeg (``None`` when running to the end).
    """
    total = playlist.total_duration
    if start_secs >= total:
        raise ValueError(f"start time {format_time(start_secs)} is beyond the end of the video ({format_time(total)})")
    if not duration_secs or duration_secs <= 0:
        end_secs = total
        clip_duration = None
    else:
        end_secs = min(total, start_secs + duration_secs)
        clip_duration = end_secs - start_secs
    # small epsilon so that an end exactly on a boundary does not pull in the next segment
    chosen = [s for s in playlist.segments if s.end > start_secs + 1e-6 and s.start < end_secs - 1e-6]
    if not chosen:
        raise ValueError("no segments cover the requested range")
    offset = max(0.0, start_secs - chosen[0].start)
    return chosen, offset, clip_duration


# --------------------------------------------------------------------------- HTTP


def make_session(workers: int = DEFAULT_WORKERS, user_agent: str = USER_AGENT) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=max(workers * 2, 8))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_bytes(session: requests.Session, url: str, retries: int = SEGMENT_RETRIES, cancel: Optional[threading.Event] = None) -> bytes:
    """GET ``url`` with exponential backoff. Raises the last error after ``retries`` attempts."""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.content
            if resp.status_code in (403, 404, 410):
                # Not transient: signed URL expired / wrong path. Fail fast.
                raise requests.HTTPError(f"HTTP {resp.status_code} for {url.split('?')[0]}")
            last_err = requests.HTTPError(f"HTTP {resp.status_code} for {url.split('?')[0]}")
        except requests.HTTPError:
            raise
        except (requests.RequestException, OSError) as e:
            last_err = e
        # backoff: 0.5, 1, 2, 4, 8 ... capped
        delay = min(8.0, 0.5 * (2**attempt))
        if cancel is not None:
            if cancel.wait(delay):
                raise DownloadCancelled()
        else:
            time.sleep(delay)
    raise DownloadError(f"giving up on {url.split('?')[0]} after {retries} attempts: {last_err}")


def load_playlist(url: str, session: Optional[requests.Session] = None) -> Playlist:
    """Fetch ``url``; if it is a master playlist, follow the best variant."""
    session = session or make_session()
    try:
        text = fetch_bytes(session, url, retries=4).decode("utf-8", errors="replace")
    except requests.HTTPError as e:
        if "403" in str(e):
            raise PlaylistError(
                "The stream server refused the playlist (HTTP 403). Signed stream links expire after a day; "
                "paste the iBabs agenda page URL instead so a fresh link is fetched."
            ) from e
        raise
    if is_master_playlist(text):
        url = select_variant(text, url)
        text = fetch_bytes(session, url, retries=4).decode("utf-8", errors="replace")
    return parse_media_playlist(text, url)


# --------------------------------------------------------------------------- crypto


def decrypt_aes128(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(key) != 16:
        raise DownloadError(f"AES-128 key must be 16 bytes, got {len(key)}")
    if len(data) % 16:
        raise DownloadError("encrypted segment length is not a multiple of 16 bytes (truncated download?)")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    # PKCS#7 padding (mandated by the HLS spec for AES-128 segments)
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    return plain


def iv_for(segment: Segment) -> bytes:
    return segment.iv if segment.iv is not None else segment.seq.to_bytes(16, "big")


# --------------------------------------------------------------------------- downloader


class DownloadError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


@dataclass
class Progress:
    fraction: float  # 0..1 of the clip's video time downloaded
    done_secs: float
    total_secs: float
    done_bytes: int
    speed_bps: float  # bytes / second, recent window
    eta_secs: Optional[float]
    segments_done: int
    segments_total: int

    def summary(self) -> str:
        eta = f", ETA {format_time(self.eta_secs)}" if self.eta_secs is not None else ""
        return (
            f"{self.fraction * 100:5.1f}%  {format_time(self.done_secs)} / {format_time(self.total_secs)}"
            f"  {format_bytes(self.done_bytes)} at {format_bytes(self.speed_bps)}/s{eta}"
        )


class HLSDownloader:
    """Download a time range of an HLS VOD into an MP4 as fast as the network allows.

    Callbacks (all optional, invoked on the downloader's worker thread, *not* the UI thread):
      status_callback(str)           human readable stage updates
      progress_callback(Progress)
      on_complete_callback(str)      output path
      on_error_callback(str)         message; a partial file may remain on disk
      on_cancel_callback(str)        message; the partial (but playable) file is kept
    """

    def __init__(
        self,
        m3u8_url: str,
        output_path: str,
        start_time: str | float = 0,
        duration: str | float | None = None,
        workers: int = DEFAULT_WORKERS,
        faststart: bool = True,
        user_agent: str = USER_AGENT,
    ):
        self.m3u8_url = m3u8_url
        self.output_path = output_path
        self.start_secs = parse_time(start_time) if start_time not in (None, "") else 0.0
        self.duration_secs: Optional[float] = parse_time(duration) if duration not in (None, "") else None
        if self.duration_secs == 0:
            self.duration_secs = None
        self.workers = max(1, int(workers))
        self.faststart = faststart
        self.user_agent = user_agent

        self.status_callback: Optional[Callable[[str], None]] = None
        self.progress_callback: Optional[Callable[[Progress], None]] = None
        self.on_complete_callback: Optional[Callable[[str], None]] = None
        self.on_error_callback: Optional[Callable[[str], None]] = None
        self.on_cancel_callback: Optional[Callable[[str], None]] = None

        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._ffmpeg_stderr: list[str] = []
        self.playlist: Optional[Playlist] = None
        self.last_progress: Optional[Progress] = None

    # -- public API -----------------------------------------------------------------

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="hls-download", daemon=True)
        self._thread.start()
        return self._thread

    def cancel(self):
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def join(self, timeout: Optional[float] = None):
        if self._thread:
            self._thread.join(timeout)

    def run(self):
        """Blocking entry point; dispatches to the callbacks and never raises."""
        try:
            self._run()
        except DownloadCancelled:
            self._finish_ffmpeg(graceful=True)
            self._emit(self.on_cancel_callback, f"Cancelled. Partial file kept: {self.output_path}")
        except Exception as e:  # noqa: BLE001 - report everything to the UI
            self._finish_ffmpeg(graceful=True)
            detail = self._ffmpeg_error_tail()
            self._emit(self.on_error_callback, f"{e}{detail}")

    # -- internals ------------------------------------------------------------------

    def _emit(self, cb, *args):
        if cb:
            cb(*args)

    def _status(self, msg: str):
        self._emit(self.status_callback, msg)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise DownloadCancelled()

    def _run(self):
        session = make_session(self.workers, self.user_agent)

        self._status("Fetching playlist...")
        self.playlist = load_playlist(self.m3u8_url, session)
        segments, offset, clip_duration = select_segments(self.playlist, self.start_secs, self.duration_secs)
        total_secs = clip_duration if clip_duration is not None else (segments[-1].end - self.start_secs)
        self._status(
            f"Stream is {format_time(self.playlist.total_duration)} long; downloading {format_time(total_secs)} "
            f"({len(segments)} segments, {self.workers} connections)..."
        )
        self._check_cancel()

        # Keys and init segments are shared by many segments: fetch each once, up front.
        keys: dict[str, bytes] = {}
        for uri in {s.key_uri for s in segments if s.key_uri}:
            keys[uri] = fetch_bytes(session, uri, cancel=self._cancel)
        maps: dict[str, bytes] = {}
        for uri in {s.map_uri for s in segments if s.map_uri}:
            maps[uri] = fetch_bytes(session, uri, cancel=self._cancel)

        self._start_ffmpeg(offset, clip_duration, fmp4=bool(maps))
        stdin = self._proc.stdin
        assert stdin is not None

        def fetch(seg: Segment) -> bytes:
            data = fetch_bytes(session, seg.uri, cancel=self._cancel)
            if seg.key_uri:
                data = decrypt_aes128(data, keys[seg.key_uri], iv_for(seg))
            return data

        window = self.workers * 2
        done_secs = 0.0
        done_bytes = 0
        recent: deque[tuple[float, int]] = deque()  # (timestamp, bytes) for speed estimate
        t0 = time.monotonic()
        last_emit = 0.0
        written_map: Optional[str] = None

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hls-fetch") as pool:
            futures: dict[int, Future] = {}
            try:
                for idx, seg in enumerate(segments):
                    # keep the window full
                    while len(futures) < window and (idx + len(futures)) < len(segments):
                        nxt = idx + len(futures)
                        if nxt in futures:
                            break
                        futures[nxt] = pool.submit(fetch, segments[nxt])
                    # pull the next in-order segment
                    fut = futures.pop(idx)
                    data = fut.result()  # raises DownloadError / DownloadCancelled from the worker
                    self._check_cancel()

                    if seg.map_uri and seg.map_uri != written_map:
                        stdin.write(maps[seg.map_uri])
                        written_map = seg.map_uri
                    try:
                        stdin.write(data)
                    except (BrokenPipeError, OSError) as e:
                        raise DownloadError(f"FFmpeg stopped accepting data: {e}")

                    # progress bookkeeping
                    done_bytes += len(data)
                    done_secs += seg.duration
                    now = time.monotonic()
                    recent.append((now, len(data)))
                    while recent and now - recent[0][0] > 5.0:
                        recent.popleft()
                    if now - last_emit >= 0.25 or idx == len(segments) - 1:
                        last_emit = now
                        span = max(now - recent[0][0], 1e-3) if len(recent) > 1 else max(now - t0, 1e-3)
                        speed = sum(b for _, b in recent) / span
                        remaining_secs = max(0.0, total_secs - min(done_secs, total_secs))
                        bytes_per_video_sec = done_bytes / max(done_secs, 1e-3)
                        eta = (remaining_secs * bytes_per_video_sec / speed) if speed > 0 else None
                        self.last_progress = Progress(
                            fraction=min(1.0, done_secs / max(total_secs, 1e-3)),
                            done_secs=min(done_secs, total_secs),
                            total_secs=total_secs,
                            done_bytes=done_bytes,
                            speed_bps=speed,
                            eta_secs=eta,
                            segments_done=idx + 1,
                            segments_total=len(segments),
                        )
                        self._emit(self.progress_callback, self.last_progress)
            except BaseException:
                # stop scheduling new work; running fetches see the cancel flag on their next retry
                self._cancel.set()
                for f in futures.values():
                    f.cancel()
                raise

        self._status("Finalising MP4...")
        self._finish_ffmpeg(graceful=True)
        rc = self._proc.returncode if self._proc else -1
        if rc != 0:
            raise DownloadError(f"FFmpeg exited with code {rc}")
        if not os.path.exists(self.output_path) or os.path.getsize(self.output_path) == 0:
            raise DownloadError("FFmpeg produced no output")
        elapsed = time.monotonic() - t0
        self._status(
            f"Done: {format_time(total_secs)} of video, {format_bytes(done_bytes)} in {format_time(elapsed)} "
            f"({total_secs / max(elapsed, 1e-3):.0f}x realtime)"
        )
        self._emit(self.on_complete_callback, self.output_path)

    def _start_ffmpeg(self, offset: float, clip_duration: Optional[float], fmp4: bool):
        out_dir = os.path.dirname(os.path.abspath(self.output_path))
        os.makedirs(out_dir, exist_ok=True)
        cmd = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "mp4" if fmp4 else "mpegts",
            "-i", "pipe:0",
        ]
        # Output-side -ss/-t work on piped input (input-side -ss needs a seekable source).
        # Timestamps are rebased to 0 at the first packet, so offset is relative to the
        # first segment we feed in.
        if offset > 0.01:
            cmd += ["-ss", f"{offset:.3f}"]
        if clip_duration is not None:
            cmd += ["-t", f"{clip_duration:.3f}"]
        cmd += ["-c", "copy", "-map", "0:v?", "-map", "0:a?", "-dn", "-sn"]
        ext = os.path.splitext(self.output_path)[1].lower()
        if ext in ("", ".mp4", ".m4v", ".mov"):
            cmd += ["-bsf:a", "aac_adtstoasc"]
            if self.faststart:
                cmd += ["-movflags", "+faststart"]
        cmd += [self.output_path]

        self._ffmpeg_stderr = []
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._drain_stderr, name="ffmpeg-stderr", daemon=True).start()

    def _drain_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._ffmpeg_stderr.append(line)
                if len(self._ffmpeg_stderr) > 50:
                    del self._ffmpeg_stderr[0]

    def _finish_ffmpeg(self, graceful: bool):
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # EOF lets FFmpeg write the moov atom -> playable file
        except OSError:
            pass
        if graceful:
            try:
                # faststart rewrites the whole file, so give large outputs plenty of time
                proc.wait(timeout=600)
                return
            except subprocess.TimeoutExpired:
                pass
        proc.kill()
        proc.wait()

    def _ffmpeg_error_tail(self) -> str:
        if not self._ffmpeg_stderr:
            return ""
        return "\nFFmpeg said:\n  " + "\n  ".join(self._ffmpeg_stderr[-5:])
