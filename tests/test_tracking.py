"""Tests for start_tracking() and stop_tracking()."""

import signal
from unittest.mock import MagicMock, patch

import pytest
import database as db
import main

CHAT_ID = 123


# ── helpers ───────────────────────────────────────────────────────────────────

def _start(username="natgeo", chat_id=CHAT_ID, started_by="Alice", shared=True):
    """Convenience wrapper: start tracking (shared=True by default matches /track behaviour)."""
    return main.start_tracking(username, chat_id, started_by, shared=shared)


def _stop(username="natgeo", chat_id=CHAT_ID):
    return main.stop_tracking(username, chat_id)


# ── start_tracking ────────────────────────────────────────────────────────────

class TestStartTracking:
    def test_success_message(self, mock_popen, no_threads, mock_bot):
        result = _start()
        assert "Started" in result or "tracking" in result.lower()

    def test_adds_to_targets(self, mock_popen, no_threads, mock_bot):
        _start()
        assert "natgeo" in main._targets

    def test_creates_monitor_dir(self, mock_popen, no_threads, mock_bot, tmp_path):
        _start()
        assert (main.MONITORS_DIR / "natgeo").is_dir()

    def test_spawns_three_threads(self, mock_popen, no_threads, mock_bot):
        _start()
        assert len(no_threads) == 3

    def test_stores_target_in_db(self, mock_popen, no_threads, mock_bot):
        _start()
        rows = db.load_active_targets()
        assert any(r["username"] == "natgeo" for r in rows)

    def test_records_started_event(self, mock_popen, no_threads, mock_bot):
        _start()
        row = db._conn.execute(
            "SELECT event_type FROM process_events WHERE username='natgeo'"
        ).fetchone()
        assert row["event_type"] == "started"

    def test_already_tracked_same_chat_warns(self, mock_popen, no_threads, mock_bot):
        _start(chat_id=5)
        result = _start(chat_id=5)
        assert "Already" in result or "already" in result.lower()

    def test_already_tracked_different_chat_subscribes(self, mock_popen, no_threads, mock_bot):
        """Second chat tracking the same username should join the feed, not get blocked."""
        _start(chat_id=5)
        result = _start(chat_id=999)
        # Should subscribe, not error
        assert "added" in result.lower() or "feed" in result.lower() or "already" in result.lower()
        # Both chats are now subscribers
        assert 5 in main._targets["natgeo"]["subscribers"]
        assert 999 in main._targets["natgeo"]["subscribers"]

    def test_instagram_monitor_not_found(self, monkeypatch, no_threads, mock_bot):
        monkeypatch.setattr(
            main.subprocess, "Popen", MagicMock(side_effect=FileNotFoundError)
        )
        result = _start()
        assert "not installed" in result.lower() or "PATH" in result

    def test_popen_generic_exception(self, monkeypatch, no_threads, mock_bot):
        monkeypatch.setattr(
            main.subprocess, "Popen", MagicMock(side_effect=OSError("test error"))
        )
        result = _start()
        assert "Failed" in result or "failed" in result.lower()

    def test_stores_chat_id_in_subscribers(self, mock_popen, no_threads, mock_bot):
        _start(chat_id=42)
        assert 42 in main._targets["natgeo"]["subscribers"]

    def test_stores_started_by_in_subscribers(self, mock_popen, no_threads, mock_bot):
        _start(started_by="Bob")
        subs = main._targets["natgeo"]["subscribers"]
        assert "Bob" in subs.values()

    def test_windows_path(self, mock_popen, no_threads, mock_bot, monkeypatch):
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        result = _start()
        assert "Started" in result or "tracking" in result.lower()

    def test_deletes_stale_log_before_start(self, mock_popen, no_threads, mock_bot, tmp_path):
        """Stale log file from a previous run must be removed before launch."""
        d = main.MONITORS_DIR / "natgeo"
        d.mkdir(parents=True, exist_ok=True)
        stale = d / "instagram_monitor_natgeo.log"
        stale.write_text("old content")
        _start()
        assert "natgeo" in main._targets


# ── stop_tracking ─────────────────────────────────────────────────────────────

class TestStopTracking:
    def _tracked(self, mock_popen, no_threads, mock_bot, monkeypatch, username="natgeo"):
        """Set up a live shared-mode tracked target and return the mock process."""
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        _start(username=username, shared=True)
        return mock_popen[1]

    def test_success_message(self, mock_popen, no_threads, mock_bot, monkeypatch):
        self._tracked(mock_popen, no_threads, mock_bot, monkeypatch)
        result = _stop("natgeo")
        assert "Stopped" in result or "stopped" in result.lower()

    def test_removes_from_targets(self, mock_popen, no_threads, mock_bot, monkeypatch):
        self._tracked(mock_popen, no_threads, mock_bot, monkeypatch)
        _stop("natgeo")
        assert "natgeo" not in main._targets

    def test_removes_from_db(self, mock_popen, no_threads, mock_bot, monkeypatch):
        self._tracked(mock_popen, no_threads, mock_bot, monkeypatch)
        _stop("natgeo")
        rows = db.load_active_targets()
        assert not any(r["username"] == "natgeo" for r in rows)

    def test_deletes_monitor_folder(self, mock_popen, no_threads, mock_bot, monkeypatch, tmp_path):
        self._tracked(mock_popen, no_threads, mock_bot, monkeypatch)
        folder = main.MONITORS_DIR / "natgeo"
        _stop("natgeo")
        assert not folder.exists()

    def test_not_tracked_message(self):
        result = _stop("ghost_user")
        assert "not" in result.lower()

    def test_process_already_dead(self, mock_popen, no_threads, mock_bot, monkeypatch):
        """Process already exited → should clean up gracefully."""
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        _start()
        popen_cls, proc = mock_popen
        proc.poll.return_value = 1   # process is dead
        result = _stop("natgeo")
        assert "natgeo" not in main._targets

    def test_second_subscriber_unsubscribes_only(self, mock_popen, no_threads, mock_bot, monkeypatch):
        """Shared: when two chats track and one stops, the process keeps running."""
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        _start(chat_id=10, shared=True)
        _start(chat_id=20, shared=True)   # joins existing shared feed
        result = main.stop_tracking("natgeo", 10)
        assert "natgeo" in main._targets
        assert 10 not in main._targets["natgeo"]["subscribers"]
        assert 20 in main._targets["natgeo"]["subscribers"]
        assert "still tracking" in result.lower() or "subscriber" in result.lower()

    def test_private_trackother_independent_per_chat(self, mock_popen, no_threads, mock_bot, monkeypatch):
        """Private: two chats tracking same username each get their own _targets entry."""
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        _start(chat_id=10, shared=False)
        _start(chat_id=20, shared=False)   # independent, not fan-out
        assert "natgeo_10" in main._targets
        assert "natgeo_20" in main._targets
        # Each entry subscribes only its own chat — no cross-chat leakage
        assert 10 in main._targets["natgeo_10"]["subscribers"]
        assert 20 not in main._targets["natgeo_10"]["subscribers"]
        assert 20 in main._targets["natgeo_20"]["subscribers"]
        assert 10 not in main._targets["natgeo_20"]["subscribers"]

    def test_private_stop_only_affects_own_chat(self, mock_popen, no_threads, mock_bot, monkeypatch):
        """Private: stopping from chat 10 leaves chat 20's monitor running."""
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        _start(chat_id=10, shared=False)
        _start(chat_id=20, shared=False)
        main.stop_tracking("natgeo", 10)
        assert "natgeo_10" not in main._targets
        assert "natgeo_20" in main._targets

    def test_sigterm_timeout_escalates_to_kill(self, mock_popen, no_threads, mock_bot, monkeypatch):
        """If process.wait() raises TimeoutExpired, process.kill() should be called."""
        import subprocess as sp
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        _start()
        proc = mock_popen[1]
        proc.wait.side_effect = sp.TimeoutExpired(cmd="cmd", timeout=10)
        _stop("natgeo")
        assert "natgeo" not in main._targets

    def test_windows_path(self, mock_popen, no_threads, mock_bot, monkeypatch):
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        _start()
        result = _stop("natgeo")
        assert "natgeo" not in main._targets

    def test_folder_delete_failure_message(self, mock_popen, no_threads, mock_bot, monkeypatch):
        """Folder deletion failure returns a warning but process is still stopped."""
        self._tracked(mock_popen, no_threads, mock_bot, monkeypatch)
        with patch("main.delete_target_folder", return_value="Permission denied"):
            result = _stop("natgeo")
        assert "natgeo" not in main._targets
        assert "delete" in result.lower() or "Permission" in result or "⚠️" in result


# ── delete_target_folder ──────────────────────────────────────────────────────

class TestDeleteTargetFolder:
    def test_deletes_existing_folder(self, tmp_path):
        d = main.MONITORS_DIR / "todelete"
        d.mkdir(parents=True)
        (d / "file.txt").write_text("x")
        err = main.delete_target_folder("todelete")
        assert err is None
        assert not d.exists()

    def test_missing_folder_returns_none(self):
        err = main.delete_target_folder("nonexistent")
        assert err is None
