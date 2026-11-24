#!/usr/bin/env python3
"""Compatibility tests for Soulbeet 2026-11 API changes and favorites cap bypass.

Stdlib unittest only so tests run anywhere `librarian` can be imported.
"""

import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

import requests

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import librarian  # noqa: E402
import web  # noqa: E402


# ---------------------------------------------------------------------------
# Captured real JSON fixtures from Soulbeet (docccccc/soulbeet:full 2026-11-23)
# ---------------------------------------------------------------------------
LIVE_SEARCH_ALBUM_LIST = [
    {
        "kind": "Album",
        "id": "1f682f43-a2d2-4c43-9d61-2176525d4e9a",
        "title": "Sensation",
        "artist": "Devault",
        "release_date": None,
        "mbid": "1f682f43-a2d2-4c43-9d61-2176525d4e9a",
        "release_types": ["Album"],
    }
]

LIVE_SEARCH_TRACK_LIST = [
    {
        "kind": "Track",
        "id": "bbcc25ba-f71d-4e4e-97d1-9e92e76700f8",
        "title": "Behind the Wheel",
        "artist": "Devault, Slayyyter",
        "album_id": "534e9a32-4e10-4b66-a329-9633dc08bc19",
        "album_title": "Behind the Wheel",
        "release_date": "2024-09-13",
        "duration": "03:32",
        "mbid": "bbcc25ba-f71d-4e4e-97d1-9e92e76700f8",
        "release_mbid": "534e9a32-4e10-4b66-a329-9633dc08bc19",
    }
]

LEGACY_SEARCH_ALBUM_DICT = {
    "results": [
        LIVE_SEARCH_ALBUM_LIST[0],
        LIVE_SEARCH_TRACK_LIST[0],
    ]
}

LEGACY_SEARCH_TRACK_DICT = {
    "results": [
        LIVE_SEARCH_ALBUM_LIST[0],
        LIVE_SEARCH_TRACK_LIST[0],
    ]
}

LIVE_SEARCH_ID = "3b356f08-a3b1-4363-bf73-77a1d2caf969"

# Representative Soulbeet 2026-11 poll group/item shapes (sanitized peers).
# Live /api/download/search/poll returned HTTP 409 backend_unreachable (slskd down)
# during probe, so poll fixtures reflect the documented 2026-11 schema including
# string duration, match_score, below_floor, and facts{bitrate_kbps, ...}.
POLL_FLAC_GROUP = {
    "quality": "flac",
    "score": 0.94,
    "item_count": 2,
    "items": [
        {
            "id": "peer1:01.flac",
            "title": "01 Sensation.flac",
            "quality": "flac",
            "quality_score": 0.98,
            "match_score": 0.96,
            "below_floor": False,
            "size": 28_500_000,
            "duration": "04:00",
            "facts": {
                "bitrate_kbps": 950,
                "sample_rate_hz": 44100,
                "bit_depth": 16,
                "source_upload_speed": 2_400_000,
                "source_queue_length": 0,
            },
        },
        {
            "id": "peer1:02.flac",
            "title": "02 Behind the Wheel.flac",
            "quality": "flac",
            "quality_score": 0.97,
            "match_score": 0.95,
            "below_floor": True,  # flagged below floor -> should be skipped
            "size": 24_000_000,
            "duration": "03:32",
            "facts": {
                "bitrate_kbps": 905,
                "sample_rate_hz": 44100,
                "bit_depth": 16,
                "source_upload_speed": 50_000,
                "source_queue_length": 40,
            },
        },
    ],
}

POLL_ALAC_GROUP = {
    "quality": "m4a",
    "score": 0.85,
    "item_count": 1,
    "items": [
        {
            "id": "peer2:01.m4a",
            "title": "01 Sensation.m4a",
            "quality": "m4a",
            "quality_score": 0.80,
            "match_score": 0.91,
            "below_floor": False,
            "size": 26_000_000,
            "duration": "03:32",
            "facts": {
                "bitrate_kbps": 880,
                "sample_rate_hz": 44100,
                "bit_depth": 16,
                "source_upload_speed": 1_500_000,
                "source_queue_length": 1,
            },
        }
    ],
}

POLL_AAC_M4A_GROUP = {
    "quality": "m4a",
    "score": 0.88,
    "item_count": 1,
    "items": [
        {
            "id": "peer3:01.m4a",
            "title": "01 Sensation.m4a",
            "quality": "m4a",
            "quality_score": 0.65,
            "match_score": 0.90,
            "below_floor": False,
            "size": 7_000_000,
            "duration": "03:32",
            "facts": {
                "bitrate_kbps": 256,
                "sample_rate_hz": 44100,
                "bit_depth": None,
                "source_upload_speed": 900_000,
                "source_queue_length": 0,
            },
        }
    ],
}

POLL_MP3_320_GROUP = {
    "quality": "mp3",
    "score": 0.72,
    "item_count": 2,
    "items": [
        {
            "id": "peer4:01_vbr.mp3",
            "title": "01 Behind the Wheel.mp3",
            "quality": "mp3",
            "quality_score": 0.60,
            "match_score": 0.88,
            "below_floor": False,
            "size": 6_400_000,
            "duration": "03:32",
            "facts": {"bitrate_kbps": 240},
        },
        {
            "id": "peer4:01_320.mp3",
            "title": "01 Behind the Wheel (320).mp3",
            "quality": "mp3",
            "quality_score": 0.62,
            "match_score": 0.92,
            "below_floor": False,
            "size": 8_500_000,
            "duration": "03:32",
            "facts": {"bitrate_kbps": 320},
        },
    ],
}


def _make_stub_soulbeet(post_side_effect):
    """Build a Soulbeet instance without making any network calls."""
    sb = object.__new__(librarian.Soulbeet)
    sb.user = "test"
    sb.pw = "test"
    sb._post = mock.Mock(side_effect=post_side_effect)
    return sb


class TestSoulbeetSearchCompat(unittest.TestCase):
    def test_search_album_accepts_bare_list(self):
        sb = _make_stub_soulbeet(lambda path, body, **kw: LIVE_SEARCH_ALBUM_LIST)
        res = sb.search_album("Devault", "Sensation")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["title"], "Sensation")
        self.assertEqual(res[0]["kind"], "Album")

    def test_search_album_accepts_legacy_dict(self):
        sb = _make_stub_soulbeet(lambda path, body, **kw: LEGACY_SEARCH_ALBUM_DICT)
        res = sb.search_album("Devault", "Sensation")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["kind"], "Album")

    def test_search_track_accepts_bare_list_with_mmss_duration(self):
        sb = _make_stub_soulbeet(lambda path, body, **kw: LIVE_SEARCH_TRACK_LIST)
        res = sb.search_track("Devault", "Behind the Wheel")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["kind"], "Track")
        self.assertEqual(res[0]["duration"], "03:32")

    def test_search_track_accepts_legacy_dict(self):
        sb = _make_stub_soulbeet(lambda path, body, **kw: LEGACY_SEARCH_TRACK_DICT)
        res = sb.search_track("Devault", "Behind the Wheel")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["kind"], "Track")

    def test_search_handles_empty_or_null_results(self):
        for payload in ([], {}, {"results": None}, None):
            sb = _make_stub_soulbeet(lambda path, body, p=payload, **kw: p)
            self.assertEqual(sb.search_album("A", "B"), [])
            self.assertEqual(sb.search_track("A", "B"), [])


class TestSoulbeetQueueCompat(unittest.TestCase):
    def test_queue_body_includes_search_id(self):
        calls = []

        def fake_post(path, body, **kw):
            calls.append((path, body))
            return [{"id": "dl-1", "status": "Enqueued"}]

        sb = _make_stub_soulbeet(fake_post)
        items = POLL_FLAC_GROUP["items"][:1]
        res = sb.queue(items, LIVE_SEARCH_ID, target_folder="/music")

        self.assertEqual(res, [{"id": "dl-1", "status": "Enqueued"}])
        self.assertEqual(len(calls), 1)
        path, body = calls[0]
        self.assertEqual(path, "/api/downloads/queue")
        self.assertEqual(
            body,
            {
                "req": {
                    "items": items,
                    "search_id": LIVE_SEARCH_ID,
                    "target_folder": "/music",
                    "backend": None,
                }
            },
        )

    def test_queue_raises_on_failed_item_status(self):
        bad_responses = [
            [{"id": "dl-1", "status": "Enqueued"}, {"id": "dl-2", "status": "Failed"}],
            [{"id": "dl-1", "status": {"Failed": "peer rejected"}}],
            [{"id": "dl-1", "status": "Error", "message": "slskd 403"}],
            [{"id": "dl-1", "status": "Enqueued", "error": "disk full"}],
            [],
        ]
        items = POLL_FLAC_GROUP["items"][:1]
        for resp in bad_responses:
            with self.subTest(resp=resp):
                sb = _make_stub_soulbeet(lambda path, body, r=resp, **kw: r)
                with self.assertRaises(RuntimeError):
                    sb.queue(items, LIVE_SEARCH_ID)

    def test_queue_accepts_legacy_ok_dict(self):
        sb = _make_stub_soulbeet(lambda path, body, **kw: {"queued": 1})
        res = sb.queue(POLL_FLAC_GROUP["items"][:1], LIVE_SEARCH_ID)
        self.assertEqual(res, {"queued": 1})

    def test_process_candidate_passes_sid_and_records_error_on_queue_failure(self):
        state = mock.MagicMock()
        sb = mock.MagicMock()
        sb.search_album.return_value = LIVE_SEARCH_ALBUM_LIST
        sb.start_search.return_value = LIVE_SEARCH_ID
        sb.poll.return_value = {"state": "Completed", "groups": [POLL_FLAC_GROUP]}

        secrets = {"SOULBEET_USER": "u", "SOULBEET_PASS": "p"}
        row = ("devault|sensation|album", "Devault", "Sensation", "album")

        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False):
            # Success path passes search_id to sb.queue
            sb.queue.return_value = [{"id": "dl-1", "status": "Enqueued"}]
            st = librarian.process_candidate(state, sb, secrets, row)
            self.assertEqual(st, "queued_flac")
            sb.queue.assert_called_once()
            args, kwargs = sb.queue.call_args
            self.assertEqual(args[1], LIVE_SEARCH_ID)
            self.assertEqual(kwargs, {})

            # Failure path does NOT record queued_flac
            state.reset_mock()
            sb.queue.side_effect = RuntimeError("Soulbeet queue failed: dl-1: status='Failed'")
            st = librarian.process_candidate(state, sb, secrets, row)
            self.assertEqual(st, "error")
            state.set_status.assert_called_once_with(row[0], "error")

    def test_upgrade_one_passes_sid_and_records_failed_on_queue_failure(self):
        state = mock.MagicMock()
        sb = mock.MagicMock()
        sb.search_album.return_value = LIVE_SEARCH_ALBUM_LIST
        sb.start_search.return_value = LIVE_SEARCH_ID
        sb.poll.return_value = {"state": "Completed", "groups": [POLL_FLAC_GROUP]}
        cand = {
            "key": "devault|sensation",
            "artist": "Devault",
            "album": "Sensation",
            "dir": "/music/Devault/Sensation",
            "codec": "mp3",
            "kbps": 320,
            "tracks": 8,
            "attempts": 0,
        }

        # Success path passes search_id and UPGRADE_FOLDER
        sb.queue.return_value = [{"id": "dl-1", "status": "Enqueued"}]
        st = librarian.upgrade_one(state, sb, cand, {})
        self.assertEqual(st, "queued")
        args, kwargs = sb.queue.call_args
        self.assertEqual(args[1], LIVE_SEARCH_ID)
        self.assertEqual(kwargs.get("target_folder"), librarian.UPGRADE_FOLDER)

        # Queue failure records status='failed' instead of 'queued'
        state.reset_mock()
        sb.queue.side_effect = RuntimeError("Soulbeet queue failed: dl-1: status='Failed'")
        st = librarian.upgrade_one(state, sb, cand, {})
        self.assertEqual(st, "failed")
        last_call = state.upsert_upgrade.call_args_list[-1]
        self.assertEqual(last_call.kwargs.get("status"), "failed")


class TestDurationAndQualityLadder(unittest.TestCase):
    def test_dur_seconds_cases(self):
        cases = [
            (212, 212.0),
            (212.5, 212.5),
            ("212", 212.0),
            ("212.5", 212.5),
            ("03:32", 212.0),
            ("3:32", 212.0),
            ("1:02:03", 3723.0),
            ("01:02:03.5", 3723.5),
            (" 04:00 ", 240.0),
            (None, 0.0),
            ("", 0.0),
            ("00:00", 0.0),
            (0, 0.0),
            (-10, 0.0),
            (True, 0.0),
            (False, 0.0),
            ("not-a-duration", 0.0),
            ("1:2:3:4", 0.0),
            ("-01:30", 0.0),
            (float("nan"), 0.0),
            (float("inf"), 0.0),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertAlmostEqual(librarian._dur_seconds(raw), expected, places=4)

    def test_est_kbps_prefers_facts_bitrate_then_falls_back_to_size_duration(self):
        # Prefers facts.bitrate_kbps even when duration is string or absent
        item_with_facts = {
            "size": 1000,
            "duration": None,
            "facts": {"bitrate_kbps": 890},
        }
        self.assertAlmostEqual(librarian.est_kbps(item_with_facts), 890.0)

        # Falls back to size / MM:SS duration when facts.bitrate_kbps missing/null
        item_string_dur = {
            "size": 21_200_000,
            "duration": "03:32",  # 212s -> 800 kbps
            "facts": {"bitrate_kbps": None},
        }
        self.assertAlmostEqual(librarian.est_kbps(item_string_dur), 800.0, places=2)

        # Falls back to numeric duration when no facts dict at all
        item_legacy = {"size": 8_000_000, "duration": 200}  # 320 kbps
        self.assertAlmostEqual(librarian.est_kbps(item_legacy), 320.0, places=2)

        # Unparseable duration returns 0.0 without crashing
        item_bad = {"size": 8_000_000, "duration": "???"}
        self.assertEqual(librarian.est_kbps(item_bad), 0.0)

    def test_pick_ladder_flac_skips_below_floor_and_uses_facts_kbps(self):
        status, items, info = librarian.pick_by_quality_ladder(
            [POLL_MP3_320_GROUP, POLL_ALAC_GROUP, POLL_FLAC_GROUP]
        )
        self.assertEqual(status, "queued_flac")
        self.assertEqual([it["id"] for it in items], ["peer1:01.flac"])
        self.assertEqual(info, {"fmt": "flac", "kbps": 950})

    def test_pick_ladder_falls_through_when_all_flac_below_floor(self):
        all_bad_flac = {
            "quality": "flac",
            "score": 0.99,
            "item_count": 1,
            "items": [POLL_FLAC_GROUP["items"][1]],  # below_floor=True
        }
        status, items, info = librarian.pick_by_quality_ladder(
            [all_bad_flac, POLL_ALAC_GROUP, POLL_MP3_320_GROUP]
        )
        self.assertEqual(status, "queued_alac")
        self.assertEqual([it["id"] for it in items], ["peer2:01.m4a"])
        self.assertEqual(info, {"fmt": "alac", "kbps": 880})

    def test_pick_ladder_alac_vs_aac_m4a_with_string_duration(self):
        # ALAC inferred from size + string "MM:SS" duration without facts.bitrate_kbps
        alac_no_facts = {
            "quality": "m4a",
            "score": 0.80,
            "item_count": 1,
            "items": [
                {
                    "id": "alac-calc",
                    "title": "01 Sensation.m4a",
                    "quality": "m4a",
                    "quality_score": 0.70,
                    "below_floor": False,
                    "size": 21_200_000,
                    "duration": "03:32",  # 800 kbps >= ALAC_MIN_KBPS
                }
            ],
        }
        status, items, info = librarian.pick_by_quality_ladder([alac_no_facts, POLL_MP3_320_GROUP])
        self.assertEqual(status, "queued_alac")
        self.assertEqual(info, {"fmt": "alac", "kbps": 800})

        # AAC .m4a (256 kbps via facts) must NOT be treated as ALAC; falls into lossy
        status, items, info = librarian.pick_by_quality_ladder([POLL_AAC_M4A_GROUP])
        self.assertEqual(status, "queued_lossy")
        self.assertEqual(info, {"fmt": "m4a", "kbps": 256})

    def test_pick_ladder_lossy_prefers_320_and_handles_null_scores(self):
        status, items, info = librarian.pick_by_quality_ladder([POLL_MP3_320_GROUP])
        self.assertEqual(status, "queued_lossy")
        self.assertEqual(items[0]["id"], "peer4:01_320.mp3")
        self.assertEqual(info["fmt"], "mp3")

        null_score_group = {
            "quality": "mp3",
            "score": None,
            "item_count": 1,
            "items": [
                {
                    "id": "null-score",
                    "title": "track.mp3",
                    "quality": "mp3",
                    "quality_score": None,
                    "below_floor": False,
                    "size": 5_000_000,
                    "duration": "03:00",
                }
            ],
        }
        status, items, info = librarian.pick_by_quality_ladder([null_score_group])
        self.assertEqual(status, "low_quality_only")
        self.assertEqual(items, [])
        self.assertIsNone(info)


class TestFavoritesBypassCap(unittest.TestCase):
    def _cfg(self, **overrides):
        base = {
            "TARGET_TB": 7.0,
            "TARGET_TRACKS": 100_000,
            "MIN_FREE_GB": 500.0,
            "FAVORITES_ENABLED": True,
            "FAVORITES_BYPASS_CAP": True,
            "FAVORITE_PRIORITY": 100,
        }
        base.update(overrides)
        return base

    def test_stop_reason_kinds_and_free_space_precedence(self):
        tb = 1024 ** 4
        cfg = self._cfg()

        # Under all limits
        self.assertIsNone(librarian.stop_reason(cfg, int(5.0 * tb), 50_000, 10_000))

        # Size cap only
        r = librarian.stop_reason(cfg, int(7.41 * tb), 50_000, 10_000)
        self.assertEqual(r.kind, librarian.STOP_TARGET_TB)
        self.assertTrue(r.is_cap)

        # Track cap only
        r = librarian.stop_reason(cfg, int(5.0 * tb), 100_000, 10_000)
        self.assertEqual(r.kind, librarian.STOP_TARGET_TRACKS)
        self.assertTrue(r.is_cap)

        # Free space floor takes precedence even when ALSO over size and track caps
        r = librarian.stop_reason(cfg, int(7.41 * tb), 150_000, 400)
        self.assertEqual(r.kind, librarian.STOP_FREE_SPACE)
        self.assertFalse(r.is_cap)

    def test_should_bypass_cap_for_favorites_matrix(self):
        tb = 1024 ** 4
        cfg = self._cfg()
        r_tb = librarian.stop_reason(cfg, int(7.41 * tb), 50_000, 10_000)
        r_trk = librarian.stop_reason(cfg, int(5.0 * tb), 120_000, 10_000)
        r_free = librarian.stop_reason(cfg, int(7.41 * tb), 120_000, 200)

        # Soft caps bypass when enabled
        self.assertTrue(librarian.should_bypass_cap_for_favorites(r_tb, cfg))
        self.assertTrue(librarian.should_bypass_cap_for_favorites(r_trk, cfg))

        # Hard free-space floor NEVER bypasses
        self.assertFalse(librarian.should_bypass_cap_for_favorites(r_free, cfg))

        # No stop reason -> not in bypass mode
        self.assertFalse(librarian.should_bypass_cap_for_favorites(None, cfg))

        # Gated by FAVORITES_BYPASS_CAP and FAVORITES_ENABLED
        self.assertFalse(
            librarian.should_bypass_cap_for_favorites(r_tb, self._cfg(FAVORITES_BYPASS_CAP=False))
        )
        self.assertFalse(
            librarian.should_bypass_cap_for_favorites(r_tb, self._cfg(FAVORITES_ENABLED=False))
        )

    def test_favorite_min_priority_guards_zero(self):
        self.assertEqual(librarian.favorite_min_priority(self._cfg(FAVORITE_PRIORITY=100)), 100)
        self.assertEqual(librarian.favorite_min_priority(self._cfg(FAVORITE_PRIORITY=50)), 50)
        self.assertEqual(librarian.favorite_min_priority(self._cfg(FAVORITE_PRIORITY=0)), 1)

    def test_state_pending_candidates_min_priority_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            with mock.patch.object(librarian, "DATA_DIR", tmp_path), \
                 mock.patch.object(librarian, "STATE_DB", tmp_path / "state.db"):
                st = librarian.State()
                try:
                    st.add_candidate("Discovery Artist", "Normal Album", "album", priority=0)
                    st.add_candidate("RHCP", "By the Way", "album", priority=100)
                    st.add_candidate("RHCP", "Stadium Arcadium", "album", priority=100)

                    all_rows = st.pending_candidates(limit=10)
                    self.assertEqual(len(all_rows), 3)

                    fav_rows = st.pending_candidates(limit=10, min_priority=100)
                    self.assertEqual(len(fav_rows), 2)
                    self.assertTrue(all(r[1] == "RHCP" for r in fav_rows))
                finally:
                    st.conn.close()

    def test_config_and_web_metadata_include_favorites_bypass_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = pathlib.Path(tmp) / "config.env"
            with mock.patch.object(librarian, "CONFIG_ENV", cfg_path):
                # Default is True when absent
                self.assertTrue(librarian.load_config()["FAVORITES_BYPASS_CAP"])
                cfg_path.write_text("FAVORITES_BYPASS_CAP=0\n")
                self.assertFalse(librarian.load_config()["FAVORITES_BYPASS_CAP"])
                cfg_path.write_text("FAVORITES_BYPASS_CAP=1\n")
                self.assertTrue(librarian.load_config()["FAVORITES_BYPASS_CAP"])

        self.assertIn("FAVORITES_BYPASS_CAP", web.SETTINGS)
        self.assertEqual(web.SETTINGS["FAVORITES_BYPASS_CAP"], ("bool", 0, 1))
        self.assertIn("FAVORITES_BYPASS_CAP", web.HELP)


def _make_session_soulbeet(post_side_effect=None, post_return_value=None):
    """Build a Soulbeet instance backed by a mocked requests.Session (real _post)."""
    sb = object.__new__(librarian.Soulbeet)
    sb.user = "test"
    sb.pw = "test"
    sb.auth_lock = threading.Lock()
    sb.s = mock.MagicMock()
    if post_side_effect is not None:
        sb.s.post.side_effect = post_side_effect
    elif post_return_value is not None:
        sb.s.post.return_value = post_return_value
    sb.login = mock.Mock()
    return sb


def _mock_response(status_code, json_data=None, text=None):
    r = mock.Mock()
    r.status_code = status_code
    if json_data is not None:
        r.json.return_value = json_data
        r.text = json.dumps(json_data) if text is None else text
    else:
        r.json.side_effect = ValueError("not json")
        r.text = "" if text is None else text
    return r


class TestSoulbeetTransientErrors(unittest.TestCase):
    def test_post_409_backend_unreachable_raises_backend_unavailable(self):
        body = {
            "message": "error running server function: Request error: error sending request",
            "code": 409,
            "data": {
                "ServerError": {
                    "message": "Request error: error sending request",
                    "code": 409,
                    "details": {
                        "cause": {"raw": "connection refused", "service": "slskd", "status": None},
                        "code": "backend_unreachable",
                        "retry": "same_source_later",
                        "stage": "source_search",
                    },
                }
            },
        }
        sb = _make_session_soulbeet(post_return_value=_mock_response(409, json_data=body))
        with self.assertRaises(librarian.BackendUnavailable) as ctx:
            sb._post("/api/download/search/poll", {"input": {"search_id": "x"}})
        self.assertIn("409", str(ctx.exception))
        self.assertIn("backend_unreachable", str(ctx.exception))

    def test_post_retry_hint_alone_is_not_an_outage(self):
        # A per-peer failure can carry retry=same_source_later; it must become a
        # normal error, not an outage that keeps the candidate pending forever.
        for retry_val in ("same_source_later", "later"):
            with self.subTest(retry=retry_val):
                body = {"data": {"ServerError": {"details": {"retry": retry_val,
                                                             "code": "source_offline"}}}}
                sb = _make_session_soulbeet(post_return_value=_mock_response(409, json_data=body))
                with self.assertRaises(requests.HTTPError) as ctx:
                    sb._post("/api/download/search/start", {})
                self.assertNotIsInstance(ctx.exception, librarian.BackendUnavailable)

    def test_post_502_503_504_raise_backend_unavailable(self):
        for code in (502, 503, 504):
            with self.subTest(code=code):
                sb = _make_session_soulbeet(
                    post_return_value=_mock_response(code, text="Service Unavailable")
                )
                with self.assertRaises(librarian.BackendUnavailable):
                    sb._post("/api/metadata/search/album", {})

    def test_post_circuit_breaker_text_raises_backend_unavailable(self):
        cases = [
            _mock_response(
                503,
                text="API error: 503 - Circuit breaker is open - slskd appears to be unavailable",
            ),
            _mock_response(
                409,
                json_data={
                    "data": {
                        "ServerError": {
                            "message": "API error: 503 - Circuit breaker is open - slskd appears to be unavailable"
                        }
                    }
                },
            ),
            _mock_response(
                500,
                text="API error: 503 - Circuit breaker is open - slskd appears to be unavailable",
            ),
        ]
        for resp in cases:
            with self.subTest(code=resp.status_code):
                sb = _make_session_soulbeet(post_return_value=resp)
                with self.assertRaises(librarian.BackendUnavailable):
                    sb._post("/api/download/search/start", {})

    def test_post_connection_error_and_timeout_raise_backend_unavailable(self):
        for exc in (requests.ConnectionError("refused"), requests.Timeout("timed out")):
            with self.subTest(exc=type(exc).__name__):
                sb = _make_session_soulbeet(post_side_effect=exc)
                with self.assertRaises(librarian.BackendUnavailable):
                    sb._post("/api/metadata/search/album", {})

    def test_post_plain_500_is_not_transient_and_truncates_body(self):
        long_body = "fatal database syntax error: " + ("x" * 500)
        sb = _make_session_soulbeet(post_return_value=_mock_response(500, text=long_body))
        with self.assertRaises(requests.HTTPError) as ctx:
            sb._post("/api/metadata/search/album", {})
        self.assertNotIsInstance(ctx.exception, librarian.BackendUnavailable)
        msg = str(ctx.exception)
        self.assertIn("500", msg)
        self.assertIn("fatal database syntax error", msg)
        # Body portion is truncated to <= 300 chars
        body_part = msg.split(": ", 1)[1]
        self.assertLessEqual(len(body_part), 300)

    def test_post_malformed_details_shape_is_not_transient_and_does_not_crash(self):
        for payload in (
            {"data": None},
            {"data": "oops"},
            {"data": {"ServerError": None}},
            {"data": {"ServerError": {"details": "not-a-dict"}}},
            {"data": {"ServerError": {"details": {"code": "invalid_input", "retry": ["bad"]}}}},
            ["unexpected", "list"],
        ):
            with self.subTest(payload=payload):
                sb = _make_session_soulbeet(post_return_value=_mock_response(400, json_data=payload))
                with self.assertRaises(requests.HTTPError) as ctx:
                    sb._post("/api/metadata/search/album", {})
                self.assertNotIsInstance(ctx.exception, librarian.BackendUnavailable)

    def test_post_401_reauthenticates_and_retries(self):
        ok_resp = _mock_response(200, json_data=[{"kind": "Album"}])
        unauth_resp = _mock_response(401, text="expired")
        sb = _make_session_soulbeet(post_side_effect=[unauth_resp, ok_resp])
        res = sb._post("/api/metadata/search/album", {})
        self.assertEqual(res, [{"kind": "Album"}])
        sb.login.assert_called_once()
        self.assertEqual(sb.s.post.call_count, 2)


class TestCandidateTransientAndErrorRetry(unittest.TestCase):
    def _with_temp_state(self):
        tmp = tempfile.TemporaryDirectory()
        tmp_path = pathlib.Path(tmp.name)
        patches = [
            mock.patch.object(librarian, "DATA_DIR", tmp_path),
            mock.patch.object(librarian, "STATE_DB", tmp_path / "state.db"),
        ]
        for p in patches:
            p.start()
        st = librarian.State()

        def cleanup():
            st.conn.close()
            for p in reversed(patches):
                p.stop()
            tmp.cleanup()

        self.addCleanup(cleanup)
        return st

    def test_process_candidate_leaves_pending_on_backend_unavailable(self):
        st = self._with_temp_state()
        st.add_candidate("Devault", "Sensation", "album", priority=50)
        row = st.pending_candidates(limit=1)[0]

        sb = mock.MagicMock()
        sb.search_album.side_effect = librarian.BackendUnavailable("slskd down")
        secrets = {"SOULBEET_USER": "u", "SOULBEET_PASS": "p"}
        down_ev = threading.Event()

        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False):
            res = librarian.process_candidate(st, sb, secrets, row, backend_down=down_ev)

        self.assertEqual(res, "backend_down")
        self.assertTrue(down_ev.is_set())
        status, attempts, priority = st.conn.execute(
            "SELECT status, attempts, priority FROM candidates WHERE key=?", (row[0],)
        ).fetchone()
        self.assertEqual(status, "pending")
        self.assertEqual(attempts, 0)
        self.assertEqual(priority, 50)

        # Subsequent candidate with event already set short-circuits without calling Soulbeet
        sb.reset_mock()
        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False) as nav_m:
            res2 = librarian.process_candidate(st, sb, secrets, row, backend_down=down_ev)
        self.assertEqual(res2, "backend_down")
        sb.search_album.assert_not_called()
        nav_m.assert_not_called()

    def test_process_candidate_sets_error_and_increments_attempts_on_other_errors(self):
        st = self._with_temp_state()
        st.add_candidate("Devault", "Sensation", "album")
        row = st.pending_candidates(limit=1)[0]

        sb = mock.MagicMock()
        sb.search_album.side_effect = requests.HTTPError("Soulbeet HTTP 500: boom")
        secrets = {"SOULBEET_USER": "u", "SOULBEET_PASS": "p"}

        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False):
            res1 = librarian.process_candidate(st, sb, secrets, row)
            self.assertEqual(res1, "error")
            status, attempts = st.conn.execute(
                "SELECT status, attempts FROM candidates WHERE key=?", (row[0],)
            ).fetchone()
            self.assertEqual((status, attempts), ("error", 1))

            # Second error increments attempts again
            res2 = librarian.process_candidate(st, sb, secrets, row)
            self.assertEqual(res2, "error")
            status, attempts = st.conn.execute(
                "SELECT status, attempts FROM candidates WHERE key=?", (row[0],)
            ).fetchone()
            self.assertEqual((status, attempts), ("error", 2))

    def test_upgrade_one_leaves_row_retryable_on_backend_unavailable(self):
        st = self._with_temp_state()
        st.put_files([
            ("/music/Devault/Sensation/01.mp3", "/music/Devault/Sensation",
             "Devault", "Sensation", "mp3", 0, 320, 1000, 100, 100),
        ])
        cand = st.lossy_albums(limit=5, older_than=0)[0]

        sb = mock.MagicMock()
        sb.search_album.side_effect = librarian.BackendUnavailable("503 circuit open")

        res = librarian.upgrade_one(st, sb, cand, {})
        self.assertEqual(res, "backend_down")
        # Row was not marked failed and not given a recent last_checked
        row = st.conn.execute("SELECT status, attempts, last_checked FROM upgrades WHERE key=?",
                              (cand["key"],)).fetchone()
        self.assertIsNone(row)
        # Remains immediately eligible on the next pass
        still_due = st.lossy_albums(limit=5, older_than=0)
        self.assertEqual([c["key"] for c in still_due], [cand["key"]])

    def test_requeue_errors_respects_cutoff_max_attempts_priority_and_disabled(self):
        st = self._with_temp_state()
        now = 1_700_000_000
        old_ts = now - 10 * 86400
        recent_ts = now - 2 * 86400
        cutoff = now - 7 * 86400

        # Seed candidates with various statuses, ages, attempts, and priorities
        rows = [
            # Eligible: old error, historical attempts=0, favorite priority=100
            ("a1|alb1|album", "A1", "Alb1", "album", "error", old_ts, 100, 0),
            # Eligible: old error, historical attempts=NULL, normal priority=0
            ("a2|alb2|album", "A2", "Alb2", "album", "error", old_ts, 0, None),
            # Eligible: old error, attempts=2 (< 3), priority=50
            ("a3|alb3|album", "A3", "Alb3", "album", "error", old_ts, 50, 2),
            # Ineligible: old error, attempts=3 (>= 3)
            ("a4|alb4|album", "A4", "Alb4", "album", "error", old_ts, 100, 3),
            # Ineligible: recent error, attempts=1
            ("a5|alb5|album", "A5", "Alb5", "album", "error", recent_ts, 0, 1),
            # Ineligible: old non-error status
            ("a6|alb6|album", "A6", "Alb6", "album", "no_source", old_ts, 0, 0),
        ]
        st.conn.executemany(
            "INSERT INTO candidates(key, artist, album, kind, status, updated_at, priority, attempts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
        st.conn.commit()

        # ERROR_RETRY_DAYS=0 must not resurrect anything
        res_disabled = librarian.maybe_requeue_errors(
            st, {"ERROR_RETRY_DAYS": 0, "ERROR_MAX_ATTEMPTS": 3}, now=now
        )
        self.assertEqual(res_disabled, 0)
        self.assertEqual(st.status_counts().get("pending", 0), 0)

        # Direct requeue_errors flips only the 3 eligible rows and preserves priority
        count = st.requeue_errors(cutoff, max_attempts=3)
        self.assertEqual(count, 3)

        got = {
            r[0]: (r[1], r[2])
            for r in st.conn.execute("SELECT key, status, priority FROM candidates").fetchall()
        }
        self.assertEqual(got["a1|alb1|album"], ("pending", 100))
        self.assertEqual(got["a2|alb2|album"], ("pending", 0))
        self.assertEqual(got["a3|alb3|album"], ("pending", 50))
        self.assertEqual(got["a4|alb4|album"], ("error", 100))
        self.assertEqual(got["a5|alb5|album"], ("error", 0))
        self.assertEqual(got["a6|alb6|album"], ("no_source", 0))

    def test_maybe_requeue_errors_respects_6h_interval(self):
        st = self._with_temp_state()
        t0 = 1_700_000_000
        old_ts = t0 - 10 * 86400
        cfg = {"ERROR_RETRY_DAYS": 7, "ERROR_MAX_ATTEMPTS": 3}

        st.conn.execute(
            "INSERT INTO candidates(key, artist, album, kind, status, updated_at, priority, attempts) "
            "VALUES('k1','A','B','album','error',?,0,1)",
            (old_ts,),
        )
        st.conn.commit()

        self.assertEqual(librarian.maybe_requeue_errors(st, cfg, now=t0), 1)
        self.assertEqual(int(st.get_meta("last_error_requeue")), t0)

        # Add another old error: within 6h cooldown it must not run yet
        st.conn.execute(
            "INSERT INTO candidates(key, artist, album, kind, status, updated_at, priority, attempts) "
            "VALUES('k2','A','C','album','error',?,0,1)",
            (old_ts,),
        )
        st.conn.commit()
        self.assertEqual(librarian.maybe_requeue_errors(st, cfg, now=t0 + 3600), 0)

        # After 6 hours it runs again
        self.assertEqual(librarian.maybe_requeue_errors(st, cfg, now=t0 + 6 * 3600), 1)

    def test_migrate_adds_attempts_column_to_legacy_db(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE candidates ("
                "key TEXT PRIMARY KEY, artist TEXT, album TEXT, kind TEXT, "
                "status TEXT DEFAULT 'pending', updated_at INTEGER DEFAULT 0)"
            )
            librarian._migrate(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
            self.assertIn("attempts", cols)
            self.assertIn("priority", cols)
        finally:
            conn.close()

    def test_error_retry_config_and_web_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = pathlib.Path(tmp) / "config.env"
            with mock.patch.object(librarian, "CONFIG_ENV", cfg_path):
                defaults = librarian.load_config()
                self.assertEqual(defaults["ERROR_RETRY_DAYS"], 7)
                self.assertEqual(defaults["ERROR_MAX_ATTEMPTS"], 3)
                cfg_path.write_text("ERROR_RETRY_DAYS=0\nERROR_MAX_ATTEMPTS=5\n")
                loaded = librarian.load_config()
                self.assertEqual(loaded["ERROR_RETRY_DAYS"], 0)
                self.assertEqual(loaded["ERROR_MAX_ATTEMPTS"], 5)

        self.assertEqual(web.SETTINGS["ERROR_RETRY_DAYS"], ("int", 0, 365))
        self.assertEqual(web.SETTINGS["ERROR_MAX_ATTEMPTS"], ("int", 1, 20))
        self.assertIn("ERROR_RETRY_DAYS", web.HELP)
        self.assertIn("ERROR_MAX_ATTEMPTS", web.HELP)


class TestBackendBackoff(unittest.TestCase):
    def test_backoff_schedule_doubles_and_caps_at_15_min(self):
        self.assertEqual(librarian.backend_backoff_seconds(0), 0)
        self.assertEqual(librarian.backend_backoff_seconds(-2), 0)
        self.assertEqual(librarian.backend_backoff_seconds(1), 120)
        self.assertEqual(librarian.backend_backoff_seconds(2), 240)
        self.assertEqual(librarian.backend_backoff_seconds(3), 480)
        self.assertEqual(librarian.backend_backoff_seconds(4), 900)
        self.assertEqual(librarian.backend_backoff_seconds(5), 900)
        self.assertEqual(librarian.backend_backoff_seconds(100), 900)


if __name__ == "__main__":
    unittest.main()
