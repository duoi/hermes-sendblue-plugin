import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Add plugin dir to path so we can import tools
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tools
import json


class TestSendblueTools(unittest.TestCase):
    def setUp(self):
        # Clear environment for a clean slate
        if "SENDBLUE_ADMIN_PHONES" in os.environ:
            del os.environ["SENDBLUE_ADMIN_PHONES"]
        if "USER_PHONE" in os.environ:
            del os.environ["USER_PHONE"]

    def test_admin_access_unauthorized(self):
        """
        Tests Active LLM Tools (Admin RCE/Spam Prevention).

        ARCHITECTURAL CONTEXT:
        We must safely register proactive outbound tools (like `sendblue_send_message`).
        Any outbound tools MUST have a hardcoded check against a `SENDBLUE_ADMIN_PHONES`
        env var inside the tool function itself, otherwise anyone texting the bot could
        trick it into using the local user's Sendblue quota to spam third parties.
        """
        os.environ["SENDBLUE_ADMIN_PHONES"] = "+19999999999"

        # An attacker task_id
        task_id = "+10000000000"

        result_json = tools.sendblue_send_message(
            {"number": "+15555555555", "message": "spam"}, task_id=task_id
        )
        result = json.loads(result_json)

        self.assertIn("Unauthorized", result.get("error", ""))

    def test_admin_access_authorized(self):
        os.environ["SENDBLUE_ADMIN_PHONES"] = "+19999999999"
        task_id = "+19999999999"

        with patch("tools.requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"status": "sent"}
            mock_post.return_value = mock_resp

            # Needs dummy keys to pass the key check
            with patch.dict(
                os.environ,
                {
                    "SENDBLUE_API_KEY": "x",
                    "SENDBLUE_API_SECRET": "x",
                    "SENDBLUE_PHONE": "x",
                },
            ):
                result_json = tools.sendblue_send_message(
                    {"number": "+15555555555", "message": "hello"}, task_id=task_id
                )
                result = json.loads(result_json)

                self.assertEqual(result.get("status"), "sent")
                self.assertTrue(mock_post.called)


if __name__ == "__main__":
    unittest.main()
