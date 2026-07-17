FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# mutagen reads the real audio codec. That's load-bearing, not cosmetic: .m4a is
# shared by ALAC (lossless) and AAC (lossy), so extension-based classification would
# both corrupt the library quality stats and aim the upgrade scanner at files that
# are already lossless.
RUN pip install --no-cache-dir requests==2.32.3 mutagen==1.47.0

COPY librarian.py /app/librarian.py
COPY web.py /app/web.py

# Config (secrets.env, config.env, exclude.txt, favorites.txt, takeout/) at /config
# State (state.db, status.json) persisted at /data
VOLUME ["/config", "/data"]

EXPOSE 8730

CMD ["python", "-u", "/app/librarian.py"]
