#!/usr/bin/env python3
"""Drain slskd's staging dir: import what's new, delete what's a verified duplicate.

Runs INSIDE the soulbeet container (`docker exec soulbeet python3 /music/drain-staging.py`),
which is the only place with beets, mutagen, and both /downloads and /music mounted.

Why this exists
---------------
beets' default is `copy`, and soulbeet's config never overrode it, so every album it
imported was copied into the library and the original left in staging forever. That
accumulated ~476 GB across ~2,000 folders, filled the NVMe cache to 95%, and tripped
the librarian's own staging guard -- so downloads stopped. `move: yes` is now set,
which stops it happening again, but the existing backlog still has to be cleared.

Safety
------
A staged folder is deleted ONLY when the library provably already contains that album:
every one of its tracks resolves to a library item whose file EXISTS on disk. If that
can't be proven, the folder is handed to beets to import (which now moves it, draining
staging as a side effect). Anything still unresolved is left alone and reported --
never deleted on a guess.

Throttled on purpose: all of this I/O crosses Unraid's /mnt/user FUSE layer, which is
a single shared chokepoint. Hammering it is believed to be what wedged this box on
2026-07-17.

    python3 drain-staging.py --dry-run     # report only, touch nothing
    python3 drain-staging.py               # do it
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata

DL = "/downloads"
LIB_DB = "/music/.beets_library.db"
LIB_DIR = "/music"
BEETS_CFG = "/config/config.yaml"
AUDIO = (".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".wma")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def norm(s):
    """Loose compare: case/punctuation/accents differ between tags and the db."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return "".join(c for c in s if c.isalnum() or c == " ").strip()


def audio_files(d):
    out = []
    try:
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            if os.path.isfile(p) and os.path.splitext(f)[1].lower() in AUDIO:
                out.append(p)
    except OSError:
        pass
    return out


def dir_bytes(d):
    t = 0
    for root, _, fs in os.walk(d):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return t


def read_tags(path):
    from mutagen import File as MF
    try:
        au = MF(path, easy=True)
        if not au or not au.tags:
            return None, None, None
        g = lambda k: (au.tags.get(k) or [None])[0]
        return g("albumartist") or g("artist"), g("album"), g("title")
    except Exception:
        return None, None, None


class Library:
    """Read-only view of the beets library, indexed for fast lookup."""

    def __init__(self):
        # Read-only: this db is live and soulbeet writes to it. We must never lock it.
        self.conn = sqlite3.connect(f"file:{LIB_DB}?mode=ro", uri=True, timeout=30)
        self.by_album = {}
        for path, aa, ar, al, ti in self.conn.execute(
                "SELECT path, albumartist, artist, album, title FROM items"):
            p = path.decode("utf-8", "surrogateescape") if isinstance(path, bytes) else str(path)
            # beets stores paths RELATIVE to the library root ("Artist/Album/01 x.flac"),
            # not absolute. Checking them as-is resolves against the cwd, so every
            # existence check silently fails and no duplicate is ever confirmed --
            # which would have re-imported the entire 2,000-folder backlog instead.
            if not p.startswith("/"):
                p = os.path.join(LIB_DIR, p)
            for artist in (aa, ar):
                if not artist or not al:
                    continue
                self.by_album.setdefault((norm(artist), norm(al)), []).append((p, norm(ti)))

    def verify(self, artist, album, titles):
        """True only if every staged track is in the library AND its file is on disk."""
        items = self.by_album.get((norm(artist), norm(album)))
        if not items:
            return False, "album not in library"
        live = {t for p, t in items if os.path.exists(p)}
        if not live:
            return False, "library rows exist but their files are missing"
        missing = [t for t in titles if norm(t) not in live]
        if missing:
            return False, f"{len(missing)}/{len(titles)} track(s) not in library"
        return True, f"all {len(titles)} track(s) present on disk"


def beets_import(folder, singleton):
    cmd = ["beet", "-c", BEETS_CFG, "-l", LIB_DB, "-d", LIB_DIR, "import", "-q", "-m"]
    if singleton:
        cmd.append("-s")
    cmd.append(folder)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return r.returncode, (r.stdout + r.stderr)[-300:]
    except subprocess.TimeoutExpired:
        return 1, "timed out after 300s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N folders")
    ap.add_argument("--sleep", type=float, default=0.15,
                    help="pause between folders; protects the FUSE layer")
    args = ap.parse_args()

    log("indexing beets library…")
    lib = Library()
    log(f"library: {len(lib.by_album)} (artist, album) key(s)")

    folders = sorted(d for d in os.listdir(DL) if os.path.isdir(os.path.join(DL, d)))
    log(f"staging: {len(folders)} folder(s){'  [DRY RUN — nothing will change]' if args.dry_run else ''}")

    stats = {"deleted": 0, "imported": 0, "empty": 0, "kept": 0, "failed": 0}
    freed = 0
    for i, name in enumerate(folders, 1):
        if args.limit and i > args.limit:
            break
        d = os.path.join(DL, name)
        files = audio_files(d)

        if not files:
            # No audio at all: nothing to import and nothing to lose.
            stats["empty"] += 1
            if not args.dry_run:
                shutil.rmtree(d, ignore_errors=True)
            continue

        artist, album, _ = read_tags(files[0])
        titles = [read_tags(f)[2] for f in files]
        titles = [t for t in titles if t]

        ok = False
        why = "untagged"
        if artist and album and len(titles) == len(files):
            ok, why = lib.verify(artist, album, titles)

        if ok:
            sz = dir_bytes(d)
            log(f"[{i}/{len(folders)}] DUPLICATE {artist} — {album} ({why}); "
                f"freeing {sz / 1048576:.0f} MB")
            if not args.dry_run:
                shutil.rmtree(d, ignore_errors=True)
            freed += sz
            stats["deleted"] += 1
        else:
            log(f"[{i}/{len(folders)}] IMPORT {name!r} ({why})")
            if not args.dry_run:
                rc, out = beets_import(d, singleton=len(files) == 1)
                if not os.path.isdir(d):
                    stats["imported"] += 1          # moved into the library
                elif not audio_files(d):
                    shutil.rmtree(d, ignore_errors=True)
                    stats["imported"] += 1
                else:
                    # Still here: beets declined (duplicate it won't re-add, or a
                    # failure). Leave it — deleting on a guess is how you lose music.
                    stats["kept" if rc == 0 else "failed"] += 1
                    log(f"    kept in staging: {out.strip()[:120]}")
            else:
                stats["imported"] += 1
        time.sleep(args.sleep)

    log("=" * 62)
    log(f"deleted (verified duplicates): {stats['deleted']}")
    log(f"imported into library:         {stats['imported']}")
    log(f"empty folders removed:         {stats['empty']}")
    log(f"kept (beets declined):         {stats['kept']}")
    log(f"failed:                        {stats['failed']}")
    log(f"space reclaimed:               {freed / 1073741824:.1f} GB")
    if args.dry_run:
        log("DRY RUN — nothing was changed.")


if __name__ == "__main__":
    main()
