"""Tests for startup functions: ensure_shared_config, migrate_existing_data, resume_persisted_targets."""

import json
import csv
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import database as db
import main


# ── ensure_shared_config ──────────────────────────────────────────────────────

class TestEnsureSharedConfig:
    def test_creates_config_if_missing(self, tmp_path, monkeypatch):
        conf = tmp_path / "telegram_monitor.conf"
        monkeypatch.setattr(main, "SHARED_CONFIG", conf)
        main.ensure_shared_config()
        assert conf.exists()

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        conf = tmp_path / "telegram_monitor.conf"
        conf.write_text("my custom config\n")
        monkeypatch.setattr(main, "SHARED_CONFIG", conf)
        main.ensure_shared_config()
        assert conf.read_text() == "my custom config\n"

    def test_config_includes_defaults(self, tmp_path, monkeypatch):
        conf = tmp_path / "telegram_monitor.conf"
        monkeypatch.setattr(main, "SHARED_CONFIG", conf)
        monkeypatch.setenv("INSTA_CHECK_INTERVAL", "3600")
        main.ensure_shared_config()
        content = conf.read_text()
        assert "INSTA_CHECK_INTERVAL = 3600" in content

    def test_config_includes_webhook_when_set(self, tmp_path, monkeypatch):
        conf = tmp_path / "telegram_monitor.conf"
        monkeypatch.setattr(main, "SHARED_CONFIG", conf)
        monkeypatch.setenv("WEBHOOK_URL", "https://example.com/hook")
        main.ensure_shared_config()
        content = conf.read_text()
        assert "WEBHOOK_URL" in content
        assert "https://example.com/hook" in content

    def test_config_no_webhook_if_not_set(self, tmp_path, monkeypatch):
        conf = tmp_path / "telegram_monitor.conf"
        monkeypatch.setattr(main, "SHARED_CONFIG", conf)
        monkeypatch.delenv("WEBHOOK_URL", raising=False)
        main.ensure_shared_config()
        content = conf.read_text()
        assert "WEBHOOK_URL" not in content

    def test_config_includes_smtp_when_set(self, tmp_path, monkeypatch):
        conf = tmp_path / "telegram_monitor.conf"
        monkeypatch.setattr(main, "SHARED_CONFIG", conf)
        monkeypatch.setenv("SMTP_PASSWORD", "s3cret")
        main.ensure_shared_config()
        content = conf.read_text()
        assert "SMTP_PASSWORD" in content


# ── migrate_existing_data ─────────────────────────────────────────────────────

class TestMigrateExistingData:
    def _user_dir(self, username="natgeo"):
        d = main.MONITORS_DIR / username
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_no_monitors_dir_is_noop(self, monkeypatch, tmp_path):
        """If MONITORS_DIR doesn't exist, migrate_existing_data returns without error."""
        missing = tmp_path / "no_monitors"
        monkeypatch.setattr(main, "MONITORS_DIR", missing)
        main.migrate_existing_data()   # should not raise

    def test_imports_csv(self, tmp_path):
        d = self._user_dir("natgeo")
        csv_path = main.csv_path_for("natgeo")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Date", "Type", "Old", "New"])
            w.writerow(["2025-01-01T00:00:00", "followers", "100", "101"])
        main.migrate_existing_data()
        rows = db._conn.execute(
            "SELECT * FROM activity_log WHERE username='natgeo'"
        ).fetchall()
        assert len(rows) == 1

    def test_imports_log(self, tmp_path):
        d = self._user_dir("natgeo")
        log_path = main.log_path_for("natgeo")
        log_path.write_text("Log line 1\nLog line 2\n")
        main.migrate_existing_data()
        assert db.has_log_entries("natgeo")

    def test_imports_media(self, tmp_path):
        d = self._user_dir("natgeo")
        media_file = d / "instagram_natgeo_profile_pic.jpg"
        media_file.write_bytes(b"\xff\xd8media_data")
        main.migrate_existing_data()
        result = db.get_latest_media("natgeo", "profile")
        assert result is not None

    def test_idempotent(self, tmp_path):
        """Running twice should not duplicate rows."""
        d = self._user_dir("natgeo")
        log_path = main.log_path_for("natgeo")
        log_path.write_text("Log line\n")
        main.migrate_existing_data()
        main.migrate_existing_data()
        count = db._conn.execute(
            "SELECT COUNT(*) FROM log_entries WHERE username='natgeo'"
        ).fetchone()[0]
        assert count == 1

    def test_skips_invalid_dir_names(self, tmp_path):
        """Directories with names that fail is_valid_username are skipped."""
        bad = main.MONITORS_DIR / "bad/user"
        # Can't create that on Windows, so create a valid-looking but too-long dir
        long_name = "a" * 31
        long_dir = main.MONITORS_DIR / long_name
        long_dir.mkdir(parents=True, exist_ok=True)
        main.migrate_existing_data()
        # No crash, and no DB row for the invalid name
        rows = db.load_active_targets()
        assert not any(r["username"] == long_name for r in rows)

    def test_reads_json_state_for_chat_id(self, tmp_path):
        d = self._user_dir("natgeo")
        state_file = main.STATE_FILE
        state_file.write_text(
            json.dumps({"natgeo": {"chat_id": 999, "started_by": "alice"}}),
            encoding="utf-8",
        )
        main.migrate_existing_data()
        rows = db.load_active_targets()
        natgeo = next((r for r in rows if r["username"] == "natgeo"), None)
        assert natgeo is not None
        assert natgeo["chat_id"] == 999


# ── resume_persisted_targets ──────────────────────────────────────────────────

class TestResumePersistedTargets:
    def test_resumes_from_db(self, mock_bot, mock_popen, no_threads):
        db.upsert_target("natgeo", 100, "Alice")
        main.resume_persisted_targets()
        # Resume is per-device: key is username_chatid, matching a fresh /track.
        assert "natgeo_100" in main._targets
        assert "natgeo" not in main._targets
        mock_bot.send_message.assert_called()

    def test_sends_resume_message(self, mock_bot, mock_popen, no_threads):
        db.upsert_target("natgeo", 100, "Alice")
        main.resume_persisted_targets()
        msg_texts = [str(c) for c in mock_bot.send_message.call_args_list]
        combined = " ".join(msg_texts)
        assert "Resumed" in combined or "resumed" in combined

    def test_nothing_to_resume(self, mock_bot, mock_popen, no_threads):
        """If DB is empty (and no JSON), nothing is resumed."""
        main.resume_persisted_targets()
        assert len(main._targets) == 0

    def test_fallback_from_json(self, mock_bot, mock_popen, no_threads):
        """If DB is empty, falls back to JSON state file."""
        state = {"natgeo": {"chat_id": 55, "started_by": "Bob"}}
        main.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        main.resume_persisted_targets()
        assert "natgeo_55" in main._targets

    def test_skips_invalid_usernames(self, mock_bot, mock_popen, no_threads):
        """Corrupted/invalid usernames in persisted state are skipped."""
        state = {"in/valid!": {"chat_id": 55, "started_by": "Bob"}}
        main.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        main.resume_persisted_targets()
        assert "in/valid!" not in main._targets

    def test_skips_entry_with_no_chat_id(self, mock_bot, mock_popen, no_threads):
        state = {"natgeo": {"started_by": "Bob"}}  # no chat_id
        main.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        main.resume_persisted_targets()
        assert "natgeo" not in main._targets

    def test_notify_failure_is_swallowed(self, mock_bot, mock_popen, no_threads):
        """If send_message raises, resume still completes without crashing."""
        db.upsert_target("natgeo", 100, "Alice")
        mock_bot.send_message.side_effect = Exception("Telegram down")
        main.resume_persisted_targets()   # must not raise
        assert "natgeo_100" in main._targets


# ── load_persisted_state ──────────────────────────────────────────────────────

class TestLoadPersistedState:
    def test_reads_from_db(self):
        db.upsert_target("natgeo", 100, "Alice")
        state = main.load_persisted_state()
        assert "natgeo" in state
        assert state["natgeo"]["chat_id"] == 100

    def test_falls_back_to_json(self, tmp_path):
        # DB is empty (no rows), state file has data
        state = {"natgeo": {"chat_id": 77, "started_by": "Bob"}}
        main.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        result = main.load_persisted_state()
        assert "natgeo" in result

    def test_missing_json_returns_empty(self):
        result = main.load_persisted_state()
        assert result == {}

    def test_corrupt_json_returns_empty(self, tmp_path):
        main.STATE_FILE.write_text("{corrupt json!!!", encoding="utf-8")
        result = main.load_persisted_state()
        assert result == {}
