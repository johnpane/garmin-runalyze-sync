#!/usr/bin/env python3
"""Upload activity files to Runalyze via the Personal API.

Scans $FIT_SOURCE_DIR (default $SYNC_BASE/fit) for .fit/.tcx/.gpx/.ttbin/.fitlog
files and POSTs each to https://runalyze.com/api/v1/activities/uploads.

Endpoint, header and multipart field name are taken from Runalyze's own
personal-api repository (bash/upload-activities.sh):

    curl --request POST 'https://runalyze.com/api/v1/activities/uploads' \
         --header 'token: <token>' --form 'file=@activity.fit'

Uploads are queued server-side, so a successful POST does not mean the activity
is immediately visible in Runalyze, and a duplicate may be queued rather than
rejected. Duplicate protection is therefore local: the sha256 of each payload is
recorded in state.sqlite3 and never posted twice.

This script never talks to Garmin.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

DEFAULT_BASE = "~/Developer/garmin-runalyze-sync"
DEFAULT_URL = "https://runalyze.com/api/v1/activities/uploads"

# Extensions accepted by Runalyze, per Runalyze/personal-api bash example.
ACCEPTED_SUFFIXES = (".fit", ".tcx", ".gpx", ".ttbin", ".fitlog")

# Runalyze's own script detects a bad token by matching this string in the body
# rather than by HTTP status, so we do the same in addition to the status check.
BAD_TOKEN_MARKER = "no valid token"

log = logging.getLogger("upload")


def load_env(path: Path) -> None:
    """Minimal KEY=VALUE loader. A real environment variable always wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env(Path(os.environ.get("SYNC_ENV") or Path(DEFAULT_BASE).expanduser() / "env"))

BASE = Path(os.environ.get("SYNC_BASE") or DEFAULT_BASE).expanduser()
LOG_DIR = BASE / "logs"
DB_PATH = BASE / "state.sqlite3"
SQLITE_TIMEOUT = 30


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "upload.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            sha256      TEXT PRIMARY KEY,
            filename    TEXT,
            size        INTEGER,
            uploaded_at TEXT,
            http_status INTEGER,
            detail      TEXT
        )
        """
    )
    conn.commit()
    return conn


def is_ready_to_send(path: Path, settle_seconds: int) -> bool:
    """Reject partial writes, empty files and mislabelled FIT files.

    A file modified within SETTLE_SECONDS is assumed to still be arriving and is
    skipped; a concurrent Sync/Gadgetbridge export must not be read mid-write.
    """
    try:
        stat = path.stat()
        if stat.st_size <= 0:
            return False
        if settle_seconds > 0 and (time.time() - stat.st_mtime) < settle_seconds:
            return False
        if path.suffix.lower() == ".fit":
            if stat.st_size <= 12:
                return False
            with path.open("rb") as handle:
                if handle.read(12)[8:12] != b".FIT":
                    return False
    except OSError:
        return False
    return True


def gather_candidates(source_dir: Path, settle_seconds: int) -> list[Path]:
    found = [
        path
        for path in source_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in ACCEPTED_SUFFIXES
        and is_ready_to_send(path, settle_seconds)
    ]
    return sorted(found, key=lambda p: p.stat().st_mtime)


def post_to_runalyze(url: str, token: str, field: str, name: str, blob: bytes,
                     timeout: int) -> requests.Response:
    headers = {
        "token": token,
        "User-Agent": "garmin-runalyze-sync/1.0",
    }
    files = {field: (name, blob, "application/octet-stream")}
    return requests.post(url, headers=headers, files=files, timeout=timeout)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=os.environ.get("FIT_SOURCE_DIR") or str(BASE / "fit"),
        help="directory to scan for activity files (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="log what would be posted without posting anything",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="retry files previously recorded as failed, ignoring the stored status",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log the full response body for every file",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)

    source_dir = Path(args.dir).expanduser()
    token = os.environ.get("RUNALYZE_TOKEN", "").strip()
    url = os.environ.get("RUNALYZE_UPLOAD_URL", DEFAULT_URL).strip()
    field = os.environ.get("RUNALYZE_FILE_FIELD", "file").strip()
    timeout = int(os.environ.get("RUNALYZE_TIMEOUT", "180"))
    settle = int(os.environ.get("SETTLE_SECONDS", "60"))

    if not token or token == "REPLACE_ME":
        log.error("RUNALYZE_TOKEN is not set (edit %s)", BASE / "env")
        return 2
    if not url:
        log.error("RUNALYZE_UPLOAD_URL is not set")
        return 2
    if not source_dir.is_dir():
        log.error("source directory does not exist: %s", source_dir)
        return 2

    conn = connect_db()
    candidates = gather_candidates(source_dir, settle)
    log.info("%d candidate files in %s (settle window %ds)",
             len(candidates), source_dir, settle)
    log.info("posting to %s with multipart field %r", url, field)

    if args.dry_run:
        pending = 0
        for path in candidates:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                log.error("cannot read %s: %s", path, exc)
                continue
            row = conn.execute(
                "SELECT http_status FROM uploads WHERE sha256 = ?", (digest,)
            ).fetchone()
            if row and not args.recheck and row[0] is not None and row[0] < 400:
                log.info("dry run: would SKIP %s (recorded HTTP %s)", path.name, row[0])
                continue
            pending += 1
            log.info("dry run: would post %s", path)
        log.info("dry run: %d of %d candidates would be posted",
                 pending, len(candidates))
        return 0
    
    uploaded = 0
    failed = 0

    for path in candidates:
        try:
            blob = path.read_bytes()
        except OSError as exc:
            log.error("cannot read %s: %s", path, exc)
            continue

        digest = hashlib.sha256(blob).hexdigest()
        row = conn.execute(
            "SELECT http_status FROM uploads WHERE sha256 = ?", (digest,)
        ).fetchone()
        if row and not args.recheck and row[0] is not None and row[0] < 400:
            continue

        try:
            response = post_to_runalyze(url, token, field, path.name, blob, timeout)
        except requests.RequestException as exc:
            log.error("network error for %s: %s", path.name, exc)
            failed += 1
            continue

        status = response.status_code
        body = (response.text or "").strip()
        snippet = body.replace("\n", " ")[:300]

        if BAD_TOKEN_MARKER in body.lower() or status in (401, 403):
            log.error("token rejected (HTTP %s): %s", status, snippet)
            log.error("check RUNALYZE_TOKEN in %s and its expiry date", BASE / "env")
            return 3

        if 200 <= status < 300:
            uploaded += 1
            log.info("queued %s (HTTP %s) %s", path.name, status,
                     snippet if args.verbose else "")
        else:
            failed += 1
            log.error("%s: HTTP %s %s", path.name, status, snippet)

        conn.execute(
            "INSERT OR REPLACE INTO uploads VALUES (?,?,?,?,?,?)",
            (
                digest,
                path.name,
                len(blob),
                time.strftime("%Y-%m-%dT%H:%M:%S") if 200 <= status < 300 else None,
                status,
                snippet,
            ),
        )
        conn.commit()

    log.info("done: %d queued, %d failed, %d candidates in %s",
             uploaded, failed, len(candidates), source_dir)
    if uploaded:
        log.info("uploads are queued by Runalyze; they may not appear "
                 "in your calendar immediately")
    return 0


if __name__ == "__main__":
    sys.exit(main())
