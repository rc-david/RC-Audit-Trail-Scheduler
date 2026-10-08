#!/usr/bin/env python3
"""
RingCentral Audit Trail collector.

WHAT IT DOES
    1. Authenticates to the RingCentral REST API with a JWT credential.
    2. Pulls the account Audit Trail for the last N days (paged).
    3. Flattens every returned field into a column (no hard-coded schema).
    4. Saves the events to the stores you choose (JSON file, SQLite, Postgres,
       SQL Server, any combination, or none), de-duplicated by event id.
    5. Writes a CSV and a self-contained, filterable HTML viewer for the run.
    6. Optionally serves that viewer on localhost so its "Schedule settings"
       panel can create / change / disable the Windows scheduled task.

WHEN SOMETHING GOES WRONG
    Every problem we can anticipate is reported as: what happened, the server's own
    message (if any), and numbered steps to fix it. Run   python rc_audit_trail.py --check
    to test your whole setup step by step. Every run is also written to output/run_log.txt
    and output/last_run.json, which matters for scheduled runs where nobody sees the console.
    Exit codes: 0 = success, 1 = failed, 2 = collected but no chosen store could be saved.

FILES WRITTEN (all in ./output unless --out is given)
    audit_trail_<YYYYMMDD>.csv    this run's events, one column per field
    audit_viewer_<YYYYMMDD>.html  self-contained viewer (data embedded)
    raw_<YYYYMMDD>.json           untouched API response   (json saving only)
    audit_master.json             cumulative JSON store    (json saving only)
    audit_trail.db                cumulative SQLite store  (db saving only)
    run_log.txt                   timestamped log of every run (secrets are redacted)
    last_run.json                 result of the most recent collection (shown on the viewer's settings panel)

USAGE
    python rc_audit_trail.py --check          # test the whole setup, with fixes for anything wrong
    python rc_audit_trail.py                  # collect using the window in schedule.json (default 35 days)
    python rc_audit_trail.py --days 90        # one-off wider window (API keeps 180 days)
    python rc_audit_trail.py --save both      # json, db, postgres, mssql, both (json+db), none; comma list ok
    python rc_audit_trail.py --serve          # open viewer with working schedule settings
    python rc_audit_trail.py --apply-schedule # create/update the Windows task from schedule.json
    python rc_audit_trail.py --from-json raw_20260101.json   # rebuild outputs offline from a saved response
    python rc_audit_trail.py --verbose        # also print each API call (never secrets)

Standard library only (Python 3.9+). Postgres / SQL Server need an optional
driver, and only if you choose them. Settings come from a .env file next to this
script (see .env.example) or from real environment variables.
"""
import sys

if sys.version_info < (3, 9):   # checked before anything else so old Pythons get a clear message, not a SyntaxError
    sys.exit("This tool needs Python 3.9 or newer; you are running %d.%d. Install a current Python from "
             "https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and try again."
             % sys.version_info[:2])

import argparse
import base64
import csv
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent   # everything (.env, schedule.json, template) lives next to this file
DEFAULT_PER_PAGE = 250   # records per API page. The beta API documents no maximum; override with RC_PER_PAGE if it rejects this.
MAX_RECORDS = 10000      # the API returns at most 10,000 records per search
REQUIRED = ["RC_SERVER_URL", "RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT"]
VERBOSE = False          # set by --verbose
LOG_PATH = None          # set once the output folder is known (see init_log)


# =============================================================================
# 1. ERRORS, LOGGING AND SAFE FILE WRITES
#    Goal: every failure we can foresee ends in a plain-language explanation and next steps,
#    never a raw Python traceback. Truly unexpected errors are logged with full detail.
# =============================================================================
class CollectorError(RuntimeError):
    """A problem we understand.
    what   - one plain sentence: what went wrong
    fix    - ordered list of things the user can do about it
    detail - the server's own message or the low-level reason, shown verbatim for support
    """

    def __init__(self, what, fix=(), detail=None):
        super().__init__(what)
        self.what, self.fix, self.detail = what, list(fix), detail

    def __str__(self):   # short one-line form, used by the settings panel
        return self.what + (" Try: " + " ".join(f"({i}) {s}" for i, s in enumerate(self.fix, 1)) if self.fix else "")

    def report(self):    # full console form
        lines = [f"ERROR: {self.what}"]
        if self.detail:
            lines.append(f"  Details: {self.detail}")
        if self.fix:
            lines.append("  What to do:")
            lines += [f"    {i}. {s}" for i, s in enumerate(self.fix, 1)]
        lines.append("  More help: run  python rc_audit_trail.py --check  and see README.md > Troubleshooting.")
        return "\n".join(lines)


def redact(text):
    """Remove any configured secret from text before it is printed or logged."""
    text = str(text)
    for k in ("RC_CLIENT_SECRET", "RC_JWT", "RC_CLIENT_ID", "RC_PG_DSN", "RC_MSSQL_CONN"):
        v = os.environ.get(k, "")
        if len(v) > 6:
            text = text.replace(v, "***")
    return text


def init_log(out_dir):
    """Choose the log file; keep it from growing forever (trim to the newest ~300 KB past 1 MB)."""
    global LOG_PATH
    LOG_PATH = out_dir / "run_log.txt"
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
            LOG_PATH.write_bytes(LOG_PATH.read_bytes()[-300_000:])
    except OSError:
        pass


def say(msg, level="INFO", to_err=False):
    """Print to the console and append to the log. Logging problems never interrupt a run."""
    msg = redact(msg)
    print(msg, file=sys.stderr if to_err else sys.stdout)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {level:5} {msg}\n")
        except OSError:
            pass


def vsay(msg):
    """Extra detail shown only with --verbose."""
    if VERBOSE:
        say("  [verbose] " + msg)


def write_last_run(out_dir, ok, message):
    """Record the outcome of a collection so the viewer's settings panel can show it (scheduled runs are silent)."""
    try:
        (out_dir / "last_run.json").write_text(
            json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "ok": ok, "message": redact(message)}),
            encoding="utf-8")
    except OSError:
        pass


def read_last_run(out_dir):
    try:
        return json.loads((out_dir / "last_run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def io_error(exc, path, what):
    """Translate a file-system error into a CollectorError that says what to do."""
    path = Path(path)
    if isinstance(exc, PermissionError):
        return CollectorError(
            f"Windows or the operating system would not let the program write {what} ({path.name}).",
            ["If that file is open in Excel, a browser, a database tool or an editor, close it and run again.",
             f"Check you have write permission to the folder {path.parent}, or choose another with --out.",
             "If the folder is inside OneDrive/Dropbox, make sure syncing is not paused or locking the file."],
            f"{type(exc).__name__}: {exc}")
    if getattr(exc, "errno", None) == 28:
        return CollectorError(f"The disk is full, so {what} could not be written.",
                              ["Free some disk space and run again."], str(exc))
    if isinstance(exc, FileNotFoundError):
        return CollectorError(f"The folder for {what} does not exist: {path.parent}",
                              ["Create the folder, or choose another with --out."], str(exc))
    return CollectorError(f"Could not write {what} ({path.name}).",
                          [f"Check the path is valid (not too long) and writable: {path.parent}"],
                          f"{type(exc).__name__}: {exc}")


def write_text(path, text, what):
    """Write a text file, converting OS errors into CollectorError."""
    try:
        Path(path).write_text(text, encoding="utf-8")
    except OSError as e:
        raise io_error(e, path, what)


def ensure_dir(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise CollectorError(f"The output folder cannot be used: {path}",
                             ["Make sure the drive/folder exists and you can write to it.",
                              "Or choose a different folder with --out C:\\some\\writable\\folder."],
                             f"{type(e).__name__}: {e}")


# =============================================================================
# 2. CONFIGURATION, SETTINGS VALIDATION AND AUTHENTICATION
# =============================================================================
def load_env():
    """Read KEY=VALUE lines from .env into os.environ. Real environment variables win (setdefault).
    utf-8-sig tolerates the invisible byte-order mark some Windows editors add."""
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_per_page():
    try:
        return max(1, min(1000, int(os.environ.get("RC_PER_PAGE", DEFAULT_PER_PAGE))))
    except ValueError:
        raise CollectorError(f"RC_PER_PAGE must be a whole number, but it is '{os.environ.get('RC_PER_PAGE')}'.",
                             ["Remove RC_PER_PAGE from .env, or set it to a number such as 100."])


def inspect_jwt(jwt):
    """Local-only sanity checks on the JWT (nothing is sent anywhere, the signature is not verified).
    Returns (problems, info). Catches the most common mistake, a badly pasted value, before any network call."""
    problems, info = [], {}
    j = jwt.strip()
    if not j:
        return ["RC_JWT is empty."], info
    if any(c in j for c in '{}"\'\r\n\t ,'):
        problems.append("RC_JWT contains braces, quotes, spaces or line breaks. It looks like a JSON block or extra "
                        "text was pasted. Paste ONLY the long token on a single line.")
        return problems, info
    parts = j.split(".")
    if not j.startswith("eyJ") or len(parts) != 3:
        problems.append("RC_JWT does not look like a JWT (it should start with 'eyJ' and contain exactly two dots). "
                        "Copy the whole token again from the Developer Console > Credentials.")
        return problems, info
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        info = {"exp": payload.get("exp"), "iss": payload.get("iss")}
    except (ValueError, TypeError):
        problems.append("RC_JWT could not be decoded. It was probably cut off when copied. Copy the whole token again.")
        return problems, info
    if isinstance(info.get("exp"), (int, float)) and info["exp"] < time.time():
        when = datetime.fromtimestamp(info["exp"], tz=timezone.utc).strftime("%Y-%m-%d")
        problems.append(f"The JWT expired on {when}. Create a new JWT credential and replace RC_JWT in .env.")
    return problems, info


def check_settings():
    """Validate .env / environment values. Returns (problems, warnings), lists of plain-language strings.
    Problems stop a run; warnings are shown but do not."""
    problems, warnings = [], []
    env_file = HERE / ".env"
    if not env_file.exists() and not any(os.environ.get(k) for k in REQUIRED):
        problems.append(f"There is no .env file at {env_file}. Copy .env.example to .env in that folder, then fill in "
                        "RC_CLIENT_ID, RC_CLIENT_SECRET and RC_JWT (README > Setting up RingCentral).")
        return problems, warnings
    for k in REQUIRED:
        if not os.environ.get(k, "").strip():
            problems.append(f"{k} is empty. Open .env and fill it in (README > Setting up RingCentral says where to find it).")
    url = os.environ.get("RC_SERVER_URL", "").strip()
    if url:
        host = urllib.parse.urlparse(url).hostname or ""
        local = host in ("127.0.0.1", "localhost")
        if not (url.startswith("https://") or (local and url.startswith("http://"))):
            problems.append(f"RC_SERVER_URL must start with https:// (it is '{url}'). Production is https://platform.ringcentral.com")
        elif "devtest" in host:
            warnings.append("RC_SERVER_URL points at the SANDBOX (devtest). Your real account's audit data is only on "
                            "https://platform.ringcentral.com.")
        elif not local and not host.endswith("ringcentral.com"):
            warnings.append(f"RC_SERVER_URL host '{host}' is not a ringcentral.com address. Check for a typo.")
    for k in ("RC_CLIENT_ID", "RC_CLIENT_SECRET"):
        v = os.environ.get(k, "")
        if v.strip() and (any(c in v for c in '{}" ,:') or len(v.strip()) < 10):
            problems.append(f"{k} does not look right (it contains spaces/braces/quotes, or is very short). Copy it again "
                            "from the Developer Console, app > Credentials, with nothing extra.")
    jwt = os.environ.get("RC_JWT", "")
    if jwt.strip():
        p, info = inspect_jwt(jwt)
        problems += p
        iss = urllib.parse.urlparse(str(info.get("iss") or "")).hostname
        if iss and url and iss != urllib.parse.urlparse(url).hostname and not p:
            warnings.append(f"The JWT was issued by {iss} but RC_SERVER_URL is {url}. Production and sandbox "
                            "credentials do not work against each other.")
    return problems, warnings


def jwt_environment_hint():
    """Extra advice appended to authentication failures when the JWT clearly belongs to another environment."""
    _, info = inspect_jwt(os.environ.get("RC_JWT", ""))
    iss = urllib.parse.urlparse(str(info.get("iss") or "")).hostname
    srv = urllib.parse.urlparse(os.environ.get("RC_SERVER_URL", "")).hostname
    if iss and srv and iss != srv:
        return [f"Your JWT was issued by {iss} but RC_SERVER_URL uses {srv}. Use the matching server URL."]
    return []


def parse_error_body(body):
    """Best-effort (error, errorCode, message) from a RingCentral error body (formats vary by endpoint)."""
    try:
        j = json.loads(body)
        if not isinstance(j, dict):
            raise ValueError
    except ValueError:
        return "", "", body.strip()[:300]
    errs = j.get("errors")
    first = errs[0] if isinstance(errs, list) and errs and isinstance(errs[0], dict) else {}
    return (str(j.get("error", "")), str(first.get("errorCode") or j.get("errorCode") or ""),
            str(j.get("error_description") or j.get("message") or first.get("message") or ""))


def api_error(code, body, phase):
    """Turn an HTTP error from RingCentral into advice. `phase` is 'token' (login) or 'search' (Audit Trail).
    The server's own words are always included as Details, because they are the most precise clue."""
    err, ecode, msg = parse_error_body(body)
    detail = f"HTTP {code}" + "".join(f" | {x}" for x in (err, ecode, msg) if x)
    text = f"{err} {ecode} {msg}".lower()
    if phase == "token":
        if "unparseable" in text or ecode == "OAU-331":
            return CollectorError("RingCentral could not read the JWT, so login failed.",
                                  ["Open .env and check the RC_JWT line holds ONLY the long token starting 'eyJ', on one line, "
                                   "with no braces, quotes or spaces (a pasted JSON block leaves just '{').",
                                   "If unsure, create a new JWT credential and paste it again."], detail)
        if "audience" in text or ecode == "OAU-336":   # seen in practice when the token is not a genuine RingCentral JWT
            return CollectorError("RingCentral says this JWT was not issued for this server.",
                                  ["RC_JWT must be the genuine token copied from the Developer Console (Credentials), unedited and complete.",
                                   "Check RC_SERVER_URL matches the environment the JWT was created in: production is "
                                   "https://platform.ringcentral.com, sandbox is https://platform.devtest.ringcentral.com.",
                                   "If in doubt, create a new JWT credential and paste it again."] + jwt_environment_hint(), detail)
        if err == "invalid_grant" or "jwt" in text or "assertion" in text:
            return CollectorError("RingCentral rejected the JWT.",
                                  ["The JWT may have expired or been deleted or revoked: create a new one (Developer Console > Credentials > Create JWT).",
                                   "The JWT may not be allowed to use this app: when creating it, allow apps in your organization "
                                   "or add this app's Client ID.",
                                   "The user the JWT belongs to may be disabled or deleted. Use an active (ideally dedicated) admin user.",
                                   "Check the JWT and RC_SERVER_URL are for the same environment (production vs sandbox)."]
                                  + jwt_environment_hint(), detail)
        if err in ("invalid_client", "unauthorized_client") or code == 401:
            return CollectorError("RingCentral did not accept the app's Client ID and Client Secret.",
                                  ["Copy RC_CLIENT_ID and RC_CLIENT_SECRET again from the Developer Console (Apps > your app > Credentials).",
                                   "Make sure they belong to the SAME app the JWT is meant for, and that the app's authentication is 'JWT auth flow'.",
                                   "If the secret was regenerated, update it in .env."] + jwt_environment_hint(), detail)
        return CollectorError("Login to RingCentral failed.",
                              ["Run  python rc_audit_trail.py --check  to test each setting separately.",
                               "Compare the Details line with README > Troubleshooting."] + jwt_environment_hint(), detail)
    # phase == "search"
    if code == 401:
        return CollectorError("RingCentral rejected the access token while reading the Audit Trail.",
                              ["Run again; a fresh token is requested every run.",
                               "If it keeps happening, re-check the JWT and app credentials with --check."], detail)
    if code == 403:
        return CollectorError("RingCentral says this app or user is not allowed to read the Audit Trail.",
                              ["App side: in the Developer Console open your app > Settings (or Security) and make sure the "
                               "'Read Audit Trail' permission is added and saved. (It is beta; you may add it yourself.)",
                               "User side: the user the JWT belongs to must have an admin role that can view the Audit Trail "
                               "(check in Admin Portal > Users > Roles / your Super Admin). The app permission AND the user's role are both needed.",
                               "After changing app permissions, wait a minute and run again."], detail)
    if code == 404:
        return CollectorError("The Audit Trail address was not found.",
                              ["Check RC_SERVER_URL is exactly https://platform.ringcentral.com (no extra path).",
                               "The Audit Trail API may not be available for this account type; ask RingCentral support."], detail)
    if code == 400:
        return CollectorError("RingCentral rejected the Audit Trail request.",
                              ["Try a smaller page size: add RC_PER_PAGE=100 to .env.",
                               "Try a shorter window, for example  --days 7  (RingCentral keeps at most 180 days)."], detail)
    if code >= 500:
        return CollectorError("RingCentral reported a server problem.",
                              ["This is usually temporary: wait a few minutes and run again.",
                               "Check RingCentral's service status page if it continues."], detail)
    return CollectorError(f"The Audit Trail request failed (HTTP {code}).",
                          ["Compare the Details line with README > Troubleshooting."], detail)


def network_error(exc, url):
    """Explain a connection-level failure (no HTTP response was received)."""
    host = urllib.parse.urlparse(url).hostname
    reason = getattr(exc, "reason", exc)
    detail = f"{type(reason).__name__}: {reason}"
    if isinstance(reason, ssl.SSLError):
        return CollectorError(f"A secure connection to {host} could not be established (certificate problem).",
                              ["This is typical behind a company proxy or antivirus that inspects HTTPS. Ask IT for the company's root "
                               "certificate bundle and point Python at it: set the environment variable SSL_CERT_FILE to that .pem file.",
                               "Check the computer's date and time are correct."], detail)
    if isinstance(reason, socket.gaierror):
        return CollectorError(f"The address {host} could not be found (DNS).",
                              ["Check the computer is online and VPN/proxy settings are right.",
                               "Check RC_SERVER_URL in .env for typos."], detail)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return CollectorError(f"Timed out waiting for {host}.",
                              ["Check your internet connection and that a firewall is not blocking outbound HTTPS.",
                               "If you use a proxy, set HTTPS_PROXY (for example http://proxy.example.com:8080) and try again."], detail)
    return CollectorError(f"Could not connect to {host}.",
                          ["Check the internet connection, VPN and firewall (outbound HTTPS, port 443).",
                           "If you use a proxy, set the HTTPS_PROXY environment variable."], detail)


def http(method, url, headers=None, data=None, retries=5, phase="search"):
    """Send one HTTP request and return the decoded JSON body.

    Retried automatically: HTTP 429 (waits for Retry-After; the Audit Trail API is on the heavy usage plan),
    HTTP 5xx and connection failures (short growing pauses). Everything else raises CollectorError
    with advice. Secrets are never printed (the token request body is not shown, even in --verbose).
    """
    vsay(f"{method} {url}")
    for attempt in range(retries):
        last = attempt == retries - 1
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
                vsay(f"-> HTTP {r.status}, {len(raw)} bytes")
            try:
                return json.loads(raw or "{}")
            except ValueError:
                raise CollectorError("RingCentral answered with something that is not JSON.",
                                     ["This usually means a proxy, VPN or firewall page is intercepting the request.",
                                      "Check RC_SERVER_URL and try from a different network."], raw.strip()[:200])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            vsay(f"-> HTTP {e.code}")
            if e.code == 429 and not last:
                try:
                    wait = int(float(e.headers.get("Retry-After", 30)))
                except ValueError:
                    wait = 30
                say(f"  rate limited by RingCentral, waiting {wait}s (attempt {attempt + 1} of {retries})", to_err=True)
                time.sleep(wait)
                continue
            if e.code >= 500 and not last:
                say(f"  RingCentral server error {e.code}, retrying shortly (attempt {attempt + 1} of {retries})", to_err=True)
                time.sleep(2 * (attempt + 1))
                continue
            raise api_error(e.code, body, phase)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            if not last:
                say(f"  network problem ({type(getattr(e, 'reason', e)).__name__}), retrying (attempt {attempt + 1} of {retries})", to_err=True)
                time.sleep(2 * (attempt + 1))
                continue
            raise network_error(e, url)
    raise CollectorError("Gave up after repeated attempts.", ["Try again later."])


def get_token():
    """Exchange the app's client id/secret plus a user's JWT for a short-lived access token (JWT bearer flow)."""
    problems, warnings = check_settings()
    for w in warnings:
        say("Note: " + w, "WARN", to_err=True)
    if problems:
        raise CollectorError(f"Setup needs attention ({len(problems)} problem{'s' if len(problems) != 1 else ''}).", problems)
    basic = base64.b64encode(f"{os.environ['RC_CLIENT_ID']}:{os.environ['RC_CLIENT_SECRET']}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": os.environ["RC_JWT"],
    }).encode()
    tok = http("POST", os.environ["RC_SERVER_URL"].rstrip("/") + "/restapi/oauth/token",
               {"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"}, body, phase="token")
    if not isinstance(tok, dict) or not tok.get("access_token"):
        raise CollectorError("Login succeeded but RingCentral returned no access token.",
                             ["Run again; if it repeats, re-create the JWT and check the app is a 'REST API App' with JWT auth flow."],
                             "keys returned: " + ", ".join(sorted(tok)) if isinstance(tok, dict) else "")
    return tok["access_token"]


# =============================================================================
# 3. FETCHING FROM THE AUDIT TRAIL API
# =============================================================================
def iso(dt):
    """Format a datetime the way the API expects (UTC, milliseconds, trailing Z)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def search_page(token, start, end, page, per_page):
    """Request one page of audit events and check the response has the expected shape."""
    url = os.environ["RC_SERVER_URL"].rstrip("/") + "/restapi/v1.0/account/~/audit-trail/search"
    body = {"eventTimeFrom": iso(start), "eventTimeTo": iso(end), "page": page, "perPage": per_page,
            "includeAdmins": True, "includeHidden": True,
            "searchParameters": []}   # empty = no server-side filtering; we capture everything and filter locally
    res = http("POST", url, {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
               json.dumps(body).encode(), phase="search")
    if not isinstance(res, dict) or not isinstance(res.get("records"), list):
        keys = ", ".join(sorted(res)) if isinstance(res, dict) else type(res).__name__
        raise CollectorError("The Audit Trail response was not in the expected format (no 'records' list).",
                             ["The beta API may have changed. Run with --verbose and compare with RingCentral's Audit Trail "
                              "documentation; update search_page() in rc_audit_trail.py if the field was renamed."],
                             "response contained: " + keys)
    return res


def fetch(days):
    """Return every audit event from the last `days` days, following the API's paging."""
    if not 1 <= days <= 180:
        raise CollectorError(f"The window of {days} days is not allowed.",
                             ["Choose 1 to 180 days (RingCentral keeps at most 180). Use --days N or change schedule.json."])
    token = get_token()
    per_page = get_per_page()
    now = datetime.now(timezone.utc)
    records, page = [], 1
    while True:
        res = search_page(token, now - timedelta(days=days), now, page, per_page)
        batch = res["records"]
        records += batch
        say(f"  page {page}: {len(batch)} records")
        total_pages = (res.get("paging") or {}).get("totalPages")
        # Stop on a short page, on the last page when the API reports a count, or at the API cap.
        if len(batch) < per_page or (total_pages and page >= total_pages) or len(records) >= MAX_RECORDS:
            break
        page += 1
    if len(records) >= MAX_RECORDS:
        say(f"Warning: reached the API cap of {MAX_RECORDS} records; use a shorter window to avoid gaps.", "WARN", to_err=True)
    if not records:
        say(f"Note: RingCentral returned no audit events for the last {days} days. That is normal for a quiet account. If you "
            "expected events, check the JWT's user can view the Audit Trail and the window is long enough.", "WARN")
    return records


# =============================================================================
# 4. SHAPING RECORDS
# =============================================================================
def flatten(obj, prefix=""):
    """Turn nested JSON into one flat dict with dotted keys, e.g. initiator.name.

    Why: the API's response schema is not fully documented and can change (beta). Flattening
    means every field becomes a CSV/viewer column automatically, with nothing hard-coded.
    """
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        elif isinstance(v, list):
            # lists of simple values become "a, b"; lists of objects are kept as JSON text
            out[key] = json.dumps(v) if v and not all(isinstance(x, (str, int, float)) for x in v) \
                else ", ".join(map(str, v))
        else:
            out[key] = "" if v is None else v
    return out


def record_id(rec, flat):
    """Stable identity for de-duplication: the event's own id, or a hash of the record if it has none."""
    for k in ("eventId", "id", "uuid"):
        if flat.get(k):
            return str(flat[k])
    return hashlib.sha1(json.dumps(rec, sort_keys=True).encode()).hexdigest()


def clean_records(raw):
    """Keep only dict records; warn (once) if anything else slipped in so a bad row cannot crash the run."""
    good = [r for r in raw if isinstance(r, dict)]
    if len(good) != len(raw):
        say(f"Warning: skipped {len(raw) - len(good)} record(s) that were not JSON objects.", "WARN", to_err=True)
    return good


def write_csv(path, rows):
    """Write rows to CSV with the union of all columns. utf-8-sig so Excel opens it correctly."""
    cols = list(dict.fromkeys(c for r in rows for c in r))
    try:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
    except OSError as e:
        raise io_error(e, path, "the CSV file")


# =============================================================================
# 5. STORAGE (every store is optional and independent)
#    Each save_* returns (all_rows_in_store, number_of_new_events) or raises; main() turns a failure into
#    a warning with advice and carries on with the other stores.
# =============================================================================
def save_json(out_dir, rows):
    """Cumulative JSON file keyed by event id. Re-saving the same event never duplicates it.
    A damaged file (for example from a crash or power loss) is renamed aside, never deleted, so the
    scheduled job heals itself; the data can be rebuilt from the raw_<date>.json files."""
    mf = out_dir / "audit_master.json"
    master = {}
    if mf.exists():
        try:
            master = json.loads(mf.read_text(encoding="utf-8"))
            if not isinstance(master, dict):
                raise ValueError("not a JSON object")
        except ValueError:
            aside = mf.with_name(f"audit_master.corrupt-{datetime.now():%Y%m%d-%H%M%S}.json")
            mf.rename(aside)
            master = {}
            say(f"Warning: audit_master.json was damaged. It was kept as {aside.name} and a fresh one was started. "
                "To rebuild history, run with --from-json on each raw_<date>.json file.", "WARN", to_err=True)
    new = sum(1 for rid in rows if rid not in master)
    master.update(rows)
    tmp = mf.with_suffix(".tmp")    # write-then-replace so a crash cannot leave a half-written file
    try:
        tmp.write_text(json.dumps(master, indent=1), encoding="utf-8")
        os.replace(tmp, mf)
    except OSError as e:
        raise io_error(e, mf, "audit_master.json")
    return list(master.values()), new


# Database table: the most useful fields become real, queryable columns; the complete flattened
# record is kept in record_json so nothing is lost if the API adds fields. (column, source field)
DB_FIELDS = [("event_time", "eventTime"), ("action_id", "actionId"), ("event_type", "eventType"),
             ("initiator_name", "initiator.name"), ("initiator_ext", "initiator.extensionNumber"),
             ("initiator_role", "initiator.role"), ("target_name", "target.name"),
             ("target_ext", "target.extensionNumber"), ("target_type", "target.objectType"),
             ("client_ip", "clientIp"), ("comment", "comment")]
DB_COLUMNS = [c for c, _ in DB_FIELDS] + ["record_json"]


def db_values(flat):
    """Values for DB_COLUMNS, in order, from one flattened record."""
    return [str(flat.get(src, "")) for _, src in DB_FIELDS] + [json.dumps(flat)]


def upsert_rows(cur, ph, rows):
    """Insert new events and update existing ones (matched on id). `ph` is the driver's placeholder
    style (? or %s). One select/update/insert path works unchanged on SQLite, Postgres and SQL Server;
    first_seen is only written on insert so it records when an event was first collected."""
    now = datetime.now(timezone.utc).isoformat()
    sets = ", ".join(f"{c} = {ph}" for c in DB_COLUMNS)
    marks = ", ".join([ph] * (len(DB_COLUMNS) + 2))
    for rid, flat in rows.items():
        vals = db_values(flat)
        cur.execute(f"SELECT 1 FROM audit_events WHERE id = {ph}", (rid,))
        if cur.fetchone():
            cur.execute(f"UPDATE audit_events SET {sets} WHERE id = {ph}", vals + [rid])
        else:
            cur.execute(f"INSERT INTO audit_events (id, {', '.join(DB_COLUMNS)}, first_seen) VALUES ({marks})",
                        [rid] + vals + [now])


def table_ddl(text_type, key_type):
    """CREATE TABLE body; only the text/key types differ between database engines."""
    cols = ", ".join(f"{c} {text_type}" for c in DB_COLUMNS)
    return f"id {key_type}, {cols}, first_seen {text_type}"


def read_back(cur):
    """Return (total_rows, all_records) from an audit_events table."""
    cur.execute("SELECT COUNT(*) FROM audit_events")
    total = cur.fetchone()[0]
    cur.execute("SELECT record_json FROM audit_events")
    return total, [json.loads(r[0]) for r in cur.fetchall()]


def save_db(db_path, rows):
    """Cumulative SQLite store (built into Python, nothing to install)."""
    con = sqlite3.connect(db_path, timeout=30)   # wait up to 30 s if another program briefly holds the file
    try:
        cur = con.cursor()
        cur.execute(f"CREATE TABLE IF NOT EXISTS audit_events ({table_ddl('TEXT', 'TEXT PRIMARY KEY')})")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_time ON audit_events(event_time)")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_action ON audit_events(action_id)")
        before = read_back(cur)[0]
        upsert_rows(cur, "?", rows)
        con.commit()
        total, allrows = read_back(cur)
        return allrows, total - before
    finally:
        con.close()


def save_sql(kind, rows):
    """Optional server databases, same table as SQLite.
    kind='postgres': needs `pip install "psycopg[binary]"` and RC_PG_DSN.
    kind='mssql'   : needs `pip install pyodbc`, the Microsoft ODBC driver, and RC_MSSQL_CONN."""
    if kind == "postgres":
        dsn = os.environ.get("RC_PG_DSN")
        if not dsn:
            raise CollectorError("RC_PG_DSN is not set, so PostgreSQL cannot be used.",
                                 ["Add RC_PG_DSN=postgresql://USER:PASSWORD@HOST:5432/DBNAME to .env, or remove 'postgres' from --save / RC_SAVE."])
        try:
            import psycopg
        except ImportError:
            raise CollectorError("The PostgreSQL driver is not installed.", ['Run:  pip install "psycopg[binary]"'])
        con, ph = psycopg.connect(dsn, connect_timeout=15), "%s"
        ddl = f"CREATE TABLE IF NOT EXISTS audit_events ({table_ddl('TEXT', 'TEXT PRIMARY KEY')})"
    else:
        cs = os.environ.get("RC_MSSQL_CONN")
        if not cs:
            raise CollectorError("RC_MSSQL_CONN is not set, so SQL Server cannot be used.",
                                 ["Add RC_MSSQL_CONN=DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=...;UID=...;PWD=... to .env, "
                                  "or remove 'mssql' from --save / RC_SAVE."])
        try:
            import pyodbc
        except ImportError:
            raise CollectorError("The SQL Server driver is not installed.",
                                 ["Run:  pip install pyodbc", "Install the Microsoft ODBC Driver 18 for SQL Server (free download from Microsoft)."])
        con, ph = pyodbc.connect(cs, timeout=15), "?"
        ddl = ("IF OBJECT_ID('audit_events','U') IS NULL "
               f"CREATE TABLE audit_events ({table_ddl('NVARCHAR(MAX)', 'NVARCHAR(450) PRIMARY KEY')})")
    try:
        cur = con.cursor()
        cur.execute(ddl)
        before = read_back(cur)[0]
        upsert_rows(cur, ph, rows)
        con.commit()
        total, allrows = read_back(cur)
        return allrows, total - before
    finally:
        con.close()


def store_hint(name, exc):
    """One-sentence advice for a failed store, chosen from the kind of error."""
    msg = str(exc)
    if isinstance(exc, CollectorError):
        return str(exc)
    if isinstance(exc, PermissionError):
        return "access was denied. Close the file if another program has it open, or check folder permissions."
    if isinstance(exc, sqlite3.OperationalError):
        low = msg.lower()
        if "locked" in low or "busy" in low:
            return "the database file is locked by another program. Close any database viewer using it and run again."
        if "unable to open" in low:
            return "the database file cannot be created or opened. Check the --db-path / RC_DB_PATH folder exists and is writable."
        if "readonly" in low:
            return "the database file is read-only. Check file permissions."
    if isinstance(exc, sqlite3.DatabaseError) and "not a database" in msg.lower():
        return "that file is not a valid SQLite database (damaged, or the wrong file). Rename it and a new one will be created."
    if name in ("postgres", "mssql"):
        return (f"could not use the database: {type(exc).__name__}: {msg[:200]}. Check host, port, name, user, password, "
                "firewall and that the user may create tables and insert rows.")
    if getattr(exc, "errno", None) == 28:
        return "the disk is full."
    return f"{type(exc).__name__}: {msg[:200]}"


# =============================================================================
# 6. SCHEDULE SETTINGS (stored in schedule.json, applied to Windows Task Scheduler)
# =============================================================================
TASK_NAME = "RC Audit Trail Collector"
SCHEDULE_FILE = HERE / "schedule.json"
DEFAULT_SCHEDULE = {"enabled": True, "every_days": 30, "time": "07:00", "window_days": 35}
_warned_schedule = False


def load_schedule():
    """Saved settings merged over the defaults. A missing file means defaults; a damaged file means
    defaults plus one clear warning (silently ignoring it would hide a typo)."""
    global _warned_schedule
    cfg = dict(DEFAULT_SCHEDULE)
    try:
        cfg.update(json.loads(SCHEDULE_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as e:
        if not _warned_schedule:
            _warned_schedule = True
            say(f"Warning: schedule.json could not be read ({e}). Using the defaults. Fix the file, or delete it to reset.",
                "WARN", to_err=True)
    return cfg


def validate_schedule(c):
    """Check and normalise settings (they arrive from a web page, so never trust them).
    Raises ValueError with a message that is safe to show the user."""
    try:
        out = {"enabled": bool(c["enabled"]), "every_days": int(c["every_days"]),
               "time": str(c["time"]), "window_days": int(c["window_days"])}
    except (KeyError, TypeError, ValueError):
        raise ValueError("Schedule settings are incomplete or not numbers.")
    if not 1 <= out["every_days"] <= 365:
        raise ValueError("Run every: choose 1 to 365 days.")
    if not 1 <= out["window_days"] <= 180:
        raise ValueError("Retrieval window: choose 1 to 180 days (RingCentral keeps 180).")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", out["time"]):   # also guarantees no shell metacharacters
        raise ValueError("Time must look like 07:00.")
    if out["window_days"] < out["every_days"]:
        raise ValueError("The retrieval window must be at least as long as the run interval, or events would be missed.")
    return out


def ps(command):
    """Run a PowerShell command and return its output. Scheduling is Windows-only."""
    if os.name != "nt":
        raise CollectorError("Scheduling from this tool is Windows-only.",
                             ["On Linux/macOS use cron or a systemd timer (README > Scheduling on other platforms)."])
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                           capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise CollectorError("PowerShell was not found on this computer.",
                             ["Windows PowerShell ships with Windows; check that C:\\Windows\\System32\\WindowsPowerShell is on PATH."])
    except subprocess.TimeoutExpired:
        raise CollectorError("Windows Task Scheduler did not answer within 60 seconds.",
                             ["Try again; if it repeats, restart the 'Task Scheduler' service or the computer."])
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip() or "PowerShell failed"
        if "denied" in err.lower() or "0x80070005" in err:
            raise CollectorError("Windows denied permission to change the scheduled task.",
                                 ["Right-click PowerShell > Run as administrator and try again.",
                                  "Group policy on managed PCs can block this; ask IT, or create the task manually in Task Scheduler."],
                                 err[:300])
        raise CollectorError("Windows Task Scheduler reported an error.", ["Read the details below, then try again."], err[:300])
    return r.stdout.strip()


def q(v):
    """Quote a value as a PowerShell single-quoted string (embedded quotes doubled)."""
    return "'" + str(v).replace("'", "''") + "'"


def apply_schedule(cfg):
    """Create/replace the scheduled task (enabled), or disable it if it exists (off).

    The task runs this script with no arguments, so the retrieval window is read from
    schedule.json at run time. Settings chosen so a 30-day cadence is dependable:
      StartWhenAvailable  - catch up after the PC was off at the scheduled time
      AllowStartIfOnBatteries / DontStopIfGoingOnBatteries - laptops still run it
      RestartCount 3 / 1 hour - retry transient failures
    """
    if cfg["enabled"]:
        ps(f"""$a = New-ScheduledTaskAction -Execute {q(sys.executable)} -Argument {q('"' + str(Path(__file__).resolve()) + '"')} -WorkingDirectory {q(HERE)}
$t = New-ScheduledTaskTrigger -Daily -DaysInterval {cfg['every_days']} -At {q(cfg['time'])}
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName {q(TASK_NAME)} -Action $a -Trigger $t -Settings $s -Description 'Pulls RingCentral Audit Trail on a schedule' -Force | Out-Null""")
    else:
        ps(f"if (Get-ScheduledTask -TaskName {q(TASK_NAME)} -ErrorAction SilentlyContinue) {{ Disable-ScheduledTask -TaskName {q(TASK_NAME)} | Out-Null }}")


def task_status():
    """Real task state for the settings panel: {exists, state, next, last}. Never raises."""
    try:
        out = ps(f"""$t = Get-ScheduledTask -TaskName {q(TASK_NAME)} -ErrorAction SilentlyContinue
if ($t) {{ $i = Get-ScheduledTaskInfo -TaskName {q(TASK_NAME)}
 [pscustomobject]@{{exists=$true; state=[string]$t.State;
  next=$(if ($i.NextRunTime -and $i.NextRunTime.Year -gt 2000) {{ $i.NextRunTime.ToString('s') }} else {{ $null }});
  last=$(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) {{ $i.LastRunTime.ToString('s') }} else {{ $null }})}} | ConvertTo-Json }}
else {{ '{{"exists":false}}' }}""")
        return json.loads(out)
    except Exception as ex:
        return {"exists": False, "error": str(ex)}


# =============================================================================
# 7. HTML VIEWER AND LOCAL HELPER
# =============================================================================
def write_viewer(path, rows, meta):
    """Fill viewer_template.html with the data and write one self-contained file (no server needed to read it)."""
    tpl = HERE / "viewer_template.html"
    try:
        html = tpl.read_text(encoding="utf-8")
    except OSError:
        raise CollectorError("viewer_template.html is missing or unreadable.",
                             [f"It must sit next to rc_audit_trail.py (looked in {HERE}). Restore it from the original download."])
    html = (html.replace("__DATA__", json.dumps(rows).replace("</", "<\\/"))   # </ escaped so data can't close the <script>
                .replace("__META__", json.dumps(meta))
                .replace("__SCHEDULE__", json.dumps(load_schedule())))
    write_text(path, html, "the viewer page")


def latest_viewer(out):
    """Newest dated viewer file (the YYYYMMDD in the name sorts chronologically)."""
    files = sorted(out.glob("audit_viewer_*.html"))
    return files[-1] if files else None


def serve(out, port=8765, open_browser=True):
    """Local-only helper behind `--serve`.

    A web page cannot change Windows Task Scheduler by itself, so this small server (bound to
    127.0.0.1 only) serves the newest viewer and exposes endpoints the Schedule settings panel uses:
      GET  /api/schedule  current settings + real task status + result of the last collection
      POST /api/schedule  validate, apply to Task Scheduler, save schedule.json
      POST /api/run       start a collection now
    Protections: loopback only; Host header must be localhost (blocks DNS-rebinding); every API call
    needs a random per-session token that is injected into the page when it is served, so other
    websites you visit cannot drive it.
    """
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # keep the console quiet
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _authed(self):
            return self._host_ok() and secrets.compare_digest(self.headers.get("X-Token", ""), token)

        def _state(self):
            return {"config": load_schedule(), "task": task_status(), "last_run": read_last_run(out)}

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, '{"error":"forbidden"}')
            if self.path in ("/", "/index.html"):
                f = latest_viewer(out)
                if not f:
                    return self._send(404, "<h3>No viewer page yet</h3><p>Collect data first: run "
                                           "<code>python rc_audit_trail.py</code> (or "
                                           "<code>python rc_audit_trail.py --from-json docs/sample_events_FICTIONAL.json</code> to try "
                                           "it with sample data), then reload this page.</p>", "text/html")
                # the token is injected only into pages served by this process
                return self._send(200, f.read_text(encoding="utf-8").replace('"__TOKEN__"', json.dumps(token)), "text/html")
            if self.path == "/api/schedule" and self._authed():
                return self._send(200, json.dumps(self._state()))
            self._send(404, '{"error":"not found"}')

        def do_POST(self):
            if not self._authed():
                return self._send(403, '{"error":"forbidden"}')
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
                if self.path == "/api/schedule":
                    cfg = validate_schedule(body)
                    apply_schedule(cfg)                       # apply first: only save what Windows accepted
                    write_text(SCHEDULE_FILE, json.dumps(cfg, indent=1), "schedule.json")
                    return self._send(200, json.dumps(self._state()))
                if self.path == "/api/run":
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve())], cwd=str(HERE))
                    return self._send(200, '{"started":true}')
            except (ValueError, RuntimeError, subprocess.SubprocessError) as ex:   # CollectorError is a RuntimeError
                return self._send(400, json.dumps({"error": redact(str(ex))}))
            self._send(404, '{"error":"not found"}')

    srv = None
    for p in range(port, port + 20):          # first free port from 8765 upward
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if not srv:
        raise CollectorError(f"No free local port between {port} and {port + 19}.",
                             ["Close other programs using those ports (a previous copy of this helper may still be running), and try again."])
    url = f"http://127.0.0.1:{port}/"
    say(f"Viewer running at {url}  (this computer only). Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# =============================================================================
# 8. DIAGNOSTICS: python rc_audit_trail.py --check
#    Tests the setup one step at a time and says exactly what to fix. Safe to run any time; it only
#    reads (one tiny Audit Trail query) and writes a temporary test file in the output folder.
# =============================================================================
def run_checks(out, save_spec):
    results = {"FAIL": 0, "WARN": 0}

    def line(status, text, fix=()):
        if status in results:
            results[status] += 1
        print(f"  [{status:^4}] {text}")
        for f in fix:
            print(f"           -> {f}")

    print("Checking your setup...\n")
    line("OK", f"Python {sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}")
    line("OK" if (HERE / "viewer_template.html").exists() else "FAIL", "viewer_template.html present",
         [] if (HERE / "viewer_template.html").exists() else ["Restore viewer_template.html next to rc_audit_trail.py."])
    try:
        ensure_dir(out)
        line("OK", f"Output folder is writable ({out})")
    except CollectorError as e:
        line("FAIL", e.what, e.fix)

    problems, warnings = check_settings()
    if (HERE / ".env").exists():
        line("OK", ".env file found")
    for p in problems:
        line("FAIL", p)
    for w in warnings:
        line("WARN", w)
    if not problems:
        line("OK", "Settings are filled in and look well-formed")
        host = urllib.parse.urlparse(os.environ["RC_SERVER_URL"]).hostname
        try:
            socket.getaddrinfo(host, 443)
            line("OK", f"Can find {host} on the network")
        except OSError as e:
            ne = network_error(urllib.error.URLError(e), os.environ["RC_SERVER_URL"])
            line("FAIL", ne.what, ne.fix)
        else:
            try:
                token = get_token()
                line("OK", "Logged in to RingCentral with the Client ID, Client Secret and JWT")
                try:
                    res = search_page(token, datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc), 1, 1)
                    line("OK", "Audit Trail access works (permission and role are fine)")
                    if not res["records"]:
                        line("INFO", "No audit events in the last 24 hours; that is normal for a quiet account")
                except CollectorError as e:
                    line("FAIL", e.what + (f"  [{e.detail}]" if e.detail else ""), e.fix)
            except CollectorError as e:
                line("FAIL", e.what + (f"  [{e.detail}]" if e.detail else ""), e.fix)

    for name in sorted(parse_targets(save_spec) - {"none", "both"}):
        if name == "json":
            line("OK", "Storage: JSON file (nothing to configure)")
        elif name == "db":
            line("OK", "Storage: SQLite (built in)")
        elif name == "postgres":
            ok = os.environ.get("RC_PG_DSN")
            try:
                import psycopg  # noqa: F401
                drv = True
            except ImportError:
                drv = False
            line("OK" if ok and drv else "FAIL", "Storage: PostgreSQL",
                 ([] if ok else ["Set RC_PG_DSN in .env."]) + ([] if drv else ['Run: pip install "psycopg[binary]"']))
        elif name == "mssql":
            ok = os.environ.get("RC_MSSQL_CONN")
            try:
                import pyodbc  # noqa: F401
                drv = True
            except ImportError:
                drv = False
            line("OK" if ok and drv else "FAIL", "Storage: SQL Server",
                 ([] if ok else ["Set RC_MSSQL_CONN in .env."]) +
                 ([] if drv else ["Run: pip install pyodbc, and install the Microsoft ODBC Driver 18 for SQL Server."]))

    if os.name == "nt":
        t = task_status()
        if t.get("error"):
            line("WARN", "Could not read Windows Task Scheduler", [t["error"][:200]])
        elif not t.get("exists"):
            line("INFO", "No scheduled task yet (optional). Create one from the viewer's Schedule settings, or run --apply-schedule")
        else:
            line("OK", f"Scheduled task '{TASK_NAME}': {t.get('state')}, next run {t.get('next') or 'none'}")
    else:
        line("INFO", "Not Windows: use cron or a systemd timer for scheduling (README > Scheduling on other platforms)")

    lr = read_last_run(out)
    if lr:
        line("OK" if lr.get("ok") else "WARN", f"Last collection ({lr.get('time')}): {lr.get('message')}",
             [] if lr.get("ok") else [f"Details are in {out / 'run_log.txt'}"])
    print()
    if results["FAIL"]:
        print(f"{results['FAIL']} problem(s) found. Fix the items marked FAIL (the -> lines say how), then run --check again.")
        return 1
    print("All checks passed." + (f" ({results['WARN']} warning(s) above.)" if results["WARN"] else ""))
    return 0


# =============================================================================
# 9. COMMAND LINE ENTRY POINT
# =============================================================================
def parse_targets(spec):
    """Turn a --save value like 'json,db' into a set of store names ('both' = json + db)."""
    targets = {t for t in spec.lower().replace(" ", "").split(",") if t}
    if "both" in targets:
        targets |= {"json", "db"}
    unknown = targets - {"json", "db", "both", "none", "postgres", "mssql"}
    if unknown:   # a typo is reported, never fatal
        say(f"Note: ignoring unknown --save value(s): {', '.join(sorted(unknown))}. "
            "Valid: json, db, postgres, mssql, both, none.", "WARN", to_err=True)
    return targets


def load_from_json(path):
    """Read a saved API response (a list of records, or an object with a 'records' list) for --from-json."""
    p = Path(path)
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CollectorError(f"The file given to --from-json was not found: {p}",
                             ["Check the path and spelling. Saved responses are named raw_<date>.json in the output folder."])
    except ValueError as e:
        raise CollectorError(f"{p.name} is not valid JSON.", ["Use a file written by this tool (raw_<date>.json), unedited."], str(e))
    except OSError as e:
        raise CollectorError(f"{p.name} could not be read.", ["Check the file is not locked or protected."], str(e))
    records = j.get("records") if isinstance(j, dict) else j
    if not isinstance(records, list):
        raise CollectorError(f"{p.name} does not contain a list of audit events.",
                             ["Expected a JSON list of events, or an object with a 'records' list."])
    return records


def collect(a, out):
    """One collection run: fetch (or load), shape, write outputs, save to the chosen stores.
    Returns (message, exit_code)."""
    days = a.days or int(load_schedule()["window_days"])
    stamp = datetime.now().strftime("%Y%m%d")      # run date, used in every dated filename

    if a.from_json:
        raw, label = load_from_json(a.from_json), "from " + Path(a.from_json).name
    else:
        say(f"Fetching audit trail, last {days} days...")
        raw, label = fetch(days), f"last {days} days"
    raw = clean_records(raw)

    rows = {}
    for r in raw:
        f = flatten(r)
        rows[record_id(r, f)] = f                  # dict keyed by id = de-duplicated within the run
    write_csv(out / f"audit_trail_{stamp}.csv", list(rows.values()))

    # Storage is optional and independent: a problem with one store never blocks another.
    targets = parse_targets(a.save)
    chosen = [n for n in ("json", "db", "postgres", "mssql") if n in targets]
    allrows, new, saved, failed = list(rows.values()), len(rows), [], []   # with no store, the viewer shows just this run
    for name in chosen:
        try:
            if name == "json":
                write_text(out / f"raw_{stamp}.json", json.dumps(raw, indent=1), "the raw response file")
                allrows, new = save_json(out, rows)
                saved.append(str(out / "audit_master.json"))
            elif name == "db":
                db_path = Path(a.db_path) if a.db_path else out / "audit_trail.db"
                allrows, new = save_db(db_path, rows)
                saved.append(str(db_path))
            else:
                allrows, new = save_sql(name, rows)
                saved.append(name + " database")
        except Exception as ex:   # warn with advice and carry on with the remaining stores
            failed.append(name)
            say(f"Warning: could not save to {name}: {store_hint(name, ex)}", "WARN", to_err=True)

    # The viewer shows the full history of the last store that saved successfully (else just this run).
    meta = f"{label} | run {datetime.now():%Y-%m-%d %H:%M} | {len(allrows)} events ({new} new this run)"
    viewer = out / f"audit_viewer_{stamp}.html"
    write_viewer(viewer, allrows, meta)
    say(f"{len(rows)} records this run, {new} new, {len(allrows)} in view.")
    say("Saved to: " + (", ".join(saved) if saved else "nothing (CSV and viewer only)"))
    say(f"Viewer: {viewer}")
    summary = f"{len(rows)} events collected, {new} new" + (f"; could not save to {', '.join(failed)} (see run_log.txt)" if failed else "")
    if chosen and not saved:   # every store you asked for failed: outputs exist, but history was NOT kept
        return summary + ". No chosen store could be saved.", 2
    return summary, 0


def main():
    global VERBOSE
    try:   # a Windows console in a legacy code page must never crash us on an unusual character
        sys.stdout.reconfigure(errors="replace")
        sys.stderr.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    load_env()   # first, so RC_SAVE / RC_DB_PATH in .env can supply argument defaults
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=None, help="look-back window; default comes from schedule.json (35). API keeps 180")
    ap.add_argument("--out", default=str(HERE / "output"), help="output folder")
    ap.add_argument("--save", default=os.environ.get("RC_SAVE", "json"),
                    help="where to keep history: json, db (SQLite), postgres, mssql, both (=json+db) or none "
                         "(comma list ok; default json, or RC_SAVE in .env)")
    ap.add_argument("--db-path", default=os.environ.get("RC_DB_PATH"), help="SQLite file (default output/audit_trail.db)")
    ap.add_argument("--check", action="store_true", help="test the whole setup step by step and explain any problem")
    ap.add_argument("--verbose", action="store_true", help="also print each API call (secrets are never shown)")
    ap.add_argument("--serve", action="store_true", help="open the viewer with working schedule settings (local helper)")
    ap.add_argument("--no-browser", action="store_true", help="with --serve: do not open a browser window")
    ap.add_argument("--apply-schedule", action="store_true", help="create/update the Windows task from schedule.json")
    ap.add_argument("--from-json", help="use a saved API response / record list instead of calling the API")
    a = ap.parse_args()
    VERBOSE = a.verbose
    out = Path(a.out)

    collecting = not (a.check or a.serve or a.apply_schedule)
    try:
        ensure_dir(out)
        init_log(out)
        if a.check:
            sys.exit(run_checks(out, a.save))
        if a.apply_schedule:
            try:
                cfg = validate_schedule(load_schedule())
            except ValueError as e:
                raise CollectorError(f"schedule.json has an invalid setting: {e}", [f"Fix {SCHEDULE_FILE}, or delete it to use the defaults."])
            apply_schedule(cfg)
            write_text(SCHEDULE_FILE, json.dumps(cfg, indent=1), "schedule.json")
            say(f"Schedule applied: {cfg}")
            return
        if a.serve:
            return serve(out, open_browser=not a.no_browser)
        say(f"--- collection started {datetime.now():%Y-%m-%d %H:%M:%S} ---")
        message, code = collect(a, out)
        write_last_run(out, code == 0, message)
        sys.exit(code)
    except CollectorError as e:
        say(e.report(), "ERROR", to_err=True)
        if collecting:   # short form for the viewer's status line: what happened, the server's words, and the first step to take
            write_last_run(out, False, e.what + (f" [{e.detail}]" if e.detail else "") + (f" First step: {e.fix[0]}" if e.fix else ""))
        sys.exit(1)
    except KeyboardInterrupt:
        say("Cancelled.", "WARN")
        sys.exit(130)
    except Exception as e:   # anything we did not foresee: keep the full detail in the log for support
        trace = redact(traceback.format_exc())
        if LOG_PATH:
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERROR unexpected error:\n{trace}\n")
            except OSError:
                pass
        err = CollectorError(f"Something unexpected went wrong ({type(e).__name__}: {redact(e)}).",
                             ["Run  python rc_audit_trail.py --check  to rule out a setup problem.",
                              "Run again with --verbose to see each step.",
                              f"The full technical detail is in {LOG_PATH or 'run_log.txt'}. It contains no secrets; attach it, with your "
                              "Python version and Windows version, when asking for help."])
        print(err.report(), file=sys.stderr)
        if collecting:
            write_last_run(out, False, f"Unexpected error: {type(e).__name__}: {redact(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
