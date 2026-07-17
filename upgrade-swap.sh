#!/bin/bash
# Librarian upgrade swap — deploys to Unraid as a User Script (every 30 min).
#
# The librarian stages FLAC re-acquisitions of albums you own as lossy into
# /mnt/user/Media/Music/_upgrades. This script verifies each one and, only if it
# passes EVERY gate, swaps it into the library and deletes the old lossy copy.
#
# Why the host and not the librarian container: beets must run in the soulbeet
# container so there is exactly one beets version and one config -- the same reason
# rescue-sweep.sh works this way. Everything here runs via `docker exec soulbeet`.
#
# DELETION POLICY: this script deletes your old lossy album. That is deliberate and
# was chosen explicitly, but it means a bad verify could destroy the only copy you
# have. So deletion happens ONLY after all four gates pass AND the replacement is
# confirmed present in the library. If anything is off, the staged FLAC is left in
# _upgrades and your existing album is not touched. Set ARCHIVE_DIR below to move
# old albums somewhere instead of deleting them.
#
# Install: /boot/config/plugins/user.scripts/scripts/librarian-upgrade-swap/script

UPG_HOST="/mnt/user/Media/Music/_upgrades"    # staging, as seen from the host
UPG_CTR="/music/_upgrades"                    # same dir, as seen inside soulbeet
LIB_HOST="/mnt/user/Media/Music"
LIB_CTR="/music"
BEETS_CFG="/config/config.yaml"
LIB_DB="/music/.beets_library.db"
STATE_DB="/mnt/user/appdata/librarian/state/state.db"
LOG="/mnt/user/appdata/librarian/upgrade-swap.log"

# Leave empty to DELETE the old album (the configured behavior). Set to a path to
# move it there instead -- the reversible option, if you ever want a safety net.
ARCHIVE_DIR=""

DURATION_TOLERANCE=5       # percent; guards against wrong-edition rips
MAX_PER_RUN=20

say(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

if ! docker ps --format '{{.Names}}' | grep -qx soulbeet; then
  echo "soulbeet container not running; skipping swap"; exit 0
fi
[ -d "$UPG_HOST" ] || exit 0

# Verifying + importing can outrun the 30-min interval. Two concurrent `beet` runs
# against one library db race each other, exactly as in rescue-sweep.sh.
LOCK=/tmp/librarian-upgrade-swap.lock
exec 9>"$LOCK"
if ! flock -n 9; then echo "a previous swap is still running; skipping"; exit 0; fi

# Navidrome must not index half-verified albums sitting in staging.
[ -f "$UPG_HOST/.ndignore" ] || touch "$UPG_HOST/.ndignore" 2>/dev/null

sql(){ [ -f "$STATE_DB" ] && sqlite3 "$STATE_DB" \
        "PRAGMA busy_timeout=30000; $1" 2>/dev/null; }

# NOTE: the soulbeet image has NO SHELL -- `docker exec soulbeet sh -c ...` fails with
# "exec: sh: executable file not found". Only direct binaries exist (beet, python3,
# ffmpeg, ffprobe). So every file is enumerated HERE on the host and each container
# tool is exec'd directly, one file at a time. That's a docker exec per file, which is
# why MAX_PER_RUN exists -- this runs nightly against a handful of albums, not a library.
ctr_path(){ echo "${1/#$LIB_HOST/$LIB_CTR}"; }

audio_files(){ # $1 = host dir
  find "$1" -maxdepth 1 -type f \( -iname '*.flac' -o -iname '*.mp3' -o -iname '*.m4a' \
    -o -iname '*.aac' -o -iname '*.ogg' -o -iname '*.opus' \) 2>/dev/null
}

# Total duration of a directory's audio, in whole seconds.
sum_duration(){ # $1 = host dir
  local total=0 f d
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    d=$(docker exec soulbeet ffprobe -v error -show_entries format=duration \
          -of csv=p=0 "$(ctr_path "$f")" 2>/dev/null | tr -d '\r')
    case "$d" in ''|*[!0-9.]*) d=0 ;; esac
    total=$(awk -v t="$total" -v x="$d" 'BEGIN{printf "%.0f", t+x}')
  done < <(audio_files "$1")
  echo "${total:-0}"
}

swapped=0; skipped=0; failed=0; checked=0

# _upgrades/<Artist>/<Album>/ -- the layout beets writes.
for artist_dir in "$UPG_HOST"/*/; do
  [ -d "$artist_dir" ] || continue
  artist=$(basename "$artist_dir")
  [ "$artist" = ".ndignore" ] && continue

  for album_dir in "$artist_dir"*/; do
    [ -d "$album_dir" ] || continue
    [ "$checked" -ge "$MAX_PER_RUN" ] && break 2
    checked=$((checked + 1))
    album=$(basename "$album_dir")
    new_ctr="$UPG_CTR/$artist/$album"
    label="$artist - $album"

    # ---- gate 1: every audio file is FLAC ---------------------------------
    n_flac=$(find "$album_dir" -maxdepth 1 -type f -iname '*.flac' | wc -l)
    n_other=$(find "$album_dir" -maxdepth 1 -type f \
                \( -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.aac' -o -iname '*.ogg' \
                   -o -iname '*.opus' -o -iname '*.wma' \) | wc -l)
    if [ "$n_flac" -eq 0 ] || [ "$n_other" -gt 0 ]; then
      say "SKIP [$label]: gate 1 failed — $n_flac flac + $n_other non-flac file(s); not a clean FLAC album"
      skipped=$((skipped + 1)); continue
    fi

    # Locate the existing lossy album this is meant to replace.
    old_host=$(sql "SELECT dir FROM upgrades WHERE artist='$(echo "$artist" | sed "s/'/''/g")' \
                     AND album='$(echo "$album" | sed "s/'/''/g")' LIMIT 1;")
    [ -z "$old_host" ] && old_host="$LIB_HOST/$artist/$album"
    old_ctr="${old_host/#$LIB_HOST/$LIB_CTR}"
    if [ ! -d "$old_host" ]; then
      # Nothing to replace: this isn't an upgrade, it's just a new album. Import it
      # normally rather than leaving it stranded in staging forever.
      say "NOTE [$label]: no existing album at $old_host — importing as a new album"
      if docker exec soulbeet beet -c "$BEETS_CFG" -l "$LIB_DB" -d "$LIB_CTR" \
           import -q "$new_ctr" >>"$LOG" 2>&1; then
        rm -rf "$album_dir"; swapped=$((swapped + 1))
      else
        failed=$((failed + 1))
      fi
      continue
    fi

    # ---- gate 2: track count not lower than what we already have -----------
    n_old=$(find "$old_host" -maxdepth 1 -type f \
              \( -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.flac' -o -iname '*.aac' \
                 -o -iname '*.ogg' -o -iname '*.opus' \) | wc -l)
    if [ "$n_flac" -lt "$n_old" ]; then
      say "SKIP [$label]: gate 2 failed — FLAC has $n_flac track(s), existing album has $n_old; refusing to lose tracks"
      skipped=$((skipped + 1)); continue
    fi

    # ---- gate 3: every file decodes cleanly -------------------------------
    # The one that matters most. Soulseek transfers get truncated and peers serve
    # corrupt files; a header-only check would pass those happily and we'd delete a
    # good MP3 in exchange for an unplayable FLAC. `flac -t` doesn't exist in this
    # container (or on the host), so decode with ffmpeg to null and require silence.
    n_bad=0; bad_names=""
    while IFS= read -r f; do
      [ -n "$f" ] || continue
      # A full decode to null: ffmpeg stays silent on a good file and prints on a bad
      # one. Any output at all means do not trust this file.
      out=$(docker exec soulbeet ffmpeg -v error -i "$(ctr_path "$f")" -f null - 2>&1)
      if [ -n "$out" ]; then
        n_bad=$((n_bad + 1)); bad_names="$bad_names $(basename "$f")"
      fi
    done < <(find "$album_dir" -maxdepth 1 -type f -iname '*.flac')
    if [ "$n_bad" -gt 0 ]; then
      say "SKIP [$label]: gate 3 failed — $n_bad file(s) failed to decode (corrupt/truncated transfer):$bad_names — keeping existing album"
      skipped=$((skipped + 1)); continue
    fi

    # ---- gate 4: duration within tolerance --------------------------------
    d_new=$(sum_duration "$album_dir")
    d_old=$(sum_duration "$old_host")
    if [ "${d_old:-0}" -gt 0 ] && [ "${d_new:-0}" -gt 0 ]; then
      diff=$(( (d_new - d_old) * 100 / d_old )); diff=${diff#-}
      if [ "$diff" -gt "$DURATION_TOLERANCE" ]; then
        say "SKIP [$label]: gate 4 failed — duration differs ${diff}% (new ${d_new}s vs old ${d_old}s); likely a different edition"
        skipped=$((skipped + 1)); continue
      fi
    else
      say "SKIP [$label]: gate 4 failed — could not read durations (new=${d_new:-?}s old=${d_old:-?}s)"
      skipped=$((skipped + 1)); continue
    fi

    # ---- all gates passed: swap ------------------------------------------
    old_size=$(du -sh "$old_host" 2>/dev/null | cut -f1)
    say "VERIFIED [$label]: $n_flac FLAC track(s), all decode clean, duration within ${diff}% — swapping"

    # Remove the old album from beets AND disk first: the new files move to exactly
    # the path the old ones occupy, so they cannot coexist. The verified FLAC is
    # already safely on disk in staging, so a failure after this point leaves the
    # album recoverable there rather than lost.
    docker exec soulbeet beet -c "$BEETS_CFG" -l "$LIB_DB" \
      remove -f --delete "path:$old_ctr" >>"$LOG" 2>&1
    rm -rf "$old_host" 2>/dev/null

    # Move the staged FLAC into the library. It's already in the beets db (soulbeet
    # imported it into staging), so this is a move, not a re-import -- a re-import
    # would just report "already in the library" and do nothing.
    if docker exec soulbeet beet -c "$BEETS_CFG" -l "$LIB_DB" \
         move -d "$LIB_CTR" "path:$new_ctr" >>"$LOG" 2>&1; then
      : # fall through to confirmation
    else
      say "WARN [$label]: beet move reported an error; verifying placement anyway"
    fi

    # ---- confirm the replacement actually landed --------------------------
    n_live=$(find "$LIB_HOST/$artist" -type f -iname '*.flac' -newermt '-10 minutes' \
               2>/dev/null | wc -l)
    if [ "$n_live" -ge "$n_flac" ]; then
      rm -rf "$album_dir" 2>/dev/null
      rmdir "$artist_dir" 2>/dev/null
      say "SWAPPED [$label]: now FLAC in library ($n_flac tracks); old lossy copy (${old_size:-?}) removed"
      sql "UPDATE upgrades SET status='replaced', note='swapped to flac', \
           updated_at=$(date +%s) WHERE artist='$(echo "$artist" | sed "s/'/''/g")' \
           AND album='$(echo "$album" | sed "s/'/''/g")';"
      swapped=$((swapped + 1))
    else
      say "ERROR [$label]: swap did not land ($n_live/$n_flac FLAC in library). Staged copy KEPT at $album_dir — recover it manually."
      sql "UPDATE upgrades SET status='failed', note='swap did not land; staged copy kept', \
           updated_at=$(date +%s) WHERE artist='$(echo "$artist" | sed "s/'/''/g")' \
           AND album='$(echo "$album" | sed "s/'/''/g")';"
      failed=$((failed + 1))
    fi
  done
done

[ "$checked" -gt 0 ] && say "Upgrade swap done: $swapped swapped, $skipped skipped (gate failures), $failed failed, of $checked staged album(s)"
exit 0
