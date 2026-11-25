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


if __name__ == "__main__":
    unittest.main()
