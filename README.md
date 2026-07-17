# Music Librarian

Self-hosted, taste-driven, resumable FLAC-preferred auto-fill for your Navidrome
library. Runs as a Docker container on the Unraid box — no Claude Code / browser
dependency. Seeds from what your household actually plays (Navidrome + Plex/Plexamp
play counts, YouTube Music Takeout history), expands via Last.fm (taste-weighted +
horizon), downloads through the existing Soulbeet API with a FLAC-preferred /
high-quality-lossy fallback ladder, keeps favorited artists' full discographies
current via MusicBrainz, and can re-acquire lossy albums as FLAC on a schedule.
State is in SQLite, so restarts resume exactly where they left off.

## Web UI

**http://\<server\>:8730** — progress, real library quality breakdown, download stats,
favorites, new releases, the taste ranking, and every tunable setting.

> **LAN only, no auth**, by design — same posture as slskd/Soulbeet/Navidrome on this
> network. **Do not route it through the Cloudflared tunnel**: it can change download
> targets and, with upgrades enabled, cause files to be deleted.

## Layout on the server

```
/mnt/user/appdata/librarian/
  secrets.env      # LASTFM_API_KEY, SOULBEET_USER/PASS, TAUTULLI_API_KEY (you fill in)
  config.env       # live-tunable settings               (edit in the UI or here)
  exclude.txt      # kids-music artist blocklist         (edit in the UI or here)
  favorites.txt    # full-discography artists            (edit in the UI or here)
  takeout/         # drop Google Takeout YT Music history JSON here
  state/state.db   # SQLite progress                     (auto)
  state/status.json# live status snapshot                (auto)
  upgrade-swap.log # every verified swap + deletion      (auto)
/mnt/user/Media/Music/_upgrades/   # FLAC upgrade staging, pre-verification (auto)
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

Everything below is editable in the **UI** (which writes `config.env` and preserves
its comments) or by editing the file directly. Changes take effect on the next loop —
no restart.

- **Grow / shrink**: `TARGET_TB` / `TARGET_TRACKS`. Raise → resumes; lower → stops cleanly.
- **More discovery vs. more of the same**: `EXPLORE_RATIO` (0.0–1.0).
- **Pause without stopping**: `PAUSED=1`, or the Pause button.
- **Recency**: `TASTE_HALF_LIFE_DAYS` (default 365) — a play that old counts half as much.
- **Source trust**: `WEIGHT_NAVIDROME` / `WEIGHT_PLEX` / `WEIGHT_YTMUSIC`.
- **Exclude kids' music**: add artists in the UI or to `exclude.txt`.
- **Favorite an artist**: star them in Navidrome, click ★ in the UI, or add to `favorites.txt`.

## Play tracking

Three signals, each **recency-weighted** by exponential decay (`TASTE_HALF_LIFE_DAYS`,
default 365 — a play a year old counts half as much as one today), then combined by
the `WEIGHT_*` knobs:

| Source | How | Decay granularity |
|---|---|---|
| **Navidrome** | Subsonic `getAlbumList2?type=frequent` | per **album** — Subsonic exposes no per-play timestamps, only `playCount` + last-`played` |
| **Plex / Plexamp** | Tautulli `get_history` | per **play** (each row has a unix timestamp) |
| **YouTube Music** | Takeout `watch-history.json` | per **listen** (each entry has an ISO-8601 `time`) |

**Plexamp needs Tautulli.** Plexamp is a Plex client, so its plays are recorded by
Plex Media Server and *never reach Navidrome* — polling Navidrome alone misses them
entirely. Set `TAUTULLI_API_KEY` in `secrets.env` to count them. It's optional: absent,
the Plex signal is simply skipped, never an error.

There is **no double-counting** between Navidrome and Plex: a play is recorded by
whichever server actually served the audio, never both.

## Favorites — full discography + new releases (MusicBrainz)

A favorited artist gets their **entire discography** queued (at `FAVORITE_PRIORITY`, so
ahead of the exploration crawl) and is re-checked every `FAVORITE_SYNC_HOURS` forever,
so **new releases are picked up automatically**.

**Why MusicBrainz and not Last.fm.** They answer different questions, and each is
useless at the other's:

- **Last.fm → discovery.** `getSimilar` / `tag.getTopAlbums` answer *"who else might I
  like"*. MusicBrainz has no similarity data at all, so Last.fm is **not** replaceable
  here and remains the recommendation engine.
- **MusicBrainz → discography.** Canonical release-groups with **release dates and
  release types**. Last.fm's `getTopAlbums` returns *neither*, so it cannot detect a
  new release or exclude a live bootleg — it is structurally unable to do this job.

Release-type filtering matters more than it sounds. Glass Animals has **75**
release-groups; only **7** are real albums/EPs:

```
FAVORITE_INCLUDE_EP=1            ->  4 albums + 3 EPs        = 7   (default)
FAVORITE_INCLUDE_SINGLES=1       -> +29 singles              = 36
FAVORITE_INCLUDE_REMIX=1         -> +26 remix singles
FAVORITE_INCLUDE_LIVE=1          -> +5  live broadcasts
FAVORITE_INCLUDE_COMPILATIONS=1  -> +3  compilations
```

MusicBrainz enforces **1 request/sec**, and throttling returns `200` with an *empty
body* rather than an error — so the client rate-limits at 1.1 s and treats a missing
expected key as a retryable failure, never as "this artist has no releases" (which
would permanently mark a discography complete).

**`exclude.txt` wins over `favorites.txt`.** Both are deliberate acts, but the blocklist
is the safety-oriented one. An artist in both is skipped and the conflict is logged.

## Scheduled lossy → FLAC upgrades

Off by default (`UPGRADE_ENABLED=0`). When on, one bounded pass runs daily at
`UPGRADE_HOUR`: it takes up to `UPGRADE_MAX_PER_RUN` albums that `mutagen` classified
as **lossy**, searches Soulseek, and stages a FLAC **only** if a true FLAC exists
(never a lossy→lossy sidegrade). `UPGRADE_ONLY_WHEN_IDLE=1` keeps it from competing
with the main fill for Soulseek slots, and `UPGRADE_RECHECK_DAYS` stops it re-asking
about the same album.

Staging goes to `<music>/_upgrades` — **inside** Soulbeet's existing `/music` mount.
That's not arbitrary: `/api/downloads/queue` does `create_dir_all(target_folder)`
*inside the Soulbeet container*, so a path outside its mounts (a bare `/upgrades`)
would be created in its writable layer — invisible to the host and discarded on
recreate, with no error. The staging dir is excluded from the library scan and carries
a `.ndignore` so Navidrome doesn't index half-verified albums.

### The swap, and why it deletes

`upgrade-swap.sh` (User Script, every 30 min) verifies each staged album against **four
gates**, and touches nothing unless *all* pass:

1. every audio file is `.flac`
2. FLAC track count **≥** the existing album's
3. **every file decodes cleanly** (`ffmpeg -v error -i … -f null -`) — catches the
   truncated/corrupt transfers Soulseek routinely produces
4. total duration within **5%** of the existing album — catches wrong-edition rips

Only then does it `beet remove --delete` the old album and `beet move` the FLAC in,
and only after *confirming the replacement landed* does it drop the staged copy.
Configured to **delete** the old lossy copy; set `ARCHIVE_DIR` in the script to move
it aside instead. Every swap and deletion is logged to `upgrade-swap.log`.

> Gate 3 is the one that matters: without a real decode check, a truncated FLAC passes
> a header check happily, and you'd trade a good MP3 for an unplayable file.

`flac -t` isn't used because no `flac` binary exists on the host *or* in the soulbeet
container — and the soulbeet image has **no shell at all**, so everything is exec'd as
a direct binary (`ffmpeg`, `ffprobe`, `beet`) with file enumeration done host-side.

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

For files **already on disk** the guess isn't needed: `mutagen` reports the real codec
(`alac` vs `mp4a.40.2`), which is what the library quality stats and the upgrade
scanner use. Verified against this library — all 16 `.m4a` files are AAC at 256–320
kbps, correctly classified as lossy rather than mistaken for lossless ALAC.

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
