# RC Audit Trail Scheduler

A small, dependency-free tool that **automatically collects your RingCentral account's Audit Trail on a schedule, keeps a de-duplicated history, and gives you a page where every field can be filtered**, so questions like *"which extensions were deleted, by whom, and when?"* take seconds instead of a manual export.

> **Please note:** This is an independent project built on RingCentral's public REST API. It is not an official RingCentral product and is not supported by RingCentral. The Audit Trail API is documented by RingCentral as **beta**, so field names and behaviour may change. Review the code and your organization's policies before running it against a production account.
>
> **All screenshots in this document use fictional sample data** (made-up people, extensions and reserved documentation IP addresses). Nothing here comes from a real account.

---

## Contents

1. [Why this exists](#why-this-exists)
2. [Features at a glance](#features-at-a-glance)
3. [What it looks like](#what-it-looks-like)
4. [How it works](#how-it-works)
5. [Quick start](#quick-start)
6. [**Setting up RingCentral, step by step**](#setting-up-ringcentral-step-by-step) (create the app, scopes, Client ID, Secret, JWT, `.env`)
7. [Repository layout](#repository-layout)
8. [Features in depth](#features-in-depth) (what each does, how, and why it was built that way)
9. [Configuration reference](#configuration-reference)
10. [Data reference](#data-reference)
11. [Security and privacy](#security-and-privacy)
12. [Limits and known caveats](#limits-and-known-caveats)
13. [**Troubleshooting**](#troubleshooting) (the `--check` command, logs, and a message-by-message guide)
14. [Extending it](#extending-it)
15. [Development and testing](#development-and-testing)

---

## Why this exists

**The short version:** RingCentral's Audit Trail is a **rolling window, not an archive.** It records administrative changes, for example *"Deleted Jane Doe, Ext. 1042"*, but it only keeps a limited history (the API documents at most **180 days and 10,000 records per search**), older events eventually fall out of reach, and there is no built-in scheduled export that saves them somewhere you control. If nobody looks in time, the answer to *"who deleted that extension?"* can simply be gone. This project closes that gap by collecting the Audit Trail **automatically, on a schedule, into a history you own**, and by making it easy to filter. (Check your own account's Audit Trail page for the date range it offers; limits can differ by account and may change.)

The Admin Portal's Audit Trail (Service Console > Audit Trail) is useful, but it has gaps for an account owner who needs to track changes over time:

| Gap | How this project closes it |
|---|---|
| Looking back is manual: someone has to open the page and download an export. | A scheduled task pulls the data automatically (default: every 30 days). |
| History is limited. The API keeps at most **180 days / 10,000 records** per search. | Each run is merged into a permanent store you control, so history outlives the 180-day window. |
| Spotting one kind of event, such as *extension deleted*, means scanning rows by eye. | A viewer with one-click quick filters, exact-action and "done by" dropdowns, a date range, search, and a filter under **every** column. |
| The team that owns the account may not want to hand out portal access just to answer an audit question. | Output is a CSV, an offline HTML page and (optionally) a database, all shareable. |

It was built first as a personal test on a single account and is packaged here as a **jumping-off point** for an administrator to adapt.

## Features at a glance

| Feature | Summary |
|---|---|
| **Automatic collection** | Pulls the last *N* days (default 35) from the Audit Trail API on a schedule. |
| **Captures everything** | No server-side filtering: every event and every field is kept. Filtering happens afterwards, locally. |
| **De-duplicated history** | Overlapping runs never create duplicates (matched on event ID). |
| **Choose where history lives** | JSON file, SQLite, PostgreSQL, SQL Server, any combination, or none. One failing never blocks another. |
| **Self-contained viewer** | One HTML file per run, no server and no internet needed to read it. Light by default, with a dark-mode switch. |
| **Plain-language filtering** | Search, date range, *Done by*, *Exact action*, quick-filter chips with live counts, per-column filters, removable "active filter" pills, CSV export of the filtered rows. |
| **On-page schedule settings** | Turn the schedule on or off, set how often, what time, and how many days each run retrieves, from the page itself. |
| **Dependable scheduling** | Catches up after the PC was off, runs on battery, retries failures. |
| **Built for troubleshooting** | A `--check` command that tests your setup step by step, plain-language errors with numbered fixes, a run log, and the last result shown on the page. |
| **Dated filenames** | Every per-run file carries the run date (`audit_viewer_20260115.html`). |
| **Zero required dependencies** | Python 3.9+ standard library only. Database drivers are optional. |

## What it looks like

*Sample data only.*

**The viewer, with nothing filtered.** Every event is shown until you choose a filter. The panel explains what each control does.

![Viewer overview](docs/screenshots/01-overview.png)

**One click to isolate deleted extensions.** The *Extension deleted* quick filter is highlighted, the table narrows to the matching events, and a removable pill shows exactly what is filtered ("Showing 3 of 47 events"). The numbers on the chips are how many events match each one.

![Extension deleted filter](docs/screenshots/02-filter-extension-deleted.png)

**Filters combine.** Here: anything deleted, done by *Alex Morgan*, from 1 January onward. Each active filter has its own pill with an **x** to remove it, plus **Clear all filters**.

![Combined filters](docs/screenshots/03-filter-combined.png)

**A filter under every column.** Type in any column's box to narrow by that field (here, target extension `1043`). A column with an active filter turns blue.

![Column filter](docs/screenshots/04-column-filter.png)

**Schedule settings.** Switch automatic collection on or off, choose the interval, time of day and how many days each run retrieves. The status line shows the real Windows task state, next run and last run, and whether the last collection succeeded.

![Schedule settings](docs/screenshots/05-schedule-settings.png)

**Guard rails.** If the retrieval window is shorter than the interval, events in between would be missed, so the page warns you immediately (and the helper refuses to save it).

![Schedule warning](docs/screenshots/06-schedule-warning.png)

**A failed scheduled run is visible.** Scheduled runs happen with nobody watching, so the settings panel reports the result of the last collection. Here the JWT has expired, and the page says so, with the first step to take and where to find more.

![Last run failed](docs/screenshots/09-last-run-failed.png)

**Read-only mode.** Opening the HTML file directly still works for viewing and filtering. The schedule panel shows the last saved settings and tells you how to enable editing (start the page with `open_viewer.bat`).

![Schedule settings read-only](docs/screenshots/07-schedule-readonly.png)

**Dark mode.** Defaults to light; one button switches, and the choice is remembered in that browser.

![Dark mode](docs/screenshots/08-dark-mode.png)

## How it works

```mermaid
flowchart LR
    S["Windows Task Scheduler<br/>(or you, manually)"] -->|runs| P["rc_audit_trail.py"]
    P -->|"1. JWT login"| O["RingCentral OAuth"]
    P -->|"2. search, paged"| A["Audit Trail API"]
    P --> C["audit_trail_DATE.csv"]
    P --> D[("JSON / SQLite /<br/>Postgres / SQL Server")]
    P --> V["audit_viewer_DATE.html"]
    P --> L["run_log.txt<br/>last_run.json"]
    H["open_viewer.bat<br/>(--serve, localhost only)"] -->|serves| V
    H -->|"apply settings"| S
    L -->|"shown on"| V
```

One collection run, in order:

1. **Read settings.** `.env` supplies credentials and optional defaults. `schedule.json` supplies the retrieval window (default 35 days). Every value is sanity-checked before any network call.
2. **Authenticate.** The client ID/secret plus a user's JWT are exchanged for a short-lived access token.
3. **Fetch.** `POST /restapi/v1.0/account/~/audit-trail/search` for the window, page by page, until the last page, a short page, or the API's 10,000-record cap.
4. **Flatten.** Each event's nested JSON becomes one flat row with dotted column names (`initiator.name`, `target.extensionNumber`).
5. **De-duplicate.** Rows are keyed by event ID.
6. **Write the CSV** for this run.
7. **Save to the stores you chose** (each independent). The merged history is read back from the last store that succeeded.
8. **Write the viewer** with that history embedded, named with the run date.
9. **Record the result** in `run_log.txt` and `last_run.json`, whether it worked or not.

Separately, `--serve` starts a tiny localhost-only helper so the viewer's *Schedule settings* panel can create, change or disable the Windows scheduled task (a web page cannot do that on its own).

## Quick start

### Prerequisites

* **Python 3.9 or newer.** Check with `python --version` in a Command Prompt or PowerShell window. If it is missing or older, install the current version from [python.org/downloads](https://www.python.org/downloads/) and **tick "Add python.exe to PATH"** on the first installer screen. No `pip install` is needed afterwards.
* **Windows** for the built-in scheduling and the settings panel. The collector, storage and viewer run on any OS; on Linux/macOS use cron or a systemd timer instead (see [Scheduling on other platforms](#scheduling-on-other-platforms)).
* A RingCentral account where you can reach the [Developer Console](https://developers.ringcentral.com), and an **admin** user who can see the Audit Trail. The next section walks through this.

### 1. Try it first with no credentials

The repository includes fictional sample events, so you can see everything working before touching your account:

```bash
python rc_audit_trail.py --from-json docs/sample_events_FICTIONAL.json --out output --save db
```

Then open `output/audit_viewer_<today>.html` in a browser. (`--from-json` rebuilds all outputs from a saved response without calling the API. It is also handy for re-processing a real `raw_<date>.json`.)

### 2. Set up RingCentral

Follow [Setting up RingCentral, step by step](#setting-up-ringcentral-step-by-step). It ends with four values in a `.env` file.

### 3. Test the setup

```bash
python rc_audit_trail.py --check
```

This tests Python, your settings, the network, login, and Audit Trail permission one step at a time, and tells you exactly what to fix if anything fails. Run it before the first real collection, and any time something stops working.

### 4. First real run

```bash
python rc_audit_trail.py
```

You will see one line per page fetched, then a summary and the path of the viewer. Open `output/audit_viewer_<today>.html`. Open `output/raw_<today>.json` to see exactly what the API returned. **Check the field names on your first run**: the response format is beta and the viewer adapts to whatever columns are present.

### 5. Use the viewer with schedule settings

Double-click **`open_viewer.bat`** (or run `python rc_audit_trail.py --serve`). It opens the newest viewer on `http://127.0.0.1:8765`. Click **Schedule settings**, adjust, and **Save schedule**. Keep the console window open while you use the page; close it when you are done.

### 6. Schedule it

Either use the **Schedule settings** panel (step 5), or, from a terminal in the project folder, create the task from `schedule.json`:

```bash
python rc_audit_trail.py --apply-schedule
```

Both create the same task, "RC Audit Trail Collector", with the settings in `schedule.json` (defaults: every 30 days at 07:00, retrieving the last 35 days).

## Setting up RingCentral, step by step

This section assumes you have never used the Developer Console. Plan on about 10 minutes. You will end up with **four values** and a `.env` file:

| You need | What it is |
|---|---|
| **Client ID** | the public name of the app you create |
| **Client Secret** | the app's password |
| **JWT** | a long token that lets the app act as a specific user, with no browser login |
| **The right permissions** | the app must be allowed to read the Audit Trail **and** the user the JWT belongs to must be allowed to view it |

> Screens change over time. The steps below follow RingCentral's own documentation as of writing. If a label differs slightly, look for the closest match, and see RingCentral's guides: [Register an app](https://developers.ringcentral.com/guide/getting-started/register-app), [Create a JWT credential](https://developers.ringcentral.com/guide/getting-started/create-credential), [App scopes](https://developers.ringcentral.com/guide/basics/permissions), and [Audit Trail](https://developers.ringcentral.com/guide/account/audit-trail).

### Before you begin

1. **A login that can create apps.** You need to sign in to the Developer Console with a RingCentral login. If the **Create App** button is greyed out later, your account lacks developer access: ask your RingCentral account administrator to grant it.
2. **Decide which user the collector acts as.** The JWT belongs to a **user**, not to the app, and everything the collector does is checked against *that user's* permissions. Recommended: create or choose a **dedicated service user** with an admin role that can view the Audit Trail (for example a Super Admin or an administrator role that includes it), rather than your own login. That way the collector keeps working if a person leaves or changes role.
3. **Pick a secure place for the secrets** (a password manager). You will handle a Client Secret and a JWT, both of which work like passwords. Never put them in chat, email or a shared document.
4. **Make sure you are in the right environment.** Use your **production** RingCentral account to read your real audit data. (The *sandbox* is a separate test environment with test data only. If the Developer Console shows a sandbox/production choice, make the app, the JWT and `RC_SERVER_URL` all belong to the same one.)

### Part A: create the app

1. Open [developers.ringcentral.com](https://developers.ringcentral.com) and **sign in** (top right).
2. Open the **Console**, then the **Apps** tab.
3. Click **Create App** (upper right).
4. **"What type of app are you creating?"** Choose **REST API App**, then **Next**.
5. **App details.**
   * **App name:** anything that helps you recognise it, for example `Audit Trail Collector`. (Internal use only.)
   * **Display name** and **App icons:** only if the form asks; not important for this use.
   * **Primary contact:** the person responsible for the app.
   * *"Will you promote this app in the App Gallery?"* Choose **No** (this is for internal use only).
6. **Authentication.** Choose **JWT auth flow**. Do **not** choose "3-legged OAuth" and do not enable the password flow. Leave refresh tokens off: the collector asks for a fresh token each run.
7. **Security / app scopes (permissions).** Select exactly these two:
   * **ReadAccounts**, the permission RingCentral's own JWT quick start adds.
   * **Read Audit Trail**, the permission the Audit Trail API requires. (Type "audit" in the scope search if there is one. The Audit Trail API is beta; RingCentral's guide says you may add this scope yourself. If a scope is marked *requires approval*, the form asks for a short justification, such as "Track administrative changes for auditing", and RingCentral reviews it.)
   * Select nothing else. Requesting only what you need is safer.
8. **Access preference.** Choose **Private**, so that only credentials from your own RingCentral account can use the app.
9. **Features.** Leave everything off.
10. Click **Create**. You land on the new app's dashboard.

> **Shortcut:** RingCentral's JWT quick start has a **Create JWT App** button that opens a form pre-filled with the JWT type, the ReadAccounts permission and private access. After it creates the app, still do Part B to add **Read Audit Trail**.

### Part B: check or add the Audit Trail permission afterwards

If you skipped it, or you get a "not allowed" error later:

1. Developer Console > **Apps** > click your app.
2. Open the app's **Settings** (its security/scopes section).
3. Make sure **Read Audit Trail** is ticked, and **Save**.
4. Wait a minute, then run `python rc_audit_trail.py --check` again.

### Part C: copy the Client ID and Client Secret

1. Developer Console > **Apps** > your app.
2. Find the **Client ID** and **Client Secret** on the app's dashboard (the **Credentials** area). The secret may be hidden until you click a **Show** or **Copy** control.
3. Copy each into your password manager now, with a label (see the table in Part E).

### Part D: create the JWT

1. In the Developer Console, **hover over your name in the upper-right corner** and choose **Credentials**.
2. Click **Create JWT**.
3. Fill in the form. Under **"What apps are permitted to use this credential?"**:
   * **Recommended: "Only specific apps of my choice".** Paste your app's **Client ID**, click **Add app**, and confirm the app appears in the table. This limits the damage if the JWT ever leaks.
   * Or "Allow apps within your organization" (simpler, broader).
4. Click **Create**.
5. The JWT appears: **one very long string that starts with `eyJ`** and contains two dots. Copy it **immediately** into your password manager. If you cannot see it again later, simply create a new JWT.

> **Whose JWT is it?** The one for whichever user is signed in to the Developer Console. To use a dedicated service user, sign in as that user for this step.

### Part E: put the values in `.env` (and record them safely)

**Where each value comes from, and where it goes:**

| Value | Where you found it | Line in `.env` | What it looks like |
|---|---|---|---|
| Server | which environment you use | `RC_SERVER_URL` | `https://platform.ringcentral.com` (production) |
| Client ID | Part C | `RC_CLIENT_ID` | letters and digits, 20+ characters |
| Client Secret | Part C | `RC_CLIENT_SECRET` | letters and digits, 40+ characters; **treat like a password** |
| JWT | Part D | `RC_JWT` | starts `eyJ`, hundreds of characters, two dots; **treat like a password** |

**Create the file:**

1. In File Explorer, open the project folder. Copy `.env.example` and paste it, then rename the copy to **`.env`** (a dot, then `env`, no other extension). Or in a terminal opened in that folder: `copy .env.example .env`.
2. If Windows hides file extensions, make sure the file is not really `.env.txt`: in File Explorer choose **View > Show > File name extensions**.
3. Open `.env` in Notepad or VS Code and fill in the four lines. Rules:
   * **One value per line, nothing else on the line.** No quotes, no braces, no trailing spaces.
   * For `RC_JWT`, paste **only the token string**. Do not paste a whole JSON file or a line such as `{ "jwt": ... }`. (If you do, `RC_JWT` ends up as just `{` and the tool will tell you so.)
   * Save as plain text (UTF-8).
4. Keep `.env` private: it is already listed in `.gitignore`. Never commit, email or paste it.

```ini
RC_SERVER_URL=https://platform.ringcentral.com
RC_CLIENT_ID=your-client-id
RC_CLIENT_SECRET=your-client-secret
RC_JWT=eyJ...the-whole-long-token...
```

**Also record it somewhere safe** (a password manager entry, not a text file next to the code). Useful things to store together: the app name and link, Client ID, Client Secret, the JWT, **which user the JWT belongs to**, the date it was created, and any expiry date. If the JWT expires or is deleted, the collector stops until a new one is created, and this record tells whoever fixes it what to replace.

### Part F: verify it

```bash
python rc_audit_trail.py --check
```

A healthy setup looks like this:

```text
Checking your setup...

  [ OK ] Python 3.12.10
  [ OK ] viewer_template.html present
  [ OK ] Output folder is writable (C:\path\to\output)
  [ OK ] .env file found
  [ OK ] Settings are filled in and look well-formed
  [ OK ] Can find platform.ringcentral.com on the network
  [ OK ] Logged in to RingCentral with the Client ID, Client Secret and JWT
  [ OK ] Audit Trail access works (permission and role are fine)
  [ OK ] Storage: JSON file (nothing to configure)
  [INFO] No scheduled task yet (optional). Create one from the viewer's Schedule settings, or run --apply-schedule

All checks passed.
```

What each line proves: *settings well-formed* means the values are filled in and the JWT is a properly shaped, unexpired token; *Logged in* means the Client ID, Secret and JWT agree with each other; *Audit Trail access works* means both the app permission **and** the user's role are right. If any line says `FAIL`, the arrow lines beneath it say what to do. See [Troubleshooting](#troubleshooting).

### Part G: maintenance

* **Rotate a credential:** create a new JWT (Part D), put it in `.env`, run `--check`, then delete the old JWT in the Developer Console > Credentials. To rotate the Client Secret, regenerate it on the app's page and update `RC_CLIENT_SECRET`.
* **If a secret may have leaked:** delete the JWT credential and regenerate the Client Secret immediately, then update `.env`.
* **If the service user is disabled or loses its admin role:** collection fails with a "rejected the JWT" or "not allowed" message; restore the user or create a JWT for another suitable admin.

### Common setup mistakes

| Mistake | What you will see | Fix |
|---|---|---|
| Pasted a whole JSON block into `RC_JWT` | `RC_JWT contains braces...` or `OAU-331 Unparseable assertion` | Paste only the `eyJ...` token on one line |
| Client ID/Secret from one app, JWT created for another | `invalid_client` (OAU-153) or a JWT rejection | Use the Client ID/Secret of the same app the JWT was created for |
| App created with 3-legged OAuth instead of JWT | login errors such as `unauthorized_client` | Create a new app with **JWT auth flow** |
| Forgot **Read Audit Trail** on the app | `403` "not allowed to read the Audit Trail" | Part B |
| Used an ordinary user (no Audit Trail role) | same `403` message | Create the JWT for an admin user with the right role |
| Production vs sandbox mismatch | a warning about the server URL, or `OAU-336`/JWT rejection | Make app, JWT and `RC_SERVER_URL` the same environment |
| `.env` saved as `.env.txt` | "There is no .env file" | Show file extensions and rename |
| JWT expired or deleted | "The JWT expired on ..." or a JWT rejection | Part D, then update `.env` |

## Repository layout

```
.
├── rc_audit_trail.py            The whole collector: errors/logging, auth, fetch, shape, store, schedule, serve, diagnostics (commented by section)
├── viewer_template.html         The viewer's HTML/CSS/JS. The script fills in the data to produce each run's page
├── open_viewer.bat              Double-click launcher for the viewer with working schedule settings
├── .env.example                 Template for credentials and optional settings (copy to .env)
├── requirements-optional.txt    Drivers needed only for --save postgres / mssql
├── .gitignore / .gitattributes  Keep secrets and data out of git; normalise line endings
├── docs/
│   ├── sample_events_FICTIONAL.json   Made-up events in the real API's shape, for trying the tool with no credentials
│   └── screenshots/             Images used in this README (sample data only)
├── tests/
│   ├── test_core.py             Flattening, de-duplication, schedule validation, stores
│   └── test_errors.py           Every error path, against a local stub that imitates RingCentral's failures
└── tools/make_sample_screenshots.py   Regenerates the sample data and screenshots (documentation tooling only)
```

Generated at run time and **never committed** (see `.gitignore`): `.env`, `schedule.json`, `output/` (CSV, JSON, SQLite, viewer pages, `run_log.txt`, `last_run.json`).

## Features in depth

### 1. Collection and paging

**What:** asks the Audit Trail API for every event between *now minus N days* and *now*, following pages of 250 records.

**How:** `fetch()` sends `POST /restapi/v1.0/account/~/audit-trail/search` with `eventTimeFrom`, `eventTimeTo`, `page`, `perPage`, `includeAdmins: true`, `includeHidden: true` and an empty `searchParameters` list. It stops on a short page, on the last page when the API reports a page count, or at the 10,000-record cap (and warns if the cap is hit). The page size can be lowered with `RC_PER_PAGE`.

**Why:** the API caps each search, so pulling in pages and warning on the cap prevents silently missing data. `searchParameters` is deliberately empty: capturing *everything* and filtering locally means a question you did not think of in advance can still be answered later. `includeAdmins` and `includeHidden` are set so administrator activity and hidden-category events are not excluded.

### 2. Authentication (JWT)

**What:** logs in without a browser or a person present.

**How:** `get_token()` sends the app's client ID/secret (HTTP Basic) and the JWT as the `assertion` of a `urn:ietf:params:oauth:grant-type:jwt-bearer` grant to `/restapi/oauth/token`, and uses the returned bearer token for the search calls. Before any request is sent, the settings and the JWT's shape and expiry date are checked locally, so a bad paste is caught instantly with a precise message.

**Why:** JWT is RingCentral's supported flow for unattended server-to-server scripts. Nothing is stored except what you put in `.env`.

### 3. Rate limits and transient failures

**What:** survives the API's rate limiting and short outages.

**How:** `http()` retries HTTP 429 (waiting for the server's `Retry-After`), HTTP 5xx and dropped connections up to five times, with growing pauses, and says on screen what it is doing. Anything else stops the run with an explanation (see [feature 12](#12-error-handling-and-diagnostics)).

**Why:** the Audit Trail API is on RingCentral's heavy usage plan, so occasional 429s are expected on large pulls, and a monthly job should not fail because of a blip.

### 4. Flattening: no hard-coded schema

**What:** every field the API returns becomes a column.

**How:** `flatten()` turns nested objects into dotted names (`initiator.name`) and lists into text. The CSV and viewer take the union of all keys across rows.

**Why:** the response is not fully documented and the API is beta. Hard-coding columns would silently drop new fields or break on change. With flattening, whatever the API sends is kept and visible, and the viewer's filters appear automatically for new columns.

### 5. De-duplication and overlapping windows

**What:** running twice, or with overlapping windows, never produces duplicate events.

**How:** `record_id()` uses the event's own `id`. Every store is keyed on it, so re-saving an event updates it rather than adding another. (If a record has no ID, a hash of its content is used.)

**Why:** this is what makes a deliberate overlap safe. The default retrieval window (35 days) is longer than the default interval (30 days), so a run that starts late, or a missed run, still covers everything since the previous one.

### 6. Storage: JSON, SQLite, PostgreSQL, SQL Server, any mix, or none

Choose with `--save` (or `RC_SAVE` in `.env`): `json` (default), `db` (SQLite), `postgres`, `mssql`, `both` (= json + db), `none`, or a comma list such as `json,postgres`.

| Store | What you get | Needs |
|---|---|---|
| `json` | `output/audit_master.json` (cumulative, keyed by event ID) plus `output/raw_<date>.json` (the untouched API response for that run) | nothing |
| `db` | `output/audit_trail.db`, a SQLite file (move it with `RC_DB_PATH` / `--db-path`) | nothing: SQLite is built into Python |
| `postgres` | the same table in PostgreSQL | `pip install "psycopg[binary]"` and `RC_PG_DSN` |
| `mssql` | the same table in SQL Server | `pip install pyodbc`, the Microsoft ODBC Driver, and `RC_MSSQL_CONN` |
| `none` | CSV and viewer only | nothing |

**Each store is independent.** Choosing one never requires another. If one fails (unwritable path, missing driver, missing setting), the script prints a warning with advice and still completes the others. A typo in `--save` is reported and ignored, never fatal. If **every** store you chose fails, the CSV and viewer are still written but the run exits with code 2 so a scheduler can flag it.

**Safe writes.** `audit_master.json` is written to a temporary file and swapped in, so a crash cannot leave it half-written. If it is ever found damaged, it is **renamed aside (never deleted)**, a fresh one is started, and a warning says how to rebuild history from the `raw_<date>.json` files.

**Why:** an admin may want a quick file, a proper database, both, or neither, and a monthly job should not fail because an optional database was unreachable.

**The table** (`audit_events`), identical on every engine:

| Column | From | Notes |
|---|---|---|
| `id` | `id` | primary key; also the de-duplication key |
| `event_time` | `eventTime` | UTC, ISO 8601 text |
| `action_id` | `actionId` | the reliable "what happened" signal, e.g. `DELETE_USER:USER` |
| `event_type` | `eventType` | see the [caveat](#data-reference); do not rely on it |
| `initiator_name`, `initiator_ext`, `initiator_role` | `initiator.*` | who did it |
| `target_name`, `target_ext`, `target_type` | `target.*` | what it was done to |
| `client_ip` | `clientIp` | where it came from |
| `comment` | `comment` | free text when present |
| `record_json` | the whole flattened record | nothing is lost if the API adds fields |
| `first_seen` | set on insert only | when *you* first collected the event |

Key fields are real, indexed columns so you can query without parsing JSON. Example (extensions deleted, newest first), using Python so no extra tools are needed:

```bash
python -c "import sqlite3;[print(r) for r in sqlite3.connect('output/audit_trail.db').execute(\"SELECT event_time, initiator_name, target_name, target_ext FROM audit_events WHERE action_id LIKE 'DELETE_USER%' ORDER BY event_time DESC\")]"
```

**Which history does the viewer show?** The full history from the last store that saved successfully. With `none`, it shows just that run.

### 7. Outputs and dated filenames

| File | Written when | Name |
|---|---|---|
| CSV of this run | always | `audit_trail_YYYYMMDD.csv` |
| Viewer page | always | `audit_viewer_YYYYMMDD.html` |
| Raw API response | `json` saving | `raw_YYYYMMDD.json` |
| Cumulative JSON | `json` saving | `audit_master.json` |
| Cumulative SQLite | `db` saving | `audit_trail.db` |
| Run log | every run | `run_log.txt` (appended; trimmed past 1 MB; secrets redacted) |
| Last result | every collection | `last_run.json` (time, ok/failed, message; shown on the viewer) |

**Does each automatic run save to the same file or a new one?** Both, depending on the kind of file:

| Kind of file | Files | What happens on each run |
|---|---|---|
| **Per-run snapshot** | `audit_trail_YYYYMMDD.csv`, `audit_viewer_YYYYMMDD.html`, `raw_YYYYMMDD.json` | A **new file** is created, named with that day's date. A run every 30 days therefore leaves `..._20261107`, then `..._20261207`, and so on, side by side. |
| **Running history** | `audit_master.json`, `audit_trail.db` (or your Postgres / SQL Server table) | The **same file or table** is used every time. New events are added, and events already stored are matched on their event ID and **updated in place, never duplicated**. |
| **Status files** | `run_log.txt`, `last_run.json` | `run_log.txt` is **appended to** (every run adds lines). `last_run.json` is **replaced** each collection with the latest result. |

**When a file is overwritten.** Only the per-run snapshots can be overwritten, and only when two collections happen on the **same calendar day**: the second run replaces that day's CSV, viewer and raw file with a fresh copy. No data is lost by this, because the running history already holds every event, and the replacement contains the same window or a newer one. If one of those files is open in a program that locks it (Excel does this with the CSV), the run cannot replace it: it does not damage the file, and it stops with a message asking you to close the file and run again. The running-history files are never replaced wholesale; they are updated, and `audit_master.json` is written safely (to a temporary file, then swapped in).

**Which file holds the complete picture?** The **newest viewer page** (`audit_viewer_<latest date>.html`) is built from the running history, so it shows **everything collected so far**, not only the last run's window. Older dated viewers are snapshots of what was known on their day, and the helper (`open_viewer.bat`) always opens the newest. The dated **CSV**, by contrast, contains only that run's window.

**If you choose `--save none`,** there is no running history. Each run's CSV and viewer then contain only that run's events, so keep the dated files if you want a record over time.

**Files accumulate.** The script never deletes anything, so you gain roughly one set of dated files per run, and the viewer files grow as history grows. Archive or delete old dated files when you no longer need them (the running history and the newest viewer are enough to see everything). `run_log.txt` trims itself once it passes 1 MB.

Other details: the CSV uses UTF-8 with a BOM so Excel opens it correctly, and the output folder can be changed with `--out`.

### 8. The viewer and its filters

**What:** one self-contained HTML file with the data embedded. No server, internet or install is needed to read it, so it can be emailed, dropped on a share, or archived.

**The filter panel** (everything combines; each control narrows the table further; nothing is hidden until you choose a filter):

| Control | What it does | Why |
|---|---|---|
| **Search all fields** | Case-insensitive match across every column | fastest way to find a name, number or IP |
| **From / To date** | Limits by event date (UTC) | "what happened last week?" |
| **Done by** | Dropdown of everyone who performed an action, with counts | "what did this admin do?" |
| **Exact action** | Dropdown of every distinct `actionId`, with counts | precise filtering without knowing the vocabulary in advance |
| **Quick filters** | One-click chips: *Anything deleted*, *Extension deleted*, *Added / assigned*, *Changed*, *Released*, *Logins*. Each shows how many events it matches, hovering explains it, and picking several shows events matching **any** of them | the common questions, answered in one click, and self-explanatory *before* you click |
| **Column filters** | A "contains..." box under every column heading | "every available field" is filterable; a column in use turns blue |
| **Active-filter pills** | A removable pill for every filter in force, a "Showing X of Y events" line, and **Clear all filters** | you can always see why rows are hidden and undo one filter at a time |
| **Export these rows as CSV** | Downloads exactly what is currently shown | hand a filtered list to someone else |

Headings are click-to-sort (newest first by default).

**Why the chips look at `actionId`, not `eventType`:** in real data, `eventType` was `Create` even for delete actions, so matching on it would mislabel events. `actionId` (for example `DELETE_USER:USER`) is the dependable signal. Quick filters are plain patterns at the top of the viewer script (the `CHIPS` list), so adding your own takes one line.

**Light/dark:** defaults to light; a button switches and the choice is remembered where the browser allows it, falling back to light if storage is blocked.

**Safety:** event data can contain text typed by users, so every value is HTML-escaped before display, and `</` is escaped when the data is embedded in the page so data can never close the script block.

### 9. Schedule settings and the local helper

**What:** a panel on the page to turn automatic collection on or off, set *run every N days*, *at what time*, and *each run retrieves the last N days*, plus **Run now** and a live status line (task state, next run, last run, and whether the last collection succeeded).

**Why a helper is needed:** a web page cannot change Windows Task Scheduler by itself, for good security reasons. `--serve` (launched by `open_viewer.bat`) starts a small server that listens on **127.0.0.1 only** and does the privileged part.

**How it behaves:**

* Settings are validated (interval 1-365 days, window 1-180 days, a real `HH:MM` time, and **window at least as long as the interval**) before anything is applied. Windows must accept the change before `schedule.json` is written.
* *Off* **disables** the task rather than deleting it. *On* re-creates it with the current settings.
* If you open the HTML file directly (no helper), the panel is **read-only** and says so.
* If the helper cannot do something (for example Windows denies permission), the panel shows the explanation and what to do.

**Security of the helper:**

| Risk | Protection |
|---|---|
| Other computers reaching it | binds to loopback (127.0.0.1) only |
| A website you visit trying to drive it | every API call needs a random per-session token that exists only in the page the helper served |
| DNS-rebinding tricks | the `Host` header must be `127.0.0.1:<port>` or `localhost:<port>` |
| Injection into PowerShell | the time is regex-validated, numbers are integers, and every string passed to PowerShell is single-quote escaped |

It runs only while its console window is open.

### 10. How the scheduled task behaves

Created by `apply_schedule()` with these settings, chosen so a once-a-month job is dependable:

| Setting | Effect |
|---|---|
| Daily trigger, every *N* days at *HH:MM* | the cadence you chose |
| Run as soon as possible after a missed start | if the PC was off or asleep at the scheduled time, it runs at the next opportunity; the following run stays on the original cadence |
| Allow start on battery / do not stop on battery | laptops still run it |
| Restart on failure: 3 times, 1 hour apart | rides out transient network or API problems |
| Runs with no arguments | the retrieval window is read from `schedule.json` at run time, so the panel can change it without editing the task |

Limits: it runs only while the user is **signed in**. Because nobody watches a scheduled run, every outcome is written to `run_log.txt` and `last_run.json`, and the viewer's settings panel shows the last result. An expired or revoked JWT makes runs fail with an authentication error and retries will not help. For unattended server use, run it under a service account on a server.

To remove it: `Unregister-ScheduledTask -TaskName "RC Audit Trail Collector" -Confirm:$false`.

#### Scheduling on other platforms

The settings panel and `--apply-schedule` are Windows-only. Elsewhere, schedule `python rc_audit_trail.py --days 35` yourself. Cron cannot express "every 30 days" exactly (`*/30` in the day-of-month field restarts each month), so use monthly (`0 7 1 * *`) or a systemd timer with `OnUnitActiveSec=30d`. Check the exit code (0 = fine) and `output/last_run.json`.

### 11. Why the window is longer than the interval

If each run only looked back exactly as far as the interval, a run that starts a little late would leave a gap. The default 35-day window against a 30-day interval gives five days of overlap, and de-duplication (feature 5) makes the overlap harmless. The settings panel and the validator both refuse a window shorter than the interval.

### 12. Error handling and diagnostics

**The principle:** every problem we can foresee ends in a plain-language explanation and numbered steps, never a raw Python error. Anything we did not foresee is logged in full for support.

**What a problem looks like** (real output from using the wrong Client Secret):

```text
ERROR: RingCentral did not accept the app's Client ID and Client Secret.
  Details: HTTP 401 | invalid_client | OAU-153 | Invalid client application: [***]
  What to do:
    1. Copy RC_CLIENT_ID and RC_CLIENT_SECRET again from the Developer Console (Apps > your app > Credentials).
    2. Make sure they belong to the SAME app the JWT is meant for, and that the app's authentication is 'JWT auth flow'.
    3. If the secret was regenerated, update it in .env.
  More help: run  python rc_audit_trail.py --check  and see README.md > Troubleshooting.
```

Each block has the same three parts: **what happened** in one sentence, **Details** (RingCentral's own error text, shown verbatim because it is the most precise clue), and **What to do**.

**How it works:**

| Mechanism | What it does | Why |
|---|---|---|
| **Settings validated up front** (`check_settings`, `inspect_jwt`) | Checks every `.env` value and decodes the JWT *locally*: right shape, not pasted JSON, not expired, issuer matches the server URL, https, sandbox warning | catches the most common mistakes instantly, before any network call and with a precise message |
| **`--check` command** | Tests Python, files, output folder, settings, DNS, login, Audit Trail permission, storage choices, scheduled task and the last run, each as `OK` / `WARN` / `FAIL` with fixes | a newcomer can see in 10 seconds which single thing is wrong |
| **Error translation** (`api_error`, `network_error`, `io_error`, `store_hint`) | Maps HTTP errors, connection failures, file-system errors and database errors to causes and fixes | the same mistake always produces the same helpful message |
| **Retries** | 429, 5xx and dropped connections are retried with pauses and narrated | transient problems do not fail a monthly job |
| **Shape checks** | Detects a response without a `records` list, non-JSON replies (a proxy page), non-object records, bad `--from-json` files | beta API changes or interference are reported, not silently turned into "0 events" |
| **Never lose history** | Atomic writes; a damaged `audit_master.json` is renamed aside; each store is independent | a crash or bad file cannot destroy or block the history |
| **`run_log.txt`** | Timestamped log of every run (trimmed past 1 MB) | scheduled runs have no console; this is the record |
| **`last_run.json` + the viewer** | The latest result, with the first fix step, is shown in the schedule panel | a failed scheduled run is visible without opening logs |
| **Secret redaction** | Client ID, secret, JWT and database passwords are replaced by `***` in anything printed or logged, including text RingCentral sends back | logs and screenshots are safe to share |
| **`--verbose`** | Also prints each API call and its HTTP status (never headers or secrets) | see exactly where a run stops |
| **Exit codes** | `0` success, `1` failed, `2` collected but no chosen store could be saved, `130` cancelled | Task Scheduler and scripts can react correctly |
| **Catch-all** | An unforeseen error prints a short message, writes the full traceback to `run_log.txt`, and exits `1` | there is always something to send to whoever helps |

## Configuration reference

### `.env` (copy from `.env.example`)

| Variable | Required | Meaning |
|---|---|---|
| `RC_SERVER_URL` | yes | `https://platform.ringcentral.com` (production) or `https://platform.devtest.ringcentral.com` (sandbox apps only) |
| `RC_CLIENT_ID` | yes | the app's Client ID |
| `RC_CLIENT_SECRET` | yes | the app's Client Secret |
| `RC_JWT` | yes | JWT credential string for the admin user |
| `RC_PER_PAGE` | no | records per API page (default 250); lower it, e.g. 100, if requests are rejected |
| `RC_SAVE` | no | default for `--save` (default `json`) |
| `RC_DB_PATH` | no | SQLite file location |
| `RC_PG_DSN` | only for `postgres` | e.g. `postgresql://user:password@host:5432/dbname` |
| `RC_MSSQL_CONN` | only for `mssql` | ODBC connection string |

Real environment variables take precedence over `.env`. Behind a corporate proxy, also set `HTTPS_PROXY`, and for a company certificate bundle `SSL_CERT_FILE`.

### `schedule.json` (created for you; safe to edit by hand)

```json
{ "enabled": true, "every_days": 30, "time": "07:00", "window_days": 35 }
```

If the file is missing, these defaults apply. If it is damaged, the defaults apply **and a warning tells you**.

### Command-line options

| Option | Meaning |
|---|---|
| *(none)* | collect using the window in `schedule.json` |
| `--check` | test the whole setup step by step and explain any problem |
| `--verbose` | also print each API call (secrets are never shown) |
| `--days N` | one-off window of N days (1-180) |
| `--save ...` | `json`, `db`, `postgres`, `mssql`, `both`, `none` (comma list ok) |
| `--db-path FILE` | SQLite file (default `output/audit_trail.db`) |
| `--out DIR` | output folder (default `./output`) |
| `--from-json FILE` | rebuild outputs from a saved API response instead of calling the API |
| `--serve` | open the viewer with working schedule settings |
| `--no-browser` | with `--serve`: do not open a browser window |
| `--apply-schedule` | create/update the Windows task from `schedule.json` |

## Data reference

Each event is flattened to columns. The fields observed from the API, and what they mean:

| Column | Meaning |
|---|---|
| `id` | unique event ID (the de-duplication key) |
| `eventTime` | when it happened, UTC |
| `actionId` | what happened, as a code, e.g. `DELETE_USER:USER` |
| `eventType` | a coarse category. **Unreliable**: reported `Create` for deletions, inconsistent capitalisation, and absent on failed logins |
| `accountId` | your account |
| `clientIp` | IP address the action came from |
| `initiator.name`, `.extensionNumber`, `.extensionId`, `.role` | who did it, and their role (e.g. Super Admin) |
| `target.name`, `.extensionNumber`, `.objectId`, `.objectType` | what it was done to (e.g. an Extension) |
| `details.parameters` | extra key/value pairs for some actions, kept as JSON text |
| `comment` | free text on some events (e.g. "Deleted custom rule ...") |

Not every field appears on every event; empty cells are normal.

**`actionId` examples seen in practice** (not exhaustive; your account may show others, and the *Exact action* dropdown lists whatever it contains):

| Pattern | Meaning |
|---|---|
| `DELETE_USER:USER` | an **extension was deleted** |
| `DELETE_CUSTOM_RULE:*` | a custom rule was deleted |
| `ADD_EXTENSION:*`, `ASSIGN_USER`, `ADD_USER_TO_CQ`, `ADD_QUEUE_MANAGER` | extensions, users or queue members added or assigned |
| `ASSIGN_NUMBER`, `ASSIGN_NUMBER:*`, `RELEASE_NUMBER*`, `CHANGE_PHONE_NUMBER_TYPE` | phone-number changes |
| `CHANGE_REGIONAL_SETTINGS:*`, `ASSIGN_SMS_RECIPIENT:*` | settings changes |
| `DOWNLOAD_USER_LIST` | the user list was exported |
| `LOGIN_SUCCESSFULLY`, `FAIL_LOGIN` | sign-in activity |

The action vocabulary is RingCentral's and can change; if a quick filter misses something in your account, adjust the `CHIPS` patterns (see [Extending it](#extending-it)).

## Security and privacy

* **Secrets.** `.env` holds the client secret and JWT. It is git-ignored; never commit, email or paste it. Revoke and regenerate the JWT if it is ever exposed. Use the least-privileged admin user that can read the Audit Trail, and restrict the JWT to this app (Part D).
* **Audit data is sensitive.** `output/` contains names, extension numbers and IP addresses. It is git-ignored, so keep it out of public places and share viewer files only with people entitled to see them.
* **Logs are redacted.** Secrets are masked in everything printed or written to `run_log.txt`, but the log can still contain account details such as server messages. Review it before sharing.
* **Nothing leaves your machine** except the calls to RingCentral. The viewer needs no internet and loads no external scripts or fonts.
* **The local helper** is loopback-only and token-protected (see [feature 9](#9-schedule-settings-and-the-local-helper)) and runs only while you keep it open.
* **Before publishing a fork**, confirm no real data crept in: check `git status` shows no `.env`, `schedule.json` or `output/`, and that screenshots and samples are fictional.

## Limits and known caveats

* The API returns at most **180 days and 10,000 records** per search. The script warns if the cap is reached; use a shorter window or run more often.
* The Audit Trail API is **beta**: field names and behaviour can change. The flattening design is the main defence, and an unexpected response shape is reported clearly.
* **`eventType` is not reliable**; use `actionId`.
* Scheduling and the settings panel are **Windows-only**; the task runs only while the user is signed in.
* The PostgreSQL and SQL Server writers share one tested code path with SQLite but were **not exercised against live servers**. Test them in your environment first.
* The error advice for some RingCentral responses is based on the codes seen in testing (for example OAU-331, OAU-336, OAU-153) and on RingCentral's documentation. For any response the tool does not recognise, it shows RingCentral's own message verbatim.
* The viewer embeds all history in one file. Tens of thousands of events are fine; millions are not what it is for (use the database and your own tooling).
* Dates in filters are UTC dates, matching `eventTime`.

## Troubleshooting

### Start here

1. **Run the setup check:**

   ```bash
   python rc_audit_trail.py --check
   ```

   It tests each part separately. Items marked `FAIL` have `->` lines saying what to do. Example of a failure (an expired JWT):

   ```text
     [ OK ] Python 3.12.10
     [ OK ] .env file found
     [FAIL] The JWT expired on 2026-02-10. Create a new JWT credential and replace RC_JWT in .env.
     [ OK ] Storage: JSON file (nothing to configure)

   1 problem(s) found. Fix the items marked FAIL (the -> lines say how), then run --check again.
   ```

2. **Read the error block.** It says what happened, quotes RingCentral's own message under *Details*, and lists what to do.
3. **Look at the record of past runs.** `output/run_log.txt` has every run with times, and `output/last_run.json` has the latest result. The viewer's *Schedule settings* panel shows it too (see the screenshot above), which is how you notice a failed *scheduled* run.
4. **Need more detail?** Re-run with `--verbose` to see each step and HTTP status.

### Exit codes (for Task Scheduler and scripts)

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | the run failed (the message says why) |
| `2` | events were collected and the CSV/viewer written, but **none** of the stores you chose could be saved |
| `130` | cancelled with Ctrl+C |

### Message-by-message guide

**Setup and login**

| What you see | Cause | What to do |
|---|---|---|
| `This tool needs Python 3.9 or newer` | old Python | install a current Python (tick *Add to PATH*) |
| `There is no .env file at ...` | the file is missing or misnamed (`.env.txt`) | copy `.env.example` to `.env` in that folder; show file extensions in Explorer |
| `RC_... is empty` | a required line has no value | fill it in; README > Setting up RingCentral says where to find each |
| `RC_JWT contains braces, quotes, spaces or line breaks` | a JSON block or extra text was pasted | paste only the `eyJ...` token, one line |
| `RC_JWT does not look like a JWT` / `could not be decoded` | not a JWT, or cut off when copied | copy the whole token again |
| `The JWT expired on <date>` | the JWT's lifetime ended | create a new JWT; update `RC_JWT` |
| `RC_SERVER_URL must start with https://` | typo or wrong value | use `https://platform.ringcentral.com` |
| Note: `... points at the SANDBOX` | `RC_SERVER_URL` is the devtest server | sandbox has test data only; use production for real data |
| Note: `... issued by X but RC_SERVER_URL is Y` | the JWT is for the other environment | match the server URL to the JWT's environment |
| `RingCentral could not read the JWT` (**OAU-331**, Unparseable assertion) | `RC_JWT` is malformed | one-line `eyJ...` token only |
| `RingCentral says this JWT was not issued for this server` (**OAU-336**, Audience claim) | the token is not a genuine JWT for this server/environment | copy the real token again; match production/sandbox |
| `RingCentral rejected the JWT` (`invalid_grant`) | expired, deleted or revoked JWT; JWT not allowed for this app; user disabled; wrong environment | create a new JWT, allow this app on it, use an active admin user |
| `did not accept the app's Client ID and Client Secret` (**OAU-153**, `invalid_client`) | wrong or mismatched ID/secret, secret regenerated, or the app is not a JWT app | copy both again from the app; use the same app as the JWT; app auth must be *JWT auth flow* |
| `Login succeeded but RingCentral returned no access token` | unusual reply | run again; recreate the JWT if it repeats |

**Reading the Audit Trail**

| What you see | Cause | What to do |
|---|---|---|
| `... not allowed to read the Audit Trail` (HTTP 403) | the app lacks **Read Audit Trail**, **or** the JWT's user has no Audit Trail permission | add the scope in the app's Settings (Part B); use an admin user whose role can view the Audit Trail; both are needed |
| `... rejected the access token` (HTTP 401) | token problem | run again; if it persists, run `--check` |
| `The Audit Trail address was not found` (HTTP 404) | wrong server URL, or API not available for the account type | check `RC_SERVER_URL`; ask RingCentral support |
| `... rejected the Audit Trail request` (HTTP 400) | page size or window not accepted | add `RC_PER_PAGE=100`; try `--days 7` |
| `RingCentral reported a server problem` (HTTP 5xx) | RingCentral side | retried automatically; if it persists wait and rerun |
| `rate limited by RingCentral, waiting ...` | HTTP 429 | automatic: the tool waits and retries |
| `... not in the expected format (no 'records' list)` | the beta API changed | `--verbose`; compare with RingCentral's docs; adjust `search_page()` |
| `... is not JSON` | a proxy, VPN or firewall page is replacing the answer | try another network; check `RC_SERVER_URL` |
| `reached the API cap of 10000 records` | window too large for your volume | shorten the window or run more often |
| `RingCentral returned no audit events` (note, not an error) | quiet account, or the user cannot see events | normal if nothing changed; otherwise check the user's role and the window |

**Network**

| What you see | Cause | What to do |
|---|---|---|
| `The address ... could not be found (DNS)` | offline, VPN/DNS problem or a typo | check connection and `RC_SERVER_URL` |
| `Timed out waiting for ...` | slow link or blocked outbound HTTPS | check firewall; set `HTTPS_PROXY` if you use a proxy |
| `Could not connect to ...` | firewall, proxy or no internet | allow outbound HTTPS (port 443); set `HTTPS_PROXY` |
| `A secure connection ... (certificate problem)` | a company proxy or antivirus inspects HTTPS | ask IT for the company root certificate bundle; set `SSL_CERT_FILE` to it; check the PC clock |

**Files, storage and output**

| What you see | Cause | What to do |
|---|---|---|
| `The output folder cannot be used` | folder missing or read-only | choose another with `--out` |
| `would not let the program write the CSV file` (or viewer/raw) | the file is open in Excel or a browser, or no permission | close it and rerun; check folder permissions; check OneDrive is not locking it |
| `The disk is full` | no space | free some space |
| `audit_master.json was damaged` | crash or disk problem | automatic: kept as `audit_master.corrupt-<time>.json`; rebuild with `--from-json raw_<date>.json` |
| `Warning: could not save to db: ... locked` | another program has the SQLite file open | close the database viewer |
| `Warning: could not save to postgres/mssql: ...` | driver, setting or connection problem | the message says which; install the driver, set `RC_PG_DSN` / `RC_MSSQL_CONN`, check host/port/credentials/firewall |
| `Note: ignoring unknown --save value(s)` | a typo | valid: `json, db, postgres, mssql, both, none` |
| `The file given to --from-json was not found` / `is not valid JSON` / `does not contain a list of audit events` | wrong or edited file | use an unedited `raw_<date>.json` |
| `schedule.json could not be read` / `has an invalid setting` | hand-edited file damaged | fix it or delete it to use the defaults |
| `viewer_template.html is missing` | file removed | restore it next to `rc_audit_trail.py` |

**Scheduling and the helper**

| What you see | Cause | What to do |
|---|---|---|
| Panel says "read-only" | you opened the HTML file directly | start with `open_viewer.bat` |
| `retrieval window must be at least as long as the run interval` | window shorter than interval | raise *retrieves the last (days)* |
| `Windows denied permission to change the scheduled task` | policy or rights | run PowerShell as administrator; ask IT; or create the task by hand |
| `PowerShell was not found` / `Task Scheduler did not answer` | system problem | check PowerShell is on PATH; restart the Task Scheduler service |
| `No free local port between 8765 and 8784` | other copies of the helper are running | close them and retry |
| Task did not run | PC off or you were signed out, or `.env` moved | check the panel's *Last collection*, `run_log.txt` and that the task is *Ready*; run `python rc_audit_trail.py` by hand |
| `Something unexpected went wrong (...)` | an error nobody anticipated | the full detail is in `run_log.txt`; send it with your Python and Windows versions |

### Asking for help

Send: the **`--check` output**, the relevant part of **`output/run_log.txt`**, your **Python version** (`python --version`) and **Windows version**. Secrets are redacted in these, but look them over first.

**Never send:** your `.env`, the Client Secret, the JWT, or the `output/` data files.

## Extending it

* **Your own quick filters.** In `viewer_template.html`, add a line to the `CHIPS` list: `['Label', /pattern/, 'hover text']`. The count and highlighting come for free.
* **Alerting on deletions.** After a run, query the store for `action_id LIKE 'DELETE_USER%'` where `first_seen` is recent, and send it to email, chat or a ticket.
* **Another database.** Implement a function like `save_sql()`: create the table with `table_ddl()`, then call `upsert_rows()` with the driver's placeholder style. `DB_FIELDS` defines the columns.
* **A new kind of failure.** Add a branch to `api_error()`, `network_error()` or `store_hint()` returning a `CollectorError(what, fix, detail)`, plus a test in `tests/test_errors.py`.
* **SIEM or log pipeline.** The per-run CSV, or `record_json`, is ready to forward.
* **Server hosting.** Run `python rc_audit_trail.py` from cron/systemd or a CI scheduler with credentials supplied as environment variables instead of `.env`.

## Development and testing

```bash
python -m unittest discover -s tests -v      # 45 tests, no network or credentials needed
python tools/make_sample_screenshots.py      # regenerate sample data + README screenshots (needs Edge or Chrome)
```

* `tests/test_core.py` covers flattening, de-duplication, schedule validation (including rejection of anything that could reach PowerShell), and the JSON and SQLite stores.
* `tests/test_errors.py` starts a small local stub that imitates RingCentral, including its failures (bad JWT, wrong client, 401/403/404/400/5xx, 429, junk replies, a missing `records` list, a dropped connection), and checks that each produces the right explanation. It also runs the real script as a subprocess to check exit codes, `last_run.json` and `--check`.
* `tools/make_sample_screenshots.py` is documentation tooling: it builds fictional data and drives a private headless browser to capture the screenshots in this README, and the collector never uses it.

Code map for reviewers: `rc_audit_trail.py` is organised in numbered, commented sections: (1) errors, logging and safe file writes, (2) configuration, settings validation and authentication, (3) fetching, (4) shaping records, (5) storage, (6) schedule settings, (7) viewer and local helper, (8) diagnostics (`--check`), (9) command-line entry point. `viewer_template.html` is commented by section as well.
