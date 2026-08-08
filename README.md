# rumblelog

Unattended capture of **who said what**, **which donations (rants) came in**, and **who
subscribed** during your Rumble streams — plus **leaderboards** ranking your viewers by
chat volume, money donated, subscriptions gifted and how long they've been subscribed,
filterable by day, week, month or year.

Polls the Live Stream API from your Rumble account settings and folds every snapshot into
SQLite, so a finished stream leaves behind a queryable record instead of a chat box that
scrolled away. Runs as a background scheduled task: once installed there are no manual
steps.

Python 3.8+, standard library only. Nothing to `pip install` to run it.

## Setup from source

```bash
python rumblelog.py configure
```

Opens a dialog (or prompts in the terminal with `--console`) for the Live Stream API URL
from Rumble → Account Settings → API, saves it, and immediately tests it so you know
straight away whether it works.

```bash
python rumblelog.py verify
```

Fetches once and prints the payload's real structure — top-level keys, livestreams seen,
and the first chat message object verbatim.

> **Run `verify` once while you are live.** Rumble's field names are not contractually
> documented, so the parser accepts several plausible spellings for each value. `verify`
> is how you confirm it guessed right for chat, rants and gifted subs — and those fields
> only exist in the payload while a stream is running. Nothing is lost if a name is wrong
> (see [If a field is mapped wrong](#if-a-field-is-mapped-wrong)), but it is better to know.

```bash
powershell -ExecutionPolicy Bypass -File install-service.ps1
```

Registers a scheduled task called `RumbleLog`: an **AtLogon** trigger plus a **5-minute
watchdog** trigger with `MultipleInstances=IgnoreNew`, so the watchdog does nothing while
the poller is alive and restarts it if it died. Runs as your own user via `pythonw.exe` —
no console window, no admin rights. Remove it with `uninstall-service.ps1`.

## What it does while running

- Polls every 30s while idle, every 10s once a stream is live.
- Opens a row in `streams` when a new livestream id appears, keyed on Rumble's own id, so
  each broadcast separates itself with no labelling from you.
- Records chat, rants, followers and subscribers as it sees them. Inserts are idempotent,
  so overlapping poll windows, crashes and restarts never duplicate or double-count.
- Archives every raw response to `data/raw/YYYY-MM-DD.jsonl` while live.
- Closes the stream row when the livestream disappears from the payload.

```bash
python rumblelog.py status
```

## Leaderboards

```bash
python rumblelog.py leaderboard
```

Four rankings, all at once by default:

| board | ranks by |
|---|---|
| `chat` | number of chat messages sent |
| `donations` | total rant value, with the number of rants |
| `gifts` | subscriptions **given** to other people |
| `tenure` | earliest subscription date — longest-standing subscribers first |

Scope any of them to a calendar window:

```bash
python rumblelog.py leaderboard --period week
```

```bash
python rumblelog.py leaderboard --board donations --period month
```

`--period` takes `day`, `week` (Monday-start), `month`, `year` or `all`. It means the
period *containing* `--date`, which defaults to today — so `--period week --date
2026-06-09` scores the week of 8–14 June. Add `--stream latest` (or an id) to score a
single broadcast, `--top N` for longer lists, and `--format csv|json` to pipe it
somewhere:

```bash
python rumblelog.py leaderboard --period month --format csv > august.csv
```

Two things worth knowing about how these are scored:

- **Tenure** ranks by the earliest subscription date Rumble reports for each user. With a
  period filter it covers subscribers *observed during* that window — who was still around
  then — rather than who started then, since "longest subscribed among people who just
  subscribed" would be meaningless. It can only rank subscribers who appeared in the API's
  recent-subscribers window at some point; it is not your full subscriber roster.
- **Gifts** depend on Rumble exposing a gifter field. The parser accepts `gifted_by`,
  `gifter`, a nested `gift` object and several variants, but this one is unconfirmed. If
  the board comes up empty during a stream that had gifted subs, check `verify` output for
  the real field name, add it to `gifted_by()`, and run `reparse --rebuild`.

## Reading the raw data

`queries.sql` holds ready-to-paste SQL. Open the database with any SQLite client:

```bash
sqlite3 data/rumblelog.db
```

Or dump a stream to CSV (`messages.csv`, `rants.csv`, `events.csv` under
`data/export/<stream_id>/`):

```bash
python rumblelog.py export --stream latest
```

### Tables

| table | holds |
|---|---|
| `streams` | one row per broadcast: title, first/last seen, ended_at, peak viewers |
| `messages` | every chat message, with username, user_id, badges and the raw JSON |
| `rants` | paid messages, `amount_cents` normalised regardless of source field |
| `events` | `kind` of `follower` or `subscriber`, with amount and `gifted_by` |
| `totals` | follower/subscriber counts, written only when a count changes |
| `polls` | one row per poll: an audit trail, including suspected chat gaps |

## Building a click-to-install installer

The end result is `rumblelog-setup-1.0.0.exe`: a double-click installer that bundles
Python, asks for the API key in the wizard, registers the background task, and adds Start
Menu shortcuts. The person installing needs nothing preinstalled.

**Build prerequisites** (on your machine only):

```bash
python -m pip install pyinstaller
```

Inno Setup 6 — free, from <https://jrsoftware.org/isdl.php>. Then:

```bash
powershell -ExecutionPolicy Bypass -File build.ps1 -Check
```

```bash
powershell -ExecutionPolicy Bypass -File build.ps1
```

That freezes two executables and compiles the installer into `dist\`:

| | |
|---|---|
| `rumblelog.exe` | console build — the full CLI |
| `rumblelogw.exe` | windowed build — the service, and the dialogs |

Two builds because a console app run at logon flashes a terminal window, while a windowed
build has no stdout for CLI output. The scheduled task and the "Set API key" shortcut use
the windowed one; the leaderboard and status shortcuts use the console one.

Installed, it lands in `%LOCALAPPDATA%\Programs\RumbleLog` with data in
`%LOCALAPPDATA%\RumbleLog` — a per-user install, so there is no UAC prompt and the
scheduled task runs as whoever owns the API key.

### Distribution notes

Read these before sending the installer to anyone.

- **Unsigned installers get a SmartScreen warning.** Every downloader sees "Windows
  protected your PC" with the publisher listed as unknown, and has to click through
  *More info → Run anyway*. The only real fix is a code-signing certificate (roughly
  $100–400/year; an OV cert still needs to accumulate reputation, an EV cert gets trusted
  immediately). Pass one to the build with `-Sign cert.pfx -SignPassword ...` and both
  exes and the installer are signed and timestamped.
- **PyInstaller output gets flagged by antivirus.** False positives on frozen Python are
  common. Signing helps a lot; so does submitting a false-positive report to whichever
  vendor flags it.
- **Never ship your own `config.json`.** The API key is per-account and grants read access
  to your channel's stream data. It is gitignored and lives in each user's own data
  directory; the installer collects it per person in the wizard.
- **Tell your users what it collects.** It stores their viewers' usernames, messages and
  payment amounts on their machine. That is ordinary for stream tooling, but it is other
  people's data and should be stated plainly.

## If a field is mapped wrong

Every raw response is archived, so a bad guess is recoverable. Fix the mapping in
`rumblelog.py`, then rebuild the derived tables from disk:

```bash
python rumblelog.py reparse --rebuild
```

This discards `messages`, `rants` and `events` and re-derives them from
`data/raw/*.jsonl`. `streams`, `totals` and `polls` are untouched. Without `--rebuild` it
just replays and fills in anything missing, which is safe to run any time. Keep
`keep_raw_responses` set to `true` — it is what makes this possible.

## Limits worth knowing

- **No backfill.** The API exposes a rolling window of recent chat, not history. Anything
  said before the poller was running is unrecoverable. This is why it runs as a service.
- **Fast chat can outrun the window.** If a poll's window shares no messages with what is
  already stored, the poll is flagged `suspect_gap = 1` and a warning goes to the log. If
  it happens often, lower `poll_live_seconds` toward the rate limit; if it still happens,
  chat is genuinely faster than this endpoint can report and you need the SSE chat stream.
- **Rate limits.** Rumble limits this endpoint. 10s is comfortable; do not go below 5s.
- **Your own channel only.** The API key scopes to one account.
- **Rants only.** Donations through Streamlabs, Ko-fi or PayPal are not in this payload.
  For reportable earnings, trust Rumble's own dashboard over this database.

## Troubleshooting

| symptom | check |
|---|---|
| `status` shows no polls | `Get-ScheduledTask RumbleLog` — registered and running? |
| every poll fails | `rumblelog verify` — usually a stale or missing `api_url` |
| stream live but nothing captured | run `verify` during the stream; the chat block may be named differently |
| gifts leaderboard always empty | Rumble may name the gifter field something unexpected — see `gifted_by()` |
| rant amounts look 100x off | a bare number was assumed to be cents; check `verify` and fix `rant_cents()` |
| installer build fails | `build.ps1 -Check` lists missing tools |

## Commands

```
configure       set the Rumble API URL (dialog, or --url / --console)
verify          fetch once and report the payload shape
init            create the database
watch           poll forever (what the service runs)
status          summarise what has been captured
leaderboard     rank viewers; alias: top
export          write CSVs for one stream
reparse         re-derive rows from the raw archive
install-task    register the background service
uninstall-task  remove it
```

## Files

```
rumblelog.py            the program: capture, leaderboards, task registration
rumblelogw.py           windowless entry point, frozen into rumblelogw.exe
config.example.json     copy to config.json, or just run `configure`
build.ps1               freeze the exes and build the installer
installer/rumblelog.iss Inno Setup script
install-service.ps1     register the task when running from source
uninstall-service.ps1   remove it
queries.sql             ready-made SQL
data/                   database, logs, raw archive, exports (gitignored)
```
