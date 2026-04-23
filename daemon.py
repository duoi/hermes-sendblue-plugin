import os
import sys
import time
import asyncio
import subprocess
import requests
import re
import sqlite3
import shutil
from datetime import datetime, timezone


def get_hermes_bin():
    path = shutil.which("hermes")
    if path:
        return path
    local_bin = os.path.expanduser("~/.local/bin/hermes")
    if os.path.exists(local_bin):
        return local_bin
    return "hermes"


# Boot check
if not shutil.which("ffmpeg"):
    print("WARNING: ffmpeg not found in PATH. Outbound audio formatting may fail.")

# Environment setup
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

env_path = os.path.expanduser("~/.hermes/.env")
plugin_env_path = os.path.expanduser("~/.hermes/plugins/sendblue/.env")
for path in [env_path, plugin_env_path]:
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                if "=" in line and not line.startswith("#"):
                    key, val = line.strip().split("=", 1)
                    os.environ[key] = val.strip("'\"")

API_KEY = os.environ.get("SENDBLUE_API_KEY")
API_SECRET = os.environ.get("SENDBLUE_API_SECRET")
USER_PHONES = [
    p.strip() for p in os.environ.get("USER_PHONE", "").split(",") if p.strip()
]
USER_PHONE = USER_PHONES[0] if USER_PHONES else None
SENDBLUE_TOOLSETS = os.environ.get("SENDBLUE_TOOLSETS", "hermes-cli")
MAX_MEDIA_SIZE_BYTES = (
    int(os.environ.get("SENDBLUE_MAX_MEDIA_SIZE_MB", 50)) * 1024 * 1024
)

SENDBLUE_PHONE = os.environ.get("SENDBLUE_PHONE")
INITIAL_SESSION_ID = os.environ.get("INITIAL_SESSION_ID", "new_session")

if not all([API_KEY, API_SECRET, USER_PHONE, SENDBLUE_PHONE]):
    print(
        "Error: SENDBLUE_API_KEY, SENDBLUE_API_SECRET, USER_PHONE, and SENDBLUE_PHONE must be set in environment."
    )
    sys.exit(1)

# Database Setup
DB_PATH = os.path.expanduser("~/.hermes/sendblue_daemon.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
        message_handle TEXT PRIMARY KEY,
        sender_number TEXT,
        status TEXT DEFAULT 'processing',
        error_log TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS user_sessions (
        phone_number TEXT PRIMARY KEY,
        session_id TEXT
    )""")
    conn.commit()
    conn.close()


def mark_processing(handle, number):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO processed_messages (message_handle, sender_number) VALUES (?, ?)",
            (handle, number),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_status(handle, status, error=None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE processed_messages SET status = ?, error_log = ? WHERE message_handle = ?",
        (status, error, handle),
    )
    conn.commit()
    conn.close()


def get_user_session(phone_number):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT session_id FROM user_sessions WHERE phone_number = ?", (phone_number,)
    )
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]

    # Auto-initialize a valid session if missing
    print(f"--> No valid session found for {phone_number}. Initializing a new one...")
    cmd = [
        get_hermes_bin(),
        "chat",
        "--toolsets",
        SENDBLUE_TOOLSETS,
        "-Q",
        "-q",
        "Initializing Sendblue daemon session.",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        combined = proc.stdout + "\n" + proc.stderr
        for line in combined.splitlines():
            if "session_id:" in line:
                new_session = line.split("session_id:")[1].strip()
                set_user_session(phone_number, new_session)
                return new_session
    except Exception as e:
        print(f"Failed to initialize session: {e}")

    return INITIAL_SESSION_ID


def set_user_session(phone_number, session_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO user_sessions (phone_number, session_id) VALUES (?, ?)",
        (phone_number, session_id),
    )
    conn.commit()
    conn.close()


# Media Uploader Abstraction
class MediaUploader:
    def __init__(self):
        self.use_s3 = False
        self.s3_client = None
        self.bucket = os.environ.get("S3_BUCKET_NAME")
        aws_key = os.environ.get("AWS_ACCESS_KEY_ID")
        aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
        endpoint = os.environ.get("R2_ENDPOINT_URL")

        if self.bucket and aws_key and aws_secret:
            try:
                import boto3

                self.s3_client = boto3.client(
                    "s3",
                    aws_access_key_id=aws_key,
                    aws_secret_access_key=aws_secret,
                    endpoint_url=endpoint,
                )
                self.use_s3 = True
                print(f"✅ Secure Cloud Storage configured (Bucket: {self.bucket})")
            except ImportError:
                print(
                    "⚠️  WARNING: S3 credentials found, but boto3 is not installed. Falling back to tmpfiles.org!"
                )

        if not self.use_s3:
            print(
                "⚠️  WARNING: No S3/R2 credentials found. Outbound media will be uploaded to PUBLIC tmpfiles.org!"
            )

    async def upload(self, filepath: str) -> str:
        if self.use_s3:
            filename = os.path.basename(filepath)
            object_name = f"sendblue/{int(time.time())}_{filename}"
            loop = asyncio.get_event_loop()

            def _do_upload():
                self.s3_client.upload_file(filepath, self.bucket, object_name)
                return self.s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": object_name},
                    ExpiresIn=3600,
                )

            return await loop.run_in_executor(None, _do_upload)
        else:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                with open(filepath, "rb") as f:
                    data = aiohttp.FormData()
                    data.add_field("file", f, filename=os.path.basename(filepath))
                    async with session.post(
                        "https://tmpfiles.org/api/v1/upload", data=data
                    ) as resp:
                        res = await resp.json()
                        url = res["data"]["url"]
                        return url.replace("tmpfiles.org/", "tmpfiles.org/dl/")


MEDIA_UPLOADER = None


async def send_message_async(text: str, number: str):
    import aiohttp

    url = "https://api.sendblue.co/api/send-message"
    headers = {
        "sb-api-key-id": API_KEY,
        "sb-api-secret-key": API_SECRET,
        "Content-Type": "application/json",
    }

    media_url = ""
    if "MEDIA:" in text:
        parts = text.split("MEDIA:", 1)
        clean_text = parts[0].strip()
        match = re.search(r"(/.*?\.(ogg|mp3|caf|wav|png|jpg|jpeg|gif))", text)
        if match:
            media_path = match.group(1).strip()
            if os.path.exists(media_path):
                if media_path.endswith(".ogg") or media_path.endswith(".mp3"):
                    new_path = media_path.rsplit(".", 1)[0] + ".caf"
                    if shutil.which("ffmpeg"):
                        proc = await asyncio.create_subprocess_exec(
                            "ffmpeg",
                            "-y",
                            "-i",
                            media_path,
                            "-acodec",
                            "libopus",
                            "-b:a",
                            "32k",
                            "-vbr",
                            "on",
                            "-compression_level",
                            "10",
                            new_path,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        await proc.communicate()
                        if os.path.exists(new_path):
                            media_path = new_path

                try:
                    media_url = await MEDIA_UPLOADER.upload(media_path)
                except Exception as e:
                    print(f"Failed to upload media: {e}")
        text = clean_text.replace("[[audio_as_voice]]", "").strip()

    if not text and media_url:
        text = " "

    # Safe truncation (1500 bytes max payload)
    text = text.encode("utf-8")[:1490].decode("utf-8", "ignore")

    data = {"number": number, "content": text, "from_number": SENDBLUE_PHONE}
    if media_url:
        data["media_url"] = media_url

    async with aiohttp.ClientSession() as session:
        await session.post(url, headers=headers, json=data)


def send_typing_indicator_sync(number):
    url = "https://api.sendblue.co/api/send-typing-indicator"
    headers = {
        "sb-api-key-id": API_KEY,
        "sb-api-secret-key": API_SECRET,
        "Content-Type": "application/json",
    }
    try:
        requests.post(
            url,
            headers=headers,
            json={"number": number, "from_number": SENDBLUE_PHONE},
            timeout=3,
        )
    except Exception:
        pass


async def process_message(msg):
    import aiohttp
    import tempfile

    handle = msg.get("message_handle")
    content = msg.get("content", "").strip()
    media_url = msg.get("media_url")
    number = msg.get("from_number")

    # Idempotent DB lock
    if not mark_processing(handle, number):
        return

    # Security: Only process messages from the authorized USER_PHONE
    if number not in USER_PHONES:
        print(f"--> Ignoring message from unauthorized number: {number}")
        return

    try:
        # Native Voice / Media Routing (Removes Whisper logic)
        if not content and media_url:
            print(
                f"--> Received media from {media_url}. Delegating natively to Hermes..."
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(media_url) as resp:
                    if resp.status == 200:
                        ext = (
                            media_url.split(".")[-1] if "." in media_url[-5:] else "caf"
                        )
                        fd, temp_path = tempfile.mkstemp(suffix=f".{ext}")

                        downloaded_size = 0
                        is_too_large = False

                        with os.fdopen(fd, "wb") as f:
                            async for chunk in resp.content.iter_chunked(
                                1024 * 64
                            ):  # 64KB chunks
                                downloaded_size += len(chunk)
                                if downloaded_size > MAX_MEDIA_SIZE_BYTES:
                                    is_too_large = True
                                    break
                                f.write(chunk)

                        if is_too_large:
                            os.remove(temp_path)
                            print(
                                f"--> Media download exceeded size limit ({MAX_MEDIA_SIZE_BYTES} bytes). Aborting."
                            )
                            update_status(handle, "failed", "media file too large")
                            return

                        content = f"MEDIA: {temp_path}"
                    else:
                        content = (
                            f"[Failed to download inbound media: HTTP {resp.status}]"
                        )

        if not content:
            update_status(handle, "completed", "empty content")
            return

        prefix_enabled = (
            os.environ.get("SENDBLUE_PREFIX_ENABLED", "true").lower() == "true"
        )
        if prefix_enabled and not content.startswith("/"):
            # Inject prompt instruction alongside the prefix
            now = datetime.now(timezone.utc)
            request_ts = msg.get("date_sent", "unknown")
            approval_prompt = f"""[System Context: The following message was received remotely via SMS/SendBlue.
Current server time: {now.isoformat()} (UTC)
Request timestamp: {request_ts}
You are running headlessly: you MUST NOT use the `clarify` tool or any interactive terminal tools (they will hang the daemon).

Execution Rules:
1. READ-ONLY ACTIONS: You are fully free to use tools for read-only tasks (e.g., browsing the web, reading files, transcribing audio, checking logs) PROACTIVELY and AUTOMATICALLY. Do NOT ask for permission or share plans for these.
2. DESTRUCTIVE ACTIONS: For destructive code execution or system modifications, you MUST ask for permission first.
3. FEATURE CHANGES: If asked to implement or change a codebase/feature, you MUST share your technical plan and ask for confirmation before executing the code.

Do not apologize for being headless. Keep your final text replies concise.]

[via SendBlue] """
            content = approval_prompt + content

        print(f"\n[!] Processing message: {content[:100]}...")

        # Fire initial typing indicator
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, send_typing_indicator_sync, number)

        # Guarantee the typing indicator is visible for at least 2 seconds before instant replies
        await asyncio.sleep(2.0)

        current_session = get_user_session(number)

        # Session reset
        if content.lower() in ["/new", "reset"]:
            print("--> Creating fresh session...")
            cmd = [
                get_hermes_bin(),
                "chat",
                "--toolsets",
                SENDBLUE_TOOLSETS,
                "-Q",
                "-q",
                "Hello! This is a brand new session. What's up?",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            combined = stdout.decode() + "\n" + stderr.decode()

            new_session = None
            for line in combined.splitlines():
                if "session_id:" in line:
                    new_session = line.split("session_id:")[1].strip()

            if new_session:
                set_user_session(number, new_session)
                await send_message_async(
                    f"Started a new session: {new_session}", number
                )
                update_status(handle, "completed")
            else:
                await send_message_async("Failed to create new session.", number)
                update_status(handle, "failed", "failed to create session")
            return

        print(f"--> Sending to Hermes (Session: {current_session})")

        env = os.environ.copy()
        env["SENDBLUE_ACTIVE_USER_PHONE"] = number

        cmd = [
            get_hermes_bin(),
            "chat",
            "--resume",
            current_session,
            "--yolo",
            "--toolsets",
            SENDBLUE_TOOLSETS,
            "-Q",
            "-q",
            content,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()

        # If the session was invalid/deleted, Hermes will print "Session not found"
        stderr_text = stderr.decode()
        if "Session not found" in stderr_text or "Session not found" in stdout.decode():
            print("--> Session was invalid/deleted. Auto-resetting and retrying...")
            conn = sqlite3.connect(DB_PATH)
            conn.execute("DELETE FROM user_sessions WHERE phone_number = ?", (number,))
            conn.commit()
            conn.close()
            current_session = get_user_session(number)
            cmd = [
                get_hermes_bin(),
                "chat",
                "--resume",
                current_session,
                "--yolo",
                "--toolsets",
                SENDBLUE_TOOLSETS,
                "-Q",
                "-q",
                content,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await proc.communicate()

        final_response = "Done."
        try:
            db_path = os.path.expanduser("~/.hermes/state.db")
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL;")
                c = conn.cursor()
                # Use role = 'assistant' to prevent echoing user input back on crash
                c.execute(
                    "SELECT id, content FROM messages WHERE session_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                    (current_session,),
                )
                row = c.fetchone()
                if row and row[1]:
                    final_response = row[1].strip()
                    ast_id = row[0]

                    # Scan recent tool outputs for media tags that were generated DURING this specific response turn
                    c.execute(
                        "SELECT id FROM messages WHERE session_id = ? AND role = 'user' AND id < ? ORDER BY id DESC LIMIT 1",
                        (current_session, ast_id),
                    )
                    user_row = c.fetchone()
                    user_id = user_row[0] if user_row else 0

                    c.execute(
                        "SELECT content FROM messages WHERE session_id = ? AND role = 'tool' AND id > ? AND id < ? ORDER BY id DESC",
                        (current_session, user_id, ast_id),
                    )
                    for trow in c.fetchall():
                        if "MEDIA:" in trow[0]:
                            try:
                                import json

                                data = json.loads(trow[0])
                                if "media_tag" in data:
                                    final_response += "\n" + data["media_tag"]
                                    break
                            except Exception:
                                match = re.search(r'MEDIA:[^\s"]+', trow[0])
                                if match:
                                    final_response += "\n" + match.group(0)
                                    break
                conn.close()
        except Exception as e:
            print("DB fetch failed:", e)
            # Fallback to parsing stdout
            output_lines = []
            for line in stdout.decode().splitlines():
                line = re.sub(r"\x1B(?:[@-Z\-_]|\\[[0-?]*[ -/]*[@-~])", "", line)
                if (
                    "session_id:" in line
                    or "🧠" in line
                    or "╭─" in line
                    or "╰─" in line
                    or "│" in line
                    or "┊" in line
                ):
                    continue
                output_lines.append(line)
            final_response = "\n".join(output_lines).strip()

        # Always scan STDOUT for media tags since they are not stored in the DB text column
        for line in stdout.decode().splitlines():
            clean_line = re.sub(r"\x1B(?:[@-Z\-_]|\\[[0-?]*[ -/]*[@-~])", "", line)
            if "MEDIA:" in clean_line:
                final_response += "\n" + clean_line.strip()

        if not final_response:
            final_response = "Done."

        print(f"--> Replying: {final_response[:50]}...")

        await send_message_async(final_response, number)
        update_status(handle, "completed")

    except Exception as e:
        print(f"Error processing {handle}: {e}")
        update_status(handle, "failed", str(e))


async def run():
    init_db()

    global MEDIA_UPLOADER
    MEDIA_UPLOADER = MediaUploader()

    print("Starting Sendblue SMS <-> Hermes CLI Daemon...")
    print(f"Monitoring messages from {', '.join(USER_PHONES)}")
    print("Ready to attach sessions per-user.")
    print("Press Ctrl+C to stop.")

    import aiohttp

    async with aiohttp.ClientSession() as session:
        # Initial sweep
        url = "https://api.sendblue.co/api/v2/messages"
        headers = {
            "sb-api-key-id": API_KEY,
            "sb-api-secret-key": API_SECRET,
            "Content-Type": "application/json",
        }
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for msg in data.get("data", []):
                        h = msg.get("message_handle")
                        if h:
                            mark_processing(h, msg.get("from_number"))
        except Exception as e:
            print("Failed initial sweep:", e)

        while True:
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        messages = data.get("data", [])
                        messages.sort(key=lambda x: x.get("date_sent", ""))

                        for msg in messages:
                            if msg.get("is_outbound"):
                                h = msg.get("message_handle")
                                if h:
                                    mark_processing(h, msg.get("from_number"))
                                continue

                            # Async dispatch - doesn't block the polling loop
                            asyncio.create_task(process_message(msg))
            except Exception as e:
                print(f"Error during polling: {e}")

            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run())
