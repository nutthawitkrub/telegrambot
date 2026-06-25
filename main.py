"""
Telegram <-> instagram_monitor bridge bot.

For each tracked Instagram username, this bot:
  1. Launches `instagram_monitor <username> --config-file telegram_monitor.conf`
     as a subprocess, running inside its own working directory
     (./monitors/<username>/) so log/json/csv files never collide between
     targets.
  2. Tails that target's *_monitor_<username>.log file in a background
     thread and forwards EVERY line written to it - including the startup
     banner - to your Telegram chat as plain text, prefixed "[<username>]".
  3. Watches that same directory for new downloaded media (profile pic,
     post/reel, story) in a second background thread, and pushes each new
     file to Telegram automatically, the moment it's saved to disk.
  4. Lets you stop/list targets from Telegram. /stop also deletes that
     username's entire data folder (logs, CSV, downloaded media) - use
     /data first if you want a copy.
  5. Lets you pull the LATEST downloaded media for a target on demand via
     /image <username> <profile|post|story>, independent of the automatic
     push above, or grab the full log + CSV activity export via
     /data <username>.

Commands:
    /start, /help      - show usage
    /track             - track the pre-configured USERNAME from .env
    /trackother        - bot asks for a username; reply with plain text
    /image <u> <type>  - send the latest downloaded image/video for <u>
    /data <u>          - send the CSV activity export for <u>
    /status            - list currently tracked usernames + process state
    /stop <username>   - stop tracking AND delete all saved data for <u>

SESSION MODE: anonymous (Mode 1) only, by design.
    This bot deliberately never logs into Instagram. Mode 2 (session
    login) support has been intentionally removed from this codebase
    after a watcher account got checkpointed by Instagram while testing
    it - both because of that direct risk, and because Mode 2's session
    login path was separately hitting an unresolved, widely-reported
    Instaloader bug (401 Unauthorized on graphql/query even with a valid,
    freshly-created session) regardless of how the session was created.
    There is no SESSION_USERNAME/-u flag anywhere in this file anymore -
    if you want Mode 2 back in the future, you'd need to re-add it
    deliberately, not just set an env var.

Requires (see requirements.txt):
    pyTelegramBotAPI, python-dotenv
    (instagram_monitor and its own dependencies must be pip-installed
    separately and available on PATH)
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import signal
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone

IS_WINDOWS = sys.platform.startswith("win")

import io

import telebot
from dotenv import load_dotenv

import database as db

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN = os.getenv("API_KEY")
DEFAULT_USERNAME = os.getenv("INSTAGRAM_USERNAME")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # optional fallback target

if not BOT_TOKEN:
    raise ValueError("Error: API_KEY is missing from your .env file.")

# DATA_DIR is where everything that needs to SURVIVE a redeploy/restart lives:
# monitors/ (per-target logs, downloaded media, follower/following JSON),
# telegram_monitor.conf, and (separately) the Instaloader session file.
#
# Locally this defaults to the project folder itself, matching the original
# behavior. On a PaaS like Railway, set DATA_DIR to your mounted volume's
# path (e.g. /data) - see README "Deploying on Railway" for the full setup.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
MONITORS_DIR = DATA_DIR / "monitors"
SHARED_CONFIG = DATA_DIR / "telegram_monitor.conf"

# Records which usernames are (meant to be) actively tracked and who/where
# started them, so a restart/redeploy can resume them automatically instead
# of silently dropping all tracking. Updated on every start_tracking/
# stop_tracking call; read once at startup.
STATE_FILE = DATA_DIR / "active_targets.json"

# Username rule: Instagram usernames are letters, digits, periods, underscores,
# 1-30 chars. We validate user-supplied input against this before ever
# passing it to subprocess or using it in a filesystem path.
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

MAX_MSG_LEN = 3500  # stay safely under Telegram's 4096-char limit

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, num_threads=20)

# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

# username -> {"process": Popen, "thread": Thread, "stop_event": Event,
#              "chat_id": int, "log_path": Path, "started_at": datetime}
_targets: dict[str, dict] = {}
_targets_lock = threading.Lock()

# (chat_id, user_id) -> True while we're waiting for a username reply after
# /trackother. Keyed per-user-per-chat so two people in the same group can
# each have their own pending /trackother prompt without colliding.
_awaiting_username: dict[tuple[int, int], bool] = {}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def awaiting_key(message) -> tuple[int, int]:
    user_id = message.from_user.id if message.from_user else 0
    return (message.chat.id, user_id)


def is_valid_username(name: str) -> bool:
    return bool(USERNAME_RE.match(name))


def describe_sender(message) -> str:
    """Human-readable 'who started this' label, for cross-chat status messages."""
    chat = message.chat
    if chat.type == "private":
        user = message.from_user
        if user:
            name = user.first_name or ""
            if user.username:
                return f"{name} (@{user.username})".strip()
            return name or f"user {user.id}"
        return "someone"
    # group / supergroup
    title = chat.title or "a group chat"
    user = message.from_user
    who = f"@{user.username}" if user and user.username else (user.first_name if user else "someone")
    return f"{who} in '{title}'"


# --------------------------------------------------------------------------
# UI: persistent keyboard + inline keyboards
# --------------------------------------------------------------------------
# Per https://core.telegram.org/bots/features#keyboards and #inline-keyboards:
# - ReplyKeyboardMarkup buttons send their text as a real chat message, so a
#   button labeled "/track" is functionally identical to typing /track - the
#   existing @bot.message_handler(commands=[...]) handlers need no changes.
# - InlineKeyboardMarkup buttons attach to one specific message and fire a
#   callback_query instead of sending chat text. Telegram requires every
#   callback_query to be acknowledged via answer_callback_query, or the
#   client shows a loading spinner indefinitely.
# - InlineKeyboardButton.callback_data is capped at 64 UTF-8 bytes. Our
#   longest scheme ("img:" + up to 30-char username + ":" + type) stays
#   comfortably under that, but if Instagram's username limit ever changes,
#   this cap is worth re-checking.

def main_reply_keyboard() -> telebot.types.ReplyKeyboardMarkup:
    """The persistent, always-visible keyboard with top-level actions."""
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.row("/track", "/trackother")
    kb.row("/status", "/stop")
    kb.row("/image", "/data")
    kb.row("/cancel")
    return kb


def username_picker_keyboard(action_prefix: str) -> telebot.types.InlineKeyboardMarkup | None:
    """
    Inline keyboard listing every currently-tracked username, for commands
    that need one (/stop, /image, /data) when called with no argument.
    Returns None if nothing is currently tracked - caller decides what to
    say in that case.
    """
    with _targets_lock:
        usernames = list(_targets.keys())
    if not usernames:
        return None

    kb = telebot.types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        telebot.types.InlineKeyboardButton(f"@{u}", callback_data=f"{action_prefix}:{u}")
        for u in usernames
    ]
    kb.add(*buttons)
    return kb


def image_type_keyboard(username: str) -> telebot.types.InlineKeyboardMarkup:
    """Inline keyboard for picking profile/post/story, once a username is chosen for /image."""
    kb = telebot.types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        telebot.types.InlineKeyboardButton("Profile", callback_data=f"img:{username}:profile"),
        telebot.types.InlineKeyboardButton("Post", callback_data=f"img:{username}:post"),
        telebot.types.InlineKeyboardButton("Story", callback_data=f"img:{username}:story"),
    )
    return kb


def target_dir(username: str) -> Path:
    return MONITORS_DIR / username


def load_persisted_state() -> dict:
    """
    Return {username: {chat_id, started_by}} for all active targets.
    Reads from the DB first; falls back to the legacy JSON file on first boot
    (before any DB rows exist) so existing deployments resume without data loss.
    """
    rows = db.load_active_targets()
    if rows:
        return {r["username"]: {"chat_id": r["chat_id"], "started_by": r["started_by"]}
                for r in rows}
    # First-boot fallback: read the old JSON file if DB is empty
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"Warning: could not read {STATE_FILE}: {e}")
        return {}


def save_persisted_state() -> None:
    """Sync active targets to DB. Caller must hold _targets_lock."""
    for username, info in _targets.items():
        for cid, started_by in info.get("subscribers", {}).items():
            db.upsert_target(username, cid, started_by or "unknown")


def log_path_for(username: str) -> Path:
    # instagram_monitor writes instagram_monitor_<username>.log into its cwd
    # by default (single-target mode names the log after the username).
    return target_dir(username) / f"instagram_monitor_{username}.log"


def stderr_path_for(username: str) -> Path:
    """Where we capture the subprocess's stderr, separate from its own log file.

    instagram_monitor's own logger only writes deliberate log lines - an
    unhandled exception, missing dependency, or auth failure goes to
    stderr instead and would otherwise be lost (previously sent to
    DEVNULL), which is why tracking could die with no visible reason.
    """
    return target_dir(username) / f"instagram_monitor_{username}.stderr.log"


def csv_path_for(username: str) -> Path:
    """Where instagram_monitor's built-in CSV activity export is written."""
    return target_dir(username) / f"instagram_monitor_{username}.csv"


# Glob patterns instagram_monitor uses for downloaded media, per its docs:
#   profile pic : instagram_<username>_profile_pic*.jpg   (current/old/history)
#   post/reel   : instagram_<username>_post_*.jpg|.mp4 , instagram_<username>_reel_*.jpg|.mp4
#   story       : instagram_<username>_story_*.jpg|.mp4
IMAGE_TYPES = {"profile", "post", "story"}


def media_glob_patterns(username: str, image_type: str) -> list[str]:
    if image_type == "profile":
        # Matches instagram_<u>_profile_pic.jpg (current), _old.jpg, and
        # the YYmmdd_HHMM history variants. We pick whichever is newest
        # by mtime below, which in practice is the current one.
        return [f"instagram_{username}_profile_pic*.jpg"]
    if image_type == "post":
        return [
            f"instagram_{username}_post_*.jpg",
            f"instagram_{username}_post_*.mp4",
            f"instagram_{username}_reel_*.jpg",
            f"instagram_{username}_reel_*.mp4",
        ]
    if image_type == "story":
        return [
            f"instagram_{username}_story_*.jpg",
            f"instagram_{username}_story_*.mp4",
        ]
    return []


def find_latest_media(username: str, image_type: str) -> Path | None:
    """Find the most recently modified downloaded media file of the given type."""
    tdir = target_dir(username)
    if not tdir.is_dir():
        return None

    candidates: list[Path] = []
    for pattern in media_glob_patterns(username, image_type):
        candidates.extend(tdir.glob(pattern))

    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


def send_chunked(chat_id: int, text: str) -> None:
    """Telegram caps messages at 4096 chars; split long output safely."""
    if not text:
        return
    for i in range(0, len(text), MAX_MSG_LEN):
        bot.send_message(chat_id, text[i:i + MAX_MSG_LEN])


def build_monitor_command(username: str) -> list[str]:
    """
    Build the instagram_monitor command line for a single target.

    Deliberately ANONYMOUS-ONLY (Mode 1) - no -u/session-login flag exists
    anywhere in this function or file. This was removed after a watcher
    account got checkpointed by Instagram during Mode 2 testing; see the
    module docstring for the full reasoning. If you want Mode 2 back in
    the future, you'd need to deliberately re-add a -u flag here, not just
    set an environment variable.

    --config-file  : shared settings (interval, jitter, be-human, etc.)
                      defined once in telegram_monitor.conf.
    -b <file>.csv  : enable instagram_monitor's built-in CSV export of all
                      activity/profile changes. A relative filename + cwd=
                      tdir (set by the caller) means this lands inside
                      this target's own monitors/<username>/ folder,
                      fetchable later via /data.
    """
    cmd = ["instagram_monitor", username, "-b", csv_path_for(username).name]
    if SHARED_CONFIG.exists():
        cmd += ["--config-file", str(SHARED_CONFIG)]
    return cmd


def _broadcast(username: str, text: str) -> None:
    """Send a message to every chat currently subscribed to `username`."""
    with _targets_lock:
        info = _targets.get(username)
        chat_ids = list(info["subscribers"].keys()) if info else []
    for cid in chat_ids:
        try:
            send_chunked(cid, text)
        except Exception:
            pass


def tail_log_and_forward(username: str, log_file: Path, stop_event: threading.Event) -> None:
    """
    Background thread: wait for the log file to appear, then forward every
    line written to it to ALL currently-subscribed chats, until stop_event
    is set.

    Uses stop_event.wait() instead of time.sleep() so it wakes up
    immediately when stop_tracking() fires the event — this releases the
    open file handle before the folder-deletion step runs (fixes the
    Windows [WinError 32] locked-file error).
    """
    waited = 0.0
    while not log_file.exists() and not stop_event.is_set():
        stop_event.wait(0.5)
        waited += 0.5
        if waited >= 30:
            _broadcast(
                username,
                f"[{username}] Warning: log file not found after 30s "
                f"({log_file}). The monitor may have failed to start — "
                f"check /status.",
            )
            return

    if stop_event.is_set():
        return

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            buffer = []
            last_flush = time.monotonic()

            while not stop_event.is_set():
                line = f.readline()
                if line:
                    buffer.append(line.rstrip("\n"))
                    if len(buffer) >= 20 or (time.monotonic() - last_flush) > 1.0:
                        _broadcast(username, "\n".join(f"[{username}] {l}" for l in buffer if l.strip()))
                        try:
                            for ln in buffer:
                                if ln.strip():
                                    db.insert_log(username, ln)
                        except Exception:
                            pass
                        buffer = []
                        last_flush = time.monotonic()
                else:
                    if buffer:
                        _broadcast(username, "\n".join(f"[{username}] {l}" for l in buffer if l.strip()))
                        try:
                            for ln in buffer:
                                if ln.strip():
                                    db.insert_log(username, ln)
                        except Exception:
                            pass
                        buffer = []
                        last_flush = time.monotonic()
                    # Interruptible wait — wakes immediately on stop_event.set()
                    stop_event.wait(1.0)
    except Exception as e:
        _broadcast(username, f"[{username}] Log tailing stopped due to an error: {e}")


def all_media_glob_patterns(username: str) -> list[str]:
    """All media glob patterns across every type, for the auto-watcher."""
    patterns = []
    for image_type in IMAGE_TYPES:
        patterns.extend(media_glob_patterns(username, image_type))
    return patterns


def classify_media_type(filename: str, username: str) -> str:
    """Best-effort reverse mapping from filename back to profile/post/story."""
    if filename.startswith(f"instagram_{username}_profile_pic"):
        return "profile"
    if f"_{username}_post_" in filename or f"_{username}_reel_" in filename:
        return "post"
    if f"_{username}_story_" in filename:
        return "story"
    return "media"


def _db_media_type(filename: str, username: str) -> str:
    """Map a media filename to the database media_type enum value."""
    if f"_{username}_profile_pic" in filename:
        return "profile_pic"
    if f"_{username}_reel_" in filename:
        return "reel"
    if f"_{username}_post_" in filename:
        return "post"
    if f"_{username}_story_" in filename:
        return "story"
    return "post"


def send_media_file(chat_id: int, username: str, media_path: Path, label: str) -> None:
    """Send one media file to Telegram, respecting the 50MB bot upload cap."""
    try:
        size_mb = media_path.stat().st_size / (1024 * 1024)
    except FileNotFoundError:
        return  # file vanished/got rotated between detection and send - skip quietly

    if size_mb > 50:
        bot.send_message(
            chat_id,
            f"[{username}] New {label} file is {size_mb:.1f} MB, over Telegram's "
            f"50 MB bot upload limit. Saved on disk at {media_path}.",
        )
        return

    try:
        caption = f"[{username}] New {label}: {media_path.name}"
        with open(media_path, "rb") as f:
            if media_path.suffix.lower() == ".mp4":
                bot.send_video(chat_id, f, caption=caption)
            else:
                bot.send_photo(chat_id, f, caption=caption)
    except Exception as e:
        bot.send_message(chat_id, f"[{username}] Failed to auto-send new {label} file {media_path.name}: {e}")


def watch_media_and_forward(username: str, stop_event: threading.Event) -> None:
    """
    Background thread: poll the target's directory for new media files and
    push each one to ALL currently-subscribed chats the moment it appears.
    Pre-existing files at watch-start are recorded but NOT sent.
    """
    tdir = target_dir(username)
    seen: set[str] = set()

    # Seed with whatever's already there so startup doesn't dump every old file.
    for pattern in all_media_glob_patterns(username):
        for p in tdir.glob(pattern):
            seen.add(p.name)

    while not stop_event.is_set():
        try:
            if tdir.is_dir():
                for pattern in all_media_glob_patterns(username):
                    for p in sorted(tdir.glob(pattern), key=lambda p: p.stat().st_mtime):
                        if p.name in seen:
                            continue
                        seen.add(p.name)
                        try:
                            db.store_media(username, _db_media_type(p.name, username),
                                           p.name, p.read_bytes())
                        except Exception as e:
                            print(f"Warning: could not store {p.name} in DB: {e}")
                        label = classify_media_type(p.name, username)
                        # Fan-out: send to every subscribed chat
                        with _targets_lock:
                            info = _targets.get(username)
                            chat_ids = list(info["subscribers"].keys()) if info else []
                        for cid in chat_ids:
                            send_media_file(cid, username, p, label)
        except Exception as e:
            print(f"Warning: media watcher for @{username} hit an error: {e}")
        stop_event.wait(timeout=5.0)


def watch_process_health(username: str, process: subprocess.Popen, stop_event: threading.Event) -> None:
    """
    Background thread: poll the subprocess for unexpected exit and notify
    Telegram immediately, including exit code and any captured stderr -
    instead of silently leaving the target "stopped" until someone happens
    to run /status.
    """
    try:
        print(f"[watchdog] started for @{username} (pid={process.pid})")
        while True:
            if stop_event.wait(timeout=3.0):
                print(f"[watchdog] @{username} stopping cleanly (stop_event set)")
                return  # stop_tracking() called - this is an intentional stop, stay quiet
            exit_code = process.poll()
            if exit_code is not None:
                print(f"[watchdog] @{username} detected exit, code={exit_code}")
                break

        stderr_file = stderr_path_for(username)
        stderr_text = ""
        try:
            if stderr_file.exists():
                stderr_text = stderr_file.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:
            print(f"Warning: could not read stderr capture for @{username}: {e}")

        message = f"🔴 [{username}] instagram_monitor stopped unexpectedly (exit code {exit_code})."
        if stderr_text:
            # Keep just the tail - a traceback's last lines are the useful part,
            # and this avoids hitting Telegram's message-length limit.
            tail = stderr_text[-1500:]
            message += f"\n\nLast captured output (stderr):\n{tail}"
        else:
            message += "\n\n(No error output was captured.)"
        message += f"\n\nRun /track or /trackother to restart tracking for @{username}."

        # Notify all subscribed chats about the crash
        _broadcast(username, message)

        # Mark the target as no-longer-active so /status reflects reality
        # immediately rather than waiting for the next manual check.
        with _targets_lock:
            info = _targets.get(username)
            if info and info["process"] is process:
                stderr_handle = info.get("stderr_file_handle")
                if stderr_handle:
                    try:
                        stderr_handle.close()
                    except Exception:
                        pass
                del _targets[username]
                try:
                    db.deactivate_target(username)
                    db.insert_event(username, "crashed", exit_code,
                                    stderr_text[:500] if stderr_text else None)
                except Exception:
                    pass
    except Exception as e:
        print(f"CRITICAL: watch_process_health for @{username} crashed: {e}")
        _broadcast(
            username,
            f"⚠️ [{username}] The crash-watcher itself hit an internal error "
            f"({e}). @{username}'s tracking status may be stale — check /status manually.",
        )


def start_tracking(username: str, chat_id: int, started_by: str) -> str:
    """
    Start instagram_monitor for `username`, or subscribe an additional chat
    to an already-running monitor.  Returns a status message.

    One instagram_monitor process runs per username.  Multiple chats can all
    subscribe to the same process — every log line and new media file is
    forwarded to every subscriber automatically.
    """
    # ── 1. Quick check under lock ──────────────────────────────────────────
    with _targets_lock:
        existing = _targets.get(username)
        if existing and existing["process"].poll() is None:
            if chat_id in existing["subscribers"]:
                return f"⚠️ Already tracking @{username} in this chat."
            # Process is already running — just subscribe this chat to its feed.
            existing["subscribers"][chat_id] = started_by
            db.upsert_target(username, chat_id, started_by)
            return (
                f"✅ @{username} is already being monitored. "
                f"This chat has been added to its feed — you'll receive all future "
                f"log lines and media from now on."
            )

    # ── 2. Slow work WITHOUT holding the lock ──────────────────────────────
    tdir = target_dir(username)
    tdir.mkdir(parents=True, exist_ok=True)

    old_log = log_path_for(username)
    old_stderr = stderr_path_for(username)
    for stale_file in (old_log, old_stderr):
        try:
            stale_file.unlink(missing_ok=True)
        except Exception as e:
            print(f"Warning: could not remove stale file {stale_file}: {e}")

    cmd = build_monitor_command(username)

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    if IS_WINDOWS:
        child_env["PYTHONLEGACYWINDOWSSTDIO"] = "1"

    try:
        stderr_file = open(stderr_path_for(username), "wb")
        popen_kwargs = dict(
            cwd=str(tdir),
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError:
        return (
            "❌ 'instagram_monitor' is not installed or not on PATH.\n"
            "Install it with: pip install instagram_monitor"
        )
    except Exception as e:
        return f"❌ Failed to start monitor for @{username}: {e}"

    stop_event = threading.Event()
    log_file = log_path_for(username)
    log_thread = threading.Thread(
        target=tail_log_and_forward,
        args=(username, log_file, stop_event),
        daemon=True,
    )
    log_thread.start()

    media_thread = threading.Thread(
        target=watch_media_and_forward,
        args=(username, stop_event),
        daemon=True,
    )
    media_thread.start()

    watchdog_thread = threading.Thread(
        target=watch_process_health,
        args=(username, process, stop_event),
        daemon=True,
    )
    watchdog_thread.start()

    # ── 3. Final write under lock — handle race where two users started the ──
    # same username simultaneously between steps 1 and 3.
    with _targets_lock:
        existing = _targets.get(username)
        if existing and existing["process"].poll() is None:
            # Another thread won the race — subscribe this chat and discard ours.
            stop_event.set()
            try:
                process.kill()
            except Exception:
                pass
            try:
                stderr_file.close()
            except Exception:
                pass
            if chat_id not in existing["subscribers"]:
                existing["subscribers"][chat_id] = started_by
            return (
                f"✅ @{username} is already being monitored. "
                f"This chat has been added to its feed."
            )

        _targets[username] = {
            "process": process,
            "thread": log_thread,
            "media_thread": media_thread,
            "watchdog_thread": watchdog_thread,
            "stop_event": stop_event,
            "log_path": log_file,
            "stderr_file_handle": stderr_file,
            "started_at": datetime.now(timezone.utc),
            "subscribers": {chat_id: started_by},  # chat_id → started_by
        }

    db.upsert_target(username, chat_id, started_by)
    db.insert_event(username, "started")

    return (
        f"🚀 Started tracking @{username}. New log lines and downloaded "
        f"images/videos will appear here automatically."
    )


def delete_target_folder(username: str) -> str | None:
    """
    Delete monitors/<username>/ entirely (logs, CSV, JSON, downloaded
    media - everything for that target). Returns an error string if
    deletion failed/partially failed, or None on success/nothing to delete.

    Called from stop_tracking() only, after the subprocess is confirmed
    dead - never while instagram_monitor might still have files open
    (e.g. on Windows, an open file handle can block deletion).
    """
    tdir = target_dir(username)
    if not tdir.exists():
        return None
    try:
        shutil.rmtree(tdir)
        return None
    except Exception as e:
        return str(e)


def stop_tracking(username: str, chat_id: int) -> str:
    """
    Unsubscribe `chat_id` from `username`'s feed.
    The process is only killed when the LAST subscriber stops.
    """
    with _targets_lock:
        info = _targets.get(username)

        # ── process already dead or never started ──────────────────────────
        if not info or info["process"].poll() is not None:
            if username in _targets:
                stderr_handle = info.get("stderr_file_handle")
                if stderr_handle:
                    try:
                        stderr_handle.close()
                    except Exception:
                        pass
                del _targets[username]
                db.delete_target(username)
            delete_error = delete_target_folder(username)
            if delete_error:
                return (
                    f"⚠️ @{username} was not being tracked, but its data folder "
                    f"could not be fully deleted: {delete_error}"
                )
            return f"⚠️ @{username} is not currently being tracked. Its data folder (if any) has been removed."

        # ── remove this subscriber ─────────────────────────────────────────
        info["subscribers"].pop(chat_id, None)

        if info["subscribers"]:
            # Other chats are still subscribed — just unsubscribe this one.
            remaining = len(info["subscribers"])
            return (
                f"🛑 Stopped updates for @{username} in this chat. "
                f"{remaining} other chat{'s' if remaining != 1 else ''} still tracking."
            )

        # ── last subscriber — shut down the process ────────────────────────
        info["stop_event"].set()
        process = info["process"]
        threads = [
            info.get("thread"),
            info.get("media_thread"),
            info.get("watchdog_thread"),
        ]
        stderr_handle = info.get("stderr_file_handle")
        del _targets[username]
        db.delete_target(username)

    # Kill process outside lock
    try:
        if IS_WINDOWS:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=10)
    except Exception:
        try:
            if IS_WINDOWS:
                process.kill()
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            pass

    if stderr_handle:
        try:
            stderr_handle.close()
        except Exception:
            pass

    # Join background threads so they release all open file handles (log,
    # CSV) before we try to delete the folder.  This is the proper fix for
    # the Windows [WinError 32] locked-file error — stop_event.wait() in the
    # tailer means the threads exit within milliseconds of the event being set.
    for t in threads:
        if t and t.is_alive():
            t.join(timeout=5.0)

    delete_error = delete_target_folder(username)
    if delete_error:
        return (
            f"🛑 Stopped tracking @{username}.\n"
            f"⚠️ Could not fully delete its data folder: {delete_error}\n"
            f"You may need to delete monitors/{username}/ manually."
        )
    return f"🛑 Stopped tracking @{username} and deleted its data folder."


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    _awaiting_username[awaiting_key(message)] = False
    welcome_text = (
        "👋 Instagram Monitor Bot\n\n"
        "Commands:\n"
        "/track - track the pre-configured Instagram user\n"
        "/trackother - track a different user (bot will ask for the username)\n"
        "/image <username> <profile|post|story> - send the latest downloaded image/video on demand\n"
        "/data <username> - send the full log + CSV activity export\n"
        "/status - list currently tracked users\n"
        "/stop <username> - stop tracking a user AND DELETE all its saved data\n"
        "/cancel - cancel a pending /trackother prompt\n\n"
        "All monitor log output is forwarded here as plain text, prefixed "
        "with [username]. New profile pics, posts/reels, and stories are "
        "also pushed here automatically as soon as they're downloaded.\n\n"
        "⚠️ /stop permanently deletes that username's saved logs, CSV, and "
        "media. Run /data first if you want to keep a copy."
    )
    bot.reply_to(message, welcome_text, reply_markup=main_reply_keyboard())


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    key = awaiting_key(message)
    was_awaiting = _awaiting_username.get(key, False)
    _awaiting_username[key] = False
    bot.reply_to(message, "Cancelled." if was_awaiting else "Nothing to cancel.")


@bot.message_handler(commands=["track"])
def cmd_track(message):
    _awaiting_username[awaiting_key(message)] = False
    if not DEFAULT_USERNAME:
        bot.reply_to(message, "❌ No INSTAGRAM_USERNAME configured in your .env file.")
        return
    result = start_tracking(DEFAULT_USERNAME, message.chat.id, describe_sender(message))
    bot.reply_to(message, result)


@bot.message_handler(commands=["trackother"])
def cmd_trackother(message):
    _awaiting_username[awaiting_key(message)] = True
    prompt = "Send me the Instagram username you want to track (just the username, no @)."
    if message.chat.type != "private":
        prompt += "\n\n(In a group, please reply directly to THIS message — tap it and hit Reply — or I won't see your answer.)"
    bot.reply_to(
        message,
        prompt,
        reply_markup=telebot.types.ForceReply(selective=True),
    )


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    _awaiting_username[awaiting_key(message)] = False
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        kb = username_picker_keyboard("stop")
        if kb is None:
            bot.reply_to(message, "🟡 Nothing is currently being tracked, so there's nothing to stop.")
            return
        bot.reply_to(
            message,
            "Which username do you want to stop tracking?\n"
            "⚠️ This deletes all its saved data (logs, CSV, media). "
            "Use /data first if you want a copy.",
            reply_markup=kb,
        )
        return
    username = parts[1].strip().lstrip("@")
    if not is_valid_username(username):
        bot.reply_to(message, "❌ That doesn't look like a valid Instagram username.")
        return
    bot.reply_to(message, stop_tracking(username, message.chat.id))


def deliver_image(chat_id: int, username: str, image_type: str) -> str | None:
    """
    Send the latest media of image_type for username to chat_id.
    Serves from the database BLOB first; falls back to disk if not yet stored.
    Returns an error/status string on failure, or None on success.

    Shared between cmd_image (typed command) and the img: callback button.
    """
    # Try database first
    result = db.get_latest_media(username, image_type)
    if result is not None:
        filename, data = result
        size_mb = len(data) / (1024 * 1024)
        if size_mb > 50:
            return (
                f"❌ {filename} is {size_mb:.1f} MB, over Telegram's 50 MB "
                f"bot upload limit."
            )
        try:
            caption = f"[{username}] {image_type} - {filename}"
            file_obj = io.BytesIO(data)
            file_obj.name = filename
            if filename.lower().endswith(".mp4"):
                bot.send_video(chat_id, file_obj, caption=caption)
            else:
                bot.send_photo(chat_id, file_obj, caption=caption)
            return None
        except Exception as e:
            return f"❌ Failed to send media for @{username}: {e}"

    # Fall back to disk (e.g. file downloaded but DB write failed)
    media_path = find_latest_media(username, image_type)
    if media_path is None:
        return (
            f"⚠️ No {image_type} image found yet for @{username}. "
            f"The monitor needs to run at least one check cycle first."
        )

    size_mb = media_path.stat().st_size / (1024 * 1024)
    if size_mb > 50:
        return (
            f"❌ {media_path.name} is {size_mb:.1f} MB, over Telegram's 50 MB "
            f"bot upload limit. It's saved on disk at {media_path}, but I can't "
            f"send it here."
        )

    try:
        caption = f"[{username}] {image_type} - {media_path.name}"
        with open(media_path, "rb") as f:
            if media_path.suffix.lower() == ".mp4":
                bot.send_video(chat_id, f, caption=caption)
            else:
                bot.send_photo(chat_id, f, caption=caption)
        return None
    except Exception as e:
        return f"❌ Failed to send media for @{username}: {e}"


@bot.message_handler(commands=["image"])
def cmd_image(message):
    _awaiting_username[awaiting_key(message)] = False
    parts = message.text.split()

    if len(parts) == 1:
        # No username given at all - offer the picker.
        kb = username_picker_keyboard("imgsel")
        if kb is None:
            bot.reply_to(message, "🟡 Nothing is currently being tracked yet.")
            return
        bot.reply_to(message, "Which username?", reply_markup=kb)
        return

    if len(parts) == 2:
        # Username given, type missing - offer the type picker.
        username = parts[1].lstrip("@")
        if not is_valid_username(username):
            bot.reply_to(message, "❌ That doesn't look like a valid Instagram username.")
            return
        bot.reply_to(message, f"Which image type for @{username}?", reply_markup=image_type_keyboard(username))
        return

    if len(parts) != 3:
        bot.reply_to(
            message,
            "Usage: /image <username> <profile|post|story>\n"
            "Example: /image natgeo profile",
        )
        return

    _, username, image_type = parts
    username = username.lstrip("@")
    image_type = image_type.lower()

    if not is_valid_username(username):
        bot.reply_to(message, "❌ That doesn't look like a valid Instagram username.")
        return

    if image_type not in IMAGE_TYPES:
        bot.reply_to(message, f"❌ Unknown type '{image_type}'. Use one of: profile, post, story.")
        return

    error = deliver_image(message.chat.id, username, image_type)
    if error:
        bot.reply_to(message, error)


def data_files_for(username: str) -> list[Path]:
    """
    Find the non-media data files worth sending via /data: the log file,
    the CSV activity export, and (always empty under this bot's
    anonymous-only Mode 1, but harmless to keep checking) the
    follower/following JSON snapshots that only exist in Mode 2.
    """
    tdir = target_dir(username)
    if not tdir.is_dir():
        return []

    candidates = [
        log_path_for(username),
        csv_path_for(username),
        tdir / f"instagram_{username}_followers.json",
        tdir / f"instagram_{username}_followings.json",
    ]
    return [p for p in candidates if p.exists() and p.stat().st_size > 0]


def deliver_data(chat_id: int, username: str) -> str | None:
    """
    Export activity log (CSV) and monitor log (text) from the database and
    send them to chat_id.  Syncs the on-disk CSV into the DB first so the
    export always reflects the latest instagram_monitor output.
    Falls back to raw disk files if the DB has no data yet.

    Shared between cmd_data (typed command) and the data: callback button.
    """
    # Sync any new CSV rows that instagram_monitor wrote since last call
    db.import_csv(username, csv_path_for(username))

    csv_bytes = db.export_activity_csv(username)
    log_bytes = db.export_log_text(username)

    sent_any = False

    # More than just the header line means there is real activity data
    if csv_bytes and csv_bytes.count(b"\n") > 1:
        try:
            f = io.BytesIO(csv_bytes)
            f.name = f"instagram_{username}_activity.csv"
            bot.send_document(chat_id, f, caption=f"[{username}] Activity Log (CSV)")
            sent_any = True
        except Exception as e:
            bot.send_message(chat_id, f"❌ Failed to send activity CSV: {e}")

    if log_bytes:
        try:
            f = io.BytesIO(log_bytes)
            f.name = f"instagram_{username}_monitor.log"
            bot.send_document(chat_id, f, caption=f"[{username}] Monitor Log")
            sent_any = True
        except Exception as e:
            bot.send_message(chat_id, f"❌ Failed to send monitor log: {e}")

    if sent_any:
        return None

    # Fall back to disk files (first run before any DB data exists)
    files = data_files_for(username)
    if not files:
        return (
            f"⚠️ No data found yet for @{username}. The monitor needs "
            f"to run at least one check cycle first."
        )
    for path in files:
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > 50:
            bot.send_message(
                chat_id,
                f"❌ {path.name} is {size_mb:.1f} MB, over Telegram's 50 MB "
                f"bot upload limit. It's saved on disk at {path}.",
            )
            continue
        try:
            with open(path, "rb") as f:
                bot.send_document(chat_id, f, caption=f"[{username}] {path.name}")
            sent_any = True
        except Exception as e:
            bot.send_message(chat_id, f"❌ Failed to send {path.name}: {e}")

    if not sent_any:
        return f"❌ Found data files for @{username} but none could be sent."
    return None


@bot.message_handler(commands=["data"])
def cmd_data(message):
    _awaiting_username[awaiting_key(message)] = False
    parts = message.text.split()
    if len(parts) != 2:
        kb = username_picker_keyboard("data")
        if kb is None:
            bot.reply_to(message, "🟡 Nothing is currently being tracked yet.")
            return
        bot.reply_to(message, "Which username's data do you want?", reply_markup=kb)
        return

    username = parts[1].lstrip("@")
    if not is_valid_username(username):
        bot.reply_to(message, "❌ That doesn't look like a valid Instagram username.")
        return

    error = deliver_data(message.chat.id, username)
    if error:
        bot.reply_to(message, error)


@bot.message_handler(commands=["status"])
def cmd_status(message):
    _awaiting_username[awaiting_key(message)] = False
    with _targets_lock:
        if not _targets:
            bot.reply_to(message, "🟡 No targets are currently being tracked.")
            return
        lines = ["Currently tracked:"]
        for username, info in _targets.items():
            alive = info["process"].poll() is None
            icon = "🟢" if alive else "🔴"
            state = "running" if alive else "stopped (process exited — check /track to resume)"
            started = info["started_at"].strftime("%Y-%m-%d %H:%M UTC")
            subs = info.get("subscribers", {})
            sub_list = ", ".join(str(v) for v in subs.values()) if subs else "unknown"
            lines.append(
                f"{icon} @{username} — {state} — since {started}\n"
                f"   👥 {len(subs)} subscriber(s): {sub_list}"
            )
    bot.reply_to(message, "\n".join(lines))


@bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    """
    Handles every inline-keyboard button press: the stop:/imgsel:/img:/data:
    callback_data schemes set up in username_picker_keyboard() and
    image_type_keyboard(). Every branch calls answer_callback_query exactly
    once, per https://core.telegram.org/bots/api - skipping this leaves the
    user's tap stuck on a loading spinner.
    """
    data = call.data or ""
    chat_id = call.message.chat.id if call.message else None

    try:
        if data.startswith("stop:"):
            username = data[len("stop:"):]
            bot.answer_callback_query(call.id, f"Stopping @{username}...")
            result = stop_tracking(username, chat_id) if chat_id is not None else "❌ Lost chat context."
            if chat_id is not None:
                bot.send_message(chat_id, result)

        elif data.startswith("imgsel:"):
            # Username chosen for /image, but type not yet - show the type picker.
            username = data[len("imgsel:"):]
            bot.answer_callback_query(call.id)
            if chat_id is not None:
                bot.send_message(chat_id, f"Which image type for @{username}?", reply_markup=image_type_keyboard(username))

        elif data.startswith("img:"):
            # img:<username>:<type>
            rest = data[len("img:"):]
            username, _, image_type = rest.partition(":")
            bot.answer_callback_query(call.id, f"Fetching {image_type} for @{username}...")
            error = deliver_image(chat_id, username, image_type) if chat_id is not None else "❌ Lost chat context."
            if error and chat_id is not None:
                bot.send_message(chat_id, error)

        elif data.startswith("data:"):
            username = data[len("data:"):]
            bot.answer_callback_query(call.id, f"Sending data for @{username}...")
            error = deliver_data(chat_id, username) if chat_id is not None else "❌ Lost chat context."
            if error and chat_id is not None:
                bot.send_message(chat_id, error)

        else:
            # Unknown/stale callback data (e.g. from a button in a very old
            # message) - acknowledge so the spinner clears, but do nothing.
            bot.answer_callback_query(call.id, "This button is no longer valid.")
    except Exception as e:
        # Always acknowledge, even on internal errors, so the user isn't
        # left staring at a stuck loading spinner.
        try:
            bot.answer_callback_query(call.id, f"Something went wrong: {e}")
        except Exception:
            pass


@bot.message_handler(func=lambda m: _awaiting_username.get(awaiting_key(m), False), content_types=["text"])
def handle_username_reply(message):
    _awaiting_username[awaiting_key(message)] = False
    username = message.text.strip().lstrip("@")

    if not is_valid_username(username):
        bot.reply_to(
            message,
            "❌ Invalid username. Instagram usernames are letters, digits, "
            "periods, and underscores only (max 30 chars). Try /trackother again.",
        )
        return

    result = start_tracking(username, message.chat.id, describe_sender(message))
    bot.reply_to(message, result)


# --------------------------------------------------------------------------
# Startup: generate telegram_monitor.conf on the (possibly mounted, possibly
# persistent) DATA_DIR if it isn't there yet. This lets a PaaS deploy with
# no shell access still end up with a working config, sourced from env vars
# instead of a committed file. If the file already exists (e.g. someone put
# it on a volume by hand, or a previous boot already created it), we leave
# it untouched - this never overwrites manual edits.
# --------------------------------------------------------------------------

def ensure_shared_config() -> None:
    if SHARED_CONFIG.exists():
        return

    check_interval = os.getenv("INSTA_CHECK_INTERVAL", "10800")  # 3 hours default
    enable_jitter = os.getenv("ENABLE_JITTER", "True")
    be_human = os.getenv("BE_HUMAN", "True")
    webhook_url = os.getenv("WEBHOOK_URL", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")

    lines = [
        "# Auto-generated on first boot from environment variables.",
        "# Safe to hand-edit afterwards - this file is never overwritten",
        "# once it exists. Do not commit this file to git.",
        f"INSTA_CHECK_INTERVAL = {check_interval}",
        f"ENABLE_JITTER = {enable_jitter}",
        f"BE_HUMAN = {be_human}",
    ]
    if webhook_url:
        lines += ["WEBHOOK_ENABLED = True", f'WEBHOOK_URL = "{webhook_url}"']
    if smtp_password:
        lines += [f'SMTP_PASSWORD = "{smtp_password}"']

    SHARED_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Generated {SHARED_CONFIG} from environment variables.")


def migrate_existing_data() -> None:
    """
    One-time import of flat-file data from monitors/ into the database.
    Safe to call on every startup:
      - CSV uses INSERT OR IGNORE on (username, occurred_at, change_type)
      - Log import is skipped if entries already exist for the username
      - Media uses INSERT OR IGNORE on (username, filename)
    """
    if not MONITORS_DIR.is_dir():
        return

    # Read old JSON state so we can restore chat_id / started_by
    json_state: dict = {}
    if STATE_FILE.exists():
        try:
            json_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    for username_dir in MONITORS_DIR.iterdir():
        if not username_dir.is_dir():
            continue
        username = username_dir.name
        if not is_valid_username(username):
            continue

        # Target row must exist before child tables can reference it (FK)
        state = json_state.get(username, {})
        db.upsert_target(
            username,
            state.get("chat_id", 0),
            state.get("started_by", "migrated-from-disk"),
        )

        n_csv   = db.import_csv(username, csv_path_for(username))
        n_log   = db.import_log_file(username, log_path_for(username))
        n_media = sum(
            db.import_media_file(username, p, _db_media_type(p.name, username))
            for pattern in all_media_glob_patterns(username)
            for p in username_dir.glob(pattern)
        )
        if n_csv or n_log or n_media:
            print(
                f"Migrated @{username}: {n_csv} activity rows, "
                f"{n_log} log lines, {n_media} media files into DB"
            )


def resume_persisted_targets() -> None:
    """
    Re-launch instagram_monitor for every username that was being tracked
    before the last restart/redeploy, sending a heads-up to each chat that
    had something resumed. Lets the bot survive Railway-style ephemeral
    container restarts without silently dropping all tracking.
    """
    saved = load_persisted_state()
    if not saved:
        return

    print(f"Resuming {len(saved)} previously-tracked username(s) from {STATE_FILE}...")
    for username, info in saved.items():
        if not is_valid_username(username):
            continue  # defensively skip anything that got corrupted on disk
        chat_id = info.get("chat_id")
        started_by = info.get("started_by", "someone")
        if chat_id is None:
            continue
        result = start_tracking(username, chat_id, started_by)
        try:
            bot.send_message(
                chat_id,
                f"🔄 Resumed tracking @{username} after a restart.\n{result}",
            )
        except Exception as e:
            print(f"Could not notify chat {chat_id} about resumed @{username}: {e}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def register_bot_commands() -> None:
    """
    Registers the command list with Telegram so the native menu button
    (tap the icon next to the message field) and the "/" autocomplete
    list both show these, with descriptions - per
    https://core.telegram.org/bots/features#menu-button and #commands.
    This is independent of the persistent reply keyboard sent on /start;
    both are real Telegram UI features serving slightly different needs
    (the menu button works even before /start has been sent once).
    """
    commands = [
        telebot.types.BotCommand("start", "Show usage and the main keyboard"),
        telebot.types.BotCommand("help", "Show usage and the main keyboard"),
        telebot.types.BotCommand("track", "Track the pre-configured Instagram user"),
        telebot.types.BotCommand("trackother", "Track a different Instagram user"),
        telebot.types.BotCommand("image", "Send a downloaded image/video on demand"),
        telebot.types.BotCommand("data", "Send the log + CSV for a tracked user"),
        telebot.types.BotCommand("status", "List currently tracked users"),
        telebot.types.BotCommand("stop", "Stop tracking a user and delete its data"),
        telebot.types.BotCommand("cancel", "Cancel a pending /trackother prompt"),
    ]
    try:
        bot.set_my_commands(commands)
    except Exception as e:
        print(f"Warning: could not register bot commands with Telegram: {e}")


if __name__ == "__main__":
    MONITORS_DIR.mkdir(parents=True, exist_ok=True)
    db.init(DATA_DIR / "bot.db")
    migrate_existing_data()
    ensure_shared_config()
    register_bot_commands()
    resume_persisted_targets()
    print("Telegram bot is running...")
    bot.infinity_polling()