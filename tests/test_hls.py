"""Unit tests for the pure parts of hls.py (no network, no FFmpeg)."""

import os
import sys

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hls import (  # noqa: E402
    Playlist,
    PlaylistError,
    Segment,
    decrypt_aes128,
    format_time,
    iv_for,
    is_master_playlist,
    parse_media_playlist,
    parse_time,
    select_segments,
    select_variant,
)

BASE = "https://sdk.example.com/playlist/1.1/x/sdk-ssl.m3u8?Policy=abc&Signature=def"

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:15
#EXT-X-MEDIA-SEQUENCE:0

#EXT-X-KEY:METHOD=AES-128,URI="/vid/tubf35fm.ury.key",IV=0x964A95CE643A7CCA3D35025387AB9D6D
#EXTINF:10.000,
/vid/seg-64-4.ts
#EXTINF:10.000,
/vid/seg-65-4.ts
#EXTINF:10.824,
/vid/seg-66-4.ts
#EXT-X-ENDLIST
"""

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
high/index.m3u8
"""


# ----------------------------------------------------------------------- time parsing


@pytest.mark.parametrize(
    "text,expected",
    [
        ("00:00:00", 0),
        ("00:10:00", 600),
        ("1:02:03", 3723),
        ("02:03", 123),
        ("45", 45),
        ("90.5", 90.5),
        ("1h30m", 5400),
        ("2h", 7200),
        ("15m10s", 910),
        (" 00:00:10 ", 10),
        (600, 600),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "-5", "10:", ":10"])
def test_parse_time_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_time(text)


def test_format_time():
    assert format_time(0) == "0:00:00"
    assert format_time(3723) == "1:02:03"
    assert format_time(10301) == "2:51:41"
    assert format_time(5 * 3600 + 7) == "5:00:07"


# ----------------------------------------------------------------------- playlist parsing


def test_parse_media_playlist_resolves_urls_keys_and_timing():
    pl = parse_media_playlist(MEDIA, BASE)
    assert pl.is_vod
    assert len(pl.segments) == 3
    assert pl.total_duration == pytest.approx(30.824)
    first, second, third = pl.segments
    # root-relative URIs resolve against the host, dropping the signed query string
    assert first.uri == "https://sdk.example.com/vid/seg-64-4.ts"
    assert first.key_uri == "https://sdk.example.com/vid/tubf35fm.ury.key"
    assert first.iv == bytes.fromhex("964A95CE643A7CCA3D35025387AB9D6D")
    assert (first.start, second.start, third.start) == (0, 10, 20)
    assert (first.seq, second.seq, third.seq) == (0, 1, 2)


def test_iv_defaults_to_media_sequence():
    seg = Segment(uri="x", duration=10, start=0, seq=66, key_uri="k", iv=None)
    assert iv_for(seg) == (66).to_bytes(16, "big")


def test_master_detection_and_best_variant():
    assert is_master_playlist(MASTER)
    assert not is_master_playlist(MEDIA)
    assert select_variant(MASTER, "https://cdn.example.com/a/master.m3u8") == "https://cdn.example.com/a/high/index.m3u8"


def test_parse_rejects_non_m3u8_and_unsupported_encryption():
    with pytest.raises(PlaylistError):
        parse_media_playlist("<html>403</html>", BASE)
    with pytest.raises(PlaylistError):
        parse_media_playlist(MEDIA.replace("AES-128", "SAMPLE-AES"), BASE)
    with pytest.raises(PlaylistError):
        parse_media_playlist(MASTER, BASE)


# ----------------------------------------------------------------------- range selection


def _playlist(n=10, seg_len=10.0):
    return Playlist(url="x", segments=[Segment(uri=str(i), duration=seg_len, start=i * seg_len, seq=i) for i in range(n)], is_vod=True)


def test_select_full_range():
    segs, offset, clip = select_segments(_playlist(), 0, None)
    assert len(segs) == 10 and offset == 0 and clip is None


def test_select_mid_range_with_offset():
    segs, offset, clip = select_segments(_playlist(), 15, 20)  # 0:15 .. 0:35
    assert [s.seq for s in segs] == [1, 2, 3]
    assert offset == pytest.approx(5)
    assert clip == pytest.approx(20)


def test_select_range_on_exact_boundaries():
    segs, offset, clip = select_segments(_playlist(), 20, 10)  # exactly segment 2
    assert [s.seq for s in segs] == [2]
    assert offset == 0 and clip == 10


def test_select_range_clamped_to_end():
    segs, offset, clip = select_segments(_playlist(), 95, 600)
    assert [s.seq for s in segs] == [9]
    assert clip == pytest.approx(5)


def test_select_rejects_start_past_end():
    with pytest.raises(ValueError):
        select_segments(_playlist(), 100, 10)


# ----------------------------------------------------------------------- crypto


def test_decrypt_aes128_roundtrip_strips_pkcs7():
    key = bytes(range(16))
    iv = bytes(range(16, 32))
    plain = b"\x47" * 188 * 3  # three TS packets
    pad = 16 - len(plain) % 16  # PKCS#7: 564 bytes -> 12 bytes of 0x0c
    padded = plain + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher = enc.update(padded) + enc.finalize()
    assert decrypt_aes128(cipher, key, iv) == plain


def test_decrypt_rejects_truncated_data():
    from hls import DownloadError

    with pytest.raises(DownloadError):
        decrypt_aes128(b"\x00" * 17, bytes(16), bytes(16))
