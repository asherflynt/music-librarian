FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# Container-internal plumbing. These are the mount points the CA template maps host
# paths onto, so they belong in the image, not in every user's template. The app's
# own defaults assume a docker-network deploy (FREE_SPACE_PATH=/music,
# STAGING_PATH=/staging); pin them here to the canonical CA layout instead. Only
# TARGET_FOLDER (the library path as SOULBEET sees it) is worth overriding per host.
ENV MUSIC_PATH=/music \
    FREE_SPACE_PATH=/cluster \
    STAGING_PATH=/cache \
    TARGET_FOLDER=/music \
    UPGRADE_FOLDER=/music/_upgrades \
    WEB_PORT=8730

WORKDIR /app

# mutagen reads the real audio codec. That's load-bearing, not cosmetic: .m4a is
# shared by ALAC (lossless) and AAC (lossy), so extension-based classification would
# both corrupt the library quality stats and aim the upgrade scanner at files that
# are already lossless.
RUN pip install --no-cache-dir requests==2.32.3 mutagen==1.47.0

COPY librarian.py /app/librarian.py
COPY web.py /app/web.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Default config files, baked in so a fresh (empty) /config self-bootstraps on first
# run. entrypoint.sh copies any that are missing into /config; it never overwrites an
# existing one, so user edits survive image updates.
COPY config.env exclude.txt favorites.txt secrets.env.template /app/defaults/

# Config (secrets.env, config.env, exclude.txt, favorites.txt, takeout/) at /config
# State (state.db, status.json) persisted at /data
VOLUME ["/config", "/data"]

EXPOSE 8730

ENTRYPOINT ["/app/entrypoint.sh"]
