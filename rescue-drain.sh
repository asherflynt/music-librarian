#!/bin/bash
# Librarian staging drainer -- host User Script, companion to librarian-rescue-sweep.
#
# Runs drain-staging.py INSIDE the soulbeet container (the only place with beets,
# mutagen, and both /downloads and /music mounted), copying the current script in
# fresh from the librarian repo each run so there is a single source of truth --
# no second beets install, no version drift, no docker.sock exposed to a container.
#
# Belt-and-suspenders against stranded downloads: it imports anything genuinely new
# (like the sweep) AND additionally deletes staging folders it can PROVE are already
# in the library (every track resolves to a library item whose file exists on disk),
# plus empty folders. It never deletes on a guess.
#
# Shares the rescue-sweep lock so two `beet import` runs never race the one library db.
#
# Install: /boot/config/plugins/user.scripts/scripts/librarian-drain-staging/script
# Schedule: every 6h (customSchedule.cron).

set -u
SRC=/mnt/user/appdata/librarian/build/drain-staging.py

if ! docker ps --format '{{.Names}}' | grep -qx soulbeet; then
  echo "soulbeet container not running; skipping drain"
  exit 0
fi

if [ ! -f "$SRC" ]; then
  echo "drain-staging.py not found at $SRC; skipping"
  exit 1
fi

# Copy the canonical drainer into the container fresh (single source of truth).
docker cp "$SRC" soulbeet:/tmp/drain-staging.py || { echo "docker cp failed"; exit 1; }

# Serialize with the rescue sweep: concurrent beet imports race the one sqlite db.
LOCK=/tmp/librarian-rescue-sweep.lock
exec 9>"$LOCK"
if ! flock -w 600 9; then
  echo "rescue sweep/drain still running after 10 min; skipping this drain"
  exit 0
fi

docker exec soulbeet python3 /tmp/drain-staging.py
