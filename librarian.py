#!/usr/bin/env python3
"""
Music Librarian — self-hosted, taste-driven, resumable FLAC-preferred auto-fill.

Runs entirely on the Unraid box (no Claude/browser dependency). Seeds from what the
household actually plays (Navidrome play counts + YouTube Music Takeout history),
expands via Last.fm (taste-weighted exploitation + horizon exploration), and downloads
through the existing Soulbeet API using a FLAC-preferred / high-quality-lossy-fallback
ladder. State lives in SQLite so restarts resume exactly where they left off.
"""

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

# ----------------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("LIBRARIAN_DATA", "/data"))
CONFIG_DIR = Path(os.environ.get("LIBRARIAN_CONFIG", "/config"))
SECRETS_ENV = CONFIG_DIR / "secrets.env"
CONFIG_ENV = CONFIG_DIR / "config.env"
EXCLUDE_TXT = CONFIG_DIR / "exclude.txt"
TAKEOUT_DIR = CONFIG_DIR / "takeout"
STATE_DB = DATA_DIR / "state.db"
STATUS_JSON = DATA_DIR / "status.json"

SOULBEET_URL = os.environ.get("SOULBEET_URL", "http://soulbeet:9765").rstrip("/")
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
MUSIC_PATH = Path(os.environ.get("MUSIC_PATH", "/music"))          # library, for size/count
FREE_SPACE_PATH = os.environ.get("FREE_SPACE_PATH", "/music")       # cluster pool: library-size floor
STAGING_PATH = os.environ.get("STAGING_PATH", "/staging")           # NVMe staging: overflow floor
TARGET_FOLDER = os.environ.get("TARGET_FOLDER", "/music")           # beets destination in Soulbeet

LOSSY_EXTS = {"mp3", "m4a", "aac"}
LOSSY_MIN_SCORE = float(os.environ.get("LOSSY_MIN_SCORE", "0.55"))  # ~clean 320 kbps MP3
# .m4a is ambiguous: ALAC (lossless) and AAC (lossy) share the extension, and the
# backend only reports the extension. Bitrate is the discriminator -- ALAC runs
# ~600-900 kbps, AAC caps ~320. Anything at/above this is treated as ALAC.
ALAC_MIN_KBPS = float(os.environ.get("ALAC_MIN_KBPS", "500"))
KIDS_TAGS = {"children's music", "childrens music", "children", "nursery",
             "nursery rhymes", "kids", "kids music", "lullaby", "lullabies"}
AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus"}

LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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


def load_config():
    c = _parse_env_file(CONFIG_ENV)

    def num(key, default, cast=float):
        try:
            return cast(c.get(key, default))
        except (TypeError, ValueError):
            return cast(default)

    return {
        "TARGET_TB": num("TARGET_TB", 3.0, float),
        "TARGET_TRACKS": num("TARGET_TRACKS", 100000, int),
        "EXPLORE_RATIO": num("EXPLORE_RATIO", 0.30, float),
        "MIN_FREE_GB": num("MIN_FREE_GB", 500, float),
        "STAGING_MIN_FREE_GB": num("STAGING_MIN_FREE_GB", 80, float),
        "CONCURRENCY": max(1, num("CONCURRENCY", 3, int)),
        "TASTE_REFRESH_MIN": num("TASTE_REFRESH_MIN", 360, int),   # re-pull play counts every 6h
        "MEASURE_EVERY_SEC": num("MEASURE_EVERY_SEC", 300, int),
        "PAUSED": c.get("PAUSED", "0") in ("1", "true", "yes"),
    }


# ----------------------------------------------------------------------------
# State DB
# ----------------------------------------------------------------------------
def db_connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB, timeout=30)
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
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    conn.commit()
    return conn


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

    def add_candidate(self, artist, album, kind):
        key = f"{norm(artist)}|{norm(album or '')}|{kind}"
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO candidates(key, artist, album, kind, updated_at) "
                "VALUES(?,?,?,?,?)", (key, artist, album, kind, int(time.time()))
            )
            self.conn.commit()
            return cur.rowcount > 0  # True if newly added

    def pending_candidates(self, limit):
        with self.lock:
            rows = self.conn.execute(
                "SELECT key, artist, album, kind FROM candidates WHERE status='pending' "
                "ORDER BY updated_at ASC LIMIT ?", (limit,)
            ).fetchall()
        return rows

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

    def get_meta(self, k, default=None):
        with self.lock:
            r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        with self.lock:
            self.conn.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
            self.conn.commit()


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

    def queue(self, items):
        return self._post("/api/downloads/queue",
                          {"req": {"items": items, "target_folder": TARGET_FOLDER, "backend": None}})


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


def navidrome_frequent_artists(user, pw):
    """Artist -> summed playCount, from most-frequently-played albums."""
    weights = {}
    try:
        resp = nav_get(user, pw, "getAlbumList2", {"type": "frequent", "size": 500})
        for al in resp.get("albumList2", {}).get("album", []):
            artist = al.get("artist")
            pc = al.get("playCount", 0) or 0
            if artist and pc > 0:
                weights[artist] = weights.get(artist, 0) + pc
    except Exception as e:
        log(f"Navidrome frequent-artists failed (non-fatal): {e}")
    return weights


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
# YouTube Music (Google Takeout) — external cold-start taste signal
# ----------------------------------------------------------------------------
def youtube_music_weights():
    """Parse Takeout watch-history JSON files; tally listen frequency by artist."""
    weights = {}
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
                    weights[artist] = weights.get(artist, 0) + 1
    if weights:
        log(f"YouTube Music: parsed {sum(weights.values())} listens across {len(weights)} artists")
    return weights


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
            log(f"Last.fm {method} failed: {e}")
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
def load_exclude_set():
    names = set()
    if EXCLUDE_TXT.exists():
        for line in EXCLUDE_TXT.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                names.add(norm(line))
    return names


def is_kids_artist(lastfm, artist, exclude_set, tag_cache):
    if norm(artist) in exclude_set:
        return True
    if artist in tag_cache:
        tags = tag_cache[artist]
    else:
        tags = lastfm.artist_tags(artist)
        tag_cache[artist] = tags
    return any(t in KIDS_TAGS for t in tags)


# ----------------------------------------------------------------------------
# Library measurement + free space
# ----------------------------------------------------------------------------
def measure_library():
    total_bytes = 0
    track_count = 0
    for root, _dirs, files in os.walk(MUSIC_PATH):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTS:
                track_count += 1
                try:
                    total_bytes += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
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
def refresh_taste(state, secrets, lastfm, exclude_set, tag_cache):
    """Pull play-count weights (Navidrome + YT Music), enqueue weighted seed artists."""
    weights = {}
    for name, w in navidrome_frequent_artists(secrets["SOULBEET_USER"], secrets["SOULBEET_PASS"]).items():
        weights[name] = weights.get(name, 0) + w * 3.0        # internal signal weighted higher
    for name, w in youtube_music_weights().items():
        weights[name] = weights.get(name, 0) + w * 1.0
    # ensure existing library artists are at least seeded (weight 1) so we always have a base
    for name in navidrome_all_artists(secrets["SOULBEET_USER"], secrets["SOULBEET_PASS"]):
        weights.setdefault(name, 1.0)

    added = 0
    for name, w in weights.items():
        if is_kids_artist(lastfm, name, exclude_set, tag_cache):
            continue
        state.upsert_artist(name, weight=w)
        added += 1
    log(f"Taste refresh: {added} weighted artists (top by play count drive expansion)")
    state.set_meta("last_taste_refresh", int(time.time()))


def expand_one_artist(state, lastfm, artist, weight, cfg, exclude_set, tag_cache):
    """Generate album candidates for one artist: exploitation + a share of exploration."""
    if is_kids_artist(lastfm, artist, exclude_set, tag_cache):
        state.mark_expanded(artist)
        return 0
    new = 0
    # exploitation: this artist's own top albums + similar artists' top albums
    for alb in lastfm.top_albums(artist, limit=8):
        if state.add_candidate(artist, alb, "album"):
            new += 1
    similars = lastfm.similar(artist, limit=25)
    for sim in similars:
        if is_kids_artist(lastfm, sim, exclude_set, tag_cache):
            continue
        state.upsert_artist(sim, weight=weight * 0.5)   # propagate a fraction of taste weight

    # exploration: from this artist's primary tags, pull artists we don't own
    if cfg["EXPLORE_RATIO"] > 0:
        for tag in (tag_cache.get(artist) or lastfm.artist_tags(artist))[:2]:
            if tag in KIDS_TAGS:
                continue
            for exp_artist in lastfm.tag_top_artists(tag, limit=int(15 * cfg["EXPLORE_RATIO"]) + 3):
                if not is_kids_artist(lastfm, exp_artist, exclude_set, tag_cache):
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
    sb = Soulbeet(secrets)
    tag_cache = {}

    last_measure = 0
    lib_bytes, track_count, free = 0, 0, float("inf")

    while True:
        cfg = load_config()
        exclude_set = load_exclude_set()

        if cfg["PAUSED"]:
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
            lib_bytes, track_count = measure_library()
            free = free_gb(FREE_SPACE_PATH)
            last_measure = time.time()
            write_status(state, cfg, lib_bytes, track_count, free)

        reason = stop_reason(cfg, lib_bytes, track_count, free)
        if reason:
            log(f"STOP CONDITION: {reason}. Idling 5 min (raise targets in config.env to resume).")
            time.sleep(300)
            last_measure = 0  # force re-measure after idle
            continue

        # refresh taste signal periodically
        last_refresh = int(state.get_meta("last_taste_refresh", "0"))
        if time.time() - last_refresh > cfg["TASTE_REFRESH_MIN"] * 60:
            refresh_taste(state, secrets, lastfm, exclude_set, tag_cache)

        # expand a few top-weighted artists into album candidates
        for artist, weight in state.unexpanded_artists(limit=5):
            expand_one_artist(state, lastfm, artist, weight, cfg, exclude_set, tag_cache)

        # process a wave of pending candidates concurrently
        batch = state.pending_candidates(limit=cfg["CONCURRENCY"] * 3)
        if not batch:
            log("No pending candidates; refreshing taste and expanding more")
            refresh_taste(state, secrets, lastfm, exclude_set, tag_cache)
            time.sleep(10)
            continue

        with ThreadPoolExecutor(max_workers=cfg["CONCURRENCY"]) as ex:
            list(ex.map(lambda r: process_candidate(state, sb, secrets, r), batch))

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Interrupted; state is persisted, safe to restart.")
