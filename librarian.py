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
import sys
import threading
import time
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
        "EXPLORE_RATIO": num("EXPLORE_RATIO", 0.30, float),
        "MIN_FREE_GB": num("MIN_FREE_GB", 500, float),
        "STAGING_MIN_FREE_GB": num("STAGING_MIN_FREE_GB", 80, float),
        "CONCURRENCY": max(1, num("CONCURRENCY", 3, int)),
        "TASTE_REFRESH_MIN": num("TASTE_REFRESH_MIN", 360, int),   # re-pull play counts every 6h
        "MEASURE_EVERY_SEC": num("MEASURE_EVERY_SEC", 300, int),
        "SCAN_MAX_PROBES_PER_PASS": max(1, num("SCAN_MAX_PROBES_PER_PASS", 250, int)),
        "PAUSED": flag("PAUSED"),
        # taste weighting
        "TASTE_HALF_LIFE_DAYS": max(1.0, num("TASTE_HALF_LIFE_DAYS", 365, float)),
        "WEIGHT_NAVIDROME": num("WEIGHT_NAVIDROME", 3.0, float),
        "WEIGHT_PLEX": num("WEIGHT_PLEX", 3.0, float),
        "WEIGHT_YTMUSIC": num("WEIGHT_YTMUSIC", 1.0, float),
        # favorites (MusicBrainz full-discography + new releases)
        "FAVORITES_ENABLED": flag("FAVORITES_ENABLED", "1"),
        "FAVORITE_SYNC_HOURS": max(1, num("FAVORITE_SYNC_HOURS", 24, int)),
        "FAVORITE_PRIORITY": num("FAVORITE_PRIORITY", 100, int),
        "FAVORITE_INCLUDE_EP": flag("FAVORITE_INCLUDE_EP", "1"),
        "FAVORITE_INCLUDE_SINGLES": flag("FAVORITE_INCLUDE_SINGLES", "0"),
        "FAVORITE_INCLUDE_LIVE": flag("FAVORITE_INCLUDE_LIVE", "0"),
        "FAVORITE_INCLUDE_COMPILATIONS": flag("FAVORITE_INCLUDE_COMPILATIONS", "0"),
        "FAVORITE_INCLUDE_REMIX": flag("FAVORITE_INCLUDE_REMIX", "0"),
        # scheduled lossy -> FLAC upgrades
        "UPGRADE_ENABLED": flag("UPGRADE_ENABLED", "0"),
        "UPGRADE_HOUR": min(23, max(0, num("UPGRADE_HOUR", 3, int))),
        "UPGRADE_MAX_PER_RUN": max(1, num("UPGRADE_MAX_PER_RUN", 10, int)),
        "UPGRADE_RECHECK_DAYS": max(1, num("UPGRADE_RECHECK_DAYS", 30, int)),
        "UPGRADE_ONLY_WHEN_IDLE": flag("UPGRADE_ONLY_WHEN_IDLE", "1"),
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
            weight REAL DEFAULT 0,      -- taste weight (play count derived)
            expanded_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS candidates (
            key TEXT PRIMARY KEY,       -- normalized 'artist|album' or 'artist|track'
            artist TEXT, album TEXT, kind TEXT,
            status TEXT DEFAULT 'pending',   -- pending|queued_flac|queued_alac|queued_lossy|no_source|low_quality_only|no_meta|error|exists
            fmt TEXT,                   -- actual format chosen: flac|alac|mp3|m4a|aac
            kbps INTEGER,               -- measured bitrate of what was queued
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

    for table, name, decl in (
        ("candidates", "fmt", "TEXT"),
        ("candidates", "kbps", "INTEGER"),
        # Favorites must outrank the exploration crawl, or a full discography would
        # queue behind however many thousand candidates the 100k-track fill has
        # already generated.
        ("candidates", "priority", "INTEGER DEFAULT 0"),
    ):
        if name not in cols(table):
            log(f"Migrating state.db: adding {table}.{name}")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_pending "
                 "ON candidates(status, priority DESC, updated_at)")
    conn.commit()


def norm(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


class State:
    def __init__(self):
        self.conn = db_connect()
        self.lock = threading.Lock()

    def upsert_artist(self, name, weight=0.0):
        n = norm(name)
        if not n:
            return
        with self.lock:
            self.conn.execute(
                "INSERT INTO artists_seen(name, display, weight) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET weight=MAX(weight, excluded.weight), "
                "display=COALESCE(display, excluded.display)",
                (n, name, weight),
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
                "SELECT display, weight FROM artists_seen WHERE expanded_at=0 "
                "ORDER BY weight DESC, name ASC LIMIT ?", (limit,)
            ).fetchall()
        return rows

    def add_candidate(self, artist, album, kind, priority=0):
        """Returns True only if this is a brand-new candidate.

        Written as an explicit SELECT-then-write rather than INSERT..ON CONFLICT DO
        UPDATE because rowcount is 1 for an upsert's update path as well as its insert
        path -- callers count on the return value meaning "new", and the old
        INSERT OR IGNORE gave that for free.
        """
        key = f"{norm(artist)}|{norm(album or '')}|{kind}"
        with self.lock:
            row = self.conn.execute("SELECT priority FROM candidates WHERE key=?",
                                    (key,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO candidates(key, artist, album, kind, priority, updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (key, artist, album, kind, priority, int(time.time())))
                self.conn.commit()
                return True
            # Already known: an album queued by exploration that later turns out to
            # belong to a favorite gets promoted rather than silently left at the back.
            if priority > (row[0] or 0):
                self.conn.execute("UPDATE candidates SET priority=? WHERE key=?",
                                  (priority, key))
                self.conn.commit()
            return False

    def pending_candidates(self, limit):
        with self.lock:
            rows = self.conn.execute(
                "SELECT key, artist, album, kind FROM candidates WHERE status='pending' "
                "ORDER BY priority DESC, updated_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return rows

    def top_artists(self, limit=100):
        with self.lock:
            return self.conn.execute(
                "SELECT display, weight, expanded_at FROM artists_seen "
                "ORDER BY weight DESC, name ASC LIMIT ?", (limit,)).fetchall()

    def set_status(self, key, status, fmt=None, kbps=None):
        with self.lock:
            self.conn.execute(
                "UPDATE candidates SET status=?, fmt=?, kbps=?, updated_at=? WHERE key=?",
                (status, fmt, kbps, int(time.time()), key))
            self.conn.commit()

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


# ----------------------------------------------------------------------------
# Soulbeet client (auth + FLAC/lossy pipeline)
# ----------------------------------------------------------------------------
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

    def _post(self, path, body, timeout=60, _retry=True):
        r = self.s.post(f"{SOULBEET_URL}{path}", json=body, timeout=timeout)
        if r.status_code in (401, 403) and _retry:
            log("Soulbeet: session expired, re-authenticating")
            self.login()
            return self._post(path, body, timeout, _retry=False)
        r.raise_for_status()
        return r.json()

    def search_album(self, artist, query):
        d = self._post("/api/metadata/search/album",
                       {"input": {"query": query, "artist": artist, "provider": None}})
        return [x for x in d.get("results", []) if x.get("kind") == "Album"]

    def search_track(self, artist, query):
        d = self._post("/api/metadata/search/track",
                       {"input": {"query": query, "artist": artist, "provider": None}})
        return [x for x in d.get("results", []) if x.get("kind") == "Track"]

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

    def queue(self, items, target_folder=None):
        return self._post("/api/downloads/queue",
                          {"req": {"items": items,
                                   "target_folder": target_folder or TARGET_FOLDER,
                                   "backend": None}})


def est_kbps(item):
    """Effective bitrate from size/duration. The backend only tells us the file
    EXTENSION, not the codec -- and .m4a is used by both ALAC (lossless) and AAC
    (lossy). Bitrate is what actually separates them: ALAC lands ~600-900 kbps,
    AAC caps around 320."""
    size = item.get("size") or 0
    dur = item.get("duration") or 0
    if size > 0 and dur > 0:
        return (size * 8) / dur / 1000
    return 0.0


def group_kbps(g):
    vals = [k for k in (est_kbps(it) for it in g.get("items", [])) if k > 0]
    return sum(vals) / len(vals) if vals else 0.0


def pick_by_quality_ladder(groups):
    """Return (status, items, info). Quality ladder, best first:

      1. FLAC                       -> lossless
      2. ALAC (.m4a at >= ALAC_MIN_KBPS) -> also lossless, so ranked with FLAC
      3. high-quality lossy (mp3/aac/.m4a-aac) at >= LOSSY_MIN_SCORE
      else: low_quality_only / no_source

    'info' carries the chosen format + measured bitrate so callers can log and
    record exactly what a fallback landed on.
    """
    if not groups:
        return "no_source", [], None

    # --- tier 1: FLAC ---
    flac = [g for g in groups if g.get("quality") == "flac"]
    if flac:
        flac.sort(key=lambda g: (g.get("score", 0), g.get("item_count", 0)), reverse=True)
        items = [it for it in flac[0].get("items", []) if it.get("quality") == "flac"]
        if items:
            return "queued_flac", items, {"fmt": "flac", "kbps": round(group_kbps(flac[0]))}

    # --- tier 2: ALAC (.m4a at lossless bitrate) -- lossless, so preferred over any lossy ---
    alac = [g for g in groups
            if g.get("quality") == "m4a" and group_kbps(g) >= ALAC_MIN_KBPS]
    if alac:
        alac.sort(key=lambda g: (g.get("score", 0), g.get("item_count", 0)), reverse=True)
        items = [it for it in alac[0].get("items", [])
                 if it.get("quality") == "m4a" and est_kbps(it) >= ALAC_MIN_KBPS]
        if items:
            return "queued_alac", items, {"fmt": "alac", "kbps": round(group_kbps(alac[0]))}

    # --- tier 3: high-quality lossy ---
    lossy = [g for g in groups if g.get("quality") in LOSSY_EXTS]
    if lossy:
        lossy.sort(key=lambda g: g.get("score", 0), reverse=True)
        for g in lossy:
            items = [it for it in g.get("items", [])
                     if it.get("quality") in LOSSY_EXTS
                     and it.get("quality_score", 0) >= LOSSY_MIN_SCORE]
            # prefer explicit 320/V0 in filename ordering
            items.sort(key=lambda it: (
                1 if re.search(r"320|v0", (it.get("title", "") or "").lower()) else 0,
                it.get("quality_score", 0)), reverse=True)
            if items:
                return "queued_lossy", items, {"fmt": g.get("quality"),
                                               "kbps": round(group_kbps(g))}
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

    def resolve_mbid(self, artist):
        """Artist name -> MBID. Returns None rather than guessing on a weak match."""
        q = artist.replace('"', "")
        data = self._get("artist", "artists", query=f'artist:"{q}"', limit=5)
        if not data:
            return None, "lookup failed"
        for a in data.get("artists", []):
            if int(a.get("score", 0)) >= 90:
                return a["id"], a.get("name")
        if data.get("artists"):
            best = data["artists"][0]
            return None, (f"no confident match (best: {best.get('name')} "
                          f"@ score {best.get('score')})")
        return None, "not found in MusicBrainz"

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
        mbid, note = mb.resolve_mbid(display)
        if not mbid:
            log(f"  favorite '{display}': {note}")
            state.update_favorite(name, last_sync=int(time.time()), note=note)
            return 0, 0
        state.update_favorite(name, mbid=mbid, display=note or display, note=None)

    rgs = mb.release_groups(mbid)
    if rgs is None:
        log(f"  favorite '{display}': MusicBrainz lookup failed; will retry next sync")
        return 0, 0

    keep = [r for r in rgs if keep_release_group(r, cfg)]
    known = set(known_rgs or [])
    added = new_count = 0
    for rg in keep:
        title, rg_id = rg.get("title"), rg.get("id")
        if not title:
            continue
        if state.add_candidate(display, title, "album", priority=cfg["FAVORITE_PRIORITY"]):
            added += 1
        # New release = a release-group we've never seen AND dated after you
        # favorited the artist. Without the date check, the very first sync would
        # announce the entire back catalogue as "new".
        released = rg.get("first-release-date") or ""
        if rg_id not in known and known_rgs is not None:
            if _parse_ts(released) > (added_at or 0):
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


def measure_library(state=None, max_probes=None):
    """Walk the library once for size, track count, and per-file quality.

    Incremental: a file whose (size, mtime) is unchanged is not re-opened, so the
    steady-state cost is a stat() per file rather than a tag parse. That matters --
    this runs every MEASURE_EVERY_SEC against a library headed for 100k files.

    `max_probes` bounds how many NEW/CHANGED files get opened per pass, and exists
    for a specific reason: the library is reached through Unraid's /mnt/user FUSE
    layer (shfs), which is a single-threaded chokepoint shared with Plex, slskd,
    beets and everything else on the box. Walking it to stat() files is cheap and has
    always been fine, but *opening* every file to read tags is not -- doing that to
    the whole library in one tight loop is believed to be what wedged the server on
    2026-07-17 (every process touching /mnt/user piled into uninterruptible D state
    until a hard reboot). Probing is therefore rate-limited and simply resumes next
    pass: the walk stays complete and correct, only the expensive part is spread out.
    """
    total_bytes = 0
    track_count = 0
    known = state.known_files() if state else {}
    seen = set()
    pending = []
    reprobed = 0
    unprobed = 0

    for root, dirs, files in os.walk(MUSIC_PATH):
        # The upgrade staging dir lives inside the library tree (see UPGRADE_FOLDER).
        # It must not be counted: staged files are unverified, would double-count
        # albums we already own, and -- since they're FLAC awaiting a swap -- would
        # skew the lossless stats toward a state that isn't real yet.
        if UPGRADE_DIRNAME in dirs:
            dirs.remove(UPGRADE_DIRNAME)
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            full = os.path.join(root, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            track_count += 1
            total_bytes += st.st_size
            if not state:
                continue
            seen.add(full)
            prev = known.get(full)
            if prev and prev[0] == st.st_size and prev[1] == int(st.st_mtime):
                continue
            # Budget exhausted: leave it for the next pass rather than hammering
            # shfs. Size/track totals above are already counted, so only the quality
            # breakdown lags -- and it catches up on its own.
            if max_probes is not None and reprobed >= max_probes:
                unprobed += 1
                continue
            probed = probe_file(full)
            if not probed:
                continue
            codec, lossless, kbps = probed
            artist, album = _tags_of(full, root)
            pending.append((full, root, artist, album, codec, int(lossless), kbps,
                            st.st_size, int(st.st_mtime), int(time.time())))
            reprobed += 1
            if len(pending) >= 500:
                state.put_files(pending)
                pending = []

    if state:
        if pending:
            state.put_files(pending)
        gone = set(known) - seen
        if gone:
            state.drop_files(gone)   # files removed/renamed shouldn't linger in stats
        if reprobed or gone or unprobed:
            msg = f"Library scan: {reprobed} new/changed file(s) probed"
            if gone:
                msg += f", {len(gone)} removed"
            if unprobed:
                msg += (f", {unprobed} awaiting probe (rate-limited to "
                        f"{max_probes}/pass to protect the /mnt/user FUSE layer)")
            log(msg)
        state.set_meta("unprobed_files", unprobed)
    return total_bytes, track_count


def free_gb(path):
    try:
        st = os.statvfs(path)
        return (st.f_bavail * st.f_frsize) / (1024 ** 3)
    except OSError:
        return float("inf")


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
    weights = {}
    user, pw = secrets["SOULBEET_USER"], secrets["SOULBEET_PASS"]
    for name, w in navidrome_play_weights(user, pw, hl).items():
        weights[name] = weights.get(name, 0) + w * cfg["WEIGHT_NAVIDROME"]
    for name, w in tautulli_play_weights(secrets.get("TAUTULLI_API_KEY"), hl).items():
        weights[name] = weights.get(name, 0) + w * cfg["WEIGHT_PLEX"]
    for name, w in youtube_music_weights(hl).items():
        weights[name] = weights.get(name, 0) + w * cfg["WEIGHT_YTMUSIC"]
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
        state.upsert_artist(name, weight=w)
        added += 1
    log(f"Taste refresh: {added} weighted artists seeded, {skipped} blocklisted "
        f"(top play counts drive expansion)")
    state.set_meta("last_taste_refresh", int(time.time()))


def expand_one_artist(state, lastfm, artist, weight, cfg, exclude_set):
    """Generate album candidates for one artist: exploitation + a share of exploration.

    This is where the kids filter actually bites: seeding is cheap and unfiltered
    (beyond the name blocklist), but nothing becomes a download candidate without
    passing the tag check here first.
    """
    if is_kids_artist(state, lastfm, artist, exclude_set):
        log(f"  excluded (kids): {artist}")
        state.mark_expanded(artist)
        return 0
    new = 0
    # exploitation: this artist's own top albums + similar artists' top albums
    for alb in lastfm.top_albums(artist, limit=8):
        if state.add_candidate(artist, alb, "album"):
            new += 1
    for sim in lastfm.similar(artist, limit=25):
        # cheap name check only; the tag check happens when sim is itself expanded
        if not is_excluded_by_name(sim, exclude_set):
            state.upsert_artist(sim, weight=weight * 0.5)  # propagate taste weight

    # exploration: from this artist's primary tags, pull artists we don't own
    if cfg["EXPLORE_RATIO"] > 0:
        for tag in (state.get_tags(artist) or [])[:2]:
            if tag in KIDS_TAGS:
                continue
            for exp_artist in lastfm.tag_top_artists(tag, limit=int(15 * cfg["EXPLORE_RATIO"]) + 3):
                if not is_excluded_by_name(exp_artist, exclude_set):
                    state.upsert_artist(exp_artist, weight=weight * 0.2)
    state.mark_expanded(artist)
    return new


def process_candidate(state, sb, secrets, row):
    key, artist, album, kind = row
    try:
        # skip if already in the Navidrome library
        if album and navidrome_album_exists(secrets["SOULBEET_USER"], secrets["SOULBEET_PASS"], artist, album):
            state.set_status(key, "exists")
            return "exists"

        album_obj, tracks = None, []
        results = sb.search_album(artist, album or artist)
        if results:
            album_obj = results[0]
        else:
            tr = sb.search_track(artist, album or artist)
            if not tr:
                state.set_status(key, "no_meta")
                return "no_meta"
            tracks = [tr[0]]

        sid = sb.start_search(album_obj, tracks)
        res = sb.poll(sid)
        status, items, info = pick_by_quality_ladder(res.get("groups", []))
        if items:
            sb.queue(items)

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
    except Exception as e:
        log(f"  candidate error [{artist} - {album}]: {e}")
        state.set_status(key, "error")
        return "error"


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
    state.upsert_upgrade(key, artist=artist, album=album, dir=cand["dir"],
                         cur_codec=cand["codec"], cur_kbps=cand["kbps"],
                         cur_tracks=cand["tracks"], last_checked=int(time.time()),
                         attempts=(cand.get("attempts", 0) + 1))
    try:
        results = sb.search_album(artist, album)
        if not results:
            state.upsert_upgrade(key, status="no_flac", note="no album metadata match")
            return "no_flac"
        sid = sb.start_search(results[0], [])
        res = sb.poll(sid)
        status, items, info = pick_by_quality_ladder(res.get("groups", []))
        if status != "queued_flac" or not items:
            state.upsert_upgrade(key, status="no_flac",
                                 note=f"best available was {status}")
            return "no_flac"
        # Stage it, never straight into the library: nothing replaces a file you
        # already have until the swap script has verified it end to end.
        sb.queue(items, target_folder=UPGRADE_FOLDER)
        state.upsert_upgrade(key, status="queued",
                             note=f"flac @ ~{(info or {}).get('kbps', 0)} kbps staged")
        log(f"  UPGRADE [{artist} - {album}]: {cand['codec']} @ ~{cand['kbps']} kbps "
            f"-> FLAC staged for verification")
        return "queued"
    except Exception as e:
        state.upsert_upgrade(key, status="failed", note=str(e)[:200])
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
        counts[upgrade_one(state, sb, c, cfg)] += 1
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


def write_status(state, cfg, lib_bytes, track_count, free):
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
    status["favorites"] = [
        {"artist": display or name, "source": src, "mbid": mbid,
         "albums_total": at or 0, "albums_have": ah or 0,
         "last_sync": ls or 0, "note": note}
        for (name, display, mbid, src, _add, ls, _k, at, ah, note) in state.favorites()
    ]
    status["staging_free_gb"] = round(free_gb(STAGING_PATH), 1)
    status["paused"] = bool(cfg["PAUSED"])
    status["stop_reason"] = stop_reason(cfg, lib_bytes, track_count, free)
    try:
        STATUS_JSON.write_text(json.dumps(status, indent=2))
    except OSError:
        pass
    log("STATUS " + json.dumps({k: status[k] for k in
        ("library_tb", "tracks", "pct_to_target", "cluster_free_gb",
         "queued_flac", "queued_alac", "queued_lossy", "lossless_pct", "pending")}))


def stop_reason(cfg, lib_bytes, track_count, free):
    if lib_bytes / (1024 ** 4) >= cfg["TARGET_TB"]:
        return f"reached target size {cfg['TARGET_TB']} TB"
    if track_count >= cfg["TARGET_TRACKS"]:
        return f"reached target tracks {cfg['TARGET_TRACKS']}"
    if free < cfg["MIN_FREE_GB"]:
        return f"free space floor: {free:.0f} GB < {cfg['MIN_FREE_GB']} GB"
    return None


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
    web.serve(WEB_PORT, {
        "state": state, "cfg": load_config, "log_ring": LOG_RING,
        "status_json": STATUS_JSON, "config_env": CONFIG_ENV,
        "exclude_txt": EXCLUDE_TXT, "favorites_txt": FAVORITES_TXT,
    })
    threading.Thread(target=upgrade_loop, daemon=True,
                     args=(state, sb, load_config, busy.is_set)).start()

    last_measure = 0
    lib_bytes, track_count, free = 0, 0, float("inf")

    while True:
        cfg = load_config()
        exclude_set = load_exclude_set()

        if cfg["PAUSED"]:
            busy.clear()
            log("PAUSED via config.env; sleeping 60s")
            time.sleep(60)
            continue

        # staging overflow guard: pause new downloads if the NVMe staging pool is
        # getting full, so beets/mover can drain it (protects the cache SSD).
        staging_free = free_gb(STAGING_PATH)
        if staging_free < cfg["STAGING_MIN_FREE_GB"]:
            log(f"Staging pool low ({staging_free:.0f} GB < {cfg['STAGING_MIN_FREE_GB']} GB); "
                f"pausing downloads 2 min to let imports drain")
            time.sleep(120)
            continue

        # periodic library measurement (throttled — walking 100k files is not free)
        if time.time() - last_measure > cfg["MEASURE_EVERY_SEC"]:
            lib_bytes, track_count = measure_library(
                state, max_probes=cfg["SCAN_MAX_PROBES_PER_PASS"])
            free = free_gb(FREE_SPACE_PATH)
            last_measure = time.time()
            write_status(state, cfg, lib_bytes, track_count, free)

        reason = stop_reason(cfg, lib_bytes, track_count, free)
        if reason:
            busy.clear()
            log(f"STOP CONDITION: {reason}. Idling 5 min (raise targets in the UI or "
                f"config.env to resume).")
            time.sleep(300)
            last_measure = 0  # force re-measure after idle
            continue

        # refresh taste signal periodically
        last_refresh = int(state.get_meta("last_taste_refresh", "0"))
        if time.time() - last_refresh > cfg["TASTE_REFRESH_MIN"] * 60:
            refresh_taste(state, secrets, lastfm, exclude_set, cfg)

        # sync favorited artists' discographies + notice new releases
        last_fav = int(state.get_meta("last_favorites_sync", "0"))
        if cfg["FAVORITES_ENABLED"] and \
                time.time() - last_fav > cfg["FAVORITE_SYNC_HOURS"] * 3600:
            favorites_sync(state, secrets, mb, cfg, exclude_set)

        # expand a few top-weighted artists into album candidates
        for artist, weight in state.unexpanded_artists(limit=5):
            expand_one_artist(state, lastfm, artist, weight, cfg, exclude_set)

        # process a wave of pending candidates concurrently
        batch = state.pending_candidates(limit=cfg["CONCURRENCY"] * 3)
        if not batch:
            busy.clear()
            log("No pending candidates; refreshing taste and expanding more")
            refresh_taste(state, secrets, lastfm, exclude_set, cfg)
            time.sleep(10)
            continue

        busy.set()      # tells the upgrade loop to stay out of the way
        try:
            with ThreadPoolExecutor(max_workers=cfg["CONCURRENCY"]) as ex:
                list(ex.map(lambda r: process_candidate(state, sb, secrets, r), batch))
        finally:
            busy.clear()

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted; state is persisted, safe to restart.")
