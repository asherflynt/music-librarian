#!/bin/bash
# One-command update: pull latest code from GitHub, rebuild the image, restart.
# Run on the Unraid host from inside this cloned repo directory.
set -e
cd "$(dirname "$0")"
echo ">> git pull"
git pull --ff-only
echo ">> docker build"
docker build -t librarian:latest .
echo ">> docker restart librarian"
docker restart librarian
echo ">> done. Runtime config/secrets in the parent /config dir are untouched."
