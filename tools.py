"""
Active LLM Tools for Sendblue.
Allows the AI to proactively send messages and read message history.
Restricted to Admin use by default via SENDBLUE_ADMIN_PHONES env var.
"""
import os
import json
import logging
import requests
from typing import Dict, Any

logger = logging.getLogger(__name__)

def _get_admin_phones() -> list[str]:
    # Try to get explicit admins list (comma separated)
    admins_env = os.environ.get("SENDBLUE_ADMIN_PHONES", "")
    if admins_env:
        return [p.strip() for p in admins_env.split(",") if p.strip()]
    
    # Fallback to the primary user phone
    primary = os.environ.get("USER_PHONE")
    if primary:
        return [primary.strip()]
        
    return []

def check_admin_access(task_id: str) -> bool:
    """
    Check if the current session/task is authorized to use Sendblue tools.
    task_id is typically the chat_id/phone number in Gateway mode, 
    but in our detached daemon mode, we need to be careful.
    If we are running in the CLI, we generally trust the user running the CLI.
    """
    # If running in a true interactive CLI or our daemon (which we own), we allow it.
    # But if someone is trying to use this from a different phone via gateway, we block it.
    admins = _get_admin_phones()
    
    # If no admins are configured at all, default to restrictive
    if not admins:
        logger.warning("No SENDBLUE_ADMIN_PHONES or USER_PHONE configured. Rejecting tool access.")
        return False

    # In our detached daemon architecture, the AI is running under the local user's shell.
    # The actual task_id might just be the session ID (e.g. 20260403_...).
    # If the system explicitly passes a phone number as task_id, we check it.
    if task_id and task_id.startswith("+"):
        if task_id not in admins:
            logger.warning(f"Unauthorized Sendblue tool access attempted by {task_id}")
            return False
            
    return True

def sendblue_send_message(args: Dict[str, Any], task_id: str = None, **kwargs) -> str:
    if not check_admin_access(task_id):
        return json.dumps({"error": "Unauthorized: Sendblue tools are restricted to admin phone numbers."})
        
    number = args.get("number")
    message = args.get("message")
    
    # Check both global environ and plugin .env
    api_key = os.environ.get("SENDBLUE_API_KEY")
    api_secret = os.environ.get("SENDBLUE_API_SECRET")
    from_num = os.environ.get("SENDBLUE_PHONE")
    
    if not all([api_key, api_secret, from_num]):
        return json.dumps({"error": "Sendblue API keys (SENDBLUE_API_KEY, SENDBLUE_API_SECRET, SENDBLUE_PHONE) not configured in environment."})
        
    try:
        url = "https://api.sendblue.co/api/send-message"
        headers = {"sb-api-key-id": api_key, "sb-api-secret-key": api_secret, "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json={"number": number, "content": message, "from_number": from_num})
        return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": str(e)})

def sendblue_list_messages(args: Dict[str, Any], task_id: str = None, **kwargs) -> str:
    if not check_admin_access(task_id):
        return json.dumps({"error": "Unauthorized: Sendblue tools are restricted to admin phone numbers."})
        
    limit = args.get("limit", 10)
    api_key = os.environ.get("SENDBLUE_API_KEY")
    api_secret = os.environ.get("SENDBLUE_API_SECRET")
    
    if not all([api_key, api_secret]):
        return json.dumps({"error": "Sendblue API keys not configured."})
    
    try:
        url = f"https://api.sendblue.co/api/v2/messages?limit={limit}"
        headers = {"sb-api-key-id": api_key, "sb-api-secret-key": api_secret, "Content-Type": "application/json"}
        resp = requests.get(url, headers=headers)
        return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": str(e)})

def register(ctx):
    ctx.register_tool(
        name="sendblue_send_message",
        toolset="sendblue",
        schema={
            "name": "sendblue_send_message",
            "description": "Send an SMS or iMessage to an arbitrary phone number via the Sendblue API. Use this to proactively text people or respond to inquiries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "string", "description": "The destination E.164 phone number (e.g. +14155551234)"},
                    "message": {"type": "string", "description": "The text content of the message."}
                },
                "required": ["number", "message"]
            }
        },
        handler=sendblue_send_message
    )
    
    ctx.register_tool(
        name="sendblue_list_messages",
        toolset="sendblue",
        schema={
            "name": "sendblue_list_messages",
            "description": "List recent inbound and outbound SMS/iMessages handled by the Sendblue API. Useful for retrieving conversation history or verifying message delivery.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of recent messages to retrieve (default 10).", "default": 10}
                }
            }
        },
        handler=sendblue_list_messages
    )
