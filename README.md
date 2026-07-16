# Music Librarian

Self-hosted, taste-driven, resumable FLAC-preferred auto-fill for your Navidrome
library. Runs as a Docker container on the Unraid box — no Claude Code / browser
dependency. Seeds from what your household actually plays (Navidrome play counts +
YouTube Music Takeout history), expands via Last.fm (taste-weighted + horizon), and
downloads through the existing Soulbeet API with a FLAC-preferred / high-quality-lossy
fallback ladder. State is in SQLite, so restarts resume exactly where they left off.

## Layout on the server

```
/mnt/user/appdata/librarian/
  secrets.env      # LASTFM_API_KEY, SOULBEET_USER/PASS  (you fill in)
  config.env       # live-tunable targets/ratios         (edit anytime)
  exclude.txt      # kids-music artist blocklist          (edit anytime)
  takeout/         # drop Google Takeout YT Music history JSON here
  state.db         # SQLite progress                      (auto)
  status.json      # live status snapshot                 (auto)
```

## Build & run (on Unraid)

```
docker build -t librarian /mnt/user/appdata/librarian/build
docker run -d --name librarian --restart unless-stopped \
  -e SOULBEET_URL=http://<unraid-ip>:9765 \
  -e NAVIDROME_URL=http://<unraid-ip>:4533 \
  -e MUSIC_PATH=/music -e FREE_SPACE_PATH=/music -e TARGET_FOLDER=/music \
  -v /mnt/user/appdata/librarian:/config \
  -v /mnt/user/appdata/librarian/state:/data \
  -v /mnt/user/Media/Music:/music:ro \
  librarian
```

`MUSIC_PATH` is mounted read-only just for size/track measurement; downloads land in
the library via Soulbeet/beets (which has its own writable mount).

## Adjusting

- **Grow / shrink**: edit `TARGET_TB` or `TARGET_TRACKS` in `config.env`. Raise → resumes; lower → stops cleanly. Takes effect next loop.
- **More discovery vs. more of the same**: `EXPLORE_RATIO` (0.0–1.0).
- **Pause without stopping**: `PAUSED=1`.
- **Refresh taste**: re-export YT Music history from Takeout into `takeout/`; Navidrome play counts update automatically.
- **Exclude more kids' music**: add artists to `exclude.txt`.

## Quality ladder (FLAC preferred, never required)

For each album/track, best available wins:

| Tier | Format | Notes |
|---|---|---|
| 1 | **FLAC** | lossless |
| 2 | **ALAC** (`.m4a` ≥ `ALAC_MIN_KBPS`, default 500) | also lossless — ranked above *any* lossy source, even a higher-scoring one |
| 3 | **High-quality lossy** (`mp3`/`aac`/`.m4a`-as-AAC) at `quality_score ≥ 0.55` (~320 kbps / V0) | 320 and V0 preferred |
| — | skipped | `low_quality_only` (only sub-threshold lossy) or `no_source` |

**The `.m4a` trap:** `.m4a` is a *container* used by both ALAC (lossless) and AAC
(lossy), and the backend only reports the file **extension**, not the codec — so
"prefer m4a" would silently pull lossy AAC most of the time. Bitrate is the real
discriminator: ALAC runs ~600–900 kbps, AAC caps ~320. The librarian computes
effective bitrate from `size`/`duration` and only calls an `.m4a` lossless if it
clears `ALAC_MIN_KBPS`. Tune via the `ALAC_MIN_KBPS` env var.

**Every fallback is documented.** When no FLAC exists, the log records what it
landed on and why:

```
FALLBACK [Artist - Album]: no FLAC source -> ALAC (.m4a @ ~874 kbps, lossless)
FALLBACK [Artist - Album]: no lossless source -> mp3 @ ~336 kbps (lossy)
SKIPPED  [Artist - Album]: only sub-threshold lossy sources
```

and `status.json` carries per-item detail plus a running lossless ratio:

```json
"queued_flac": 412, "queued_alac": 9, "queued_lossy": 63,
"lossless_total": 421, "lossless_pct": 87.0,
"recent_fallbacks": [
  {"artist": "...", "album": "...", "format": "mp3", "kbps": 336, "lossless": false}
]
```

The SQLite `candidates` table keeps `fmt` + `kbps` for every acquisition, so you can
always audit exactly what is lossless vs. fallback.

## Status

`cat /mnt/user/appdata/librarian/state/status.json` or `docker logs librarian` — shows
library TB, track count, % to target, free space, and FLAC-vs-lossy counts.

## The rescue sweep (deliberately NOT in this container)

Soulbeet's `DownloadMonitor` doesn't reliably trigger a beets import for every
completed download under sustained concurrent load — fully-downloaded albums can
sit in slskd's staging dir with no import ever attempted. A periodic sweep re-runs
`beet import` against that staging dir to catch them.

That sweep intentionally lives **outside** this container, as an Unraid User Script
(`librarian-rescue-sweep`, every 20 min) that calls
`docker exec soulbeet beet import ...`.

Why not run beets in this container? Because it would mean a *second* beets install
writing the same `.beets_library.db` as Soulbeet's. Pinning versions to match only
papers over that — if Soulbeet's image ever bumps beets, two different versions would
share one library db, which beets does not support. Calling Soulbeet's own beets via
`docker exec` uses the canonical version and config, with zero drift.

Why not have this container `docker exec`? That needs `/var/run/docker.sock` mounted
in, granting root-equivalent host control to a container that parses external data
(Takeout JSON) and talks to third-party APIs. Not worth it for a convenience feature.

The host-side User Script avoids both problems, and runs on its own schedule
independent of this container's auth/paused state.
