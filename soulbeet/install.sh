#!/bin/bash
# Install the decodecheck beets plugin and render Soulbeet's beets config.yaml
# on the Unraid host. Idempotent; backs up the existing config and rolls back
# automatically if `docker exec soulbeet beet -c /config/config.yaml version`
# (or `... config`) fails.
#
# Usage:
#   ./install.sh            Install plugin + config and validate in container
#   ./install.sh --dry-run  Render config to stdout with LASTFM_API_KEY masked
#   ./install.sh --rollback Restore the newest config.yaml.bak-* and re-validate
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_FILE="${TEMPLATE_FILE:-$SCRIPT_DIR/beets-config.yaml.template}"
PLUGIN_SRC="${PLUGIN_SRC:-$SCRIPT_DIR/beets-plugins/decodecheck.py}"

SECRETS_ENV="${SECRETS_ENV:-/mnt/user/appdata/librarian/secrets.env}"
CONFIG_DIR="${CONFIG_DIR:-/mnt/user/appdata/soulbeet/config}"
CONFIG_DEST="${CONFIG_DEST:-$CONFIG_DIR/config.yaml}"
PLUGIN_DIR="${PLUGIN_DIR:-/mnt/user/appdata/soulbeet/data/beets-plugins}"
PLUGIN_DEST="${PLUGIN_DEST:-$PLUGIN_DIR/decodecheck.py}"
STATE_DIR="${STATE_DIR:-/mnt/cluster/Media/slskd_downloads/.decodecheck}"
CONTAINER="${CONTAINER:-soulbeet}"
# Path of CONFIG_DEST inside the container. beets ignores $BEETS_CONFIG (that
# variable is read by Soulbeet, which passes -c); a bare `beet version` inside
# the container would read /tmp/.config/beets/config.yaml instead.
CONTAINER_CONFIG="${CONTAINER_CONFIG:-/config/config.yaml}"

DRY_RUN=0
ROLLBACK=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)
            DRY_RUN=1
            ;;
        --rollback)
            ROLLBACK=1
            ;;
        -h|--help)
            cat <<'EOF'
Usage: install.sh [--dry-run | --rollback]

Installs soulbeet/beets-plugins/decodecheck.py and renders
soulbeet/beets-config.yaml.template into Soulbeet's beets config.yaml.

Options:
  --dry-run   Render the config to stdout with LASTFM_API_KEY masked; make no changes.
  --rollback  Restore the newest config.yaml.bak-* over config.yaml; remove the
              installed decodecheck.py if the restored config does not use it.
  -h, --help  Show this help message.
EOF
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

die() {
    echo "ERROR: $*" >&2
    exit 1
}

read_lastfm_key() {
    local secrets_file="$1"
    [[ -f "$secrets_file" ]] || die "secrets file not found: $secrets_file"

    # Extract LASTFM_API_KEY without sourcing (values may contain shell metacharacters).
    local raw_line val
    raw_line="$(grep -E '^[[:space:]]*LASTFM_API_KEY=' "$secrets_file" | tail -n 1 || true)"
    [[ -n "$raw_line" ]] || die "LASTFM_API_KEY not found in $secrets_file"

    val="$(printf '%s' "$raw_line" | cut -d= -f2-)"
    val="${val%$'\r'}"

    # Strip matching surrounding single or double quotes if present.
    if [[ "$val" =~ ^\"(.*)\"$ ]]; then
        val="${BASH_REMATCH[1]}"
    elif [[ "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
    fi

    [[ -n "$val" ]] || die "LASTFM_API_KEY is empty in $secrets_file"
    # Reject characters that would break double-quoted YAML scalar syntax.
    if [[ "$val" == *\"* || "$val" == *\\* ]]; then
        die "LASTFM_API_KEY contains unsupported YAML quote/backslash characters"
    fi

    printf '%s' "$val"
}

render_template() {
    local replacement="$1"
    # Pass replacement via ENVIRON so it never appears on argv and avoids
    # sed/gsub metacharacter expansion (&, \, /).
    LASTFM_REPLACEMENT="$replacement" awk '
        BEGIN {
            tok = "${LASTFM_API_KEY}"
            toklen = length(tok)
        }
        {
            idx = index($0, tok)
            while (idx > 0) {
                $0 = substr($0, 1, idx - 1) ENVIRON["LASTFM_REPLACEMENT"] substr($0, idx + toklen)
                idx = index($0, tok)
            }
            print
        }
    ' "$TEMPLATE_FILE"
}

if [[ "$DRY_RUN" -eq 1 && "$ROLLBACK" -eq 1 ]]; then
    die "--dry-run and --rollback are mutually exclusive"
fi

if [[ "$ROLLBACK" -eq 1 ]]; then
    # Newest backup by mtime (backup names mix timestamp formats).
    newest="$(ls -1t "$CONFIG_DEST".bak-* 2>/dev/null | head -n 1 || true)"
    [[ -n "$newest" && -f "$newest" ]] || die "no backup found matching $CONFIG_DEST.bak-*"
    cp -p "$newest" "$CONFIG_DEST"
    echo ">> restored $CONFIG_DEST from $newest"
    if [[ -f "$PLUGIN_DEST" ]] && ! grep -qE '^[[:space:]]*plugins:.*\bdecodecheck\b' "$CONFIG_DEST"; then
        rm -f "$PLUGIN_DEST"
        echo ">> removed $PLUGIN_DEST (restored config does not load it)"
    fi
    if ver_out="$(docker exec "$CONTAINER" beet -c "$CONTAINER_CONFIG" version 2>&1)"; then
        printf '%s\n' "$ver_out" | grep -E '^plugins:' || true
        echo ">> rollback OK"
        exit 0
    fi
    printf '%s\n' "$ver_out" >&2
    die "beet version failed after rollback"
fi

[[ -f "$TEMPLATE_FILE" ]] || die "template not found: $TEMPLATE_FILE"
[[ -f "$PLUGIN_SRC" ]] || die "plugin source not found: $PLUGIN_SRC"

if [[ "$DRY_RUN" -eq 1 ]]; then
    if [[ -f "$SECRETS_ENV" ]]; then
        # Verify key is present and non-empty without ever echoing it.
        _unused_key="$(read_lastfm_key "$SECRETS_ENV")"
        unset _unused_key
    fi
    render_template "<REDACTED>"
    exit 0
fi

lastfm_key="$(read_lastfm_key "$SECRETS_ENV")"

[[ -d "$CONFIG_DIR" ]] || die "Soulbeet config directory not found: $CONFIG_DIR"
mkdir -p "$PLUGIN_DIR"
mkdir -p "$STATE_DIR"

tmp_dir="$(mktemp -d)"
had_plugin=0
backup_plugin=""
tmp_config="$CONFIG_DIR/.config.yaml.tmp.$$"
cleanup() {
    rm -rf "$tmp_dir"
    rm -f "$tmp_config"
}
trap cleanup EXIT

# Render first (0600 because it holds LASTFM_API_KEY), so a re-run that would
# not change anything does not create another backup (keeps --rollback useful).
(umask 077; render_template "$lastfm_key" > "$tmp_config")
unset lastfm_key
chmod 0600 "$tmp_config"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_config=""
had_config=0
if [[ -f "$CONFIG_DEST" ]]; then
    had_config=1
    if cmp -s "$CONFIG_DEST" "$tmp_config"; then
        echo ">> config already up to date; no new backup"
        backup_config="$tmp_dir/config.yaml.prev"
        cp -p "$CONFIG_DEST" "$backup_config"
    else
        backup_config="${CONFIG_DEST}.bak-${timestamp}"
        cp -p "$CONFIG_DEST" "$backup_config"
        echo ">> backed up existing config to $backup_config"
    fi
fi

if [[ -f "$PLUGIN_DEST" ]]; then
    had_plugin=1
    backup_plugin="$tmp_dir/decodecheck.py.prev"
    cp -p "$PLUGIN_DEST" "$backup_plugin"
fi

rollback() {
    echo ">> validation failed; rolling back changes..." >&2
    if [[ "$had_config" -eq 1 && -n "$backup_config" && -f "$backup_config" ]]; then
        cp -p "$backup_config" "$CONFIG_DEST"
        echo ">> restored $CONFIG_DEST from $backup_config" >&2
    else
        rm -f "$CONFIG_DEST"
    fi
    if [[ "$had_plugin" -eq 1 && -n "$backup_plugin" && -f "$backup_plugin" ]]; then
        cp -p "$backup_plugin" "$PLUGIN_DEST"
        echo ">> restored previous $PLUGIN_DEST" >&2
    else
        rm -f "$PLUGIN_DEST"
    fi
}

# Install plugin atomically.
tmp_plugin="$PLUGIN_DIR/.decodecheck.py.tmp.$$"
cp "$PLUGIN_SRC" "$tmp_plugin"
chmod 0644 "$tmp_plugin"
mv -f "$tmp_plugin" "$PLUGIN_DEST"
echo ">> installed plugin to $PLUGIN_DEST"

# Install config atomically.
mv -f "$tmp_config" "$CONFIG_DEST"
echo ">> rendered config to $CONFIG_DEST"

echo ">> validating with docker exec $CONTAINER beet -c $CONTAINER_CONFIG version"
if ! ver_out="$(docker exec "$CONTAINER" beet -c "$CONTAINER_CONFIG" version 2>&1)"; then
    echo "$ver_out" >&2
    rollback
    die "beet version exited non-zero"
fi

plugins_line="$(printf '%s\n' "$ver_out" | grep -E '^plugins:' || true)"
if [[ -z "$plugins_line" ]]; then
    printf '%s\n' "$ver_out" >&2
    rollback
    die "could not find plugins line in beet version output"
fi

required_plugins=(musicbrainz fetchart embedart decodecheck)
missing=()
for plug in "${required_plugins[@]}"; do
    if ! printf '%s\n' "$plugins_line" | grep -qE "(^|[[:space:],])${plug}([[:space:],]|$)"; then
        missing+=("$plug")
    fi
done

if [[ "${#missing[@]}" -gt 0 ]]; then
    printf '%s\n' "$ver_out" >&2
    rollback
    die "missing expected plugin(s) in beet version: ${missing[*]}"
fi

echo ">> validating with docker exec $CONTAINER beet -c $CONTAINER_CONFIG config"
if ! cfg_out="$(docker exec "$CONTAINER" beet -c "$CONTAINER_CONFIG" config 2>&1)"; then
    echo "beet config failed (output suppressed to avoid leaking secrets)" >&2
    rollback
    die "beet config exited non-zero"
fi

if ! printf '%s\n' "$cfg_out" | grep -qE '^decodecheck:' || \
   ! printf '%s\n' "$cfg_out" | grep -qE '^fetchart:'; then
    rollback
    die "beet config output missing expected decodecheck/fetchart sections"
fi

echo ">> OK: $plugins_line"
echo ">> state dir ready: $STATE_DIR"
