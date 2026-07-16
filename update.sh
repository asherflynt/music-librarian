#!/bin/bash
# One-command update: pull latest code, rebuild, and RECREATE the container.
#
# IMPORTANT: `docker restart` is NOT sufficient and was a real bug here. It restarts
# the existing container using the image it was originally created from, so a freshly
# rebuilt image is silently ignored -- the container keeps running stale code while
# `docker images` shows the new one. The container must be removed and re-run.
#
# Host-specific settings (URLs, mount paths) live in deploy.env next to your config,
# NOT in this repo. Copy deploy.env.template to <config-dir>/deploy.env and edit.
set -e
cd "$(dirname "$0")"

CONFIG_DIR="${CONFIG_DIR:-/mnt/user/appdata/librarian}"
[ -f "$CONFIG_DIR/deploy.env" ] && . "$CONFIG_DIR/deploy.env"

# Defaults are placeholders; deploy.env supplies the real values.
SOULBEET_URL="${SOULBEET_URL:-http://soulbeet:9765}"
NAVIDROME_URL="${NAVIDROME_URL:-http://navidrome:4533}"
MUSIC_DIR="${MUSIC_DIR:-/mnt/user/Media/Music}"
CLUSTER_DIR="${CLUSTER_DIR:-/mnt/cluster}"
CACHE_DIR="${CACHE_DIR:-/mnt/cache}"
TARGET_FOLDER="${TARGET_FOLDER:-/music}"

echo ">> git pull"
git pull --ff-only

echo ">> docker build"
docker build -t librarian:latest .

echo ">> recreating container (a plain restart would keep the OLD image)"
docker rm -f librarian >/dev/null 2>&1 || true
docker run -d --name librarian --restart unless-stopped \
  -e SOULBEET_URL="$SOULBEET_URL" \
  -e NAVIDROME_URL="$NAVIDROME_URL" \
  -e MUSIC_PATH=/music \
  -e FREE_SPACE_PATH=/cluster \
  -e STAGING_PATH=/cache \
  -e TARGET_FOLDER="$TARGET_FOLDER" \
  -v "$CONFIG_DIR":/config \
  -v "$CONFIG_DIR/state":/data \
  -v "$MUSIC_DIR":/music:ro \
  -v "$CLUSTER_DIR":/cluster:ro \
  -v "$CACHE_DIR":/cache:ro \
  librarian:latest >/dev/null

# Guard against the exact failure this script exists to prevent: verify the running
# container is actually built from the image we just made.
sleep 2
want=$(docker images -q librarian:latest)
got=$(docker inspect librarian -f '{{.Image}}' | sed 's/^sha256://' | cut -c1-12)
if [ "$want" = "$got" ]; then
  echo ">> OK: running container matches the freshly built image ($want)"
else
  echo ">> ERROR: container is running $got but latest image is $want" >&2
  exit 1
fi
echo ">> done. Runtime config/secrets in $CONFIG_DIR are untouched."
