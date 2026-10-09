#!/bin/bash
# Sequential fetch then upload, with all output appended to one log.
# No `set -e`: a fetch failure must not stop the upload of files already present.
set -uo pipefail

BASE="$HOME/Developer/garmin-runalyze-sync"
PY="$BASE/.venv/bin/python"
LOG="$BASE/logs/launchd.log"

mkdir -p "$BASE/logs"
cd "$BASE" || exit 1

{
  echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') run start"
  "$PY" "$BASE/fetch.py"  || echo "fetch.py exited $?"
  "$PY" "$BASE/upload.py" || echo "upload.py exited $?"
  echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') run end"
} >> "$LOG" 2>&1
