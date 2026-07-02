"""Tests for pure helper / utility functions in main.py."""

import main
from tests.conftest import make_message


# ── is_private_profile_log ────────────────────────────────────────────────────

class TestIsPrivateProfileLog:
    def test_detects_private_status_line(self):
        assert main.is_private_profile_log("Profile:\t\t\t\tprivate") is True

    def test_public_status_line_is_false(self):
        assert main.is_private_profile_log("Profile:\t\t\t\tpublic") is False

    def test_unrelated_line_mentioning_private_is_false(self):
        # Must start with 'profile:' — collab-leak notes mention 'private' but aren't the status line.
        assert main.is_private_profile_log("Detection of collab posts from private accounts") is False

    def test_case_insensitive(self):
        assert main.is_private_profile_log("PROFILE:   PRIVATE") is True


# ── is_valid_username ─────────────────────────────────────────────────────────

class TestIsValidUsername:
    def test_simple_letters(self):
        assert main.is_valid_username("natgeo") is True

    def test_with_digits(self):
        assert main.is_valid_username("user123") is True

    def test_with_period(self):
        assert main.is_valid_username("john.doe") is True

    def test_with_underscore(self):
        assert main.is_valid_username("john_doe") is True

    def test_max_length_30(self):
        assert main.is_valid_username("a" * 30) is True

    def test_too_long_31(self):
        assert main.is_valid_username("a" * 31) is False

    def test_empty_string(self):
        assert main.is_valid_username("") is False

    def test_space_invalid(self):
        assert main.is_valid_username("john doe") is False

    def test_at_sign_invalid(self):
        assert main.is_valid_username("@natgeo") is False

    def test_slash_invalid(self):
        assert main.is_valid_username("na/tgeo") is False

    def test_mixed_case_ok(self):
        assert main.is_valid_username("JohnDoe") is True


# ── classify_media_type ───────────────────────────────────────────────────────

class TestClassifyMediaType:
    def test_profile(self):
        assert main.classify_media_type("instagram_natgeo_profile_pic.jpg", "natgeo") == "profile"

    def test_profile_with_timestamp(self):
        assert main.classify_media_type("instagram_natgeo_profile_pic_20240101_1200.jpg", "natgeo") == "profile"

    def test_post(self):
        assert main.classify_media_type("instagram_natgeo_post_20240101_120000.jpg", "natgeo") == "post"

    def test_reel_maps_to_post(self):
        assert main.classify_media_type("instagram_natgeo_reel_20240101_120000.mp4", "natgeo") == "post"

    def test_story(self):
        assert main.classify_media_type("instagram_natgeo_story_20240101_120000.jpg", "natgeo") == "story"

    def test_unknown_returns_media(self):
        assert main.classify_media_type("some_random_file.jpg", "natgeo") == "media"


# ── _db_media_type ────────────────────────────────────────────────────────────

class TestDbMediaType:
    def test_profile_pic(self):
        assert main._db_media_type("instagram_natgeo_profile_pic.jpg", "natgeo") == "profile_pic"

    def test_reel(self):
        assert main._db_media_type("instagram_natgeo_reel_001.mp4", "natgeo") == "reel"

    def test_post(self):
        assert main._db_media_type("instagram_natgeo_post_001.jpg", "natgeo") == "post"

    def test_story(self):
        assert main._db_media_type("instagram_natgeo_story_001.jpg", "natgeo") == "story"

    def test_fallback_returns_post(self):
        assert main._db_media_type("unknown_file.jpg", "natgeo") == "post"


# ── media_glob_patterns ───────────────────────────────────────────────────────

class TestMediaGlobPatterns:
    def test_profile_one_pattern(self):
        p = main.media_glob_patterns("natgeo", "profile")
        assert len(p) == 1
        assert "profile_pic" in p[0]

    def test_post_four_patterns(self):
        p = main.media_glob_patterns("natgeo", "post")
        assert len(p) == 4
        assert any("post" in x for x in p)
        assert any("reel" in x for x in p)

    def test_story_two_patterns(self):
        p = main.media_glob_patterns("natgeo", "story")
        assert len(p) == 2
        assert any("story" in x for x in p)

    def test_unknown_returns_empty(self):
        assert main.media_glob_patterns("natgeo", "unknown") == []


# ── describe_sender ───────────────────────────────────────────────────────────

class TestDescribeSender:
    def test_private_with_username(self):
        msg = make_message(chat_type="private", tg_username="alice", first_name="Alice")
        result = main.describe_sender(msg)
        assert "alice" in result.lower() or "Alice" in result

    def test_private_no_username(self):
        msg = make_message(chat_type="private", first_name="Bob")
        msg.from_user.username = None
        result = main.describe_sender(msg)
        assert "Bob" in result

    def test_private_no_user_at_all(self):
        msg = make_message(chat_type="private")
        msg.from_user = None
        result = main.describe_sender(msg)
        assert result  # non-empty string

    def test_group_includes_group_name(self):
        msg = make_message(chat_type="group", group_title="MyGroup", tg_username="carol")
        result = main.describe_sender(msg)
        assert "MyGroup" in result


# ── awaiting_key ──────────────────────────────────────────────────────────────

class TestAwaitingKey:
    def test_returns_tuple(self):
        msg = make_message(chat_id=55, user_id=77)
        key = main.awaiting_key(msg)
        assert key == (55, 77)

    def test_no_from_user_uses_zero(self):
        msg = make_message(chat_id=55)
        msg.from_user = None
        key = main.awaiting_key(msg)
        assert key == (55, 0)


# ── keyboard builders ─────────────────────────────────────────────────────────

class TestKeyboards:
    def test_main_reply_keyboard_has_buttons(self):
        kb = main.main_reply_keyboard()
        assert kb is not None

    def test_username_picker_empty_returns_none(self):
        assert main.username_picker_keyboard("stop") is None

    def test_username_picker_with_targets(self, mock_popen, no_threads, tmp_path):
        # Add a fake entry to _targets
        from unittest.mock import MagicMock
        proc = MagicMock()
        proc.poll.return_value = None
        main._targets["fakeuser"] = {"username": "fakeuser", "shared": True, "process": proc, "subscribers": {1: "tester"}, "stop_event": __import__("threading").Event()}
        kb = main.username_picker_keyboard("stop")
        assert kb is not None

    def test_image_type_keyboard_has_profile_and_post_only(self):
        kb = main.image_type_keyboard("natgeo")
        assert kb is not None
        labels = [btn.text for row in kb.keyboard for btn in row]
        assert labels == ["Profile", "Post"]
        assert "Story" not in labels


# ── path helpers ──────────────────────────────────────────────────────────────

class TestPathHelpers:
    def test_log_path_for(self):
        p = main.log_path_for("natgeo")
        assert "natgeo" in str(p)
        assert p.suffix == ".log"

    def test_stderr_path_for(self):
        p = main.stderr_path_for("natgeo")
        assert "stderr" in p.name

    def test_csv_path_for(self):
        p = main.csv_path_for("natgeo")
        assert p.suffix == ".csv"

    def test_target_dir(self, tmp_path):
        d = main.target_dir("natgeo")
        assert d.name == "natgeo"


# ── send_chunked ──────────────────────────────────────────────────────────────

class TestSendChunked:
    def test_short_message_one_call(self, mock_bot):
        main.send_chunked(123, "Hello, World!")
        mock_bot.send_message.assert_called_once_with(123, "Hello, World!")

    def test_empty_string_no_call(self, mock_bot):
        main.send_chunked(123, "")
        mock_bot.send_message.assert_not_called()

    def test_long_message_splits(self, mock_bot):
        long_text = "x" * (main.MAX_MSG_LEN + 50)
        main.send_chunked(123, long_text)
        assert mock_bot.send_message.call_count == 2


# ── build_monitor_command ─────────────────────────────────────────────────────

class TestBuildMonitorCommand:
    def test_basic_command(self):
        cmd = main.build_monitor_command("natgeo")
        assert cmd[0] == "instagram_monitor"
        assert "natgeo" in cmd

    def test_includes_csv_flag(self):
        cmd = main.build_monitor_command("natgeo")
        assert "-b" in cmd

    def test_with_config_file(self, tmp_path):
        conf = tmp_path / "telegram_monitor.conf"
        conf.write_text("[settings]\n")
        import main as m
        m.SHARED_CONFIG = conf
        cmd = main.build_monitor_command("natgeo")
        assert "--config-file" in cmd

    def test_without_config_file(self, tmp_path):
        import main as m
        m.SHARED_CONFIG = tmp_path / "no_such.conf"
        cmd = main.build_monitor_command("natgeo")
        assert "--config-file" not in cmd

    def test_proxy_flags_added_when_env_set(self, monkeypatch):
        monkeypatch.setenv("INSTA_PROXY_URL", "http://user:pass@proxy.example.com:8080")
        cmd = main.build_monitor_command("natgeo")
        assert "--enable-proxy" in cmd
        assert "--proxy-url" in cmd
        assert "http://user:pass@proxy.example.com:8080" in cmd

    def test_no_proxy_flags_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("INSTA_PROXY_URL", raising=False)
        cmd = main.build_monitor_command("natgeo")
        assert "--enable-proxy" not in cmd
        assert "--proxy-url" not in cmd


# ── find_latest_media ─────────────────────────────────────────────────────────

class TestFindLatestMedia:
    def test_returns_none_if_dir_missing(self, tmp_path):
        assert main.find_latest_media("no_user", "profile") is None

    def test_returns_none_if_no_files(self, tmp_path):
        d = tmp_path / "monitors" / "natgeo"
        d.mkdir(parents=True)
        import main as m
        m.MONITORS_DIR = tmp_path / "monitors"
        assert main.find_latest_media("natgeo", "profile") is None

    def test_returns_file(self, tmp_path):
        d = tmp_path / "monitors" / "natgeo"
        d.mkdir(parents=True)
        f = d / "instagram_natgeo_profile_pic.jpg"
        f.write_bytes(b"pic")
        import main as m
        m.MONITORS_DIR = tmp_path / "monitors"
        result = main.find_latest_media("natgeo", "profile")
        assert result == f

    def test_returns_most_recent_by_mtime(self, tmp_path):
        import time
        d = tmp_path / "monitors" / "natgeo"
        d.mkdir(parents=True)
        import main as m
        m.MONITORS_DIR = tmp_path / "monitors"
        old = d / "instagram_natgeo_post_20240101_120000.jpg"
        new = d / "instagram_natgeo_post_20240201_120000.jpg"
        old.write_bytes(b"old")
        time.sleep(0.01)
        new.write_bytes(b"new")
        result = main.find_latest_media("natgeo", "post")
        assert result == new

    def test_per_device_key_finds_file_in_key_folder(self, tmp_path):
        """File named with clean username lives in the key folder; pass key to find it."""
        import main as m
        m.MONITORS_DIR = tmp_path / "monitors"
        d = m.MONITORS_DIR / "natgeo_100"          # per-device folder
        d.mkdir(parents=True)
        f = d / "instagram_natgeo_profile_pic.jpg"  # named with clean username
        f.write_bytes(b"pic")
        # Without key → wrong folder (monitors/natgeo) → not found
        assert main.find_latest_media("natgeo", "profile") is None
        # With key → correct folder → found
        assert main.find_latest_media("natgeo", "profile", key="natgeo_100") == f


# ── resolve_username_and_key ──────────────────────────────────────────────────

class TestResolveUsernameAndKey:
    def test_plain_username_no_target(self):
        # Unknown identifier, no live target → returns it unchanged for both.
        assert main.resolve_username_and_key("natgeo", 100) == ("natgeo", "natgeo")

    def test_key_identifier_resolves_clean_username(self, mock_popen, no_threads):
        from unittest.mock import MagicMock
        proc = MagicMock(); proc.poll.return_value = None
        main._targets["natgeo_100"] = {
            "username": "natgeo", "shared": False, "process": proc,
            "subscribers": {100: "t"}, "stop_event": __import__("threading").Event(),
        }
        # Identifier IS the key → clean username extracted, key preserved.
        assert main.resolve_username_and_key("natgeo_100", 100) == ("natgeo", "natgeo_100")

    def test_typed_username_prefers_per_device_key(self, mock_popen, no_threads):
        from unittest.mock import MagicMock
        proc = MagicMock(); proc.poll.return_value = None
        main._targets["natgeo_100"] = {
            "username": "natgeo", "shared": False, "process": proc,
            "subscribers": {100: "t"}, "stop_event": __import__("threading").Event(),
        }
        # Typed plain username → resolves to this chat's per-device key folder.
        assert main.resolve_username_and_key("natgeo", 100) == ("natgeo", "natgeo_100")
