"""
Shared fixtures and environment bootstrap for the Telegram bot test suite.

Environment variables must be set at module-load time, before any test file
triggers `import main` (which reads them at the top level).
"""

import os
import tempfile
from unittest.mock import MagicMock

# ── bootstrap env before main.py is imported ──────────────────────────────────
_TEMP_BASE = tempfile.mkdtemp(prefix="tgbot_test_")
os.environ["API_KEY"] = "123456789:AABBCCDDEEFFaabbccddeeff1234567890AB"
os.environ.setdefault("INSTAGRAM_USERNAME", "default_test_user")
os.environ["DATA_DIR"] = _TEMP_BASE

import pytest  # noqa: E402 (must be after env setup)


# ── message / callback factories ─────────────────────────────────────────────

def make_message(
    text="/start",
    chat_id=100,
    user_id=200,
    tg_username="testuser",
    first_name="Test",
    chat_type="private",
    group_title="Test Group",
):
    """Build a minimal fake Telegram Message object."""
    msg = MagicMock()
    msg.text = text
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.chat.title = group_title
    msg.from_user.id = user_id
    msg.from_user.username = tg_username
    msg.from_user.first_name = first_name
    return msg


def make_callback(data, chat_id=100):
    """Build a minimal fake Telegram CallbackQuery object."""
    call = MagicMock()
    call.data = data
    call.id = "cq_test_123"
    call.message.chat.id = chat_id
    return call


# ── core autouse fixture ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """
    Before each test
    ----------------
    * Close any existing DB connection and open a fresh one in *tmp_path*.
    * Redirect all main.py path constants to *tmp_path*.
    * Clear _targets and _awaiting_username.

    After each test
    ---------------
    * Signal and discard any leftover tracked targets.
    * Close the DB connection.
    """
    import database as db
    import main

    # --- set up fresh DB ---
    if db._conn is not None:
        try:
            db._conn.close()
        except Exception:
            pass
        db._conn = None

    db.init(tmp_path / "test.db")

    # --- redirect filesystem paths ---
    monitors_dir = tmp_path / "monitors"
    monitors_dir.mkdir()
    monkeypatch.setattr(main, "MONITORS_DIR", monitors_dir)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "STATE_FILE", tmp_path / "active_targets.json")
    monkeypatch.setattr(main, "SHARED_CONFIG", tmp_path / "telegram_monitor.conf")

    # --- clear mutable state ---
    with main._targets_lock:
        main._targets.clear()
    main._awaiting_username.clear()

    yield

    # --- teardown: kill leftover subprocesses / threads ---
    with main._targets_lock:
        remaining = list(main._targets.items())
    for uname, info in remaining:
        try:
            info["stop_event"].set()
        except Exception:
            pass
        try:
            info["process"].kill()
        except Exception:
            pass
    with main._targets_lock:
        main._targets.clear()

    # --- close DB ---
    if db._conn is not None:
        try:
            db._conn.close()
        except Exception:
            pass
        db._conn = None


# ── reusable fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def mock_bot(monkeypatch):
    """Replace main.bot with a MagicMock so no real Telegram calls are made."""
    import main
    m = MagicMock()
    monkeypatch.setattr(main, "bot", m)
    return m


@pytest.fixture
def mock_popen(monkeypatch):
    """
    Replace subprocess.Popen with a factory that returns a fake live process.
    Returns (popen_class_mock, process_mock).
    """
    import main
    proc = MagicMock()
    proc.pid = 99_999
    proc.poll.return_value = None   # alive
    proc.wait.return_value = 0
    popen_cls = MagicMock(return_value=proc)
    monkeypatch.setattr(main.subprocess, "Popen", popen_cls)
    return popen_cls, proc


@pytest.fixture
def no_threads(monkeypatch):
    """
    Replace threading.Thread with a stub that records calls but never starts
    real threads.  Returns the list of stub instances created by start_tracking.
    """
    import main
    created = []

    class _Stub:
        def __init__(self, target=None, args=(), kwargs=None, daemon=False, name=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            created.append(self)

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(main.threading, "Thread", _Stub)
    return created
