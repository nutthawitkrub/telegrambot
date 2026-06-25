"""
SQLite persistence layer for the Telegram Instagram Monitor bot.

Tables
------
targets        – tracked Instagram usernames, chat context, active flag
activity_log   – profile-change events from instagram_monitor CSV (replaces .csv)
media_files    – image/video BLOBs: profile pics, posts, stories
log_entries    – monitor log lines (replaces .log)
process_events – lifecycle events (started / stopped / crashed / resumed)

Thread safety
-------------
A single SQLite connection opened with check_same_thread=False and WAL
journal mode is shared across all threads.  A threading.Lock serialises
every write so concurrent threads never race on a commit.  Reads proceed
without the lock; WAL allows concurrent readers alongside a writer.
"""

from __future__ import annotations

import csv
import io
import mimetypes
import re
import sqlite3
import threading
from pathlib import Path
from datetime import datetime

_conn: sqlite3.Connection | None = None
_write_lock = threading.Lock()

# ── init ──────────────────────────────────────────────────────────────────────

def init(db_path: Path) -> None:
    global _conn
    _conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _apply_schema()


def _apply_schema() -> None:
    with _write_lock:
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS targets (
                username   TEXT    PRIMARY KEY,
                chat_id    INTEGER NOT NULL,
                started_by TEXT    NOT NULL DEFAULT 'unknown',
                started_at TEXT    NOT NULL
                           DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                is_active  INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS activity_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT    NOT NULL,
                occurred_at TEXT    NOT NULL,
                change_type TEXT    NOT NULL,
                old_value   TEXT,
                new_value   TEXT,
                UNIQUE (username, occurred_at, change_type),
                FOREIGN KEY (username) REFERENCES targets(username)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_act_u_t
                ON activity_log(username, occurred_at);

            CREATE TABLE IF NOT EXISTS media_files (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                username         TEXT    NOT NULL,
                media_type       TEXT    NOT NULL
                                 CHECK(media_type IN
                                     ('profile_pic','post','reel','story')),
                filename         TEXT    NOT NULL,
                captured_at      TEXT,
                stored_at        TEXT    NOT NULL
                                 DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                file_size        INTEGER,
                mime_type        TEXT,
                data             BLOB,
                sent_to_telegram INTEGER NOT NULL DEFAULT 0,
                telegram_file_id TEXT,
                UNIQUE (username, filename),
                FOREIGN KEY (username) REFERENCES targets(username)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_med_u_type
                ON media_files(username, media_type, stored_at);

            CREATE TABLE IF NOT EXISTS log_entries (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                username  TEXT NOT NULL,
                logged_at TEXT NOT NULL
                          DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                message   TEXT NOT NULL,
                FOREIGN KEY (username) REFERENCES targets(username)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_log_u_t
                ON log_entries(username, logged_at, id);

            CREATE TABLE IF NOT EXISTS process_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL,
                occurred_at   TEXT NOT NULL
                              DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                event_type    TEXT NOT NULL
                              CHECK(event_type IN
                                  ('started','stopped','crashed','resumed')),
                exit_code     INTEGER,
                error_message TEXT,
                FOREIGN KEY (username) REFERENCES targets(username)
                    ON DELETE CASCADE
            );
        """)


# ── targets ───────────────────────────────────────────────────────────────────

def upsert_target(username: str, chat_id: int, started_by: str) -> None:
    with _write_lock:
        _conn.execute("""
            INSERT INTO targets(username, chat_id, started_by, is_active)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET
                chat_id    = excluded.chat_id,
                started_by = excluded.started_by,
                started_at = strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                is_active  = 1
        """, (username, chat_id, started_by))
        _conn.commit()


def deactivate_target(username: str) -> None:
    with _write_lock:
        _conn.execute(
            "UPDATE targets SET is_active = 0 WHERE username = ?", (username,)
        )
        _conn.commit()


def delete_target(username: str) -> None:
    """Hard-delete the target row and all its cascaded child rows."""
    with _write_lock:
        _conn.execute("DELETE FROM targets WHERE username = ?", (username,))
        _conn.commit()


def load_active_targets() -> list[dict]:
    rows = _conn.execute(
        "SELECT username, chat_id, started_by FROM targets WHERE is_active = 1"
    ).fetchall()
    return [dict(r) for r in rows]


# ── activity_log ──────────────────────────────────────────────────────────────

def export_activity_csv(username: str) -> bytes:
    """Return the full activity log for *username* as UTF-8 CSV bytes."""
    rows = _conn.execute("""
        SELECT occurred_at, change_type, old_value, new_value
        FROM   activity_log
        WHERE  username = ?
        ORDER  BY occurred_at, id
    """, (username,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(["Date", "Type", "Old", "New"])
    for r in rows:
        w.writerow([r["occurred_at"], r["change_type"],
                    r["old_value"] or "", r["new_value"] or ""])
    return buf.getvalue().encode("utf-8")


def import_csv(username: str, csv_path: Path) -> int:
    """
    Parse an instagram_monitor CSV export and bulk-insert into activity_log.
    Skips the header row and duplicate rows (INSERT OR IGNORE on UNIQUE key).
    Returns the number of new rows inserted.
    """
    if not csv_path.exists():
        return 0
    rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if len(row) < 2:
                    continue
                rows.append((
                    username,
                    row[0].strip(),
                    row[1].strip(),
                    row[2].strip() if len(row) > 2 else None,
                    row[3].strip() if len(row) > 3 else None,
                ))
    except Exception as e:
        print(f"Warning: could not read CSV {csv_path}: {e}")
        return 0

    with _write_lock:
        cur = _conn.executemany("""
            INSERT OR IGNORE INTO activity_log
                (username, occurred_at, change_type, old_value, new_value)
            VALUES (?, ?, ?, ?, ?)
        """, rows)
        _conn.commit()
        return cur.rowcount


# ── media_files ───────────────────────────────────────────────────────────────

_TS_LONG  = re.compile(r'_(\d{8}_\d{6})\.')   # YYYYMMDD_HHMMSS
_TS_SHORT = re.compile(r'_(\d{8}_\d{4})\.')    # YYYYMMDD_HHMM


def _parse_captured_at(filename: str) -> str | None:
    m = _TS_LONG.search(filename)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").isoformat()
        except ValueError:
            pass
    m = _TS_SHORT.search(filename)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d_%H%M").isoformat()
        except ValueError:
            pass
    return None


def store_media(username: str, media_type: str, filename: str,
                data: bytes, captured_at: str | None = None) -> int:
    """
    Upsert a media BLOB.  Returns the rowid.
    media_type: 'profile_pic' | 'post' | 'reel' | 'story'
    """
    if captured_at is None:
        captured_at = _parse_captured_at(filename)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with _write_lock:
        cur = _conn.execute("""
            INSERT INTO media_files
                (username, media_type, filename, captured_at,
                 file_size, mime_type, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username, filename) DO UPDATE SET
                data      = excluded.data,
                stored_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        """, (username, media_type, filename,
               captured_at, len(data), mime, data))
        _conn.commit()
        return cur.lastrowid


def get_latest_media(username: str, image_type: str) -> tuple[str, bytes] | None:
    """
    Return (filename, raw_bytes) of the most-recently stored media, or None.

    image_type maps the bot-facing strings to DB enum values:
        'profile' → profile_pic
        'post'    → post + reel
        'story'   → story
    """
    if image_type == "profile":
        types = ("profile_pic",)
    elif image_type == "post":
        types = ("post", "reel")
    else:
        types = (image_type,)

    ph = ",".join("?" * len(types))
    row = _conn.execute(f"""
        SELECT filename, data FROM media_files
        WHERE  username = ? AND media_type IN ({ph})
        ORDER  BY stored_at DESC, id DESC
        LIMIT  1
    """, (username, *types)).fetchone()

    return (row["filename"], bytes(row["data"])) if row else None


def mark_sent(filename: str, telegram_file_id: str | None = None) -> None:
    with _write_lock:
        _conn.execute("""
            UPDATE media_files
            SET    sent_to_telegram = 1, telegram_file_id = ?
            WHERE  filename = ?
        """, (telegram_file_id, filename))
        _conn.commit()


def filename_stored(filename: str) -> bool:
    row = _conn.execute(
        "SELECT 1 FROM media_files WHERE filename = ?", (filename,)
    ).fetchone()
    return row is not None


def import_media_file(username: str, path: Path, media_type: str) -> bool:
    """
    Read a disk file and store it as a BLOB.
    Returns True if newly stored, False if already present or on error.
    """
    if filename_stored(path.name):
        return False
    try:
        data = path.read_bytes()
        store_media(username, media_type, path.name, data)
        return True
    except Exception as e:
        print(f"Warning: could not import media {path.name}: {e}")
        return False


# ── log_entries ───────────────────────────────────────────────────────────────

def insert_log(username: str, message: str) -> None:
    with _write_lock:
        _conn.execute(
            "INSERT INTO log_entries(username, message) VALUES (?, ?)",
            (username, message)
        )
        _conn.commit()


def export_log_text(username: str) -> bytes:
    """Return all stored log lines for *username* as UTF-8 plain text."""
    rows = _conn.execute("""
        SELECT logged_at, message FROM log_entries
        WHERE  username = ?
        ORDER  BY logged_at, id
    """, (username,)).fetchall()
    return "\n".join(
        f"[{r['logged_at']}] {r['message']}" for r in rows
    ).encode("utf-8")


def has_log_entries(username: str) -> bool:
    row = _conn.execute(
        "SELECT 1 FROM log_entries WHERE username = ? LIMIT 1", (username,)
    ).fetchone()
    return row is not None


def import_log_file(username: str, log_path: Path) -> int:
    """
    Import a .log file into log_entries.  Skipped if entries already exist
    for this username (idempotent across restarts).  Returns lines inserted.
    """
    if has_log_entries(username) or not log_path.exists():
        return 0
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except Exception as e:
        print(f"Warning: could not read log {log_path}: {e}")
        return 0
    with _write_lock:
        _conn.executemany(
            "INSERT INTO log_entries(username, message) VALUES (?, ?)",
            [(username, line) for line in lines]
        )
        _conn.commit()
    return len(lines)


# ── process_events ────────────────────────────────────────────────────────────

def insert_event(username: str, event_type: str,
                 exit_code: int | None = None,
                 error_message: str | None = None) -> None:
    with _write_lock:
        _conn.execute("""
            INSERT INTO process_events(username, event_type, exit_code, error_message)
            VALUES (?, ?, ?, ?)
        """, (username, event_type, exit_code, error_message))
        _conn.commit()
