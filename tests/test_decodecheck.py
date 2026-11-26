"""Unit tests for soulbeet/beets-plugins/decodecheck.py.

Runs locally using Python stdlib unittest + real /usr/bin/ffmpeg, with beets
stubbed via sys.modules so beets does not need to be installed locally.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_PATH = REPO_ROOT / "soulbeet" / "beets-plugins" / "decodecheck.py"
FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"


# ---------------------------------------------------------------------------
# Minimal beets stub so decodecheck.py can be imported without beets installed
# ---------------------------------------------------------------------------


class _FakeConfigView:
    def __init__(self, values: dict | None = None):
        self._values = dict(values or {})

    def add(self, defaults: dict) -> None:
        for k, v in defaults.items():
            self._values.setdefault(k, v)

    def __getitem__(self, key: str) -> "_FakeConfigValue":
        return _FakeConfigValue(self._values[key])

    def set(self, key: str, value) -> None:
        self._values[key] = value


class _FakeConfigValue:
    def __init__(self, value):
        self._value = value

    def get(self, typ=None):
        if typ is None:
            return self._value
        return typ(self._value)

    def as_str(self) -> str:
        return str(self._value)


class _FakeLogger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _rec(self, level: str, msg: str, *args) -> None:
        formatted = msg.format(*args) if args else msg
        self.records.append((level, formatted))

    def debug(self, msg: str, *args) -> None:
        self._rec("debug", msg, *args)

    def info(self, msg: str, *args) -> None:
        self._rec("info", msg, *args)

    def warning(self, msg: str, *args) -> None:
        self._rec("warning", msg, *args)

    def error(self, msg: str, *args) -> None:
        self._rec("error", msg, *args)


class _FakeBeetsPlugin:
    def __init__(self, name: str | None = None):
        self.name = name or "decodecheck"
        self.config = _FakeConfigView()
        self._log = _FakeLogger()
        self._listeners: list[tuple[str, object]] = []

    def register_listener(self, event: str, func) -> None:
        self._listeners.append((event, func))


class _FakeImportTask:
    def __init__(self, toppath, paths, items):
        self.toppath = toppath
        self.paths = list(paths)
        self.items = list(items)
        self.is_album = True
        self.skip = False


class _FakeSingletonImportTask(_FakeImportTask):
    def __init__(self, toppath, item):
        super().__init__(toppath, [item.path], [item])
        self.item = item
        self.is_album = False


class _FakeSentinelImportTask(_FakeImportTask):
    def __init__(self, toppath, paths=()):
        super().__init__(toppath, paths, ())
        self.skip = True


class _FakeArchiveImportTask(_FakeSentinelImportTask):
    pass


class _FakeItem:
    def __init__(
        self,
        path: str | bytes,
        *,
        length: float | None = 20.0,
        fmt: str = "FLAC",
        artist: str = "Test Artist",
        album: str = "Test Album",
        title: str = "Track One",
        albumartist: str = "Test AlbumArtist",
        track: int = 1,
    ):
        self.path = os.fsencode(path) if isinstance(path, str) else path
        self.length = length
        self.format = fmt
        self.artist = artist
        self.album = album
        self.title = title
        self.albumartist = albumartist
        self.track = track


def _load_plugin_module():
    beets_mod = types.ModuleType("beets")
    plugins_mod = types.ModuleType("beets.plugins")
    plugins_mod.BeetsPlugin = _FakeBeetsPlugin
    importer_mod = types.ModuleType("beets.importer")
    tasks_mod = types.ModuleType("beets.importer.tasks")
    tasks_mod.ImportTask = _FakeImportTask
    tasks_mod.SingletonImportTask = _FakeSingletonImportTask
    tasks_mod.SentinelImportTask = _FakeSentinelImportTask
    tasks_mod.ArchiveImportTask = _FakeArchiveImportTask
    importer_mod.tasks = tasks_mod
    beets_mod.plugins = plugins_mod
    beets_mod.importer = importer_mod

    injected = {
        "beets": beets_mod,
        "beets.plugins": plugins_mod,
        "beets.importer": importer_mod,
        "beets.importer.tasks": tasks_mod,
    }
    saved = {k: sys.modules.get(k) for k in injected}
    sys.modules.update(injected)
    try:
        spec = importlib.util.spec_from_file_location(
            "decodecheck_under_test", PLUGIN_PATH
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, prev in saved.items():
            if prev is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = prev


dc = _load_plugin_module()


# ---------------------------------------------------------------------------
# Audio fixture helpers (real ffmpeg-generated audio)
# ---------------------------------------------------------------------------


def _gen_flac(path: Path, duration_s: int = 20) -> None:
    subprocess.run(
        [
            FFMPEG_BIN,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}",
            "-c:a",
            "flac",
            str(path),
        ],
        check=True,
    )


def _gen_mp3(path: Path, duration_s: int = 20) -> None:
    subprocess.run(
        [
            FFMPEG_BIN,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(path),
        ],
        check=True,
    )


def _gen_flac_with_cover(path: Path, duration_s: int = 20) -> None:
    cover = path.parent / "_tmp_cover.jpg"
    subprocess.run(
        [
            FFMPEG_BIN,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=300x300:d=1",
            "-frames:v",
            "1",
            str(cover),
        ],
        check=True,
    )
    try:
        subprocess.run(
            [
                FFMPEG_BIN,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={duration_s}",
                "-i",
                str(cover),
                "-map",
                "0:a",
                "-map",
                "1:v",
                "-c:a",
                "flac",
                "-c:v",
                "mjpeg",
                "-disposition:v:0",
                "attached_pic",
                str(path),
            ],
            check=True,
        )
    finally:
        cover.unlink(missing_ok=True)


def _zero_flac_total_samples(src: Path, dst: Path) -> None:
    raw = bytearray(src.read_bytes())
    assert raw[:4] == b"fLaC", "expected raw FLAC magic"
    # STREAMINFO starts at byte 8; total_samples is low 4 bits of byte 13 + bytes 14..17.
    raw[8 + 13] &= 0xF0
    raw[8 + 14 : 8 + 18] = b"\x00\x00\x00\x00"
    dst.write_bytes(raw)


def _corrupt_middle(src: Path, dst: Path) -> None:
    raw = bytearray(src.read_bytes())
    mid = len(raw) // 2
    raw[mid : mid + 512] = b"\xff" * 512
    dst.write_bytes(raw)


def _truncate_bytes(src: Path, dst: Path, fraction: float = 0.33) -> None:
    raw = src.read_bytes()
    dst.write_bytes(raw[: max(256, int(len(raw) * fraction))])


def _prepend_id3v2(src: Path, dst: Path) -> None:
    # Minimal 10-byte ID3v2.3 header with 0-byte payload.
    dst.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00" + src.read_bytes())


def _make_id3v1(title: str = "Squatty Roo", artist: str = "Ella Fitzgerald") -> bytes:
    body = (
        b"TAG"
        + title.encode("latin-1")[:30].ljust(30, b"\x00")
        + artist.encode("latin-1")[:30].ljust(30, b"\x00")
        + b"Test Album".ljust(30, b"\x00")
        + b"1993"
        + b"Track 17".ljust(28, b"\x00")
        + b"\x00\x11\x08"
    )
    assert len(body) == 128
    return body


def _make_id3v1_enhanced(title: str = "Extended Title") -> bytes:
    block = b"TAG+" + title.encode("latin-1")[:60].ljust(223, b"\x00")
    assert len(block) == 227
    return block


def _ffmpeg_has_mp3_encoder() -> bool:
    if not os.path.exists(FFMPEG_BIN):
        return False
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "libmp3lame" in proc.stdout


HAS_MP3_ENCODER = _ffmpeg_has_mp3_encoder()


def _make_lyrics3v2(fields: bytes = b"IND0000200CRC00008EDEB4646") -> bytes:
    body = b"LYRICSBEGIN" + fields
    return body + f"{len(body):06d}".encode("ascii") + b"LYRICS200"


def _make_lyrics3v1(lyrics: bytes = b"Hello world\r\nLine two") -> bytes:
    return b"LYRICSBEGIN" + lyrics + b"LYRICSEND"


def _make_apev2_tag(
    *,
    items: list[tuple[str, bytes]] | None = None,
    with_header: bool = True,
    version: int = 2000,
) -> bytes:
    items = items or [("Comment", b"Test APEv2")]
    body = bytearray()
    for key, val in items:
        body += struct.pack("<II", len(val), 0)
        body += key.encode("ascii") + b"\x00" + val
    tag_size = len(body) + 32  # items + footer (excludes optional header)
    header_flag = 0x80000000 if (version == 2000 and with_header) else 0
    footer = (
        b"APETAGEX"
        + struct.pack("<IIII", version, tag_size, len(items), header_flag)
        + b"\x00" * 8
    )
    if header_flag:
        header = (
            b"APETAGEX"
            + struct.pack(
                "<IIII", version, tag_size, len(items), header_flag | 0x20000000
            )
            + b"\x00" * 8
        )
        return header + bytes(body) + footer
    return bytes(body) + footer


# ---------------------------------------------------------------------------
# Pure logic tests
# ---------------------------------------------------------------------------


class PureLogicTests(unittest.TestCase):
    def test_parse_progress_out_time_us_takes_last_valid(self):
        stdout = (
            "out_time_us=N/A\n"
            "out_time_us=5000000\n"
            "progress=continue\n"
            "out_time_us=29800000\n"
            "out_time_us=N/A\n"
            "progress=end\n"
        )
        self.assertAlmostEqual(dc.parse_progress_out_time_s(stdout), 29.8)

    def test_parse_progress_fallback_to_hms(self):
        stdout = "out_time=00:03:32.500000\nprogress=end\n"
        self.assertAlmostEqual(dc.parse_progress_out_time_s(stdout), 212.5)

    def test_parse_progress_empty(self):
        self.assertIsNone(dc.parse_progress_out_time_s(""))
        self.assertIsNone(dc.parse_progress_out_time_s("out_time_us=N/A\n"))

    def test_compute_timeout(self):
        self.assertEqual(dc.compute_timeout(None), 120.0)
        self.assertEqual(dc.compute_timeout(0.0), 120.0)
        self.assertEqual(dc.compute_timeout(30.0), 120.0)
        self.assertEqual(dc.compute_timeout(200.0), 400.0)

    def test_truncation_rule_boundaries(self):
        # Under/at min_check_s (15s) -> never marked truncated.
        res = dc.evaluate_decode(
            "/x/short.mp3",
            15.0,
            "MP3",
            0,
            "out_time_us=5000000\n",
            "",
            check_magic=False,
        )
        self.assertTrue(res.ok)

        # Over 15s: header=20s, tolerance=max(5, 1.0)=5.0 -> decoded 14.9s is truncated.
        res = dc.evaluate_decode(
            "/x/t.mp3",
            20.0,
            "MP3",
            0,
            "out_time_us=14900000\n",
            "",
            check_magic=False,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "truncated")

        # Over 15s: header=20s, decoded 15.1s is within 5s tolerance -> ok.
        res = dc.evaluate_decode(
            "/x/t.mp3",
            20.0,
            "MP3",
            0,
            "out_time_us=15100000\n",
            "",
            check_magic=False,
        )
        self.assertTrue(res.ok)

        # Long track: header=300s, tolerance=max(5, 15.0)=15.0 -> 284s truncated, 286s ok.
        res_bad = dc.evaluate_decode(
            "/x/long.flac",
            300.0,
            "FLAC",
            0,
            "out_time_us=284000000\n",
            "",
            check_magic=False,
        )
        self.assertEqual(res_bad.reason, "truncated")
        res_ok = dc.evaluate_decode(
            "/x/long.flac",
            300.0,
            "FLAC",
            0,
            "out_time_us=286000000\n",
            "",
            check_magic=False,
        )
        self.assertTrue(res_ok.ok)

    def test_decode_error_from_stderr_even_with_rc_zero(self):
        long_err = "E" * 500
        res = dc.evaluate_decode(
            "/x/bad.flac",
            200.0,
            "FLAC",
            0,
            "out_time_us=200000000\n",
            long_err,
            check_magic=False,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "decode_error")
        self.assertEqual(len(res.detail), 300)

    def test_mjpeg_only_stderr_full_decode_accepted(self):
        stderr = (
            "[mjpeg @ 0x14aae2d5d080] unable to decode APP fields: "
            "Invalid data found when processing input\n"
        )
        res = dc.evaluate_decode(
            "/x/cover_warn.flac",
            154.6,
            "FLAC",
            0,
            "out_time_us=154600000\nprogress=end\n",
            stderr,
            check_magic=False,
        )
        self.assertTrue(res.ok, res)
        self.assertIsNone(res.reason)
        self.assertEqual(res.decoded_s, 154.6)
        self.assertEqual(len(res.ignored_warnings), 1)
        self.assertIn("[mjpeg @ 0x14aae2d5d080]", res.ignored_warnings[0])

    def test_mjpeg_plus_flac_error_rejected(self):
        stderr = (
            "[mjpeg @ 0x14aae2d5d080] unable to decode APP fields: "
            "Invalid data found when processing input\n"
            "[flac @ 0x55a01234abcd] CRC mismatch!\n"
        )
        res = dc.evaluate_decode(
            "/x/both.flac",
            200.0,
            "FLAC",
            0,
            "out_time_us=200000000\nprogress=end\n",
            stderr,
            check_magic=False,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "decode_error")
        self.assertIn("[flac @ 0x55a01234abcd] CRC mismatch!", res.detail)
        self.assertNotIn("[mjpeg @", res.detail)

    def test_png_and_other_image_only_stderr_accepted(self):
        for codec in (
            "png",
            "bmp",
            "gif",
            "webp",
            "tiff",
            "jpeg2000",
            "mjpegb",
            "jpegls",
        ):
            stderr = f"[{codec} @ 0x7f1234567890] corrupt picture chunk\n"
            res = dc.evaluate_decode(
                "/x/art.flac",
                120.0,
                "FLAC",
                0,
                "out_time_us=120000000\nprogress=end\n",
                stderr,
                check_magic=False,
            )
            self.assertTrue(res.ok, f"{codec} should be ignored: {res}")
            self.assertEqual(len(res.ignored_warnings), 1)

    def test_audio_and_unprefixed_stderr_and_nonzero_rc_still_reject(self):
        for stderr in (
            "[flac @ 0x1234] invalid sync code\n",
            "[mp3float @ 0x1234] Header missing\n",
            "[aac @ 0x1234] channel element 0.0 is not allocated\n",
            "Invalid data found when processing input\n",
        ):
            res = dc.evaluate_decode(
                "/x/bad.flac",
                120.0,
                "FLAC",
                0,
                "out_time_us=120000000\n",
                stderr,
                check_magic=False,
            )
            self.assertFalse(res.ok, stderr)
            self.assertEqual(res.reason, "decode_error")

        # Non-zero exit code must still reject even when stderr only has mjpeg.
        res_rc = dc.evaluate_decode(
            "/x/bad_rc.flac",
            120.0,
            "FLAC",
            1,
            "out_time_us=120000000\n",
            "[mjpeg @ 0x1234] unable to decode APP fields\n",
            check_magic=False,
        )
        self.assertFalse(res_rc.ok)
        self.assertEqual(res_rc.reason, "decode_error")
        self.assertIn("status 1", res_rc.detail)

        # Truncation and no_duration rules still fire when mjpeg warning is ignored.
        res_trunc = dc.evaluate_decode(
            "/x/trunc.flac",
            120.0,
            "FLAC",
            0,
            "out_time_us=30000000\n",
            "[mjpeg @ 0x1234] unable to decode APP fields\n",
            check_magic=False,
        )
        self.assertFalse(res_trunc.ok)
        self.assertEqual(res_trunc.reason, "truncated")

        res_nodur = dc.evaluate_decode(
            "/x/nodur.flac",
            0.0,
            "FLAC",
            0,
            "out_time_us=120000000\n",
            "[mjpeg @ 0x1234] unable to decode APP fields\n",
            check_magic=False,
        )
        self.assertFalse(res_nodur.ok)
        self.assertEqual(res_nodur.reason, "no_duration")

    def test_id3v2_bom_and_lyrics_only_accepted(self):
        stderr = "Incorrect BOM value\nError reading lyrics, skipped\n"
        res = dc.evaluate_decode(
            "/x/02 - WTF.mp3",
            220.85,
            "MP3",
            0,
            "out_time_us=220840000\nprogress=end\n",
            stderr,
            check_magic=False,
        )
        self.assertTrue(res.ok, res)
        self.assertIsNone(res.reason)
        self.assertEqual(res.decoded_s, 220.84)
        self.assertEqual(
            res.ignored_warnings,
            ("Incorrect BOM value", "Error reading lyrics, skipped"),
        )

    def test_id3v2_bom_plus_header_missing_rejected(self):
        stderr = (
            "Incorrect BOM value\n"
            "Error reading lyrics, skipped\n"
            "[mp3float @ 0x145993720bc0] Header missing\n"
        )
        res = dc.evaluate_decode(
            "/x/corrupt.mp3",
            220.85,
            "MP3",
            0,
            "out_time_us=220840000\nprogress=end\n",
            stderr,
            check_magic=False,
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "decode_error")
        self.assertIn("Header missing", res.detail)
        self.assertNotIn("Incorrect BOM value", res.detail)

    def test_known_metadata_warnings_with_and_without_prefix_accepted(self):
        samples = (
            "Cannot read BOM value, input too short",
            "[mp3 @ 0x55a01234abcd] Incorrect BOM value",
            "[id3v2 @ 0x7f001234] Error reading comment frame, skipped",
            "Error reading frame TXXX, skipped",
            "Error reading frame GEOB, skipped",
            "Error reading frame, skipped",
            "Error reading GEOB frame, data truncated.",
            "Error decoding attached picture description.",
            "Failed to read tag data",
            "Failed to read compressed tag",
            "Failed to uncompress tag: -3",
            "invalid frame id, assuming padding",
            "Skipping encrypted ID3v2 frame PRIV.",
            "ID3v2.4 tag skipped, cannot handle unsynchronisation",
            "[mp3 @ 0x1234] APE tag size too large.",
            "[mp3 @ 0x1234] Unsupported tag version. (>=2000)",
            "[mp3 @ 0x1234] Invalid tag size 999999.",
            "[mp3 @ 0x1234] Too many tag fields (70000)",
            "[flac @ 0x1234] Attached picture metadata block too short",
            "[flac @ 0x1234] Invalid picture type: 99.",
            "[flac @ 0x1234] Could not read mimetype from an attached picture.",
            "[flac @ 0x1234] Attached picture metadata block too big 50000000",
            "[flac @ 0x1234] Error parsing attached picture.",
            "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1234] Error parsing cover art.",
            "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1234] The 'keys' atom with the invalid key count: 999999999",
            "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x1234] The key# 1 in meta has invalid size:4",
            "Incorrect BOM value\n    Last message repeated 3 times",
        )
        for sample in samples:
            res = dc.evaluate_decode(
                "/x/meta_warn.mp3",
                120.0,
                "MP3",
                0,
                "out_time_us=120000000\nprogress=end\n",
                sample + "\n",
                check_magic=False,
            )
            self.assertTrue(res.ok, f"expected metadata warning ignored: {sample!r} -> {res}")
            self.assertTrue(res.ignored_warnings, sample)

    def test_metadata_filter_exact_match_and_safety_conditions(self):
        # Substrings or generic errors must NOT be ignored.
        must_reject = (
            "Incorrect BOM value: extra trailing error",
            "Error while decoding stream #0:0: Incorrect BOM value",
            "Invalid data found when processing input",
            "Header missing",
            "[mp3float @ 0x1234] Header missing",
            "[mp3 @ 0x1234] Skipping 0 bytes of junk at 1024.",
            "[flac @ 0x1234] CRC error at PTS 12345",
            "Last message repeated 2 times",
            "[flac @ 0x1234] invalid sync code\nLast message repeated 1 times",
            "[aist#0:0/mp3 @ 0x1234] [dec:mp3float @ 0x5678] Error submitting packet to decoder: Invalid data found when processing input",
        )
        for sample in must_reject:
            res = dc.evaluate_decode(
                "/x/bad.mp3",
                120.0,
                "MP3",
                0,
                "out_time_us=120000000\nprogress=end\n",
                sample + "\n",
                check_magic=False,
            )
            self.assertFalse(res.ok, f"must reject: {sample!r}")
            self.assertEqual(res.reason, "decode_error", sample)

        # Ignored metadata warnings never override non-zero exit code.
        res_rc = dc.evaluate_decode(
            "/x/bad_rc.mp3",
            120.0,
            "MP3",
            1,
            "out_time_us=120000000\n",
            "Incorrect BOM value\nError reading lyrics, skipped\n",
            check_magic=False,
        )
        self.assertFalse(res_rc.ok)
        self.assertEqual(res_rc.reason, "decode_error")
        self.assertIn("status 1", res_rc.detail)

        # Ignored metadata warnings never override truncation.
        res_trunc = dc.evaluate_decode(
            "/x/trunc.mp3",
            120.0,
            "MP3",
            0,
            "out_time_us=30000000\n",
            "Incorrect BOM value\nError reading lyrics, skipped\n",
            check_magic=False,
        )
        self.assertFalse(res_trunc.ok)
        self.assertEqual(res_trunc.reason, "truncated")

        # Ignored metadata warnings never override lossless no_duration.
        res_nodur = dc.evaluate_decode(
            "/x/nodur.flac",
            0.0,
            "FLAC",
            0,
            "out_time_us=120000000\n",
            "[flac @ 0x1234] Error parsing attached picture.\n",
            check_magic=False,
        )
        self.assertFalse(res_nodur.ok)
        self.assertEqual(res_nodur.reason, "no_duration")

    def test_no_duration_lossless_vs_lossy(self):
        # Lossless with 0 length -> no_duration.
        res_flac = dc.evaluate_decode(
            "/x/a.flac",
            0.0,
            "FLAC",
            0,
            "out_time_us=20000000\n",
            "",
            check_magic=False,
        )
        self.assertFalse(res_flac.ok)
        self.assertEqual(res_flac.reason, "no_duration")

        # Lossy (MP3) with 0 header length is NOT rejected by no_duration rule.
        res_mp3 = dc.evaluate_decode(
            "/x/a.mp3",
            0.0,
            "MP3",
            0,
            "out_time_us=20000000\n",
            "",
            check_magic=False,
        )
        self.assertTrue(res_mp3.ok)

    def test_reject_record_matches_contract(self):
        res = dc.CheckResult(
            ok=False,
            reason="truncated",
            detail="decoded 29.80s < expected 212.00s (tolerance 10.60s)",
            header_s=212.0,
            decoded_s=29.8,
        )
        rec = dc.build_reject_record(
            ts=1764000000,
            task_id="20261124T163000-ab12cd",
            result=res,
            staging_path="/downloads/Some Dir/04 Track.flac",
            quarantined_to="/downloads/.decodecheck/quarantine/2026-11-24/20261124T163000-ab12cd/Some Dir/04 Track.flac",
            item_meta={
                "artist": "A",
                "album": "B",
                "title": "T",
                "albumartist": "AA",
                "track": 4,
            },
            task_files=12,
            task_bad=1,
            album_mode=True,
        )
        expected_keys = {
            "v",
            "ts",
            "task_id",
            "reason",
            "detail",
            "header_s",
            "decoded_s",
            "staging_path",
            "basename",
            "quarantined_to",
            "artist",
            "album",
            "title",
            "albumartist",
            "track",
            "task_files",
            "task_bad",
            "album_mode",
        }
        self.assertEqual(set(rec.keys()), expected_keys)
        self.assertEqual(rec["v"], 1)
        self.assertEqual(rec["basename"], "04 Track.flac")
        self.assertEqual(rec["track"], 4)
        self.assertTrue(rec["album_mode"])

    def test_build_quarantine_dest_under_staging_root(self):
        dest = dc.build_quarantine_dest(
            "/downloads/Some Dir/04 Track.flac",
            "/downloads/.decodecheck",
            "2026-11-24",
            "20261124T163000-ab12cd",
            toppath="/downloads/Some Dir",
        )
        self.assertEqual(
            dest,
            "/downloads/.decodecheck/quarantine/2026-11-24/20261124T163000-ab12cd/Some Dir/04 Track.flac",
        )

    def test_prune_quarantine_and_hourly_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / ".decodecheck"
            q = state_dir / "quarantine"
            old_dir = q / "2026-11-01" / "task-old"
            recent_dir = q / "2026-11-20" / "task-new"
            non_date = q / "notes"
            old_dir.mkdir(parents=True)
            recent_dir.mkdir(parents=True)
            non_date.mkdir(parents=True)
            (old_dir / "a.flac").write_bytes(b"x")

            # Reference "now": 2026-11-24 12:00:00 UTC; cutoff for 14 days = 2026-11-10.
            t0 = datetime.datetime(
                2026, 11, 24, 12, 0, 0, tzinfo=datetime.timezone.utc
            ).timestamp()
            removed = dc.prune_quarantine(str(state_dir), 14, now_ts=t0)
            self.assertEqual(removed, [str(q / "2026-11-01")])
            self.assertFalse((q / "2026-11-01").exists())
            self.assertTrue((q / "2026-11-20").exists())
            self.assertTrue(non_date.exists())

            # Create another old folder and verify rate-limit skips within 1 hour.
            old2 = q / "2026-11-02"
            old2.mkdir(parents=True)
            removed_soon = dc.prune_quarantine(
                str(state_dir), 14, now_ts=t0 + 1800
            )
            self.assertEqual(removed_soon, [])
            self.assertTrue(old2.exists())

            # After 1 hour, pruning runs again and deletes old2.
            removed_later = dc.prune_quarantine(
                str(state_dir), 14, now_ts=t0 + 3601
            )
            self.assertEqual(removed_later, [str(old2)])
            self.assertFalse(old2.exists())

    def test_detect_trailing_tag_variants_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = b"fLaC" + b"\x11\x22\x33\x44" * 64

            plain = root / "plain.flac"
            plain.write_bytes(base)
            self.assertIsNone(dc.detect_trailing_tag(str(plain)))

            id3 = root / "id3.flac"
            id3.write_bytes(base + _make_id3v1())
            self.assertEqual(dc.detect_trailing_tag(str(id3)), (len(base), "id3v1"))

            id3_plus = root / "id3_plus.flac"
            id3_plus.write_bytes(base + _make_id3v1_enhanced() + _make_id3v1())
            self.assertEqual(
                dc.detect_trailing_tag(str(id3_plus)), (len(base), "id3v1+")
            )

            ape_no_hdr = root / "ape_no_hdr.flac"
            ape_no_hdr.write_bytes(base + _make_apev2_tag(with_header=False))
            self.assertEqual(
                dc.detect_trailing_tag(str(ape_no_hdr)), (len(base), "apev2")
            )

            ape_with_hdr = root / "ape_with_hdr.flac"
            ape_with_hdr.write_bytes(base + _make_apev2_tag(with_header=True))
            self.assertEqual(
                dc.detect_trailing_tag(str(ape_with_hdr)), (len(base), "apev2")
            )

            ape1 = root / "ape1.flac"
            ape1.write_bytes(
                base + _make_apev2_tag(with_header=False, version=1000)
            )
            self.assertEqual(dc.detect_trailing_tag(str(ape1)), (len(base), "apev1"))

            ape_and_id3 = root / "ape_and_id3.flac"
            ape_and_id3.write_bytes(
                base + _make_apev2_tag(with_header=True) + _make_id3v1()
            )
            self.assertEqual(
                dc.detect_trailing_tag(str(ape_and_id3)),
                (len(base), "apev2+id3v1"),
            )

            # Invalid APE footers must not match.
            bad_is_hdr = root / "bad_is_hdr.flac"
            bad_is_hdr.write_bytes(
                base
                + b"APETAGEX"
                + struct.pack("<IIII", 2000, 32, 0, 0x20000000)
                + b"\x00" * 8
            )
            self.assertIsNone(dc.detect_trailing_tag(str(bad_is_hdr)))

            bad_reserved = root / "bad_reserved.flac"
            bad_reserved.write_bytes(
                base
                + b"APETAGEX"
                + struct.pack("<IIII", 2000, 32, 0, 0)
                + b"\x01" * 8
            )
            self.assertIsNone(dc.detect_trailing_tag(str(bad_reserved)))

            bad_size = root / "bad_size.flac"
            bad_size.write_bytes(
                base
                + b"APETAGEX"
                + struct.pack("<IIII", 2000, 999999, 0, 0)
                + b"\x00" * 8
            )
            self.assertIsNone(dc.detect_trailing_tag(str(bad_size)))

            self.assertIsNone(dc.detect_trailing_tag(str(root / "missing.flac")))

    def test_detect_trailing_tag_bytes_lyrics3_and_stacks(self):
        base = b"\xff\xfb\x90\x00" + b"\xaa\x55\x11\x22" * 64

        # Lyrics3v2 alone at EOF.
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + _make_lyrics3v2()),
            (len(base), "lyrics3v2"),
        )

        # Lyrics3v2 + ID3v1 (matches real Hi-Five - The Return layout).
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + _make_lyrics3v2() + _make_id3v1()),
            (len(base), "lyrics3v2+id3v1"),
        )

        # Lyrics3v2 + TAG+ + ID3v1.
        self.assertEqual(
            dc.detect_trailing_tag_bytes(
                base + _make_lyrics3v2() + _make_id3v1_enhanced() + _make_id3v1()
            ),
            (len(base), "lyrics3v2+id3v1+"),
        )

        # Lyrics3v1 + ID3v1 -> accepted.
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + _make_lyrics3v1() + _make_id3v1()),
            (len(base), "lyrics3v1+id3v1"),
        )

        # Lyrics3v1 alone at EOF (no ID3v1) -> NOT valid, must not strip.
        self.assertIsNone(dc.detect_trailing_tag_bytes(base + _make_lyrics3v1()))

        # Lyrics3v1 followed only by APEv2 (no ID3v1) -> peels APEv2 only.
        l3v1 = _make_lyrics3v1()
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + l3v1 + _make_apev2_tag()),
            (len(base) + len(l3v1), "apev2"),
        )

        # Lyrics3v1 exact max boundary (5100 B total) vs oversized (5101 B).
        max_l3v1 = _make_lyrics3v1(b"a" * (5100 - len(b"LYRICSBEGIN") - len(b"LYRICSEND")))
        self.assertEqual(len(max_l3v1), 5100)
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + max_l3v1 + _make_id3v1()),
            (len(base), "lyrics3v1+id3v1"),
        )
        over_l3v1 = _make_lyrics3v1(b"a" * (5101 - len(b"LYRICSBEGIN") - len(b"LYRICSEND")))
        self.assertEqual(len(over_l3v1), 5101)
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + over_l3v1 + _make_id3v1()),
            (len(base) + len(over_l3v1), "id3v1"),
        )

        # Stacked APE + Lyrics3 + ID3v1 in multiple orders.
        self.assertEqual(
            dc.detect_trailing_tag_bytes(
                base + _make_apev2_tag() + _make_lyrics3v2() + _make_id3v1()
            ),
            (len(base), "apev2+lyrics3v2+id3v1"),
        )
        self.assertEqual(
            dc.detect_trailing_tag_bytes(
                base + _make_apev2_tag() + _make_lyrics3v1() + _make_id3v1()
            ),
            (len(base), "apev2+lyrics3v1+id3v1"),
        )
        self.assertEqual(
            dc.detect_trailing_tag_bytes(
                base + _make_lyrics3v2() + _make_apev2_tag() + _make_id3v1()
            ),
            (len(base), "lyrics3v2+apev2+id3v1"),
        )
        self.assertEqual(
            dc.detect_trailing_tag_bytes(
                base
                + _make_apev2_tag(with_header=False, version=1000)
                + _make_lyrics3v2()
                + _make_id3v1_enhanced()
                + _make_id3v1()
            ),
            (len(base), "apev1+lyrics3v2+id3v1+"),
        )
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + _make_id3v1() + _make_apev2_tag()),
            (len(base), "id3v1+apev2"),
        )

        # APEv2 whose payload happens to contain "TAG" at offset -128 must still
        # peel as APEv2, not be mis-parsed as ID3v1.
        ape_with_tag_word = _make_apev2_tag(
            items=[("Comment", b"A" * 50 + b"TAG" + b"B" * 150)],
            with_header=True,
        )
        self.assertEqual(
            dc.detect_trailing_tag_bytes(base + ape_with_tag_word),
            (len(base), "apev2"),
        )

        # Bogus Lyrics3v2 size fields / missing LYRICSBEGIN -> no strip.
        bogus_cases = [
            # Non-digit character in 6-byte size field
            base + b"LYRICSBEGININD0000200" + b"00002a" + b"LYRICS200",
            # Leading space in size field
            base + b"LYRICSBEGININD0000200" + b" 00021" + b"LYRICS200",
            # Size smaller than len("LYRICSBEGIN") (11)
            base + b"LYRICSBEGIN" + b"000005" + b"LYRICS200",
            # Off-by-one size (LYRICSBEGIN not at computed offset)
            base + b"LYRICSBEGININD0000200" + b"000020" + b"LYRICS200",
            base + b"LYRICSBEGININD0000200" + b"000022" + b"LYRICS200",
            # Size larger than file
            base + b"LYRICSBEGININD0000200" + b"999999" + b"LYRICS200",
            # Valid size length, but wrong magic instead of LYRICSBEGIN
            base + b"NOTLYRICS!!IND0000200" + b"000021" + b"LYRICS200",
        ]
        for idx, buf in enumerate(bogus_cases):
            with self.subTest(bogus_idx=idx):
                self.assertIsNone(dc.detect_trailing_tag_bytes(buf))
                # Even when followed by a valid ID3v1, the bogus Lyrics3v2 is
                # NOT stripped (only the valid 128-byte ID3v1 peels off).
                self.assertEqual(
                    dc.detect_trailing_tag_bytes(buf + _make_id3v1()),
                    (len(buf), "id3v1"),
                )


# ---------------------------------------------------------------------------
# Real ffmpeg audio tests + full plugin hook tests
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.path.exists(FFMPEG_BIN), "ffmpeg not available")
class RealFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.good_flac = root / "good.flac"
        cls.good_mp3 = root / "good.mp3"
        cls.art_flac = root / "art.flac"
        cls.trunc_mp3 = root / "trunc.mp3"
        cls.corrupt_flac = root / "corrupt.flac"
        cls.nodur_flac = root / "nodur.flac"
        cls.id3_flac = root / "id3.flac"

        _gen_flac(cls.good_flac, duration_s=20)
        _gen_mp3(cls.good_mp3, duration_s=20)
        _gen_flac_with_cover(cls.art_flac, duration_s=20)
        _truncate_bytes(cls.good_mp3, cls.trunc_mp3, fraction=0.33)
        _corrupt_middle(cls.good_flac, cls.corrupt_flac)
        _zero_flac_total_samples(cls.good_flac, cls.nodur_flac)
        _prepend_id3v2(cls.good_flac, cls.id3_flac)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_good_flac_and_mp3_pass(self):
        r_flac = dc.run_ffmpeg_decode(
            str(self.good_flac), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
        )
        self.assertTrue(r_flac.ok, r_flac)
        self.assertAlmostEqual(r_flac.decoded_s, 20.0, delta=0.2)
        self.assertIsNone(r_flac.magic_mismatch)

        r_mp3 = dc.run_ffmpeg_decode(
            str(self.good_mp3), 20.04, "MP3", ffmpeg_bin=FFMPEG_BIN
        )
        self.assertTrue(r_mp3.ok, r_mp3)
        self.assertAlmostEqual(r_mp3.decoded_s, 20.0, delta=0.2)
        self.assertIsNone(r_mp3.magic_mismatch)

    def test_flac_with_embedded_cover_art_does_not_false_truncate(self):
        # Regression test: without -vn, ffmpeg 8.x reports out_time_us=11 for attached_pic.
        res = dc.run_ffmpeg_decode(
            str(self.art_flac), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
        )
        self.assertTrue(res.ok, res)
        self.assertAlmostEqual(res.decoded_s, 20.0, delta=0.2)

    def test_truncated_mp3_rejected_as_truncated(self):
        # Header claims 20.04s (from LAME/Xing header) while audio was chopped to ~6.6s.
        res = dc.run_ffmpeg_decode(
            str(self.trunc_mp3), 20.04, "MP3", ffmpeg_bin=FFMPEG_BIN
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "truncated")
        self.assertLess(res.decoded_s, 10.0)

    def test_corrupt_middle_flac_rejected_as_decode_error(self):
        res = dc.run_ffmpeg_decode(
            str(self.corrupt_flac), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "decode_error")
        self.assertTrue(res.detail)

    def test_zero_duration_flac_rejected_as_no_duration(self):
        res = dc.run_ffmpeg_decode(
            str(self.nodur_flac), 0.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "no_duration")
        self.assertEqual(res.header_s, 0.0)
        self.assertAlmostEqual(res.decoded_s, 20.0, delta=0.2)

    def test_id3v2_prepended_flac_accepted_with_magic_mismatch(self):
        res = dc.run_ffmpeg_decode(
            str(self.id3_flac), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
        )
        self.assertTrue(res.ok, res)
        self.assertEqual(res.magic_mismatch, "flac_with_id3v2")

    def test_missing_ffmpeg_fails_closed_checker_unavailable(self):
        res = dc.run_ffmpeg_decode(
            str(self.good_flac),
            20.0,
            "FLAC",
            ffmpeg_bin="/nonexistent/ffmpeg-binary",
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "checker_unavailable")

    def test_plugin_hook_accept_and_reject_and_passthrough(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            downloads.mkdir()

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)

            # 1. Sentinel & Archive tasks pass through untouched.
            sentinel = _FakeSentinelImportTask(str(downloads).encode())
            archive = _FakeArchiveImportTask(str(downloads).encode())
            self.assertEqual(plugin.on_import_task_created(None, sentinel), [sentinel])
            self.assertEqual(plugin.on_import_task_created(None, archive), [archive])

            # 2. All-good album (including ID3v2-FLAC cosmetic mismatch) passes.
            good_dir = downloads / "Good Album"
            good_dir.mkdir()
            g1 = good_dir / "01 Good.flac"
            g2 = good_dir / "02 ID3.flac"
            shutil.copyfile(self.good_flac, g1)
            shutil.copyfile(self.id3_flac, g2)
            good_task = _FakeImportTask(
                str(good_dir).encode(),
                [str(good_dir).encode()],
                [
                    _FakeItem(str(g1), length=20.0, track=1),
                    _FakeItem(str(g2), length=20.0, track=2),
                ],
            )
            self.assertEqual(
                plugin.on_import_task_created(None, good_task), [good_task]
            )
            checked_lines = [
                json.loads(ln)
                for ln in (state_dir / "checked.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(checked_lines), 1)
            self.assertEqual(checked_lines[0]["files"], 2)
            self.assertEqual(checked_lines[0]["magic_mismatches"], 1)

            # 3. Album with 1 good track, 1 corrupt track, and non-audio cover.jpg
            #    -> task dropped, ALL files quarantined, 1 reject line written.
            bad_dir = downloads / "Bad Album"
            bad_dir.mkdir()
            b1 = bad_dir / "01 Good.flac"
            b2 = bad_dir / "02 Corrupt.flac"
            cover = bad_dir / "cover.jpg"
            shutil.copyfile(self.good_flac, b1)
            shutil.copyfile(self.corrupt_flac, b2)
            cover.write_bytes(b"fake-jpg-bytes")

            bad_task = _FakeImportTask(
                str(bad_dir).encode(),
                [str(bad_dir).encode()],
                [
                    _FakeItem(str(b1), length=20.0, title="Good", track=1),
                    _FakeItem(str(b2), length=20.0, title="Corrupt", track=2),
                ],
            )
            self.assertEqual(plugin.on_import_task_created(None, bad_task), [])
            # Entire album dir cleaned up from staging.
            self.assertFalse(bad_dir.exists())

            rejects = [
                json.loads(ln)
                for ln in (state_dir / "rejects.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rejects), 1)
            rec = rejects[0]
            self.assertEqual(rec["reason"], "decode_error")
            self.assertEqual(rec["basename"], "02 Corrupt.flac")
            self.assertEqual(rec["task_files"], 2)
            self.assertEqual(rec["task_bad"], 1)
            self.assertTrue(rec["album_mode"])
            self.assertTrue(Path(rec["quarantined_to"]).exists())
            q_album_dir = Path(rec["quarantined_to"]).parent
            self.assertTrue((q_album_dir / "01 Good.flac").exists())
            self.assertTrue((q_album_dir / "cover.jpg").exists())

            # 4. Singleton task with truncated MP3 -> album_mode False, quarantined.
            s_dir = downloads / "Singletons"
            s_dir.mkdir()
            s1 = s_dir / "trunc.mp3"
            s_other = s_dir / "other.mp3"
            shutil.copyfile(self.trunc_mp3, s1)
            shutil.copyfile(self.good_mp3, s_other)
            s_task = _FakeSingletonImportTask(
                str(s_dir).encode(),
                _FakeItem(str(s1), length=20.04, fmt="MP3", title="Trunc", track=3),
            )
            self.assertEqual(plugin.on_import_task_created(None, s_task), [])
            self.assertFalse(s1.exists())
            # Sibling file in singleton mode must NOT be touched.
            self.assertTrue(s_other.exists())

            rejects = [
                json.loads(ln)
                for ln in (state_dir / "rejects.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rejects), 2)
            self.assertEqual(rejects[1]["reason"], "truncated")
            self.assertFalse(rejects[1]["album_mode"])

    def test_plugin_fails_closed_on_unexpected_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            alb = downloads / "Crash Album"
            alb.mkdir(parents=True)
            f1 = alb / "01.flac"
            shutil.copyfile(self.good_flac, f1)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)

            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [_FakeItem(str(f1), length=20.0)],
            )
            with mock.patch.object(
                dc, "run_ffmpeg_decode", side_effect=RuntimeError("boom")
            ):
                out = plugin.on_import_task_created(None, task)
            self.assertEqual(out, [])
            self.assertFalse(f1.exists())
            rejects = [
                json.loads(ln)
                for ln in (state_dir / "rejects.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rejects), 1)
            self.assertEqual(rejects[0]["reason"], "checker_unavailable")
            self.assertIn("boom", rejects[0]["detail"])

    def test_decode_timeout_rejects(self):
        # A fake "ffmpeg" that emits partial progress then hangs; timeout patched to 1s.
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "slow-ffmpeg"
            fake.write_text(
                "#!/bin/sh\necho out_time_us=3000000\nexec sleep 30\n"
            )
            fake.chmod(0o755)
            with mock.patch.object(dc, "compute_timeout", return_value=1.0):
                res = dc.run_ffmpeg_decode(
                    str(self.good_flac), 20.0, "FLAC", ffmpeg_bin=str(fake)
                )
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "decode_timeout")
            self.assertEqual(res.header_s, 20.0)
            self.assertIn("timed out", res.detail)

    def test_non_executable_ffmpeg_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ffmpeg"
            fake.write_text("not a program")
            fake.chmod(0o644)
            res = dc.run_ffmpeg_decode(
                str(self.good_flac), 20.0, "FLAC", ffmpeg_bin=str(fake)
            )
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "checker_unavailable")

    def test_reject_line_and_quarantine_path_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            alb = downloads / "Some Dir"
            (alb / "Scans").mkdir(parents=True)
            f1 = alb / "01 Track.flac"
            f4 = alb / "04 Track.flac"
            shutil.copyfile(self.good_flac, f1)
            shutil.copyfile(self.nodur_flac, f4)
            (alb / "folder.jpg").write_bytes(b"jpg")
            (alb / "Scans" / "back.png").write_bytes(b"png")
            # A file mutagen could not read: beets drops it from task.items.
            (alb / "05 Unreadable.flac").write_bytes(b"\x00" * 64)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [
                    _FakeItem(str(f1), length=20.0, track=1),
                    _FakeItem(str(f4), length=0.0, title="Four", track=4),
                ],
            )
            self.assertEqual(plugin.on_import_task_created(None, task), [])
            self.assertFalse(alb.exists())
            self.assertTrue(downloads.exists())

            lines = (state_dir / "rejects.jsonl").read_text().splitlines()
            recs = [json.loads(ln) for ln in lines]
            self.assertEqual(len(recs), 2)
            by_base = {r["basename"]: r for r in recs}
            r4 = by_base["04 Track.flac"]
            self.assertEqual(r4["reason"], "no_duration")
            self.assertEqual(r4["staging_path"], str(f4))
            self.assertEqual(r4["title"], "Four")
            self.assertEqual(r4["track"], 4)
            self.assertEqual(r4["task_files"], 3)
            self.assertEqual(r4["task_bad"], 2)
            self.assertIn(by_base["05 Unreadable.flac"]["reason"],
                          ("decode_error", "no_duration"))
            tid = r4["task_id"]
            self.assertRegex(tid, r"^\d{8}T\d{6}-[0-9a-f]{6}$")
            date_str = datetime.datetime.fromtimestamp(
                r4["ts"], tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d")
            qroot = state_dir / "quarantine" / date_str / tid / "Some Dir"
            self.assertEqual(r4["quarantined_to"], str(qroot / "04 Track.flac"))
            for rel in ("01 Track.flac", "04 Track.flac", "folder.jpg",
                        "Scans/back.png", "05 Unreadable.flac"):
                self.assertTrue((qroot / rel).exists(), rel)

    def test_flac_bit_flips_rejected_via_crccheck(self):
        # Noise-like audio so the flips land in residual data with framing intact;
        # plain ffmpeg decodes most of these "clean", crccheck catches them.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "noise.flac"
            subprocess.run(
                [FFMPEG_BIN, "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "anoisesrc=d=30:c=pink:a=0.3", "-ac", "2",
                 "-sample_fmt", "s16", str(src)],
                check=True,
            )
            self.assertTrue(dc.run_ffmpeg_decode(
                str(src), 30.0, "FLAC", ffmpeg_bin=FFMPEG_BIN).ok)
            import random
            raw0 = src.read_bytes()
            n = len(raw0)
            for seed in range(5):
                raw = bytearray(raw0)
                rnd = random.Random(seed)
                for _ in range(20):
                    raw[rnd.randrange(n // 3, 2 * n // 3)] ^= 0x10
                bad = Path(tmp) / f"flip{seed}.flac"
                bad.write_bytes(raw)
                res = dc.run_ffmpeg_decode(
                    str(bad), 30.0, "FLAC", ffmpeg_bin=FFMPEG_BIN)
                self.assertEqual(res.reason, "decode_error", seed)

    def test_crccheck_only_for_flac(self):
        self.assertIn("crccheck", dc.build_ffmpeg_cmd("ffmpeg", "/x/a.flac"))
        self.assertIn("crccheck", dc.build_ffmpeg_cmd("ffmpeg", "/x/a", "FLAC"))
        self.assertNotIn("crccheck", dc.build_ffmpeg_cmd("ffmpeg", "/x/a.mp3", "MP3"))
        cmd = dc.build_ffmpeg_cmd("ffmpeg", "/x/a.flac")
        self.assertLess(cmd.index("-err_detect"), cmd.index("-i"))

    def test_self_test(self):
        self.assertIsNone(dc.ffmpeg_self_test(FFMPEG_BIN))
        # a binary that runs but is not a working ffmpeg
        self.assertIsNotNone(dc.ffmpeg_self_test(sys.executable))
        self.assertIsNotNone(dc.ffmpeg_self_test("/nonexistent/ffmpeg"))

    def test_broken_checker_rcs_are_checker_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            for body, rc in (("exit 127", 127), ("exit 126", 126),
                             ("kill -9 $$", -9)):
                fake = Path(tmp) / "ffmpeg"
                fake.write_text(f"#!/bin/sh\necho broken >&2\n{body}\n")
                fake.chmod(0o755)
                res = dc.run_ffmpeg_decode(
                    str(self.good_flac), 20.0, "FLAC", ffmpeg_bin=str(fake))
                self.assertEqual(res.reason, "checker_unavailable", rc)
                self.assertIn(str(rc), res.detail)

    def _album(self, downloads: Path, name: str, src: Path) -> tuple[Path, object]:
        alb = downloads / name
        alb.mkdir()
        files = []
        for i in (1, 2):
            f = alb / f"0{i} T.flac"
            shutil.copyfile(src, f)
            files.append(f)
        task = _FakeImportTask(
            str(alb).encode(), [str(alb).encode()],
            [_FakeItem(str(f), length=20.0, track=i)
             for i, f in enumerate(files, 1)],
        )
        return alb, task

    def test_broken_ffmpeg_self_test_rejects_as_checker_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            state_dir = downloads / ".decodecheck"
            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", sys.executable)  # runs, but not ffmpeg
            _alb, task = self._album(downloads, "Good", self.good_flac)
            self.assertEqual(plugin.on_import_task_created(None, task), [])
            recs = [json.loads(ln) for ln in
                    (state_dir / "rejects.jsonl").read_text().splitlines()]
            self.assertEqual([r["reason"] for r in recs], ["checker_unavailable"])

    def test_vanished_files_abort_without_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            downloads.mkdir()
            state_dir = downloads / ".decodecheck"
            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            alb, task = self._album(downloads, "Moving", self.good_flac)
            real = dc.run_ffmpeg_decode

            def racing(path, *a, **k):
                # another beets process moves the whole folder mid-check
                if os.path.exists(alb):
                    shutil.rmtree(alb)
                return real(path, *a, **k)

            with mock.patch.object(dc, "run_ffmpeg_decode", side_effect=racing):
                self.assertEqual(plugin.on_import_task_created(None, task), [])
            self.assertFalse((state_dir / "rejects.jsonl").exists())
            self.assertFalse((state_dir / "quarantine").exists())

    def test_hook_never_raises(self):
        plugin = dc.DecodeCheckPlugin()

        class Weird:
            @property
            def items(self):
                raise RuntimeError("items exploded")

        self.assertEqual(plugin.on_import_task_created(None, Weird()), [])

    def test_plugin_records_ignored_image_warnings_in_checked_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            alb = downloads / "Art Warn Album"
            alb.mkdir(parents=True)
            f1 = alb / "01 Track.flac"
            shutil.copyfile(self.good_flac, f1)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            plugin._self_tested_bin = FFMPEG_BIN

            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [_FakeItem(str(f1), length=20.0, track=1)],
            )
            fake_res = dc.evaluate_decode(
                str(f1),
                20.0,
                "FLAC",
                0,
                "out_time_us=20000000\nprogress=end\n",
                "[mjpeg @ 0x14aae2d5d080] unable to decode APP fields: Invalid data found when processing input\n",
                check_magic=False,
            )
            with mock.patch.object(dc, "run_ffmpeg_decode", return_value=fake_res):
                self.assertEqual(plugin.on_import_task_created(None, task), [task])

            self.assertFalse((state_dir / "rejects.jsonl").exists())
            checked = [
                json.loads(ln)
                for ln in (state_dir / "checked.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["ignored_image_warnings"], 1)
            self.assertEqual(
                checked[0]["ignored_image_warning_details"][0]["basename"],
                "01 Track.flac",
            )
            self.assertIn(
                "[mjpeg @ 0x14aae2d5d080]",
                checked[0]["ignored_image_warning_details"][0]["warning"],
            )

    def test_plugin_records_ignored_metadata_warnings_in_checked_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            alb = downloads / "Conversational Lush"
            alb.mkdir(parents=True)
            f1 = alb / "02 - WTF.mp3"
            shutil.copyfile(self.good_mp3, f1)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            plugin._self_tested_bin = FFMPEG_BIN

            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [_FakeItem(str(f1), length=20.0, fmt="MP3", track=2)],
            )
            fake_res = dc.evaluate_decode(
                str(f1),
                20.0,
                "MP3",
                0,
                "out_time_us=20000000\nprogress=end\n",
                "Incorrect BOM value\nError reading lyrics, skipped\n",
                check_magic=False,
            )
            with mock.patch.object(dc, "run_ffmpeg_decode", return_value=fake_res):
                self.assertEqual(plugin.on_import_task_created(None, task), [task])

            self.assertFalse((state_dir / "rejects.jsonl").exists())
            checked = [
                json.loads(ln)
                for ln in (state_dir / "checked.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["ignored_metadata_warnings"], 1)
            self.assertEqual(
                checked[0]["ignored_metadata_warning_details"][0]["basename"],
                "02 - WTF.mp3",
            )
            self.assertEqual(
                checked[0]["ignored_metadata_warning_details"][0]["warning"],
                "Incorrect BOM value",
            )
            self.assertNotIn("ignored_image_warnings", checked[0])

    def test_plugin_disabled_passes_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            alb = downloads / "Disabled"
            alb.mkdir(parents=True)
            f1 = alb / "01.flac"
            shutil.copyfile(self.corrupt_flac, f1)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("enabled", False)
            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [_FakeItem(str(f1), length=20.0)],
            )
            self.assertEqual(plugin.on_import_task_created(None, task), [task])
            self.assertTrue(f1.exists())

    def test_flac_with_trailing_id3v1_and_apev2_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = self.good_flac.read_bytes()

            cases = [
                ("id3v1.flac", raw + _make_id3v1(), "id3v1"),
                (
                    "id3v1_plus.flac",
                    raw + _make_id3v1_enhanced() + _make_id3v1(),
                    "id3v1+",
                ),
                (
                    "apev2_hdr.flac",
                    raw + _make_apev2_tag(with_header=True),
                    "apev2",
                ),
                (
                    "apev2_no_hdr.flac",
                    raw + _make_apev2_tag(with_header=False),
                    "apev2",
                ),
                (
                    "apev2_id3v1.flac",
                    raw + _make_apev2_tag(with_header=True) + _make_id3v1(),
                    "apev2+id3v1",
                ),
            ]
            for name, payload, expected_tag in cases:
                with self.subTest(case=name):
                    p = root / name
                    p.write_bytes(payload)
                    res = dc.run_ffmpeg_decode(
                        str(p), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
                    )
                    self.assertTrue(res.ok, f"{name}: {res}")
                    self.assertEqual(res.trailing_tag, expected_tag)
                    self.assertAlmostEqual(res.decoded_s, 20.0, delta=0.2)
                    # Audio file on disk must never be modified.
                    self.assertEqual(p.read_bytes(), payload)

    def test_flac_with_corrupt_audio_and_trailing_id3v1_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "noise.flac"
            subprocess.run(
                [
                    FFMPEG_BIN,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "anoisesrc=d=20:c=pink:a=0.3",
                    "-ac",
                    "2",
                    "-sample_fmt",
                    "s16",
                    str(src),
                ],
                check=True,
            )
            raw = bytearray(src.read_bytes())
            # Flip a single audio byte in the middle -> CRC error must still reject
            # even when a valid 128-byte ID3v1 tag is appended.
            raw[len(raw) // 2] ^= 0x10
            bad = root / "flipped_with_id3v1.flac"
            bad.write_bytes(bytes(raw) + _make_id3v1())
            res = dc.run_ffmpeg_decode(
                str(bad), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
            )
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "decode_error")
            self.assertIsNone(res.trailing_tag)

            # Truncated FLAC with trailing ID3v1 must still reject.
            trunc = root / "trunc_with_id3v1.flac"
            trunc.write_bytes(src.read_bytes()[: len(raw) // 3] + _make_id3v1())
            res_t = dc.run_ffmpeg_decode(
                str(trunc), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
            )
            self.assertFalse(res_t.ok)
            self.assertIn(res_t.reason, ("decode_error", "truncated"))

    def test_flac_trailing_tag_retry_timeout_and_checker_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f_id3 = root / "id3.flac"
            f_id3.write_bytes(self.good_flac.read_bytes() + _make_id3v1())

            # Fake ffmpeg that fails first decode (file arg) with decode_error,
            # then hangs on the piped retry (pipe:0).
            slow = root / "slow-retry-ffmpeg"
            slow.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *pipe:0*) echo out_time_us=4000000; exec sleep 30 ;;\n'
                '  *) echo "[flac @ 0x1] invalid sync code" >&2; exit 0 ;;\n'
                "esac\n"
            )
            slow.chmod(0o755)
            with mock.patch.object(dc, "compute_timeout", return_value=1.0):
                res = dc.run_ffmpeg_decode(
                    str(f_id3), 20.0, "FLAC", ffmpeg_bin=str(slow)
                )
            self.assertFalse(res.ok)
            self.assertEqual(res.reason, "decode_timeout")

            # Fake ffmpeg that fails first decode with decode_error, then exits 127
            # on the piped retry -> must surface checker_unavailable.
            broken = root / "broken-retry-ffmpeg"
            broken.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *pipe:0*) echo "missing lib" >&2; exit 127 ;;\n'
                '  *) echo "[flac @ 0x1] invalid sync code" >&2; exit 0 ;;\n'
                "esac\n"
            )
            broken.chmod(0o755)
            res2 = dc.run_ffmpeg_decode(
                str(f_id3), 20.0, "FLAC", ffmpeg_bin=str(broken)
            )
            self.assertFalse(res2.ok)
            self.assertEqual(res2.reason, "checker_unavailable")

    def test_plugin_records_trailing_tag_in_checked_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            alb = downloads / "Ella Song Books"
            alb.mkdir(parents=True)
            f1 = alb / "100 - Squatty Roo.flac"
            payload = self.good_flac.read_bytes() + _make_id3v1()
            f1.write_bytes(payload)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [_FakeItem(str(f1), length=20.0, fmt="FLAC", track=1)],
            )
            self.assertEqual(plugin.on_import_task_created(None, task), [task])
            self.assertFalse((state_dir / "rejects.jsonl").exists())
            self.assertEqual(f1.read_bytes(), payload)

            checked = [
                json.loads(ln)
                for ln in (state_dir / "checked.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["trailing_tag"], "id3v1")
            self.assertEqual(checked[0]["trailing_tag_files"], 1)
            self.assertEqual(
                checked[0]["trailing_tag_details"],
                [{"basename": "100 - Squatty Roo.flac", "kind": "id3v1"}],
            )

    @unittest.skipUnless(HAS_MP3_ENCODER, "ffmpeg libmp3lame encoder not available")
    def test_mp3_with_trailing_lyrics3_and_id3v1_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "short.mp3"
            _gen_mp3(src, duration_s=18)
            raw = src.read_bytes()

            # Confirm raw MP3 decodes cleanly on its own.
            r0 = dc.run_ffmpeg_decode(str(src), 18.0, "MP3", ffmpeg_bin=FFMPEG_BIN)
            self.assertTrue(r0.ok, r0)

            cases = [
                (
                    "lyrics3v2_id3v1.mp3",
                    raw + _make_lyrics3v2() + _make_id3v1(),
                    "lyrics3v2+id3v1",
                ),
                (
                    "lyrics3v2_only.mp3",
                    raw + _make_lyrics3v2(),
                    "lyrics3v2",
                ),
                (
                    "lyrics3v1_id3v1.mp3",
                    raw + _make_lyrics3v1() + _make_id3v1(),
                    "lyrics3v1+id3v1",
                ),
                (
                    "lyrics3v2_apev2_id3v1.mp3",
                    raw + _make_lyrics3v2() + _make_apev2_tag() + _make_id3v1(),
                    "lyrics3v2+apev2+id3v1",
                ),
            ]
            for name, payload, expected_tag in cases:
                with self.subTest(case=name):
                    p = root / name
                    p.write_bytes(payload)
                    res = dc.run_ffmpeg_decode(
                        str(p), 18.0, "MP3", ffmpeg_bin=FFMPEG_BIN
                    )
                    self.assertTrue(res.ok, f"{name}: {res}")
                    self.assertEqual(res.trailing_tag, expected_tag)
                    self.assertAlmostEqual(res.decoded_s, 18.0, delta=0.3)
                    # Audio file on disk must never be modified.
                    self.assertEqual(p.read_bytes(), payload)

            # FLAC with stacked APEv2 + Lyrics3v2 + ID3v1 also peels cleanly.
            flac_stack = root / "apev2_lyrics3v2_id3v1.flac"
            flac_payload = (
                self.good_flac.read_bytes()
                + _make_apev2_tag()
                + _make_lyrics3v2()
                + _make_id3v1()
            )
            flac_stack.write_bytes(flac_payload)
            res_f = dc.run_ffmpeg_decode(
                str(flac_stack), 20.0, "FLAC", ffmpeg_bin=FFMPEG_BIN
            )
            self.assertTrue(res_f.ok, res_f)
            self.assertEqual(res_f.trailing_tag, "apev2+lyrics3v2+id3v1")
            self.assertEqual(flac_stack.read_bytes(), flac_payload)

    @unittest.skipUnless(HAS_MP3_ENCODER, "ffmpeg libmp3lame encoder not available")
    def test_mp3_midfile_corruption_with_lyrics3v2_id3v1_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "short.mp3"
            _gen_mp3(src, duration_s=18)
            raw = bytearray(src.read_bytes())
            # Corrupt a chunk right in the middle of the MP3 audio frames.
            mid = len(raw) // 2
            raw[mid : mid + 512] = b"\x00" * 512
            bad = root / "corrupt_lyrics3.mp3"
            bad.write_bytes(bytes(raw) + _make_lyrics3v2() + _make_id3v1())

            res = dc.run_ffmpeg_decode(str(bad), 18.0, "MP3", ffmpeg_bin=FFMPEG_BIN)
            self.assertFalse(res.ok, res)
            self.assertEqual(res.reason, "decode_error")
            self.assertIsNone(res.trailing_tag)

            # Truncated MP3 + Lyrics3v2 + ID3v1 must still reject.
            trunc = root / "trunc_lyrics3.mp3"
            trunc.write_bytes(
                src.read_bytes()[: len(raw) // 3] + _make_lyrics3v2() + _make_id3v1()
            )
            res_t = dc.run_ffmpeg_decode(
                str(trunc), 18.0, "MP3", ffmpeg_bin=FFMPEG_BIN
            )
            self.assertFalse(res_t.ok, res_t)
            self.assertIn(res_t.reason, ("truncated", "decode_error"))
            self.assertIsNone(res_t.trailing_tag)

    @unittest.skipUnless(HAS_MP3_ENCODER, "ffmpeg libmp3lame encoder not available")
    def test_plugin_records_mp3_lyrics3v2_trailing_tag_in_checked_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            alb = downloads / "The Return [155104be]"
            alb.mkdir(parents=True)
            f1 = alb / "01 - Intro.mp3"
            _gen_mp3(f1, duration_s=18)
            payload = f1.read_bytes() + _make_lyrics3v2() + _make_id3v1()
            f1.write_bytes(payload)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [_FakeItem(str(f1), length=18.0, fmt="MP3", track=1)],
            )
            self.assertEqual(plugin.on_import_task_created(None, task), [task])
            self.assertFalse((state_dir / "rejects.jsonl").exists())
            self.assertEqual(f1.read_bytes(), payload)

            checked = [
                json.loads(ln)
                for ln in (state_dir / "checked.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["trailing_tag"], "lyrics3v2+id3v1")
            self.assertEqual(checked[0]["trailing_tag_files"], 1)
            self.assertEqual(
                checked[0]["trailing_tag_details"],
                [{"basename": "01 - Intro.mp3", "kind": "lyrics3v2+id3v1"}],
            )

    def test_probe_extra_audio_mutagen_valid_unreadable_and_vanished(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "track.flac"
            p.write_bytes(b"placeholder")

            class FLAC:
                def __init__(self, length):
                    self.info = types.SimpleNamespace(length=length)

            class MP4:
                def __init__(self, length, codec="alac"):
                    self.info = types.SimpleNamespace(length=length, codec=codec)

            # Valid FLAC length.
            fake_mut = types.SimpleNamespace(File=lambda _p: FLAC(189.4))
            with mock.patch.object(dc, "mutagen", fake_mut):
                self.assertEqual(dc.probe_extra_audio(str(p)), (189.4, "FLAC", None))

            # ALAC inside M4A.
            fake_mut = types.SimpleNamespace(File=lambda _p: MP4(210.5, "alac"))
            with mock.patch.object(dc, "mutagen", fake_mut):
                self.assertEqual(dc.probe_extra_audio(str(p)), (210.5, "ALAC", None))

            # Zero-duration FLAC (real STREAMINFO total_samples=0).
            fake_mut = types.SimpleNamespace(File=lambda _p: FLAC(0.0))
            with mock.patch.object(dc, "mutagen", fake_mut):
                self.assertEqual(dc.probe_extra_audio(str(p)), (0.0, "FLAC", None))

            # Unreadable: mutagen raises an exception.
            def _boom(_p):
                raise ValueError("not a valid FLAC file")

            fake_mut = types.SimpleNamespace(File=_boom)
            with mock.patch.object(dc, "mutagen", fake_mut):
                hdr, fmt, err = dc.probe_extra_audio(str(p))
                self.assertIsNone(hdr)
                self.assertIsNone(fmt)
                self.assertEqual(err, "unreadable by mutagen: not a valid FLAC file")

            # Unreadable: mutagen returns None.
            fake_mut = types.SimpleNamespace(File=lambda _p: None)
            with mock.patch.object(dc, "mutagen", fake_mut):
                hdr, fmt, err = dc.probe_extra_audio(str(p))
                self.assertIsNone(hdr)
                self.assertIsNone(fmt)
                self.assertTrue(err and err.startswith("unreadable by mutagen: "))

            # Vanished file raises FileNotFoundError.
            with self.assertRaises(FileNotFoundError):
                dc.probe_extra_audio(str(root / "missing.flac"))

    def test_extra_readable_flac_in_album_dir_passes_with_real_mutagen_length(self):
        # Regression test for 2026-11-25 live bug: a valid FLAC arrives in the
        # album directory after albums_in_dir() snapshotted it (e.g. concurrent
        # slskd download during rescue-sweep), so it is absent from task.items.
        # Must probe its real header duration via mutagen and PASS instead of
        # hardcoding header_s=0.0 and falsely rejecting as no_duration.
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            alb = downloads / "The Complete Ella Fitzgerald Song Books [8ad0f3cc]"
            alb.mkdir(parents=True)
            f1 = alb / "01 Track.flac"
            f_extra = alb / "04 - Somebody From Somewhere.flac"
            shutil.copyfile(self.good_flac, f1)
            shutil.copyfile(self.good_flac, f_extra)

            class FLAC:
                info = types.SimpleNamespace(length=20.0)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            # Only f1 is in task.items; f_extra is present on disk in the album dir.
            task = _FakeImportTask(
                str(alb).encode(),
                [str(alb).encode()],
                [_FakeItem(str(f1), length=20.0, fmt="FLAC", track=1)],
            )
            fake_mut = types.SimpleNamespace(File=lambda _p: FLAC())
            with mock.patch.object(dc, "mutagen", fake_mut):
                out = plugin.on_import_task_created(None, task)

            self.assertEqual(out, [task])
            self.assertFalse((state_dir / "rejects.jsonl").exists())
            checked = [
                json.loads(ln)
                for ln in (state_dir / "checked.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(checked), 1)
            self.assertEqual(checked[0]["files"], 2)

    def test_extra_unreadable_flac_rejected_as_decode_error_not_no_duration(self):
        # Genuinely unreadable extra file (mutagen raises or returns None) must
        # still reject & quarantine the whole album, with reason decode_error
        # and detail "unreadable by mutagen: <exc>" (never no_duration).
        for mode in ("raises", "returns_none"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                downloads = Path(tmp) / "downloads"
                state_dir = downloads / ".decodecheck"
                alb = downloads / "Album With Unreadable Extra"
                alb.mkdir(parents=True)
                f1 = alb / "01 Good.flac"
                f_bad = alb / "05 Corrupt.flac"
                shutil.copyfile(self.good_flac, f1)
                f_bad.write_bytes(b"\x00" * 64)

                def _fake_open(_p, _mode=mode):
                    if _mode == "raises":
                        raise RuntimeError("not a valid FLAC file")
                    return None

                plugin = dc.DecodeCheckPlugin()
                plugin.config.set("state_dir", str(state_dir))
                plugin.config.set("ffmpeg", FFMPEG_BIN)
                task = _FakeImportTask(
                    str(alb).encode(),
                    [str(alb).encode()],
                    [_FakeItem(str(f1), length=20.0, fmt="FLAC", track=1)],
                )
                fake_mut = types.SimpleNamespace(File=_fake_open)
                with mock.patch.object(dc, "mutagen", fake_mut):
                    out = plugin.on_import_task_created(None, task)

                self.assertEqual(out, [])
                self.assertFalse(alb.exists())
                recs = [
                    json.loads(ln)
                    for ln in (state_dir / "rejects.jsonl").read_text().splitlines()
                ]
                self.assertEqual(len(recs), 1)
                rec = recs[0]
                self.assertEqual(rec["reason"], "decode_error")
                self.assertEqual(rec["basename"], "05 Corrupt.flac")
                self.assertTrue(
                    rec["detail"].startswith("unreadable by mutagen: "),
                    rec["detail"],
                )
                if mode == "raises":
                    self.assertIn("not a valid FLAC file", rec["detail"])
                self.assertIsNone(rec["header_s"])
                self.assertIsNone(rec["decoded_s"])
                self.assertEqual(rec["task_files"], 2)
                self.assertEqual(rec["task_bad"], 1)
                q_dir = Path(rec["quarantined_to"]).parent
                self.assertTrue((q_dir / "01 Good.flac").exists())
                self.assertTrue((q_dir / "05 Corrupt.flac").exists())

    def test_extra_zero_duration_and_corrupt_body_flacs_still_rejected(self):
        # Fail-closed check: even when mutagen opens an extra file, a genuine
        # total_samples=0 FLAC still rejects as no_duration, and a corrupt-body
        # FLAC with a valid header still rejects as decode_error.
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)

            class FLAC:
                def __init__(self, length):
                    self.info = types.SimpleNamespace(length=length)

            # Case 1: genuine zero-duration FLAC extra -> no_duration
            alb1 = downloads / "Zero Extra"
            alb1.mkdir(parents=True)
            f1 = alb1 / "01 Good.flac"
            f2 = alb1 / "02 ZeroDur.flac"
            shutil.copyfile(self.good_flac, f1)
            shutil.copyfile(self.nodur_flac, f2)
            t1 = _FakeImportTask(
                str(alb1).encode(),
                [str(alb1).encode()],
                [_FakeItem(str(f1), length=20.0, fmt="FLAC", track=1)],
            )
            with mock.patch.object(
                dc, "mutagen", types.SimpleNamespace(File=lambda _p: FLAC(0.0))
            ):
                self.assertEqual(plugin.on_import_task_created(None, t1), [])

            # Case 2: valid header but corrupt audio body extra -> decode_error
            alb2 = downloads / "Corrupt Body Extra"
            alb2.mkdir(parents=True)
            g1 = alb2 / "01 Good.flac"
            g2 = alb2 / "02 CorruptBody.flac"
            shutil.copyfile(self.good_flac, g1)
            shutil.copyfile(self.corrupt_flac, g2)
            t2 = _FakeImportTask(
                str(alb2).encode(),
                [str(alb2).encode()],
                [_FakeItem(str(g1), length=20.0, fmt="FLAC", track=1)],
            )
            with mock.patch.object(
                dc, "mutagen", types.SimpleNamespace(File=lambda _p: FLAC(20.0))
            ):
                self.assertEqual(plugin.on_import_task_created(None, t2), [])

            recs = [
                json.loads(ln)
                for ln in (state_dir / "rejects.jsonl").read_text().splitlines()
            ]
            by_base = {r["basename"]: r for r in recs}
            self.assertEqual(by_base["02 ZeroDur.flac"]["reason"], "no_duration")
            self.assertEqual(by_base["02 ZeroDur.flac"]["header_s"], 0.0)
            self.assertEqual(by_base["02 CorruptBody.flac"]["reason"], "decode_error")
            self.assertEqual(by_base["02 CorruptBody.flac"]["header_s"], 20.0)

    def test_extra_vanished_during_probe_aborts_and_symlink_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            downloads = Path(tmp) / "downloads"
            state_dir = downloads / ".decodecheck"
            real_alb = downloads / "Real Album"
            real_alb.mkdir(parents=True)
            link_alb = downloads / "Link Album"
            link_alb.symlink_to(real_alb)
            f1 = real_alb / "01 Track.flac"
            shutil.copyfile(self.good_flac, f1)

            # Symlinked task_path vs realpath item_path -> not treated as extra.
            extras = dc._find_unreadable_album_audio(
                [str(link_alb)], [str(f1)], str(state_dir)
            )
            self.assertEqual(extras, [])

            # Vanished extra during probe -> aborts without reject or quarantine.
            f_extra = real_alb / "02 Vanishing.flac"
            shutil.copyfile(self.good_flac, f_extra)

            def _vanish(p):
                os.unlink(p)
                raise FileNotFoundError(p)

            plugin = dc.DecodeCheckPlugin()
            plugin.config.set("state_dir", str(state_dir))
            plugin.config.set("ffmpeg", FFMPEG_BIN)
            task = _FakeImportTask(
                str(real_alb).encode(),
                [str(real_alb).encode()],
                [_FakeItem(str(f1), length=20.0, fmt="FLAC", track=1)],
            )
            with mock.patch.object(
                dc, "mutagen", types.SimpleNamespace(File=_vanish)
            ):
                self.assertEqual(plugin.on_import_task_created(None, task), [])
            self.assertFalse((state_dir / "rejects.jsonl").exists())
            self.assertFalse((state_dir / "quarantine").exists())


if __name__ == "__main__":
    unittest.main()
