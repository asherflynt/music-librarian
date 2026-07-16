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

## Status

`cat /mnt/user/appdata/librarian/state/status.json` or `docker logs librarian` — shows
library TB, track count, % to target, free space, and FLAC-vs-lossy counts.
