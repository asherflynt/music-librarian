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
  -e SLSKD_DOWNLOADS_PATH=/downloads -e BEETS_CONFIG_PATH=/soulbeet-config/config.yaml \
  -v /mnt/user/appdata/librarian:/config \
  -v /mnt/user/appdata/librarian/state:/data \
  -v /mnt/user/Media/Music:/music \
  -v /mnt/user/appdata/slskd/downloads:/downloads \
  -v /mnt/user/appdata/soulbeet/config:/soulbeet-config:ro \
  librarian
```

`MUSIC_PATH`/`TARGET_FOLDER` must be **read-write** (not `:ro`) — the rescue sweep
(below) runs `beet import` directly against it, moving files in from staging.

## Rescue sweep

Soulbeet spawns a background task per queued download that's supposed to import it
into the library once every file finishes. Under sustained concurrent load from this
librarian, that task doesn't always reliably fire — downloads complete and sit fully
formed in slskd's staging folder, never imported. Beets itself isn't the problem: every
time it's actually invoked it behaves correctly (imports, or correctly recognizes and
skips duplicates). The gap is upstream, in Soulbeet's own completion-detection.

To cover for that, the librarian periodically re-runs the exact same `beet import`
command Soulbeet uses internally against the whole staging directory
(`SLSKD_DOWNLOADS_PATH`), using Soulbeet's own beets config (`BEETS_CONFIG_PATH`, mounted
read-only). This is safe to run as often as you like — beets recognizes anything already
in the library and skips it almost instantly, so a sweep against a mostly-clean staging
dir is cheap. Controlled by `RESCUE_SWEEP_EVERY_MIN` (default 20 minutes); runs even
while `PAUSED=1` since it only imports what's already downloaded, never starts anything
new.

## Adjusting

- **Grow / shrink**: edit `TARGET_TB` or `TARGET_TRACKS` in `config.env`. Raise → resumes; lower → stops cleanly. Takes effect next loop.
- **More discovery vs. more of the same**: `EXPLORE_RATIO` (0.0–1.0).
- **Pause without stopping**: `PAUSED=1`.
- **Rescue sweep frequency**: `RESCUE_SWEEP_EVERY_MIN` (default 20).
- **Refresh taste**: re-export YT Music history from Takeout into `takeout/`; Navidrome play counts update automatically.
- **Exclude more kids' music**: add artists to `exclude.txt`.

## Status

`cat /mnt/user/appdata/librarian/state/status.json` or `docker logs librarian` — shows
library TB, track count, % to target, free space, FLAC-vs-lossy counts, and the last
rescue sweep's timestamp + imported/skipped/empty/failed counts.
