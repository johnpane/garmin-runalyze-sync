# garmin-runalyze-sync

Pulls original FIT files from Garmin Connect and uploads them to Runalyze via the
Personal API, on a schedule. This initial codebase was mostly written by an LLM.

## Why this exists

As of October 2026 Garmin's API delivers a reduced FIT file to third-party
platforms. Runalyze staff compared the file exported from Garmin Connect against
the file received through Garmin's API for the same activity and found that the
API version no longer contains Garmin's undocumented, proprietary data, and that
there is no equivalent field in the public FIT profile (checked against FIT SDK
21.217).

The reported values lost on the API path include: VO2max (by file), recovery time,
performance condition, primary training benefit, estimated sweat loss, lactate
threshold pace and power, age and maximum heart rate from the device's user profile,
and some sensor and data-source information.

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
| `Dockerfile` | container image for the Docker path; see section 6b |
| `.dockerignore` | keeps `env` and local state out of the image |
| `state.sqlite3` | sha256 of every handled file (created on demand) |
| `fit/` | local archive of original FIT files |
| `logs/` | run logs |

## Requirements

Two supported pathways:

* **macOS, native**: venv plus a launchd LaunchAgent. Python 3.10 or newer.
* **Any platform, Docker**: macOS, Linux or Windows. The Python scripts are
  OS-independent; only the scheduler is platform-specific, and the container
  supplies its own.

Either way you need a Garmin Connect account and a Runalyze account with a
Personal API token (free tier is sufficient; create it at
<https://runalyze.com/settings/personal-api> and give it an expiry date).

Sections 1-4 use native-install commands. Docker users: see section 6b.

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
launchd or Docker, authenticate from that token store and never redo the SSO
password flow.

That distinction matters: Garmin rate limits the SSO login endpoint with HTTP 429,
the limit is per-account once triggered, and repeated failed attempts extend it
(users report 24-48 hours). Token-store authentication is not subject to it. So:

* Run the first login by hand. Never loop it.
* If `fetch.py` reports a 429, stop for the day. It exits without retrying, by
  design.
* launchd or a detached container has no terminal, so `fetch.py` refuses to
  attempt an interactive login from a scheduled run. If the token store is ever
  rejected, re-run step 2 by hand.

Start with `--lookback-days 1` so the first call does not pull your whole history.
`--list` shows what would be downloaded without downloading.

## 3. Verifying fidelity

`fit_inventory.py` compares two FIT files by global message number and field
definition number, which are intrinsic to the file and do not depend on any
library's profile being current. It also labels undocumented Garmin messages as
`unknown_<number>`, which is where the filtered values live.

Pass the known-complete file first. The diff reports what the second file is
missing relative to the first:

    .venv/bin/python fit_inventory.py ~/Downloads/manual.fit fit/<activityId>.fit    # macOS

(or, `docker exec garmin-runalyze-sync python fit_inventory.py ...`)

Docker users: substitute `~/garmin-runalyze-data/fit/<activityId>.fit` for the
`fit/` path, and run `fittxt` and `grep` inside the container, for example
`docker exec garmin-runalyze-sync fittxt -o /tmp/one.txt /data/fit/<activityId>.fit`.
Anything you want to compare from the website export must be copied into the data
directory first, since only `/data` exists inside the container.

Exit status is 0 when equivalent, 1 when not.

To answer the question definitively for one activity, avoid the diff entirely and
compare hashes. In a browser at connect.garmin.com, open the activity, use the
gear icon, choose "Export Original", unzip, then:

    shasum -a 256 ~/Downloads/<activityId>_ACTIVITY.fit fit/<activityId>.fit     # macOS
    sha256sum ~/Downloads/<activityId>_ACTIVITY.fit fit/<activityId>.fit         # Linux
    Get-FileHash -Algorithm SHA256 ~\Downloads\<activityId>_ACTIVITY.fit         # Windows

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

Logs: `logs/fetch.log`, `logs/upload.log`, or `docker logs -f garmin-runalyze-sync`
for the container.

## 5. Runalyze's native Garmin auto-sync: leave it enabled

Do not disconnect it.

Runalyze merges on re-upload: uploading a file for an activity that is already in
Runalyze adds the missing data to that activity instead of rejecting it as a
duplicate, preserving its title, tags and equipment. This works on both the
upload form and `POST /api/v1/activities/uploads`.

Keeping auto-sync on is preferable to disabling it, because the activity title is
not part of the FIT file: the auto-synced activity carries a title that a
standalone upload cannot supply, plus any tags or equipment you have added.

Note that as of 2026-10-10, the help article "Merge activity file into existing 
activity" still says duplicates are detected and not imported, with no automatic 
merging. That text predates the change and is stale.

## 6. Schedule it

### 6a. LaunchAgent (Mac only)
The LaunchAgent in this repository is a template. The file launchd actually reads is
`~/Library/LaunchAgents/com.local.garmin-runalyze-sync.plist`; copy the template there,
and keep the two in sync if you change either.

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

### 6b. Docker (cross-platform)

Instead of a venv and launchd, run it in a container. Everything (`env`, `fit/`,
`logs/`, `state.sqlite3`, Garmin tokens) lives in one host directory mounted at
`/data`. `SYNC_BASE`, `SYNC_ENV`, `GARMIN_TOKENSTORE`, `SETTLE_SECONDS` and
`INTERVAL` are set by the image and cannot be changed from `env` inside the
container, because the scripts load that file with `setdefault` and an existing
environment variable wins. Use `-e NAME=value` to override any of them at docker
run.

    docker build -t garmin-runalyze-sync .
    mkdir -p ~/garmin-runalyze-data && cp env.example ~/garmin-runalyze-data/env
    # edit ~/garmin-runalyze-data/env, set RUNALYZE_TOKEN

    # first Garmin login, interactive (MFA prompt)
    docker run --rm -it --user "$(id -u):$(id -g)" -v ~/garmin-runalyze-data:/data garmin-runalyze-sync \
        python fetch.py --lookback-days 1 --limit 5

    # then run fetch + upload every INTERVAL seconds (default 1800)
    docker run -d --name garmin-runalyze-sync --restart unless-stopped \
        --user "$(id -u):$(id -g)" -v ~/garmin-runalyze-data:/data garmin-runalyze-sync
    docker logs -f garmin-runalyze-sync

Do not pass the env file with `--env-file`; mount it as `/data/env` as shown.
`--user` keeps the files in the data directory owned by you instead of root.

Two things are not visible from the commands above.

The scripts are copied into the image at build time, so editing `fetch.py`,
`upload.py` or `fit_inventory.py` in your checkout has no effect on a running
container. Rebuild, then remove and recreate the container, because
`docker restart` reuses the existing image:

    docker build -t garmin-runalyze-sync .
    docker rm -f garmin-runalyze-sync
    docker run -d --name garmin-runalyze-sync --restart unless-stopped \
        --user "$(id -u):$(id -g)" -v ~/garmin-runalyze-data:/data garmin-runalyze-sync

On Windows, use the `docker run` form from the subsection below in place of the
last command.

The dependencies are installed without pinned versions, so a rebuild can pull
newer releases even when nothing in the repository has changed. If a rebuild
starts failing, a library upgrade is the likely cause rather than your edit; see
the `garminconnect` note in section 9.

#### Windows

PowerShell supports neither `\` line continuation nor `$(id -u)`, and `--user`
is meaningless there: the container runs in a Linux VM where your Windows
account has no UID. Omit it and use an explicit path. WSL2 users can follow the
POSIX instructions verbatim instead.

    docker build -t garmin-runalyze-sync .
    mkdir $HOME\garmin-runalyze-data
    copy env.example $HOME\garmin-runalyze-data\env
    # edit $HOME\garmin-runalyze-data\env, set RUNALYZE_TOKEN

    docker run --rm -it -v ${HOME}/garmin-runalyze-data:/data garmin-runalyze-sync `
      python fetch.py --lookback-days 1 --limit 5

    docker run -d --name garmin-runalyze-sync --restart unless-stopped `
      -v ${HOME}/garmin-runalyze-data:/data garmin-runalyze-sync

Files in the data directory are owned by root inside the VM rather than by you.
That is expected on Windows and has no practical effect on the bind mount.

## 7. Idempotency and failure behaviour

* `fetch.py` skips an activity whose `<activityId>.fit` already exists. `--force`
  overrides.
* `upload.py` keys on the sha256 of the payload, so a rename, a re-export or a
  second copy elsewhere in the tree does not upload twice.
* A failed upload is recorded with its HTTP status and retried next run.
  `--recheck` ignores stored statuses and re-posts everything, which is how you
  re-attempt files previously accepted but not applied.
* 401/403, or a body containing "No valid token", stops the run with exit 3.
* Files modified within `SETTLE_SECONDS` are ignored, so a half-written export is
  not read mid-copy. Default 60; the Docker image sets 0, where no settle
  delay is needed because `fetch.py` is the only writer of the data directory
  and writes atomically.
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

This alternative is not available inside the Docker container as shipped, which
mounts only `/data`. Using this mode there also requires bind-mounting the source
directory and pointing `FIT_SOURCE_DIR` at its path inside the container.

## 9. Known fragility

* `garminconnect` is an unofficial wrapper around Garmin's mobile SSO endpoints,
  not a supported API. It authenticates as the Garmin Connect iOS app and needs
  `curl_cffi` for TLS fingerprint impersonation. Garmin changes the login flow
  periodically; upgrade the library before debugging anything else when logins
  start failing.
* Automated access to Garmin Connect is governed by Garmin's terms of service.
* The Runalyze Personal API is the same API third-party apps use, is
  write-oriented, and is covered by a fair-use request.
* The upload endpoint can return HTTP 201 while applying nothing, so read a recorded 
  201 in `state.sqlite3` as "accepted", not "applied". Re-run `upload.py --recheck`
  occasionally, or after any change on Runalyze's side, and confirm in Runalyze 
  that VO2max (by file), recovery time and performance condition are present on the 
  activity.
* Garmin's filtering has been introduced, rolled back under public pressure, and
  reintroduced. Re-check with `fit_inventory.py`, which is a one-command test
  once you hold both files.
