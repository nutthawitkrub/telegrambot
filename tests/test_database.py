"""Tests for database.py — full coverage of every public function."""

import csv
import io
import sqlite3
from pathlib import Path

import pytest
import database as db


# ── helpers ───────────────────────────────────────────────────────────────────

def _add_target(username="natgeo", chat_id=1, started_by="tester"):
    db.upsert_target(username, chat_id, started_by)


def _write_csv(path: Path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Type", "Old", "New"])
        w.writerows(rows)


# ── schema / init ─────────────────────────────────────────────────────────────

class TestInit:
    def test_creates_all_tables(self):
        tables = {
            r[0]
            for r in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"targets", "activity_log", "media_files", "log_entries", "process_events"}.issubset(tables)

    def test_wal_mode(self):
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_on(self):
        fk = db._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1

    def test_idempotent_reinit(self, tmp_path):
        """Calling init a second time must not raise or drop tables."""
        db.init(tmp_path / "test.db")   # second init on same (new) file
        self.test_creates_all_tables()


# ── targets ───────────────────────────────────────────────────────────────────

class TestTargets:
    def test_upsert_insert(self):
        db.upsert_target("alice", 42, "me")
        rows = db._conn.execute("SELECT * FROM targets WHERE username='alice'").fetchall()
        assert len(rows) == 1
        assert rows[0]["chat_id"] == 42
        assert rows[0]["is_active"] == 1

    def test_upsert_updates_on_conflict(self):
        db.upsert_target("bob", 1, "first")
        db.upsert_target("bob", 999, "second")
        row = db._conn.execute("SELECT * FROM targets WHERE username='bob'").fetchone()
        assert row["chat_id"] == 999
        assert row["started_by"] == "second"
        assert row["is_active"] == 1

    def test_upsert_reactivates_inactive(self):
        db.upsert_target("carol", 5, "x")
        db.deactivate_target("carol")
        db.upsert_target("carol", 5, "x")
        row = db._conn.execute("SELECT is_active FROM targets WHERE username='carol'").fetchone()
        assert row["is_active"] == 1

    def test_deactivate(self):
        db.upsert_target("dave", 7, "y")
        db.deactivate_target("dave")
        row = db._conn.execute("SELECT is_active FROM targets WHERE username='dave'").fetchone()
        assert row["is_active"] == 0

    def test_deactivate_nonexistent_is_noop(self):
        db.deactivate_target("nobody")  # must not raise

    def test_delete_removes_row(self):
        db.upsert_target("eve", 8, "z")
        db.delete_target("eve")
        row = db._conn.execute("SELECT 1 FROM targets WHERE username='eve'").fetchone()
        assert row is None

    def test_delete_cascades_to_log_entries(self):
        db.upsert_target("fred", 9, "z")
        db.insert_log("fred", "hello")
        db.delete_target("fred")
        rows = db._conn.execute("SELECT 1 FROM log_entries WHERE username='fred'").fetchall()
        assert rows == []

    def test_delete_cascades_to_media_files(self):
        db.upsert_target("gina", 10, "z")
        db.store_media("gina", "post", "pic.jpg", b"data")
        db.delete_target("gina")
        rows = db._conn.execute("SELECT 1 FROM media_files WHERE username='gina'").fetchall()
        assert rows == []

    def test_delete_nonexistent_is_noop(self):
        db.delete_target("nobody")  # must not raise

    def test_load_active_targets_returns_only_active(self):
        db.upsert_target("active_one", 1, "a")
        db.upsert_target("inactive_one", 2, "b")
        db.deactivate_target("inactive_one")
        result = db.load_active_targets()
        usernames = {r["username"] for r in result}
        assert "active_one" in usernames
        assert "inactive_one" not in usernames

    def test_load_active_targets_empty(self):
        assert db.load_active_targets() == []

    def test_load_active_targets_fields(self):
        db.upsert_target("hank", 55, "by_hank")
        rows = db.load_active_targets()
        assert rows[0]["username"] == "hank"
        assert rows[0]["chat_id"] == 55
        assert rows[0]["started_by"] == "by_hank"


# ── activity_log ──────────────────────────────────────────────────────────────

class TestActivityLog:
    def test_export_empty_returns_header_only(self):
        _add_target()
        csv_bytes = db.export_activity_csv("natgeo")
        lines = csv_bytes.decode().strip().splitlines()
        assert len(lines) == 1
        assert "Date" in lines[0]

    def test_export_with_rows(self):
        _add_target()
        db._conn.execute(
            "INSERT INTO activity_log(username,occurred_at,change_type,old_value,new_value)"
            " VALUES(?,?,?,?,?)",
            ("natgeo", "2025-01-01T00:00:00", "followers", "100", "200"),
        )
        db._conn.commit()
        csv_bytes = db.export_activity_csv("natgeo")
        reader = csv.reader(io.StringIO(csv_bytes.decode()))
        rows = list(reader)
        assert rows[0] == ["Date", "Type", "Old", "New"]
        assert rows[1][1] == "followers"
        assert rows[1][3] == "200"

    def test_import_csv_happy_path(self, tmp_path):
        _add_target()
        p = tmp_path / "act.csv"
        _write_csv(p, [("2025-01-01", "bio_changed", "old bio", "new bio")])
        n = db.import_csv("natgeo", p)
        assert n == 1
        rows = db._conn.execute("SELECT * FROM activity_log").fetchall()
        assert len(rows) == 1

    def test_import_csv_missing_file_returns_zero(self, tmp_path):
        _add_target()
        n = db.import_csv("natgeo", tmp_path / "no_such.csv")
        assert n == 0

    def test_import_csv_dedup_on_unique_key(self, tmp_path):
        _add_target()
        p = tmp_path / "act.csv"
        _write_csv(p, [("2025-01-01", "bio", "a", "b")])
        db.import_csv("natgeo", p)
        n = db.import_csv("natgeo", p)   # second import of same file
        assert n == 0                     # nothing new inserted

    def test_import_csv_skips_short_rows(self, tmp_path):
        _add_target()
        p = tmp_path / "act.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            f.write("Date,Type,Old,New\n")
            f.write("only_one_column\n")
            f.write("2025-01-01,followers,100,200\n")
        n = db.import_csv("natgeo", p)
        assert n == 1   # only the valid row

    def test_import_csv_partial_row_ok(self, tmp_path):
        _add_target()
        p = tmp_path / "act.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            f.write("Date,Type\n2025-01-01,event\n")
        n = db.import_csv("natgeo", p)
        assert n == 1


# ── media_files ───────────────────────────────────────────────────────────────

class TestMediaFiles:
    def test_store_and_get_profile(self):
        _add_target()
        db.store_media("natgeo", "profile_pic", "profile_pic.jpg", b"\xff\xd8pic")
        result = db.get_latest_media("natgeo", "profile")
        assert result is not None
        name, data = result
        assert name == "profile_pic.jpg"
        assert data == b"\xff\xd8pic"

    def test_store_and_get_post(self):
        _add_target()
        db.store_media("natgeo", "post", "post_001.jpg", b"postdata")
        result = db.get_latest_media("natgeo", "post")
        assert result is not None
        assert result[0] == "post_001.jpg"

    def test_get_post_includes_reel(self):
        _add_target()
        db.store_media("natgeo", "reel", "reel_001.mp4", b"reeldata")
        result = db.get_latest_media("natgeo", "post")
        assert result is not None
        assert result[0] == "reel_001.mp4"

    def test_get_latest_returns_most_recent(self):
        _add_target()
        db.store_media("natgeo", "post", "post_old.jpg", b"old")
        db.store_media("natgeo", "post", "post_new.jpg", b"new")
        name, _ = db.get_latest_media("natgeo", "post")
        assert name == "post_new.jpg"

    def test_get_story(self):
        _add_target()
        db.store_media("natgeo", "story", "story_001.jpg", b"storydata")
        result = db.get_latest_media("natgeo", "story")
        assert result is not None

    def test_get_returns_none_when_empty(self):
        _add_target()
        assert db.get_latest_media("natgeo", "profile") is None
        assert db.get_latest_media("natgeo", "post") is None
        assert db.get_latest_media("natgeo", "story") is None

    def test_filename_stored_true(self):
        _add_target()
        db.store_media("natgeo", "post", "already.jpg", b"x")
        assert db.filename_stored("already.jpg") is True

    def test_filename_stored_false(self):
        assert db.filename_stored("never_seen.jpg") is False

    def test_upsert_updates_existing(self):
        _add_target()
        db.store_media("natgeo", "post", "same.jpg", b"v1")
        db.store_media("natgeo", "post", "same.jpg", b"v2")
        _, data = db.get_latest_media("natgeo", "post")
        assert data == b"v2"

    def test_store_parses_long_timestamp(self):
        _add_target()
        db.store_media("natgeo", "post", "instagram_natgeo_post_20240625_143022.jpg", b"x")
        row = db._conn.execute("SELECT captured_at FROM media_files").fetchone()
        assert row["captured_at"] == "2024-06-25T14:30:22"

    def test_store_parses_short_timestamp(self):
        _add_target()
        db.store_media("natgeo", "profile_pic", "instagram_natgeo_profile_pic_20240114_1806.jpg", b"x")
        row = db._conn.execute("SELECT captured_at FROM media_files").fetchone()
        assert row["captured_at"] == "2024-01-14T18:06:00"

    def test_store_no_timestamp_uses_none(self):
        _add_target()
        db.store_media("natgeo", "post", "no_ts.jpg", b"x")
        row = db._conn.execute("SELECT captured_at FROM media_files").fetchone()
        assert row["captured_at"] is None

    def test_store_sets_file_size(self):
        _add_target()
        data = b"1234567"
        db.store_media("natgeo", "post", "sized.jpg", data)
        row = db._conn.execute("SELECT file_size FROM media_files").fetchone()
        assert row["file_size"] == 7

    def test_store_sets_mime_type(self):
        _add_target()
        db.store_media("natgeo", "post", "photo.jpg", b"x")
        row = db._conn.execute("SELECT mime_type FROM media_files").fetchone()
        assert row["mime_type"] == "image/jpeg"

    def test_mark_sent(self):
        _add_target()
        db.store_media("natgeo", "post", "send_me.jpg", b"x")
        db.mark_sent("send_me.jpg", "TG_FILE_ID_123")
        row = db._conn.execute("SELECT sent_to_telegram, telegram_file_id FROM media_files").fetchone()
        assert row["sent_to_telegram"] == 1
        assert row["telegram_file_id"] == "TG_FILE_ID_123"

    def test_import_media_file(self, tmp_path):
        _add_target()
        p = tmp_path / "photo.jpg"
        p.write_bytes(b"\xff\xd8test")
        result = db.import_media_file("natgeo", p, "post")
        assert result is True
        assert db.filename_stored("photo.jpg")

    def test_import_media_file_skips_duplicate(self, tmp_path):
        _add_target()
        p = tmp_path / "dupe.jpg"
        p.write_bytes(b"data")
        db.import_media_file("natgeo", p, "post")
        result = db.import_media_file("natgeo", p, "post")
        assert result is False

    def test_import_media_file_missing_file(self, tmp_path):
        _add_target()
        result = db.import_media_file("natgeo", tmp_path / "ghost.jpg", "post")
        assert result is False


# ── log_entries ───────────────────────────────────────────────────────────────

class TestLogEntries:
    def test_insert_and_export(self):
        _add_target()
        db.insert_log("natgeo", "Profile updated")
        db.insert_log("natgeo", "Followers: 1000")
        exported = db.export_log_text("natgeo").decode()
        assert "Profile updated" in exported
        assert "Followers: 1000" in exported

    def test_export_empty_is_empty_bytes(self):
        _add_target()
        assert db.export_log_text("natgeo") == b""

    def test_export_includes_timestamp(self):
        _add_target()
        db.insert_log("natgeo", "some message")
        text = db.export_log_text("natgeo").decode()
        assert "[" in text   # timestamp bracket present

    def test_has_log_entries_false(self):
        _add_target()
        assert db.has_log_entries("natgeo") is False

    def test_has_log_entries_true(self):
        _add_target()
        db.insert_log("natgeo", "x")
        assert db.has_log_entries("natgeo") is True

    def test_import_log_file(self, tmp_path):
        _add_target()
        log_p = tmp_path / "monitor.log"
        log_p.write_text("Line 1\nLine 2\nLine 3\n", encoding="utf-8")
        n = db.import_log_file("natgeo", log_p)
        assert n == 3
        assert db.has_log_entries("natgeo")

    def test_import_log_file_missing(self, tmp_path):
        _add_target()
        n = db.import_log_file("natgeo", tmp_path / "no.log")
        assert n == 0

    def test_import_log_file_idempotent(self, tmp_path):
        _add_target()
        log_p = tmp_path / "monitor.log"
        log_p.write_text("Line A\n", encoding="utf-8")
        db.import_log_file("natgeo", log_p)
        n = db.import_log_file("natgeo", log_p)  # second import skipped
        assert n == 0

    def test_import_log_file_skips_blank_lines(self, tmp_path):
        _add_target()
        log_p = tmp_path / "monitor.log"
        log_p.write_text("Real line\n\n   \nAnother\n", encoding="utf-8")
        n = db.import_log_file("natgeo", log_p)
        assert n == 2


# ── process_events ────────────────────────────────────────────────────────────

class TestProcessEvents:
    def test_insert_started(self):
        _add_target()
        db.insert_event("natgeo", "started")
        row = db._conn.execute("SELECT event_type FROM process_events").fetchone()
        assert row["event_type"] == "started"

    def test_insert_crashed_with_details(self):
        _add_target()
        db.insert_event("natgeo", "crashed", exit_code=1, error_message="Traceback...")
        row = db._conn.execute("SELECT * FROM process_events").fetchone()
        assert row["event_type"] == "crashed"
        assert row["exit_code"] == 1
        assert "Traceback" in row["error_message"]

    def test_insert_all_event_types(self):
        _add_target()
        for ev in ("started", "stopped", "crashed", "resumed"):
            db.insert_event("natgeo", ev)
        count = db._conn.execute("SELECT COUNT(*) FROM process_events").fetchone()[0]
        assert count == 4

    def test_invalid_event_type_raises(self):
        _add_target()
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO process_events(username,event_type) VALUES(?,?)",
                ("natgeo", "INVALID"),
            )
            db._conn.commit()
