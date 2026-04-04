"""Sendblue Plugin for Hermes.

Provides typing indicators automatically if SENDBLUE_ACTIVE_USER_PHONE is set.
"""

import os
import logging
import threading
from .tools import register as register_tools

logger = logging.getLogger(__name__)


def _do_send_indicator():
    # Attempt to load plugin-specific .env if global one lacks keys
    env_path = os.path.expanduser("~/.hermes/plugins/sendblue/.env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    if k not in os.environ:
                        os.environ[k] = v.strip("'\"")

    phone = os.environ.get("SENDBLUE_ACTIVE_USER_PHONE")

    api_key = os.environ.get("SENDBLUE_API_KEY")
    api_secret = os.environ.get("SENDBLUE_API_SECRET")
    from_num = os.environ.get("SENDBLUE_PHONE")

    if phone and api_key and api_secret and from_num:
        try:
            import requests

            url = "https://api.sendblue.co/api/send-typing-indicator"
            headers = {
                "sb-api-key-id": api_key,
                "sb-api-secret-key": api_secret,
                "Content-Type": "application/json",
            }
            requests.post(
                url,
                headers=headers,
                json={"number": phone, "from_number": from_num},
                timeout=5,
            )
            logger.debug(f"Sent Sendblue typing indicator to {phone}")
        except Exception as e:
            logger.error(f"Failed to send Sendblue typing indicator: {e}")


def on_pre_llm_call(session_id, **kwargs):
    # Run in background to prevent blocking Hermes execution
    threading.Thread(target=_do_send_indicator).start()


def register(ctx):
    # Load plugin-specific .env variables at startup to make them available for tools
    env_path = os.path.expanduser("~/.hermes/plugins/sendblue/.env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    k, v = line.strip().split("=", 1)
                    if k not in os.environ:
                        os.environ[k] = v.strip("'\"")

    ctx.register_hook("pre_llm_call", on_pre_llm_call)

    # Register Sendblue API tools
    register_tools(ctx)
