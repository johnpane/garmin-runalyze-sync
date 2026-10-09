# garmin-runalyze-sync

Pulls original FIT files from Garmin Connect and uploads them to Runalyze via the
Personal API, on a schedule, from a Mac. This initial codebase was mostly written
by DeepSeek-V4.1-Flash.

## Why this exists

As of October 2026 Garmin's API delivers a reduced FIT file to third-party
platforms. Runalyze staff compared the file exported from Garmin Connect against
the file received through Garmin's API for the same activity and found that the
API version no longer contains Garmin's undocumented, proprietary data, and that
there is no equivalent field in the public FIT profile (checked against FIT SDK
21.217).

Values lost on the API path: VO2max (by file), recovery time, performance
condition, primary training benefit, estimated sweat loss, lactate threshold pace
and power, age and maximum heart rate from the device's user profile, and some
sensor and data-source information.

Runalyze's native Garmin auto-sync uses that push path and therefore inherits the
loss. Fetching the file directly from the Garmin download service does not: a file
downloaded by this script was verified byte-identical (sha256) to the same
activity's manual "Export Original" from connect.garmin.com.

Garmin appears to be rolling the change out gradually, and has introduced and
rolled it back before. Re-check periodically — see "Verifying fidelity" below.

## Files

| File | Role |
|---|---|
| `env` | credentials and configuration (mode 600, never committed) |
| `env.example` | template for `env` |
| `fetch.py` | Garmin API to `fit/<activityId>.fit` |
| `upload.py` | scans a directory, POSTs activity files to Runalyze |
| `fit_inventory.py` | compares two FIT files message by message |
| `run.sh` | fetch then upload, logging to `logs/launchd.log` |
| `com.local.garmin-runalyze-sync.plist` | LaunchAgent, every 30 minutes |
| `state.sqlite3` | sha256 of every handled file (created on demand) |
| `fit/` | local archive of original FIT files |
| `logs/` | run logs |

## Requirements

macOS. Python 3.10 or newer. A Garmin Connect account. A Runalyze account with a
Personal API token (free tier is sufficient; the token is created at
<https://runalyze.com/settings/personal-api> and must be given an expiry date).

## 1. Install

    cd ~/Developer/garmin-runalyze-sync
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install garminconnect curl_cffi requests fitdecode
    cp env.example env
    chmod 600 env

Adjust `SYNC_BASE` in `env` if you cloned somewhere other than
`~/Developer/garmin-runalyze-sync`. `run.sh` and the plist both assume that path.

Every script resolves its own paths from `SYNC_BASE`, so relocating the directory
needs no code change - but the venv is not relocatable. If you move the
directory, delete `.venv` and rebuild it, because a venv embeds absolute paths in
its shebangs and `pyvenv.cfg`.

## 2. First Garmin login (interactive, once)

    .venv/bin/python fetch.py --lookback-days 1 --limit 5

Prompts for email, password and MFA code, then writes
`~/.garminconnect/garmin_tokens.json`. Later runs, including runs started by
launchd, authenticate from that token store and never redo the SSO password flow.

That distinction matters: Garmin rate limits the SSO login endpoint with HTTP 429,
the limit is per-account once triggered, and repeated failed attempts extend it
(users report 24-48 hours). Token-store authentication is not subject to it. So:

* Run the first login by hand. Never loop it.
* If `fetch.py` reports a 429, stop for the day. It exits without retrying, by
  design.
* launchd has no terminal, so `fetch.py` refuses to attempt an interactive login
  from a scheduled run. If the token store is ever rejected, re-run step 2 by
  hand.

Start with `--lookback-days 1` so the first call does not pull your whole history.
`--list` shows what would be downloaded without downloading.

## 3. Verifying fidelity

`fit_inventory.py` compares two FIT files by global message number and field
definition number, which are intrinsic to the file and do not depend on any
library's profile being current. It also labels undocumented Garmin messages as
`unknown_<number>`, which is where the filtered values live.

Pass the known-complete file first. The diff reports what the second file is
missing relative to the first:

    .venv/bin/python fit_inventory.py ~/Downloads/manual.fit fit/<activityId>.fit

Exit status is 0 when equivalent, 1 when not.

To answer the question definitively for one activity, avoid the diff entirely and
compare hashes. In a browser at connect.garmin.com, open the activity, use the
gear icon, choose "Export Original", unzip, then:

    shasum -a 256 ~/Downloads/<activityId>_ACTIVITY.fit fit/<activityId>.fit

Identical hashes mean this script is receiving the same bytes the website serves.

A quicker sanity check uses fitdecode's bundled dump utility:

    .venv/bin/fittxt -o /tmp/one.txt fit/<activityId>.fit
    grep -c unknown_ /tmp/one.txt

A large count of `unknown_` fields indicates the undocumented data is present.
Note the caveat: grepping for field *names* is unreliable in both directions,
because these values have no name in the public profile, and because fitdecode
labels a documented field as `unknown_` if its bundled profile is older than the
file's profile version. Use the message-and-field-number comparison above when it
matters.

## 4. Run it by hand

    .venv/bin/python fetch.py
    .venv/bin/python upload.py --dry-run
    .venv/bin/python upload.py

`--dry-run` validates configuration and lists scan results. It does not consult
the state database, so it lists files already uploaded as well as new ones, and it
reads no file contents.

Logs: `logs/fetch.log`, `logs/upload.log`.

## 5. Runalyze's native Garmin auto-sync: leave it enabled

Do not disconnect it.

Runalyze added merge-on-reupload in early October 2026. Per Runalyze staff, an
upload via the **upload form** merges the data into an activity that was already
synced rather than being rejected as a duplicate.

Keeping auto-sync on is preferable to disabling it, because the activity title is
not part of the FIT file: the auto-synced activity carries a title that a
standalone upload cannot supply, plus any tags or equipment you have added.

**Status of the API path is unconfirmed.** The staff statement about the API was
"you can upload the file via our API too, it should just replace that data too",
which is a hedge, and when asked directly the answer was that it might currently
only work on the upload form and would be checked. Observed behaviour at the time
of writing: a `POST` to the upload endpoint for an activity that already exists
returns HTTP 201, creates no duplicate, and changes nothing.

So, until that is resolved:

* This script's fetch half works and is verified.
* Uploading an activity that does **not** yet exist in Runalyze should create it.
* Uploading an activity that already exists is currently a no-op server-side.
* For the values that matter to you, upload the file from `fit/` through the
  Runalyze upload form. The files are already local; no trip to Garmin is needed.

Note that the help article "Merge activity file into existing activity" still
says duplicates are detected and not imported, with no automatic merging. That
text predates the change and is stale.

## 6. Schedule it

    chmod +x run.sh
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.garmin-runalyze-sync.plist
    launchctl kickstart -k gui/$(id -u)/com.local.garmin-runalyze-sync
    tail -30 logs/launchd.log

Runs once at load, then every 30 minutes (`StartInterval` 1800). Successful
scheduled output looks like `authenticated from token store`, `0 downloaded`,
`0 queued`.

LaunchAgents run in the GUI session and stop at logout. Interval timers do not
fire while the Mac is asleep; behaviour on wake has not been verified here, but
every run is idempotent so a missed occurrence costs nothing.

To remove:

    launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.local.garmin-runalyze-sync.plist

If the request volume seems high, increase `StartInterval` to 3600. Activities
reach Garmin within minutes, so little is lost. Runalyze asks that its Personal
API not be abused.

## 7. Idempotency and failure behaviour

* `fetch.py` skips an activity whose `<activityId>.fit` already exists. `--force`
  overrides.
* `upload.py` keys on the sha256 of the payload, so a rename, a re-export or a
  second copy elsewhere in the tree does not upload twice.
* A failed upload is recorded with its HTTP status and retried next run.
  `--recheck` ignores stored statuses and re-posts everything, which is how you
  re-attempt files previously accepted but not applied.
* 401/403, or a body containing "No valid token", stops the run with exit 3.
* Files modified within `SETTLE_SECONDS` (default 60) are ignored, so a
  half-written export is not read mid-copy.
* Garmin's SSO 429 aborts the run with an explanatory message and never retries.
* Nothing is deleted. `fit/` doubles as your local archive of original files.

## 8. Alternative source: pre-upload device files

`fetch.py` is not required. If you would rather not talk to Garmin's API at all,
set `FIT_SOURCE_DIR` in `env` to a directory of pre-upload FIT files and leave
`fetch.py` unscheduled:

    FIT_SOURCE_DIR=~/Library/Caches/Garmin/Express
    FIT_SOURCE_DIR=~/Library/Application Support/Garmin/GarminConnect

Those paths are Garmin Express's caches on macOS; Runalyze's own Garmin help page
documents them. Device-written files have never touched Garmin's servers, so
server-side filtering cannot apply to them. They are only populated if Garmin
Express actually syncs your watch on that Mac, and modern Garmin watches use MTP
rather than USB mass storage, so Finder will not browse the device directly.

## 9. Known fragility

* `garminconnect` is an unofficial wrapper around Garmin's mobile SSO endpoints,
  not a supported API. It authenticates as the Garmin Connect iOS app and needs
  `curl_cffi` for TLS fingerprint impersonation. Garmin changes the login flow
  periodically; upgrade the library before debugging anything else when logins
  start failing.
* Automated access to Garmin Connect is governed by Garmin's terms of service.
* The Runalyze Personal API is the same API third-party apps use, is
  write-oriented, and is covered by a fair-use request.
* Garmin's filtering has been introduced, rolled back under public pressure, and
  reintroduced. Re-check with `fit_inventory.py`, which is a one-command test
  once you hold both files.
