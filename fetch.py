#!/usr/bin/env python3
"""Download recent original FIT files from Garmin Connect.

Writes <activityId>.fit into $SYNC_BASE/fit. Files already present are skipped
unless --force is given.

First run must be interactive (terminal) so the MFA prompt can be answered;
it writes a token store to GARMIN_TOKENSTORE which later runs reuse.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_BASE = "~/Developer/garmin-runalyze-sync"
MIN_PLAUSIBLE_EPOCH = 1_000_000_000        # 2001-09-09, guards against junk
MS_THRESHOLD = 1e11                        # above this, a value is milliseconds

log = logging.getLogger("fetch")


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
FIT_DIR = BASE / "fit"
LOG_DIR = BASE / "logs"


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "fetch.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def login(tokenstore: str):
    from garminconnect import Garmin  # imported late so --help works without it

    try:
        api = Garmin()
        api.login(tokenstore)
        log.info("Garmin: authenticated from token store %s", tokenstore)
        return api
    except Exception as exc:  # noqa: BLE001
        log.warning("Garmin: token login failed: %s", exc)

    if not sys.stdin.isatty():
        raise SystemExit(
            "Garmin tokens are missing or expired and there is no terminal to "
            f"prompt on. Run fetch.py manually once to refresh {tokenstore}."
        )

    from getpass import getpass

    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass("Garmin password: ")
    api = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("MFA code: ").strip(),
    )
    api.login(tokenstore)
    log.info("Garmin: interactive login OK, tokens stored in %s", tokenstore)
    return api


def activity_start_ms(activity: dict) -> int | None:
    """Start instant of an activity as epoch milliseconds, best effort."""
    for key in ("beginTimestamp", "startTimeLocal", "startTimeGMT"):
        value = activity.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value > MIN_PLAUSIBLE_EPOCH:
            return int(value * 1000) if value < MS_THRESHOLD else int(value)
        if isinstance(value, str):
            for pattern in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S",
            ):
                try:
                    return int(datetime.strptime(value, pattern).timestamp() * 1000)
                except ValueError:
                    continue
    return None


def download_original(api, activity_id: str) -> bytes:
    """Fetch the 'original' payload. Returns a zip (Garmin) or raw FIT bytes."""
    fmt = getattr(api, "ActivityDownloadFormat", None)
    if fmt is not None:
        return api.download_activity(activity_id, dl_fmt=fmt.ORIGINAL)
    return api.download_activity(activity_id)


def extract_fit(blob: bytes, activity_id: str) -> tuple[str, bytes]:
    """Return (member_name, fit_bytes) from a zip payload or a raw FIT blob."""
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            members = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not members:
                raise ValueError(f"no .fit inside archive: {zf.namelist()}")
            return Path(members[0]).name, zf.read(members[0])
    return f"{activity_id}.fit", blob


def looks_like_fit(blob: bytes) -> bool:
    return len(blob) > 12 and blob[8:12] == b".FIT"


def write_atomic(target: Path, blob: bytes) -> None:
    """Write via a temporary file so a concurrent reader never sees a partial file."""
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(blob)
    os.replace(tmp, target)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.environ.get("LOOKBACK_DAYS", "7")),
        help="only download activities started within this many days (default: %(default)s)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("FETCH_LIMIT", "20")),
        help="how many recent activities to list from Garmin (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download and overwrite files that already exist",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list what would be downloaded, then exit without downloading",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv)

    FIT_DIR.mkdir(parents=True, exist_ok=True)
    tokenstore = os.environ.get("GARMIN_TOKENSTORE", "~/.garminconnect")

    api = login(tokenstore)
    activities = api.get_activities(0, args.limit)
    log.info("Garmin: %d activities listed", len(activities))

    cutoff_ms = int(
        (datetime.now() - timedelta(days=args.lookback_days)).timestamp() * 1000
    )

    downloaded = 0
    skipped = 0

    for activity in activities:
        activity_id = str(activity.get("activityId") or activity.get("id") or "").strip()
        if not activity_id:
            log.warning("activity without an id, skipping: %r", activity.get("activityName"))
            continue

        start_ms = activity_start_ms(activity)
        when = activity.get("startTimeLocal") or activity.get("startTimeGMT") or "?"
        if start_ms is not None and start_ms < cutoff_ms:
            continue

        target = FIT_DIR / f"{activity_id}.fit"
        if target.exists() and not args.force:
            skipped += 1
            log.info("%s already present (%s), skipping", activity_id, when)
            continue

        if args.list:
            log.info("would download %s (%s)", activity_id, when)
            continue

        try:
            blob = download_original(api, activity_id)
            member, fit_bytes = extract_fit(blob, activity_id)
            if not looks_like_fit(fit_bytes):
                raise ValueError(f"payload is not a FIT file ({len(fit_bytes)} bytes)")

            write_atomic(target, fit_bytes)
            downloaded += 1
            log.info(
                "downloaded %s -> %s (%d bytes, member %s, started %s)",
                activity_id,
                target.name,
                len(fit_bytes),
                member,
                when,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("activity %s failed: %s", activity_id, exc)

    log.info("done: %d downloaded, %d already present, %d fit files in %s",
             downloaded, skipped, len(list(FIT_DIR.glob("*.fit"))), FIT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
