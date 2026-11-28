#!/usr/bin/env python3
"""Unit tests for played-artist new-release watch, recency priority, and tag-drift controls.

Stdlib unittest only (mocking MusicBrainz / Last.fm), matching existing test suites.
"""

import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import librarian  # noqa: E402
import web  # noqa: E402

# Fixed reference clock: 2026-11-27T12:00:00Z
NOW_TS = int(datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def _base_cfg(**overrides):
    cfg = {
        "EXPLORE_RATIO": 0.15,
        "EXPLORE_MAX_INACTIVE_YEARS": 15,
        "FAVORITES_ENABLED": True,
        "FAVORITES_BYPASS_CAP": True,
        "FAVORITE_SYNC_HOURS": 24,
        "FAVORITE_PRIORITY": 100,
        "FAVORITE_INCLUDE_EP": True,
        "FAVORITE_INCLUDE_SINGLES": False,
        "FAVORITE_INCLUDE_LIVE": False,
        "FAVORITE_INCLUDE_COMPILATIONS": False,
        "FAVORITE_INCLUDE_REMIX": False,
        "NEW_RELEASE_SYNC_HOURS": 24,
        "NEW_RELEASE_TOP_ARTISTS": 500,
        "NEW_RELEASE_MAX_AGE_DAYS": 730,
        "NEW_RELEASE_PRIORITY": 50,
    }
    cfg.update(overrides)
    return cfg


class TempStateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(tmp.name)
        self._patches = [
            mock.patch.object(librarian, "DATA_DIR", self.tmp),
            mock.patch.object(librarian, "STATE_DB", self.tmp / "state.db"),
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


class _FakeMB(librarian.MusicBrainz):
    """In-memory MusicBrainz double that records calls without hitting the network."""

    def __init__(self, artists=None, rgs=None):
        super().__init__("test-agent")
        self.artists = artists or {}   # artist_name -> (info_dict, note)
        self.rgs = rgs or {}           # mbid -> list of release-groups or None
        self.resolve_calls = []
        self.rg_calls = []

    def resolve_artist_info(self, artist):
        self.resolve_calls.append(artist)
        if artist in self.artists:
            return self.artists[artist]
        return None, "not found in MusicBrainz"

    def release_groups(self, mbid):
        self.rg_calls.append(mbid)
        return self.rgs.get(mbid, [])


class TestNewReleaseWatch(TempStateTestCase):
    def test_age_window_type_filter_and_priority(self):
        cfg = _base_cfg()
        mb = _FakeMB(
            artists={
                "Glass Animals": (
                    {"mbid": "mb-ga", "name": "Glass Animals", "ended": False, "end_year": None},
                    None,
                )
            },
            rgs={
                "mb-ga": [
                    # Within 1 year -> kept, priority 50 + 15 = 65
                    {"id": "rg-2026", "title": "Fresh LP", "primary-type": "Album",
                     "secondary-types": [], "first-release-date": "2026-06-01"},
                    # 1-2 years old (within 730d) -> kept, EP allowed, priority 50 + 10 = 60
                    {"id": "rg-2025-ep", "title": "Recent EP", "primary-type": "EP",
                     "secondary-types": [], "first-release-date": "2025-05"},
                    # Year-only current year -> kept
                    {"id": "rg-2026-yr", "title": "Year Only LP", "primary-type": "Album",
                     "secondary-types": [], "first-release-date": "2026"},
                    # Too old (> 730 days before 2026-11-27) -> skipped
                    {"id": "rg-old", "title": "Old Album", "primary-type": "Album",
                     "secondary-types": [], "first-release-date": "2024-07-19"},
                    # Far future unreleased announcement -> skipped until released
                    {"id": "rg-future", "title": "Unreleased 2027", "primary-type": "Album",
                     "secondary-types": [], "first-release-date": "2027-03-01"},
                    # Filtered out by type rules (reuses FAVORITE_INCLUDE_*)
                    {"id": "rg-single", "title": "Hot Single", "primary-type": "Single",
                     "secondary-types": [], "first-release-date": "2026-08-01"},
                    {"id": "rg-live", "title": "Live at Red Rocks", "primary-type": "Album",
                     "secondary-types": ["Live"], "first-release-date": "2026-08-01"},
                    {"id": "rg-comp", "title": "Greatest Hits", "primary-type": "Album",
                     "secondary-types": ["Compilation"], "first-release-date": "2026-08-01"},
                    {"id": "rg-remix", "title": "Remixes", "primary-type": "Album",
                     "secondary-types": ["Remix"], "first-release-date": "2026-08-01"},
                ]
            },
        )
        self.st.upsert_artist("Glass Animals", weight=180.0, play_weight=180.0)
        # Pre-existing pending Last.fm candidate from 2023 (outside 730d window, within 5y)
        self.st.add_candidate("Glass Animals", "Old Album", "album", priority=0)

        found, queued, ok = librarian.sync_one_new_release_artist(
            self.st, mb, cfg, "glass animals", "Glass Animals", now=NOW_TS
        )
        self.assertTrue(ok)
        self.assertEqual(found, 3)
        self.assertEqual(queued, 3)

        rows = {
            r[0]: (r[1], r[2])
            for r in self.st.conn.execute(
                "SELECT album, priority, release_year FROM candidates"
            ).fetchall()
        }
        self.assertEqual(rows["Fresh LP"], (65, 2026))
        self.assertEqual(rows["Recent EP"], (60, 2025))
        self.assertEqual(rows["Year Only LP"], (65, 2026))
        # Pre-existing 2024-07-19 pending candidate gets gentle 2-5y recency bonus (+5)
        self.assertEqual(rows["Old Album"], (5, 2024))
        # Filtered types & future album were NOT added
        for rejected in ("Unreleased 2027", "Hot Single", "Live at Red Rocks",
                         "Greatest Hits", "Remixes"):
            self.assertNotIn(rejected, rows)

        # Logged NEW RELEASE lines & populated new_releases table
        nr_albums = {r[1] for r in self.st.recent_new_releases(10)}
        self.assertEqual(nr_albums, {"Fresh LP", "Recent EP", "Year Only LP"})
        logged = "\n".join(str(c.args[0]) for c in librarian.log.call_args_list)
        self.assertIn("NEW RELEASE [Glass Animals - Fresh LP]", logged)

    def test_priority_capped_below_favorite_priority(self):
        cfg = _base_cfg(FAVORITE_PRIORITY=60, NEW_RELEASE_PRIORITY=50)
        mb = _FakeMB(
            artists={"Band": ({"mbid": "mb-1", "name": "Band", "ended": False, "end_year": None}, None)},
            rgs={"mb-1": [{"id": "rg-1", "title": "New LP", "primary-type": "Album",
                           "secondary-types": [], "first-release-date": "2026-10-01"}]},
        )
        self.st.upsert_artist("Band", weight=50.0, play_weight=50.0)
        librarian.sync_one_new_release_artist(self.st, mb, cfg, "band", "Band", now=NOW_TS)
        prio = self.st.conn.execute("SELECT priority FROM candidates").fetchone()[0]
        # 50 + 15 = 65, clamped to FAVORITE_PRIORITY - 1 = 59 so it never bypasses cap
        self.assertEqual(prio, 59)

    def test_mbid_cached_once_and_negative_cache_and_transient_retry(self):
        cfg = _base_cfg(NEW_RELEASE_SYNC_HOURS=24)
        mb = _FakeMB(
            artists={
                "Cached Artist": (
                    {"mbid": "mb-c", "name": "Cached Artist", "ended": True, "end_year": 2005},
                    None,
                ),
                "Missing Artist": (None, "not found in MusicBrainz"),
                "Flaky Artist": (None, "lookup failed"),
            },
            rgs={"mb-c": []},
        )
        for name, w in (("Cached Artist", 100), ("Missing Artist", 90), ("Flaky Artist", 80)):
            self.st.upsert_artist(name, weight=w, play_weight=w)

        lastfm = mock.MagicMock()
        lastfm.artist_tags.return_value = ["indie"]

        # Pass 1: resolves all 3
        s1 = librarian.new_releases_sync_step(self.st, mb, lastfm, cfg, set(), batch_size=10, now=NOW_TS)
        self.assertEqual(s1["checked"], 3)
        self.assertEqual(mb.resolve_calls, ["Cached Artist", "Missing Artist", "Flaky Artist"])
        self.assertEqual(mb.rg_calls, ["mb-c"])

        # Cached Artist stored MBID + ended life-span
        cached = self.st.get_artist_mb("Cached Artist")
        self.assertEqual(cached["mbid"], "mb-c")
        self.assertTrue(cached["ended"])
        self.assertEqual(cached["end_year"], 2005)

        # Within sync window: only Flaky Artist (failed transiently) is still due
        mb.resolve_calls.clear()
        mb.rg_calls.clear()
        s2 = librarian.new_releases_sync_step(self.st, mb, lastfm, cfg, set(), batch_size=10, now=NOW_TS + 3600)
        self.assertEqual(s2["checked"], 1)
        self.assertEqual(mb.resolve_calls, ["Flaky Artist"])

        # After 25h: Cached Artist re-checks release_groups WITHOUT re-resolving MBID;
        # Missing Artist hits negative cache (0 resolve calls); Flaky Artist retries resolve.
        mb.resolve_calls.clear()
        mb.rg_calls.clear()
        s3 = librarian.new_releases_sync_step(self.st, mb, lastfm, cfg, set(), batch_size=10,
                                              now=NOW_TS + 25 * 3600)
        self.assertEqual(s3["checked"], 3)
        self.assertEqual(mb.resolve_calls, ["Flaky Artist"])
        self.assertEqual(mb.rg_calls, ["mb-c"])

    def test_batching_excludes_kids_blocklist_and_favorites_and_prefers_play_weight(self):
        cfg = _base_cfg(NEW_RELEASE_TOP_ARTISTS=10)
        # Played artist (lower total weight than propagated, but play_weight > 0 -> ranked first)
        self.st.upsert_artist("Played One", weight=20.0, play_weight=20.0)
        self.st.upsert_artist("Played Two", weight=15.0, play_weight=15.0)
        self.st.upsert_artist("Propagated Only", weight=90.0, depth=1)
        self.st.upsert_artist("Kids Act", weight=50.0, play_weight=50.0)
        self.st.upsert_artist("Blocked Act", weight=60.0, play_weight=60.0)
        self.st.upsert_artist("Fav Act", weight=200.0, play_weight=200.0)
        self.st.upsert_favorite("fav act", "Fav Act", "manual")
        self.st.put_tags("Kids Act", ["childrens", "pop"])

        mb = _FakeMB(
            artists={
                "Played One": ({"mbid": "mb-1", "name": "Played One", "ended": False, "end_year": None}, None),
                "Played Two": ({"mbid": "mb-2", "name": "Played Two", "ended": False, "end_year": None}, None),
                "Propagated Only": ({"mbid": "mb-3", "name": "Propagated Only", "ended": False, "end_year": None}, None),
            },
            rgs={"mb-1": [], "mb-2": [], "mb-3": []},
        )
        lastfm = mock.MagicMock()
        lastfm.artist_tags.return_value = ["indie"]

        # Batch size 2: Blocked Act & Kids Act skipped, Fav Act excluded, checks Played One & Played Two
        s1 = librarian.new_releases_sync_step(
            self.st, mb, lastfm, cfg, exclude_set={"blocked act"}, batch_size=2, now=NOW_TS
        )
        self.assertEqual(s1["checked"], 2)
        self.assertEqual(s1["skipped"], 2)
        self.assertEqual(mb.resolve_calls, ["Played One", "Played Two"])
        self.assertEqual(s1["due"], 1)  # Propagated Only remains

        # Next pass checks remaining 1
        s2 = librarian.new_releases_sync_step(
            self.st, mb, lastfm, cfg, exclude_set={"blocked act"}, batch_size=2, now=NOW_TS + 60
        )
        self.assertEqual(s2["checked"], 1)
        self.assertEqual(s2["due"], 0)
        self.assertEqual(mb.resolve_calls, ["Played One", "Played Two", "Propagated Only"])

    def test_skips_already_owned_or_already_queued_and_no_double_promote(self):
        cfg = _base_cfg()
        self.st.upsert_artist("Artist", weight=50.0, play_weight=50.0)
        # Already in library_files
        self.st.put_files([
            ("/music/Artist/Owned LP/01.flac", "/music/Artist/Owned LP",
             "Artist", "Owned LP", "flac", 1, 900, 1000, 100, 100),
        ])
        # Already queued_flac in candidates
        self.st.add_candidate("Artist", "Done LP", "album", priority=0)
        done_key = f"{librarian.norm('Artist')}|{librarian.norm('Done LP')}|album"
        self.st.set_status(done_key, "queued_flac", fmt="flac", kbps=900)
        # Pending at priority 0 -> should promote to 65 once, not twice
        self.st.add_candidate("Artist", "Pending LP", "album", priority=0)

        mb = _FakeMB(
            artists={"Artist": ({"mbid": "mb-a", "name": "Artist", "ended": False, "end_year": None}, None)},
            rgs={"mb-a": [
                {"id": "rg-owned", "title": "Owned LP", "primary-type": "Album",
                 "secondary-types": [], "first-release-date": "2026-05-01"},
                {"id": "rg-done", "title": "Done LP", "primary-type": "Album",
                 "secondary-types": [], "first-release-date": "2026-05-02"},
                {"id": "rg-pend", "title": "Pending LP", "primary-type": "Album",
                 "secondary-types": [], "first-release-date": "2026-05-03"},
            ]},
        )
        f1, q1, ok1 = librarian.sync_one_new_release_artist(
            self.st, mb, cfg, "artist", "Artist", now=NOW_TS
        )
        self.assertTrue(ok1)
        self.assertEqual((f1, q1), (3, 1))
        self.assertEqual(self.st.candidate_priority(
            f"{librarian.norm('Artist')}|{librarian.norm('Pending LP')}|album"), 65)

        # Second sync: Pending LP is already priority 65 -> queued=0 (no double-count)
        f2, q2, ok2 = librarian.sync_one_new_release_artist(
            self.st, mb, cfg, "artist", "Artist", cached_mbid="mb-a", now=NOW_TS + 90000
        )
        self.assertTrue(ok2)
        self.assertEqual((f2, q2), (3, 0))

    def test_write_status_includes_new_release_watch(self):
        cfg = librarian.load_config()
        self.st.upsert_artist("A", weight=10.0, play_weight=10.0)
        out = self.tmp / "status.json"
        with mock.patch.object(librarian, "STATUS_JSON", out), \
             mock.patch.object(librarian, "free_gb", return_value=1000.0):
            librarian.write_status(self.st, cfg, 0, 0, 1000.0)
        s = json.loads(out.read_text())
        self.assertIn("new_release_watch", s)
        self.assertEqual(s["new_release_watch"]["watched_artists"], 1)
        self.assertEqual(s["new_release_watch"]["due"], 1)


class TestRecencyBonusAndOrdering(TempStateTestCase):
    def test_recency_priority_bonus_tiers(self):
        self.assertEqual(librarian.recency_priority_bonus("2026-09-01", now=NOW_TS), 15)
        self.assertEqual(librarian.recency_priority_bonus("2025-06-01", now=NOW_TS), 10)
        self.assertEqual(librarian.recency_priority_bonus("2023-01-01", now=NOW_TS), 5)
        self.assertEqual(librarian.recency_priority_bonus("2018-01-01", now=NOW_TS), 2)
        self.assertEqual(librarian.recency_priority_bonus("1999-01-01", now=NOW_TS), 0)
        self.assertEqual(librarian.recency_priority_bonus("", now=NOW_TS), 0)
        self.assertEqual(librarian.recency_priority_bonus(None, now=NOW_TS), 0)

    def test_pending_candidates_orders_by_priority_then_artist_weight_then_updated_at(self):
        self.st.upsert_artist("Low Weight", weight=5.0)
        self.st.upsert_artist("High Weight", weight=120.0)
        self.st.upsert_artist("Mid Weight", weight=40.0)

        # Added in reverse weight order at priority 0
        self.st.add_candidate("Low Weight", "Album L", "album", priority=0)
        self.st.add_candidate("Mid Weight", "Album M", "album", priority=0)
        self.st.add_candidate("High Weight", "Album H", "album", priority=0)
        # Higher priority still beats higher weight
        self.st.add_candidate("Low Weight", "New Release", "album", priority=60)

        rows = self.st.pending_candidates(limit=10)
        got = [(r[1], r[2]) for r in rows]
        self.assertEqual(got, [
            ("Low Weight", "New Release"),
            ("High Weight", "Album H"),
            ("Mid Weight", "Album M"),
            ("Low Weight", "Album L"),
        ])

    def test_favorite_sync_ranks_newer_albums_first(self):
        cfg = _base_cfg(FAVORITE_PRIORITY=100)
        self.st.upsert_favorite("sylvan esso", "Sylvan Esso", "manual")
        mb = _FakeMB(
            rgs={"mb-se": [
                {"id": "rg-1", "title": "Debut", "primary-type": "Album",
                 "secondary-types": [], "first-release-date": "2014-05-12"},
                {"id": "rg-2", "title": "Latest", "primary-type": "Album",
                 "secondary-types": [], "first-release-date": "2026-08-01"},
            ]}
        )
        with mock.patch("time.time", return_value=NOW_TS):
            librarian.sync_one_favorite(
                self.st, mb, cfg, "sylvan esso", "Sylvan Esso", "mb-se", None, NOW_TS
            )
        rows = self.st.pending_candidates(limit=10, min_priority=100)
        self.assertEqual([r[2] for r in rows], ["Latest", "Debut"])
        prios = {
            r[0]: r[1]
            for r in self.st.conn.execute("SELECT album, priority FROM candidates").fetchall()
        }
        self.assertEqual(prios["Latest"], 115)
        self.assertEqual(prios["Debut"], 100)

    def test_process_candidate_records_soulbeet_release_year(self):
        self.st.add_candidate("Devault", "Behind the Wheel", "album")
        row = self.st.pending_candidates(limit=1)[0]
        sb = mock.MagicMock()
        sb.search_album.return_value = [{
            "kind": "Album", "id": "x", "title": "Behind the Wheel",
            "artist": "Devault", "release_date": "2024-09-13",
        }]
        sb.start_search.return_value = "sid-1"
        sb.poll.return_value = {"state": "Completed", "groups": []}
        with mock.patch.object(librarian, "navidrome_album_exists", return_value=False):
            librarian.process_candidate(self.st, sb, {"SOULBEET_USER": "u", "SOULBEET_PASS": "p"}, row)
        year = self.st.conn.execute(
            "SELECT release_year FROM candidates WHERE key=?", (row[0],)
        ).fetchone()[0]
        self.assertEqual(year, 2024)

    def test_migrate_backfills_artist_weight_and_legacy_depth(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript("""
                CREATE TABLE artists_seen (
                    name TEXT PRIMARY KEY, display TEXT, weight REAL DEFAULT 0, expanded_at INTEGER DEFAULT 0
                );
                CREATE TABLE candidates (
                    key TEXT PRIMARY KEY, artist TEXT, album TEXT, kind TEXT,
                    status TEXT DEFAULT 'pending', updated_at INTEGER DEFAULT 0
                );
                INSERT INTO artists_seen VALUES ('glass animals', 'Glass Animals', 184.0, 1);
                INSERT INTO candidates VALUES ('glass animals|zaba|album', 'Glass Animals', 'ZABA', 'album', 'pending', 10);
            """)
            librarian._migrate(conn)
            aw = conn.execute("SELECT artist_weight FROM candidates").fetchone()[0]
            self.assertAlmostEqual(aw, 184.0)
            depth = conn.execute("SELECT depth FROM artists_seen").fetchone()[0]
            self.assertEqual(depth, 2)
        finally:
            conn.close()


class TestTagDriftControls(TempStateTestCase):
    def test_is_inactive_explore_artist_uses_cached_mb_and_tags_only(self):
        # Ended in 1970 via cached MB -> inactive (>15y before 2026)
        self.st.put_artist_mb("The Beatles", "mb-b", ended=True, end_year=1970)
        self.assertTrue(librarian.is_inactive_explore_artist(self.st, "The Beatles", 15, now=NOW_TS))

        # Ended in 2020 via cached MB -> still recent enough (6y <= 15y)
        self.st.put_artist_mb("Daft Punk", "mb-dp", ended=True, end_year=2020)
        self.assertFalse(librarian.is_inactive_explore_artist(self.st, "Daft Punk", 15, now=NOW_TS))

        # Cached Last.fm tags: only 60s -> inactive
        self.st.put_tags("Donovan", ["folk", "60s", "classic rock"])
        self.assertTrue(librarian.is_inactive_explore_artist(self.st, "Donovan", 15, now=NOW_TS))

        # Cached Last.fm tags: oldies without newer decade -> inactive
        self.st.put_tags("Oldies Act", ["oldies", "rock"])
        self.assertTrue(librarian.is_inactive_explore_artist(self.st, "Oldies Act", 15, now=NOW_TS))

        # Cached Last.fm tags: spans 80s and 2010s -> kept (latest decade 2019 >= 2011)
        self.st.put_tags("Veteran Band", ["rock", "80s", "2010s"])
        self.assertFalse(librarian.is_inactive_explore_artist(self.st, "Veteran Band", 15, now=NOW_TS))

        # Uncached artist -> kept (never makes network calls)
        self.assertFalse(librarian.is_inactive_explore_artist(self.st, "Brand New Band", 15, now=NOW_TS))

        # Disabled when max_inactive_years == 0
        self.assertFalse(librarian.is_inactive_explore_artist(self.st, "The Beatles", 0, now=NOW_TS))

    def test_expand_skips_inactive_explored_artists_and_caps_propagation_depth(self):
        cfg = _base_cfg(EXPLORE_RATIO=0.15, EXPLORE_MAX_INACTIVE_YEARS=15)
        self.st.put_tags("Modern Seed", ["rock", "indie"])
        self.st.put_tags("Classic 60s Act", ["classic rock", "60s"])
        self.st.put_artist_mb("Ended 1980 Act", "mb-old", ended=True, end_year=1980)

        lastfm = mock.MagicMock()
        lastfm.artist_tags.side_effect = lambda a: {
            "Modern Seed": ["rock", "indie"],
            "Depth1 Artist": ["indie"],
            "Depth2 Active": ["indie"],
            "Depth2 Classic": ["folk", "70s"],
        }.get(a, ["indie"])
        lastfm.top_albums.return_value = ["Album 1"]
        lastfm.similar.return_value = ["Sim Artist"]
        lastfm.tag_top_artists.return_value = [
            "Classic 60s Act", "Ended 1980 Act", "Modern Explored Act"
        ]

        with mock.patch("time.gmtime", return_value=datetime(2026, 11, 27, tzinfo=timezone.utc).timetuple()):
            # Depth 0: expands albums, similars (depth 1), and tag exploration (depth 2)
            librarian.expand_one_artist(self.st, lastfm, "Modern Seed", 100.0, cfg, set(), depth=0)

            seen = {
                r[0]: (r[1], r[2])
                for r in self.st.conn.execute("SELECT name, weight, depth FROM artists_seen").fetchall()
            }
            self.assertEqual(seen["sim artist"], (50.0, 1))
            self.assertEqual(seen["modern explored act"], (20.0, 2))
            self.assertNotIn("classic 60s act", seen)
            self.assertNotIn("ended 1980 act", seen)

            # Depth 1: propagates similars at depth 2, does NOT tag-explore
            lastfm.similar.reset_mock()
            lastfm.tag_top_artists.reset_mock()
            lastfm.similar.return_value = ["Two Hop Artist"]
            librarian.expand_one_artist(self.st, lastfm, "Depth1 Artist", 50.0, cfg, set(), depth=1)
            lastfm.similar.assert_called_once()
            lastfm.tag_top_artists.assert_not_called()
            d2 = self.st.conn.execute(
                "SELECT depth FROM artists_seen WHERE name='two hop artist'"
            ).fetchone()[0]
            self.assertEqual(d2, 2)

            # Depth 2 (active): adds top albums ONLY, no similar() or tag_top_artists()
            lastfm.similar.reset_mock()
            lastfm.tag_top_artists.reset_mock()
            n_added = librarian.expand_one_artist(
                self.st, lastfm, "Depth2 Active", 20.0, cfg, set(), depth=2
            )
            self.assertEqual(n_added, 1)
            lastfm.similar.assert_not_called()
            lastfm.tag_top_artists.assert_not_called()

            # Depth 2 (classic tags fetched during expand): skipped completely
            lastfm.top_albums.reset_mock()
            n_classic = librarian.expand_one_artist(
                self.st, lastfm, "Depth2 Classic", 20.0, cfg, set(), depth=2
            )
            self.assertEqual(n_classic, 0)
            lastfm.top_albums.assert_not_called()


class TestSettingsAndDocs(unittest.TestCase):
    def test_defaults_and_web_metadata_and_readme_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = pathlib.Path(tmp) / "config.env"
            with mock.patch.object(librarian, "CONFIG_ENV", cfg_path):
                d = librarian.load_config()
        self.assertAlmostEqual(d["EXPLORE_RATIO"], 0.15)
        self.assertEqual(d["EXPLORE_MAX_INACTIVE_YEARS"], 15)
        self.assertEqual(d["NEW_RELEASE_SYNC_HOURS"], 24)
        self.assertEqual(d["NEW_RELEASE_TOP_ARTISTS"], 500)
        self.assertEqual(d["NEW_RELEASE_MAX_AGE_DAYS"], 730)
        self.assertEqual(d["NEW_RELEASE_PRIORITY"], 50)

        for key in (
            "EXPLORE_RATIO", "EXPLORE_MAX_INACTIVE_YEARS",
            "NEW_RELEASE_SYNC_HOURS", "NEW_RELEASE_TOP_ARTISTS",
            "NEW_RELEASE_MAX_AGE_DAYS", "NEW_RELEASE_PRIORITY",
        ):
            self.assertIn(key, web.SETTINGS)
            self.assertIn(key, web.HELP)

        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "gen_settings_doc.py"), "--check"],
            capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, msg=res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
