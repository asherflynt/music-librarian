#!/usr/bin/env python3
"""Unit tests for LIBRARY_SCAN_ROOT path translation, auto-detection, and
non-blocking background library scanning.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import librarian  # noqa: E402
import web  # noqa: E402


def _make_tree(root, files, mtime=1_700_000_000):
    """Create files under `root` (dict of rel_path -> bytes) with deterministic mtimes."""
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    dirs_seen = set()
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        os.utime(p, (mtime, mtime))
        cur = p.parent
        while cur != root and cur not in dirs_seen:
            dirs_seen.add(cur)
            cur = cur.parent
    # Set directory mtimes deepest-first so creating children doesn't clobber them.
    for d in sorted(dirs_seen, key=lambda x: len(x.parts), reverse=True):
        os.utime(d, (mtime, mtime))
    os.utime(root, (mtime, mtime))


class TempStateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(tmp.name)
        self._patches = [
            mock.patch.object(librarian, "DATA_DIR", self.tmp),
            mock.patch.object(librarian, "STATE_DB", self.tmp / "state.db"),
            mock.patch.object(librarian, "STATUS_JSON", self.tmp / "status.json"),
            mock.patch.object(librarian, "log", mock.MagicMock()),
        ]
        for p in self._patches:
            p.start()
        self.st = librarian.State()

        def cleanup():
            self.st.conn.close()
            for p in reversed(self._patches):
                p.stop()
            tmp.cleanup()

        self.addCleanup(cleanup)


class TestPathTranslation(unittest.TestCase):
    def test_scan_root_to_music_and_back(self):
        scan = "/cluster/Media/Music"
        music = "/music"
        cases = [
            (scan, music),
            (f"{scan}/Non-Album.mp3", f"{music}/Non-Album.mp3"),
            (f"{scan}/Artist/Album/01 Track.flac", f"{music}/Artist/Album/01 Track.flac"),
            (f"{scan}/Artist/Album/Disc 1/02 Track (Deluxe).m4a",
             f"{music}/Artist/Album/Disc 1/02 Track (Deluxe).m4a"),
        ]
        for s_path, m_path in cases:
            with self.subTest(s_path=s_path):
                self.assertEqual(librarian.translate_library_path(s_path, scan, music), m_path)
                self.assertEqual(librarian.translate_library_path(m_path, music, scan), s_path)

    def test_unicode_and_special_names(self):
        scan = "/cluster/Media/Music"
        music = "/music"
        rels = [
            "Björk/Homogenic/01 Jóga.flac",
            "宇多田ヒカル/First Love/01 Automatic.flac",
            "Sigur Rós/( )/01 Untitled #1 (Vaka).flac",
            "Cafe\u0301 Tacvba/Re/01 Ingrata.mp3",  # NFD combining acute
            "((( O )))/((( 1 )))/01 Moonchild 🌙.flac",
        ]
        for rel in rels:
            with self.subTest(rel=rel):
                s_path = f"{scan}/{rel}"
                m_path = f"{music}/{rel}"
                self.assertEqual(librarian.translate_library_path(s_path, scan, music), m_path)
                self.assertEqual(librarian.translate_library_path(m_path, music, scan), s_path)

    def test_trailing_slashes_and_prefix_boundary(self):
        self.assertEqual(
            librarian.translate_library_path(
                "/cluster/Media/Music/A/B/01.flac",
                "/cluster/Media/Music/",
                "/music/",
            ),
            "/music/A/B/01.flac",
        )
        # Sibling directory with shared string prefix must NOT be rewritten
        self.assertEqual(
            librarian.translate_library_path(
                "/cluster/Media/Music2/A/01.flac",
                "/cluster/Media/Music",
                "/music",
            ),
            "/cluster/Media/Music2/A/01.flac",
        )
        # Identity
        self.assertEqual(
            librarian.translate_library_path("/music/A/01.flac", "/music", "/music"),
            "/music/A/01.flac",
        )


class TestScanRootAutoDetection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = pathlib.Path(self._tmp.name)
        self.music = self.base / "music"
        self.cluster = self.base / "cluster" / "Media" / "Music"

    def test_auto_uses_verified_matching_tree_with_distinct_inodes(self):
        files = {
            "Non-Album.mp3": b"a" * 100,
            "Björk/Homogenic/01 Jóga.flac": b"b" * 2048,
            "Radiohead/In Rainbows/01 15 Step.flac": b"c" * 4096,
        }
        _make_tree(self.music, files, mtime=1_750_000_000)
        _make_tree(self.cluster, files, mtime=1_750_000_000)
        # Sanity: separate directories have distinct inodes, just like FUSE vs ZFS
        self.assertNotEqual(
            os.stat(self.music / "Non-Album.mp3").st_ino,
            os.stat(self.cluster / "Non-Album.mp3").st_ino,
        )
        root, reason = librarian.resolve_scan_root(
            "auto", music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.cluster)
        self.assertIn("auto", reason)

    def test_auto_falls_back_when_candidate_missing(self):
        _make_tree(self.music, {"Artist/Album/01.flac": b"abc"})
        root, _ = librarian.resolve_scan_root(
            "auto", music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.music)

    def test_auto_falls_back_on_size_or_mtime_mismatch(self):
        files = {"Artist/Album/01.flac": b"x" * 1000}
        _make_tree(self.music, files, mtime=1_750_000_000)
        _make_tree(self.cluster, {"Artist/Album/01.flac": b"x" * 999}, mtime=1_750_000_000)
        root, _ = librarian.resolve_scan_root(
            "auto", music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.music)

        # Now same size, different mtime
        _make_tree(self.cluster, files, mtime=1_750_000_123)
        root, _ = librarian.resolve_scan_root(
            "auto", music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.music)

    def test_auto_falls_back_on_missing_or_extra_entries(self):
        _make_tree(
            self.music,
            {"ArtistA/Album/01.flac": b"111", "ArtistB/Album/01.flac": b"222"},
            mtime=1_750_000_000,
        )
        # Candidate missing ArtistB (e.g. share spans multiple pools)
        _make_tree(
            self.cluster,
            {"ArtistA/Album/01.flac": b"111"},
            mtime=1_750_000_000,
        )
        root, _ = librarian.resolve_scan_root(
            "auto", music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.music)

        # Candidate has an extra unrelated top-level dir
        shutil.rmtree(self.cluster)
        _make_tree(
            self.cluster,
            {
                "ArtistA/Album/01.flac": b"111",
                "ArtistB/Album/01.flac": b"222",
                "ArtistC/Album/01.flac": b"333",
            },
            mtime=1_750_000_000,
        )
        root, _ = librarian.resolve_scan_root(
            "auto", music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.music)

    def test_auto_falls_back_when_both_empty(self):
        self.music.mkdir(parents=True)
        self.cluster.mkdir(parents=True)
        root, _ = librarian.resolve_scan_root(
            "auto", music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.music)

    def test_explicit_setting_music_path_or_verified_override_or_mismatch(self):
        files = {"Artist/Album/01.flac": b"hello"}
        _make_tree(self.music, files, mtime=1_750_000_000)
        _make_tree(self.cluster, files, mtime=1_750_000_000)

        # Explicit MUSIC_PATH forces FUSE path even though cluster matches
        root, _ = librarian.resolve_scan_root(
            str(self.music), music_path=self.music, auto_candidate=self.cluster
        )
        self.assertEqual(root, self.music)

        # Explicit valid override
        root, _ = librarian.resolve_scan_root(
            str(self.cluster), music_path=self.music, auto_candidate=self.base / "unused"
        )
        self.assertEqual(root, self.cluster)

        # Explicit nonexistent override falls back safely
        with mock.patch.object(librarian, "log"):
            root, _ = librarian.resolve_scan_root(
                str(self.base / "nonexistent"), music_path=self.music
            )
        self.assertEqual(root, self.music)


class TestMeasureLibraryWithScanRoot(TempStateTestCase):
    def test_matches_existing_canonical_rows_without_reprobing_and_stores_music_paths(self):
        music_root = self.tmp / "music"
        scan_root = self.tmp / "cluster" / "Media" / "Music"
        mtime = 1_750_000_000

        files = {
            "Non-Album.mp3": b"1" * 300,
            "Björk/Homogenic/01 Jóga.flac": b"2" * 500,
            "New Artist/New Album/01 Fresh.mp3": b"3" * 700,
            "_upgrades/Staged Artist/Staged Album/01.flac": b"4" * 900,
        }
        _make_tree(music_root, files, mtime=mtime)
        _make_tree(scan_root, files, mtime=mtime)

        # Pre-seed existing rows under canonical `music_root` prefix (plus one removed file)
        m_str = str(music_root)
        self.st.put_files([
            (f"{m_str}/Non-Album.mp3", m_str, "Unknown", "Singles", "mp3", 0, 320, 300, mtime, 1),
            (f"{m_str}/Björk/Homogenic/01 Jóga.flac", f"{m_str}/Björk/Homogenic",
             "Björk", "Homogenic", "flac", 1, 900, 500, mtime, 1),
            (f"{m_str}/Removed/Old/01.flac", f"{m_str}/Removed/Old",
             "Removed", "Old", "flac", 1, 900, 400, mtime, 1),
        ])

        probed_paths = []
        tags_calls = []

        def fake_probe(path):
            probed_paths.append(path)
            return ("mp3", False, 320)

        def fake_tags(path, fallback_dir):
            tags_calls.append((path, fallback_dir))
            return ("New Artist", "New Album")

        with mock.patch.object(librarian, "probe_file", side_effect=fake_probe), \
                mock.patch.object(librarian, "_tags_of", side_effect=fake_tags):
            total_bytes, track_count = librarian.measure_library(
                self.st, max_probes=100, scan_root=scan_root, music_path=music_root
            )

        # _upgrades file is excluded; 3 real tracks counted
        self.assertEqual(track_count, 3)
        self.assertEqual(total_bytes, 300 + 500 + 700)

        # Only the 1 genuinely new file was probed, and it was read through scan_root!
        self.assertEqual(probed_paths, [f"{scan_root}/New Artist/New Album/01 Fresh.mp3"])
        # _tags_of received scan_root path to read, but canonical music_root dir for fallback
        self.assertEqual(
            tags_calls,
            [(f"{scan_root}/New Artist/New Album/01 Fresh.mp3",
              f"{m_str}/New Artist/New Album")],
        )

        # DB contains ONLY canonical music_root paths — zero scan_root paths, zero duplicates
        rows = self.st.conn.execute(
            "SELECT path, dir, artist, album FROM library_files ORDER BY path"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        stored_paths = [r[0] for r in rows]
        for p, d, _, _ in rows:
            self.assertTrue(p.startswith(m_str + "/"), f"unexpected stored path: {p}")
            self.assertFalse(p.startswith(str(scan_root)), f"scan_root leaked into DB: {p}")
            self.assertFalse(d.startswith(str(scan_root)), f"scan_root leaked into dir: {d}")

        self.assertIn(f"{m_str}/Non-Album.mp3", stored_paths)
        self.assertIn(f"{m_str}/Björk/Homogenic/01 Jóga.flac", stored_paths)
        self.assertIn(f"{m_str}/New Artist/New Album/01 Fresh.mp3", stored_paths)
        self.assertNotIn(f"{m_str}/Removed/Old/01.flac", stored_paths)

        # Upgrade candidate lookup still sees canonical music_root directories
        lossy = self.st.lossy_albums()
        lossy_dirs = {c["dir"] for c in lossy}
        self.assertIn(f"{m_str}/New Artist/New Album", lossy_dirs)
        self.assertEqual(self.st.library_totals(), (1500, 3))


class TestBackgroundLibraryScanner(TempStateTestCase):
    def _base_cfg(self, **overrides):
        cfg = {
            "TARGET_TB": 10.0,
            "TARGET_TRACKS": 500_000,
            "MIN_FREE_GB": 500.0,
            "STAGING_MIN_FREE_GB": 80.0,
            "MEASURE_EVERY_SEC": 300,
            "SCAN_MAX_PROBES_PER_PASS": 250,
            "LIBRARY_SCAN_ROOT": "auto",
            "PAUSED": False,
            "FAVORITES_ENABLED": True,
            "NEW_RELEASE_TOP_ARTISTS": 500,
            "NEW_RELEASE_SYNC_HOURS": 24,
            "DECODE_REJECTS_PATH": str(self.tmp / "rejects.jsonl"),
            "BAD_PEER_STRIKES": 3,
            "BAD_PEER_DAYS": 90,
        }
        cfg.update(overrides)
        return cfg

    def test_background_scan_does_not_block_process_candidate_or_run_concurrently(self):
        # Seed persisted library totals so startup snapshot is already populated
        self.st.put_files([
            ("/music/A/B/01.flac", "/music/A/B", "A", "B", "flac", 1, 900, 1234, 100, 100),
        ])
        self.st.add_candidate("Glass Animals", "ZABA", "album")
        row = self.st.pending_candidates(limit=1)[0]

        scan_entered = threading.Event()
        release_scan = threading.Event()
        measure_calls = 0

        def slow_measure(state, max_probes=None, scan_root=None):
            nonlocal measure_calls
            measure_calls += 1
            scan_entered.set()
            assert release_scan.wait(timeout=5), "test timed out waiting to release scan"
            return (1234 + 5678, 2)

        cfg = self._base_cfg()
        scanner = librarian.LibraryScanner(
            self.st,
            get_cfg=lambda: cfg,
            measure_fn=slow_measure,
            resolve_root_fn=lambda setting: (pathlib.Path("/cluster/Media/Music"), "auto"),
            free_gb_fn=lambda _p: 9000.0,
        )
        # Seeded from state.library_totals() before first scan finishes
        self.assertEqual(scanner.snapshot()[:2], (1234, 1))

        # Trigger background scan
        started = scanner.maybe_start(cfg)
        self.assertTrue(started)
        self.assertTrue(scan_entered.wait(timeout=3), "background scan did not start")
        self.assertTrue(scanner.is_running)

        # Second trigger while running MUST be a no-op (no concurrent scans)
        self.assertFalse(scanner.maybe_start(cfg))
        self.assertFalse(scanner.maybe_start(cfg, force=True))

        # While the scan is blocked mid-walk, process_candidate completes unimpeded!
        sb = mock.MagicMock()
        sb.search_album.return_value = [{"id": "alb1", "release_date": "2014-06-06"}]
        sb.start_search.return_value = "sid-1"
        sb.poll.return_value = {
            "groups": [{
                "source": "goodpeer",
                "quality": "flac",
                "score": 0.95,
                "item_count": 1,
                "items": [{
                    "username": "goodpeer",
                    "filename": "Glass Animals/ZABA/01 Flip.flac",
                    "quality": "flac",
                    "size": 25_000_000,
                    "duration_seconds": 220,
                }],
            }]
        }
        sb.queue.return_value = {"ok": True}
        secrets = {"SOULBEET_USER": "u", "SOULBEET_PASS": "p"}

        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False):
            result = librarian.process_candidate(self.st, sb, secrets, row, cfg=cfg)

        self.assertEqual(result, "queued_flac")
        self.assertEqual(self.st.status_counts().get("queued_flac"), 1)
        self.assertTrue(scanner.is_running, "scan should still be in progress")

        # Release scan and verify STATUS + status.json + updated snapshot
        release_scan.set()
        self.assertTrue(scanner.wait(timeout=5))
        self.assertFalse(scanner.is_running)
        self.assertEqual(measure_calls, 1)
        self.assertEqual(scanner.snapshot(), (1234 + 5678, 2, 9000.0))

        # status.json produced after scan with scan metadata
        status = json.loads((self.tmp / "status.json").read_text())
        self.assertEqual(status["tracks"], 2)
        self.assertEqual(status["queued_flac"], 1)
        self.assertEqual(status["scan_root"], "/cluster/Media/Music")
        self.assertIsNotNone(status["scan_duration_sec"])

        # STATUS log line emitted
        logged = [str(c.args[0]) for c in librarian.log.call_args_list if c.args]
        self.assertTrue(any(m.startswith("STATUS ") for m in logged), f"no STATUS log: {logged}")

        # Within MEASURE_EVERY_SEC, maybe_start does not re-trigger
        self.assertFalse(scanner.maybe_start(cfg))
        # reset_schedule allows triggering again
        scanner.reset_schedule()
        self.assertTrue(scanner.maybe_start(cfg))
        self.assertTrue(scanner.wait(timeout=5))
        self.assertEqual(measure_calls, 2)

    def test_scan_error_clears_running_flag(self):
        cfg = self._base_cfg()
        scanner = librarian.LibraryScanner(
            self.st,
            get_cfg=lambda: cfg,
            measure_fn=mock.MagicMock(side_effect=OSError("disk busy")),
            resolve_root_fn=lambda setting: (pathlib.Path("/music"), "fallback"),
            free_gb_fn=lambda _p: 1000.0,
        )
        self.assertTrue(scanner.maybe_start(cfg))
        self.assertTrue(scanner.wait(timeout=5))
        self.assertFalse(scanner.is_running)
        info = scanner.last_scan_info()
        self.assertIn("disk busy", info["last_error"] or "")


class TestScanRootSettingsAndDocs(unittest.TestCase):
    def test_default_web_coerce_and_readme_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = pathlib.Path(tmp) / "config.env"
            with mock.patch.object(librarian, "CONFIG_ENV", cfg_path), \
                    mock.patch.object(librarian, "DEFAULT_LIBRARY_SCAN_ROOT", "auto"):
                d = librarian.load_config()
        self.assertEqual(d["LIBRARY_SCAN_ROOT"], "auto")

        self.assertIn("LIBRARY_SCAN_ROOT", web.SETTINGS)
        self.assertEqual(web.SETTINGS["LIBRARY_SCAN_ROOT"][0], "str")
        self.assertIn("LIBRARY_SCAN_ROOT", web.HELP)

        self.assertEqual(web.coerce("LIBRARY_SCAN_ROOT", "auto"), "auto")
        self.assertEqual(web.coerce("LIBRARY_SCAN_ROOT", "AUTO"), "auto")
        self.assertEqual(web.coerce("LIBRARY_SCAN_ROOT", ""), "auto")
        self.assertEqual(
            web.coerce("LIBRARY_SCAN_ROOT", "/cluster/Media/Music/"),
            "/cluster/Media/Music",
        )
        for bad in ("relative/path", "/music/../etc", "/music #comment", "/music\nbad"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    web.coerce("LIBRARY_SCAN_ROOT", bad)

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "gen_settings_doc.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
