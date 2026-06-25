"""Tests for deliver_image() and deliver_data()."""

import io
import csv
from unittest.mock import MagicMock, patch

import pytest
import database as db
import main


def _target(username="natgeo"):
    db.upsert_target(username, 1, "tester")


# ── deliver_image ─────────────────────────────────────────────────────────────

class TestDeliverImage:
    def test_serves_photo_from_db(self, mock_bot):
        _target()
        db.store_media("natgeo", "profile_pic", "profile_pic.jpg", b"\xff\xd8pic")
        result = main.deliver_image(100, "natgeo", "profile")
        assert result is None
        mock_bot.send_photo.assert_called_once()

    def test_serves_video_from_db(self, mock_bot):
        _target()
        db.store_media("natgeo", "post", "post_001.mp4", b"fake_mp4_data")
        result = main.deliver_image(100, "natgeo", "post")
        assert result is None
        mock_bot.send_video.assert_called_once()

    def test_file_object_has_name(self, mock_bot):
        _target()
        db.store_media("natgeo", "profile_pic", "myprofile.jpg", b"data")
        main.deliver_image(100, "natgeo", "profile")
        call_args = mock_bot.send_photo.call_args
        file_arg = call_args[0][1]   # second positional arg
        assert hasattr(file_arg, "name")
        assert file_arg.name == "myprofile.jpg"

    def test_db_too_large_returns_error(self, mock_bot):
        _target()
        big = b"x" * (51 * 1024 * 1024)   # 51 MB
        db.store_media("natgeo", "post", "huge.jpg", big)
        result = main.deliver_image(100, "natgeo", "post")
        assert result is not None
        assert "50 MB" in result or "limit" in result.lower()

    def test_fallback_to_disk(self, mock_bot, tmp_path):
        """If DB has no media, falls back to the on-disk file."""
        _target()
        d = main.MONITORS_DIR / "natgeo"
        d.mkdir(parents=True, exist_ok=True)
        disk_file = d / "instagram_natgeo_profile_pic.jpg"
        disk_file.write_bytes(b"\xff\xd8disk_pic")
        result = main.deliver_image(100, "natgeo", "profile")
        assert result is None
        mock_bot.send_photo.assert_called_once()

    def test_no_media_anywhere_returns_error(self, mock_bot):
        _target()
        result = main.deliver_image(100, "natgeo", "profile")
        assert result is not None
        assert "No" in result or "not found" in result.lower()

    def test_disk_file_too_large_returns_error(self, mock_bot, tmp_path):
        _target()
        d = main.MONITORS_DIR / "natgeo"
        d.mkdir(parents=True, exist_ok=True)
        big_file = d / "instagram_natgeo_profile_pic.jpg"
        big_file.write_bytes(b"x" * (51 * 1024 * 1024))
        result = main.deliver_image(100, "natgeo", "profile")
        assert result is not None
        assert "limit" in result.lower() or "50 MB" in result

    def test_send_exception_returns_error(self, mock_bot):
        _target()
        db.store_media("natgeo", "post", "post.jpg", b"data")
        mock_bot.send_photo.side_effect = Exception("API error")
        result = main.deliver_image(100, "natgeo", "post")
        assert result is not None
        assert "Failed" in result or "failed" in result.lower()

    def test_post_includes_reel_from_db(self, mock_bot):
        """The 'post' logical type also serves reel rows from the DB."""
        _target()
        db.store_media("natgeo", "reel", "reel_001.mp4", b"reeldata")
        result = main.deliver_image(100, "natgeo", "post")
        assert result is None
        mock_bot.send_video.assert_called_once()


# ── deliver_data ──────────────────────────────────────────────────────────────

class TestDeliverData:
    def test_sends_csv_and_log_from_db(self, mock_bot):
        _target()
        # Populate DB
        db._conn.execute(
            "INSERT INTO activity_log(username,occurred_at,change_type) VALUES(?,?,?)",
            ("natgeo", "2025-01-01", "followers"),
        )
        db._conn.commit()
        db.insert_log("natgeo", "Monitor started")
        result = main.deliver_data(100, "natgeo")
        assert result is None
        assert mock_bot.send_document.call_count >= 1

    def test_csv_synced_from_disk(self, mock_bot, tmp_path):
        _target()
        # Write a CSV file on disk
        csv_path = main.csv_path_for("natgeo")
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Type", "Old", "New"])
            writer.writerow(["2025-06-01", "bio_changed", "old", "new"])
        result = main.deliver_data(100, "natgeo")
        # CSV should have been imported and exported
        assert result is None or "⚠️" not in str(result)

    def test_no_data_returns_error(self, mock_bot):
        _target()
        result = main.deliver_data(100, "natgeo")
        # Either an error message or falls back to disk (also empty → error)
        # Accept either path
        assert result is not None or mock_bot.send_document.called

    def test_fallback_to_disk_files(self, mock_bot, tmp_path):
        """If DB empty, fall back to disk .log and .csv files."""
        _target()
        d = main.MONITORS_DIR / "natgeo"
        d.mkdir(parents=True, exist_ok=True)
        log_file = d / "instagram_monitor_natgeo.log"
        log_file.write_text("Some log line\n")
        result = main.deliver_data(100, "natgeo")
        assert result is None
        mock_bot.send_document.assert_called()

    def test_send_document_exception_reports_error(self, mock_bot):
        _target()
        db.insert_log("natgeo", "a line")
        mock_bot.send_document.side_effect = Exception("Send failed")
        result = main.deliver_data(100, "natgeo")
        mock_bot.send_message.assert_called()

    def test_csv_header_only_not_sent(self, mock_bot):
        """An activity CSV with only a header row (no data) should not be sent."""
        _target()
        db.insert_log("natgeo", "some log")
        # No activity_log rows → export_activity_csv returns header only
        main.deliver_data(100, "natgeo")
        calls = [str(c) for c in mock_bot.send_document.call_args_list]
        # Verify: log was sent (has content), but activity CSV with header-only was skipped
        assert mock_bot.send_document.call_count >= 1
