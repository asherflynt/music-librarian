#!/bin/sh
# First-run bootstrap for the /config volume.
#
# A Community Applications install starts with an EMPTY /config. The librarian is
# file-driven (config.env, secrets.env, exclude.txt, favorites.txt live in /config
# and are editable in the web UI), so on a clean install there is nothing to read.
# This seeds any file that isn't already there from the defaults baked into the
# image, then hands off to the app. It is idempotent: an existing file is never
# overwritten, so your edits — and the comments the UI preserves — always survive an
# image update.
set -e

DEFAULTS=/app/defaults
CONFIG_DIR="${LIBRARIAN_CONFIG:-/config}"
DATA_DIR="${LIBRARIAN_DATA:-/data}"

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$CONFIG_DIR/takeout"

seed() {
  # seed <source-in-defaults> <dest-name-in-config>
  if [ ! -e "$CONFIG_DIR/$2" ]; then
    cp "$DEFAULTS/$1" "$CONFIG_DIR/$2"
    echo "seed: created $CONFIG_DIR/$2"
  fi
}

seed config.env            config.env
seed exclude.txt           exclude.txt
seed favorites.txt         favorites.txt
# secrets.env ships as a template with blank values. The app waits (it does not
# crash) until you fill LASTFM_API_KEY / SOULBEET_USER / SOULBEET_PASS — edit the
# file in the appdata share or paste them in the web UI.
seed secrets.env.template  secrets.env

if [ ! -s "$CONFIG_DIR/secrets.env" ] || ! grep -q '[A-Za-z0-9]=[^[:space:]]' "$CONFIG_DIR/secrets.env" 2>/dev/null; then
  echo "----------------------------------------------------------------------"
  echo " Fill in $CONFIG_DIR/secrets.env (LASTFM_API_KEY, SOULBEET_USER,"
  echo " SOULBEET_PASS). The librarian will wait here until it is filled —"
  echo " no restart needed once you save it."
  echo "----------------------------------------------------------------------"
fi

exec python -u /app/librarian.py
