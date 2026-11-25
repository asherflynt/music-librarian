#!/bin/bash
# Librarian rescue sweep — deploys to Unraid as a User Script (every 20 min).
#
# Soulbeet's DownloadMonitor doesn't reliably trigger a beets import for every
# completed download under sustained concurrent load: fully-downloaded albums can
# sit in slskd's staging dir with no import ever attempted. This re-runs the exact
# beet import Soulbeet uses internally, via `docker exec` into the soulbeet
# container against Soulbeet's canonical library DB
# (/data/beets-libraries/music/.beets_library.db), so it uses the canonical beets
# version + config (no second beets install, no version drift, no docker.sock
# exposed to a container).
#
# Settle guard: slskd downloads files into /app/incomplete and moves each track
# into /app/downloads as it finishes, so an active album folder accumulates files
# over time. Any folder with a file modified in the last 15 minutes is skipped as
# busy so we never import an incomplete album.
#
# Idempotent: beets recognizes anything already in the library and skips it fast.
# Only imports what's already downloaded; never starts a download.
#
# Install: /boot/config/plugins/user.scripts/scripts/librarian-rescue-sweep/script

DL_HOST="/mnt/cluster/Media/slskd_downloads"            # staging, as seen from the host
DL_CTR="/downloads"                                     # same dir, as seen inside soulbeet
BEETS_CFG="/config/config.yaml"                         # soulbeet's beets config (in-container)
LIB_DB="/data/beets-libraries/music/.beets_library.db"  # soulbeet's canonical library db (in-container)
TARGET="/music"                                         # library destination (in-container)
SETTLE_MINUTES=15                                       # skip folders with files modified within this window

if ! docker ps --format '{{.Names}}' | grep -qx soulbeet; then
  echo "soulbeet container not running; skipping sweep"
  exit 0
fi

# A sweep can outlive its 20-min interval on a big backlog. Never let two overlap:
# concurrent `beet import` against one library db races its schema/locking.
LOCK=/tmp/librarian-rescue-sweep.lock
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "a previous sweep is still running; skipping this run"
  exit 0
fi

imported=0; skipped=0; busy=0; empty=0; failed=0; total=0

for d in "$DL_HOST"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  total=$((total + 1))

  if [ -n "$(find "$d" -mmin "-$SETTLE_MINUTES" -type f 2>/dev/null | head -1)" ]; then
    busy=$((busy + 1))
    continue
  fi

  n=$(find "$d" -type f \( -iname '*.flac' -o -iname '*.mp3' -o -iname '*.m4a' \
        -o -iname '*.wav' -o -iname '*.ogg' \) 2>/dev/null | wc -l)
  if [ "$n" -eq 0 ]; then
    empty=$((empty + 1))
    continue
  fi

  # single file -> singleton import (-s); multiple -> album import
  if [ "$n" -eq 1 ]; then
    out=$(docker exec soulbeet beet -c "$BEETS_CFG" -l "$LIB_DB" -d "$TARGET" \
            import -q -s "$DL_CTR/$name" 2>&1); rc=$?
  else
    out=$(docker exec soulbeet beet -c "$BEETS_CFG" -l "$LIB_DB" -d "$TARGET" \
            import -q "$DL_CTR/$name" 2>&1); rc=$?
  fi

  if echo "$out" | grep -q "already in the library"; then
    skipped=$((skipped + 1))
  elif [ "$rc" -eq 0 ]; then
    imported=$((imported + 1))
    echo "imported: $name ($n files)"
  else
    failed=$((failed + 1))
    echo "FAILED [$name]: $(echo "$out" | tail -2)"
  fi
done

echo "Rescue sweep done: $imported imported, $skipped already-present, $busy busy, $empty empty, $failed failed (of $total folders)"
