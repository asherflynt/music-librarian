# Soulbeet Beets Configuration & Decode Gate

This directory contains the Soulbeet side of the [decode gate](../docs/decode-gate.md) and beets 2.11 configuration:

- [`beets-plugins/decodecheck.py`](beets-plugins/decodecheck.py) — beets plugin hooking `import_task_created` to full-decode every audio file with `ffmpeg` before Soulbeet/beets imports it. Any decode error, truncated stream, missing lossless duration, timeout, or checker failure drops the entire task, moves all of its files into quarantine, and appends one JSONL line per bad file to `rejects.jsonl` (under `fcntl.flock`) so librarian can block the offending peer and re-queue.
- [`beets-config.yaml.template`](beets-config.yaml.template) — replacement for `/mnt/user/appdata/soulbeet/config/config.yaml` enabling `musicbrainz`, `fetchart`, `embedart`, and `decodecheck`, plus emoji-stripping path sanitization rules.
- [`install.sh`](install.sh) — host-side installer for Unraid (bash + `awk`/`grep`/`cut`; no `python3` required).

## Install (on Unraid as root)

Preview the rendered config first (`LASTFM_API_KEY` is masked):

```bash
bash soulbeet/install.sh --dry-run
```

Install and validate:

```bash
bash soulbeet/install.sh
```

What `install.sh` does:
1. Reads `LASTFM_API_KEY` from `/mnt/user/appdata/librarian/secrets.env` without sourcing it.
2. Backs up `/mnt/user/appdata/soulbeet/config/config.yaml` to `config.yaml.bak-<UTC timestamp>`.
3. Installs `decodecheck.py` into `/mnt/user/appdata/soulbeet/data/beets-plugins/`.
4. Renders `beets-config.yaml.template` into `/mnt/user/appdata/soulbeet/config/config.yaml` (`0600`).
5. Ensures `/mnt/cluster/Media/slskd_downloads/.decodecheck/` exists.
6. Validates with `docker exec soulbeet beet -c /config/config.yaml version` (confirming `musicbrainz`, `fetchart`, `embedart`, `decodecheck` all load) and `... beet -c /config/config.yaml config`. On any failure it restores the previous config and plugin and exits non-zero.

Re-running is safe: if the rendered config is unchanged, no new backup is made.

The `-c` matters: beets itself ignores `BEETS_CONFIG` (Soulbeet reads it and passes `-c` to every import). A bare `docker exec soulbeet beet version` reads `/tmp/.config/beets/config.yaml` and shows only `musicbrainz`.

No container restart is needed. Soulbeet starts a fresh `beet` process for each import, and that process reads `/config/config.yaml` and `/data/beets-plugins/` when it starts.

## Rollback

```bash
bash soulbeet/install.sh --rollback
```

This restores the newest `config.yaml.bak-*` (by mtime). If the restored config does not load `decodecheck`, it also removes `/mnt/user/appdata/soulbeet/data/beets-plugins/decodecheck.py`. It then re-runs `beet -c /config/config.yaml version`.

To bypass the decode gate without rolling back, set `decodecheck: {enabled: no}` in `/mnt/user/appdata/soulbeet/config/config.yaml`.

## State, Rejects & Quarantine

All state lives in `/mnt/cluster/Media/slskd_downloads/.decodecheck/` (mounted inside Soulbeet as `/downloads/.decodecheck/` and read-only inside librarian as `/cluster/Media/slskd_downloads/.decodecheck/`):

- `rejects.jsonl` — append-only log, one JSON line per rejected audio file (schema in [`docs/decode-gate.md`](../docs/decode-gate.md)).
- `checked.jsonl` — append-only log, one compact summary line per accepted task (including cosmetic magic mismatches).
- `quarantine/<YYYY-MM-DD>/<task_id>/<relative path>` — all files (audio and non-audio) moved out of staging for each rejected task. Dated folders older than `quarantine_days` (default `14`) are pruned automatically at most once per hour.
