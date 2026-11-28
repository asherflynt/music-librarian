#!/usr/bin/env python3
"""
Music Librarian — self-hosted, taste-driven, resumable FLAC-preferred auto-fill.

Runs entirely on the Unraid box (no Claude/browser dependency). Seeds from what the
household actually plays (Navidrome + Plex/Plexamp play counts, YouTube Music Takeout
history), expands via Last.fm (taste-weighted exploitation + horizon exploration), and
downloads through the existing Soulbeet API using a FLAC-preferred /
high-quality-lossy-fallback ladder. State lives in SQLite so restarts resume exactly
where they left off.

Two recommendation sources, deliberately split by what each is actually good at:
  * Last.fm     -- DISCOVERY ("who else might I like"): getSimilar / tag.getTopAlbums.
                   MusicBrainz has no similarity data at all, so this can't be replaced.
  * MusicBrainz -- DISCOGRAPHY ("what exactly did this artist release"), for favorited
                   artists. Last.fm's getTopAlbums has no release dates and no release
                   types, so it can neither detect a new release nor exclude a live
                   bootleg -- it is structurally unable to do this job.
"""

import collections
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import web

# ----------------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("LIBRARIAN_DATA", "/data"))
CONFIG_DIR = Path(os.environ.get("LIBRARIAN_CONFIG", "/config"))
SECRETS_ENV = CONFIG_DIR / "secrets.env"
CONFIG_ENV = CONFIG_DIR / "config.env"
EXCLUDE_TXT = CONFIG_DIR / "exclude.txt"
FAVORITES_TXT = CONFIG_DIR / "favorites.txt"
TAKEOUT_DIR = CONFIG_DIR / "takeout"
STATE_DB = DATA_DIR / "state.db"
STATUS_JSON = DATA_DIR / "status.json"

SOULBEET_URL = os.environ.get("SOULBEET_URL", "http://soulbeet:9765").rstrip("/")
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
TAUTULLI_URL = os.environ.get("TAUTULLI_URL", "").rstrip("/")       # Plex/Plexamp play history
MUSIC_PATH = Path(os.environ.get("MUSIC_PATH", "/music"))          # library, for size/count
DEFAULT_LIBRARY_SCAN_ROOT = os.environ.get("LIBRARY_SCAN_ROOT", "auto")
AUTO_SCAN_ROOT_CANDIDATE = Path(os.environ.get("LIBRARY_SCAN_AUTO_PATH", "/cluster/Media/Music"))
FREE_SPACE_PATH = os.environ.get("FREE_SPACE_PATH", "/music")       # cluster pool: library-size floor
STAGING_PATH = os.environ.get("STAGING_PATH", "/staging")           # NVMe staging: overflow floor
TARGET_FOLDER = os.environ.get("TARGET_FOLDER", "/music")           # beets destination in Soulbeet
# Where FLAC upgrades land before anything replaces a file you already have.
#
# This is a path *inside the Soulbeet container*: /api/downloads/queue does
# create_dir_all(target_folder) there and points beets at it. An arbitrary path like
# "/upgrades" would therefore be created in Soulbeet's own writable layer -- invisible
# to the host, invisible to this container, and discarded whenever Soulbeet is
# recreated. It fails silently, which is the worst way to fail.
#
# So staging lives under Soulbeet's existing rw /music mount instead. That needs no
# new mount and no recreation of the Soulbeet container (whose env holds secrets that
# must not be reproduced). The cost is that the staging dir sits inside the library
# tree, so it is explicitly skipped by the library scan below and carries a .ndignore
# so Navidrome doesn't index half-verified albums.
UPGRADE_FOLDER = os.environ.get("UPGRADE_FOLDER", "/music/_upgrades")
UPGRADE_DIRNAME = "_upgrades"
DEFAULT_DECODE_REJECTS_PATH = os.environ.get(
    "DECODE_REJECTS_PATH",
    "/cluster/Media/slskd_downloads/.decodecheck/rejects.jsonl",
)
WEB_PORT = int(os.environ.get("WEB_PORT", "8730"))

LOSSY_EXTS = {"mp3", "m4a", "aac"}
LOSSY_MIN_SCORE = float(os.environ.get("LOSSY_MIN_SCORE", "0.55"))  # ~clean 320 kbps MP3
# .m4a is ambiguous: ALAC (lossless) and AAC (lossy) share the extension, and the
# backend only reports the extension. Bitrate is the discriminator -- ALAC runs
# ~600-900 kbps, AAC caps ~320. Anything at/above this is treated as ALAC.
ALAC_MIN_KBPS = float(os.environ.get("ALAC_MIN_KBPS", "500"))
# Verified against real Last.fm data (2026-07): the tag Last.fm actually uses is the
# bare "childrens" -- NOT "childrens music"/"children". That gap let Bluey (tagged
# australian/cartoon/soundtrack/pop/childrens) through as a top-5 taste artist.
# Deliberately NOT included: "cartoon" (would also catch Gorillaz et al) and
# "novelty" (too broad). Artists with no kids tag at all -- Parry Gripp
# (comedy/novelty), Ms Rachel (no tags whatsoever) -- are uncatchable by tags;
# exclude.txt is the real defense. Last.fm tags are user-generated and troll-infested
# (Cocomelon is tagged "goregrind"), so only the leading tags are weighed.
KIDS_TAGS = {"childrens", "children's", "children", "childrens music",
             "children's music", "kids", "kids music", "kids tv", "kid's music",
             "nursery", "nursery rhyme", "nursery rhymes", "lullaby", "lullabies"}
KIDS_TAG_DEPTH = int(os.environ.get("KIDS_TAG_DEPTH", "6"))
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}

LOG_LOCK = threading.Lock()
# Backs the UI's /api/log. stdout is not readable from inside the process, and
# shelling out to `docker logs` would need the docker socket -- which this container
# deliberately does not get.
LOG_RING = collections.deque(maxlen=500)


def scrub_secrets(msg):
    """Strip credentials out of anything headed for the log. requests embeds the full
    URL in its exception messages, which carries the Last.fm api_key, the Subsonic
    auth token/salt, and the Tautulli apikey.

    Applied inside log() rather than at call sites: it was previously called at exactly
    one of them, so every Navidrome failure path was still logging u=/t=/s= in the
    clear. One choke point is the only version of this that stays true.
    """
    s = str(msg)
    s = re.sub(r"(api_key=)[^&\s]+", r"\1[REDACTED]", s)
    s = re.sub(r"(apikey=)[^&\s]+", r"\1[REDACTED]", s)
    s = re.sub(r"([?&](?:t|s|p|u)=)[^&\s]+", r"\1[REDACTED]", s)
    return s


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {scrub_secrets(msg)}"
    with LOG_LOCK:
        LOG_RING.append(line)
        print(line, flush=True)


# ----------------------------------------------------------------------------
# Config / secrets (config re-read every loop; secrets read once)
# ----------------------------------------------------------------------------
def _parse_env_file(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_secrets():
    """Wait patiently until secrets.env is filled, then return it. Lets the
    container be deployed before credentials exist without crash-looping."""
    announced = False
    while True:
        s = _parse_env_file(SECRETS_ENV)
        missing = [k for k in ("LASTFM_API_KEY", "SOULBEET_USER", "SOULBEET_PASS") if not s.get(k)]
        if not missing:
            return s
        if not announced:
            log(f"Waiting for {SECRETS_ENV} — missing {missing}. "
                f"Fill it in (no restart needed); retrying every 30s.")
            announced = True
        time.sleep(30)


def load_config_raw():
    """Raw string values, for settings that aren't numeric/boolean."""
    return _parse_env_file(CONFIG_ENV)


def load_config():
    c = _parse_env_file(CONFIG_ENV)

    def num(key, default, cast=float):
        try:
            return cast(c.get(key, default))
        except (TypeError, ValueError):
            return cast(default)

    def flag(key, default="0"):
        return c.get(key, default).strip().lower() in ("1", "true", "yes", "on")

    return {
        "TARGET_TB": num("TARGET_TB", 3.0, float),
        "TARGET_TRACKS": num("TARGET_TRACKS", 100000, int),
        "EXPLORE_RATIO": num("EXPLORE_RATIO", 0.15, float),
        "EXPLORE_MAX_INACTIVE_YEARS": max(0, num("EXPLORE_MAX_INACTIVE_YEARS", 15, int)),
        "MIN_FREE_GB": num("MIN_FREE_GB", 500, float),
        "STAGING_MIN_FREE_GB": num("STAGING_MIN_FREE_GB", 80, float),
        "CONCURRENCY": max(1, num("CONCURRENCY", 3, int)),
        "TASTE_REFRESH_MIN": num("TASTE_REFRESH_MIN", 360, int),   # re-pull play counts every 6h
        "MEASURE_EVERY_SEC": num("MEASURE_EVERY_SEC", 300, int),
        "LIBRARY_SCAN_ROOT": (c.get("LIBRARY_SCAN_ROOT") or DEFAULT_LIBRARY_SCAN_ROOT).strip() or "auto",
        "SCAN_MAX_PROBES_PER_PASS": max(1, num("SCAN_MAX_PROBES_PER_PASS", 250, int)),
        "ERROR_RETRY_DAYS": max(0, num("ERROR_RETRY_DAYS", 7, int)),
        "ERROR_MAX_ATTEMPTS": max(1, num("ERROR_MAX_ATTEMPTS", 3, int)),
        "PAUSED": flag("PAUSED"),
        # taste weighting
        "TASTE_HALF_LIFE_DAYS": max(1.0, num("TASTE_HALF_LIFE_DAYS", 365, float)),
        "WEIGHT_NAVIDROME": num("WEIGHT_NAVIDROME", 3.0, float),
        "WEIGHT_PLEX": num("WEIGHT_PLEX", 3.0, float),
        "WEIGHT_YTMUSIC": num("WEIGHT_YTMUSIC", 1.0, float),
        # favorites (MusicBrainz full-discography + new releases)
        "FAVORITES_ENABLED": flag("FAVORITES_ENABLED", "1"),
        "FAVORITES_BYPASS_CAP": flag("FAVORITES_BYPASS_CAP", "1"),
        "FAVORITE_SYNC_HOURS": max(1, num("FAVORITE_SYNC_HOURS", 24, int)),
        "FAVORITE_PRIORITY": num("FAVORITE_PRIORITY", 100, int),
        "FAVORITE_INCLUDE_EP": flag("FAVORITE_INCLUDE_EP", "1"),
        "FAVORITE_INCLUDE_SINGLES": flag("FAVORITE_INCLUDE_SINGLES", "0"),
        "FAVORITE_INCLUDE_LIVE": flag("FAVORITE_INCLUDE_LIVE", "0"),
        "FAVORITE_INCLUDE_COMPILATIONS": flag("FAVORITE_INCLUDE_COMPILATIONS", "0"),
        "FAVORITE_INCLUDE_REMIX": flag("FAVORITE_INCLUDE_REMIX", "0"),
        # new-release watch for top played artists (reuses FAVORITE_INCLUDE_* rules)
        "NEW_RELEASE_SYNC_HOURS": max(1, num("NEW_RELEASE_SYNC_HOURS", 24, int)),
        "NEW_RELEASE_TOP_ARTISTS": max(0, num("NEW_RELEASE_TOP_ARTISTS", 500, int)),
        "NEW_RELEASE_MAX_AGE_DAYS": max(1, num("NEW_RELEASE_MAX_AGE_DAYS", 730, int)),
        "NEW_RELEASE_PRIORITY": max(0, num("NEW_RELEASE_PRIORITY", 50, int)),
        # scheduled lossy -> FLAC upgrades
        "UPGRADE_ENABLED": flag("UPGRADE_ENABLED", "0"),
        "UPGRADE_HOUR": min(23, max(0, num("UPGRADE_HOUR", 3, int))),
        "UPGRADE_MAX_PER_RUN": max(1, num("UPGRADE_MAX_PER_RUN", 10, int)),
        "UPGRADE_RECHECK_DAYS": max(1, num("UPGRADE_RECHECK_DAYS", 30, int)),
        "UPGRADE_ONLY_WHEN_IDLE": flag("UPGRADE_ONLY_WHEN_IDLE", "1"),
        # decode gate (corrupt-file learning + bad-peer blocking)
        "DECODE_REJECTS_PATH": (c.get("DECODE_REJECTS_PATH") or DEFAULT_DECODE_REJECTS_PATH).strip(),
        "DECODE_MAX_RETRIES": max(0, num("DECODE_MAX_RETRIES", 3, int)),
        "BAD_PEER_STRIKES": max(1, num("BAD_PEER_STRIKES", 3, int)),
        "BAD_PEER_DAYS": max(1, num("BAD_PEER_DAYS", 90, int)),
    }


# ----------------------------------------------------------------------------
# State DB
# ----------------------------------------------------------------------------
def db_connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: candidates are processed in a ThreadPoolExecutor, so
    # worker threads touch this connection. Every access in State is serialized by
    # self.lock, which is what actually makes that safe. Without this flag sqlite3
    # raises "SQLite objects created in a thread can only be used in that same
    # thread" on every single candidate, silently failing the whole pipeline.
    conn = sqlite3.connect(STATE_DB, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS artists_seen (
            name TEXT PRIMARY KEY,      -- normalized artist name
            display TEXT,
            weight REAL DEFAULT 0,      -- taste weight (play count + propagated)
            play_weight REAL DEFAULT 0, -- direct play-derived weight from refresh_taste
            depth INTEGER DEFAULT 0,    -- 0=direct seed, 1=similar hop, 2+=propagated/explored
            expanded_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS artist_mb (
            name TEXT PRIMARY KEY,      -- normalized artist name
            mbid TEXT,                  -- resolved MusicBrainz UUID, or '' if not found
            mb_name TEXT,
            ended INTEGER DEFAULT 0,    -- 1 if MB life-span.ended is true
            end_year INTEGER,           -- parsed year from MB life-span.end
            resolved_at INTEGER DEFAULT 0,
            last_nr_sync INTEGER DEFAULT 0,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS candidates (
            key TEXT PRIMARY KEY,       -- normalized 'artist|album' or 'artist|track'
            artist TEXT, album TEXT, kind TEXT,
            status TEXT DEFAULT 'pending',   -- pending|queued_flac|queued_alac|queued_lossy|no_source|low_quality_only|no_meta|error|exists|corrupt_only
            fmt TEXT,                   -- actual format chosen: flac|alac|mp3|m4a|aac
            kbps INTEGER,               -- measured bitrate of what was queued
            release_year INTEGER,       -- known release year (from MB or Soulbeet)
            artist_weight REAL DEFAULT 0, -- cached taste weight for fast pending order
            updated_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS artist_tags (
            name TEXT PRIMARY KEY,      -- normalized artist name
            tags TEXT,                  -- json list of lowercase Last.fm tags
            checked_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

        -- Per-file library inventory. Powers the UI's real quality breakdown and is
        -- the input to the upgrade scanner. Incremental: a file is only re-read when
        -- (size, mtime) changes, so rescanning 100k files stays cheap.
        CREATE TABLE IF NOT EXISTS library_files (
            path TEXT PRIMARY KEY,
            dir TEXT, artist TEXT, album TEXT,
            codec TEXT,                 -- flac|alac|aac|mp3|opus|vorbis|wav|...
            lossless INTEGER DEFAULT 0, -- real codec, not extension: .m4a is BOTH alac and aac
            kbps INTEGER, size INTEGER, mtime INTEGER,
            scanned_at INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_library_files_dir ON library_files(dir);
        CREATE INDEX IF NOT EXISTS idx_library_files_lossless ON library_files(lossless);

        -- Lossy albums we may be able to re-acquire as FLAC later.
        CREATE TABLE IF NOT EXISTS upgrades (
            key TEXT PRIMARY KEY,       -- normalized 'artist|album'
            artist TEXT, album TEXT, dir TEXT,
            cur_codec TEXT, cur_kbps INTEGER, cur_tracks INTEGER,
            status TEXT DEFAULT 'pending',  -- pending|queued|verifying|replaced|no_flac|failed
            last_checked INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            note TEXT,
            updated_at INTEGER DEFAULT 0
        );

        -- Favorited artists: full discography + ongoing new releases, via MusicBrainz.
        CREATE TABLE IF NOT EXISTS favorites (
            name TEXT PRIMARY KEY,      -- normalized artist name
            display TEXT,
            mbid TEXT,                  -- resolved once, cached forever
            source TEXT,                -- navidrome|manual
            added_at INTEGER DEFAULT 0,
            last_sync INTEGER DEFAULT 0,
            known_rgs TEXT,             -- json list of release-group ids already seen
            albums_total INTEGER DEFAULT 0,
            albums_have INTEGER DEFAULT 0,
            note TEXT
        );

        -- New releases spotted for favorites, for the UI feed.
        CREATE TABLE IF NOT EXISTS new_releases (
            rg_id TEXT PRIMARY KEY,
            artist TEXT, album TEXT, released TEXT, found_at INTEGER
        );

        -- Files queued to Soulbeet, so decode-gate rejections can be attributed
        -- back to the Soulseek peer and candidate/upgrade that queued them.
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY,
            ts INTEGER DEFAULT 0,
            kind TEXT,                  -- 'candidate' | 'upgrade'
            ref_key TEXT,               -- candidate key or upgrade key
            username TEXT,              -- Soulseek peer username
            filename TEXT,              -- full remote filename
            basename TEXT,              -- last path component of filename
            remote_dir TEXT,            -- last directory component of filename
            search_id TEXT,
            norm_basename TEXT,         -- slskd_name_key(basename), what rejects match on
            norm_remote_dir TEXT        -- slskd_name_key(remote_dir)
        );
        CREATE INDEX IF NOT EXISTS idx_downloads_basename ON downloads(basename);
        CREATE INDEX IF NOT EXISTS idx_downloads_ts ON downloads(ts);

        -- Peer-attributed corrupt downloads learned from the decode gate.
        CREATE TABLE IF NOT EXISTS bad_sources (
            id INTEGER PRIMARY KEY,
            username TEXT,
            ref_key TEXT,
            reason TEXT,
            basename TEXT,
            ts INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_bad_sources_ref_key ON bad_sources(ref_key);
        CREATE INDEX IF NOT EXISTS idx_bad_sources_username_ts ON bad_sources(username, ts);

        -- Ingested rejects.jsonl events, deduplicated by (task_id, basename) so
        -- re-reads never double-count or burn extra retries. Also backs status stats.
        CREATE TABLE IF NOT EXISTS decode_rejects (
            task_id TEXT,
            basename TEXT,
            ts INTEGER DEFAULT 0,
            reason TEXT,
            matched INTEGER DEFAULT 0,
            username TEXT,
            ref_key TEXT,
            kind TEXT,
            requeued INTEGER DEFAULT 0, -- checker_unavailable: 0 waiting, 2 re-pended, 1 handled
            PRIMARY KEY (task_id, basename)
        );
        CREATE INDEX IF NOT EXISTS idx_decode_rejects_ts ON decode_rejects(ts);
        """
    )
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Bring an older state.db up to the current schema.

    CREATE TABLE IF NOT EXISTS does NOT alter a table that already exists, so a db
    written by an earlier build silently lacks newer columns and every write fails
    with "no such column". Add them idempotently instead of requiring users to
    delete their state (which would throw away taste weights and progress).
    """
    def cols(table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    cand_cols_before = cols("candidates")
    need_weight_backfill = bool(cand_cols_before) and "artist_weight" not in cand_cols_before

    for table, name, decl in (
        ("artists_seen", "play_weight", "REAL DEFAULT 0"),
        # Pre-existing rows default to depth=2 so already-propagated legacy artists
        # don't keep chaining; refresh_taste resets directly seeded artists to 0.
        ("artists_seen", "depth", "INTEGER DEFAULT 2"),
        ("candidates", "fmt", "TEXT"),
        ("candidates", "kbps", "INTEGER"),
        # Favorites must outrank the exploration crawl, or a full discography would
        # queue behind however many thousand candidates the 100k-track fill has
        # already generated.
        ("candidates", "priority", "INTEGER DEFAULT 0"),
        # Cap how many times an errored candidate is automatically retried.
        ("candidates", "attempts", "INTEGER DEFAULT 0"),
        # Cap how many times a decode-rejected candidate is re-queued from other peers.
        ("candidates", "decode_retries", "INTEGER DEFAULT 0"),
        ("candidates", "release_year", "INTEGER"),
        ("candidates", "artist_weight", "REAL DEFAULT 0"),
        # Decode-gate matching keys that survive slskd's filename rewrites.
        ("downloads", "norm_basename", "TEXT"),
        ("downloads", "norm_remote_dir", "TEXT"),
        ("decode_rejects", "requeued", "INTEGER DEFAULT 0"),
    ):
        have = cols(table)
        if not have:
            continue  # table not created here (bare legacy db in tests)
        if name not in have:
            log(f"Migrating state.db: adding {table}.{name}")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    if need_weight_backfill and cols("artists_seen"):
        log("Migrating state.db: backfilling candidates.artist_weight for pending rows")
        conn.execute(
            "UPDATE candidates SET artist_weight = COALESCE("
            "(SELECT weight FROM artists_seen "
            " WHERE name = substr(candidates.key, 1, instr(candidates.key, '|') - 1)), 0) "
            "WHERE status='pending'"
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_pending "
                 "ON candidates(status, priority DESC, updated_at)")
    if "artist_weight" in cols("candidates"):
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_pending_order "
                     "ON candidates(status, priority DESC, artist_weight DESC, updated_at ASC)")
    if "norm_basename" in cols("downloads"):
        for rid, bn, rd in conn.execute(
                "SELECT id, basename, remote_dir FROM downloads "
                "WHERE norm_basename IS NULL").fetchall():
            conn.execute("UPDATE downloads SET norm_basename=?, norm_remote_dir=? "
                         "WHERE id=?", (slskd_name_key(bn), slskd_name_key(rd), rid))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_norm_basename "
                     "ON downloads(norm_basename)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_norm_remote_dir "
                     "ON downloads(norm_remote_dir)")
    conn.commit()


# slskd writes a remote file under a local name it has rewritten: characters that
# are invalid on Windows/Linux become '_' ("Road to Hell: Intro" -> "Road to
# Hell_ Intro"), and a name that already exists gets "_<ticks>" before the
# extension. A reject carries the local name; downloads rows carry the remote one.
_SLSKD_BAD_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SLSKD_TICKS_RE = re.compile(r"_\d{15,19}(?=(\.[^.]*)?$)")


def slskd_name_key(name):
    """Comparison key for a file/dir name on either side of slskd's rename."""
    s = unicodedata.normalize("NFC", (name or "").strip())
    s = _SLSKD_BAD_CHARS_RE.sub("_", s)
    s = _SLSKD_TICKS_RE.sub("", s)
    return s.casefold()


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class State:
    def __init__(self):
        self.conn = db_connect()
        self.lock = threading.Lock()

    def upsert_artist(self, name, weight=0.0, depth=0, play_weight=None):
        n = norm(name)
        if not n:
            return
        pw = float(play_weight) if play_weight is not None else 0.0
        with self.lock:
            if play_weight is not None:
                self.conn.execute(
                    "INSERT INTO artists_seen(name, display, weight, play_weight, depth) "
                    "VALUES(?,?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "weight=MAX(weight, excluded.weight), "
                    "play_weight=excluded.play_weight, "
                    "depth=MIN(COALESCE(depth, excluded.depth), excluded.depth), "
                    "display=COALESCE(display, excluded.display)",
                    (n, name, weight, pw, int(depth)),
                )
            else:
                self.conn.execute(
                    "INSERT INTO artists_seen(name, display, weight, depth) VALUES(?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "weight=MAX(weight, excluded.weight), "
                    "depth=MIN(COALESCE(depth, excluded.depth), excluded.depth), "
                    "display=COALESCE(display, excluded.display)",
                    (n, name, weight, int(depth)),
                )
            self.conn.commit()

    def mark_expanded(self, name):
        with self.lock:
            self.conn.execute("UPDATE artists_seen SET expanded_at=? WHERE name=?",
                              (int(time.time()), norm(name)))
            self.conn.commit()

    def unexpanded_artists(self, limit=50):
        with self.lock:
            rows = self.conn.execute(
                "SELECT display, weight, COALESCE(depth, 0) FROM artists_seen WHERE expanded_at=0 "
                "ORDER BY weight DESC, name ASC LIMIT ?", (limit,)
            ).fetchall()
        return rows

    def add_candidate(self, artist, album, kind, priority=0, release_year=None,
                      artist_weight=None):
        """Returns True only if this is a brand-new candidate.

        Written as an explicit SELECT-then-write rather than INSERT..ON CONFLICT DO
        UPDATE because rowcount is 1 for an upsert's update path as well as its insert
        path -- callers count on the return value meaning "new", and the old
        INSERT OR IGNORE gave that for free.
        """
        anorm = norm(artist)
        key = f"{anorm}|{norm(album or '')}|{kind}"
        with self.lock:
            if artist_weight is None:
                aw_row = self.conn.execute(
                    "SELECT weight FROM artists_seen WHERE name=?", (anorm,)
                ).fetchone()
                aw = float(aw_row[0] or 0.0) if aw_row else 0.0
            else:
                aw = float(artist_weight or 0.0)
            row = self.conn.execute(
                "SELECT priority, release_year, COALESCE(artist_weight, 0) "
                "FROM candidates WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO candidates(key, artist, album, kind, priority, "
                    "release_year, artist_weight, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (key, artist, album, kind, priority, release_year, aw, int(time.time())))
                self.conn.commit()
                return True
            # Already known: an album queued by exploration that later turns out to
            # belong to a favorite or new-release watch gets promoted rather than
            # silently left at the back.
            cur_prio, cur_year, cur_aw = row[0] or 0, row[1], float(row[2] or 0.0)
            updates, params = [], []
            if priority > cur_prio:
                updates.append("priority=?")
                params.append(priority)
            if release_year is not None and cur_year is None:
                updates.append("release_year=?")
                params.append(int(release_year))
            if aw > cur_aw:
                updates.append("artist_weight=?")
                params.append(aw)
            if updates:
                params.append(key)
                self.conn.execute(f"UPDATE candidates SET {', '.join(updates)} WHERE key=?",
                                  params)
                self.conn.commit()
            return False

    def promote_pending_candidate(self, artist, album, kind, priority, release_year=None):
        """Promote an already-pending candidate's priority and backfill release_year.

        Used when MusicBrainz release-groups are already fetched in memory: enriches
        existing Last.fm candidates with recency priority without adding new albums.
        """
        key = f"{norm(artist)}|{norm(album or '')}|{kind}"
        with self.lock:
            row = self.conn.execute(
                "SELECT status, COALESCE(priority, 0), release_year FROM candidates WHERE key=?",
                (key,),
            ).fetchone()
            if not row or row[0] != "pending":
                return False
            cur_prio, cur_year = int(row[1] or 0), row[2]
            updates, params = [], []
            if priority > cur_prio:
                updates.append("priority=?")
                params.append(int(priority))
            if release_year is not None and cur_year is None:
                updates.append("release_year=?")
                params.append(int(release_year))
            if not updates:
                return False
            params.append(key)
            self.conn.execute(f"UPDATE candidates SET {', '.join(updates)} WHERE key=?", params)
            self.conn.commit()
            return True

    def set_candidate_release_year(self, key, release_year):
        if release_year is None:
            return
        with self.lock:
            self.conn.execute(
                "UPDATE candidates SET release_year=? WHERE key=? AND release_year IS NULL",
                (int(release_year), key),
            )
            self.conn.commit()

    def sync_pending_artist_weights(self):
        """Refresh cached artist_weight on pending candidates after refresh_taste."""
        with self.lock:
            self.conn.execute(
                "UPDATE candidates SET artist_weight = COALESCE("
                "(SELECT weight FROM artists_seen "
                " WHERE name = substr(candidates.key, 1, instr(candidates.key, '|') - 1)), 0) "
                "WHERE status='pending'"
            )
            self.conn.commit()

    def pending_candidates(self, limit, min_priority=None):
        with self.lock:
            if min_priority is not None:
                rows = self.conn.execute(
                    "SELECT key, artist, album, kind FROM candidates "
                    "WHERE status='pending' AND priority >= ? "
                    "ORDER BY priority DESC, COALESCE(artist_weight, 0) DESC, updated_at ASC "
                    "LIMIT ?",
                    (min_priority, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT key, artist, album, kind FROM candidates WHERE status='pending' "
                    "ORDER BY priority DESC, COALESCE(artist_weight, 0) DESC, updated_at ASC "
                    "LIMIT ?", (limit,)
                ).fetchall()
        return rows

    def top_artists(self, limit=100):
        with self.lock:
            return self.conn.execute(
                "SELECT display, weight, expanded_at FROM artists_seen "
                "ORDER BY weight DESC, name ASC LIMIT ?", (limit,)).fetchall()

    def set_status(self, key, status, fmt=None, kbps=None):
        with self.lock:
            if status == "error":
                self.conn.execute(
                    "UPDATE candidates SET status=?, fmt=?, kbps=?, updated_at=?, "
                    "attempts=COALESCE(attempts, 0) + 1 WHERE key=?",
                    (status, fmt, kbps, int(time.time()), key))
            else:
                self.conn.execute(
                    "UPDATE candidates SET status=?, fmt=?, kbps=?, updated_at=? WHERE key=?",
                    (status, fmt, kbps, int(time.time()), key))
            self.conn.commit()

    def requeue_errors(self, older_than_ts, max_attempts):
        """Flip old 'error' candidates back to 'pending' while under max_attempts.

        Historical error rows (attempts 0 or NULL) are eligible. Priority is
        left untouched.
        """
        if not max_attempts or max_attempts <= 0:
            return 0
        now = int(time.time())
        with self.lock:
            cur = self.conn.execute(
                "UPDATE candidates SET status='pending', updated_at=? "
                "WHERE status='error' AND COALESCE(updated_at, 0) < ? "
                "AND COALESCE(attempts, 0) < ?",
                (now, int(older_than_ts), int(max_attempts)),
            )
            self.conn.commit()
            return cur.rowcount

    def fallbacks(self, limit=25):
        """Recent non-FLAC acquisitions, so it's always visible what fell back."""
        with self.lock:
            return self.conn.execute(
                "SELECT artist, album, status, fmt, kbps FROM candidates "
                "WHERE status IN ('queued_alac','queued_lossy') "
                "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()

    def status_counts(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) FROM candidates GROUP BY status").fetchall()
        return dict(rows)

    def get_tags(self, name):
        """Cached Last.fm tags, or None if never successfully fetched."""
        with self.lock:
            r = self.conn.execute("SELECT tags FROM artist_tags WHERE name=?",
                                  (norm(name),)).fetchone()
        return json.loads(r[0]) if r and r[0] else None

    def put_tags(self, name, tags):
        with self.lock:
            self.conn.execute(
                "INSERT INTO artist_tags(name, tags, checked_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET tags=excluded.tags, "
                "checked_at=excluded.checked_at",
                (norm(name), json.dumps(tags), int(time.time())))
            self.conn.commit()

    def get_meta(self, k, default=None):
        with self.lock:
            r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        with self.lock:
            self.conn.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            self.conn.commit()

    # -- library inventory ----------------------------------------------------
    def library_totals(self):
        """(total_bytes, track_count) from persisted library_files rows."""
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(size), 0), COUNT(*) FROM library_files"
            ).fetchone()
        return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)

    def known_files(self):
        """path -> (size, mtime), so the scanner can skip unchanged files."""
        with self.lock:
            return {r[0]: (r[1], r[2]) for r in
                    self.conn.execute("SELECT path, size, mtime FROM library_files")}

    def put_files(self, rows):
        with self.lock:
            self.conn.executemany(
                "INSERT INTO library_files(path, dir, artist, album, codec, lossless, "
                "kbps, size, mtime, scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET dir=excluded.dir, artist=excluded.artist, "
                "album=excluded.album, codec=excluded.codec, lossless=excluded.lossless, "
                "kbps=excluded.kbps, size=excluded.size, mtime=excluded.mtime, "
                "scanned_at=excluded.scanned_at", rows)
            self.conn.commit()

    def drop_files(self, paths):
        with self.lock:
            self.conn.executemany("DELETE FROM library_files WHERE path=?",
                                  [(p,) for p in paths])
            self.conn.commit()

    def quality_breakdown(self):
        """Actual library composition by codec/bitrate tier -- what's on disk."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT codec, lossless, COUNT(*), SUM(size), "
                "       CAST(AVG(COALESCE(kbps,0)) AS INTEGER) "
                "FROM library_files GROUP BY codec, lossless").fetchall()
        out = []
        for codec, lossless, n, size, avg in rows:
            out.append({"codec": codec or "unknown", "lossless": bool(lossless),
                        "tracks": n, "bytes": size or 0, "avg_kbps": avg or 0})
        out.sort(key=lambda x: -x["tracks"])
        return out

    def lossy_albums(self, limit=None, older_than=0):
        """Lossy albums grouped by directory -- upgrade candidates.

        Directory is the grouping key rather than tags: beets lays out one album per
        directory, and it's the unit the swap script has to move/delete anyway.
        """
        q = ("SELECT lf.dir, lf.artist, lf.album, lf.codec, "
             "       CAST(AVG(lf.kbps) AS INTEGER), COUNT(*) "
             "FROM library_files lf WHERE lf.lossless=0 "
             "GROUP BY lf.dir ORDER BY lf.dir")
        with self.lock:
            rows = self.conn.execute(q).fetchall()
            checked = {r[0]: r[1] for r in
                       self.conn.execute("SELECT key, last_checked FROM upgrades")}
        out = []
        for d, artist, album, codec, kbps, n in rows:
            if not artist or not album:
                continue
            key = f"{norm(artist)}|{norm(album)}"
            if checked.get(key, 0) > older_than:
                continue
            out.append({"key": key, "dir": d, "artist": artist, "album": album,
                        "codec": codec, "kbps": kbps or 0, "tracks": n})
            if limit and len(out) >= limit:
                break
        return out

    def upsert_upgrade(self, key, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO upgrades(key) VALUES(?)", (key,))
            self.conn.execute(f"UPDATE upgrades SET {cols}, updated_at=? WHERE key=?",
                              (*kw.values(), int(time.time()), key))
            self.conn.commit()

    def upgrade_counts(self):
        with self.lock:
            return dict(self.conn.execute(
                "SELECT status, COUNT(*) FROM upgrades GROUP BY status").fetchall())

    # -- favorites ------------------------------------------------------------
    def upsert_favorite(self, name, display, source):
        with self.lock:
            self.conn.execute(
                "INSERT INTO favorites(name, display, source, added_at) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET display=COALESCE(excluded.display, display), "
                "source=excluded.source",
                (norm(name), display, source, int(time.time())))
            self.conn.commit()

    def drop_favorites_not_in(self, names):
        """Un-starring in Navidrome / deleting from favorites.txt removes the favorite."""
        keep = {norm(n) for n in names}
        with self.lock:
            have = {r[0] for r in self.conn.execute("SELECT name FROM favorites")}
            gone = have - keep
            for n in gone:
                self.conn.execute("DELETE FROM favorites WHERE name=?", (n,))
            self.conn.commit()
        return gone

    def favorites(self):
        with self.lock:
            return self.conn.execute(
                "SELECT name, display, mbid, source, added_at, last_sync, known_rgs, "
                "albums_total, albums_have, note FROM favorites ORDER BY display").fetchall()

    def update_favorite(self, name, **kw):
        cols = ", ".join(f"{k}=?" for k in kw)
        with self.lock:
            self.conn.execute(f"UPDATE favorites SET {cols} WHERE name=?",
                              (*kw.values(), norm(name)))
            self.conn.commit()

    def add_new_release(self, rg_id, artist, album, released):
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO new_releases(rg_id, artist, album, released, found_at) "
                "VALUES(?,?,?,?,?)", (rg_id, artist, album, released, int(time.time())))
            self.conn.commit()
            return cur.rowcount > 0

    def recent_new_releases(self, limit=25):
        with self.lock:
            return self.conn.execute(
                "SELECT artist, album, released, found_at FROM new_releases "
                "ORDER BY found_at DESC LIMIT ?", (limit,)).fetchall()

    # -- artist MusicBrainz cache + played-artist new-release watch -----------
    def get_artist_mb(self, name):
        n = norm(name)
        if not n:
            return None
        with self.lock:
            r = self.conn.execute(
                "SELECT name, mbid, mb_name, ended, end_year, resolved_at, last_nr_sync, note "
                "FROM artist_mb WHERE name=?", (n,)
            ).fetchone()
        if not r:
            return None
        return {
            "name": r[0], "mbid": r[1], "mb_name": r[2],
            "ended": bool(r[3]), "end_year": r[4],
            "resolved_at": r[5] or 0, "last_nr_sync": r[6] or 0, "note": r[7],
        }

    def put_artist_mb(self, name, mbid, mb_name=None, ended=False, end_year=None,
                      resolved_at=None, last_nr_sync=None, note=None):
        n = norm(name)
        if not n:
            return
        now = int(time.time())
        res_ts = now if resolved_at is None else int(resolved_at)
        sync_ts = 0 if last_nr_sync is None else int(last_nr_sync)
        with self.lock:
            self.conn.execute(
                "INSERT INTO artist_mb(name, mbid, mb_name, ended, end_year, "
                "resolved_at, last_nr_sync, note) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "mbid=excluded.mbid, "
                "mb_name=COALESCE(excluded.mb_name, mb_name), "
                "ended=excluded.ended, "
                "end_year=excluded.end_year, "
                "resolved_at=excluded.resolved_at, "
                "last_nr_sync=CASE WHEN excluded.last_nr_sync > 0 THEN excluded.last_nr_sync ELSE last_nr_sync END, "
                "note=excluded.note",
                (n, mbid if mbid is not None else "", mb_name, int(bool(ended)),
                 end_year, res_ts, sync_ts, note),
            )
            self.conn.commit()

    def touch_artist_nr_sync(self, name, ts=None):
        n = norm(name)
        if not n:
            return
        now = int(time.time() if ts is None else ts)
        with self.lock:
            self.conn.execute(
                "INSERT INTO artist_mb(name, mbid, resolved_at, last_nr_sync) "
                "VALUES(?, NULL, 0, ?) "
                "ON CONFLICT(name) DO UPDATE SET last_nr_sync=excluded.last_nr_sync",
                (n, now),
            )
            self.conn.commit()

    def top_watched_artists(self, limit=500, exclude_favorites=True):
        """Top taste-weighted artists for played-artist new-release watch.

        Prefers direct play-derived weight from refresh_taste (play_weight > 0),
        falling back to overall taste weight so it also works before the first
        refresh_taste or when fewer than `limit` artists have direct plays.
        """
        if limit <= 0:
            return []
        fav_clause = "AND f.name IS NULL " if exclude_favorites else ""
        q = (
            "SELECT a.name, COALESCE(a.display, a.name), a.weight, COALESCE(a.play_weight, 0), "
            "       m.mbid, m.mb_name, COALESCE(m.resolved_at, 0), COALESCE(m.last_nr_sync, 0), "
            "       f.mbid "
            "FROM artists_seen a "
            "LEFT JOIN artist_mb m ON m.name = a.name "
            "LEFT JOIN favorites f ON f.name = a.name "
            f"WHERE (COALESCE(a.play_weight, 0) > 0 OR COALESCE(a.weight, 0) > 0) {fav_clause}"
            "ORDER BY (CASE WHEN COALESCE(a.play_weight, 0) > 0 THEN 1 ELSE 0 END) DESC, "
            "         COALESCE(a.play_weight, 0) DESC, a.weight DESC, a.name ASC "
            "LIMIT ?"
        )
        with self.lock:
            return self.conn.execute(q, (int(limit),)).fetchall()

    def new_release_watch_stats(self, top_n, sync_hours, exclude_favorites=True, now=None):
        now = int(time.time() if now is None else now)
        cutoff = now - max(1, int(sync_hours)) * 3600
        rows = self.top_watched_artists(limit=max(0, int(top_n)),
                                        exclude_favorites=exclude_favorites)
        watched = len(rows)
        synced_recent = sum(1 for r in rows if (r[7] or 0) >= cutoff)
        with self.lock:
            mbids_cached = self.conn.execute(
                "SELECT COUNT(*) FROM artist_mb WHERE mbid IS NOT NULL AND mbid != ''"
            ).fetchone()[0]
            releases_total = self.conn.execute(
                "SELECT COUNT(*) FROM new_releases"
            ).fetchone()[0]
            releases_30d = self.conn.execute(
                "SELECT COUNT(*) FROM new_releases WHERE found_at >= ?",
                (now - 30 * 86400,),
            ).fetchone()[0]
        raw_summary = self.get_meta("last_new_release_summary")
        last_summary = None
        if raw_summary:
            try:
                last_summary = json.loads(raw_summary)
            except ValueError:
                last_summary = None
        return {
            "watched_artists": watched,
            "synced_recent": synced_recent,
            "due": max(0, watched - synced_recent),
            "mbids_cached": mbids_cached,
            "releases_total": releases_total,
            "releases_30d": releases_30d,
            "last_sync": int(self.get_meta("last_new_release_sync", "0") or 0),
            "last_summary": last_summary,
        }

    # -- decode gate: downloads, bad_sources, rejects -------------------------
    def record_downloads(self, kind, ref_key, items, search_id, ts=None):
        """One row per queued item, so a decode-gate reject can be traced back to
        the peer that served it and the candidate/upgrade that asked for it."""
        now = int(time.time() if ts is None else ts)
        rows = []
        for it in items or ():
            if not isinstance(it, dict):
                continue
            filename = item_filename(it)
            basename, remote_dir = split_remote_filename(filename)
            rows.append((now, kind, ref_key, item_peer(it), filename,
                         basename, remote_dir, search_id or "",
                         slskd_name_key(basename), slskd_name_key(remote_dir)))
        if not rows:
            return 0
        with self.lock:
            self.conn.executemany(
                "INSERT INTO downloads(ts, kind, ref_key, username, filename, "
                "basename, remote_dir, search_id, norm_basename, norm_remote_dir) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self.conn.commit()
        return len(rows)

    def prune_downloads(self, older_than_days=30, now=None):
        now = int(time.time() if now is None else now)
        cutoff = now - max(1, int(older_than_days)) * 86400
        with self.lock:
            cur = self.conn.execute("DELETE FROM downloads WHERE ts < ?", (cutoff,))
            self.conn.commit()
            return cur.rowcount

    @staticmethod
    def _download_row(r):
        return {
            "id": r[0],
            "ts": r[1],
            "kind": r[2],
            "ref_key": r[3],
            "username": r[4] or "",
            "filename": r[5] or "",
            "basename": r[6] or "",
            "remote_dir": r[7] or "",
            "search_id": r[8] or "",
        }

    def match_download(self, basename, staging_parent="", reject_ts=None, window_days=7,
                       tag_keys=()):
        """Attribute a rejected local file to the downloads row that queued it.

        Names are compared with slskd_name_key() on both sides, so slskd's ':'->'_'
        and "_<ticks>" rewrites still match. Among rows with that basename (inside
        `window_days`, with a small clock-skew allowance so a later retry from
        another peer is not blamed for an older reject):
          1. the most recent row whose remote_dir matches the staging parent;
          2. else the most recent row, but only if every row is for one ref_key;
          3. else the most recent row whose ref_key is in `tag_keys` (candidate
             keys built from the reject's artist/album tags).
        Otherwise it is ambiguous and None is returned rather than blaming a peer.

        If no basename matches, rows from the same remote dir can still identify
        the album when they are all for a single ref_key; then the result's
        username is "" (album retried, no peer blamed).
        """
        nb = slskd_name_key(basename)
        if not nb:
            return None
        np_ = slskd_name_key(staging_parent)
        ref_ts = int(time.time() if reject_ts is None else reject_ts)
        cutoff = ref_ts - max(1, int(window_days)) * 86400
        upper = ref_ts + 300
        cols = ("SELECT id, ts, kind, ref_key, username, filename, basename, "
                "       remote_dir, search_id, norm_remote_dir FROM downloads ")
        with self.lock:
            rows = self.conn.execute(
                cols + "WHERE norm_basename=? AND ts >= ? AND ts <= ? "
                "ORDER BY ts DESC, id DESC",
                (nb, cutoff, upper),
            ).fetchall()
            dir_rows = []
            if not rows and np_:
                dir_rows = self.conn.execute(
                    cols + "WHERE norm_remote_dir=? AND ts >= ? AND ts <= ? "
                    "ORDER BY ts DESC, id DESC",
                    (np_, cutoff, upper),
                ).fetchall()
        if rows:
            if np_:
                for r in rows:
                    if r[9] == np_:
                        return self._download_row(r)
            if len({r[3] for r in rows}) == 1:
                return self._download_row(rows[0])
            tagged = [r for r in rows if r[3] in set(tag_keys or ())]
            if tagged:
                return self._download_row(tagged[0])
            return None
        if dir_rows and len({r[3] for r in dir_rows}) == 1:
            m = self._download_row(dir_rows[0])
            m["username"] = ""
            return m
        return None

    def add_bad_source(self, username, ref_key, reason, basename, ts=None):
        if not username:
            return
        now = int(time.time() if ts is None else ts)
        with self.lock:
            self.conn.execute(
                "INSERT INTO bad_sources(username, ref_key, reason, basename, ts) "
                "VALUES(?,?,?,?,?)",
                (username, ref_key or "", reason or "unknown", basename or "", now),
            )
            self.conn.commit()

    def global_blocked_peers(self, strikes=3, days=90, now=None):
        """Peers with >= `strikes` bad downloads in the last `days`.

        A strike is one distinct ref_key (album/upgrade), not one file: a whole bad
        album writes one bad_sources row per broken track, and one bad rip should
        not ban a peer everywhere on its own.
        """
        now = int(time.time() if now is None else now)
        cutoff = now - max(1, int(days)) * 86400
        with self.lock:
            return frozenset(
                r[0] for r in self.conn.execute(
                    "SELECT username FROM bad_sources "
                    "WHERE ts >= ? AND username IS NOT NULL AND username != '' "
                    "GROUP BY username HAVING COUNT(DISTINCT ref_key) >= ?",
                    (cutoff, max(1, int(strikes))),
                )
            )

    def blocked_peers(self, ref_key, strikes=3, days=90, now=None):
        """Peers never to pick for `ref_key`: any peer that already served a bad
        file for it (no expiry), plus globally struck-out peers."""
        per_key = set()
        if ref_key:
            with self.lock:
                per_key = {
                    r[0] for r in self.conn.execute(
                        "SELECT DISTINCT username FROM bad_sources "
                        "WHERE ref_key=? AND username IS NOT NULL AND username != ''",
                        (ref_key,),
                    )
                }
        return frozenset(per_key) | self.global_blocked_peers(strikes, days, now)

    def has_decode_reject(self, task_id, basename):
        with self.lock:
            r = self.conn.execute(
                "SELECT 1 FROM decode_rejects WHERE task_id=? AND basename=? LIMIT 1",
                (task_id, basename),
            ).fetchone()
        return bool(r)

    def task_already_matched_key(self, task_id, ref_key):
        """True if an earlier bad file from the same import task already bumped `ref_key`."""
        if not task_id or not ref_key:
            return False
        with self.lock:
            r = self.conn.execute(
                "SELECT 1 FROM decode_rejects "
                "WHERE task_id=? AND ref_key=? AND matched=1 "
                "AND reason != 'checker_unavailable' LIMIT 1",
                (task_id, ref_key),
            ).fetchone()
        return bool(r)

    def record_decode_reject(self, task_id, basename, ts, reason, matched,
                             username="", ref_key="", kind=""):
        """Insert a reject event if not already seen. Returns True if newly inserted."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO decode_rejects("
                "task_id, basename, ts, reason, matched, username, ref_key, kind"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (task_id, basename, int(ts), reason, int(bool(matched)),
                 username or "", ref_key or "", kind or ""),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def candidate_info(self, key):
        with self.lock:
            return self.conn.execute(
                "SELECT artist, album, status, COALESCE(decode_retries, 0) "
                "FROM candidates WHERE key=?",
                (key,),
            ).fetchone()

    def candidate_priority(self, key):
        with self.lock:
            r = self.conn.execute(
                "SELECT COALESCE(priority, 0) FROM candidates WHERE key=?",
                (key,),
            ).fetchone()
        return int(r[0]) if r else None

    def upgrade_info(self, key):
        with self.lock:
            return self.conn.execute(
                "SELECT artist, album, status FROM upgrades WHERE key=?",
                (key,),
            ).fetchone()

    def handle_candidate_decode_reject(self, key, max_retries):
        """Re-queue candidate if under max_retries, else mark corrupt_only.

        Returns (new_status, decode_retries) or (None, 0) if key does not exist.
        """
        max_retries = max(0, int(max_retries))
        now = int(time.time())
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(decode_retries, 0) FROM candidates WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                return None, 0
            cur_retries = int(row[0] or 0)
            if cur_retries < max_retries:
                new_retries = cur_retries + 1
                # updated_at=0 puts it at the front of its priority class
                # (pending is ordered priority DESC, updated_at ASC), so the
                # retry happens in the next wave instead of behind the backlog.
                self.conn.execute(
                    "UPDATE candidates SET status='pending', decode_retries=?, "
                    "updated_at=0 WHERE key=?",
                    (new_retries, key),
                )
                self.conn.commit()
                return "pending", new_retries
            self.conn.execute(
                "UPDATE candidates SET status='corrupt_only', updated_at=? WHERE key=?",
                (now, key),
            )
            self.conn.commit()
            return "corrupt_only", cur_retries

    def checker_unavailable_pending(self):
        """checker_unavailable rejects whose candidate/upgrade was not re-pended yet."""
        with self.lock:
            return self.conn.execute(
                "SELECT task_id, basename, ts, ref_key, kind FROM decode_rejects "
                "WHERE reason='checker_unavailable' AND matched=1 "
                "AND COALESCE(requeued, 0)=0 AND ref_key != ''",
            ).fetchall()

    def repend_after_checker_outage(self, task_id, basename, ref_key, kind, max_repends=3):
        """Put a checker_unavailable item back in line without consuming a decode
        retry or blaming a peer. Returns True if a candidate went back to pending.

        Capped at `max_repends` per ref_key, so a file that reliably kills the
        checker (e.g. OOM) cannot cause an endless re-download loop."""
        changed = False
        with self.lock:
            prior = self.conn.execute(
                "SELECT COUNT(*) FROM decode_rejects WHERE ref_key=? AND requeued=2",
                (ref_key,)).fetchone()[0]
            if prior >= max(0, int(max_repends)):
                self.conn.execute(
                    "UPDATE decode_rejects SET requeued=1 WHERE task_id=? AND basename=?",
                    (task_id, basename))
                self.conn.commit()
                return False
            if kind == "candidate":
                cur = self.conn.execute(
                    "UPDATE candidates SET status='pending', updated_at=0 "
                    "WHERE key=? AND status LIKE 'queued_%'", (ref_key,))
                changed = cur.rowcount > 0
            elif kind == "upgrade":
                self.conn.execute(
                    "UPDATE upgrades SET status='failed', "
                    "note='decode checker unavailable; retry later', updated_at=? "
                    "WHERE key=? AND status='queued'", (int(time.time()), ref_key))
            # 2 = re-pended (counts toward the cap), 1 = handled without re-pending
            self.conn.execute(
                "UPDATE decode_rejects SET requeued=? WHERE task_id=? AND basename=?",
                (2 if changed else 1, task_id, basename))
            self.conn.commit()
        return changed

    def decode_reject_stats(self, now=None):
        now = int(time.time() if now is None else now)
        cut_24h = now - 86400
        cut_7d = now - 7 * 86400
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, reason, matched FROM decode_rejects WHERE ts >= ?",
                (cut_7d,),
            ).fetchall()
        by_reason_24h = {}
        by_reason_7d = {}
        matched_24h = unmatched_24h = 0
        matched_7d = unmatched_7d = 0
        checker_unavail_24h = 0
        for ts, reason, matched in rows:
            reason = reason or "unknown"
            by_reason_7d[reason] = by_reason_7d.get(reason, 0) + 1
            if matched:
                matched_7d += 1
            else:
                unmatched_7d += 1
            if ts >= cut_24h:
                by_reason_24h[reason] = by_reason_24h.get(reason, 0) + 1
                if matched:
                    matched_24h += 1
                else:
                    unmatched_24h += 1
                if reason == "checker_unavailable":
                    checker_unavail_24h += 1
        return {
            "rejects_24h": matched_24h + unmatched_24h,
            "rejects_7d": matched_7d + unmatched_7d,
            "by_reason_24h": by_reason_24h,
            "by_reason_7d": by_reason_7d,
            "matched_24h": matched_24h,
            "unmatched_24h": unmatched_24h,
            "matched_7d": matched_7d,
            "unmatched_7d": unmatched_7d,
            "checker_unavailable_24h": checker_unavail_24h,
        }


# ----------------------------------------------------------------------------
# Soulbeet client (auth + FLAC/lossy pipeline)
# ----------------------------------------------------------------------------
class BackendUnavailable(RuntimeError):
    """Raised when Soulbeet or its upstream backend (slskd) is transiently down."""


_TRANSIENT_HTTP_CODES = frozenset({502, 503, 504})

# /api/downloads/queue scales with item count: Soulbeet enqueues each file
# synchronously in slskd and starts monitoring before replying, which can exceed
# the default 60s timeout on multi-disc box sets (hundreds of files).
QUEUE_TIMEOUT_BASE_SEC = 120
QUEUE_TIMEOUT_PER_ITEM_SEC = 2
QUEUE_TIMEOUT_MIN_SEC = 60
QUEUE_TIMEOUT_MAX_SEC = 900  # 15 min cap


def queue_timeout_seconds(item_count):
    """HTTP timeout (seconds) for /api/downloads/queue, scaled by item count."""
    n = max(0, int(item_count or 0))
    return min(
        QUEUE_TIMEOUT_MAX_SEC,
        max(QUEUE_TIMEOUT_MIN_SEC, QUEUE_TIMEOUT_BASE_SEC + QUEUE_TIMEOUT_PER_ITEM_SEC * n),
    )


def _truncate_body(text, limit=300):
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _iter_error_details(payload):
    """Yield every dict stored under a 'details' key anywhere in a JSON payload."""
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            det = cur.get("details")
            if isinstance(det, dict):
                yield det
            for v in cur.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, (dict, list)):
                    stack.append(v)


def _transient_backend_reason(status_code, body_text, payload):
    """Return a short reason string if a 4xx/5xx response signals a transient backend
    outage, else None."""
    if payload is not None:
        for det in _iter_error_details(payload):
            # Only a whole-backend outage counts. A bare retry hint such as
            # "same_source_later" can describe one offline peer; treating that as
            # an outage would leave the candidate pending at the head of the queue
            # and back off forever.
            if det.get("code") == "backend_unreachable":
                return "backend_unreachable"
    if "circuit breaker is open" in (body_text or "").lower():
        return "circuit breaker open"
    if status_code in _TRANSIENT_HTTP_CODES:
        return f"HTTP {status_code}"
    return None


class Soulbeet:
    def __init__(self, secrets):
        self.user = secrets["SOULBEET_USER"]
        self.pw = secrets["SOULBEET_PASS"]
        self.s = requests.Session()
        self.auth_lock = threading.Lock()
        self.login()

    def login(self):
        with self.auth_lock:
            r = self.s.post(f"{SOULBEET_URL}/api/auth/login",
                            json={"username": self.user, "password": self.pw}, timeout=30)
            r.raise_for_status()
            log("Soulbeet: authenticated")

    def _post(self, path, body, timeout=60, _retry=True, allow_read_timeout=False):
        try:
            r = self.s.post(f"{SOULBEET_URL}{path}", json=body, timeout=timeout)
        except requests.ReadTimeout as e:
            if allow_read_timeout:
                raise
            raise BackendUnavailable(f"Soulbeet {path} unreachable: {e}") from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise BackendUnavailable(f"Soulbeet {path} unreachable: {e}") from e
        if r.status_code in (401, 403) and _retry:
            log("Soulbeet: session expired, re-authenticating")
            try:
                self.login()
            except (requests.ConnectionError, requests.Timeout) as e:
                raise BackendUnavailable(f"Soulbeet login unreachable: {e}") from e
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code in _TRANSIENT_HTTP_CODES:
                    raise BackendUnavailable(f"Soulbeet login HTTP {e.response.status_code}") from e
                raise
            return self._post(path, body, timeout, _retry=False,
                              allow_read_timeout=allow_read_timeout)
        if r.status_code >= 400:
            raw_text = getattr(r, "text", "")
            body_text = raw_text if isinstance(raw_text, str) else ""
            payload = None
            try:
                payload = r.json()
            except Exception:
                payload = None
            if not body_text and payload is not None:
                try:
                    body_text = json.dumps(payload)
                except Exception:
                    body_text = str(payload)
            snippet = _truncate_body(body_text, 300)
            reason = _transient_backend_reason(r.status_code, body_text, payload)
            if reason:
                tag = f" ({reason})" if reason != f"HTTP {r.status_code}" else ""
                msg = f"Soulbeet HTTP {r.status_code} on {path}{tag}" + (f": {snippet}" if snippet else "")
                raise BackendUnavailable(msg)
            msg = f"Soulbeet HTTP {r.status_code} on {path}" + (f": {snippet}" if snippet else "")
            raise requests.HTTPError(msg, response=r)
        return r.json()

    @staticmethod
    def _extract_results(d):
        """Accept both Soulbeet 2026-11+ bare JSON lists and legacy {'results': [...]}."""
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get("results") or []
        return []

    def search_album(self, artist, query):
        d = self._post("/api/metadata/search/album",
                       {"input": {"query": query, "artist": artist, "provider": None}})
        return [x for x in self._extract_results(d)
                if isinstance(x, dict) and x.get("kind") == "Album"]

    def search_track(self, artist, query):
        d = self._post("/api/metadata/search/track",
                       {"input": {"query": query, "artist": artist, "provider": None}})
        return [x for x in self._extract_results(d)
                if isinstance(x, dict) and x.get("kind") == "Track"]

    def start_search(self, album, tracks):
        return self._post("/api/download/search/start",
                          {"data": {"album": album, "tracks": tracks, "backend": None}})

    def poll(self, search_id, max_wait=150):
        start = time.time()
        while True:
            res = self._post("/api/download/search/poll",
                             {"input": {"search_id": search_id, "backend": None}})
            if res.get("state") != "InProgress":
                return res
            if time.time() - start > max_wait:
                return res
            time.sleep(3)

    def queue(self, items, search_id, target_folder=None):
        timeout = queue_timeout_seconds(len(items))
        try:
            res = self._post(
                "/api/downloads/queue",
                {"req": {"items": items,
                         "search_id": search_id,
                         "target_folder": target_folder or TARGET_FOLDER,
                         "backend": None}},
                timeout=timeout,
                allow_read_timeout=True,
            )
        except requests.ReadTimeout:
            return {"unconfirmed": True, "timeout": timeout}
        errs = queue_response_errors(res, expected_count=len(items))
        if errs:
            raise RuntimeError(f"Soulbeet queue failed: {'; '.join(errs)}")
        return res


_QUEUE_FAIL_TOKENS = (
    "fail", "error", "reject", "denied", "cancel", "abort", "timeout",
    "timed_out", "timedout", "invalid", "unavail", "refus",
)


def _status_is_failure(val):
    """True if a Soulbeet queue item status (string or serde-tagged dict) signals failure."""
    if isinstance(val, str):
        low = val.strip().lower()
        return any(tok in low for tok in _QUEUE_FAIL_TOKENS)
    if isinstance(val, dict):
        for k, v in val.items():
            if _status_is_failure(k) or _status_is_failure(v):
                return True
    return False


def queue_response_errors(resp, expected_count=None):
    """Return failure descriptions for a /api/downloads/queue response, or [] if OK.

    Soulbeet 2026-11+ returns a list like [{"id": "...", "status": "Enqueued"}].
    Legacy dict responses without error fields are treated as OK.
    """
    errors = []
    if resp is None:
        if expected_count:
            return ["empty response from /api/downloads/queue"]
        return []
    if isinstance(resp, dict):
        if resp.get("error") or resp.get("ServerError"):
            errors.append(str(resp.get("error") or resp.get("ServerError")))
        elif _status_is_failure(resp.get("status")):
            errors.append(f"status={resp.get('status')!r}")
        return errors
    if isinstance(resp, list):
        if expected_count and len(resp) == 0:
            return [f"expected {expected_count} queued item(s), got empty list"]
        for i, entry in enumerate(resp):
            if not isinstance(entry, dict):
                if _status_is_failure(entry):
                    errors.append(f"item[{i}]: {entry!r}")
                continue
            item_id = entry.get("id") or entry.get("title") or f"item[{i}]"
            err = entry.get("error") or entry.get("err")
            if err:
                errors.append(f"{item_id}: {err}")
                continue
            if entry.get("ok") is False or entry.get("success") is False:
                errors.append(f"{item_id}: {entry.get('message') or entry.get('status') or 'failed'}")
                continue
            st = entry.get("status")
            if _status_is_failure(st):
                msg = entry.get("message") or entry.get("reason")
                detail = f"status={st!r}" + (f" ({msg})" if msg else "")
                errors.append(f"{item_id}: {detail}")
        return errors
    return []


def _pos_float(v):
    if v is None or isinstance(v, bool):
        return 0.0
    try:
        n = float(v)
        return n if (n > 0 and n == n and n != float("inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _dur_seconds(v):
    """Parse duration in seconds from int/float, numeric string, 'MM:SS', 'H:MM:SS', or None."""
    if v is None or isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v) if (v > 0 and v == v and v != float("inf")) else 0.0
    s = str(v).strip()
    if not s:
        return 0.0
    if ":" in s:
        parts = s.split(":")
        if len(parts) not in (2, 3):
            return 0.0
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return 0.0
        if any(n < 0 or n != n or n == float("inf") for n in nums):
            return 0.0
        total = (nums[0] * 60.0 + nums[1]) if len(parts) == 2 else (
            nums[0] * 3600.0 + nums[1] * 60.0 + nums[2])
        return total if total > 0 else 0.0
    return _pos_float(s)


def est_kbps(item):
    """Effective bitrate in kbps. Prefers Soulbeet's reported facts.bitrate_kbps when
    present, falling back to size/duration. The backend only tells us the file
    EXTENSION, not the codec -- and .m4a is used by both ALAC (lossless) and AAC
    (lossy). Bitrate is what actually separates them: ALAC lands ~600-900 kbps,
    AAC caps around 320."""
    facts = item.get("facts")
    if isinstance(facts, dict):
        reported = _pos_float(facts.get("bitrate_kbps"))
        if reported > 0:
            return reported
    size = _pos_float(item.get("size"))
    dur = _dur_seconds(item.get("duration"))
    if size > 0 and dur > 0:
        return (size * 8) / dur / 1000
    return 0.0


def _items_kbps(items):
    vals = [k for k in (est_kbps(it) for it in items) if k > 0]
    return sum(vals) / len(vals) if vals else 0.0


def group_kbps(g):
    return _items_kbps(g.get("items", []))


def _item_backend_data(item):
    """Parse Soulbeet's JSON-encoded `backend_data` (slskd SearchResult) if present."""
    if not isinstance(item, dict):
        return {}
    raw = item.get("backend_data")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def item_peer(item, group=None):
    """Soulseek peer that serves a Soulbeet search-poll item.

    Soulbeet puts the slskd username in `source` (and again in the JSON-encoded
    `backend_data` as `username`); groups carry the same `source`.
    """
    if isinstance(item, dict):
        v = item.get("source") or _item_backend_data(item).get("username")
        if isinstance(v, str) and v.strip():
            return v.strip()
    if isinstance(group, dict):
        v = group.get("source")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def item_filename(item):
    """Full remote filename of a search-poll item: Soulbeet's `id` is the slskd
    filename (Windows-style backslashes); `backend_data.filename` is the fallback."""
    if isinstance(item, dict):
        v = item.get("id") or _item_backend_data(item).get("filename")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _split_path_parts(path):
    return [p for p in re.split(r"[\\/]+", (path or "").strip()) if p]


def split_remote_filename(filename):
    """Return (basename, remote_dir) of a remote path, splitting on both '\\' and '/'."""
    parts = _split_path_parts(filename)
    if not parts:
        return "", ""
    return parts[-1], (parts[-2] if len(parts) >= 2 else "")


def pick_by_quality_ladder(groups, blocked=frozenset()):
    """Return (status, items, info). Quality ladder, best first:

      1. FLAC                       -> lossless
      2. ALAC (.m4a at >= ALAC_MIN_KBPS) -> also lossless, so ranked with FLAC
      3. high-quality lossy (mp3/aac/.m4a-aac) at >= LOSSY_MIN_SCORE
      else: low_quality_only / no_source

    Items flagged `below_floor: true` by Soulbeet, or served by a peer in `blocked`,
    are skipped. 'info' carries the chosen format + measured bitrate so callers can
    log and record exactly what a fallback landed on.
    """
    if not groups:
        return "no_source", [], None

    blocked_set = frozenset(
        p.strip() for p in (blocked or ()) if isinstance(p, str) and p.strip()
    )
    if blocked_set:
        filtered = []
        for g in groups:
            if not isinstance(g, dict) or (g.get("source") or "").strip() in blocked_set:
                continue
            raw_items = g.get("items") or []
            kept = [it for it in raw_items
                    if isinstance(it, dict) and item_peer(it, g) not in blocked_set]
            if not kept:
                continue
            if len(kept) == len(raw_items):
                filtered.append(g)
            else:
                ng = dict(g)
                ng["items"] = kept
                ng["item_count"] = len(kept)
                filtered.append(ng)
        groups = filtered
        if not groups:
            return "no_source", [], None

    def eligible(items):
        return [it for it in (items or []) if not it.get("below_floor")]

    def qstr(obj):
        return (obj.get("quality") or "").lower()

    # --- tier 1: FLAC ---
    flac = [g for g in groups if qstr(g) == "flac"]
    if flac:
        flac.sort(key=lambda g: (g.get("score") or 0, g.get("item_count") or 0), reverse=True)
        for g in flac:
            items = [it for it in eligible(g.get("items", [])) if qstr(it) == "flac"]
            if items:
                return "queued_flac", items, {"fmt": "flac", "kbps": round(_items_kbps(items))}

    # --- tier 2: ALAC (.m4a at lossless bitrate) -- lossless, so preferred over any lossy ---
    alac = [g for g in groups if qstr(g) == "m4a"]
    if alac:
        alac.sort(key=lambda g: (g.get("score") or 0, g.get("item_count") or 0), reverse=True)
        for g in alac:
            elig = [it for it in eligible(g.get("items", [])) if qstr(it) == "m4a"]
            if _items_kbps(elig) < ALAC_MIN_KBPS:
                continue
            items = [it for it in elig if est_kbps(it) >= ALAC_MIN_KBPS]
            if items:
                return "queued_alac", items, {"fmt": "alac", "kbps": round(_items_kbps(items))}

    # --- tier 3: high-quality lossy ---
    lossy = [g for g in groups if qstr(g) in LOSSY_EXTS]
    if lossy:
        lossy.sort(key=lambda g: (g.get("score") or 0), reverse=True)
        for g in lossy:
            items = [it for it in eligible(g.get("items", []))
                     if qstr(it) in LOSSY_EXTS
                     and (it.get("quality_score") or 0) >= LOSSY_MIN_SCORE]
            # prefer explicit 320/V0 in filename ordering
            items.sort(key=lambda it: (
                1 if re.search(r"320|v0", (it.get("title", "") or "").lower()) else 0,
                it.get("quality_score") or 0), reverse=True)
            if items:
                return "queued_lossy", items, {"fmt": qstr(g),
                                               "kbps": round(_items_kbps(items))}
        return "low_quality_only", [], None

    return "low_quality_only", [], None


# ----------------------------------------------------------------------------
# Taste signal — recency decay
# ----------------------------------------------------------------------------
def decay(ts, half_life_days):
    """Exponential decay: a play `half_life_days` old counts half as much as one today.

    Keeps the library growing toward who you are now rather than who you were. An
    unknown/unparseable date gets 0.25 (treated as old but not worthless) rather than
    0, so a source with missing timestamps degrades instead of vanishing.
    """
    if not ts:
        return 0.25
    age_days = (time.time() - ts) / 86400.0
    if age_days < 0:            # clock skew / future-dated entry
        age_days = 0.0
    return 0.5 ** (age_days / half_life_days)


def _parse_ts(v):
    """Best-effort timestamp -> unix seconds. Handles unix ints and ISO-8601."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    try:
        # Takeout uses e.g. "2024-03-11T02:14:53.417Z"; Subsonic uses ISO-8601 too.
        from datetime import datetime, timezone
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, ImportError):
        return 0


# ----------------------------------------------------------------------------
# Navidrome (Subsonic) — internal play-count taste signal + dedup
# ----------------------------------------------------------------------------
def subsonic_params(user, pw):
    salt = hashlib.sha1(os.urandom(8)).hexdigest()[:8]
    token = hashlib.md5((pw + salt).encode()).hexdigest()
    return {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": "librarian", "f": "json"}


def nav_get(user, pw, endpoint, extra=None):
    params = subsonic_params(user, pw)
    if extra:
        params.update(extra)
    r = requests.get(f"{NAVIDROME_URL}/rest/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("subsonic-response", {})


def navidrome_play_weights(user, pw, half_life_days):
    """Artist -> recency-weighted play count, from most-frequently-played albums.

    Subsonic exposes no per-play timestamps -- only playCount and `played` (the
    album's LAST played date). So decay is necessarily applied at album granularity:
    100 plays spread over years and 100 plays last week are indistinguishable except
    by that last date. Coarser than Tautulli's true per-play history, but it's the
    best this API offers.
    """
    weights = {}
    try:
        resp = nav_get(user, pw, "getAlbumList2", {"type": "frequent", "size": 500})
        for al in resp.get("albumList2", {}).get("album", []):
            artist = al.get("artist")
            pc = al.get("playCount", 0) or 0
            if artist and pc > 0:
                weights[artist] = weights.get(artist, 0) + pc * decay(
                    _parse_ts(al.get("played")), half_life_days)
    except Exception as e:
        log(f"Navidrome play weights failed (non-fatal): {e}")
    if weights:
        log(f"Navidrome: {len(weights)} artists with plays")
    return weights


def navidrome_starred_artists(user, pw):
    """Artists you've starred in the player -- the natural place to mark a favorite."""
    names = {}
    try:
        resp = nav_get(user, pw, "getStarred2")
        for a in resp.get("starred2", {}).get("artist", []):
            if a.get("name"):
                names[a["name"]] = a.get("id")
    except Exception as e:
        log(f"Navidrome getStarred2 failed (non-fatal): {e}")
    return names


def navidrome_all_artists(user, pw):
    names = set()
    try:
        resp = nav_get(user, pw, "getArtists")
        for idx in resp.get("artists", {}).get("index", []):
            for a in idx.get("artist", []):
                if a.get("name"):
                    names.add(a["name"])
    except Exception as e:
        log(f"Navidrome getArtists failed (non-fatal): {e}")
    return names


def navidrome_album_exists(user, pw, artist, album):
    try:
        resp = nav_get(user, pw, "search3",
                       {"query": album, "albumCount": 10, "artistCount": 0, "songCount": 0})
        for al in resp.get("searchResult3", {}).get("album", []):
            if norm(al.get("artist")) == norm(artist) and norm(al.get("name")) == norm(album):
                return True
    except Exception:
        pass
    return False


# ----------------------------------------------------------------------------
# Tautulli — Plex / Plexamp play history
# ----------------------------------------------------------------------------
def tautulli_play_weights(api_key, half_life_days):
    """Artist -> recency-weighted play count from Plex, via Tautulli.

    Plexamp is a Plex client, so its plays are recorded by Plex Media Server and never
    reach Navidrome -- polling Navidrome alone would miss them entirely. Tautulli
    already logs Plex playback and, unlike Subsonic, gives one row per play with a
    unix timestamp, so this gets true per-play decay.

    No double-counting with Navidrome: a play is recorded by whichever server actually
    served the audio, never both.

    Entirely optional -- an unset key just means no Plex signal, never a failure.
    """
    weights = {}
    if not (TAUTULLI_URL and api_key):
        return weights
    try:
        r = requests.get(f"{TAUTULLI_URL}/api/v2", timeout=30, params={
            "apikey": api_key, "cmd": "get_history", "media_type": "track",
            "length": 5000, "order_column": "date", "order_dir": "desc"})
        r.raise_for_status()
        data = (r.json().get("response") or {}).get("data") or {}
        for row in data.get("data", []):
            # grandparent_title is the track's artist (parent_title is the album).
            artist = row.get("grandparent_title")
            if not artist:
                continue
            weights[artist] = weights.get(artist, 0) + decay(
                _parse_ts(row.get("date")), half_life_days)
    except Exception as e:
        log(f"Tautulli play weights failed (non-fatal): {e}")
        return {}
    if weights:
        log(f"Tautulli/Plex: {len(weights)} artists with plays")
    return weights


# ----------------------------------------------------------------------------
# YouTube Music (Google Takeout) — external cold-start taste signal
# ----------------------------------------------------------------------------
def youtube_music_weights(half_life_days):
    """Parse Takeout watch-history JSON files; tally recency-weighted listens by artist.

    Verified against real Takeout data (2026-07). Notes for future editors:
      - `header` ("YouTube Music" vs "YouTube") + the music.youtube.com URL are the
        ONLY reliable music signals. Do NOT filter on `products`: real exports set
        it to ["YouTube"] on every entry, music or not, so it matches nothing.
      - YT Music artist channels are named "<Artist> - Topic"; strip that suffix.
      - Music watched on regular youtube.com is indistinguishable from any other
        video and is intentionally not counted.
      - `time` is ISO-8601 and each entry is one listen, so this gets true per-listen
        decay -- important, since this history spans years and is currently the only
        taste signal with any real volume behind it.
    """
    weights = {}
    listens = 0
    if not TAKEOUT_DIR.exists():
        return weights
    for path in TAKEOUT_DIR.rglob("*.json"):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            # Only YouTube Music listens (header or music.youtube.com URL)
            header = (entry.get("header") or "")
            url = (entry.get("titleUrl") or "")
            if "YouTube Music" not in header and "music.youtube.com" not in url:
                continue
            subs = entry.get("subtitles") or []
            if subs and subs[0].get("name"):
                artist = subs[0]["name"]
                # strip trailing " - Topic" that YT uses for auto-generated artist channels
                artist = re.sub(r"\s*-\s*Topic$", "", artist).strip()
                if artist:
                    listens += 1
                    weights[artist] = weights.get(artist, 0) + decay(
                        _parse_ts(entry.get("time")), half_life_days)
    if weights:
        log(f"YouTube Music: parsed {listens} listens across {len(weights)} artists "
            f"(recency-weighted, {half_life_days:.0f}d half-life)")
    return weights


# ----------------------------------------------------------------------------
# MusicBrainz — canonical discography for favorited artists
# ----------------------------------------------------------------------------
class MusicBrainz:
    """Discography source for favorites. Deliberately NOT a Last.fm replacement.

    Last.fm answers "who else might I like" (getSimilar), which MusicBrainz cannot do
    at all. MusicBrainz answers "what exactly did this artist release" -- with release
    dates and release types -- which Last.fm cannot do at all: getTopAlbums returns
    neither, so it can't spot a new release or filter out a live bootleg.

    Verified against Glass Animals (2026-07): 75 release-groups, of which exactly 7 are
    real Albums/EPs. The other 68 are remix singles, live broadcasts, and compilations.
    """
    BASE = "https://musicbrainz.org/ws/2"
    MIN_INTERVAL = 1.1          # MusicBrainz enforces 1 req/sec, strictly.

    def __init__(self, user_agent):
        self.ua = user_agent
        self.lock = threading.Lock()
        self.last = 0

    def _get(self, path, expect, **params):
        """GET with rate limiting; `expect` is the key that must be in the response.

        Throttling here does NOT come back as an HTTP error -- it comes back 200 with
        an empty/garbage body. Treating that as "this artist has no releases" would
        permanently mark a discography complete and silently stop syncing it, so a
        missing `expect` key is a retryable failure, not an empty result.
        """
        params["fmt"] = "json"
        for attempt in range(3):
            with self.lock:
                dt = time.time() - self.last
                if dt < self.MIN_INTERVAL:
                    time.sleep(self.MIN_INTERVAL - dt)
                self.last = time.time()
            try:
                r = requests.get(f"{self.BASE}/{path}", params=params, timeout=30,
                                 headers={"User-Agent": self.ua})
                if r.status_code == 503:        # explicit rate limit
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                data = r.json()
                if expect in data:
                    return data
                log(f"MusicBrainz {path}: response missing '{expect}' "
                    f"(likely throttled); retry {attempt + 1}/3")
            except Exception as e:
                log(f"MusicBrainz {path} failed: {e}; retry {attempt + 1}/3")
            time.sleep(2 * (attempt + 1))
        return None

    def resolve_artist_info(self, artist):
        """Artist name -> (info_dict, None) or (None, note).

        Extracts MusicBrainz MBID alongside life-span metadata already present in
        the same /ws/2/artist response (zero extra API calls).
        """
        q = artist.replace('"', "")
        data = self._get("artist", "artists", query=f'artist:"{q}"', limit=5)
        if not data:
            return None, "lookup failed"
        for a in data.get("artists", []):
            if int(a.get("score", 0)) >= 90:
                ls = a.get("life-span") or {}
                return {
                    "mbid": a["id"],
                    "name": a.get("name"),
                    "ended": bool(ls.get("ended")),
                    "end_year": parse_release_year(ls.get("end")),
                }, None
        if data.get("artists"):
            best = data["artists"][0]
            return None, (f"no confident match (best: {best.get('name')} "
                          f"@ score {best.get('score')})")
        return None, "not found in MusicBrainz"

    def resolve_mbid(self, artist):
        """Artist name -> MBID. Returns None rather than guessing on a weak match."""
        info, note = self.resolve_artist_info(artist)
        if info:
            return info["mbid"], info.get("name")
        return None, note

    def release_groups(self, mbid):
        """Every release-group for an artist, paginated."""
        out, offset = [], 0
        while True:
            data = self._get("release-group", "release-groups",
                             artist=mbid, limit=100, offset=offset)
            if not data:
                return None                     # failure != "no releases"
            batch = data.get("release-groups", [])
            out.extend(batch)
            total = data.get("release-group-count", len(out))
            offset += len(batch)
            if not batch or offset >= total:
                return out


_YEAR_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")


def parse_release_year(val):
    """Extract a 4-digit release year (1800-2100) from a date string or int, else None."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        y = int(val)
        return y if 1800 <= y <= 2100 else None
    m = _YEAR_RE.match(str(val).strip())
    if not m:
        return None
    y = int(m.group(1))
    return y if 1800 <= y <= 2100 else None


def parse_mb_date_ts(val):
    """Parse MusicBrainz / Soulbeet partial dates ('YYYY', 'YYYY-MM', 'YYYY-MM-DD')
    to UTC unix seconds, or None if missing/unparseable.

    Unlike _parse_ts(), bare 4-digit years ('2025') are parsed as 2025-01-01 rather
    than 2025 seconds past the Unix epoch.
    """
    if val is None or isinstance(val, bool):
        return None
    s = str(val).strip()
    if not s:
        return None
    m = _YEAR_RE.match(s)
    if not m:
        return None
    y = int(m.group(1))
    if not (1800 <= y <= 2100):
        return None
    mo = int(m.group(2)) if m.group(2) else 1
    d = int(m.group(3)) if m.group(3) else 1
    try:
        from datetime import datetime, timezone
        return int(datetime(y, mo, d, tzinfo=timezone.utc).timestamp())
    except ValueError:
        try:
            from datetime import datetime, timezone
            return int(datetime(y, min(12, max(1, mo)), 1, tzinfo=timezone.utc).timestamp())
        except ValueError:
            return None


def recency_priority_bonus(release_date, now=None):
    """Small priority bonus for releases whose date is known at enqueue time.

    Newer releases rank ahead of older back-catalogue items within the same tier:
      <= 1 year : +15
      <= 2 years: +10
      <= 5 years:  +5
      <= 10 years: +2
      older / unknown: 0
    """
    rel_ts = parse_mb_date_ts(release_date)
    if rel_ts is None:
        return 0
    now_ts = int(time.time() if now is None else now)
    age_days = (now_ts - rel_ts) / 86400.0
    if age_days < 0:
        age_days = 0.0
    if age_days <= 365:
        return 15
    if age_days <= 730:
        return 10
    if age_days <= 5 * 365:
        return 5
    if age_days <= 10 * 365:
        return 2
    return 0


def keep_release_group(rg, cfg):
    """Apply the configured release-type policy. Verified against Glass Animals."""
    primary = (rg.get("primary-type") or "").lower()
    secondary = {s.lower() for s in (rg.get("secondary-types") or [])}

    if "compilation" in secondary and not cfg["FAVORITE_INCLUDE_COMPILATIONS"]:
        return False
    if "live" in secondary and not cfg["FAVORITE_INCLUDE_LIVE"]:
        return False
    if "remix" in secondary and not cfg["FAVORITE_INCLUDE_REMIX"]:
        return False
    # Everything else with a secondary type (Soundtrack, Demo, Mixtape, DJ-mix, ...)
    # is not what "the artist's discography" means to a listener.
    if secondary - {"compilation", "live", "remix"}:
        return False

    if primary == "album":
        return True
    if primary == "ep":
        return cfg["FAVORITE_INCLUDE_EP"]
    if primary == "single":
        return cfg["FAVORITE_INCLUDE_SINGLES"]
    # Broadcast / Other / unset: not a release you'd want in a library.
    return False


# ----------------------------------------------------------------------------
# Last.fm — recommendation engine
# ----------------------------------------------------------------------------
class LastFM:
    BASE = "http://ws.audioscrobbler.com/2.0/"

    def __init__(self, api_key):
        self.key = api_key
        self.lock = threading.Lock()
        self.last = 0

    def _get(self, method, **params):
        params.update({"method": method, "api_key": self.key, "format": "json"})
        with self.lock:  # gentle rate limit (~4 req/s)
            dt = time.time() - self.last
            if dt < 0.25:
                time.sleep(0.25 - dt)
            self.last = time.time()
        try:
            r = requests.get(self.BASE, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            # requests puts the full request URL in the exception -- which includes
            # api_key=... Scrub it so the key never lands in docker logs.
            log(f"Last.fm {method} failed: {scrub_secrets(e)}")
            return {}

    def similar(self, artist, limit=30):
        d = self._get("artist.getSimilar", artist=artist, limit=limit)
        return [a["name"] for a in d.get("similarartists", {}).get("artist", [])]

    def top_albums(self, artist, limit=10):
        d = self._get("artist.getTopAlbums", artist=artist, limit=limit)
        out = []
        for al in d.get("topalbums", {}).get("album", []):
            name = al.get("name")
            if name and name.lower() not in ("(null)", "null"):
                out.append(name)
        return out

    def top_tracks(self, artist, limit=10):
        d = self._get("artist.getTopTracks", artist=artist, limit=limit)
        return [t["name"] for t in d.get("toptracks", {}).get("track", [])]

    def artist_tags(self, artist):
        d = self._get("artist.getTopTags", artist=artist)
        return [t["name"].lower() for t in d.get("toptags", {}).get("tag", [])]

    def tag_top_artists(self, tag, limit=30):
        d = self._get("tag.getTopArtists", tag=tag, limit=limit)
        return [a["name"] for a in d.get("topartists", {}).get("artist", [])]


# ----------------------------------------------------------------------------
# Exclusions (kids' music)
# ----------------------------------------------------------------------------
def _read_name_list(path):
    names = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.append(line)
    return names


def load_exclude_set():
    return {norm(n) for n in _read_name_list(EXCLUDE_TXT)}


def is_excluded_by_name(artist, exclude_set):
    """Local, free, no API call. The only reliable defense for artists Last.fm
    tags poorly or not at all (Parry Gripp, Ms Rachel)."""
    return norm(artist) in exclude_set


def is_kids_artist(state, lastfm, artist, exclude_set):
    """Name blocklist first (free), then cached Last.fm tags (one API call ever,
    persisted to SQLite so restarts don't re-query thousands of artists)."""
    if is_excluded_by_name(artist, exclude_set):
        return True
    tags = state.get_tags(artist)
    if tags is None:
        tags = lastfm.artist_tags(artist)
        # Only cache successful lookups: Last.fm 500s intermittently, and caching
        # an error as "no tags" would permanently whitelist a kids artist.
        if tags:
            state.put_tags(artist, tags)
    return any(t in KIDS_TAGS for t in tags[:KIDS_TAG_DEPTH])


# ----------------------------------------------------------------------------
# Favorites — full discography + new releases
# ----------------------------------------------------------------------------
def sync_favorite_list(state, secrets, exclude_set):
    """Reconcile the favorites table with its two sources.

    Sources are Navidrome stars (star an artist in the player you already use) and
    favorites.txt (works before anything is starred, and covers artists not yet in the
    library at all). Removing from both un-favorites the artist.
    """
    found = {}
    for name in _read_name_list(FAVORITES_TXT):
        found[norm(name)] = (name, "manual")
    for name in navidrome_starred_artists(secrets["SOULBEET_USER"],
                                          secrets["SOULBEET_PASS"]):
        found.setdefault(norm(name), (name, "navidrome"))

    kept = []
    for n, (display, source) in found.items():
        # exclude.txt wins. Both are deliberate acts, but the blocklist is the
        # safety-oriented one -- and silently picking a side would be worse than
        # saying so out loud.
        if n in exclude_set:
            log(f"CONFLICT: '{display}' is in BOTH favorites and exclude.txt — "
                f"honoring exclude.txt and skipping it. Remove it from one of them.")
            continue
        kept.append(display)
        state.upsert_favorite(n, display, source)
    gone = state.drop_favorites_not_in(kept)
    for n in gone:
        log(f"Favorite removed: {n}")
    return kept


def sync_one_favorite(state, mb, cfg, name, display, mbid, known_rgs, added_at):
    """Queue an artist's whole discography, and log anything newly released."""
    if not mbid:
        if hasattr(mb, "resolve_artist_info") and isinstance(mb, MusicBrainz):
            info, note = mb.resolve_artist_info(display)
            if not info:
                log(f"  favorite '{display}': {note}")
                state.update_favorite(name, last_sync=int(time.time()), note=note)
                return 0, 0
            mbid = info["mbid"]
            mb_name = info.get("name") or display
            state.update_favorite(name, mbid=mbid, display=mb_name, note=None)
            state.put_artist_mb(name, mbid, mb_name=mb_name,
                                ended=info.get("ended", False),
                                end_year=info.get("end_year"))
        else:
            mbid, note = mb.resolve_mbid(display)
            if not mbid:
                log(f"  favorite '{display}': {note}")
                state.update_favorite(name, last_sync=int(time.time()), note=note)
                return 0, 0
            state.update_favorite(name, mbid=mbid, display=note or display, note=None)
            state.put_artist_mb(name, mbid, mb_name=note or display)

    rgs = mb.release_groups(mbid)
    if rgs is None:
        log(f"  favorite '{display}': MusicBrainz lookup failed; will retry next sync")
        return 0, 0

    keep = [r for r in rgs if keep_release_group(r, cfg)]
    known = set(known_rgs or [])
    now = int(time.time())
    added = new_count = 0
    for rg in keep:
        title, rg_id = rg.get("title"), rg.get("id")
        if not title:
            continue
        released = rg.get("first-release-date") or ""
        rel_year = parse_release_year(released)
        prio = cfg["FAVORITE_PRIORITY"] + recency_priority_bonus(released, now=now)
        if state.add_candidate(display, title, "album", priority=prio, release_year=rel_year):
            added += 1
        # New release = a release-group we've never seen AND dated after you
        # favorited the artist. Without the date check, the very first sync would
        # announce the entire back catalogue as "new".
        rel_ts = parse_mb_date_ts(released) or _parse_ts(released)
        if rg_id not in known and known_rgs is not None:
            if rel_ts > (added_at or 0):
                if state.add_new_release(rg_id, display, title, released):
                    log(f"  NEW RELEASE [{display} - {title}] (released {released})")
                    new_count += 1

    have = sum(1 for r in keep if _album_in_library(state, display, r.get("title")))
    state.update_favorite(name, last_sync=int(time.time()),
                          known_rgs=json.dumps([r["id"] for r in keep]),
                          albums_total=len(keep), albums_have=have)
    log(f"  favorite '{display}': {len(keep)}/{len(rgs)} release-groups kept, "
        f"{added} new candidate(s), {have} already in library")
    return added, new_count


def _album_in_library(state, artist, album):
    if not album:
        return False
    with state.lock:
        r = state.conn.execute(
            "SELECT 1 FROM library_files WHERE artist=? AND album=? LIMIT 1",
            (artist, album)).fetchone()
    return bool(r)


def favorites_sync(state, secrets, mb, cfg, exclude_set):
    names = sync_favorite_list(state, secrets, exclude_set)
    if not names:
        log("Favorites: none configured (star artists in Navidrome, or add them "
            "in the UI / favorites.txt)")
        state.set_meta("last_favorites_sync", int(time.time()))
        return
    log(f"Favorites: syncing {len(names)} artist(s) against MusicBrainz")
    total_added = total_new = 0
    for (name, display, mbid, _src, added_at, _ls, known, _at, _ah, _n) in state.favorites():
        try:
            a, n = sync_one_favorite(state, mb, cfg, name, display or name, mbid,
                                     json.loads(known) if known else None, added_at)
            total_added += a
            total_new += n
        except Exception as e:
            log(f"  favorite '{display}' sync error: {e}")
    log(f"Favorites: {total_added} candidate(s) queued at priority "
        f"{cfg['FAVORITE_PRIORITY']}, {total_new} new release(s)")
    state.set_meta("last_favorites_sync", int(time.time()))


# ----------------------------------------------------------------------------
# Played-artist new-release watch (MusicBrainz, batched per main-loop pass)
# ----------------------------------------------------------------------------
NEW_RELEASE_BATCH_SIZE = 20
MBID_MISS_RETRY_DAYS = 30
_NR_ALREADY_DONE_STATUSES = frozenset({"exists", "queued_flac", "queued_alac", "queued_lossy"})


def _resolve_artist_mb_cached(state, mb, name, display, cached_mbid, fav_mbid,
                              resolved_at, now):
    """Return (mbid, ok_to_continue). Caches hits forever and misses for 30d."""
    if cached_mbid:
        return cached_mbid, True
    if fav_mbid:
        state.put_artist_mb(name, fav_mbid, mb_name=display, resolved_at=now)
        return fav_mbid, True
    # Negative cache: empty string with a recent resolved_at means MB confirmed no match
    if cached_mbid == "" and resolved_at and (now - resolved_at) < MBID_MISS_RETRY_DAYS * 86400:
        state.touch_artist_nr_sync(name, ts=now)
        return None, True

    if hasattr(mb, "resolve_artist_info") and isinstance(mb, MusicBrainz):
        info, note = mb.resolve_artist_info(display)
        if info:
            state.put_artist_mb(name, info["mbid"], mb_name=info.get("name") or display,
                                ended=info.get("ended", False),
                                end_year=info.get("end_year"), resolved_at=now)
            return info["mbid"], True
    else:
        mbid, note = mb.resolve_mbid(display)
        if mbid:
            state.put_artist_mb(name, mbid, mb_name=note or display, resolved_at=now)
            return mbid, True

    if note == "lookup failed":
        return None, False  # transient MB failure; don't negative-cache
    state.put_artist_mb(name, "", mb_name=display, resolved_at=now,
                        last_nr_sync=now, note=note)
    return None, True


def sync_one_new_release_artist(state, mb, cfg, name, display, cached_mbid=None,
                                fav_mbid=None, resolved_at=0, now=None):
    """Check one watched artist on MusicBrainz for releases within NEW_RELEASE_MAX_AGE_DAYS.

    Returns (found, queued, ok) where `ok` is False only on a transient MB error.
    Reuses FAVORITE_INCLUDE_* release-type rules via keep_release_group().
    """
    now = int(time.time() if now is None else now)
    mbid, ok = _resolve_artist_mb_cached(
        state, mb, name, display, cached_mbid, fav_mbid, resolved_at, now)
    if not ok:
        return 0, 0, False
    if not mbid:
        return 0, 0, True

    rgs = mb.release_groups(mbid)
    if rgs is None:
        log(f"  new-release watch '{display}': MusicBrainz lookup failed; will retry")
        return 0, 0, False

    max_age_sec = int(cfg["NEW_RELEASE_MAX_AGE_DAYS"]) * 86400
    base_prio = int(cfg["NEW_RELEASE_PRIORITY"])
    fav_prio = int(cfg.get("FAVORITE_PRIORITY", 100))

    found = queued = 0
    for rg in rgs:
        if not keep_release_group(rg, cfg):
            continue
        title, rg_id = rg.get("title"), rg.get("id")
        if not title:
            continue
        released = rg.get("first-release-date") or ""
        rel_ts = parse_mb_date_ts(released)
        rel_year = parse_release_year(released)
        bonus = recency_priority_bonus(released, now=now)

        # Skip undated or unreleased far-future announcements (>7d ahead)
        is_recent = (
            rel_ts is not None
            and rel_ts <= now + 7 * 86400
            and (now - rel_ts) <= max_age_sec
        )
        if is_recent:
            found += 1
            prio = base_prio + bonus
            # Never let a non-favorite new release accidentally qualify as a favorite
            # during favorites_only cap bypass.
            if fav_prio > base_prio:
                prio = min(prio, fav_prio - 1)
            if _album_in_library(state, display, title):
                continue
            key = f"{norm(display)}|{norm(title)}|album"
            existing = state.candidate_info(key)
            if existing and existing[2] in _NR_ALREADY_DONE_STATUSES:
                continue
            prev_prio = state.candidate_priority(key) if existing else None
            is_new = state.add_candidate(display, title, "album",
                                         priority=prio, release_year=rel_year)
            promoted = (
                not is_new
                and existing is not None
                and existing[2] == "pending"
                and (prev_prio is None or prio > prev_prio)
            )
            if is_new or promoted:
                queued += 1
                if rg_id and state.add_new_release(rg_id, display, title, released):
                    log(f"  NEW RELEASE [{display} - {title}] (released {released})")
        elif bonus > 0:
            # Already have the release date in memory: gently prioritize existing
            # pending Last.fm candidates from the last decade (zero extra API calls).
            state.promote_pending_candidate(display, title, "album",
                                            priority=bonus, release_year=rel_year)

    state.touch_artist_nr_sync(name, ts=now)
    return found, queued, True


def new_releases_sync_step(state, mb, lastfm, cfg, exclude_set,
                           batch_size=NEW_RELEASE_BATCH_SIZE, now=None):
    """Incrementally sync up to `batch_size` due top-played artists on MusicBrainz.

    Spreading work across main-loop passes respects MusicBrainz's 1 req/s limit
    without stalling downloads for 10+ minutes.
    """
    top_n = int(cfg.get("NEW_RELEASE_TOP_ARTISTS", 500) or 0)
    if top_n <= 0:
        return {"checked": 0, "due": 0, "found": 0, "queued": 0}
    now = int(time.time() if now is None else now)
    sync_cutoff = now - int(cfg.get("NEW_RELEASE_SYNC_HOURS", 24)) * 3600

    watched = state.top_watched_artists(
        limit=top_n, exclude_favorites=bool(cfg.get("FAVORITES_ENABLED", True)))
    due = [r for r in watched if (r[7] or 0) < sync_cutoff]
    if not due:
        return {"checked": 0, "due": 0, "found": 0, "queued": 0}

    checked = skipped = found = queued = consec_fail = 0
    for (name, display, _w, _pw, cached_mbid, _mb_name, resolved_at, _last_sync, fav_mbid) in due:
        if checked >= batch_size:
            break
        if is_kids_artist(state, lastfm, display, exclude_set):
            state.touch_artist_nr_sync(name, ts=now)
            skipped += 1
            continue
        try:
            f, q, ok = sync_one_new_release_artist(
                state, mb, cfg, name, display,
                cached_mbid=cached_mbid, fav_mbid=fav_mbid,
                resolved_at=resolved_at, now=now,
            )
        except Exception as e:
            log(f"  new-release watch '{display}' error: {e}")
            f, q, ok = 0, 0, False
        checked += 1
        found += f
        queued += q
        if ok:
            consec_fail = 0
        else:
            consec_fail += 1
            if consec_fail >= 3:
                log("New releases: 3 consecutive MusicBrainz failures; pausing batch")
                break

    remaining = max(0, len(due) - checked - skipped)
    summary = {
        "ts": now,
        "checked": checked,
        "skipped": skipped,
        "due": remaining,
        "found": found,
        "queued": queued,
    }
    state.set_meta("last_new_release_sync", now)
    state.set_meta("last_new_release_summary", json.dumps(summary))
    if checked or skipped:
        log(f"New releases: checked {checked} artist(s) ({remaining} due"
            + (f", {skipped} excluded" if skipped else "")
            + f"), {found} recent release(s) found, {queued} queued/promoted "
              f"at priority {cfg['NEW_RELEASE_PRIORITY']}+")
    return summary


# ----------------------------------------------------------------------------
# Library measurement + free space
# ----------------------------------------------------------------------------
def probe_file(path):
    """(codec, lossless, kbps) for one audio file, or None if unreadable.

    Extension is NOT enough, which is the whole reason mutagen is here: .m4a is a
    container used by BOTH ALAC (lossless) and AAC (lossy). Classifying by extension
    would let the upgrade scanner try to "upgrade" a lossless ALAC album to FLAC, or
    silently count AAC as lossless in the quality stats. mutagen reports the real
    codec, so this stops being a guess.
    """
    from mutagen import File as MutagenFile
    try:
        au = MutagenFile(path)
    except Exception:
        return None
    if au is None or not getattr(au, "info", None):
        return None
    info = au.info
    cls = type(info).__module__ + "." + type(info).__name__
    kbps = int(round((getattr(info, "bitrate", 0) or 0) / 1000))

    if "flac" in cls.lower():
        return "flac", True, kbps
    if "mp3" in cls.lower():
        return "mp3", False, kbps
    if "mp4" in cls.lower():
        # 'alac' vs 'mp4a.40.2' (AAC-LC). This is the distinction that matters.
        codec = (getattr(info, "codec", "") or "").lower()
        if "alac" in codec:
            return "alac", True, kbps
        return "aac", False, kbps
    if "wave" in cls.lower() or "aiff" in cls.lower():
        return "wav", True, kbps
    if "opus" in cls.lower():
        return "opus", False, kbps
    if "vorbis" in cls.lower() or "oggvorbis" in cls.lower():
        return "vorbis", False, kbps
    if "wavpack" in cls.lower() or "monkeysaudio" in cls.lower() or "tak" in cls.lower():
        return cls.split(".")[-1].lower(), True, kbps
    return (cls.split(".")[-1].lower() or "unknown"), False, kbps


def _tags_of(path, fallback_dir):
    """(artist, album) from tags, falling back to beets' Artist/Album/ layout."""
    from mutagen import File as MutagenFile
    artist = album = None
    try:
        au = MutagenFile(path, easy=True)
        if au and au.tags:
            artist = (au.tags.get("albumartist") or au.tags.get("artist") or [None])[0]
            album = (au.tags.get("album") or [None])[0]
    except Exception:
        pass
    if not artist or not album:
        # beets writes <library>/<Artist>/<Album>/<track>, so the path itself carries
        # both when tags are missing or unreadable.
        p = Path(fallback_dir)
        album = album or p.name
        artist = artist or p.parent.name
    return artist, album


def translate_library_path(path, src_root, dst_root):
    """Translate a library path from `src_root` prefix to `dst_root` prefix.

    Preserves exact relative path components (including Unicode names and
    nested directories) and enforces path-boundary matching so sibling prefixes
    like `/cluster/Media/Music2` are never rewritten.
    """
    p = str(path)
    src = os.path.normpath(str(src_root))
    dst = os.path.normpath(str(dst_root))
    if src == dst:
        return p
    if p == src:
        return dst
    sep = os.sep
    prefix = src if src.endswith(sep) else src + sep
    if p.startswith(prefix):
        rel = p[len(prefix):]
        return (dst + rel) if dst.endswith(sep) else (dst + sep + rel)
    return p


def _sample_dir_entries(path, limit):
    """Return up to `limit` non-upgrade entry names from `path` using os.scandir."""
    names = []
    with os.scandir(path) as it:
        for entry in it:
            if entry.name == UPGRADE_DIRNAME:
                continue
            names.append(entry.name)
            if len(names) >= limit:
                break
    return names


def _stat_pair_matches(cand_path, music_path):
    """Compare two filesystem paths using inode-independent metadata.

    FUSE user shares (/mnt/user) and direct pool mounts (/mnt/cluster) have
    different st_dev and st_ino for the same underlying files, so verification
    relies on file type, size, and integer mtime.
    """
    try:
        st_c = os.stat(cand_path)
        st_m = os.stat(music_path)
    except OSError:
        return None, False

    c_is_dir = stat.S_ISDIR(st_c.st_mode)
    m_is_dir = stat.S_ISDIR(st_m.st_mode)
    if c_is_dir != m_is_dir:
        return None, False
    if c_is_dir:
        if int(st_c.st_mtime) != int(st_m.st_mtime):
            return None, False
        return "dir", True

    c_is_reg = stat.S_ISREG(st_c.st_mode)
    m_is_reg = stat.S_ISREG(st_m.st_mode)
    if c_is_reg != m_is_reg:
        return None, False
    if c_is_reg:
        if st_c.st_size != st_m.st_size or int(st_c.st_mtime) != int(st_m.st_mtime):
            return None, False
        return "file", True

    return "other", True


def _trees_match(candidate, music_path, sample_size=8):
    """Verify `candidate` exposes the same library tree as `music_path`.

    Samples top-level entries from both roots and descends up to 2 levels into
    a few sampled subdirectories to confirm inode-independent signatures
    (entry names, types, sizes, and mtimes) match on both sides.
    """
    cand = Path(candidate)
    music = Path(music_path)
    try:
        if not cand.is_dir() or not music.is_dir():
            return False
        if os.path.realpath(cand) == os.path.realpath(music):
            return True
        music_names = _sample_dir_entries(music, sample_size)
        cand_names = _sample_dir_entries(cand, sample_size)
    except OSError:
        return False

    names = list(dict.fromkeys(music_names + cand_names))
    if not names:
        return False

    matched_files = 0
    matched_dirs = []
    for name in names:
        kind, ok = _stat_pair_matches(cand / name, music / name)
        if not ok:
            return False
        if kind == "file":
            matched_files += 1
        elif kind == "dir":
            matched_dirs.append(name)

    # Descend into a few sampled subdirs (Artist -> Album -> track) so regular
    # file names, sizes, and mtimes are verified even when top-level entries
    # are all artist directories.
    queue = [(d, 1) for d in matched_dirs[:3]]
    while queue and matched_files < 4:
        rel_dir, depth = queue.pop(0)
        try:
            sub_m = _sample_dir_entries(music / rel_dir, 4)
            sub_c = _sample_dir_entries(cand / rel_dir, 4)
        except OSError:
            return False
        sub_names = list(dict.fromkeys(sub_m + sub_c))
        for name in sub_names:
            rel_child = os.path.join(rel_dir, name)
            kind, ok = _stat_pair_matches(cand / rel_child, music / rel_child)
            if not ok:
                return False
            if kind == "file":
                matched_files += 1
            elif kind == "dir" and depth < 2:
                queue.append((rel_child, depth + 1))

    return True


def resolve_scan_root(setting="auto", music_path=None, auto_candidate=None):
    """Resolve the filesystem root to walk during library scans.

    Returns (scan_root_path, reason_str). Never returns a write target — the
    returned path is used strictly for read-only walk/stat/probe calls, and all
    paths persisted in `library_files` are translated back to `music_path`.
    """
    music = Path(music_path) if music_path is not None else MUSIC_PATH
    cand_default = (Path(auto_candidate) if auto_candidate is not None
                    else AUTO_SCAN_ROOT_CANDIDATE)
    raw = (str(setting) if setting is not None else "auto").strip()
    if not raw or raw.lower() == "auto":
        if _trees_match(cand_default, music):
            return cand_default, "auto-detected direct pool mount"
        return music, "auto fallback to MUSIC_PATH"

    explicit = Path(os.path.normpath(raw))
    if os.path.normpath(str(explicit)) == os.path.normpath(str(music)):
        return music, "configured"
    if _trees_match(explicit, music):
        return explicit, "configured (verified match)"
    log(f"LIBRARY_SCAN_ROOT={explicit} does not exist or does not match {music}; "
        f"falling back to {music}")
    return music, "configured mismatch fallback"


def measure_library(state=None, max_probes=None, scan_root=None, music_path=None):
    """Walk the library once for size, track count, and per-file quality.

    Incremental: a file whose (size, mtime) is unchanged is not re-opened, so the
    steady-state cost is a stat() per file rather than a tag parse.

    When `scan_root` differs from `music_path` (e.g., walking the read-only ZFS
    mount `/cluster/Media/Music` instead of Unraid's FUSE `/music` share), all
    walk/stat/probe reads go through `scan_root`, while every path stored or
    compared in `library_files` is translated back to the canonical `music_path`
    prefix (`/music/...`) so existing rows match without re-probing or
    duplicating anything.
    """
    started = time.monotonic()
    canonical_base = os.path.normpath(str(music_path if music_path is not None else MUSIC_PATH))
    walk_base = os.path.normpath(str(scan_root if scan_root is not None else canonical_base))
    if not os.path.isdir(walk_base):
        raise OSError(f"library scan root is not a directory: {walk_base}")

    total_bytes = 0
    track_count = 0
    known = state.known_files() if state else {}
    seen = set()
    pending = []
    reprobed = 0
    unprobed = 0

    for root, dirs, files in os.walk(walk_base):
        # The upgrade staging dir lives inside the library tree (see UPGRADE_FOLDER).
        # It must not be counted: staged files are unverified, would double-count
        # albums we already own, and -- since they're FLAC awaiting a swap -- would
        # skew the lossless stats toward a state that isn't real yet.
        if UPGRADE_DIRNAME in dirs:
            dirs.remove(UPGRADE_DIRNAME)
        canonical_root = translate_library_path(root, walk_base, canonical_base)
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            full_scan = os.path.join(root, f)
            try:
                st = os.stat(full_scan)
            except OSError:
                continue
            track_count += 1
            total_bytes += st.st_size
            if not state:
                continue
            canonical_full = os.path.join(canonical_root, f)
            seen.add(canonical_full)
            prev = known.get(canonical_full)
            if prev and prev[0] == st.st_size and prev[1] == int(st.st_mtime):
                continue
            # Budget exhausted: leave it for the next pass rather than hammering
            # storage. Size/track totals above are already counted, so only the
            # quality breakdown lags -- and it catches up on its own.
            if max_probes is not None and reprobed >= max_probes:
                unprobed += 1
                continue
            probed = probe_file(full_scan)
            if not probed:
                continue
            codec, lossless, kbps = probed
            artist, album = _tags_of(full_scan, canonical_root)
            pending.append((canonical_full, canonical_root, artist, album, codec,
                            int(lossless), kbps, st.st_size, int(st.st_mtime),
                            int(time.time())))
            reprobed += 1
            if len(pending) >= 250:
                state.put_files(pending)
                pending = []

    if state:
        if pending:
            state.put_files(pending)
        gone = list(set(known) - seen)
        if gone:
            for i in range(0, len(gone), 500):
                state.drop_files(gone[i:i + 500])
        elapsed = time.monotonic() - started
        msg = (f"Library scan ({walk_base}, {elapsed:.1f}s): "
               f"{reprobed} new/changed file(s) probed")
        if gone:
            msg += f", {len(gone)} removed"
        if unprobed:
            msg += (f", {unprobed} awaiting probe (rate-limited to "
                    f"{max_probes}/pass)")
        log(msg)
        state.set_meta("unprobed_files", unprobed)
    return total_bytes, track_count


class LibraryScanner:
    """Runs periodic library scans on a single background thread so the main
    candidate-processing loop is never starved while walking the library.

    Guarantees at most one scan runs at a time, updates cached totals atomically
    on completion, and produces STATUS / status.json after each completed scan.
    """

    def __init__(self, state, get_cfg=None, measure_fn=None,
                 resolve_root_fn=None, free_gb_fn=None, write_status_fn=None):
        self.state = state
        self.get_cfg = get_cfg or load_config
        self._measure_fn = measure_fn or measure_library
        self._resolve_root_fn = resolve_root_fn or resolve_scan_root
        self._free_gb_fn = free_gb_fn or free_gb
        self._write_status_fn = write_status_fn or write_status

        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._last_finished = 0.0
        self._last_scan_root = None
        self._last_duration = None
        self._scan_count = 0
        self._last_error = None

        # Seed initial totals from persisted library_files rows so stop_reason()
        # has accurate library size/track counts immediately on startup while the
        # first background scan runs.
        init_bytes, init_tracks = state.library_totals() if state else (0, 0)
        self._lib_bytes = init_bytes
        self._track_count = init_tracks
        self._free_gb = self._free_gb_fn(FREE_SPACE_PATH)

    @property
    def is_running(self):
        with self._lock:
            return self._running

    def snapshot(self):
        """Return latest (lib_bytes, track_count, free_gb) thread-safely."""
        with self._lock:
            return self._lib_bytes, self._track_count, self._free_gb

    def last_scan_info(self):
        with self._lock:
            return {
                "running": self._running,
                "scan_root": self._last_scan_root,
                "duration_sec": self._last_duration,
                "scan_count": self._scan_count,
                "last_finished": self._last_finished,
                "last_error": self._last_error,
            }

    def reset_schedule(self):
        """Allow the next maybe_start() to trigger immediately (unless running)."""
        with self._lock:
            self._last_finished = 0.0

    def maybe_start(self, cfg, force=False):
        """Launch a background scan if due and none is already running.

        Returns True if a new background scan was started, False otherwise.
        """
        now = time.time()
        with self._lock:
            if self._running:
                return False
            if (not force and self._last_finished > 0
                    and (now - self._last_finished) <= cfg["MEASURE_EVERY_SEC"]):
                return False
            self._running = True
            t = threading.Thread(
                target=self._run,
                args=(dict(cfg),),
                name="library-scan",
                daemon=True,
            )
            self._thread = t
        t.start()
        return True

    def wait(self, timeout=None):
        """Wait for the current background scan thread (if any) to finish."""
        with self._lock:
            t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        return not self.is_running

    def _run(self, cfg):
        t0 = time.monotonic()
        try:
            scan_root, reason = self._resolve_root_fn(cfg.get("LIBRARY_SCAN_ROOT", "auto"))
            with self._lock:
                prev_root = self._last_scan_root
                self._last_scan_root = str(scan_root)
            if prev_root != str(scan_root):
                log(f"Library scan root: {scan_root} ({reason}; canonical prefix {MUSIC_PATH})")

            lib_bytes, track_count = self._measure_fn(
                self.state,
                max_probes=cfg["SCAN_MAX_PROBES_PER_PASS"],
                scan_root=scan_root,
            )
            free = self._free_gb_fn(FREE_SPACE_PATH)
            elapsed = time.monotonic() - t0
            with self._lock:
                self._lib_bytes = lib_bytes
                self._track_count = track_count
                self._free_gb = free
                self._last_duration = elapsed
                self._scan_count += 1
                self._last_error = None
            status_cfg = self.get_cfg() if self.get_cfg else cfg
            self._write_status_fn(
                self.state, status_cfg, lib_bytes, track_count, free,
                scan_root=str(scan_root), scan_duration_sec=elapsed,
            )
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            log(f"Library scan error (non-fatal): {e}")
        finally:
            with self._lock:
                self._last_finished = time.time()
                self._running = False


def free_gb(path):
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except OSError:
        return float("inf")


MAX_SIMILAR_DEPTH = 2
_SHORT_DECADE_END = {
    "50s": 1959, "60s": 1969, "70s": 1979, "80s": 1989, "90s": 1999, "00s": 2009,
}
_LONG_DECADE_RE = re.compile(r"^(19[2-9]0|20[0-2]0)s$")


def is_inactive_explore_artist(state, artist, max_inactive_years, now=None):
    """Cheaply detect clearly old/ended artists using ONLY already-cached state.

    Returns True when:
      1. Cached MusicBrainz life-span (artist_mb) shows the artist ended before
         (current_year - max_inactive_years), OR
      2. Cached Last.fm tags (artist_tags) contain decade tags and the latest
         decade ended before (current_year - max_inactive_years), OR top tags
         include 'oldies' with no recent decade tag.

    Makes zero network calls. Unknown/uncached artists return False.
    """
    max_years = int(max_inactive_years or 0)
    if max_years <= 0:
        return False
    cur_year = time.gmtime(now).tm_year if now is not None else time.gmtime().tm_year
    cutoff_year = cur_year - max_years

    mb_info = state.get_artist_mb(artist)
    if mb_info and mb_info.get("ended") and mb_info.get("end_year"):
        if int(mb_info["end_year"]) < cutoff_year:
            return True

    tags = state.get_tags(artist)
    if tags:
        decade_ends = []
        for t in tags[:15]:
            tl = (t or "").strip().lower()
            if tl in _SHORT_DECADE_END:
                decade_ends.append(_SHORT_DECADE_END[tl])
            else:
                m = _LONG_DECADE_RE.match(tl)
                if m:
                    decade_ends.append(int(m.group(1)) + 9)
        if decade_ends:
            return max(decade_ends) < cutoff_year
        if "oldies" in {((t or "").strip().lower()) for t in tags[:6]}:
            return True
    return False


# ----------------------------------------------------------------------------
# Core: expand seed graph, process candidates
# ----------------------------------------------------------------------------
def refresh_taste(state, secrets, lastfm, exclude_set, cfg):
    """Pull recency-weighted play counts from every source, enqueue weighted seeds.

    Three signals, each weighted by how much it says about current taste:
      Navidrome (what you play in this library), Plex/Plexamp (via Tautulli), and
      YouTube Music history (the cold-start signal, from Takeout).
    """
    hl = cfg["TASTE_HALF_LIFE_DAYS"]
    played_weights = {}
    user, pw = secrets["SOULBEET_USER"], secrets["SOULBEET_PASS"]
    for name, w in navidrome_play_weights(user, pw, hl).items():
        played_weights[name] = played_weights.get(name, 0) + w * cfg["WEIGHT_NAVIDROME"]
    for name, w in tautulli_play_weights(secrets.get("TAUTULLI_API_KEY"), hl).items():
        played_weights[name] = played_weights.get(name, 0) + w * cfg["WEIGHT_PLEX"]
    for name, w in youtube_music_weights(hl).items():
        played_weights[name] = played_weights.get(name, 0) + w * cfg["WEIGHT_YTMUSIC"]
    weights = dict(played_weights)
    # ensure existing library artists are at least seeded (weight 1) so we always have a base
    for name in navidrome_all_artists(user, pw):
        weights.setdefault(name, 1.0)

    # Only the free local blocklist here. Tag lookups are NOT done at refresh time:
    # doing them for every artist meant ~1900 sequential Last.fm calls (~8 min) on
    # every refresh and again on every restart. They now happen lazily in
    # expand_one_artist -- one call per artist we actually try to expand, cached
    # in SQLite forever.
    added = skipped = 0
    for name, w in weights.items():
        if is_excluded_by_name(name, exclude_set):
            skipped += 1
            continue
        state.upsert_artist(name, weight=w, depth=0,
                            play_weight=played_weights.get(name, 0.0))
        added += 1
    state.sync_pending_artist_weights()
    log(f"Taste refresh: {added} weighted artists seeded, {skipped} blocklisted "
        f"(top play counts drive expansion)")
    state.set_meta("last_taste_refresh", int(time.time()))


def expand_one_artist(state, lastfm, artist, weight, cfg, exclude_set, depth=0):
    """Generate album candidates for one artist: exploitation + a share of exploration.

    This is where the kids filter actually bites: seeding is cheap and unfiltered
    (beyond the name blocklist), but nothing becomes a download candidate without
    passing the tag check here first.
    """
    if is_kids_artist(state, lastfm, artist, exclude_set):
        log(f"  excluded (kids): {artist}")
        state.mark_expanded(artist)
        return 0
    max_inactive = cfg.get("EXPLORE_MAX_INACTIVE_YEARS", 15)
    # Artists that only arrived via tag exploration or deep propagation (>=2 hops):
    # now that is_kids_artist() has cached their Last.fm tags, skip clearly old ones.
    if depth >= MAX_SIMILAR_DEPTH and is_inactive_explore_artist(state, artist, max_inactive):
        log(f"  excluded (inactive explore): {artist}")
        state.mark_expanded(artist)
        return 0
    new = 0
    # exploitation: this artist's own top albums + similar artists' top albums
    for alb in lastfm.top_albums(artist, limit=8):
        if state.add_candidate(artist, alb, "album", artist_weight=weight):
            new += 1
    # Cap similar-artist propagation depth so the graph doesn't drift 5+ hops away
    # from the user's actual listening into classic/generic acts.
    if depth < MAX_SIMILAR_DEPTH:
        for sim in lastfm.similar(artist, limit=25):
            # cheap name check only; the tag check happens when sim is itself expanded
            if not is_excluded_by_name(sim, exclude_set):
                state.upsert_artist(sim, weight=weight * 0.5, depth=depth + 1)

    # exploration: from direct taste seeds' primary tags, pull artists we don't own
    if cfg["EXPLORE_RATIO"] > 0 and depth == 0:
        for tag in (state.get_tags(artist) or [])[:2]:
            if tag in KIDS_TAGS:
                continue
            for exp_artist in lastfm.tag_top_artists(tag, limit=int(15 * cfg["EXPLORE_RATIO"]) + 3):
                if is_excluded_by_name(exp_artist, exclude_set):
                    continue
                if is_inactive_explore_artist(state, exp_artist, max_inactive):
                    continue
                state.upsert_artist(exp_artist, weight=weight * 0.2,
                                    depth=MAX_SIMILAR_DEPTH)
    state.mark_expanded(artist)
    return new


def process_candidate(state, sb, secrets, row, backend_down=None, cfg=None):
    key, artist, album, kind = row
    if backend_down is not None and backend_down.is_set():
        return "backend_down"
    try:
        # skip if already in the Navidrome library
        if album and navidrome_album_exists(secrets["SOULBEET_USER"], secrets["SOULBEET_PASS"], artist, album):
            state.set_status(key, "exists")
            return "exists"

        if backend_down is not None and backend_down.is_set():
            return "backend_down"

        album_obj, tracks = None, []
        results = sb.search_album(artist, album or artist)
        if results:
            album_obj = results[0]
            rel_year = parse_release_year(album_obj.get("release_date"))
            if rel_year is not None and hasattr(state, "set_candidate_release_year"):
                state.set_candidate_release_year(key, rel_year)
        else:
            tr = sb.search_track(artist, album or artist)
            if not tr:
                state.set_status(key, "no_meta")
                return "no_meta"
            tracks = [tr[0]]

        sid = sb.start_search(album_obj, tracks)
        res = sb.poll(sid)
        status, items, info = pick_by_quality_ladder(
            res.get("groups", []), blocked=_blocked_for(state, key, cfg))
        if items:
            qres = sb.queue(items, sid)
            if isinstance(qres, dict) and qres.get("unconfirmed"):
                log(f"  queue response timed out after {qres.get('timeout')}s "
                    f"for {artist} - {album}; assuming queued "
                    f"(Soulbeet usually finishes queuing)")
            _record_downloads_safe(state, "candidate", key, items, sid)

        # Document every non-FLAC outcome explicitly: what it fell back to and why.
        if info and status == "queued_alac":
            log(f"  FALLBACK [{artist} - {album}]: no FLAC source -> ALAC "
                f"(.m4a @ ~{info['kbps']} kbps, lossless)")
        elif info and status == "queued_lossy":
            log(f"  FALLBACK [{artist} - {album}]: no lossless source -> "
                f"{info['fmt']} @ ~{info['kbps']} kbps (lossy)")
        elif status == "low_quality_only":
            log(f"  SKIPPED [{artist} - {album}]: only sub-threshold lossy sources")

        state.set_status(key, status,
                         fmt=(info or {}).get("fmt"), kbps=(info or {}).get("kbps"))
        return status
    except BackendUnavailable as e:
        if backend_down is None or not backend_down.is_set():
            if backend_down is not None:
                backend_down.set()
            log(f"  backend unavailable [{artist} - {album}] (leaving pending): {e}")
        return "backend_down"
    except Exception as e:
        log(f"  candidate error [{artist} - {album}]: {e}")
        state.set_status(key, "error")
        return "error"


# ----------------------------------------------------------------------------
# Decode gate — ingest beets decodecheck rejects, block bad peers, retry
# ----------------------------------------------------------------------------
DOWNLOADS_PRUNE_DAYS = 30
DOWNLOADS_PRUNE_INTERVAL_SEC = 24 * 3600


def _blocked_for(state, key, cfg):
    cfg = cfg or {}
    return state.blocked_peers(key, strikes=int(cfg.get("BAD_PEER_STRIKES", 3)),
                               days=int(cfg.get("BAD_PEER_DAYS", 90)))


def _record_downloads_safe(state, kind, key, items, sid):
    """Record queued items; never let a bookkeeping failure turn an already-queued
    download into an 'error' (which would re-queue it)."""
    try:
        state.record_downloads(kind, key, items, sid)
    except Exception as e:
        log(f"  decode gate: could not record downloads for {key}: {e}")


def _load_rejects_cursor(state):
    raw = state.get_meta("decode_rejects_cursor")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _save_rejects_cursor(state, path_str, ino, offset, size):
    state.set_meta("decode_rejects_cursor", json.dumps({
        "path": path_str,
        "ino": int(ino),
        "offset": int(offset),
        "size": int(size),
    }))


def _process_decode_reject(state, cfg, rec):
    """Apply one parsed rejects.jsonl record. Returns True if newly processed."""
    reason = str(rec.get("reason") or "unknown").strip() or "unknown"
    staging_path = str(rec.get("staging_path") or "").strip()
    sp_parts = _split_path_parts(staging_path)
    basename = str(rec.get("basename") or (sp_parts[-1] if sp_parts else "")).strip()
    staging_parent = sp_parts[-2] if len(sp_parts) >= 2 else ""
    reject_ts = _parse_ts(rec.get("ts")) or int(time.time())
    task_id = str(rec.get("task_id") or "").strip()
    dedupe_task_id = task_id or f"notask:{reject_ts}:{staging_path}"

    if state.has_decode_reject(dedupe_task_id, basename):
        return False

    tag_keys = []
    if rec.get("album"):
        for a in (rec.get("albumartist"), rec.get("artist")):
            if a:
                k = f"{norm(str(a))}|{norm(str(rec.get('album')))}|album"
                if k not in tag_keys:
                    tag_keys.append(k)
    match = state.match_download(basename, staging_parent=staging_parent,
                                 reject_ts=reject_ts, tag_keys=tag_keys)
    if match is None:
        # No downloads row identifies it: if the tags name a candidate that is
        # still waiting on a download, retry that album anyway (no peer blamed).
        for k in tag_keys:
            info = state.candidate_info(k)
            if info and str(info[2] or "").startswith("queued_"):
                match = {"username": "", "ref_key": k, "kind": "candidate"}
                break
    matched = match is not None
    peer = match["username"] if match else ""
    ref_key = match["ref_key"] if match else ""
    kind = match["kind"] if match else ""

    already_acted_task = bool(
        task_id and matched and state.task_already_matched_key(task_id, ref_key)
    )

    if not state.record_decode_reject(
        dedupe_task_id, basename, reject_ts, reason, matched,
        username=peer, ref_key=ref_key, kind=kind,
    ):
        return False

    # Fail-closed checker errors are our own fault (e.g. missing ffmpeg), never
    # the Soulseek peer's — do not block peers or consume retries.
    if reason == "checker_unavailable":
        detail = _truncate_body(str(rec.get("detail") or "decode checker unavailable"), 200)
        log(f"DECODE CHECKER UNAVAILABLE ({basename or staging_path}): {detail} "
            f"— not blocking peer or consuming retries; re-queued once the checker "
            f"accepts a task again")
        return True

    if not matched:
        artist = rec.get("artist") or rec.get("albumartist") or "?"
        album = rec.get("album") or "?"
        log(f"DECODE REJECT [unmatched {artist} - {album}]: {reason} ({basename})")
        return True

    if peer:
        state.add_bad_source(peer, ref_key, reason, basename, ts=reject_ts)

    peer_lbl = peer or "unknown"

    if kind == "candidate":
        cinfo = state.candidate_info(ref_key)
        artist = (cinfo[0] if cinfo and cinfo[0] else rec.get("artist") or ref_key)
        album = (cinfo[1] if cinfo and cinfo[1] else rec.get("album") or "")
        label = f"{artist} - {album}" if album else artist
        max_retries = int(cfg.get("DECODE_MAX_RETRIES", 3))
        if already_acted_task:
            log(f"DECODE REJECT [{label}]: {reason} from peer {peer_lbl} "
                f"({basename}; same task)")
        else:
            new_status, retries = state.handle_candidate_decode_reject(ref_key, max_retries)
            if new_status == "pending":
                log(f"DECODE REJECT [{label}]: {reason} from peer {peer_lbl}; "
                    f"retry {retries}/{max_retries}")
            elif new_status == "corrupt_only":
                log(f"DECODE REJECT [{label}]: {reason} from peer {peer_lbl}; "
                    f"corrupt_only ({retries}/{max_retries} retries used)")
            else:
                log(f"DECODE REJECT [{label}]: {reason} from peer {peer_lbl} "
                    f"(candidate not found)")
        return True

    if kind == "upgrade":
        uinfo = state.upgrade_info(ref_key)
        artist = (uinfo[0] if uinfo and uinfo[0] else rec.get("artist") or ref_key)
        album = (uinfo[1] if uinfo and uinfo[1] else rec.get("album") or "")
        label = f"{artist} - {album}" if album else artist
        note = f"decode reject: {reason} from {peer_lbl}"
        state.upsert_upgrade(ref_key, status="failed", note=note)
        log(f"DECODE REJECT [upgrade {label}]: {reason} from peer {peer_lbl}; "
            f"marked failed")
        return True

    log(f"DECODE REJECT [{kind}:{ref_key}]: {reason} from peer {peer_lbl} ({basename})")
    return True


def ingest_decode_rejects(state, cfg):
    """Incrementally read new lines from DECODE_REJECTS_PATH and learn from them.

    Tracks (path, inode, byte_offset, size) in meta so steady-state polling is one
    stat() call. Handles missing files, rotation/truncation, partial trailing lines
    without a newline, and malformed JSON lines cleanly.
    """
    path_str = (cfg.get("DECODE_REJECTS_PATH") or DEFAULT_DECODE_REJECTS_PATH).strip()
    if not path_str:
        return 0
    path = Path(path_str)
    try:
        st = os.stat(path)
    except OSError:
        return 0

    ino = st.st_ino
    size = st.st_size
    cur = _load_rejects_cursor(state)
    same_file = (cur.get("path") == path_str and cur.get("ino") == ino)
    offset = int(cur.get("offset", 0)) if same_file else 0
    if size < offset:
        offset = 0  # truncated in place

    if same_file and size == offset and cur.get("size") == size:
        return 0

    processed = 0
    new_offset = offset
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            while True:
                raw = f.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    # Incomplete line mid-write: wait for the trailing newline next loop.
                    break
                new_offset += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError as e:
                    log(f"Decode gate: skipping malformed JSON line in {path}: {e}")
                    continue
                if not isinstance(rec, dict):
                    log(f"Decode gate: skipping non-object JSON line in {path}")
                    continue
                try:
                    if _process_decode_reject(state, cfg, rec):
                        processed += 1
                except Exception as e:  # one odd record must not wedge the reader
                    log(f"Decode gate: failed to apply reject line in {path}: {e}")
    except OSError as e:
        log(f"Decode gate: failed reading {path}: {e}")
        return processed

    if new_offset != offset or not same_file or cur.get("size") != size:
        _save_rejects_cursor(state, path_str, ino, new_offset, size)
    return processed


def checker_recovered_since(checked_path):
    """ts of the newest accepted task in checked.jsonl (tail only), or 0."""
    p = Path(checked_path)
    newest = 0
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > 256 * 1024:
                f.seek(size - 256 * 1024)
                f.readline()
            for raw in f:
                try:
                    rec = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    newest = max(newest, _parse_ts(rec.get("ts")))
    except OSError:
        return 0
    return newest


def repend_checker_unavailable(state, cfg):
    """Re-pend candidates whose download was dropped while the checker was
    broken, once checked.jsonl shows it accepting tasks again (a later accepted
    line). Waiting for that proof avoids a re-download loop during an outage."""
    rows = state.checker_unavailable_pending()
    if not rows:
        return 0
    rejects_path = Path((cfg.get("DECODE_REJECTS_PATH") or DEFAULT_DECODE_REJECTS_PATH).strip())
    recovered = checker_recovered_since(rejects_path.parent / "checked.jsonl")
    n = 0
    for task_id, basename, ts, ref_key, kind in rows:
        if recovered <= int(ts or 0):
            continue
        if state.repend_after_checker_outage(
                task_id, basename, ref_key, kind,
                max_repends=int(cfg.get("DECODE_MAX_RETRIES", 3))):
            n += 1
            log(f"Decode gate: checker works again; re-queued {ref_key} "
                f"(dropped as checker_unavailable)")
    return n


def maybe_prune_downloads(state, now=None, older_than_days=DOWNLOADS_PRUNE_DAYS):
    """Prune downloads rows older than 30 days at most once per day."""
    now = int(time.time() if now is None else now)
    raw_last = state.get_meta("last_downloads_prune")
    if raw_last is not None and now - int(raw_last) < DOWNLOADS_PRUNE_INTERVAL_SEC:
        return 0
    removed = state.prune_downloads(older_than_days=older_than_days, now=now)
    state.set_meta("last_downloads_prune", now)
    if removed:
        log(f"Decode gate: pruned {removed} downloads row(s) older than {older_than_days}d")
    return removed


def count_checked_accepted(path, since_ts):
    """Count accepted tasks in checked.jsonl with ts >= since_ts, or None if absent."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    max_tail = 2 * 1024 * 1024
    seen_tasks = set()
    count = 0
    try:
        with open(p, "rb") as f:
            if st.st_size > max_tail:
                f.seek(st.st_size - max_tail)
                f.readline()  # discard first partial line
            for raw in f:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if _parse_ts(rec.get("ts")) < since_ts:
                    continue
                tid = rec.get("task_id")
                if tid:
                    if tid in seen_tasks:
                        continue
                    seen_tasks.add(tid)
                count += 1
    except OSError:
        return None
    return count


def decode_gate_status(state, cfg, now=None):
    """Build the `decode_gate` section of status.json."""
    now = int(time.time() if now is None else now)
    rejects_path = Path((cfg.get("DECODE_REJECTS_PATH") or DEFAULT_DECODE_REJECTS_PATH).strip())
    gate_dir = rejects_path.parent
    dir_missing = not gate_dir.is_dir()

    stats = state.decode_reject_stats(now=now)
    strikes = int(cfg.get("BAD_PEER_STRIKES", 3))
    days = int(cfg.get("BAD_PEER_DAYS", 90))
    blocked_peers = len(state.global_blocked_peers(strikes=strikes, days=days, now=now))
    accepted_24h = count_checked_accepted(gate_dir / "checked.jsonl", since_ts=now - 86400)

    checker_unavail_24h = stats["checker_unavailable_24h"]
    checker_warning = bool(dir_missing or checker_unavail_24h > 0)
    if dir_missing:
        warning_reason = (f"decode gate not installed: {gate_dir} is missing, so "
                          f"corrupt downloads are not being checked")
    elif checker_unavail_24h > 0:
        warning_reason = f"decode checker unavailable ({checker_unavail_24h}x in last 24h)"
    else:
        warning_reason = None

    return {
        "rejects_24h": stats["rejects_24h"],
        "rejects_7d": stats["rejects_7d"],
        "matched": stats["matched_7d"],
        "unmatched": stats["unmatched_7d"],
        "by_reason_24h": stats["by_reason_24h"],
        "by_reason_7d": stats["by_reason_7d"],
        "matched_24h": stats["matched_24h"],
        "unmatched_24h": stats["unmatched_24h"],
        "matched_7d": stats["matched_7d"],
        "unmatched_7d": stats["unmatched_7d"],
        "blocked_peers": blocked_peers,
        "accepted_24h": accepted_24h,
        "checker_warning": checker_warning,
        "checker_warning_reason": warning_reason,
    }


# ----------------------------------------------------------------------------
# Scheduled lossy -> FLAC upgrades
# ----------------------------------------------------------------------------
def upgrade_one(state, sb, cand, cfg):
    """Search for a FLAC of an album we own as lossy; stage it if one exists.

    Only acts on queued_flac. A lossy->lossy sidegrade is not an upgrade, and an
    ALAC result can't appear here because the scanner only ever considers files
    mutagen classified as lossy in the first place.
    """
    key, artist, album = cand["key"], cand["artist"], cand["album"]

    def record(status, note):
        state.upsert_upgrade(
            key, artist=artist, album=album, dir=cand["dir"],
            cur_codec=cand["codec"], cur_kbps=cand["kbps"],
            cur_tracks=cand["tracks"], last_checked=int(time.time()),
            attempts=(cand.get("attempts", 0) + 1),
            status=status, note=note,
        )

    try:
        results = sb.search_album(artist, album)
        if not results:
            record("no_flac", "no album metadata match")
            return "no_flac"
        sid = sb.start_search(results[0], [])
        res = sb.poll(sid)
        status, items, info = pick_by_quality_ladder(
            res.get("groups", []), blocked=_blocked_for(state, key, cfg))
        if status != "queued_flac" or not items:
            record("no_flac", f"best available was {status}")
            return "no_flac"
        # Stage it, never straight into the library: nothing replaces a file you
        # already have until the swap script has verified it end to end.
        qres = sb.queue(items, sid, target_folder=UPGRADE_FOLDER)
        if isinstance(qres, dict) and qres.get("unconfirmed"):
            log(f"  queue response timed out after {qres.get('timeout')}s "
                f"for {artist} - {album} (upgrade); assuming queued "
                f"(Soulbeet usually finishes queuing)")
        _record_downloads_safe(state, "upgrade", key, items, sid)
        record("queued", f"flac @ ~{(info or {}).get('kbps', 0)} kbps staged")
        log(f"  UPGRADE [{artist} - {album}]: {cand['codec']} @ ~{cand['kbps']} kbps "
            f"-> FLAC staged for verification")
        return "queued"
    except BackendUnavailable as e:
        log(f"  upgrade deferred [{artist} - {album}] (backend unavailable): {e}")
        return "backend_down"
    except Exception as e:
        record("failed", str(e)[:200])
        log(f"  upgrade error [{artist} - {album}]: {e}")
        return "failed"


def upgrade_pass(state, sb, cfg):
    cutoff = int(time.time()) - cfg["UPGRADE_RECHECK_DAYS"] * 86400
    cands = state.lossy_albums(limit=cfg["UPGRADE_MAX_PER_RUN"], older_than=cutoff)
    if not cands:
        log("Upgrade pass: nothing eligible (all lossy albums checked recently)")
        return
    log(f"Upgrade pass: checking {len(cands)} lossy album(s) for FLAC "
        f"(max {cfg['UPGRADE_MAX_PER_RUN']}/run)")
    counts = collections.Counter()
    for c in cands:
        res = upgrade_one(state, sb, c, cfg)
        counts[res] += 1
        if res == "backend_down":
            log("Upgrade pass: backend unavailable; stopping pass early")
            break
    log(f"Upgrade pass done: {dict(counts)}")


def upgrade_loop(state, sb, get_cfg, is_busy):
    """Daemon: one bounded pass per day at UPGRADE_HOUR.

    Deliberately conservative -- this competes with the main fill for the same
    Soulseek peers, and being a nuisance to peers is how you get banned.
    """
    while True:
        try:
            cfg = get_cfg()
            if not cfg["UPGRADE_ENABLED"]:
                time.sleep(300)
                continue
            now = time.localtime()
            last = int(state.get_meta("last_upgrade_pass", "0"))
            forced = state.get_meta("force_upgrade_pass", "0") == "1"  # "Scan now" in the UI
            due = forced or (now.tm_hour == cfg["UPGRADE_HOUR"]
                             and time.time() - last > 20 * 3600)
            if not due:
                time.sleep(300)
                continue
            if cfg["UPGRADE_ONLY_WHEN_IDLE"] and is_busy():
                log("Upgrade pass due, but the main fill is busy; waiting")
                time.sleep(600)
                continue
            state.set_meta("force_upgrade_pass", 0)
            state.set_meta("last_upgrade_pass", int(time.time()))
            upgrade_pass(state, sb, cfg)
        except Exception as e:
            log(f"upgrade loop error (non-fatal): {e}")
            time.sleep(300)


_STATUS_WRITE_LOCK = threading.Lock()


def write_status(state, cfg, lib_bytes, track_count, free,
                 scan_root=None, scan_duration_sec=None):
    counts = state.status_counts()
    tb = lib_bytes / (1024 ** 4)
    status = {
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "library_tb": round(tb, 3),
        "target_tb": cfg["TARGET_TB"],
        "tracks": track_count,
        "target_tracks": cfg["TARGET_TRACKS"],
        "pct_to_target": round(100 * max(tb / cfg["TARGET_TB"] if cfg["TARGET_TB"] else 0,
                                         track_count / cfg["TARGET_TRACKS"] if cfg["TARGET_TRACKS"] else 0), 1),
        "cluster_free_gb": round(free, 1),
        # lossless
        "queued_flac": counts.get("queued_flac", 0),
        "queued_alac": counts.get("queued_alac", 0),
        # lossy fallback
        "queued_lossy": counts.get("queued_lossy", 0),
        # skipped / other
        "no_source": counts.get("no_source", 0),
        "low_quality_only": counts.get("low_quality_only", 0),
        "no_meta": counts.get("no_meta", 0),
        "exists": counts.get("exists", 0),
        "error": counts.get("error", 0),
        "corrupt_only": counts.get("corrupt_only", 0),
        "pending": counts.get("pending", 0),
        # exactly what fell back to what, most recent first
        "recent_fallbacks": [
            {"artist": a, "album": b, "format": f, "kbps": k,
             "lossless": s == "queued_alac"}
            for (a, b, s, f, k) in state.fallbacks(25)
        ],
    }
    lossless = status["queued_flac"] + status["queued_alac"]
    status["lossless_total"] = lossless
    status["lossless_pct"] = (round(100 * lossless / (lossless + status["queued_lossy"]), 1)
                              if (lossless + status["queued_lossy"]) else None)

    # What's actually on disk, as opposed to what we queued. These differ: queue stats
    # only describe this run, the library predates it.
    breakdown = state.quality_breakdown()
    lib_lossless = sum(b["tracks"] for b in breakdown if b["lossless"])
    lib_total = sum(b["tracks"] for b in breakdown)
    status["library_quality"] = breakdown
    status["library_lossless_tracks"] = lib_lossless
    status["library_lossless_pct"] = (round(100 * lib_lossless / lib_total, 1)
                                      if lib_total else None)
    status["upgrades"] = state.upgrade_counts()
    status["upgrade_candidates"] = lib_total - lib_lossless
    # The quality breakdown only covers files probed so far. Say so, rather than
    # letting a partial scan read as a complete picture of the library.
    status["scan_unprobed"] = int(state.get_meta("unprobed_files", "0") or 0)
    status["scan_complete"] = status["scan_unprobed"] == 0
    status["new_releases"] = [
        {"artist": a, "album": b, "released": rel, "found_at": f}
        for (a, b, rel, f) in state.recent_new_releases(10)
    ]
    try:
        status["new_release_watch"] = state.new_release_watch_stats(
            top_n=cfg.get("NEW_RELEASE_TOP_ARTISTS", 500),
            sync_hours=cfg.get("NEW_RELEASE_SYNC_HOURS", 24),
            exclude_favorites=bool(cfg.get("FAVORITES_ENABLED", True)),
        )
    except Exception as e:
        log(f"new release watch status failed (non-fatal): {e}")
        status["new_release_watch"] = None
    status["favorites"] = [
        {"artist": display or name, "source": src, "mbid": mbid,
         "albums_total": at or 0, "albums_have": ah or 0,
         "last_sync": ls or 0, "note": note}
        for (name, display, mbid, src, _add, ls, _k, at, ah, note) in state.favorites()
    ]
    status["staging_free_gb"] = round(free_gb(STAGING_PATH), 1)
    try:
        status["decode_gate"] = decode_gate_status(state, cfg)
    except Exception as e:
        log(f"decode gate status failed (non-fatal): {e}")
        status["decode_gate"] = None
    status["paused"] = bool(cfg["PAUSED"])
    if scan_root is not None:
        status["scan_root"] = str(scan_root)
    if scan_duration_sec is not None:
        status["scan_duration_sec"] = round(float(scan_duration_sec), 2)
    reason = stop_reason(cfg, lib_bytes, track_count, free)
    status["stop_reason"] = reason.message if reason else None
    payload = json.dumps(status, indent=2)
    with _STATUS_WRITE_LOCK:
        try:
            tmp = STATUS_JSON.with_name(STATUS_JSON.name + ".tmp")
            tmp.write_text(payload)
            os.replace(tmp, STATUS_JSON)
        except OSError:
            pass
    log("STATUS " + json.dumps({k: status[k] for k in
        ("library_tb", "tracks", "pct_to_target", "cluster_free_gb",
         "queued_flac", "queued_alac", "queued_lossy", "lossless_pct", "pending")}))


STOP_FREE_SPACE = "free_space"
STOP_TARGET_TB = "target_tb"
STOP_TARGET_TRACKS = "target_tracks"
CAP_STOP_KINDS = frozenset({STOP_TARGET_TB, STOP_TARGET_TRACKS})


class StopReason(collections.namedtuple("StopReason", ["kind", "message"])):
    """Structured stop reason so main() can distinguish soft library caps from
    hard safety floors without string matching."""

    @property
    def is_cap(self):
        return self.kind in CAP_STOP_KINDS

    def __str__(self):
        return self.message


def stop_reason(cfg, lib_bytes, track_count, free):
    # Check the hard free-space floor first so a low-disk condition is never
    # masked by a soft size/track cap when FAVORITES_BYPASS_CAP is enabled.
    if free < cfg["MIN_FREE_GB"]:
        return StopReason(STOP_FREE_SPACE,
                          f"free space floor: {free:.0f} GB < {cfg['MIN_FREE_GB']} GB")
    if lib_bytes / (1024 ** 4) >= cfg["TARGET_TB"]:
        return StopReason(STOP_TARGET_TB,
                          f"reached target size {cfg['TARGET_TB']} TB")
    if track_count >= cfg["TARGET_TRACKS"]:
        return StopReason(STOP_TARGET_TRACKS,
                          f"reached target tracks {cfg['TARGET_TRACKS']}")
    return None


def favorite_min_priority(cfg):
    """Minimum candidate priority treated as a favorite during cap bypass."""
    return max(1, int(cfg.get("FAVORITE_PRIORITY", 100)))


def should_bypass_cap_for_favorites(reason, cfg):
    """True when discovery is stopped by a soft cap (TARGET_TB / TARGET_TRACKS)
    and favorites should still sync and download."""
    return bool(
        reason is not None
        and reason.is_cap
        and cfg.get("FAVORITES_ENABLED", True)
        and cfg.get("FAVORITES_BYPASS_CAP", True)
    )


BACKEND_BACKOFF_INITIAL_SEC = 120
BACKEND_BACKOFF_MAX_SEC = 900  # 15 min
ERROR_RETRY_CHECK_INTERVAL_SEC = 6 * 3600


def backend_backoff_seconds(consecutive_failures,
                            initial=BACKEND_BACKOFF_INITIAL_SEC,
                            maximum=BACKEND_BACKOFF_MAX_SEC):
    """Exponential backoff delay (seconds) across consecutive backend-down waves."""
    if consecutive_failures <= 0:
        return 0
    return min(maximum, initial * (2 ** min(consecutive_failures - 1, 30)))


def maybe_requeue_errors(state, cfg, now=None):
    """Periodically flip old errored candidates back to pending.

    Runs at most once every 6 hours (tracked in meta['last_error_requeue']).
    Disabled when ERROR_RETRY_DAYS == 0.
    """
    retry_days = int(cfg.get("ERROR_RETRY_DAYS", 7) or 0)
    if retry_days <= 0:
        return 0
    now = int(time.time() if now is None else now)
    raw_last = state.get_meta("last_error_requeue")
    if raw_last is not None and now - int(raw_last) < ERROR_RETRY_CHECK_INTERVAL_SEC:
        return 0
    max_attempts = int(cfg.get("ERROR_MAX_ATTEMPTS", 3) or 0)
    cutoff = now - retry_days * 86400
    count = state.requeue_errors(cutoff, max_attempts)
    state.set_meta("last_error_requeue", now)
    log(f"Error retry: requeued {count} errored candidate(s) "
        f"(older than {retry_days}d, max_attempts={max_attempts})")
    return count


def main():
    log("Librarian starting")
    secrets = load_secrets()
    state = State()
    lastfm = LastFM(secrets["LASTFM_API_KEY"])
    mb = MusicBrainz(load_config_raw().get(
        "MB_USER_AGENT", "music-librarian/2.0 ( https://github.com/asherflynt/music-librarian )"))
    sb = Soulbeet(secrets)

    if not secrets.get("TAUTULLI_API_KEY"):
        log("No TAUTULLI_API_KEY set — Plex/Plexamp plays will not be counted "
            "(optional; Navidrome + YouTube Music still work)")

    busy = threading.Event()
    backend_down = threading.Event()
    down_waves = 0
    web.serve(WEB_PORT, {
        "state": state, "cfg": load_config, "log_ring": LOG_RING,
        "status_json": STATUS_JSON, "config_env": CONFIG_ENV,
        "exclude_txt": EXCLUDE_TXT, "favorites_txt": FAVORITES_TXT,
    })
    threading.Thread(target=upgrade_loop, daemon=True,
                     args=(state, sb, load_config, busy.is_set)).start()

    scanner = LibraryScanner(state, get_cfg=load_config)

    while True:
        cfg = load_config()
        exclude_set = load_exclude_set()

        # Periodic library measurement runs on a single background thread so
        # walking the library never starves downloads. Triggered before pause/
        # stop checks so status.json stays fresh even while paused or idling.
        scanner.maybe_start(cfg)
        lib_bytes, track_count, _ = scanner.snapshot()

        # Learn from the decode gate every iteration (cheap: one stat() when idle),
        # even while paused, so a rejected album is re-queued from another peer.
        try:
            ingest_decode_rejects(state, cfg)
            repend_checker_unavailable(state, cfg)
            maybe_prune_downloads(state)
        except Exception as e:
            log(f"decode gate ingest error (non-fatal): {e}")

        if cfg["PAUSED"]:
            busy.clear()
            log("PAUSED via config.env; sleeping 60s")
            time.sleep(60)
            continue

        # staging overflow guard: pause new downloads if the NVMe staging pool is
        # getting full, so beets/mover can drain it (protects the cache SSD).
        staging_free = free_gb(STAGING_PATH)
        if staging_free < cfg["STAGING_MIN_FREE_GB"]:
            busy.clear()
            log(f"Staging pool low ({staging_free:.0f} GB < {cfg['STAGING_MIN_FREE_GB']} GB); "
                f"pausing downloads 2 min to let imports drain")
            time.sleep(120)
            continue

        free = free_gb(FREE_SPACE_PATH)
        reason = stop_reason(cfg, lib_bytes, track_count, free)
        favorites_only = False
        if reason:
            if should_bypass_cap_for_favorites(reason, cfg):
                favorites_only = True
            else:
                busy.clear()
                log(f"STOP CONDITION: {reason}. Idling 5 min (raise targets in the UI or "
                    f"config.env to resume).")
                time.sleep(300)
                scanner.reset_schedule()  # force re-measure after idle
                continue

        if not favorites_only:
            # refresh taste signal periodically
            last_refresh = int(state.get_meta("last_taste_refresh", "0"))
            if time.time() - last_refresh > cfg["TASTE_REFRESH_MIN"] * 60:
                refresh_taste(state, secrets, lastfm, exclude_set, cfg)

        # sync favorited artists' discographies + notice new releases
        last_fav = int(state.get_meta("last_favorites_sync", "0"))
        if cfg["FAVORITES_ENABLED"] and \
                time.time() - last_fav > cfg["FAVORITE_SYNC_HOURS"] * 3600:
            favorites_sync(state, secrets, mb, cfg, exclude_set)

        # incrementally check top played artists for recent releases (batched)
        if not favorites_only:
            try:
                new_releases_sync_step(state, mb, lastfm, cfg, exclude_set)
            except Exception as e:
                log(f"new-release watch error (non-fatal): {e}")

        # periodically re-queue old errored candidates (respects priority)
        maybe_requeue_errors(state, cfg)

        if not favorites_only:
            # expand a few top-weighted artists into album candidates
            for artist, weight, depth in state.unexpanded_artists(limit=5):
                expand_one_artist(state, lastfm, artist, weight, cfg, exclude_set, depth=depth)

        # process a wave of pending candidates concurrently
        if favorites_only:
            batch = state.pending_candidates(
                limit=cfg["CONCURRENCY"] * 3,
                min_priority=favorite_min_priority(cfg),
            )
            if not batch:
                busy.clear()
                log(f"STOP CONDITION: {reason} (favorites synced, none pending). "
                    f"Idling 5 min (raise targets in the UI or config.env to resume discovery).")
                time.sleep(300)
                scanner.reset_schedule()  # force re-measure after idle
                continue
            log(f"Cap reached ({reason}); processing {len(batch)} favorite candidate(s)")
        else:
            batch = state.pending_candidates(limit=cfg["CONCURRENCY"] * 3)
            if not batch:
                busy.clear()
                log("No pending candidates; refreshing taste and expanding more")
                refresh_taste(state, secrets, lastfm, exclude_set, cfg)
                time.sleep(10)
                continue

        backend_down.clear()
        busy.set()      # tells the upgrade loop to stay out of the way
        try:
            with ThreadPoolExecutor(max_workers=cfg["CONCURRENCY"]) as ex:
                results = list(ex.map(
                    lambda r: process_candidate(state, sb, secrets, r,
                                                backend_down=backend_down, cfg=cfg),
                    batch,
                ))
        finally:
            busy.clear()

        if "backend_down" in results:
            down_waves += 1
            delay = backend_backoff_seconds(down_waves)
            log(f"Soulbeet backend unavailable (wave {down_waves}); "
                f"backing off {delay}s before retrying")
            time.sleep(delay)
            continue
        down_waves = 0

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted; state is persisted, safe to restart.")
