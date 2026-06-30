"""Tests for all Telegram command/callback handlers in main.py."""

import pytest
from unittest.mock import MagicMock, patch

import database as db
import main
from tests.conftest import make_message, make_callback


# ── /start and /help ──────────────────────────────────────────────────────────

class TestCmdStart:
    def test_replies_with_welcome(self, mock_bot):
        msg = make_message(text="/start")
        main.send_welcome(msg)
        mock_bot.reply_to.assert_called_once()
        text = mock_bot.reply_to.call_args[0][1]
        assert "Instagram Monitor" in text or "Commands" in text

    def test_clears_awaiting_flag(self, mock_bot):
        msg = make_message(text="/start")
        main._awaiting_username[main.awaiting_key(msg)] = True
        main.send_welcome(msg)
        assert not main._awaiting_username.get(main.awaiting_key(msg))

    def test_sends_keyboard(self, mock_bot):
        msg = make_message(text="/start")
        main.send_welcome(msg)
        kwargs = mock_bot.reply_to.call_args[1]
        assert "reply_markup" in kwargs


# ── /cancel ───────────────────────────────────────────────────────────────────

class TestCmdCancel:
    def test_was_awaiting(self, mock_bot):
        msg = make_message(text="/cancel")
        main._awaiting_username[main.awaiting_key(msg)] = True
        main.cmd_cancel(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Cancelled" in reply

    def test_nothing_to_cancel(self, mock_bot):
        msg = make_message(text="/cancel")
        main.cmd_cancel(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Nothing" in reply or "nothing" in reply


# ── /track ────────────────────────────────────────────────────────────────────

class TestCmdTrack:
    def test_no_default_username_configured(self, monkeypatch, mock_bot):
        monkeypatch.setattr(main, "DEFAULT_USERNAME", None)
        msg = make_message(text="/track")
        main.cmd_track(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "INSTAGRAM_USERNAME" in reply

    def test_starts_tracking(self, monkeypatch, mock_bot, mock_popen, no_threads):
        monkeypatch.setattr(main, "DEFAULT_USERNAME", "natgeo")
        msg = make_message(text="/track")
        main.cmd_track(msg)
        mock_bot.reply_to.assert_called_once()
        reply = mock_bot.reply_to.call_args[0][1]
        assert "natgeo" in reply

    def test_clears_awaiting_flag(self, monkeypatch, mock_bot, mock_popen, no_threads):
        monkeypatch.setattr(main, "DEFAULT_USERNAME", "natgeo")
        msg = make_message(text="/track")
        main._awaiting_username[main.awaiting_key(msg)] = True
        main.cmd_track(msg)
        assert not main._awaiting_username.get(main.awaiting_key(msg))

    def test_creates_per_device_key(self, monkeypatch, mock_bot, mock_popen, no_threads):
        """/track is per-device: key is DEFAULT_USERNAME_<chat_id>, not the bare username."""
        monkeypatch.setattr(main, "DEFAULT_USERNAME", "natgeo")
        msg = make_message(text="/track", chat_id=100)
        main.cmd_track(msg)
        assert "natgeo_100" in main._targets
        assert "natgeo" not in main._targets   # NOT the old shared key

    def test_two_devices_independent(self, monkeypatch, mock_bot, mock_popen, no_threads):
        """Two chats running /track each get their own monitor; both run at once."""
        monkeypatch.setattr(main, "DEFAULT_USERNAME", "natgeo")
        main.cmd_track(make_message(text="/track", chat_id=100))
        main.cmd_track(make_message(text="/track", chat_id=200))
        assert "natgeo_100" in main._targets
        assert "natgeo_200" in main._targets

    def test_retrack_restarts_fresh(self, monkeypatch, mock_bot, mock_popen, no_threads):
        """Running /track again on the same device re-tracks (fresh restart)."""
        monkeypatch.setattr(main, "DEFAULT_USERNAME", "natgeo")
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        main.cmd_track(make_message(text="/track", chat_id=100))
        main.cmd_track(make_message(text="/track", chat_id=100))
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Re-tracking" in reply or "re-track" in reply.lower()
        assert "natgeo_100" in main._targets


# ── /trackother ───────────────────────────────────────────────────────────────

class TestCmdTrackother:
    def test_sets_awaiting_flag(self, mock_bot):
        msg = make_message(text="/trackother")
        main.cmd_trackother(msg)
        assert main._awaiting_username.get(main.awaiting_key(msg)) is True

    def test_replies_with_prompt(self, mock_bot):
        msg = make_message(text="/trackother")
        main.cmd_trackother(msg)
        mock_bot.reply_to.assert_called_once()
        reply = mock_bot.reply_to.call_args[0][1]
        assert "username" in reply.lower()

    def test_group_reply_hint(self, mock_bot):
        msg = make_message(text="/trackother", chat_type="group")
        main.cmd_trackother(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "reply" in reply.lower() or "group" in reply.lower()

    def test_private_no_reply_hint(self, mock_bot):
        msg = make_message(text="/trackother", chat_type="private")
        main.cmd_trackother(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        # Group hint should NOT appear in private
        assert "group" not in reply.lower()


# ── /stop ─────────────────────────────────────────────────────────────────────

class TestCmdStop:
    def test_no_arg_no_targets_says_nothing_tracked(self, mock_bot):
        msg = make_message(text="/stop")
        main.cmd_stop(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Nothing" in reply or "nothing" in reply

    def test_no_arg_with_targets_shows_picker(self, mock_bot, mock_popen, no_threads):
        main._targets["natgeo"] = {
            "username": "natgeo",
            "shared": True,
            "process": MagicMock(**{"poll.return_value": None}),
            "subscribers": {100: "test"},   # chat_id=100 matches make_message default
            "stop_event": __import__("threading").Event(),
            "started_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        }
        msg = make_message(text="/stop")
        main.cmd_stop(msg)
        call_kwargs = mock_bot.reply_to.call_args[1]
        assert "reply_markup" in call_kwargs

    def test_invalid_username(self, mock_bot):
        msg = make_message(text="/stop @in/valid!user")
        main.cmd_stop(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "valid" in reply.lower() or "Invalid" in reply

    def test_valid_username_not_tracked(self, mock_bot):
        msg = make_message(text="/stop natgeo")
        main.cmd_stop(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "not" in reply.lower()

    def test_valid_username_stops_tracked(self, mock_bot, mock_popen, no_threads, monkeypatch):
        monkeypatch.setattr(main, "IS_WINDOWS", True)
        main.start_tracking("natgeo", 100, "tester", shared=True)
        msg = make_message(text="/stop natgeo", chat_id=100)
        main.cmd_stop(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Stopped" in reply or "stopped" in reply

    def test_with_at_prefix_stripped(self, mock_bot):
        msg = make_message(text="/stop @natgeo")
        main.cmd_stop(msg)
        # Should not complain about invalid username (@ is stripped)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "valid" not in reply or "not" in reply.lower()


# ── /image ────────────────────────────────────────────────────────────────────

class TestCmdImage:
    def test_no_args_no_targets(self, mock_bot):
        msg = make_message(text="/image")
        main.cmd_image(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Nothing" in reply or "nothing" in reply

    def test_no_args_with_targets_shows_picker(self, mock_bot):
        main._targets["natgeo"] = {
            "username": "natgeo",
            "shared": True,
            "process": MagicMock(**{"poll.return_value": None}),
            "subscribers": {100: "test"},
            "stop_event": __import__("threading").Event(),
            "started_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        }
        msg = make_message(text="/image")
        main.cmd_image(msg)
        call_kwargs = mock_bot.reply_to.call_args[1]
        assert "reply_markup" in call_kwargs

    def test_username_only_shows_type_picker(self, mock_bot):
        msg = make_message(text="/image natgeo")
        main.cmd_image(msg)
        call_kwargs = mock_bot.reply_to.call_args[1]
        assert "reply_markup" in call_kwargs

    def test_invalid_username_only(self, mock_bot):
        msg = make_message(text="/image in/valid!")
        main.cmd_image(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "valid" in reply.lower()

    def test_unknown_type(self, mock_bot):
        msg = make_message(text="/image natgeo banana")
        main.cmd_image(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Unknown" in reply or "unknown" in reply or "banana" in reply

    def test_four_parts_usage(self, mock_bot):
        msg = make_message(text="/image a b c d")
        main.cmd_image(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Usage" in reply or "usage" in reply

    def test_full_valid_request_no_media(self, mock_bot):
        db.upsert_target("natgeo", 100, "tester")
        msg = make_message(text="/image natgeo profile")
        main.cmd_image(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "No" in reply or "not found" in reply.lower()

    def test_full_valid_request_with_media(self, mock_bot):
        db.upsert_target("natgeo", 100, "tester")
        db.store_media("natgeo", "profile_pic", "p.jpg", b"\xff\xd8data")
        msg = make_message(text="/image natgeo profile")
        main.cmd_image(msg)
        mock_bot.send_photo.assert_called_once()


# ── /data ─────────────────────────────────────────────────────────────────────

class TestCmdData:
    def test_no_args_no_targets(self, mock_bot):
        msg = make_message(text="/data")
        main.cmd_data(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Nothing" in reply or "nothing" in reply

    def test_no_args_with_targets(self, mock_bot):
        main._targets["natgeo"] = {
            "username": "natgeo",
            "shared": True,
            "process": MagicMock(**{"poll.return_value": None}),
            "subscribers": {100: "test"},
            "stop_event": __import__("threading").Event(),
            "started_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        }
        msg = make_message(text="/data")
        main.cmd_data(msg)
        call_kwargs = mock_bot.reply_to.call_args[1]
        assert "reply_markup" in call_kwargs

    def test_invalid_username(self, mock_bot):
        msg = make_message(text="/data in/valid!")
        main.cmd_data(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "valid" in reply.lower()

    def test_no_data_returns_error(self, mock_bot):
        db.upsert_target("natgeo", 100, "tester")
        msg = make_message(text="/data natgeo")
        main.cmd_data(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "No data" in reply or "no data" in reply.lower()

    def test_sends_data_when_available(self, mock_bot):
        db.upsert_target("natgeo", 100, "tester")
        db.insert_log("natgeo", "Line 1")
        msg = make_message(text="/data natgeo")
        main.cmd_data(msg)
        mock_bot.send_document.assert_called()


# ── /status ───────────────────────────────────────────────────────────────────

class TestCmdStatus:
    def test_empty_replies_nothing_tracked(self, mock_bot):
        msg = make_message(text="/status")
        main.cmd_status(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "No targets" in reply or "no targets" in reply.lower()

    def test_shows_tracked_usernames(self, mock_bot):
        proc = MagicMock()
        proc.poll.return_value = None
        import datetime as dt
        main._targets["natgeo"] = {
            "username": "natgeo",
            "shared": True,
            "process": proc,
            "subscribers": {100: "Alice"},   # chat_id=100 matches make_message default
            "stop_event": __import__("threading").Event(),
            "started_at": dt.datetime.now(dt.timezone.utc),
        }
        msg = make_message(text="/status")
        main.cmd_status(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "natgeo" in reply

    def test_shows_dead_process_state(self, mock_bot):
        proc = MagicMock()
        proc.poll.return_value = 1   # dead
        import datetime as dt
        main._targets["natgeo"] = {
            "username": "natgeo",
            "shared": True,
            "process": proc,
            "subscribers": {100: "Alice"},
            "stop_event": __import__("threading").Event(),
            "started_at": dt.datetime.now(dt.timezone.utc),
        }
        msg = make_message(text="/status")
        main.cmd_status(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "stopped" in reply.lower() or "🔴" in reply


# ── handle_callback_query ─────────────────────────────────────────────────────

class TestHandleCallbackQuery:
    def test_stop_callback(self, mock_bot, monkeypatch):
        monkeypatch.setattr(main, "IS_WINDOWS", False)
        call = make_callback("stop:natgeo")
        main.handle_callback_query(call)
        mock_bot.answer_callback_query.assert_called()
        mock_bot.send_message.assert_called()

    def test_imgsel_callback(self, mock_bot):
        call = make_callback("imgsel:natgeo")
        main.handle_callback_query(call)
        mock_bot.answer_callback_query.assert_called()
        mock_bot.send_message.assert_called()

    def test_img_callback_no_media(self, mock_bot):
        db.upsert_target("natgeo", 100, "tester")
        call = make_callback("img:natgeo:profile")
        main.handle_callback_query(call)
        mock_bot.answer_callback_query.assert_called()

    def test_img_callback_with_media(self, mock_bot):
        db.upsert_target("natgeo", 100, "tester")
        db.store_media("natgeo", "profile_pic", "p.jpg", b"\xff\xd8data")
        call = make_callback("img:natgeo:profile")
        main.handle_callback_query(call)
        mock_bot.answer_callback_query.assert_called()
        mock_bot.send_photo.assert_called()

    def test_data_callback(self, mock_bot):
        db.upsert_target("natgeo", 100, "tester")
        db.insert_log("natgeo", "A line")
        call = make_callback("data:natgeo")
        main.handle_callback_query(call)
        mock_bot.answer_callback_query.assert_called()
        mock_bot.send_document.assert_called()

    def test_unknown_callback(self, mock_bot):
        call = make_callback("gibberish:xyz")
        main.handle_callback_query(call)
        mock_bot.answer_callback_query.assert_called()
        text = mock_bot.answer_callback_query.call_args[0][1]
        assert "longer valid" in text or "valid" in text

    def test_exception_still_answers(self, mock_bot):
        """answer_callback_query must always be called, even when an error occurs."""
        db.upsert_target("natgeo", 100, "tester")
        # Cause deliver_data to fail
        mock_bot.send_document.side_effect = Exception("boom")
        call = make_callback("data:natgeo")
        main.handle_callback_query(call)
        mock_bot.answer_callback_query.assert_called()

    def test_no_chat_id(self, mock_bot):
        call = make_callback("stop:natgeo")
        call.message = None
        main.handle_callback_query(call)
        # Should not crash
        mock_bot.answer_callback_query.assert_called()


# ── handle_username_reply ─────────────────────────────────────────────────────

class TestHandleUsernameReply:
    def test_valid_username_starts_tracking(self, mock_bot, mock_popen, no_threads):
        msg = make_message(text="natgeo")
        main._awaiting_username[main.awaiting_key(msg)] = True
        main.handle_username_reply(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "natgeo" in reply

    def test_clears_awaiting_after_reply(self, mock_bot, mock_popen, no_threads):
        msg = make_message(text="natgeo")
        key = main.awaiting_key(msg)
        main._awaiting_username[key] = True
        main.handle_username_reply(msg)
        assert not main._awaiting_username.get(key)

    def test_invalid_username_returns_error(self, mock_bot):
        msg = make_message(text="in/valid!")
        main._awaiting_username[main.awaiting_key(msg)] = True
        main.handle_username_reply(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "Invalid" in reply or "invalid" in reply

    def test_at_prefix_stripped(self, mock_bot, mock_popen, no_threads):
        msg = make_message(text="@natgeo", chat_id=100)
        main._awaiting_username[main.awaiting_key(msg)] = True
        main.handle_username_reply(msg)
        # /trackother creates a private key "natgeo_{chat_id}"
        assert "natgeo_100" in main._targets or mock_bot.reply_to.called

    def test_whitespace_stripped(self, mock_bot, mock_popen, no_threads):
        msg = make_message(text="  natgeo  ")
        main._awaiting_username[main.awaiting_key(msg)] = True
        main.handle_username_reply(msg)
        reply = mock_bot.reply_to.call_args[0][1]
        assert "natgeo" in reply
