#!/usr/bin/env python3
"""Capture chat, rants, subscribers and followers from Rumble's Live Stream API.

Polls the personal API URL from Rumble account settings and accumulates every
snapshot into SQLite. Designed to run unattended as a service: it idles cheaply
when no stream is live and starts recording the moment one appears.

Commands:
    init      create the database
    verify    fetch once and report what the API actually returned
    watch     poll forever (this is what the service runs)
    status    summarise what has been captured
    export    write CSVs for one stream

Stdlib only, Python 3.8+.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import logging.handlers
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

USER_AGENT = "rumble-chat-chart/1.0 (+https://github.com/local)"

DEFAULTS: Dict[str, Any] = {
    "api_url": "",
    # Rumble rate-limits this endpoint; 10s while live is comfortably inside it.
    "poll_live_seconds": 10,
    "poll_idle_seconds": 30,
    "request_timeout_seconds": 20,
    "max_backoff_seconds": 300,
    "keep_raw_responses": True,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    created_on      TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    ended_at        TEXT,
    peak_watching   INTEGER DEFAULT 0,
    max_likes       INTEGER DEFAULT 0,
    max_dislikes    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    msg_id       TEXT PRIMARY KEY,
    stream_id    TEXT,
    username     TEXT,
    user_id      TEXT,
    text         TEXT,
    created_on   TEXT,
    captured_at  TEXT NOT NULL,
    badges       TEXT,
    raw          TEXT
);
CREATE TABLE IF NOT EXISTS rants (
    msg_id       TEXT PRIMARY KEY,
    stream_id    TEXT,
    username     TEXT,
    user_id      TEXT,
    text         TEXT,
    amount_cents INTEGER,
    created_on   TEXT,
    captured_at  TEXT NOT NULL,
    raw          TEXT
);
CREATE TABLE IF NOT EXISTS events (
    kind         TEXT NOT NULL,          -- 'follower' | 'subscriber'
    username     TEXT NOT NULL,
    user_id      TEXT,
    stream_id    TEXT,
    amount_cents INTEGER,
    occurred_on  TEXT,
    captured_at  TEXT NOT NULL,
    gifted_by    TEXT,                   -- who paid, when the sub was a gift
    raw          TEXT,
    PRIMARY KEY (kind, username, occurred_on)
);
CREATE TABLE IF NOT EXISTS polls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at     TEXT NOT NULL,
    stream_id       TEXT,
    watching        INTEGER,
    window_messages INTEGER,
    new_messages    INTEGER,
    overlap         INTEGER,
    http_status     INTEGER,
    suspect_gap     INTEGER DEFAULT 0,
    error           TEXT
);
-- Written only when a count actually changes, so this stays a compact history
-- rather than one row per poll.
CREATE TABLE IF NOT EXISTS totals (
    captured_at        TEXT PRIMARY KEY,
    followers_total    INTEGER,
    subscribers_total  INTEGER
);
"""

# Kept separate from the table DDL and applied last, because an index over a
# column added by a migration cannot be created until that migration has run.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_stream ON messages(stream_id, created_on);
CREATE INDEX IF NOT EXISTS idx_messages_user   ON messages(username);
CREATE INDEX IF NOT EXISTS idx_rants_stream    ON rants(stream_id, created_on);
CREATE INDEX IF NOT EXISTS idx_events_stream   ON events(stream_id, kind);
CREATE INDEX IF NOT EXISTS idx_events_gift     ON events(gifted_by);
CREATE INDEX IF NOT EXISTS idx_polls_time      ON polls(captured_at);
"""

log = logging.getLogger("rumble-chat-chart")


# --------------------------------------------------------------------------- #
# paths, config, logging
# --------------------------------------------------------------------------- #

def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Where the program lives. Read-only once installed."""
    if frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def home() -> Path:
    """Where config and captured data live. Must be writable.

    Running from source that is the project directory. Installed, it is
    %LOCALAPPDATA%\\RumbleChatChart, because an installed program's own
    directory is not a legitimate place to write user data.
    """
    override = os.environ.get("RUMBLE_CHAT_CHART_HOME")
    if override:
        base = Path(override)
    elif frozen():
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "RumbleChatChart"
    else:
        base = app_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


def data_dir() -> Path:
    d = home() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "rumble-chat-chart.db"


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    path = home() / "config.json"
    if path.exists():
        try:
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise SystemExit("could not read %s: %s" % (path, exc))
    env_url = os.environ.get("RUMBLE_CHAT_CHART_API_URL")
    if env_url:
        cfg["api_url"] = env_url.strip()
    return cfg


def require_api_url(cfg: Dict[str, Any]) -> str:
    url = (cfg.get("api_url") or "").strip()
    if not url:
        raise SystemExit(
            "No API URL configured.\n"
            "Copy config.example.json to config.json and paste the URL from\n"
            "Rumble -> Account Settings -> API, or set RUMBLE_CHAT_CHART_API_URL."
        )
    return url


def save_api_url(url: str) -> Path:
    """Write the key into config.json, preserving any other settings."""
    path = home() / "config.json"
    config: Dict[str, Any] = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            config = {}
    if not isinstance(config, dict):
        config = {}
    for key, value in DEFAULTS.items():
        config.setdefault(key, value)
    config["api_url"] = url.strip()
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def mask_url(url: str) -> str:
    """Never write the API key to a log file."""
    if "key=" not in url:
        return url
    head, _, tail = url.partition("key=")
    return head + "key=" + (tail[:4] + "..." if len(tail) > 4 else "...")


def setup_logging(verbose: bool = False) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.handlers[:] = []
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    handler = logging.handlers.RotatingFileHandler(
        data_dir() / "rumble-chat-chart.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(fmt)
    log.addHandler(handler)

    if sys.stderr and sys.stderr.isatty():
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        log.addHandler(console)


# --------------------------------------------------------------------------- #
# tolerant extraction
#
# The API's exact field names are not contractually documented, so every read
# goes through a helper that accepts the plausible spellings. Raw responses are
# archived to data/raw/ so anything mis-mapped can be re-parsed from disk.
# --------------------------------------------------------------------------- #

def pick(obj: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(obj, dict):
        return default
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return default


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_ts(value: Any) -> Optional[str]:
    """Normalise epoch seconds or an ISO-ish string to a sortable ISO string."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return str(value)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def cents_from(obj: Dict[str, Any]) -> Optional[int]:
    """Money arrives as cents or dollars depending on the field."""
    for key in ("amount_cents", "price_cents", "rant_price_cents", "cents"):
        value = as_int(obj.get(key))
        if value is not None and value > 0:
            return value
    for key in ("amount_dollars", "price_dollars", "dollars", "amount"):
        raw = obj.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            return int(round(float(raw) * 100))
        except (TypeError, ValueError):
            continue
    return None


def rant_cents(message: Dict[str, Any]) -> Optional[int]:
    """A chat message is a rant if it carries a positive amount."""
    direct = cents_from(message)
    if direct:
        return direct
    rant = message.get("rant")
    if isinstance(rant, dict):
        return cents_from(rant)
    value = as_int(rant)
    if value and value > 0:
        # Bare number: assume cents. `verify` prints the raw object so this can
        # be confirmed against a real rant.
        return value
    return None


def gifted_by(entry: Dict[str, Any]) -> Optional[str]:
    """Who paid for a gifted subscription, if this one was a gift.

    Rumble's naming here is unconfirmed, so several spellings are accepted and a
    nested gift object is unwrapped. Returns None for ordinary subscriptions.
    """
    direct = pick(entry, "gifted_by", "gifter", "gift_by", "gifted_by_username", "gifter_username")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if isinstance(direct, dict):
        nested = pick(direct, "username", "user", "display_name")
        return str(nested).strip() if nested else None

    gift = entry.get("gift")
    if isinstance(gift, dict):
        nested = pick(gift, "username", "gifted_by", "gifter", "from", "user")
        if nested:
            return str(nested).strip()
    elif isinstance(gift, str) and gift.strip():
        return gift.strip()
    return None


def block(payload: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Find a top-level block, tolerating a {"data": {...}} wrapper."""
    found = pick(payload, name)
    if isinstance(found, dict):
        return found
    nested = payload.get("data")
    if isinstance(nested, dict) and isinstance(nested.get(name), dict):
        return nested[name]
    return {}


def livestreams_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    for container in (payload, payload.get("data")):
        if isinstance(container, dict):
            found = container.get("livestreams")
            if isinstance(found, list):
                return [s for s in found if isinstance(s, dict)]
            single = container.get("livestream")
            if isinstance(single, dict):
                return [single]
    return []


def messages_of(stream: Dict[str, Any]) -> List[Dict[str, Any]]:
    chat = stream.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    found = pick(chat, "recent_messages", "messages", default=[])
    out = [m for m in found if isinstance(m, dict)] if isinstance(found, list) else []
    latest = chat.get("latest_message")
    if isinstance(latest, dict):
        out.append(latest)          # deduped downstream by message id
    return out


def explicit_rants_of(stream: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Some payload shapes carry rants separately from the message list."""
    chat = stream.get("chat")
    chat = chat if isinstance(chat, dict) else {}
    for source in (chat, stream):
        found = pick(source, "recent_rants", "rants")
        if isinstance(found, list):
            return [r for r in found if isinstance(r, dict)]
    return []


def message_id(stream_id: str, message: Dict[str, Any]) -> str:
    native = pick(message, "id", "message_id", "chat_id")
    if native is not None:
        return "%s:%s" % (stream_id, native)
    # No id: hash the identifying fields so repeated polls still collapse.
    seed = "|".join([
        stream_id,
        str(pick(message, "username", "user", default="")),
        str(norm_ts(pick(message, "created_on", "created_at", "time")) or ""),
        str(pick(message, "text", "message", default="")),
    ])
    return "h:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def badges_of(message: Dict[str, Any]) -> Optional[str]:
    found = pick(message, "badges", "badge")
    if found is None:
        return None
    if isinstance(found, str):
        found = [found]
    try:
        return json.dumps(found, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not add
# them to a database that already exists, so upgrades patch them in here.
ADDED_COLUMNS = (
    ("events", "gifted_by", "TEXT"),
)


def migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if existing and column not in existing:
            with conn:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
            log.info("migrated: added %s.%s", table, column)


def init_db() -> None:
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
    migrate(conn)
    with conn:
        conn.executescript(INDEXES)
    conn.close()


@dataclass
class PollStats:
    stream_id: Optional[str] = None
    watching: Optional[int] = None
    window_messages: int = 0
    new_messages: int = 0
    overlap: int = 0
    new_rants: int = 0
    new_events: int = 0
    suspect_gap: bool = False
    live_ids: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)


class Recorder:
    def __init__(self, conn: sqlite3.Connection, cfg: Dict[str, Any]) -> None:
        self.conn = conn
        self.cfg = cfg
        self._warned_shape = False

    # -- raw archive ------------------------------------------------------- #

    def archive(self, body: str) -> None:
        if not self.cfg.get("keep_raw_responses", True):
            return
        raw = data_dir() / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with (raw / ("%s.jsonl" % stamp)).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"captured_at": now_iso(), "body": body}, ensure_ascii=False) + "\n")

    # -- one snapshot ------------------------------------------------------ #

    def record(self, payload: Dict[str, Any], status: int, replay: bool = False) -> PollStats:
        """Fold one snapshot into the database.

        `replay` is for re-parsing the raw archive: it skips the poll audit row
        and leaves stream open/closed state alone, since an old payload says
        nothing about what is live now.
        """
        captured = now_iso()
        stats = PollStats()
        streams = livestreams_of(payload)

        if not streams and not self._warned_shape:
            top = sorted(payload.keys())[:12] if isinstance(payload, dict) else []
            log.debug("no livestreams in payload; top-level keys: %s", top)

        for stream in streams:
            if stream.get("is_live") is False:
                continue
            self._record_stream(stream, captured, stats)

        self._record_totals(payload, captured, stats)

        if not replay:
            self._close_finished(stats.live_ids, captured)
            with self.conn:
                self.conn.execute(
                    "INSERT INTO polls (captured_at, stream_id, watching,"
                    " window_messages, new_messages, overlap, http_status, suspect_gap, error)"
                    " VALUES (?,?,?,?,?,?,?,?,NULL)",
                    (captured, stats.stream_id, stats.watching, stats.window_messages,
                     stats.new_messages, stats.overlap, status, 1 if stats.suspect_gap else 0),
                )
        return stats

    def _record_stream(self, stream: Dict[str, Any], captured: str, stats: PollStats) -> None:
        stream_id = str(pick(stream, "id", "livestream_id", "stream_id", default="unknown"))
        title = pick(stream, "title", "name")
        watching = as_int(pick(stream, "watching_now", "viewers", "watching"))
        likes = as_int(pick(stream, "likes", "num_likes")) or 0
        dislikes = as_int(pick(stream, "dislikes", "num_dislikes")) or 0

        stats.stream_id = stream_id
        stats.watching = watching
        stats.live_ids.append(stream_id)

        with self.conn:
            cur = self.conn.execute("SELECT id FROM streams WHERE id = ?", (stream_id,))
            if cur.fetchone() is None:
                self.conn.execute(
                    "INSERT INTO streams (id, title, created_on, first_seen, last_seen,"
                    " peak_watching, max_likes, max_dislikes) VALUES (?,?,?,?,?,?,?,?)",
                    (stream_id, title, norm_ts(pick(stream, "created_on", "created_at")),
                     captured, captured, watching or 0, likes, dislikes),
                )
                stats.lines.append("● live · %s" % (title or stream_id))
                log.info("stream started: %s (%s)", title or "(untitled)", stream_id)
            else:
                self.conn.execute(
                    "UPDATE streams SET last_seen = ?, title = COALESCE(?, title),"
                    " peak_watching = MAX(peak_watching, COALESCE(?, 0)),"
                    " max_likes = MAX(max_likes, ?), max_dislikes = MAX(max_dislikes, ?),"
                    " ended_at = NULL WHERE id = ?",
                    (captured, title, watching or 0, likes, dislikes, stream_id),
                )

        # Collapse the window first: latest_message is normally also present in
        # recent_messages, and counting it twice would corrupt the overlap
        # figure that gap detection depends on.
        window: Dict[str, Dict[str, Any]] = {}
        for message in messages_of(stream):
            window.setdefault(message_id(stream_id, message), message)

        stats.window_messages += len(window)
        for msg_id, message in window.items():
            self._save_message(stream_id, message, captured, stats, msg_id)
        for rant in explicit_rants_of(stream):
            self._save_rant(stream_id, rant, captured, stats, forced=True)

        # A window with zero overlap against what we already hold means chat
        # outran the poll interval and messages fell off the far end.
        if window and stats.overlap == 0 and self._has_prior_messages(stream_id, exclude=stats.new_messages):
            stats.suspect_gap = True
            log.warning(
                "possible dropped messages on %s: %d-message window shared nothing with"
                " the previous poll; consider lowering poll_live_seconds",
                stream_id, len(window),
            )

    def _has_prior_messages(self, stream_id: str, exclude: int) -> bool:
        cur = self.conn.execute("SELECT COUNT(*) FROM messages WHERE stream_id = ?", (stream_id,))
        return int(cur.fetchone()[0]) > exclude

    def _save_message(self, stream_id: str, message: Dict[str, Any], captured: str,
                      stats: PollStats, msg_id: Optional[str] = None) -> None:
        msg_id = msg_id or message_id(stream_id, message)
        username = pick(message, "username", "user", "display_name")
        user_id = pick(message, "user_id", "uid")
        text = pick(message, "text", "message", "body", default="")
        created = norm_ts(pick(message, "created_on", "created_at", "time"))
        raw = json.dumps(message, ensure_ascii=False)

        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages (msg_id, stream_id, username, user_id,"
                " text, created_on, captured_at, badges, raw) VALUES (?,?,?,?,?,?,?,?,?)",
                (msg_id, stream_id, username, str(user_id) if user_id is not None else None,
                 text, created, captured, badges_of(message), raw),
            )
        if cur.rowcount:
            stats.new_messages += 1
            stats.lines.append("chat   %s: %s" % (username or "?", (text or "")[:80]))
        else:
            stats.overlap += 1
            return          # already stored, and so is its rant

        amount = rant_cents(message)
        if amount:
            self._save_rant(stream_id, message, captured, stats, forced=True, msg_id=msg_id, amount=amount)

    def _save_rant(self, stream_id: str, rant: Dict[str, Any], captured: str,
                   stats: PollStats, forced: bool = False,
                   msg_id: Optional[str] = None, amount: Optional[int] = None) -> None:
        amount = amount if amount is not None else rant_cents(rant)
        if not amount and not forced:
            return
        if not amount:
            return
        msg_id = msg_id or message_id(stream_id, rant)
        username = pick(rant, "username", "user", "display_name")
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO rants (msg_id, stream_id, username, user_id, text,"
                " amount_cents, created_on, captured_at, raw) VALUES (?,?,?,?,?,?,?,?,?)",
                (msg_id, stream_id, username,
                 str(pick(rant, "user_id", "uid")) if pick(rant, "user_id", "uid") is not None else None,
                 pick(rant, "text", "message", default=""), amount,
                 norm_ts(pick(rant, "created_on", "created_at", "time")), captured,
                 json.dumps(rant, ensure_ascii=False)),
            )
        if cur.rowcount:
            stats.new_rants += 1
            stats.lines.append("RANT   $%.2f  %s: %s"
                               % (amount / 100.0, username or "?",
                                  (pick(rant, "text", "message", default="") or "")[:60]))
            log.info("rant $%.2f from %s", amount / 100.0, username or "?")

    def _record_totals(self, payload: Dict[str, Any], captured: str, stats: PollStats) -> None:
        followers = block(payload, "followers")
        subscribers = block(payload, "subscribers")

        counts = (as_int(pick(followers, "num_followers_total", "num_followers")),
                  as_int(pick(subscribers, "num_subscribers_total", "num_subscribers")))
        if counts != (None, None):
            previous = self.conn.execute(
                "SELECT followers_total, subscribers_total FROM totals"
                " ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
            if previous is None or (previous[0], previous[1]) != counts:
                with self.conn:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO totals (captured_at, followers_total,"
                        " subscribers_total) VALUES (?,?,?)", (captured,) + counts,
                    )

        for kind, source, list_keys, latest_key, ts_keys in (
            ("follower", followers, ("recent_followers", "followers"), "latest_follower",
             ("followed_on", "created_on", "date")),
            ("subscriber", subscribers, ("recent_subscribers", "subscribers"), "latest_subscriber",
             ("subscribed_on", "followed_on", "created_on", "date")),
        ):
            entries = pick(source, *list_keys, default=[])
            entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            latest = source.get(latest_key)
            if isinstance(latest, dict):
                entries.append(latest)
            for entry in entries:
                self._save_event(kind, entry, ts_keys, captured, stats)

    def _save_event(self, kind: str, entry: Dict[str, Any], ts_keys: Sequence[str],
                    captured: str, stats: PollStats) -> None:
        username = pick(entry, "username", "user", "display_name")
        if not username:
            return
        occurred = norm_ts(pick(entry, *ts_keys)) or ""
        amount = cents_from(entry)
        user_id = pick(entry, "user_id", "uid")
        gifter = gifted_by(entry)

        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO events (kind, username, user_id, stream_id,"
                " amount_cents, occurred_on, captured_at, gifted_by, raw)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (kind, str(username), str(user_id) if user_id is not None else None,
                 stats.stream_id, amount, occurred, captured, gifter,
                 json.dumps(entry, ensure_ascii=False)),
            )
        if cur.rowcount:
            stats.new_events += 1
            money = " ($%.2f)" % (amount / 100.0) if amount else ""
            gift = " gifted by %s" % gifter if gifter else ""
            stats.lines.append("%-6s %s%s%s" % (kind[:3].upper(), username, money, gift))
            log.info("new %s: %s%s%s", kind, username, money, gift)

    def _close_finished(self, live_ids: Sequence[str], captured: str) -> None:
        cur = self.conn.execute("SELECT id, title FROM streams WHERE ended_at IS NULL")
        for row in cur.fetchall():
            if row["id"] not in live_ids:
                with self.conn:
                    self.conn.execute(
                        "UPDATE streams SET ended_at = ? WHERE id = ?", (captured, row["id"])
                    )
                log.info("stream ended: %s (%s)", row["title"] or "(untitled)", row["id"])


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

def fetch(url: str, timeout: int) -> Tuple[int, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:       # noqa: BLE001 - body is best-effort context only
            pass
        return exc.code, body


def note_failure(conn: sqlite3.Connection, status: Optional[int], message: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO polls (captured_at, http_status, error) VALUES (?,?,?)",
            (now_iso(), status, message[:500]),
        )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_init(_args: argparse.Namespace) -> int:
    init_db()
    print("database ready: %s" % db_path())
    config = home() / "config.json"
    if not config.exists():
        print("\nNext: copy config.example.json to config.json and paste your API URL")
        print("from Rumble -> Account Settings -> API.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = load_config()
    url = require_api_url(cfg)
    status, body = fetch(url, int(cfg["request_timeout_seconds"]))
    print("HTTP %s, %d bytes" % (status, len(body)))
    if status != 200:
        print("\nresponse body:\n%s" % body[:1000])
        return 1

    try:
        payload = json.loads(body)
    except ValueError as exc:
        print("response was not JSON: %s" % exc)
        return 1

    out = data_dir() / ("verify-%s.json" % datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("top-level keys: %s" % ", ".join(sorted(payload.keys())) if isinstance(payload, dict) else "not an object")

    streams = livestreams_of(payload)
    print("\nlivestreams found: %d" % len(streams))
    for stream in streams:
        print("  id=%s  title=%r  is_live=%r  watching=%r"
              % (pick(stream, "id", "livestream_id", default="?"),
                 pick(stream, "title", default=None),
                 stream.get("is_live"),
                 pick(stream, "watching_now", "viewers")))
        window = messages_of(stream)
        print("  chat window: %d messages" % len(window))
        if window:
            print("  first message object:")
            print(indent(json.dumps(window[0], indent=2, ensure_ascii=False)))
            rants = [m for m in window if rant_cents(m)]
            if rants:
                print("  a rant, as parsed -> $%.2f:" % (rant_cents(rants[0]) / 100.0))
                print(indent(json.dumps(rants[0], indent=2, ensure_ascii=False)))

    for name in ("followers", "subscribers"):
        data = block(payload, name)
        if not data:
            print("\n%s: block not found" % name)
            continue
        print("\n%s keys: %s" % (name, ", ".join(sorted(data.keys()))))
        for key, value in data.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                print("  %s[0]:" % key)
                print(indent(json.dumps(value[0], indent=2, ensure_ascii=False)))
                break

    print("\nfull response saved to %s" % out)
    if not streams:
        print("Nothing is live right now, so chat fields cannot be confirmed yet.")
        print("Re-run verify during a stream to check message and rant mapping.")
    return 0


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = load_config()
    url = require_api_url(cfg)
    init_db()
    conn = connect()
    recorder = Recorder(conn, cfg)

    stopping = {"flag": False}

    def stop(signum, _frame):
        stopping["flag"] = True
        log.info("signal %s received, shutting down", signum)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_number = getattr(signal, name, None)
        if signal_number is not None:
            try:
                signal.signal(signal_number, stop)
            except (ValueError, OSError):
                pass

    live_interval = max(5, int(cfg["poll_live_seconds"]))
    idle_interval = max(10, int(cfg["poll_idle_seconds"]))
    max_backoff = int(cfg["max_backoff_seconds"])
    timeout = int(cfg["request_timeout_seconds"])
    backoff = 0

    log.info("watching %s (live %ss / idle %ss)", mask_url(url), live_interval, idle_interval)

    while not stopping["flag"]:
        interval = idle_interval
        try:
            status, body = fetch(url, timeout)
            if status != 200:
                raise RuntimeError("HTTP %s: %s" % (status, body[:200]))
            payload = json.loads(body)
        except (urllib.error.URLError, RuntimeError, ValueError, OSError) as exc:
            backoff = min(max_backoff, (backoff * 2) or live_interval)
            log.warning("poll failed (%s); retrying in %ds", exc, backoff)
            note_failure(conn, None, str(exc))
            interval = backoff
        else:
            backoff = 0
            if livestreams_of(payload):
                recorder.archive(body)
            stats = recorder.record(payload, status)
            for line in stats.lines:
                log.info("%s", line)
            if stats.stream_id:
                interval = live_interval
                log.debug("poll: %d in window, %d new, %d overlap, %s watching",
                          stats.window_messages, stats.new_messages, stats.overlap, stats.watching)

        slept = 0.0
        while slept < interval and not stopping["flag"]:
            time.sleep(min(1.0, interval - slept))
            slept += 1.0

    conn.close()
    log.info("stopped")
    return 0


def resolve_stream(conn: sqlite3.Connection, selector: str) -> Optional[sqlite3.Row]:
    if selector in ("latest", "last"):
        cur = conn.execute("SELECT * FROM streams ORDER BY first_seen DESC LIMIT 1")
    else:
        cur = conn.execute("SELECT * FROM streams WHERE id = ?", (selector,))
    return cur.fetchone()


def cmd_status(args: argparse.Namespace) -> int:
    if not db_path().exists():
        print("no database yet; run: python rumble_chat_chart.py init")
        return 1
    conn = connect()
    init_db()

    row = conn.execute(
        "SELECT captured_at, http_status, error FROM polls ORDER BY captured_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        print("no polls recorded yet - is the service running?")
    else:
        state = "ok" if not row["error"] else "ERROR: %s" % row["error"]
        print("last poll: %s  (%s)" % (row["captured_at"], state))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    failures = conn.execute(
        "SELECT COUNT(*) FROM polls WHERE error IS NOT NULL AND captured_at LIKE ?",
        (today + "%",),
    ).fetchone()[0]
    gaps = conn.execute("SELECT COUNT(*) FROM polls WHERE suspect_gap = 1").fetchone()[0]
    print("failed polls today: %d    suspected chat gaps (all time): %d" % (failures, gaps))

    print("\nstreams:")
    rows = conn.execute("SELECT * FROM streams ORDER BY first_seen DESC LIMIT %d" % int(args.limit)).fetchall()
    if not rows:
        print("  (none captured yet)")
    for stream in rows:
        messages = conn.execute("SELECT COUNT(*) FROM messages WHERE stream_id = ?", (stream["id"],)).fetchone()[0]
        rant_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM rants WHERE stream_id = ?",
            (stream["id"],)).fetchone()
        subs = conn.execute(
            "SELECT COUNT(*) FROM events WHERE stream_id = ? AND kind = 'subscriber'",
            (stream["id"],)).fetchone()[0]
        print("  %s  %s" % (stream["first_seen"], stream["title"] or "(untitled)"))
        print("    id=%s  %s  peak %s watching" % (
            stream["id"],
            "LIVE NOW" if stream["ended_at"] is None else "ended %s" % stream["ended_at"],
            stream["peak_watching"]))
        print("    %d messages · %d rants ($%.2f) · %d subs"
              % (messages, rant_row[0], rant_row[1] / 100.0, subs))
    conn.close()
    return 0


DERIVED_TABLES = ("messages", "rants", "events")


def cmd_reparse(args: argparse.Namespace) -> int:
    """Re-derive chat/rants/events from the archived raw responses.

    Use this if a field turned out to be named something other than what the
    parser guessed: fix the mapping, then --rebuild to recover the real values
    from disk instead of losing the stream.
    """
    cfg = load_config()
    init_db()
    conn = connect()

    raw_dir = data_dir() / "raw"
    files = sorted(raw_dir.glob("*.jsonl")) if raw_dir.exists() else []
    if not files:
        print("no raw archive in %s - nothing to re-parse." % raw_dir)
        print("(is keep_raw_responses enabled in config.json?)")
        return 1

    before = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in DERIVED_TABLES}
    if args.rebuild:
        print("rebuilding from scratch; discarding %s"
              % ", ".join("%d %s" % (before[t], t) for t in DERIVED_TABLES))
        with conn:
            for table in DERIVED_TABLES:
                conn.execute("DELETE FROM %s" % table)

    recorder = Recorder(conn, cfg)
    snapshots = skipped = 0
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(json.loads(line)["body"])
                except (ValueError, KeyError, TypeError):
                    skipped += 1
                    continue
                recorder.record(payload, 200, replay=True)
                snapshots += 1
        print("  %s" % path.name)

    after = {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0] for t in DERIVED_TABLES}
    print("\nreplayed %d snapshots from %d file(s)%s"
          % (snapshots, len(files), ", %d unreadable" % skipped if skipped else ""))
    for table in DERIVED_TABLES:
        delta = after[table] - before[table]
        print("  %-9s %d (%+d)" % (table, after[table], delta))
    conn.close()
    return 0


EXPORTS = {
    "messages": ("SELECT created_on, captured_at, username, user_id, text, badges"
                 " FROM messages WHERE stream_id = ? ORDER BY created_on, captured_at"),
    "rants": ("SELECT created_on, username, user_id, amount_cents, text"
              " FROM rants WHERE stream_id = ? ORDER BY created_on"),
    "events": ("SELECT occurred_on, captured_at, kind, username, user_id, amount_cents"
               " FROM events WHERE stream_id = ? ORDER BY kind, occurred_on"),
}


def cmd_export(args: argparse.Namespace) -> int:
    conn = connect()
    stream = resolve_stream(conn, args.stream)
    if stream is None:
        print("no stream matching %r; try: python rumble_chat_chart.py status" % args.stream)
        return 1

    target = data_dir() / "export" / str(stream["id"])
    target.mkdir(parents=True, exist_ok=True)
    for name, query in EXPORTS.items():
        rows = conn.execute(query, (stream["id"],)).fetchall()
        path = target / ("%s.csv" % name)
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys() if rows else ["(no rows)"])
            for row in rows:
                writer.writerow(list(row))
        print("%-9s %5d rows -> %s" % (name, len(rows), path))
    conn.close()
    return 0


# --------------------------------------------------------------------------- #
# leaderboards
# --------------------------------------------------------------------------- #

PERIODS = ("day", "week", "month", "year", "all")
BOARDS = ("chat", "donations", "gifts", "tenure")


def period_bounds(period: str, anchor: Optional[date] = None) -> Tuple[Optional[str], Optional[str], str]:
    """Return (start, end, label) for a calendar period containing `anchor`.

    Bounds are date-only ISO strings. Stored timestamps are full ISO strings
    beginning with the same date format, so plain string comparison scopes them
    correctly without any timezone arithmetic.
    """
    today = anchor or datetime.now(timezone.utc).date()

    if period == "all":
        return None, None, "all time"
    if period == "day":
        start, end = today, today + timedelta(days=1)
        label = start.isoformat()
    elif period == "week":
        start = today - timedelta(days=today.weekday())      # ISO weeks start Monday
        end = start + timedelta(days=7)
        label = "week of %s (%s to %s)" % (start.isoformat(), start.isoformat(),
                                           (end - timedelta(days=1)).isoformat())
    elif period == "month":
        start = today.replace(day=1)
        end = (start.replace(year=start.year + 1, month=1) if start.month == 12
               else start.replace(month=start.month + 1))
        label = start.strftime("%B %Y")
    elif period == "year":
        start = today.replace(month=1, day=1)
        end = start.replace(year=start.year + 1)
        label = str(start.year)
    else:
        raise SystemExit("unknown period %r; choose from %s" % (period, ", ".join(PERIODS)))

    return start.isoformat(), end.isoformat(), label


def scope(column: str, start: Optional[str], end: Optional[str],
          stream: Optional[str], stream_column: str) -> Tuple[str, List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if start and end:
        clauses.append("%s >= ? AND %s < ?" % (column, column))
        params += [start, end]
    if stream:
        clauses.append("%s = ?" % stream_column)
        params.append(stream)
    return ("".join(" AND " + c for c in clauses), params)


def board_chat(conn, start, end, stream, top):
    where, params = scope("COALESCE(created_on, captured_at)", start, end, stream, "stream_id")
    rows = conn.execute(
        "SELECT username, COUNT(*) AS n FROM messages"
        " WHERE username IS NOT NULL AND username != ''" + where +
        " GROUP BY username ORDER BY n DESC, username LIMIT ?", params + [top]).fetchall()
    return (["#", "user", "messages"],
            [[i, r["username"], r["n"]] for i, r in enumerate(rows, 1)])


def board_donations(conn, start, end, stream, top):
    where, params = scope("COALESCE(created_on, captured_at)", start, end, stream, "stream_id")
    rows = conn.execute(
        "SELECT username, SUM(amount_cents) AS cents, COUNT(*) AS n FROM rants"
        " WHERE username IS NOT NULL AND username != ''" + where +
        " GROUP BY username ORDER BY cents DESC, username LIMIT ?", params + [top]).fetchall()
    return (["#", "user", "total", "rants"],
            [[i, r["username"], "$%.2f" % ((r["cents"] or 0) / 100.0), r["n"]]
             for i, r in enumerate(rows, 1)])


def board_gifts(conn, start, end, stream, top):
    where, params = scope("COALESCE(occurred_on, captured_at)", start, end, stream, "stream_id")
    rows = conn.execute(
        "SELECT gifted_by AS username, COUNT(*) AS n FROM events"
        " WHERE kind = 'subscriber' AND gifted_by IS NOT NULL AND gifted_by != ''" + where +
        " GROUP BY gifted_by ORDER BY n DESC, gifted_by LIMIT ?", params + [top]).fetchall()
    return (["#", "user", "subs gifted"],
            [[i, r["username"], r["n"]] for i, r in enumerate(rows, 1)])


def board_tenure(conn, start, end, stream, top):
    """Longest-running subscribers.

    Ranked by the earliest subscription date Rumble has reported for each user.
    A period filter scopes this to subscribers *observed* during that period —
    that is, who was still around then — rather than who started then, since
    ranking by tenure over "people who just subscribed" would be meaningless.
    """
    where, params = scope("captured_at", start, end, stream, "stream_id")
    rows = conn.execute(
        "SELECT username, MIN(occurred_on) AS since FROM events"
        " WHERE kind = 'subscriber' AND occurred_on IS NOT NULL AND occurred_on != ''" + where +
        " GROUP BY username ORDER BY since ASC, username LIMIT ?", params + [top]).fetchall()

    now = datetime.now(timezone.utc)
    out = []
    for i, row in enumerate(rows, 1):
        days = ""
        try:
            started = datetime.fromisoformat(str(row["since"]).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            days = str((now - started).days)
        except ValueError:
            pass
        out.append([i, row["username"], str(row["since"])[:10], days])
    return ["#", "user", "subscribed since", "days"], out


BOARD_FUNCTIONS = {
    "chat": ("Most chat messages", board_chat),
    "donations": ("Most donated (rants)", board_donations),
    "gifts": ("Most subscriptions gifted", board_gifts),
    "tenure": ("Longest subscribed", board_tenure),
}


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return "    (nothing in this period)"
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))

    def line(cells):
        return "    " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()

    out = [line(headers), "    " + "  ".join("-" * w for w in widths)]
    out += [line(row) for row in rows]
    return "\n".join(out)


def cmd_leaderboard(args: argparse.Namespace) -> int:
    if not db_path().exists():
        print("no database yet; run: python rumble_chat_chart.py init")
        return 1
    conn = connect()
    migrate(conn)

    anchor = None
    if args.date:
        try:
            anchor = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            raise SystemExit("--date must be YYYY-MM-DD, got %r" % args.date)

    start, end, label = period_bounds(args.period, anchor)
    wanted = list(BOARDS) if args.board == "all" else [args.board]

    stream = args.stream
    if stream in ("latest", "last"):
        row = resolve_stream(conn, "latest")
        if row is None:
            print("no streams captured yet")
            return 1
        stream = row["id"]

    results = {}
    for name in wanted:
        title, function = BOARD_FUNCTIONS[name]
        headers, rows = function(conn, start, end, stream, int(args.top))
        results[name] = {"title": title, "period": label, "stream": stream,
                         "headers": headers, "rows": rows}

    if args.format == "json":
        print(json.dumps(results, indent=2, ensure_ascii=False))
    elif args.format == "csv":
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(["board", "period", "rank", "user", "value", "extra"])
        for name in wanted:
            for row in results[name]["rows"]:
                padded = list(row) + [""] * (4 - len(row))
                writer.writerow([name, label] + padded[:4])
    else:
        scope_note = "  ·  stream %s" % stream if stream else ""
        print("\nLeaderboards — %s%s\n" % (label, scope_note))
        for name in wanted:
            board = results[name]
            print("  %s" % board["title"])
            print(render_table(board["headers"], board["rows"]))
            if name == "gifts" and not board["rows"]:
                print("    (no gifted subscriptions recorded — if Rumble reports them under a")
                print("     field name this parser does not recognise, see gifted_by() and then")
                print("     run: python rumble_chat_chart.py reparse --rebuild)")
            print("")

    conn.close()
    return 0


# --------------------------------------------------------------------------- #
# scheduled task registration
#
# Done here rather than in PowerShell so the frozen exe can register itself from
# the installer, and so source and installed builds share one implementation.
# --------------------------------------------------------------------------- #

TASK_NAME = "RumbleChatChart"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Polls the Rumble Live Stream API and logs chat, rants and subscribers.</Description>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def service_command() -> Tuple[str, str]:
    """The (executable, arguments) the task should launch.

    Installed, that is the windowless service exe sitting in a sibling folder.
    From source, pythonw.exe so no console window appears.
    """
    if frozen():
        sibling = app_dir().parent / "gui" / "rumble-chat-chartw.exe"
        if sibling.exists():
            return str(sibling), ""
        return str(Path(sys.executable).resolve()), "watch"

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return str(interpreter), '"%s" watch' % (app_dir() / "rumble_chat_chart.py")


def task_xml() -> str:
    from xml.sax.saxutils import escape

    command, arguments = service_command()
    user = "%s\\%s" % (os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ".",
                       os.environ.get("USERNAME") or "")
    return TASK_XML.format(
        name=TASK_NAME,
        user=escape(user),
        start=datetime.now().replace(microsecond=0).isoformat(),
        command=escape(command),
        arguments=escape(arguments),
        workdir=escape(str(app_dir())),
    )


def run_schtasks(args: Sequence[str]) -> Tuple[int, str]:
    import subprocess

    completed = subprocess.run(
        ["schtasks"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    return completed.returncode, completed.stdout.decode("utf-8", "replace").strip()


def cmd_install_task(args: argparse.Namespace) -> int:
    xml = task_xml()
    if args.dry_run:
        print(xml)
        return 0

    init_db()
    xml_path = home() / "task.xml"
    # schtasks /XML requires UTF-16.
    xml_path.write_text(xml, encoding="utf-16")
    try:
        code, output = run_schtasks(["/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
    finally:
        try:
            xml_path.unlink()
        except OSError:
            pass

    if code != 0:
        print("could not register the task (exit %d):\n%s" % (code, output))
        return code
    print(output or "task '%s' registered" % TASK_NAME)

    code, output = run_schtasks(["/Run", "/TN", TASK_NAME])
    if code != 0:
        print("registered, but could not start it now (exit %d): %s" % (code, output))
        print("it will start at your next logon regardless.")
        return 0

    command, arguments = service_command()
    print("started: %s %s" % (command, arguments))
    print("\nIt will now start automatically at every logon.")
    return 0


def cmd_uninstall_task(_args: argparse.Namespace) -> int:
    run_schtasks(["/End", "/TN", TASK_NAME])
    code, output = run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if code != 0:
        print("could not remove the task (exit %d): %s" % (code, output))
        return code
    print(output or "task '%s' removed" % TASK_NAME)
    print("Captured data in %s was left alone." % data_dir())
    return 0


# --------------------------------------------------------------------------- #
# key entry
# --------------------------------------------------------------------------- #

def check_api_url(url: str, timeout: int = 20) -> Tuple[bool, str]:
    try:
        status, body = fetch(url, timeout)
    except Exception as exc:                    # noqa: BLE001 - report any failure verbatim
        return False, "could not reach Rumble: %s" % exc
    if status != 200:
        return False, "Rumble returned HTTP %s - check the URL was copied whole." % status
    try:
        payload = json.loads(body)
    except ValueError:
        return False, "that URL did not return JSON."
    live = len(livestreams_of(payload))
    who = pick(payload, "username", default=None) or pick(payload.get("data") or {}, "username")
    return True, "Key works%s. %s" % (
        " for channel '%s'" % who if who else "",
        "%d livestream(s) reported." % live if live else "Nothing live right now, which is fine.",
    )


def ask_url_gui(current: str) -> Optional[str]:
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except ImportError:
        return None

    root = tk.Tk()
    root.withdraw()
    try:
        prompt = ("Paste your Rumble Live Stream API URL.\n\n"
                  "Find it in Rumble under Account Settings → API.")
        if current:
            prompt += "\n\nCurrently set to: %s" % mask_url(current)
        return simpledialog.askstring("Rumble Chat Chart — API key", prompt, parent=root)
    finally:
        root.destroy()


def cmd_configure(args: argparse.Namespace) -> int:
    current = (load_config().get("api_url") or "").strip()

    url = args.url
    if not url and not args.console:
        url = ask_url_gui(current)
        if url is None and not sys.stdin.isatty():
            print("cancelled")
            return 1
    if not url:
        print("Paste your Rumble Live Stream API URL (Account Settings -> API).")
        if current:
            print("Currently: %s" % mask_url(current))
        try:
            url = input("URL: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled")
            return 1

    url = (url or "").strip().strip('"')
    if not url.lower().startswith("http"):
        message = "That does not look like a URL. Nothing was saved."
        print(message)
        _gui_notice("Rumble Chat Chart", message, error=True, enabled=not args.console)
        return 1

    path = save_api_url(url)
    print("saved to %s" % path)

    ok, detail = check_api_url(url, int(load_config()["request_timeout_seconds"]))
    print(detail)
    _gui_notice("Rumble Chat Chart", detail, error=not ok, enabled=not args.console)
    return 0 if ok else 1


def _gui_notice(title: str, message: str, error: bool = False, enabled: bool = True) -> None:
    if not enabled:
        return
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError:
        return
    root = tk.Tk()
    root.withdraw()
    try:
        if error:
            messagebox.showerror(title, message, parent=root)
        else:
            messagebox.showinfo(title, message, parent=root)
    except Exception:                           # noqa: BLE001 - a dialog is never worth crashing over
        pass
    finally:
        root.destroy()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)
    sub.add_parser("verify", help="fetch once and report the payload shape").set_defaults(func=cmd_verify)
    sub.add_parser("watch", help="poll forever (the service entry point)").set_defaults(func=cmd_watch)

    status = sub.add_parser("status", help="summarise what has been captured")
    status.add_argument("--limit", default=10, help="streams to list")
    status.set_defaults(func=cmd_status)

    board = sub.add_parser("leaderboard", aliases=["top"], help="rank viewers")
    board.add_argument("--board", default="all", choices=list(BOARDS) + ["all"],
                       help="which ranking to show (default: all four)")
    board.add_argument("--period", default="all", choices=list(PERIODS),
                       help="calendar window to score (default: all time)")
    board.add_argument("--date", default=None,
                       help="anchor date YYYY-MM-DD for --period (default: today)")
    board.add_argument("--stream", default=None, help="limit to one stream id, or 'latest'")
    board.add_argument("--top", default=10, help="rows per board")
    board.add_argument("--format", default="table", choices=("table", "csv", "json"))
    board.set_defaults(func=cmd_leaderboard)

    task = sub.add_parser("install-task", help="register the background service")
    task.add_argument("--dry-run", action="store_true", help="print the task XML and exit")
    task.set_defaults(func=cmd_install_task)

    sub.add_parser("uninstall-task", help="remove the background service").set_defaults(
        func=cmd_uninstall_task)

    configure = sub.add_parser("configure", help="set the Rumble API URL")
    configure.add_argument("--url", default=None, help="set non-interactively")
    configure.add_argument("--console", action="store_true", help="never show a dialog")
    configure.set_defaults(func=cmd_configure)

    reparse = sub.add_parser("reparse", help="re-derive rows from the raw archive")
    reparse.add_argument("--rebuild", action="store_true",
                         help="discard messages/rants/events first and rebuild them")
    reparse.set_defaults(func=cmd_reparse)

    export = sub.add_parser("export", help="write CSVs for one stream")
    export.add_argument("--stream", default="latest", help="stream id, or 'latest'")
    export.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2

    setup_logging(args.verbose)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
