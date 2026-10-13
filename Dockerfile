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
    SETTLE_SECONDS=0 \
    PYTHONUNBUFFERED=1

VOLUME /data

# No pause is needed between fetch.py and upload.py. fetch.py writes
# each file to a .part name and os.replace()s it into place, so /data/fit never
# contains a partial file, which is what SETTLE_SECONDS protects against. That holds
# only while fetch.py is the sole writer of /data/fit. Note that these ENV values
# win over /data/env, because the scripts load that file with setdefault.

CMD ["sh", "-c", "while :; do python fetch.py; python upload.py; sleep \"$INTERVAL\"; done"]
