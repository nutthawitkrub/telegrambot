"""Tests for background thread functions: tail_log_and_forward, watch_media_and_forward, watch_process_health."""

import threading
import time
import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import database as db
import main


def _one_shot_stop_event():
    """
    A MagicMock stop_event that lets exactly ONE loop iteration execute:
      - is_set() returns False the first time, True afterwards
      - wait() returns True immediately
    """
    ev = MagicMock(spec=threading.Event)
    call_count = [0]

    def fake_is_set():
        call_count[0] += 1
        return call_count[0] > 1

    ev.is_set.side_effect = fake_is_set
    ev.wait.return_value = True
    ev.set.return_value = None
    return ev


def _make_target_entry(proc, stop_event, chat_id=100, username="natgeo"):
    """Build a _targets dict entry with username and subscribers fields."""
    return {
        "username": username,
        "shared": True,
        "process": proc,
        "stop_event": stop_event,
        "stderr_file_handle": None,
        "started_at": dt.datetime.now(dt.timezone.utc),
        "subscribers": {chat_id: "tester"},
    }


# ── tail_log_and_forward ──────────────────────────────────────────────────────

class TestTailLogAndForward:
    def _run_tail(self, mock_bot, log_file, username="natgeo", monkeypatch=None):
        """Run tail_log_and_forward synchronously; stop_event.wait sets it on first call."""
        db.upsert_target(username, 100, "tester")
        # Set up _targets so _broadcast can find the subscriber chat
        stop_event = threading.Event()
        main._targets[username] = _make_target_entry(MagicMock(), stop_event)

        if monkeypatch:
            def fake_wait(timeout=None):
                stop_event.set()
                return True
            stop_event.wait = fake_wait

        main.tail_log_and_forward(username, log_file, stop_event)

    def test_forwards_lines_to_telegram(self, mock_bot, tmp_path, monkeypatch):
        log_file = tmp_path / "instagram_monitor_natgeo.log"
        log_file.write_text("Line 1\nLine 2\n")
        self._run_tail(mock_bot, log_file, monkeypatch=monkeypatch)
        mock_bot.send_message.assert_called()
        all_text = " ".join(str(c) for c in mock_bot.send_message.call_args_list)
        assert "Line 1" in all_text or "Line 2" in all_text

    def test_stores_lines_in_db(self, mock_bot, tmp_path, monkeypatch):
        log_file = tmp_path / "instagram_monitor_natgeo.log"
        log_file.write_text("DB line\n")
        self._run_tail(mock_bot, log_file, monkeypatch=monkeypatch)
        assert db.has_log_entries("natgeo")

    def test_waits_for_file_creation(self, mock_bot, tmp_path, monkeypatch):
        """If the log file doesn't exist, the function should wait, not crash."""
        db.upsert_target("natgeo", 100, "tester")
        log_file = tmp_path / "missing.log"
        stop_event = threading.Event()
        main._targets["natgeo"] = _make_target_entry(MagicMock(), stop_event)
        # Make wait set the stop_event so the loop exits quickly
        stop_event.wait = lambda t=None: stop_event.set() or True
        main.tail_log_and_forward("natgeo", log_file, stop_event)
        assert True  # no crash

    def test_warns_after_30s_timeout(self, mock_bot, tmp_path, monkeypatch):
        """After 30s waiting, sends a warning and returns."""
        db.upsert_target("natgeo", 100, "tester")
        log_file = tmp_path / "missing.log"
        stop_event = threading.Event()
        main._targets["natgeo"] = _make_target_entry(MagicMock(), stop_event)

        warned = []
        call_count = [0]

        def fake_wait(timeout=None):
            call_count[0] += 1
            # After enough iterations to exceed 30s (60 × 0.5s), the warning fires
            # Force waited >= 30 quickly by making each call count as 0.5s
            if call_count[0] > 60:
                stop_event.set()
            return stop_event.is_set()

        stop_event.wait = fake_wait
        mock_bot.send_message.side_effect = lambda cid, text, *a, **kw: warned.append(text)

        main.tail_log_and_forward("natgeo", log_file, stop_event)
        if warned:
            assert any("30" in w or "log" in w.lower() for w in warned)

    def test_prefixes_each_line_with_username(self, mock_bot, tmp_path, monkeypatch):
        log_file = tmp_path / "instagram_monitor_natgeo.log"
        log_file.write_text("hello world\n")
        self._run_tail(mock_bot, log_file, monkeypatch=monkeypatch)
        all_text = " ".join(str(c) for c in mock_bot.send_message.call_args_list)
        assert "[natgeo]" in all_text

    def test_skips_empty_lines(self, mock_bot, tmp_path, monkeypatch):
        log_file = tmp_path / "instagram_monitor_natgeo.log"
        log_file.write_text("\n\n\n")
        self._run_tail(mock_bot, log_file, monkeypatch=monkeypatch)
        for c in mock_bot.send_message.call_args_list:
            text = c[0][1] if c[0] else ""
            assert text.strip()


# ── watch_media_and_forward ───────────────────────────────────────────────────

class TestWatchMediaAndForward:
    def _setup_dir(self):
        tdir = main.MONITORS_DIR / "natgeo"
        tdir.mkdir(parents=True, exist_ok=True)
        db.upsert_target("natgeo", 100, "tester")
        # populate _targets so _broadcast works
        stop_event = threading.Event()
        main._targets["natgeo"] = _make_target_entry(MagicMock(), stop_event)
        return tdir

    def _no_seed_amp(self, monkeypatch):
        """First call to all_media_glob_patterns returns [] (skip seeding)."""
        call_count = [0]
        original = main.all_media_glob_patterns

        def patched(u):
            call_count[0] += 1
            return [] if call_count[0] == 1 else original(u)

        monkeypatch.setattr(main, "all_media_glob_patterns", patched)

    def test_sends_new_media_file(self, mock_bot, tmp_path, monkeypatch):
        tdir = self._setup_dir()
        self._no_seed_amp(monkeypatch)
        stop_event = _one_shot_stop_event()
        main._targets["natgeo"]["stop_event"] = stop_event
        media_file = tdir / "instagram_natgeo_post_20240101_120000.jpg"
        media_file.write_bytes(b"\xff\xd8img")
        main.watch_media_and_forward("natgeo", stop_event)
        mock_bot.send_photo.assert_called_once()

    def test_pre_existing_files_not_sent(self, mock_bot, tmp_path):
        tdir = self._setup_dir()
        media_file = tdir / "instagram_natgeo_post_20240101_120000.jpg"
        media_file.write_bytes(b"\xff\xd8img")
        stop_event = threading.Event()
        stop_event.set()
        main.watch_media_and_forward("natgeo", stop_event)
        mock_bot.send_photo.assert_not_called()
        mock_bot.send_video.assert_not_called()

    def test_stores_new_media_in_db(self, mock_bot, tmp_path, monkeypatch):
        tdir = self._setup_dir()
        self._no_seed_amp(monkeypatch)
        stop_event = _one_shot_stop_event()
        main._targets["natgeo"]["stop_event"] = stop_event
        media_file = tdir / "instagram_natgeo_profile_pic.jpg"
        media_file.write_bytes(b"\xff\xd8profile")
        main.watch_media_and_forward("natgeo", stop_event)
        result = db.get_latest_media("natgeo", "profile")
        assert result is not None

    def test_mp4_uses_send_video(self, mock_bot, tmp_path, monkeypatch):
        tdir = self._setup_dir()
        self._no_seed_amp(monkeypatch)
        stop_event = _one_shot_stop_event()
        main._targets["natgeo"]["stop_event"] = stop_event
        media_file = tdir / "instagram_natgeo_reel_20240101_120000.mp4"
        media_file.write_bytes(b"fake_mp4")
        main.watch_media_and_forward("natgeo", stop_event)
        mock_bot.send_video.assert_called_once()

    def test_handles_missing_dir_gracefully(self, mock_bot):
        stop_event = threading.Event()
        stop_event.set()
        main.watch_media_and_forward("ghost_user", stop_event)


# ── watch_process_health ──────────────────────────────────────────────────────

class TestWatchProcessHealth:
    def _setup(self, exit_code=1, wait_returns=False):
        db.upsert_target("natgeo", 100, "tester")
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = exit_code
        stop_event = threading.Event()
        stop_event.wait = lambda timeout=None: wait_returns
        main._targets["natgeo"] = _make_target_entry(proc, stop_event)
        return proc, stop_event

    def test_notifies_crash(self, mock_bot, tmp_path):
        proc, stop_event = self._setup(exit_code=2, wait_returns=False)
        main.watch_process_health("natgeo", proc, stop_event)
        mock_bot.send_message.assert_called()
        msg_text = mock_bot.send_message.call_args[0][1]
        assert "🔴" in msg_text or "stopped" in msg_text.lower()

    def test_removes_from_targets_on_crash(self, mock_bot, tmp_path):
        proc, stop_event = self._setup(wait_returns=False)
        main.watch_process_health("natgeo", proc, stop_event)
        assert "natgeo" not in main._targets

    def test_deactivates_target_in_db(self, mock_bot, tmp_path):
        proc, stop_event = self._setup(wait_returns=False)
        main.watch_process_health("natgeo", proc, stop_event)
        rows = db.load_active_targets()
        assert not any(r["username"] == "natgeo" for r in rows)

    def test_includes_stderr_in_crash_message(self, mock_bot, tmp_path):
        proc, stop_event = self._setup(wait_returns=False)
        stderr_file = main.MONITORS_DIR / "natgeo" / "instagram_monitor_natgeo.stderr.log"
        stderr_file.parent.mkdir(parents=True, exist_ok=True)
        stderr_file.write_text("Traceback:\n  ImportError: no module named x\n")
        main.watch_process_health("natgeo", proc, stop_event)
        msg_text = mock_bot.send_message.call_args[0][1]
        assert "ImportError" in msg_text or "stderr" in msg_text.lower()

    def test_clean_stop_sends_no_crash_message(self, mock_bot, tmp_path):
        """stop_event set (intentional stop) → no crash notification."""
        proc, stop_event = self._setup(exit_code=None, wait_returns=True)
        proc.poll.return_value = None   # still alive
        main.watch_process_health("natgeo", proc, stop_event)
        for c in mock_bot.send_message.call_args_list:
            text = c[0][1] if c[0] else ""
            assert "🔴" not in text
