import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock, AsyncMock


# Helper for mocking async iterators
async def async_generator(items):
    for item in items:
        yield item


import sys  # noqa: E402

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
        """
        Tests the multi-tenant SQLite session isolation.

        ARCHITECTURAL CONTEXT:
        Do not use a single flat file (`sendblue_session.txt`) to store the current session ID.
        If multiple people text the bot, their messages will all be piped into the exact same
        Hermes session, causing the AI to hallucinate and leak information between conversations.
        We MUST use the SQLite table `user_sessions(phone_number, session_id)` to map each
        unique sender to their own isolated Hermes session.
        """
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
        """
        Tests that `--toolsets hermes-cli` and `-Q` are always injected into the CLI subprocess.

        ARCHITECTURAL CONTEXT:
        If you do not pass `--toolsets hermes-cli`, the agent will lack `browser_navigate`. When the
        agent is prompted "You are running headlessly" and sees no browser tools, it will
        hallucinate that headless environments cannot use the internet. You MUST explicitly
        inject `--toolsets hermes-cli` to prevent this bug.
        """
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
            self.assertIn("hermes-cli", call_args)
            self.assertIn("-Q", call_args)

    async def test_security_filter(self):
        """
        Tests inbound RCE prevention via strict phone number gating.

        ARCHITECTURAL CONTEXT:
        Because Hermes can execute shell commands, the background daemon is effectively an
        unauthenticated gateway to the local machine. We MUST explicitly check the incoming
        `from_number` against an authorized `USER_PHONE` before handing the text to the Hermes
        subprocess. Do not rely solely on the LLM's system prompt to prevent abuse.
        """
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
        """
        Tests the hidden system prompt injection for headless execution.

        ARCHITECTURAL CONTEXT:
        The background daemon cannot interact with `prompt_toolkit` to stream interactive Y/n
        approvals over SMS. If a dangerous command is triggered or if it uses the `clarify` tool,
        the subprocess will hang permanently. The injected prefix explicitly bans interactive
        tools while green-lighting read-only tasks proactively.
        """
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

    @patch("daemon.update_status")
    async def test_media_download_size_limit(self, mock_update_status):
        """
        Tests OOM DOS prevention during media ingestion.

        ARCHITECTURAL CONTEXT:
        An attacker could send a 10GB media file via MMS. If we use `await resp.read()`, the
        daemon will attempt to load the entire file into RAM, causing an Out-Of-Memory crash.
        We MUST stream downloads asynchronously in chunks (`iter_chunked`) and strictly enforce
        `MAX_MEDIA_SIZE_BYTES`, aborting immediately if the limit is exceeded.
        """
        # We need to simulate aiohttp downloading a file that exceeds the size limit
        msg = {
            "message_handle": "test_msg_large_media",
            "content": "",
            "media_url": "https://example.com/huge.mp4",
            "from_number": daemon.USER_PHONE,
        }

        # We need to mock the aiohttp ClientSession to yield chunks
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.content.iter_chunked = MagicMock(
            return_value=async_generator([b"123456", b"789012"])
        )

        mock_get_ctx = AsyncMock()
        mock_get_ctx.__aenter__.return_value = mock_resp

        mock_session_instance = MagicMock()
        mock_session_instance.get.return_value = mock_get_ctx
        mock_session_instance.__aenter__.return_value = mock_session_instance

        with (
            patch("aiohttp.ClientSession", return_value=mock_session_instance),
            patch("daemon.asyncio.create_subprocess_exec") as mock_exec,
            patch("daemon.send_typing_indicator_sync"),
        ):
            # Temporarily set the size limit to 10 bytes
            original_limit = daemon.MAX_MEDIA_SIZE_BYTES
            daemon.MAX_MEDIA_SIZE_BYTES = 10

            try:
                await daemon.process_message(msg)

                # Should not have called subprocess because it failed
                self.assertFalse(mock_exec.called)

                # Should have completed with error
                mock_update_status.assert_called_with(
                    "test_msg_large_media", "failed", "media file too large"
                )
            finally:
                daemon.MAX_MEDIA_SIZE_BYTES = original_limit

    @patch("daemon.sqlite3.connect")
    @patch("daemon.asyncio.create_subprocess_exec")
    @patch("daemon.MediaUploader")
    async def test_outbound_media_quiet_mode_extraction(
        self, mock_uploader_class, mock_exec, mock_sqlite_connect
    ):
        """
        Tests Voice & Media Routing (The Quiet Mode Bug).

        ARCHITECTURAL CONTEXT:
        Do NOT run Whisper locally; it consumes RAM and duplicates Hermes's native pipeline.
        Furthermore, when Hermes is run as a subprocess with `-Q` (Quiet Mode), it suppresses
        `MEDIA:` tags from stdout. The only way to extract generated audio/images to text
        back to the user is to manually query `~/.hermes/state.db` for the `tool` message
        generated during that conversational turn. We MUST use `boto3` and private S3/R2
        buckets for outbound media rather than public URL shorteners.
        """
        daemon.set_user_session(daemon.USER_PHONE, "sess_media")

        # Mock Hermes returning normally
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"Normal text reply", b"")
        mock_exec.return_value = mock_proc

        # Mock the state.db query to return a media tool message
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # the daemon runs three queries: get user id, get media tools, get all non-tool roles
        # We will configure side_effects to return what it expects.

        def execute_side_effect(query, params=()):
            if "role = 'assistant'" in query:
                mock_cur.fetchone.return_value = (10, "Normal text reply")
            elif "role = 'user'" in query:
                mock_cur.fetchone.return_value = (5,)  # user message id
            elif "role = 'tool'" in query:
                # Return a fake generated image
                mock_cur.fetchall.return_value = [
                    ('{"media_tag": "MEDIA: /tmp/generated.png"}',)
                ]
            else:
                mock_cur.fetchone.return_value = None
            return mock_cur

        mock_cur.execute.side_effect = execute_side_effect
        mock_conn.cursor.return_value = mock_cur
        mock_sqlite_connect.return_value = mock_conn

        # Mock the media uploader
        mock_uploader = MagicMock()
        mock_uploader.upload.return_value = "https://s3.private/generated.png"
        daemon.MEDIA_UPLOADER = mock_uploader

        with (
            patch("daemon.send_typing_indicator_sync"),
            patch("daemon.send_message_async") as mock_send_sms,
        ):
            msg = {
                "message_handle": "handle_media",
                "content": "Draw an image",
                "from_number": daemon.USER_PHONE,
            }
            await daemon.process_message(msg)

            # Verify the bot extracted the hidden media tag from state.db and sent it
            mock_send_sms.assert_called_with(
                "Normal text reply\nMEDIA: /tmp/generated.png"
            )

    @patch("daemon.asyncio.sleep")
    @patch("daemon.send_typing_indicator_sync")
    @patch("daemon.asyncio.create_subprocess_exec")
    async def test_typing_indicator_timing(self, mock_exec, mock_typing, mock_sleep):
        """
        Tests Typing Indicator Latency (The Illusion of Presence).

        ARCHITECTURAL CONTEXT:
        SendBlue's `/api/send-typing-indicator` requires `from_number` or fails 400.
        Do *not* calculate an artificial delay after generation to fake human typing.
        Instead, fire the typing indicator immediately, `await asyncio.sleep(2.0)`, and
        *then* start the LLM subprocess. This guarantees the bubble appears on the iPhone
        before the instant reply arrives without blocking the LLM generation loop.
        """
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"Hi", b"")
        mock_exec.return_value = mock_proc

        # We use a MagicMock to track order of execution
        manager = MagicMock()
        manager.attach_mock(mock_typing, "typing")
        manager.attach_mock(mock_sleep, "sleep")
        manager.attach_mock(mock_exec, "exec")

        msg = {
            "message_handle": "handle_timing",
            "content": "hello",
            "from_number": daemon.USER_PHONE,
        }
        await daemon.process_message(msg)

        # Verify execution order: typing -> sleep -> subprocess
        expected_calls = [
            unittest.mock.call.typing(),
            unittest.mock.call.sleep(2.0),
            unittest.mock.call.exec(*mock_exec.call_args[0], **mock_exec.call_args[1]),
        ]
        manager.assert_has_calls(expected_calls, any_order=False)

    def test_troubleshooting_queries(self):
        """
        Tests Production Troubleshooting Queries (Debugging).

        ARCHITECTURAL CONTEXT:
        When the daemon fails silently, developers rely on hardcoded raw SQL strings to debug.
        `sqlite3 ~/.hermes/sendblue_daemon.db "SELECT message_handle, status, error_log FROM processed_messages WHERE status='failed' ..."`
        We MUST ensure these specific query structures remain valid against the schema.
        """
        conn = sqlite3.connect(self.temp_db)
        cur = conn.cursor()

        # The specific debug query from the docs
        try:
            cur.execute(
                "SELECT message_handle, status, error_log FROM processed_messages WHERE status='failed' ORDER BY created_at DESC LIMIT 5;"
            )
            rows = cur.fetchall()
            self.assertEqual(len(rows), 0)
        except sqlite3.OperationalError as e:
            self.fail(f"Troubleshooting query is invalid against schema: {e}")

    def test_architecture_boundaries(self):
        """
        Tests Core Architecture Boundaries (Detached vs Native).

        ARCHITECTURAL CONTEXT:
        There is a fatal flaw in Native Gateway Platforms: modifying core source code causes
        git merge conflicts during `hermes update`. This daemon MUST remain a 100% detached,
        update-safe polling script in `~/.hermes/plugins/`. It MUST NOT import from
        `hermes-agent.gateway.platforms` or hook into the native framework directly.
        """
        daemon_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "daemon.py")
        )
        with open(daemon_path, "r") as f:
            content = f.read()

        self.assertNotIn(
            "from hermes_cli.gateway",
            content,
            "Daemon violates boundary by importing native gateway modules.",
        )
        self.assertNotIn(
            "from agent",
            content,
            "Daemon violates boundary by importing core agent modules.",
        )


if __name__ == "__main__":
    unittest.main()
