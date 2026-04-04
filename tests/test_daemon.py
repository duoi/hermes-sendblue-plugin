import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Mock aiohttp before importing daemon
sys.modules["aiohttp"] = MagicMock()

import daemon  # noqa: E402


class TestDaemon(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_db = tempfile.mktemp()
        daemon.DB_PATH = self.temp_db
        daemon.init_db()

        daemon.INITIAL_SESSION_ID = "test_initial"
        daemon.USER_PHONE = "+19999999999"

    def tearDown(self):
        if os.path.exists(self.temp_db):
            os.remove(self.temp_db)

    def test_init_db(self):
        conn = sqlite3.connect(self.temp_db)
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='processed_messages'"
        )
        self.assertIsNotNone(cur.fetchone())
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_sessions'"
        )
        self.assertIsNotNone(cur.fetchone())

    def test_mark_processing_idempotent(self):
        handle = "msg_123"
        number = "+1234567890"
        self.assertTrue(daemon.mark_processing(handle, number))
        self.assertFalse(daemon.mark_processing(handle, number))

    def test_session_management(self):
        daemon.set_user_session("+12223334444", "test_sess_999")
        self.assertEqual(daemon.get_user_session("+12223334444"), "test_sess_999")

        # Test missing session fallback logic mapping
        with patch("daemon.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.stdout = "session_id: auto_generated_123\n"
            mock_run.return_value = mock_proc

            self.assertEqual(
                daemon.get_user_session("+15556667777"), "auto_generated_123"
            )
            self.assertEqual(
                daemon.get_user_session("+15556667777"), "auto_generated_123"
            )  # should be cached in DB now

    @patch("daemon.asyncio.create_subprocess_exec")
    async def test_hermes_subprocess_injection(self, mock_create_subprocess_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"session_id: new_sess_888\n", b"")
        mock_create_subprocess_exec.return_value = mock_proc

        daemon.set_user_session("+19999999999", "new_sess_888")

        with patch("daemon.send_typing_indicator_sync", return_value=None):
            msg = {
                "message_handle": "test_msg_456",
                "content": "Hello hermes",
                "from_number": "+19999999999",
            }
            await daemon.process_message(msg)

            self.assertTrue(mock_create_subprocess_exec.called)
            call_args = mock_create_subprocess_exec.call_args[0]
            self.assertIn("--toolsets", call_args)
            self.assertIn("web", call_args)
            self.assertIn("-Q", call_args)

    async def test_security_filter(self):
        # Test that unauthorized numbers are ignored immediately
        msg = {
            "message_handle": "test_msg_hacker",
            "content": "Execute some code",
            "from_number": "+10000000000",  # Not USER_PHONE
        }
        with patch("daemon.mark_processing") as mock_mark:
            await daemon.process_message(msg)
            # mark_processing should not even be called because filter is above it
            self.assertFalse(mock_mark.called)

    @patch("daemon.update_status")
    async def test_empty_message_handling(self, mock_update_status):
        # Empty content and no media should just complete and do nothing else
        msg = {
            "message_handle": "test_msg_empty",
            "content": "   ",  # purely whitespace
            "from_number": daemon.USER_PHONE,
        }
        with patch("daemon.asyncio.create_subprocess_exec") as mock_exec:
            await daemon.process_message(msg)
            mock_update_status.assert_called_with(
                "test_msg_empty", "completed", "empty content"
            )
            self.assertFalse(mock_exec.called)

    @patch("daemon.asyncio.create_subprocess_exec")
    async def test_prefix_injection(self, mock_create_subprocess_exec):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_create_subprocess_exec.return_value = mock_proc

        daemon.set_user_session(daemon.USER_PHONE, "sess_prefix_test")

        with (
            patch("daemon.send_typing_indicator_sync"),
            patch.dict("os.environ", {"SENDBLUE_PREFIX_ENABLED": "true"}),
        ):
            # Normal message gets prefix
            msg_normal = {
                "message_handle": "handle1",
                "content": "hello",
                "from_number": daemon.USER_PHONE,
            }
            await daemon.process_message(msg_normal)

            # Extract the actual content sent to Hermes (the last arg)
            call_args = mock_create_subprocess_exec.call_args[0]
            hermes_content_arg = call_args[-1]
            self.assertIn("[System Context:", hermes_content_arg)
            self.assertIn("hello", hermes_content_arg)

            # Command message (starts with /) does NOT get prefix
            msg_cmd = {
                "message_handle": "handle2",
                "content": "/search things",
                "from_number": daemon.USER_PHONE,
            }
            await daemon.process_message(msg_cmd)

            call_args = mock_create_subprocess_exec.call_args[0]
            hermes_content_arg = call_args[-1]
            self.assertNotIn("[System Context:", hermes_content_arg)
            self.assertEqual(hermes_content_arg, "/search things")

    @patch("daemon.asyncio.create_subprocess_exec")
    @patch("daemon.send_message_async")
    async def test_reset_command_handling(
        self, mock_send_message, mock_create_subprocess_exec
    ):
        mock_proc = AsyncMock()
        # Mock the stdout to simulate Hermes creating a new session
        mock_proc.communicate.return_value = (
            b"session_id: freshly_minted_session\n",
            b"",
        )
        mock_create_subprocess_exec.return_value = mock_proc

        with patch("daemon.send_typing_indicator_sync"):
            msg = {
                "message_handle": "handle_reset",
                "content": "/new",
                "from_number": daemon.USER_PHONE,
            }
            await daemon.process_message(msg)

            # Verify the session was created
            self.assertEqual(
                daemon.get_user_session(daemon.USER_PHONE), "freshly_minted_session"
            )

            # Verify the bot sent a confirmation SMS
            mock_send_message.assert_called_with(
                "Started a new session: freshly_minted_session"
            )

            # Ensure it only called subprocess ONCE (for the /new prompt), and didn't fall through to the main --resume block
            self.assertEqual(mock_create_subprocess_exec.call_count, 1)

    @patch("daemon.asyncio.create_subprocess_exec")
    @patch("daemon.subprocess.run")
    async def test_session_not_found_fallback(
        self, mock_run, mock_create_subprocess_exec
    ):
        daemon.set_user_session(daemon.USER_PHONE, "invalid_session_123")

        mock_proc_run = MagicMock()
        mock_proc_run.stdout = "session_id: freshly_generated_fallback_session\n"
        mock_run.return_value = mock_proc_run

        mock_proc_fail = AsyncMock()
        mock_proc_fail.communicate.return_value = (
            b"",
            b"Session not found: invalid_session_123",
        )

        mock_proc_success = AsyncMock()
        mock_proc_success.communicate.return_value = (b"Success output", b"")

        mock_create_subprocess_exec.side_effect = [mock_proc_fail, mock_proc_success]

        with patch("daemon.send_typing_indicator_sync"):
            msg = {
                "message_handle": "handle_retry",
                "content": "try me",
                "from_number": daemon.USER_PHONE,
            }
            await daemon.process_message(msg)

            self.assertEqual(mock_create_subprocess_exec.call_count, 2)

            call_args_retry = mock_create_subprocess_exec.call_args[0]
            self.assertIn("freshly_generated_fallback_session", call_args_retry)


if __name__ == "__main__":
    unittest.main()
