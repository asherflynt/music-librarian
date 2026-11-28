#!/usr/bin/env python3
"""LAN web UI for the librarian.

stdlib http.server on purpose: this container's only dependencies are `requests` and
`mutagen`, and a web framework would be by far the largest thing in the image for a
handful of endpoints serving one page.

Security posture: LAN-only, no auth, by explicit choice -- consistent with the slskd /
Soulbeet / Navidrome services already on this network. This UI can queue downloads and
(via the upgrade scanner) cause files to be deleted, so it must NOT be exposed through
the Cloudflared tunnel. There is deliberately no static-file handler and no path
routing to disk: the page is a constant below, so there is nothing to traverse to.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Every knob the UI is allowed to write, with its type and bounds. A POST touching
# anything not in here is rejected -- this is the write boundary to a file the
# librarian re-reads and acts on, so it is an allowlist rather than a denylist.
#   name: (kind, lo, hi)
SETTINGS = {
    "TARGET_TB":                ("float", 0.0, 500.0),
    "TARGET_TRACKS":            ("int", 0, 100_000_000),
    "EXPLORE_RATIO":            ("float", 0.0, 1.0),
    "EXPLORE_MAX_INACTIVE_YEARS": ("int", 0, 100),
    "MIN_FREE_GB":              ("float", 0.0, 100_000.0),
    "STAGING_MIN_FREE_GB":      ("float", 0.0, 100_000.0),
    "CONCURRENCY":              ("int", 1, 16),
    "TASTE_REFRESH_MIN":        ("int", 5, 100_000),
    "MEASURE_EVERY_SEC":        ("int", 30, 86_400),
    "LIBRARY_SCAN_ROOT":        ("str", None, None),
    "SCAN_MAX_PROBES_PER_PASS": ("int", 10, 100_000),
    "ERROR_RETRY_DAYS":         ("int", 0, 365),
    "ERROR_MAX_ATTEMPTS":       ("int", 1, 20),
    "PAUSED":                   ("bool", 0, 1),
    "TASTE_HALF_LIFE_DAYS":     ("float", 1.0, 36_500.0),
    "WEIGHT_NAVIDROME":         ("float", 0.0, 100.0),
    "WEIGHT_PLEX":              ("float", 0.0, 100.0),
    "WEIGHT_YTMUSIC":           ("float", 0.0, 100.0),
    "FAVORITES_ENABLED":        ("bool", 0, 1),
    "FAVORITES_BYPASS_CAP":     ("bool", 0, 1),
    "FAVORITE_SYNC_HOURS":      ("int", 1, 8760),
    "FAVORITE_PRIORITY":        ("int", 0, 10_000),
    "FAVORITE_INCLUDE_EP":      ("bool", 0, 1),
    "FAVORITE_INCLUDE_SINGLES": ("bool", 0, 1),
    "FAVORITE_INCLUDE_LIVE":    ("bool", 0, 1),
    "FAVORITE_INCLUDE_COMPILATIONS": ("bool", 0, 1),
    "FAVORITE_INCLUDE_REMIX":   ("bool", 0, 1),
    "NEW_RELEASE_SYNC_HOURS":   ("int", 1, 8760),
    "NEW_RELEASE_TOP_ARTISTS":  ("int", 0, 5000),
    "NEW_RELEASE_MAX_AGE_DAYS": ("int", 1, 3650),
    "NEW_RELEASE_PRIORITY":     ("int", 0, 10_000),
    "UPGRADE_ENABLED":          ("bool", 0, 1),
    "UPGRADE_HOUR":             ("int", 0, 23),
    "UPGRADE_MAX_PER_RUN":      ("int", 1, 1000),
    "UPGRADE_RECHECK_DAYS":     ("int", 1, 3650),
    "UPGRADE_ONLY_WHEN_IDLE":   ("bool", 0, 1),
    "DECODE_MAX_RETRIES":       ("int", 0, 20),
    "BAD_PEER_STRIKES":         ("int", 1, 50),
    "BAD_PEER_DAYS":            ("int", 1, 3650),
}

# Plain-English explanation of every setting, shown as a hover box in the UI.
#   name:    (short line under the field, one-liner)
#   detail:  what it actually does, in plain English, and what happens if you change
#            it. This is the only place these are written down; README.md's settings
#            table is generated from this dict by tools/gen_settings_doc.py, so the
#            docs cannot drift from the UI.
HELP = {
    "TARGET_TB": (
        "Stop once the library reaches this size",
        "The size you want your music library to grow to, in terabytes. The librarian "
        "keeps downloading until the library hits this OR the track count target, "
        "whichever comes first, then idles.\n\n"
        "Raise it and it picks straight back up where it left off — nothing is lost or "
        "re-downloaded. Lower it below your current size and it stops cleanly; it never "
        "deletes anything to get back under the number.\n\n"
        "This is the one to change when you clear space and want more music."),
    "TARGET_TRACKS": (
        "…or once it reaches this many tracks",
        "The other finish line: stop once the library holds this many tracks. Whichever "
        "target is hit first wins.\n\n"
        "In practice size is almost always the binding limit — 100,000 FLAC tracks would "
        "be far more than 3 TB — so treat this as a safety ceiling rather than the "
        "real goal."),
    "EXPLORE_RATIO": (
        "0 = only what you love · 1 = only new horizons",
        "How adventurous the recommendations are.\n\n"
        "At 0 the librarian only fetches more of what you already play and artists that "
        "sound like them. At 1 it only chases artists you don't own from genres you lean "
        "into. The default 0.15 spends about 85% of its effort deepening your existing "
        "taste and 15% stretching it.\n\n"
        "Turn it down if you're getting music you don't recognise; turn it up if the "
        "library feels like an echo chamber."),
    "EXPLORE_MAX_INACTIVE_YEARS": (
        "Skip tag-explored artists inactive for this many years (0 = off)",
        "Stops broad genre tags (like 'rock' or 'soul') from flooding the queue with "
        "classic acts whose careers ended decades ago.\n\n"
        "When tag exploration suggests an artist, the librarian checks any cached "
        "MusicBrainz life-span (ended year) and cached Last.fm decade tags — with zero "
        "extra API calls — and skips artists clearly inactive for longer than this many "
        "years. Artists you play directly or reach via close similarity are never "
        "filtered.\n\n"
        "Set to 0 to allow tag exploration of arbitrarily old/ended artists."),
    "MIN_FREE_GB": (
        "Hard floor — never fill the pool below this",
        "A safety net for your storage pool. If free space on the cluster pool drops "
        "below this many gigabytes, the librarian stops adding music entirely, no "
        "matter what the targets say.\n\n"
        "This exists so a runaway fill can't fill the pool that also holds your movies, "
        "TV, and everything else. Don't set it near zero — a full ZFS pool performs "
        "badly and is unpleasant to dig out of."),
    "STAGING_MIN_FREE_GB": (
        "Pause if the NVMe staging pool gets this low",
        "Downloads land on the fast NVMe cache first, then beets imports them into the "
        "library. If that cache drops below this many gigabytes free, the librarian "
        "pauses new downloads for a couple of minutes to let imports drain it.\n\n"
        "It protects the cache SSD from filling up mid-transfer, which would stall "
        "every download at once."),
    "CONCURRENCY": (
        "How many albums to download at once",
        "How many albums the librarian works on simultaneously.\n\n"
        "Higher isn't better: Soulseek peers are ordinary people's computers, and "
        "hammering several at once gets you queued, throttled, or banned. 3–4 is the "
        "sweet spot. Raise it only if downloads are clearly idling."),
    "TASTE_REFRESH_MIN": (
        "How often to re-read your play counts (minutes)",
        "How often the librarian re-reads what you've been playing from Navidrome, "
        "Plex/Plexamp, and your YouTube Music history, and re-ranks artists.\n\n"
        "The default of 360 (6 hours) is plenty — your taste doesn't change by the "
        "minute, and each refresh costs a round of API calls. Use the 'Refresh taste' "
        "button if you want it to happen right now."),
    "MEASURE_EVERY_SEC": (
        "How often to re-scan the library (seconds)",
        "How often the librarian walks the library to recount size, tracks, and audio "
        "quality.\n\n"
        "Scans run in a background thread so downloads keep queuing while a scan is in "
        "progress, and files whose size and mtime haven't changed are not re-read. "
        "Lower it if you want the progress numbers to feel more live; raise it if the "
        "disk is busy."),
    "LIBRARY_SCAN_ROOT": (
        "Read-only directory tree to walk during library scans",
        "Where the background library scanner reads directory listings and audio "
        "metadata from.\n\n"
        "At the default 'auto', the librarian checks whether /cluster/Media/Music "
        "(Unraid's direct read-only ZFS pool mount) exists and matches /music by "
        "verifying sample entry names, sizes, and mtimes. When it matches, scans walk "
        "ZFS directly — avoiding Unraid's single-threaded /mnt/user FUSE (shfs) "
        "bottleneck under disk load — and otherwise fall back to /music.\n\n"
        "All paths stored in state.db are translated back to /music/... regardless of "
        "the scan root, so existing inventory rows, upgrades, and UI stats stay "
        "identical. Set to /music to force FUSE scanning, or another absolute path to "
        "a read-only mount of the same library."),
    "SCAN_MAX_PROBES_PER_PASS": (
        "How many new files to analyse per scan (safety limit)",
        "How many new or changed files the librarian opens per scan pass to read "
        "their real audio format.\n\n"
        "This is a safety limit rather than a speed dial. Your library is reached "
        "through Unraid's /mnt/user layer, which is a single shared chokepoint that "
        "Plex, slskd, beets and everything else also go through. Counting files there "
        "is cheap; opening thousands of them in a tight loop is not — that is what "
        "locked the server up hard enough to need a power cycle on 17 Jul 2026.\n\n"
        "Files that don't fit in a pass simply wait for the next one, so the scan "
        "still finishes — it just spreads the load. At the default it works through "
        "roughly 3,000 files an hour. Only raise it if you know the server is idle."),
    "ERROR_RETRY_DAYS": (
        "Retry errored albums after this many days (0 = never)",
        "How long to wait before giving a failed download candidate another try.\n\n"
        "Transient Soulbeet or slskd outages don't mark albums as errors at all — the "
        "librarian simply backs off and keeps them pending. When an album does hit a "
        "real error, it sits out for this many days before being re-queued automatically "
        "(checked every 6 hours, without changing its priority), up to "
        "ERROR_MAX_ATTEMPTS total tries.\n\n"
        "Set to 0 to disable automatic error retries and leave errored albums alone."),
    "ERROR_MAX_ATTEMPTS": (
        "Maximum failed tries before giving up on an album",
        "How many times a candidate can error out before the librarian stops "
        "re-queuing it.\n\n"
        "Prevents broken metadata or permanently unfetchable albums from cycling "
        "through the queue forever. Once an album hits this many errors, "
        "ERROR_RETRY_DAYS will leave it alone.\n\n"
        "Historical errors recorded before attempt counting was added start at 0, so "
        "they get a clean set of retries."),
    "PAUSED": (
        "Stop queueing downloads, keep everything else running",
        "Pauses new downloads without stopping the container or losing any progress.\n\n"
        "In-flight transfers finish, imports keep happening, and the UI stays live. Flip "
        "it back off and it resumes exactly where it was. Same as the Pause button up top."),

    "TASTE_HALF_LIFE_DAYS": (
        "A play this old counts half as much",
        "Makes recent listening matter more than old listening.\n\n"
        "At the default 365, a song you played a year ago counts half as much as one you "
        "played today; two years ago, a quarter. Without this, a 2019 obsession would "
        "weigh exactly as much as last week and the library would grow toward who you "
        "used to be.\n\n"
        "Lower it (e.g. 180) to chase your current mood harder. Raise it (e.g. 1095) to "
        "give long-time favourites more say."),
    "WEIGHT_NAVIDROME": (
        "How much Navidrome plays count",
        "How much weight to give plays from Navidrome — this library, in the web player "
        "or a Subsonic app.\n\n"
        "This is direct evidence of what you like, so it's weighted high by default. "
        "Note Navidrome only reports an album's LAST played date, so the recency decay "
        "here is coarser than for Plex."),
    "WEIGHT_PLEX": (
        "How much Plex / Plexamp plays count",
        "How much weight to give plays from Plex and Plexamp, read via Tautulli.\n\n"
        "Plexamp is a Plex client, so its plays are recorded by Plex and never reach "
        "Navidrome — without Tautulli they'd be invisible. Requires TAUTULLI_API_KEY in "
        "secrets.env; without it this signal is simply skipped.\n\n"
        "There's no double-counting: a play is recorded by whichever server actually "
        "served the audio, never both."),
    "WEIGHT_YTMUSIC": (
        "How much YouTube Music history counts",
        "How much weight to give your YouTube Music listening history from Google "
        "Takeout.\n\n"
        "Weighted lower than Navidrome/Plex by default because it's noisier — it "
        "includes a lot of incidental, background, and one-off listening rather than "
        "music you deliberately chose. It's most valuable as the cold-start signal "
        "before you've played much in this library."),

    "FAVORITES_ENABLED": (
        "Fetch full discographies for favourited artists",
        "Turns the favourites system on or off.\n\n"
        "When on, any artist you favourite gets their ENTIRE discography downloaded, and "
        "is re-checked forever so new releases arrive automatically. Favourite an artist "
        "by starring them in Navidrome, clicking ★ in the Taste list, or adding them to "
        "favorites.txt.\n\n"
        "Turning this off leaves existing downloads alone; it just stops new discography "
        "syncing."),
    "FAVORITES_BYPASS_CAP": (
        "Keep syncing favourites even after hitting size/track targets",
        "Lets favourited artists' discographies and new releases keep syncing and "
        "downloading even after the library reaches TARGET_TB or TARGET_TRACKS.\n\n"
        "On by default. The size and track targets stop ordinary discovery and "
        "exploration, not artists you explicitly starred. The hard free-space floor "
        "(MIN_FREE_GB), staging cache guard (STAGING_MIN_FREE_GB), and Pause still stop "
        "everything — favourites never bypass those.\n\n"
        "Turn this off if you want TARGET_TB and TARGET_TRACKS to act as a strict freeze "
        "on all downloads, including favourites."),
    "FAVORITE_SYNC_HOURS": (
        "How often to check favourites for new releases",
        "How often to ask MusicBrainz whether your favourited artists have released "
        "anything new.\n\n"
        "This is what makes new albums show up on their own. Daily (24) is plenty — "
        "artists don't release albums hourly, and MusicBrainz allows only one request "
        "per second. Use 'Sync favorites' to check right now."),
    "FAVORITE_PRIORITY": (
        "Queue position vs ordinary discovery (higher = sooner)",
        "How far ahead of the normal discovery queue favourites jump.\n\n"
        "This matters more than it sounds: the 100k-track fill generates thousands of "
        "candidates, so without a priority boost a favourite's discography would sit "
        "behind all of them and effectively never download. Anything above 0 puts "
        "favourites first; the exact number only matters if you want tiers."),
    "FAVORITE_INCLUDE_EP": (
        "Include EPs in a favourite's discography",
        "Whether EPs count as part of 'the discography'.\n\n"
        "On by default — EPs are usually real, wanted releases. For Glass Animals this "
        "is the difference between 4 albums and 7 releases."),
    "FAVORITE_INCLUDE_SINGLES": (
        "Include singles — warning, there are a LOT",
        "Whether to also download every single.\n\n"
        "Off for a good reason. Glass Animals alone has 29 singles and 26 remix singles "
        "against just 4 studio albums — turning this on takes them from 7 releases to 36, "
        "and almost all of it is duplicate versions of tracks you already have from the "
        "albums.\n\n"
        "Only turn this on for an artist whose singles genuinely aren't on any album."),
    "FAVORITE_INCLUDE_LIVE": (
        "Include live albums and broadcasts",
        "Whether live recordings count as part of the discography.\n\n"
        "Off by default. These are often radio sessions and broadcast recordings of "
        "songs you already own, and their audio quality is inconsistent."),
    "FAVORITE_INCLUDE_COMPILATIONS": (
        "Include compilations and greatest-hits",
        "Whether compilations, best-ofs, and greatest-hits count.\n\n"
        "Off by default, because if you already have the studio albums a compilation is "
        "almost entirely duplicate tracks."),
    "FAVORITE_INCLUDE_REMIX": (
        "Include remix albums and remix singles",
        "Whether remix releases count.\n\n"
        "Off by default. Remixes are the single biggest source of near-duplicates — for "
        "Glass Animals there are 26 remix singles alone."),

    "NEW_RELEASE_SYNC_HOURS": (
        "How often to check top played artists for new releases (hours)",
        "How often to ask MusicBrainz whether your top played artists (not just "
        "favourites) have released new albums or EPs.\n\n"
        "Work is spread in small batches across main-loop passes so MusicBrainz's "
        "1 req/s rate limit never stalls downloads. Artist MBIDs are cached in state.db "
        "so each artist is resolved once. Release-type filters reuse the "
        "FAVORITE_INCLUDE_* settings (albums + EPs by default)."),
    "NEW_RELEASE_TOP_ARTISTS": (
        "How many top played artists to watch for new releases (0 = off)",
        "How many of your highest-weighted artists (ranked by play-derived taste "
        "weight, excluding blocklisted/kids artists and active favourites already "
        "covered by favorites sync) are watched for new releases on MusicBrainz.\n\n"
        "At the default 500, newly released albums from artists you actually play are "
        "spotted and queued automatically without having to star every one of them. "
        "Set to 0 to watch favourites only."),
    "NEW_RELEASE_MAX_AGE_DAYS": (
        "Max age (days) of releases queued by the new-release watch",
        "How recent a release's MusicBrainz first-release-date must be to qualify for "
        "the played-artist new-release watch.\n\n"
        "At the default 730 (two years), recent albums and EPs from your top played "
        "artists are queued ahead of the general discovery backlog without pulling "
        "every artist's entire back catalogue. Use Favourites if you want an artist's "
        "complete discography."),
    "NEW_RELEASE_PRIORITY": (
        "Queue priority for played-artist new releases",
        "Base queue priority given to recent releases spotted for top played artists.\n\n"
        "Defaults to 50 — below FAVORITE_PRIORITY (100) so starred artists always go "
        "first, and well above normal discovery (0) so new releases jump ahead of the "
        "general backlog. Recent releases also receive a small recency bonus (always "
        "capped below FAVORITE_PRIORITY when favourites rank higher)."),

    "UPGRADE_ENABLED": (
        "Re-download lossy albums as FLAC when one appears",
        "Periodically re-checks albums you own as MP3 or AAC to see whether a FLAC "
        "version has shown up on Soulseek since you got them.\n\n"
        "Off by default. When a FLAC is found it's downloaded to a staging area, NOT "
        "straight into your library. A separate script then verifies it — every file "
        "must be FLAC, the track count can't drop, every file must fully decode, and the "
        "running time must match within 5% — before it replaces the old copy.\n\n"
        "Your old lossy copy is DELETED once the replacement is verified and confirmed "
        "in place. If any check fails, nothing is touched."),
    "UPGRADE_HOUR": (
        "Hour of day to run the upgrade check (0–23)",
        "What time of day the nightly upgrade pass runs, in 24-hour server local time "
        "(3 = 3am).\n\n"
        "It runs once a day at this hour. Pick a time when nobody's streaming and the "
        "server is otherwise quiet."),
    "UPGRADE_MAX_PER_RUN": (
        "Albums checked per nightly pass",
        "The most albums the upgrade pass will check in one night.\n\n"
        "This is the main 'don't overwhelm anything' dial. Each check is a full Soulseek "
        "search against real peers, so checking hundreds at once is both slow and a good "
        "way to get banned. At 10/night it works through a backlog steadily without "
        "anyone noticing."),
    "UPGRADE_RECHECK_DAYS": (
        "Don't re-check the same album within this many days",
        "How long to wait before asking about an album again after finding no FLAC.\n\n"
        "If nobody on Soulseek had a FLAC of an album yesterday, they almost certainly "
        "won't today either. Waiting 30 days stops the pass from burning its whole nightly "
        "budget re-asking the same hopeless albums instead of trying new ones."),
    "UPGRADE_ONLY_WHEN_IDLE": (
        "Wait for the main download queue to go quiet first",
        "Makes upgrades yield to the main fill.\n\n"
        "On by default. Upgrades and normal downloads compete for the same Soulseek "
        "peers and the same bandwidth; upgrades are a nice-to-have, so they wait rather "
        "than slow down music you don't have yet."),

    "DECODE_MAX_RETRIES": (
        "Retries from other peers after a corrupt download",
        "How many times the librarian will re-queue an album after the decode gate "
        "rejects a corrupt or truncated download.\n\n"
        "The peer that served the bad file is blocked for that album immediately, so "
        "each retry tries a different Soulseek peer. Once an album uses up this many "
        "retries, it is marked corrupt_only so it stops churning the queue.\n\n"
        "Set to 0 to never automatically retry rejected downloads."),
    "BAD_PEER_STRIKES": (
        "Bad albums before blocking a Soulseek peer everywhere",
        "How many different albums a Soulseek peer can serve corrupt or truncated "
        "files for within BAD_PEER_DAYS before the librarian stops picking that peer "
        "for ANY album. Several bad tracks from the same album count once.\n\n"
        "A peer is already blocked immediately for the specific album it broke. This "
        "global threshold catches peers whose whole shared collection is damaged or "
        "re-encoded junk, without permanently banning a good peer over one bad rip.\n\n"
        "Lower it (e.g. 2) to cut off bad peers faster; raise it to be more forgiving."),
    "BAD_PEER_DAYS": (
        "How many days a peer's bad-file strikes stay on record",
        "The rolling window for counting a peer's strikes toward BAD_PEER_STRIKES.\n\n"
        "Rejections older than this many days stop counting toward a global block, so "
        "a peer who fixes their library isn't banished forever. Per-album blocks stay "
        "in place so the librarian won't re-pull the same broken copy."),
}

CTX = {}
_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# env-file read/write (comment- and order-preserving)
# ---------------------------------------------------------------------------
_KV = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")


def read_env(path):
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if line.strip().startswith("#"):
            continue
        m = _KV.match(line)
        if m:
            out[m.group(2)] = m.group(4).split("#")[0].strip()
    return out


def write_env(path, updates):
    """Rewrite matching KEY= lines in place; append anything new.

    config.env's comments ARE its documentation -- they explain what each knob does
    and are the only such docs on the server. A naive dump of key=value would erase
    them, so existing lines are edited in place and everything else is passed through
    untouched. Written via temp+rename so the librarian, which re-reads this file
    every loop, can never observe a half-written config.
    """
    with _write_lock:
        lines = path.read_text().splitlines() if path.exists() else []
        seen = set()
        out = []
        for line in lines:
            m = _KV.match(line)
            if m and m.group(2) in updates and not line.strip().startswith("#"):
                key = m.group(2)
                trailing = ""
                # keep any inline comment that documents the value
                if "#" in m.group(4):
                    trailing = "   # " + m.group(4).split("#", 1)[1].strip()
                out.append(f"{m.group(1)}{key}={updates[key]}{trailing}")
                seen.add(key)
            else:
                out.append(line)
        for k, v in updates.items():
            if k not in seen:
                out.append(f"{k}={v}")
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(out) + "\n")
        os.replace(tmp, path)


def write_lines(path, names):
    with _write_lock:
        header = ""
        if path.exists():
            keep = []
            for line in path.read_text().splitlines():
                if line.strip().startswith("#") or not line.strip():
                    keep.append(line)
                else:
                    break        # header block only: stop at the first real entry
            header = "\n".join(keep).rstrip() + "\n\n" if keep else ""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(header + "\n".join(sorted(names, key=str.lower)) + "\n")
        os.replace(tmp, path)


def read_lines(path):
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text().splitlines()
            if l.strip() and not l.strip().startswith("#")]


def current_config():
    """Every setting's current value, as a string.

    Falls back to the librarian's own effective value for keys absent from
    config.env (they have code defaults). Without this the UI would render those
    fields blank and saving would write an empty value back over a working default.
    """
    on_disk = read_env(CTX["config_env"])
    try:
        effective = CTX["cfg"]()
    except Exception:
        effective = {}
    out = {}
    for k in SETTINGS:
        if on_disk.get(k, "") != "":
            out[k] = on_disk[k]
            continue
        v = effective.get(k)
        if isinstance(v, bool):
            out[k] = "1" if v else "0"
        elif isinstance(v, float) and SETTINGS[k][0] == "int":
            out[k] = str(int(v))
        else:
            out[k] = "" if v is None else str(v)
    return out


def coerce(key, raw):
    """Validate + clamp one setting. Raises ValueError on anything unacceptable."""
    if key not in SETTINGS:
        raise ValueError(f"unknown setting {key!r}")
    kind, lo, hi = SETTINGS[key]
    if kind == "bool":
        if isinstance(raw, bool):
            return "1" if raw else "0"
        return "1" if str(raw).strip().lower() in ("1", "true", "yes", "on") else "0"
    if kind == "str":
        s = str(raw if raw is not None else "").strip()
        if not s or s.lower() == "auto":
            return "auto"
        if any(ch in s for ch in ("\r", "\n", "\x00", "#", '"', "'")):
            raise ValueError(f"{key}: invalid characters in path")
        if not s.startswith("/"):
            raise ValueError(f"{key}: must be 'auto' or an absolute path starting with '/'")
        if ".." in Path(s).parts:
            raise ValueError(f"{key}: '..' is not allowed")
        return os.path.normpath(s)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: {raw!r} is not a number")
    if v != v:                       # NaN
        raise ValueError(f"{key}: not a number")
    v = max(lo, min(hi, v))          # clamp rather than reject: the UI is a slider,
                                     # not a config parser, and a clamp is predictable
    return str(int(v)) if kind == "int" else str(v)


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "librarian"

    def log_message(self, fmt, *args):
        pass                          # don't spam the librarian's log with hits

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                      # browser navigated away mid-poll

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    MAX_BODY = 1 << 20   # 1 MiB — this API only ever receives small JSON

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > self.MAX_BODY:
            # Don't read an attacker-declared multi-GB Content-Length into memory.
            return {}
        try:
            return json.loads(self.rfile.read(n) or "{}")
        except ValueError:
            return {}

    def _same_origin(self):
        """CSRF guard for state-changing requests.

        This server is unauthenticated by design (LAN only), which means any web page
        the user happens to open could otherwise POST to it cross-origin and change
        settings, clear the kids-music blocklist, or trigger a scan -- and, via DNS
        rebinding, even read responses. Browsers attach an Origin header to such
        cross-origin POSTs, so requiring Origin (when present) to match the Host we
        were reached on rejects them. Same-origin requests from our own page match and
        pass; tools like curl send no Origin and are allowed, so automation is
        unaffected. No allowlist to maintain and no way to lock the user out: the check
        is relative to whatever host they actually used.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        st = CTX["state"]
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")

            if u.path == "/api/status":
                # status.json is produced by the main loop's measurement pass; the
                # UI reads that rather than re-walking the library on every poll.
                try:
                    data = json.loads(CTX["status_json"].read_text())
                except (OSError, ValueError):
                    data = {"updated": None, "note": "no measurement yet"}
                data["config"] = current_config()
                return self._json(data)

            if u.path == "/api/config":
                return self._json(current_config())

            if u.path == "/api/help":
                # Served rather than duplicated into the page so HELP stays the one
                # place these explanations are written.
                return self._json({k: {"kind": SETTINGS[k][0],
                                       "min": SETTINGS[k][1], "max": SETTINGS[k][2],
                                       "short": v[0], "detail": v[1]}
                                   for k, v in HELP.items() if k in SETTINGS})

            if u.path == "/api/exclude":
                return self._json({"names": read_lines(CTX["exclude_txt"])})

            if u.path == "/api/favorites":
                return self._json({"names": read_lines(CTX["favorites_txt"])})

            if u.path == "/api/artists":
                limit = min(500, int(q.get("limit", ["60"])[0]))
                excl = {n.lower() for n in read_lines(CTX["exclude_txt"])}
                favs = {n.lower() for n in read_lines(CTX["favorites_txt"])}
                return self._json({"artists": [
                    {"artist": a, "weight": round(w or 0, 2),
                     "expanded": bool(e),
                     "excluded": (a or "").lower() in excl,
                     "favorite": (a or "").lower() in favs}
                    for (a, w, e) in st.top_artists(limit)]})

            if u.path == "/api/log":
                n = min(500, int(q.get("n", ["120"])[0]))
                # Already scrubbed: log() runs scrub_secrets() before anything
                # reaches the ring buffer, so no key can be here to leak.
                return self._json({"lines": list(CTX["log_ring"])[-n:]})

            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        st = CTX["state"]
        try:
            if not self._same_origin():
                return self._json(
                    {"error": "cross-origin request rejected"}, 403)

            if u.path == "/api/config":
                body = self._body()
                updates, errors = {}, []
                for k, v in body.items():
                    try:
                        updates[k] = coerce(k, v)
                    except ValueError as e:
                        errors.append(str(e))
                if errors:
                    return self._json({"error": "; ".join(errors)}, 400)
                if updates:
                    write_env(CTX["config_env"], updates)
                return self._json({"ok": True, "written": updates,
                                   "note": "applies on the next loop (~1 min)"})

            if u.path in ("/api/pause", "/api/resume"):
                write_env(CTX["config_env"],
                          {"PAUSED": "1" if u.path.endswith("pause") else "0"})
                return self._json({"ok": True})

            if u.path == "/api/exclude":
                names = [n.strip() for n in self._body().get("names", []) if n.strip()]
                write_lines(CTX["exclude_txt"], set(names))
                return self._json({"ok": True, "count": len(set(names))})

            if u.path == "/api/favorites":
                names = [n.strip() for n in self._body().get("names", []) if n.strip()]
                write_lines(CTX["favorites_txt"], set(names))
                # Make the next loop pick it up now rather than up to
                # FAVORITE_SYNC_HOURS later -- adding a favorite should feel immediate.
                st.set_meta("last_favorites_sync", 0)
                return self._json({"ok": True, "count": len(set(names))})

            if u.path == "/api/refresh-taste":
                st.set_meta("last_taste_refresh", 0)
                return self._json({"ok": True})

            if u.path == "/api/sync-favorites":
                st.set_meta("last_favorites_sync", 0)
                return self._json({"ok": True})

            if u.path == "/api/scan-upgrades":
                if not CTX["cfg"]()["UPGRADE_ENABLED"]:
                    return self._json(
                        {"error": "upgrades are disabled — enable UPGRADE_ENABLED first"}, 400)
                st.set_meta("force_upgrade_pass", 1)
                return self._json({"ok": True, "note": "pass starts within ~5 min"})

            return self._json({"error": "not found"}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)


def serve(port, ctx):
    CTX.update(ctx)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Web UI on http://0.0.0.0:{port} "
          f"(LAN only — do not expose publicly)", flush=True)
    return srv


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Music Librarian</title>
<style>
:root{
  --bg:#0f1115; --card:#171a21; --line:#262b36; --fg:#e6e9ef; --dim:#98a0b3;
  --acc:#5aa9e6; --ok:#4ec9a0; --warn:#e6b45a; --bad:#e66a6a; --fav:#c58af9;
}
@media (prefers-color-scheme:light){
  :root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ec;--fg:#1a1d24;--dim:#5d6577;--acc:#1f6feb}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:16px 20px;border-bottom:1px solid var(--line);display:flex;
  align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0;font-weight:650;letter-spacing:.2px}
.wrap{padding:20px;max-width:1180px;margin:0 auto}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(290px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{font-size:12px;margin:0 0 12px;color:var(--dim);text-transform:uppercase;
  letter-spacing:.9px;font-weight:600}
.big{font-size:26px;font-weight:650;letter-spacing:-.4px}
.sub{color:var(--dim);font-size:12px}
.bar{height:7px;background:var(--line);border-radius:4px;overflow:hidden;margin:10px 0 6px}
.bar>i{display:block;height:100%;background:var(--acc);transition:width .4s}
.row{display:flex;justify-content:space-between;gap:10px;padding:4px 0}
.row+.row{border-top:1px solid var(--line)}
table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.scroll{max-height:290px;overflow:auto}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;
  background:var(--line);color:var(--dim)}
.pill.ok{background:rgba(78,201,160,.16);color:var(--ok)}
.pill.warn{background:rgba(230,180,90,.16);color:var(--warn)}
.pill.bad{background:rgba(230,106,106,.16);color:var(--bad)}
.pill.fav{background:rgba(197,138,249,.16);color:var(--fav)}
button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:7px 13px;
  font-size:13px;font-weight:550;cursor:pointer}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--fg)}
button:disabled{opacity:.45;cursor:not-allowed}
button:active{transform:translateY(1px)}
input,select{background:var(--bg);border:1px solid var(--line);color:var(--fg);
  border-radius:6px;padding:5px 8px;font-size:13px;width:100%}
label{display:block;font-size:12px;color:var(--dim);margin:0 0 4px}
/* Each setting is one self-contained block so its label always sits directly above
   its own input -- a flat grid of alternating label/input cells lets them drift into
   neighbouring columns as the viewport changes, which mislabels the fields. */
.settings{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:14px}
.setting{display:flex;flex-direction:column;justify-content:flex-start}
.setting .name{color:var(--fg);font-weight:550;font-size:12.5px}
.setting .desc{display:block;color:var(--dim);font-size:11px;line-height:1.35;
  margin-top:1px;min-height:29px}
.setting input,.setting select{margin-top:auto}
.setting .q{display:inline-flex;align-items:center;justify-content:center;width:13px;
  height:13px;border-radius:99px;border:1px solid var(--line);color:var(--dim);
  font-size:9px;font-weight:700;margin-left:5px;cursor:help;vertical-align:1px}
.setting:hover .q{border-color:var(--acc);color:var(--acc)}
/* Hover detail box. Fixed-position and appended to <body> so it is never clipped by
   the settings card's own bounds or scroll container. */
#tip{position:fixed;z-index:50;max-width:340px;background:var(--card);
  border:1px solid var(--line);border-radius:9px;padding:11px 13px;
  box-shadow:0 10px 34px #0009;font-size:12.5px;line-height:1.5;
  opacity:0;visibility:hidden;transition:opacity .13s;pointer-events:none}
#tip.on{opacity:1;visibility:visible}
#tip h4{margin:0 0 6px;font-size:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--acc);font-weight:650;letter-spacing:.2px}
#tip p{margin:0 0 7px}
#tip p:last-child{margin-bottom:0}
#tip .meta{color:var(--dim);font-size:11px;border-top:1px solid var(--line);
  padding-top:6px;margin-top:8px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:10px;
  overflow:auto;max-height:320px;font-size:11.5px;line-height:1.55;margin:0}
.banner{padding:9px 13px;border-radius:8px;margin-bottom:14px;font-size:13px;display:none}
.banner.on{display:block}
.banner.stop{background:rgba(230,180,90,.14);border:1px solid rgba(230,180,90,.4)}
.banner.pause{background:rgba(230,106,106,.14);border:1px solid rgba(230,106,106,.4)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:flex;align-items:center;gap:5px;background:var(--bg);
  border:1px solid var(--line);border-radius:99px;padding:2px 5px 2px 10px;font-size:12px}
.chip b{font-weight:500}
.chip button{background:none;color:var(--dim);padding:0 4px;font-size:14px;line-height:1}
.chip button:hover{color:var(--bad)}
.add{display:flex;gap:6px;margin-top:9px}
.muted{color:var(--dim);font-size:12px;padding:8px 0}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--card);
  border:1px solid var(--line);border-radius:8px;padding:9px 15px;font-size:13px;
  opacity:0;transition:opacity .25s;pointer-events:none;z-index:10;box-shadow:0 6px 22px #0006}
#toast.on{opacity:1}
</style></head><body>
<header>
  <h1>🎵 Music Librarian</h1>
  <span id="hdr" class="sub"></span>
  <span style="flex:1"></span>
  <button id="pause" class="ghost">Pause</button>
  <button id="refresh" class="ghost">Refresh taste</button>
  <button id="syncfav" class="ghost">Sync favorites</button>
  <button id="scan" class="ghost">Scan upgrades</button>
</header>
<div class="wrap">
  <div id="pausebanner" class="banner pause">Paused — no new downloads are being queued.</div>
  <div id="stopbanner" class="banner stop"></div>

  <div class="grid">
    <div class="card">
      <h2>Progress</h2>
      <div class="big" id="tb">–</div>
      <div class="bar"><i id="tbbar" style="width:0"></i></div>
      <div class="sub" id="tbsub">–</div>
      <div class="row" style="margin-top:10px"><span class="sub">Tracks</span><b id="tracks">–</b></div>
      <div class="row"><span class="sub">Cluster free</span><b id="free">–</b></div>
      <div class="row"><span class="sub">Staging free</span><b id="sfree">–</b></div>
      <div class="row"><span class="sub">Updated</span><span class="sub" id="upd">–</span></div>
    </div>

    <div class="card">
      <h2>Library quality (on disk)</h2>
      <div class="big" id="llpct">–</div>
      <div class="sub">lossless</div>
      <div class="bar"><i id="llbar" style="width:0;background:var(--ok)"></i></div>
      <table id="qual"></table>
      <div class="sub" id="upcand" style="margin-top:8px"></div>
    </div>

    <div class="card">
      <h2>This run</h2>
      <table id="counts"></table>
    </div>

    <div class="card">
      <h2>Decode gate</h2>
      <div id="dgwarn" class="banner pause" style="margin-bottom:10px"></div>
      <div class="row"><span class="sub">Accepted (24h)</span><b id="dgacc">–</b></div>
      <div class="row"><span class="sub">Rejected (24h / 7d)</span><b id="dgrej">–</b></div>
      <div class="row"><span class="sub">Matched / unmatched (7d)</span><span class="sub" id="dgmatch">–</span></div>
      <div class="row"><span class="sub">Blocked peers</span><b id="dgpeers">–</b></div>
      <table id="dgreasons" style="margin-top:8px"></table>
    </div>

    <div class="card">
      <h2>Favorites <span class="sub">— full discography + new releases</span></h2>
      <div id="favlist" class="scroll"></div>
      <div class="add">
        <input id="favin" placeholder="Add artist (exact name)…">
        <button id="favadd">Add</button>
      </div>
      <div class="sub" style="margin-top:7px">Starring an artist in Navidrome works too.</div>
    </div>

    <div class="card">
      <h2>New releases</h2>
      <div class="sub" id="nrwatch" style="margin-bottom:8px"></div>
      <div class="scroll"><table id="newrel"></table></div>
    </div>

    <div class="card">
      <h2>Lossy fallbacks</h2>
      <div class="scroll"><table id="fb"></table></div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Settings <span class="sub">— saved to config.env, applied next loop · hover any setting for details</span></h2>
    <div class="settings" id="settings"></div>
    <div style="margin-top:13px;display:flex;gap:8px;align-items:center">
      <button id="save">Save settings</button>
      <span class="sub" id="savenote"></span>
    </div>
  </div>

  <div class="grid" style="margin-top:14px">
    <div class="card">
      <h2>Taste — top artists <span class="sub">(vet the list; exclude what you don't want)</span></h2>
      <div class="scroll"><table id="artists"></table></div>
    </div>
    <div class="card">
      <h2>Excluded artists</h2>
      <div id="exclist" class="scroll"></div>
      <div class="add">
        <input id="excin" placeholder="Add artist to blocklist…">
        <button id="excadd">Add</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Log</h2>
    <pre id="log">…</pre>
  </div>
</div>
<div id="toast"></div>
<div id="tip"></div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gb = n => n == null ? '–' : (n >= 1024 ? (n/1024).toFixed(2)+' TB' : Math.round(n)+' GB');
let toastT;
function toast(m){ const t=$('#toast'); t.textContent=m; t.classList.add('on');
  clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove('on'),2600); }
async function api(p, opt){ const r = await fetch(p, opt);
  const j = await r.json().catch(()=>({})); if(!r.ok) throw new Error(j.error||r.status); return j; }
async function post(p, body){ return api(p, {method:'POST',
  headers:{'Content-Type':'application/json'}, body: body?JSON.stringify(body):'{}'}); }

// Setting metadata (type, bounds, plain-English help) comes from the server so it
// lives in exactly one place — see HELP/SETTINGS in web.py.
let HELP = {};
const isBool = k => (HELP[k]||{}).kind === 'bool';

// ---- hover detail box ----------------------------------------------------
const tip = () => $('#tip');
function showTip(el, k){
  const h = HELP[k]; if(!h) return;
  const bounds = h.kind === 'bool' ? 'on / off'
    : h.kind === 'str' ? 'allowed: auto or absolute path'
    : `allowed: ${h.min} – ${h.max}${h.kind === 'int' ? ' (whole numbers)' : ''}`;
  tip().innerHTML = `<h4>${esc(k)}</h4>` +
    h.detail.split('\n\n').map(p => `<p>${esc(p)}</p>`).join('') +
    `<div class="meta">${esc(bounds)}</div>`;
  tip().classList.add('on');
  // Anchor beside the setting, then keep it inside the viewport. Prefer the right,
  // flip left when there's no room, and clamp vertically rather than letting it run
  // off the bottom of a long settings grid.
  const r = el.getBoundingClientRect(), t = tip().getBoundingClientRect();
  let x = r.right + 10, y = r.top;
  if(x + t.width > innerWidth - 8) x = r.left - t.width - 10;
  if(x < 8) x = 8;
  if(y + t.height > innerHeight - 8) y = innerHeight - t.height - 8;
  if(y < 8) y = 8;
  tip().style.left = x + 'px'; tip().style.top = y + 'px';
}
const hideTip = () => tip().classList.remove('on');

let settingsBuilt = false;
function buildSettings(cfg){
  if(settingsBuilt || !Object.keys(HELP).length) return; settingsBuilt = true;
  $('#settings').innerHTML = Object.keys(cfg).map(k => {
    const h = HELP[k] || {};
    const field = isBool(k)
      ? `<select data-k="${k}"><option value="0">off</option><option value="1">on</option></select>`
      : (h.kind === 'str'
        ? `<input data-k="${k}" type="text" placeholder="auto">`
        : `<input data-k="${k}" type="number" step="any">`);
    return `<div class="setting" data-help="${k}">
      <label><span class="name">${k}</span><span class="q">?</span>
      <span class="desc">${esc(h.short || '')}</span></label>${field}</div>`;
  }).join('');
  for(const [k,v] of Object.entries(cfg)){
    const el = document.querySelector(`[data-k="${k}"]`); if(!el) continue;
    el.value = isBool(k) ? (['1','true','yes','on'].includes(String(v).toLowerCase())?'1':'0') : v;
  }
  document.querySelectorAll('[data-help]').forEach(el => {
    el.addEventListener('mouseenter', () => showTip(el, el.dataset.help));
    el.addEventListener('mouseleave', hideTip);
    // Keyboard + touch: focusing the field explains it too, so the help isn't
    // mouse-only.
    el.addEventListener('focusin', () => showTip(el, el.dataset.help));
    el.addEventListener('focusout', hideTip);
  });
}
addEventListener('scroll', hideTip, true);

function renderFavs(favs, names){
  const byName = {}; (favs||[]).forEach(f => byName[f.artist.toLowerCase()] = f);
  if(!names.length && !(favs||[]).length){
    $('#favlist').innerHTML = '<div class="muted">No favorites yet. Add one below, or star an artist in Navidrome — the librarian will fetch their whole discography and keep watching for new releases.</div>';
    return;
  }
  const all = new Set([...names, ...(favs||[]).map(f=>f.artist)]);
  $('#favlist').innerHTML = `<table>${[...all].sort((a,b)=>a.localeCompare(b)).map(n => {
    const f = byName[n.toLowerCase()] || {};
    const manual = names.some(x => x.toLowerCase() === n.toLowerCase());
    let st = '<span class="pill">pending sync</span>';
    if(f.note) st = `<span class="pill warn" title="${esc(f.note)}">${esc(f.note.slice(0,26))}</span>`;
    else if(f.albums_total) st = `<span class="pill ${f.albums_have>=f.albums_total?'ok':''}">${f.albums_have}/${f.albums_total} albums</span>`;
    return `<tr><td><b>${esc(n)}</b><br><span class="sub">${f.source==='navidrome'?'★ starred in Navidrome':'manual'}</span></td>
      <td style="text-align:right">${st}</td>
      <td style="width:26px">${manual?`<button class="chip-x ghost" data-fav="${esc(n)}" style="padding:2px 7px">✕</button>`:''}</td></tr>`;
  }).join('')}</table>`;
  document.querySelectorAll('[data-fav]').forEach(b => b.onclick = async () => {
    const next = names.filter(x => x !== b.dataset.fav);
    await post('/api/favorites', {names: next}); toast('Favorite removed'); load();
  });
}

async function load(){
  try{
    // Help text never changes at runtime — fetch it once, not on every 5s poll.
    if(!Object.keys(HELP).length) HELP = await api('/api/help');
    const [s, arts, lg, exc, fav] = await Promise.all([
      api('/api/status'), api('/api/artists?limit=60'), api('/api/log?n=120'),
      api('/api/exclude'), api('/api/favorites')]);

    // progress
    const pct = s.pct_to_target ?? 0;
    $('#tb').textContent = (s.library_tb ?? 0).toFixed(3) + ' TB';
    $('#tbbar').style.width = Math.min(100, pct) + '%';
    $('#tbsub').textContent = `${pct}% of ${s.target_tb ?? '?'} TB target`;
    $('#tracks').textContent = `${(s.tracks ?? 0).toLocaleString()} / ${(s.target_tracks ?? 0).toLocaleString()}`;
    $('#free').textContent = gb(s.cluster_free_gb);
    $('#sfree').textContent = gb(s.staging_free_gb);
    $('#upd').textContent = s.updated ?? '–';
    $('#hdr').textContent = s.paused ? 'paused' : (s.stop_reason ? 'idle' : 'running');

    $('#pause').textContent = s.paused ? 'Resume' : 'Pause';
    $('#pausebanner').classList.toggle('on', !!s.paused);
    $('#stopbanner').classList.toggle('on', !!s.stop_reason && !s.paused);
    if(s.stop_reason) $('#stopbanner').textContent = 'Stopped: ' + s.stop_reason +
      ' — raise a target below to resume.';

    // library quality
    const lp = s.library_lossless_pct;
    $('#llpct').textContent = lp == null ? '–' : lp + '%';
    $('#llbar').style.width = (lp ?? 0) + '%';
    $('#qual').innerHTML = '<tr><th>Codec</th><th>Tracks</th><th>Avg kbps</th><th>Size</th></tr>' +
      (s.library_quality||[]).map(b => `<tr><td>${esc(b.codec)}
        <span class="pill ${b.lossless?'ok':'warn'}">${b.lossless?'lossless':'lossy'}</span></td>
        <td>${b.tracks.toLocaleString()}</td><td>${b.avg_kbps||'–'}</td>
        <td>${(b.bytes/1073741824).toFixed(1)} GB</td></tr>`).join('') ||
      '<tr><td class="muted" colspan="4">No scan yet.</td></tr>';
    const uc = s.upgrade_candidates ?? 0;
    const ups = s.upgrades || {};
    let qnote = uc ? `${uc.toLocaleString()} lossy track(s) — upgrade candidates` +
      (Object.keys(ups).length ? ' · ' + Object.entries(ups).map(([k,v])=>`${k}: ${v}`).join(', ') : '')
      : 'Everything scanned so far is lossless.';
    // A partial scan must not read as a finished one — these numbers are a subset
    // until every file has been probed.
    if(s.scan_unprobed > 0) qnote += ` · scan in progress: ${s.scan_unprobed.toLocaleString()} file(s) not yet analysed`;
    $('#upcand').textContent = qnote;

    // counts
    const C = [['queued_flac','FLAC','ok'],['queued_alac','ALAC','ok'],
      ['queued_lossy','Lossy fallback','warn'],['exists','Already had','' ],
      ['pending','Pending',''],['no_source','No source',''],['no_meta','No metadata',''],
      ['low_quality_only','Too low quality','warn'],['corrupt_only','Corrupt only','bad'],
      ['error','Errors','bad']];
    $('#counts').innerHTML = C.map(([k,lbl,cls]) => `<tr><td>${lbl}</td>
      <td style="text-align:right"><span class="pill ${(s[k]&&cls)||''}">${(s[k]??0).toLocaleString()}</span></td></tr>`).join('');

    // decode gate
    const dg = s.decode_gate || null;
    if(dg){
      $('#dgwarn').classList.toggle('on', !!dg.checker_warning);
      if(dg.checker_warning){
        $('#dgwarn').textContent = 'Warning: ' + (dg.checker_warning_reason || 'decode checker unavailable');
      }
      $('#dgacc').textContent = dg.accepted_24h == null ? '–' : dg.accepted_24h.toLocaleString();
      const r24 = dg.rejects_24h ?? 0, r7 = dg.rejects_7d ?? 0;
      $('#dgrej').innerHTML = `<span class="pill ${r24?'bad':''}">${r24.toLocaleString()}</span> / <span class="pill ${r7?'warn':''}">${r7.toLocaleString()}</span>`;
      $('#dgmatch').textContent = `${(dg.matched_7d??0).toLocaleString()} / ${(dg.unmatched_7d??0).toLocaleString()}`;
      const bp = dg.blocked_peers ?? 0;
      $('#dgpeers').innerHTML = `<span class="pill ${bp?'warn':''}">${bp.toLocaleString()}</span>`;
      const reasons = Object.entries(dg.by_reason_7d || {}).sort((a,b) => b[1] - a[1]);
      const r24map = dg.by_reason_24h || {};
      $('#dgreasons').innerHTML = reasons.length
        ? '<tr><th>Reason</th><th style="text-align:right">24h / 7d</th></tr>' +
          reasons.map(([k,v]) => `<tr><td>${esc(k)}</td>
            <td style="text-align:right"><span class="pill ${k==='checker_unavailable'?'bad':'warn'}">${(r24map[k]||0).toLocaleString()} / ${v.toLocaleString()}</span></td></tr>`).join('')
        : '<tr><td class="muted">No corrupt files rejected in the last 7 days.</td></tr>';
    } else {
      $('#dgwarn').classList.remove('on');
      $('#dgacc').textContent = '–';
      $('#dgrej').textContent = '–';
      $('#dgmatch').textContent = '–';
      $('#dgpeers').textContent = '–';
      $('#dgreasons').innerHTML = '<tr><td class="muted">No decode gate data yet.</td></tr>';
    }

    // favorites + new releases
    renderFavs(s.favorites, fav.names);
    const nrw = s.new_release_watch || null;
    if(nrw && nrw.watched_artists){
      $('#nrwatch').textContent = `Watch: ${(nrw.synced_recent??0).toLocaleString()}/${nrw.watched_artists.toLocaleString()} synced · ` +
        `${(nrw.mbids_cached??0).toLocaleString()} MBIDs cached · ` +
        `${(nrw.releases_30d??0).toLocaleString()} found (30d)`;
    } else {
      $('#nrwatch').textContent = '';
    }
    $('#newrel').innerHTML = (s.new_releases||[]).length
      ? (s.new_releases||[]).map(n => `<tr><td><b>${esc(n.artist)}</b><br>
          <span class="sub">${esc(n.album)}</span></td>
          <td style="text-align:right" class="sub">${esc(n.released||'')}</td></tr>`).join('')
      : '<tr><td class="muted">Nothing new yet. Favorites and top played artists are checked on their sync cadence.</td></tr>';

    // fallbacks
    $('#fb').innerHTML = (s.recent_fallbacks||[]).length
      ? '<tr><th>Artist</th><th>Album</th><th>Got</th></tr>' + (s.recent_fallbacks||[])
        .map(f => `<tr><td>${esc(f.artist)}</td><td>${esc(f.album)}</td>
          <td><span class="pill ${f.lossless?'ok':'warn'}">${esc(f.format)} ${f.kbps||''}</span></td></tr>`).join('')
      : '<tr><td class="muted">No fallbacks — everything so far was lossless.</td></tr>';

    // artists
    const excSet = new Set(exc.names.map(n=>n.toLowerCase()));
    $('#artists').innerHTML = '<tr><th>Artist</th><th>Weight</th><th></th></tr>' +
      arts.artists.map(a => `<tr><td>${esc(a.artist)}
        ${a.favorite?'<span class="pill fav">favorite</span>':''}
        ${a.excluded?'<span class="pill bad">excluded</span>':''}</td>
        <td>${a.weight}</td>
        <td style="text-align:right">${a.excluded?'':
          `<button class="ghost" data-exc="${esc(a.artist)}" style="padding:2px 7px">exclude</button>
           <button class="ghost" data-favadd="${esc(a.artist)}" style="padding:2px 7px">★</button>`}</td></tr>`).join('');
    document.querySelectorAll('[data-exc]').forEach(b => b.onclick = async () => {
      await post('/api/exclude', {names: [...exc.names, b.dataset.exc]});
      toast('Excluded ' + b.dataset.exc); load();
    });
    document.querySelectorAll('[data-favadd]').forEach(b => b.onclick = async () => {
      if(excSet.has(b.dataset.favadd.toLowerCase())) return toast('That artist is excluded.');
      await post('/api/favorites', {names: [...fav.names, b.dataset.favadd]});
      toast('Favorited ' + b.dataset.favadd); load();
    });

    // excludes
    $('#exclist').innerHTML = '<div class="chips">' + exc.names.map(n =>
      `<span class="chip"><b>${esc(n)}</b><button data-rm="${esc(n)}">×</button></span>`).join('') + '</div>';
    document.querySelectorAll('[data-rm]').forEach(b => b.onclick = async () => {
      await post('/api/exclude', {names: exc.names.filter(x => x !== b.dataset.rm)});
      toast('Removed ' + b.dataset.rm); load();
    });

    buildSettings(s.config || {});
    $('#log').textContent = lg.lines.join('\n');
    $('#log').scrollTop = $('#log').scrollHeight;
  }catch(e){ $('#hdr').textContent = 'error: ' + e.message; }
}

$('#save').onclick = async () => {
  const body = {};
  document.querySelectorAll('[data-k]').forEach(el => body[el.dataset.k] = el.value);
  try{ const r = await post('/api/config', body);
    $('#savenote').textContent = r.note || ''; toast('Saved — applies next loop'); }
  catch(e){ toast('Error: ' + e.message); }
};
$('#pause').onclick = async () => {
  const paused = $('#pause').textContent === 'Resume';
  try{ await post(paused ? '/api/resume' : '/api/pause');
    settingsBuilt = false;                  // PAUSED changed underneath the form
    toast(paused ? 'Resumed' : 'Paused'); load(); }
  catch(e){ toast('Error: ' + e.message); }
};
$('#refresh').onclick = async () => { await post('/api/refresh-taste'); toast('Taste refresh queued'); };
$('#syncfav').onclick = async () => { await post('/api/sync-favorites'); toast('Favorites sync queued'); };
$('#scan').onclick = async () => {
  try{ const r = await post('/api/scan-upgrades'); toast(r.note || 'Upgrade scan queued'); }
  catch(e){ toast(e.message); }
};
$('#favadd').onclick = async () => {
  const v = $('#favin').value.trim(); if(!v) return;
  const cur = (await api('/api/favorites')).names;
  await post('/api/favorites', {names: [...cur, v]});
  $('#favin').value = ''; toast('Favorited ' + v); load();
};
$('#excadd').onclick = async () => {
  const v = $('#excin').value.trim(); if(!v) return;
  const cur = (await api('/api/exclude')).names;
  await post('/api/exclude', {names: [...cur, v]});
  $('#excin').value = ''; toast('Excluded ' + v); load();
};
$('#favin').addEventListener('keydown', e => { if(e.key === 'Enter') $('#favadd').click(); });
$('#excin').addEventListener('keydown', e => { if(e.key === 'Enter') $('#excadd').click(); });

load(); setInterval(load, 5000);
</script></body></html>
"""
