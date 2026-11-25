# Decode gate: no corrupt audio enters the library

Corrupt files come from bad Soulseek peers, and Soulbeet/beets imports them with
no decode check. A 2026-11 audit of the library found about 600 damaged tracks
(decode errors, truncated audio, FLACs with no duration). The decode gate stops
new ones at import time, and librarian learns from each rejection so the same
peer is not picked again.

```
slskd download -> /downloads (staging) -> beets import (Soulbeet)
                                              |
                                  decodecheck plugin (import_task_created)
                                     | all files decode clean -> import normally
                                     | any file bad -> whole task dropped,
                                     |                 files moved to quarantine,
                                     |                 one JSON line per bad file
                                     v
             /downloads/.decodecheck/rejects.jsonl  (append-only)
                                     |
                      librarian (reads it via its read-only /cluster mount)
                        -> bad_sources table (peer blocked for that candidate,
                           and globally after N strikes)
                        -> candidate re-queued; the ladder skips blocked peers
```

## Paths (same directory, three views)

| Who | Path |
|---|---|
| Host | `/mnt/cluster/Media/slskd_downloads/.decodecheck/` |
| Soulbeet / beets (`/downloads` mount, rw) | `/downloads/.decodecheck/` |
| Librarian (`/cluster` mount, ro) | `/cluster/Media/slskd_downloads/.decodecheck/` |

- `rejects.jsonl`: append-only, one JSON object per **bad file**. The plugin
  writes it under `fcntl.flock`; librarian only reads it.
- `quarantine/<YYYY-MM-DD>/<task-id>/<original relative path>`: every file of a
  rejected task, the good ones included, so a human can inspect the download. The
  plugin prunes dated folders older than `quarantine_days` (default 14).
- `checked.jsonl`: optional, one line per **accepted** task (counts only), for
  the status page.

## The check, per audio file

1. **Header duration** comes from beets' `item.length` (mutagen stream info).
2. **Full decode:** `ffmpeg -nostdin -v error -progress pipe:1 -i FILE -map 0:a:0 -f null -`.
   - **`-map 0:a:0` is required.** Most FLACs carry embedded cover art as an
     mjpeg "video" stream (attached_pic). Without the map, ffmpeg also outputs
     that one-frame stream and `out_time_us` reports about 0. Every file with
     art would then look truncated. This was verified on real library files on
     2026-11-24: decoded 11 µs without the map, 302 s with it.
   - A file with no audio stream at all is a `decode_error`.
   - Any stderr output, or a non-zero exit code, is a `decode_error` — **except**
     two explicit, conservative allowlists in `decodecheck.py`:
     1. **Non-audio (image/video) decoder contexts** (`IGNORED_NON_AUDIO_CODECS`),
        such as `[mjpeg @ 0x...]`, `[png @ 0x...]`, `[bmp @ 0x...]`,
        `[gif @ 0x...]`, `[webp @ 0x...]`, `[tiff @ 0x...]`, `[jpeg2000 @ 0x...]`,
        `[mjpegb @ 0x...]`, and `[jpegls @ 0x...]`. Even with `-vn`, ffmpeg still
        probes embedded cover art (`attached_pic` streams) during input open and
        can log e.g. `[mjpeg @ 0x...] unable to decode APP fields: Invalid data
        found when processing input` on damaged cover art while the audio decodes
        cleanly (verified 2026-11-25 on live rejects: 44 FLACs across 3 albums
        had `decoded_s == header_s` with only `[mjpeg @ ...]` stderr).
     2. **Metadata / tag-parsing warnings** (`IGNORED_METADATA_WARNINGS` and
        `IGNORED_METADATA_WARNING_PATTERNS`), matched on the exact full line
        (optionally after a single `[<ctx> @ 0x...]` prefix such as
        `[mp3 @ 0x...]`, `[flac @ 0x...]`, `[id3v2 @ 0x...]`, or
        `[mov,mp4,m4a,3gp,3g2,mj2 @ 0x...]`). These cover strictly tag-only
        messages verified in FFmpeg 8.0 `libavformat/` (`id3v2.c`, `apetag.c`,
        `flac_picture.c`, `flacdec.c`, `oggparsevorbis.c`, `mov.c`), such as
        unprefixed `Incorrect BOM value` / `Error reading lyrics, skipped` from
        malformed ID3v2 USLT/COMM text frames (verified 2026-11-25 on live
        rejects: 3 MP3s in *Conversational Lush* had `decoded_s == header_s`
        with only those two lines). `Last message repeated N times` is also
        ignored only when the immediately preceding stderr line was ignored.
     Ignored warnings are recorded in the accepted `checked.jsonl` line
     (`ignored_image_warnings` / `ignored_image_warning_details` and
     `ignored_metadata_warnings` / `ignored_metadata_warning_details`) and never
     reject on their own. Generic errors (`Invalid data found when processing
     input`, `Header missing`, `Error while decoding stream`, audio decoder
     errors like `[flac @ ...]` / `[mp3float @ ...]` / `[aac @ ...]`), any
     non-zero exit code, truncation, and lossless `no_duration` still reject.
   - The last `out_time_us` gives the decoded seconds.
   - **FLAC also gets `-err_detect crccheck`** (before `-i`). Without it ffmpeg
     ignores FLAC frame CRCs, and bit flips inside audio data with intact
     framing decode with no error at all (verified 2026-11-24, ffmpeg 8.0.1).
     Other formats do not get it: MP3 CRCs are optional and cannot be trusted.
3. **Truncation:** if the header length is over 15 s and decoded <
   header - max(5 s, 5%), the reason is `truncated`.
4. **No duration:** if the header length is 0 or missing for a lossless file,
   the reason is `no_duration` (a FLAC with STREAMINFO total_samples=0).
5. **Magic mismatch** (e.g. ID3v2 prepended to a FLAC) is logged in the accepted
   line, never rejected. It is cosmetic.
6. The per-file timeout is `max(120 s, 2 x header length)`. A timeout is
   `decode_timeout` and rejects the file.
7. **Fail closed:** if ffmpeg is missing or cannot run, the reason is
   `checker_unavailable`. The task is rejected and the error is logged loudly.
   "Cannot run" includes a binary that starts but is broken: once per process
   (and again after any checker failure) the plugin decodes 1 s of generated
   silence with the same flags, and a failure there makes the task
   `checker_unavailable`. Exit codes 126/127 and SIGKILL/SIGTERM are also
   `checker_unavailable`, never `decode_error`, so peers are not blamed.
   Librarian re-queues such albums (without using a retry or blocking a peer)
   once `checked.jsonl` shows a later accepted task.
8. **Concurrent import:** if a file of the task disappears during the check
   (another beets process, e.g. rescue-sweep, moved it), the task is dropped
   with no reject line and no quarantine.

## rejects.jsonl line (contract between the plugin and librarian)

```json
{"v": 1, "ts": 1764000000, "task_id": "20261124T163000-ab12cd",
 "reason": "decode_error|truncated|no_duration|decode_timeout|checker_unavailable",
 "detail": "first 300 chars of ffmpeg stderr, or the numbers",
 "header_s": 212.0, "decoded_s": 29.8,
 "staging_path": "/downloads/Some Dir/04 Track.flac",
 "basename": "04 Track.flac",
 "quarantined_to": "/downloads/.decodecheck/quarantine/2026-11-24/<task_id>/Some Dir/04 Track.flac",
 "artist": "tag artist or null", "album": "tag album or null",
 "title": "tag title or null", "albumartist": "...", "track": 4,
 "task_files": 12, "task_bad": 1, "album_mode": true}
```

Librarian matches a reject to the peer that served it by `basename`. It also uses
the staging directory's last component, which slskd names after the remote
folder. Both are compared after undoing slskd's local renames (`\ / : * ? " < > |`
and control characters become `_`, a `_<ticks>` de-duplication suffix is
dropped, Unicode NFC, case-folded). A basename shared by rows for different
albums only blames a peer when the directory or the reject's artist/album tags
pick one; otherwise nobody is blamed. A reject with no basename match can still
re-queue its album (with no peer blamed) by directory or tags. The match goes against the `downloads` rows it recorded when it queued
the items, so the plugin never needs to know the peer.

## Why the whole task is dropped

In album mode, importing the good tracks of a partly bad album leaves an
incomplete album in beets. The later re-download of the full album is then
skipped as a duplicate, so the album stays incomplete forever. Dropping the whole
task keeps an album all-good or absent. Librarian re-requests it from a different
peer.
