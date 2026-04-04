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


if __name__ == "__main__":
    unittest.main()
