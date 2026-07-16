FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir requests==2.32.3

COPY librarian.py /app/librarian.py

# Config (secrets.env, config.env, exclude.txt, takeout/) mounted at /config
# State (state.db, status.json) persisted at /data
VOLUME ["/config", "/data"]

CMD ["python", "-u", "/app/librarian.py"]
