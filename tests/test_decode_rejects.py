#!/usr/bin/env python3
"""Tests for librarian's side of the decode gate (docs/decode-gate.md).

Covers: downloads rows at queue time, the incremental rejects.jsonl reader,
reject -> peer matching, candidate/upgrade handling, peer blocking and the
decode_gate status section. Stdlib unittest only.
"""

import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import librarian  # noqa: E402

DAY = 86400
REMOTE_DIR = "Sensation (2024)"
FILE1 = "Devault - Sensation - 01 - Fire to Stone.flac"
FILE2 = "Devault - Sensation - 02 - Behind the Wheel.flac"


def make_item(peer, basename, remote_dir=REMOTE_DIR, quality="flac"):
    """A search-poll item in the real Soulbeet 2026-11 shape."""
    remote = f"music\\Devault\\{remote_dir}\\{basename}"
    return {
        "id": remote,
        "source": peer,
        "title": basename.rsplit(".", 1)[0],
        "artist": "Devault",
        "album": "Sensation",
        "size": 25225167,
        "duration": 190,
        "quality": quality,
        "quality_score": 1.0,
        "backend_data": json.dumps({"username": peer, "filename": remote, "size": 25225167}),
        "match_score": 0.67,
        "below_floor": False,
        "facts": {"extension": quality, "bitrate_kbps": None, "sample_rate_hz": 44100,
                  "bit_depth": 16, "folder_audio_files": 7},
    }


def make_group(peer, items, score=1.0, quality="flac"):
    return {"source": peer, "group_id": f"{peer}:{REMOTE_DIR}", "title": "Sensation",
            "artist": "Devault", "item_count": len(items),
            "total_size": sum(i["size"] for i in items), "quality": quality,
            "score": score, "items": items}


def reject_line(basename=FILE1, task_id="20261124T163000-ab12cd", reason="decode_error",
                remote_dir=REMOTE_DIR, ts=None, **extra):
    rec = {"v": 1, "ts": int(time.time() if ts is None else ts), "task_id": task_id,
           "reason": reason, "detail": "Invalid data found when processing input",
           "header_s": 212.0, "decoded_s": 29.8,
           "staging_path": f"/downloads/{remote_dir}/{basename}",
           "basename": basename,
           "quarantined_to": f"/downloads/.decodecheck/quarantine/2026-11-24/{task_id}/"
                             f"{remote_dir}/{basename}",
           "artist": "Devault", "album": "Sensation", "title": None,
           "albumartist": "Devault", "track": 1, "task_files": 7, "task_bad": 1,
           "album_mode": True}
    rec.update(extra)
    return json.dumps(rec) + "\n"


class DecodeGateBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(tmp.name)
        patches = [
            mock.patch.object(librarian, "DATA_DIR", self.tmp),
            mock.patch.object(librarian, "STATE_DB", self.tmp / "state.db"),
            mock.patch.object(librarian, "log", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        self.st = librarian.State()
        self.gate_dir = self.tmp / "slskd_downloads" / ".decodecheck"
        self.gate_dir.mkdir(parents=True)
        self.rejects = self.gate_dir / "rejects.jsonl"
        self.cfg = {"DECODE_REJECTS_PATH": str(self.rejects), "DECODE_MAX_RETRIES": 3,
                    "BAD_PEER_STRIKES": 3, "BAD_PEER_DAYS": 90}

        def cleanup():
            self.st.conn.close()
            for p in reversed(patches):
                p.stop()
            tmp.cleanup()
        self.addCleanup(cleanup)

    # helpers ---------------------------------------------------------------
    def add_queued_candidate(self, artist="Devault", album="Sensation"):
        self.st.add_candidate(artist, album, "album")
        key = [r[0] for r in self.st.pending_candidates(limit=50)
               if r[1] == artist and r[2] == album][0]
        self.st.set_status(key, "queued_flac", fmt="flac", kbps=900)
        return key

    def queue_from(self, key, peer, files=(FILE1, FILE2), kind="candidate", ts=None,
                   remote_dir=REMOTE_DIR):
        self.st.record_downloads(kind, key, [make_item(peer, f, remote_dir) for f in files],
                                 "sid-1", ts=ts)

    def append(self, *lines):
        with open(self.rejects, "a", encoding="utf-8") as f:
            f.write("".join(lines))

    def ingest(self):
        return librarian.ingest_decode_rejects(self.st, self.cfg)

    def cand(self, key):
        return self.st.conn.execute(
            "SELECT status, decode_retries FROM candidates WHERE key=?", (key,)).fetchone()

    def bad_rows(self):
        return self.st.conn.execute(
            "SELECT username, ref_key, reason, basename FROM bad_sources ORDER BY id").fetchall()


class TestDownloadsRecordedAtQueue(DecodeGateBase):
    def _sb(self, groups):
        sb = mock.MagicMock()
        sb.search_album.return_value = [{"id": "mbid", "title": "Sensation", "artist": "Devault"}]
        sb.start_search.return_value = "sid-42"
        sb.poll.return_value = {"state": "Done", "groups": groups}
        sb.queue.return_value = [{"id": "x", "status": "Enqueued"}]
        return sb

    def test_process_candidate_records_one_row_per_item(self):
        self.st.add_candidate("Devault", "Sensation", "album")
        row = self.st.pending_candidates(limit=1)[0]
        sb = self._sb([make_group("matthewaasi", [make_item("matthewaasi", FILE1),
                                                  make_item("matthewaasi", FILE2)])])
        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False):
            res = librarian.process_candidate(self.st, sb, {"SOULBEET_USER": "u",
                                                            "SOULBEET_PASS": "p"},
                                              row, cfg=self.cfg)
        self.assertEqual(res, "queued_flac")
        rows = self.st.conn.execute(
            "SELECT kind, ref_key, username, filename, basename, remote_dir, search_id "
            "FROM downloads ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("candidate", row[0], "matthewaasi",
                                   f"music\\Devault\\{REMOTE_DIR}\\{FILE1}", FILE1,
                                   REMOTE_DIR, "sid-42"))

    def test_process_candidate_skips_blocked_peer(self):
        self.st.add_candidate("Devault", "Sensation", "album")
        row = self.st.pending_candidates(limit=1)[0]
        self.st.add_bad_source("bananabean84627", row[0], "decode_error", FILE1)
        sb = self._sb([
            make_group("bananabean84627", [make_item("bananabean84627", FILE1)], score=1.0),
            make_group("goodpeer", [make_item("goodpeer", FILE1)], score=0.5),
        ])
        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False):
            librarian.process_candidate(self.st, sb, {"SOULBEET_USER": "u",
                                                      "SOULBEET_PASS": "p"},
                                        row, cfg=self.cfg)
        queued = sb.queue.call_args[0][0]
        self.assertEqual({i["source"] for i in queued}, {"goodpeer"})

    def test_upgrade_one_records_rows(self):
        sb = self._sb([make_group("peerx", [make_item("peerx", FILE1)])])
        cand = {"key": "devault|sensation", "artist": "Devault", "album": "Sensation",
                "dir": "/music/Devault/Sensation", "codec": "mp3", "kbps": 192, "tracks": 7}
        res = librarian.upgrade_one(self.st, sb, cand, self.cfg)
        self.assertEqual(res, "queued")
        rows = self.st.conn.execute(
            "SELECT kind, ref_key, username, basename FROM downloads").fetchall()
        self.assertEqual(rows, [("upgrade", "devault|sensation", "peerx", FILE1)])

    def test_backend_data_fallback_and_split(self):
        it = make_item("p", FILE1)
        del it["source"], it["id"]
        self.assertEqual(librarian.item_peer(it), "p")
        self.assertEqual(librarian.split_remote_filename(librarian.item_filename(it)),
                         (FILE1, REMOTE_DIR))
        self.assertEqual(librarian.split_remote_filename("a/b/c.flac"), ("c.flac", "b"))
        self.assertEqual(librarian.split_remote_filename("c.flac"), ("c.flac", ""))

    def test_prune_downloads(self):
        now = int(time.time())
        self.queue_from("k", "old", files=(FILE1,), ts=now - 31 * DAY)
        self.queue_from("k", "new", files=(FILE1,), ts=now - DAY)
        self.assertEqual(librarian.maybe_prune_downloads(self.st, now=now), 1)
        # throttled: a second call within a day does nothing
        self.queue_from("k", "old2", files=(FILE1,), ts=now - 40 * DAY)
        self.assertEqual(librarian.maybe_prune_downloads(self.st, now=now + 60), 0)
        self.assertEqual(self.st.conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0], 2)


class TestIncrementalReader(DecodeGateBase):
    def test_missing_file_is_noop(self):
        self.assertEqual(self.ingest(), 0)
        self.assertIsNone(self.st.get_meta("decode_rejects_cursor"))

    def test_offset_persists_and_only_new_lines_are_read(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1")
        self.append(reject_line(task_id="t1"))
        self.assertEqual(self.ingest(), 1)
        cur = json.loads(self.st.get_meta("decode_rejects_cursor"))
        self.assertEqual(cur["offset"], self.rejects.stat().st_size)
        self.assertEqual(cur["ino"], self.rejects.stat().st_ino)
        self.assertEqual(self.ingest(), 0)  # nothing new
        self.append(reject_line(task_id="t2", basename=FILE2))
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self.cand(key), ("pending", 2))

    def test_partial_last_line_waits_for_newline(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1")
        line = reject_line(task_id="t1")
        self.append(line[:40])
        self.assertEqual(self.ingest(), 0)
        cur = json.loads(self.st.get_meta("decode_rejects_cursor"))
        self.assertEqual(cur["offset"], 0)
        self.append(line[40:])
        self.assertEqual(self.ingest(), 1)

    def test_bad_json_is_skipped(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1")
        self.append("{not json\n", "[1,2]\n", "\n", reject_line(task_id="t1"))
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self.cand(key), ("pending", 1))

    def test_truncation_restarts_from_zero(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1")
        self.append(reject_line(task_id="t1"), reject_line(task_id="t1", basename=FILE2))
        self.ingest()
        # truncate in place (same inode) and write a shorter, new line
        with open(self.rejects, "w", encoding="utf-8") as f:
            f.write(reject_line(task_id="t9"))
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self.st.conn.execute(
            "SELECT COUNT(*) FROM decode_rejects WHERE task_id='t9'").fetchone()[0], 1)

    def test_rotation_new_inode_reads_from_start(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1")
        self.append(reject_line(task_id="t1"))
        self.ingest()
        os.rename(self.rejects, self.gate_dir / "rejects.jsonl.1")
        # pad so the new file is longer than the old offset: inode alone must trigger reset
        self.append(reject_line(task_id="t2", detail="x" * 500))
        self.assertEqual(self.ingest(), 1)

    def test_dedupe_on_reread(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1")
        self.append(reject_line(task_id="t1"))
        self.ingest()
        self.st.set_meta("decode_rejects_cursor", "")  # forget the offset
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(self.cand(key), ("pending", 1))
        self.assertEqual(len(self.bad_rows()), 1)


class TestMatchingAndHandling(DecodeGateBase):
    def test_match_tie_break_by_remote_dir_then_recency(self):
        now = int(time.time())
        self.queue_from("k1", "peer_a", files=(FILE1,), remote_dir="Other Dir", ts=now - 100)
        self.queue_from("k2", "peer_b", files=(FILE1,), remote_dir=REMOTE_DIR, ts=now - 3000)
        self.queue_from("k3", "peer_c", files=(FILE1,), remote_dir="Third", ts=now - 50)
        m = self.st.match_download(FILE1, staging_parent=REMOTE_DIR, reject_ts=now)
        self.assertEqual(m["username"], "peer_b")
        # three albums share the basename and none matches the dir: ambiguous,
        # so nobody is blamed (finding: generic basenames blamed innocent peers)
        self.assertIsNone(self.st.match_download(FILE1, staging_parent="Unknown",
                                                 reject_ts=now))
        # ...unless the reject's tags name one of them
        m = self.st.match_download(FILE1, staging_parent="Unknown", reject_ts=now,
                                   tag_keys=["k1"])
        self.assertEqual(m["username"], "peer_a")
        # outside the 7-day window -> no match
        self.assertIsNone(self.st.match_download(FILE1, reject_ts=now + 8 * DAY))
        # a download queued after the reject can't be its source
        self.assertEqual(self.st.match_download(FILE1, reject_ts=now - 2000)["username"],
                         "peer_b")

    def test_requeue_then_corrupt_only(self):
        self.cfg["DECODE_MAX_RETRIES"] = 2
        key = self.add_queued_candidate()
        for i, peer in enumerate(("bad1", "bad2", "bad3"), 1):
            self.queue_from(key, peer, files=(FILE1,))
            self.append(reject_line(task_id=f"t{i}"))
            self.ingest()
            if i <= 2:
                self.assertEqual(self.cand(key), ("pending", i))
                self.st.set_status(key, "queued_flac")
        self.assertEqual(self.cand(key)[0], "corrupt_only")
        self.assertEqual([r[0] for r in self.bad_rows()], ["bad1", "bad2", "bad3"])
        self.assertEqual(self.st.blocked_peers(key), {"bad1", "bad2", "bad3"})

    def test_whole_task_requeues_once(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bananabean84627", files=(FILE1, FILE2, "03.flac"))
        self.append(reject_line(task_id="tA", basename=FILE1, task_bad=3),
                    reject_line(task_id="tA", basename=FILE2, task_bad=3))
        self.ingest()
        # third bad file of the same task arrives on a later read
        self.append(reject_line(task_id="tA", basename="03.flac", task_bad=3))
        self.ingest()
        self.assertEqual(self.cand(key), ("pending", 1))
        self.assertEqual(len(self.bad_rows()), 3)
        # ...and the three rows count as ONE global strike
        self.assertNotIn("bananabean84627",
                         self.st.global_blocked_peers(strikes=2))

    def test_upgrade_marked_failed(self):
        ukey = "devault|sensation"
        self.st.upsert_upgrade(ukey, artist="Devault", album="Sensation", status="queued")
        self.queue_from(ukey, "peerx", files=(FILE1,), kind="upgrade")
        self.append(reject_line(task_id="tU", reason="truncated"))
        self.ingest()
        status, note = self.st.conn.execute(
            "SELECT status, note FROM upgrades WHERE key=?", (ukey,)).fetchone()
        self.assertEqual(status, "failed")
        self.assertEqual(note, "decode reject: truncated from peerx")
        self.assertEqual(self.st.blocked_peers(ukey), {"peerx"})

    def test_checker_unavailable_does_not_block_or_consume(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "innocent", files=(FILE1,))
        self.append(reject_line(task_id="tC", reason="checker_unavailable"))
        self.ingest()
        self.assertEqual(self.cand(key), ("queued_flac", 0))
        self.assertEqual(self.bad_rows(), [])
        self.assertEqual(self.st.blocked_peers(key), frozenset())
        self.assertTrue(librarian.decode_gate_status(self.st, self.cfg)["checker_warning"])

    def test_same_remote_dir_identifies_album_without_peer(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "peer_a", files=(FILE1,))
        self.append(reject_line(task_id="tD", basename="unlisted.flac",
                                artist="Else", albumartist="Else", album="Other"))
        self.ingest()
        self.assertEqual(self.cand(key), ("pending", 1))
        self.assertEqual(self.bad_rows(), [])

    def test_unmatched_counted_only(self):
        key = self.add_queued_candidate()
        self.append(reject_line(task_id="tX", basename="nobody queued this.flac",
                                artist="Else", albumartist="Else", album="Other"))
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(self.cand(key), ("queued_flac", 0))
        self.assertEqual(self.bad_rows(), [])
        dg = librarian.decode_gate_status(self.st, self.cfg)
        self.assertEqual((dg["matched"], dg["unmatched"]), (0, 1))


class TestReviewFixes(DecodeGateBase):
    def test_slskd_name_key(self):
        k = librarian.slskd_name_key
        self.assertEqual(k("01 - Road to Hell: Intro.flac"), k("01 - Road to Hell_ Intro.flac"))
        self.assertEqual(k("02 - Romance With a Memory.flac"),
                         k("02 - Romance With a Memory_639207859713545150.flac"))
        self.assertEqual(k("Hadestown: Original"), k("Hadestown_ Original"))
        self.assertEqual(k("Cafe\u0301.flac"), k("Caf\u00e9.flac"))  # NFD vs NFC
        self.assertNotEqual(k("Track_12.flac"), k("Track.flac"))  # short digits kept

    def test_sanitized_and_ticks_names_match(self):
        for remote, local, rdir, ldir in (
                ("01 - Road to Hell: Intro.flac", "01 - Road to Hell_ Intro.flac",
                 "Real: Album", "Real_ Album"),
                ("02 - Romance.flac", "02 - Romance_639207859713545150.flac",
                 "Hideous Bastard (2022)", "Hideous Bastard (2022)")):
            with self.subTest(local=local):
                key = self.add_queued_candidate(album=f"A {local}")
                self.queue_from(key, "badpeer", files=(remote,), remote_dir=rdir)
                self.append(reject_line(task_id=f"t-{local}", basename=local,
                                        remote_dir=ldir, album=f"A {local}"))
                self.ingest()
                self.assertEqual(self.cand(key), ("pending", 1))
                self.assertIn("badpeer", self.st.blocked_peers(key))

    def test_generic_basename_does_not_blame_other_album(self):
        now = int(time.time())
        k1 = self.add_queued_candidate(album="Real: Album")
        k2 = self.add_queued_candidate(album="Other")
        self.queue_from(k1, "guilty", files=("01 - Intro.flac",), remote_dir="Real: Album",
                        ts=now - 600)
        self.queue_from(k2, "innocent", files=("01 - Intro.flac",), remote_dir="Other",
                        ts=now - 60)
        self.append(reject_line(task_id="tG", basename="01 - Intro.flac",
                                remote_dir="Real_ Album", album="Real: Album"))
        self.ingest()
        self.assertEqual(self.cand(k1), ("pending", 1))
        self.assertEqual(self.cand(k2), ("queued_flac", 0))
        self.assertEqual([r[0] for r in self.bad_rows()], ["guilty"])

    def test_ambiguous_generic_basename_blames_nobody(self):
        now = int(time.time())
        k1 = self.add_queued_candidate(album="One")
        k2 = self.add_queued_candidate(album="Two")
        self.queue_from(k1, "p1", files=("01.flac",), remote_dir="CD1", ts=now - 600)
        self.queue_from(k2, "p2", files=("01.flac",), remote_dir="CD1", ts=now - 60)
        # staging dir and tags match neither album -> ambiguous, nobody blamed
        self.append(reject_line(task_id="tAmb", basename="01.flac", remote_dir="Disc 1",
                                artist="X", albumartist="X", album="Y"))
        self.ingest()
        self.assertEqual(self.cand(k1), ("queued_flac", 0))
        self.assertEqual(self.cand(k2), ("queued_flac", 0))
        self.assertEqual(self.bad_rows(), [])

    def test_tag_fallback_retries_album_without_blaming(self):
        key = self.add_queued_candidate()
        self.append(reject_line(task_id="tT", basename="renamed beyond recognition.flac"))
        self.ingest()
        self.assertEqual(self.cand(key), ("pending", 1))
        self.assertEqual(self.bad_rows(), [])

    def test_decode_retry_keeps_place_in_line(self):
        for i in range(5):
            self.st.add_candidate(f"Backlog {i}", "LP", "album")
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1", files=(FILE1,))
        self.append(reject_line(task_id="tQ"))
        self.ingest()
        self.assertEqual(self.st.pending_candidates(limit=1)[0][0], key)

    def test_checker_unavailable_repended_after_recovery(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "innocent", files=(FILE1,))
        now = int(time.time())
        self.append(reject_line(task_id="tC", reason="checker_unavailable", ts=now - 100))
        self.ingest()
        self.assertEqual(librarian.repend_checker_unavailable(self.st, self.cfg), 0)
        self.assertEqual(self.cand(key), ("queued_flac", 0))  # no proof yet
        checked = self.gate_dir / "checked.jsonl"
        checked.write_text(json.dumps({"ts": now - 200, "task_id": "old"}) + "\n")
        self.assertEqual(librarian.repend_checker_unavailable(self.st, self.cfg), 0)
        with open(checked, "a") as f:
            f.write(json.dumps({"ts": now, "task_id": "ok"}) + "\n")
        self.assertEqual(librarian.repend_checker_unavailable(self.st, self.cfg), 1)
        self.assertEqual(self.cand(key), ("pending", 0))  # no retry consumed
        self.assertEqual(self.bad_rows(), [])
        self.st.set_status(key, "queued_flac")
        self.assertEqual(librarian.repend_checker_unavailable(self.st, self.cfg), 0)  # once

    def test_checker_unavailable_repend_is_capped(self):
        self.cfg["DECODE_MAX_RETRIES"] = 2
        key = self.add_queued_candidate()
        now = int(time.time())
        (self.gate_dir / "checked.jsonl").write_text(
            json.dumps({"ts": now + 1000, "task_id": "ok"}) + "\n")
        results = []
        for i in range(4):
            self.queue_from(key, f"p{i}", files=(FILE1,), ts=now - 100 + i)
            self.append(reject_line(task_id=f"tk{i}", reason="checker_unavailable",
                                    ts=now - 100 + i))
            self.ingest()
            results.append(librarian.repend_checker_unavailable(self.st, self.cfg))
            self.st.set_status(key, "queued_flac")
        self.assertEqual(results, [1, 1, 0, 0])

    def test_migrate_backfills_norm_columns(self):
        self.st.conn.execute(
            "INSERT INTO downloads(ts, kind, ref_key, username, basename, remote_dir) "
            "VALUES(?,?,?,?,?,?)", (int(time.time()), "candidate", "k", "p", "A: B.flac", "D"))
        self.st.conn.commit()
        librarian._migrate(self.st.conn)
        self.assertEqual(self.st.match_download("A_ B.flac")["username"], "p")


class TestDrainStagingSkipsDotDirs(unittest.TestCase):
    def test_decodecheck_dir_never_drained(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "drain_staging", REPO_ROOT / "drain-staging.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as d:
            for n in (".decodecheck", "Album (2020)", ".hidden"):
                os.mkdir(os.path.join(d, n))
            open(os.path.join(d, "loose.flac"), "w").close()
            self.assertEqual(mod.staging_folders(d), ["Album (2020)"])


class TestPeerBlocking(DecodeGateBase):
    def test_ladder_skips_blocked_items_and_groups(self):
        mixed = make_group("goodpeer", [make_item("goodpeer", FILE1),
                                        make_item("badpeer", FILE2)], score=1.0)
        bad = make_group("badpeer", [make_item("badpeer", FILE1)], score=2.0)
        status, items, _ = librarian.pick_by_quality_ladder([bad, mixed],
                                                            blocked=frozenset({"badpeer"}))
        self.assertEqual(status, "queued_flac")
        self.assertEqual([i["source"] for i in items], ["goodpeer"])
        # without blocking, the higher-scored bad group wins
        _, items, _ = librarian.pick_by_quality_ladder([bad, mixed])
        self.assertEqual([i["source"] for i in items], ["badpeer"])
        # every source blocked -> no_source
        self.assertEqual(librarian.pick_by_quality_ladder(
            [bad], blocked=frozenset({"badpeer"}))[0], "no_source")
        # a group whose own source is blocked is skipped even if its items look clean
        odd = make_group("badpeer", [make_item("goodpeer", FILE1)])
        self.assertEqual(librarian.pick_by_quality_ladder(
            [odd], blocked=frozenset({"badpeer"}))[0], "no_source")

    def test_per_key_and_global_strikes_with_window(self):
        now = int(time.time())
        self.st.add_bad_source("p1", "album-a", "decode_error", FILE1, ts=now - DAY)
        self.assertEqual(self.st.blocked_peers("album-a", now=now), {"p1"})
        self.assertEqual(self.st.blocked_peers("album-b", now=now), frozenset())
        self.st.add_bad_source("p1", "album-b", "truncated", FILE1, ts=now - DAY)
        self.st.add_bad_source("p1", "album-c", "no_duration", FILE1, ts=now - DAY)
        self.assertIn("p1", self.st.blocked_peers("album-z", strikes=3, days=90, now=now))
        # strikes age out of the global window, per-key blocks don't
        later = now + 91 * DAY
        self.assertNotIn("p1", self.st.blocked_peers("album-z", strikes=3, days=90, now=later))
        self.assertIn("p1", self.st.blocked_peers("album-a", strikes=3, days=90, now=later))


class TestDecodeGateStatus(DecodeGateBase):
    def test_structure_and_counts(self):
        key = self.add_queued_candidate()
        self.queue_from(key, "bad1")
        self.append(reject_line(task_id="t1"),
                    reject_line(task_id="t1", basename=FILE2, reason="truncated"),
                    reject_line(task_id="t2", basename="x.flac", remote_dir="Elsewhere",
                                artist="Else", albumartist="Else", album="Other"))
        self.ingest()
        now = int(time.time())
        with open(self.gate_dir / "checked.jsonl", "w") as f:
            f.write(json.dumps({"ts": now, "task_id": "ok1"}) + "\n")
            f.write(json.dumps({"ts": now - 2 * DAY, "task_id": "ok0"}) + "\n")
        dg = librarian.decode_gate_status(self.st, self.cfg)
        for k in ("rejects_24h", "rejects_7d", "by_reason_7d", "matched", "unmatched",
                  "blocked_peers", "checker_warning", "accepted_24h"):
            self.assertIn(k, dg)
        self.assertEqual(dg["rejects_24h"], 3)
        self.assertEqual(dg["by_reason_7d"], {"decode_error": 2, "truncated": 1})
        self.assertEqual((dg["matched"], dg["unmatched"]), (2, 1))
        self.assertEqual(dg["accepted_24h"], 1)
        self.assertEqual(dg["blocked_peers"], 0)  # one album = one strike
        self.assertFalse(dg["checker_warning"])
        json.dumps(dg)

    def test_missing_gate_dir_warns(self):
        self.cfg["DECODE_REJECTS_PATH"] = str(self.tmp / "nope" / ".decodecheck" / "rejects.jsonl")
        dg = librarian.decode_gate_status(self.st, self.cfg)
        self.assertTrue(dg["checker_warning"])
        self.assertIn("not installed", dg["checker_warning_reason"])
        self.assertIsNone(dg["accepted_24h"])

    def test_write_status_includes_decode_gate(self):
        cfg = librarian.load_config()
        cfg.update(self.cfg)
        out = self.tmp / "status.json"
        with mock.patch.object(librarian, "STATUS_JSON", out), \
             mock.patch.object(librarian, "free_gb", return_value=100.0):
            librarian.write_status(self.st, cfg, 0, 0, 100.0)
        s = json.loads(out.read_text())
        self.assertIn("decode_gate", s)
        self.assertIn("corrupt_only", s)
        self.assertFalse(s["decode_gate"]["checker_warning"])


if __name__ == "__main__":
    unittest.main()
