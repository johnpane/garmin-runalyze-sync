FROM python:3.12-slim

RUN pip install --no-cache-dir garminconnect curl_cffi requests fitdecode

WORKDIR /app
COPY fetch.py upload.py fit_inventory.py ./

# All state lives in the /data volume: env, fit/, logs/, state.sqlite3 and the
# Garmin token store. The scripts load /data/env with setdefault, so these
# ENV values win over the Mac paths in an env copied from env.example.
# Do not pass the env file with --env-file: that would override them.
ENV SYNC_BASE=/data \
    SYNC_ENV=/data/env \
    GARMIN_TOKENSTORE=/data/garminconnect \
    INTERVAL=1800 \
    PYTHONUNBUFFERED=1
VOLUME /data

# Same as run.sh: a fetch failure must not stop the upload.
CMD ["sh", "-c", "while :; do python fetch.py; python upload.py; sleep \"$INTERVAL\"; done"]
