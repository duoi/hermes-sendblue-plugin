import os
import sys
import time
import json
import subprocess
import requests
import re
from pathlib import Path

# Try to load from .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
    
# Alternatively load directly from ~/.hermes/.env
env_path = os.path.expanduser("~/.hermes/.env")
if os.path.exists(env_path):
    with open(env_path, "r") as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                key, val = line.strip().split("=", 1)
                os.environ[key] = val.strip("'\"")

API_KEY = os.environ.get("SENDBLUE_API_KEY")
API_SECRET = os.environ.get("SENDBLUE_API_SECRET")
USER_PHONE = os.environ.get("USER_PHONE")
SENDBLUE_PHONE = os.environ.get("SENDBLUE_PHONE")
INITIAL_SESSION_ID = os.environ.get("INITIAL_SESSION_ID", "new_session")

if not all([API_KEY, API_SECRET, USER_PHONE, SENDBLUE_PHONE]):
    print("Error: SENDBLUE_API_KEY, SENDBLUE_API_SECRET, USER_PHONE, and SENDBLUE_PHONE must be set in environment.")
    sys.exit(1)

PROCESSED_MESSAGES_FILE = os.path.expanduser("~/.hermes/sendblue_processed.txt")
SESSION_FILE = os.path.expanduser("~/.hermes/sendblue_session.txt")

def get_processed():
    if not os.path.exists(PROCESSED_MESSAGES_FILE):
        return set()
    with open(PROCESSED_MESSAGES_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def mark_processed(handle):
    with open(PROCESSED_MESSAGES_FILE, "a") as f:
        f.write(handle + "\n")

def get_current_session():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, "r") as f:
            return f.read().strip()
    return INITIAL_SESSION_ID

def set_current_session(session_id):
    with open(SESSION_FILE, "w") as f:
        f.write(session_id)

def fetch_messages():
    url = "https://api.sendblue.co/api/v2/messages"
    headers = {
        "sb-api-key-id": API_KEY,
        "sb-api-secret-key": API_SECRET,
        "Content-Type": "application/json"
    }
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        return resp.json().get("data", [])
    return []

def send_message(text):
    url = "https://api.sendblue.co/api/send-message"
    headers = {
        "sb-api-key-id": API_KEY,
        "sb-api-secret-key": API_SECRET,
        "Content-Type": "application/json"
    }
    
    media_url = ""
    if "MEDIA:" in text:
        parts = text.split("MEDIA:", 1)
        clean_text = parts[0].strip()
        match = re.search(r'(/.*?\.(ogg|mp3|caf|wav|png|jpg|jpeg|gif))', text)
        if match:
            media_path = match.group(1).strip()
            if os.path.exists(media_path):
                if media_path.endswith('.ogg') or media_path.endswith('.mp3'):
                    new_path = media_path.rsplit('.', 1)[0] + '.caf'
                    subprocess.run(["ffmpeg", "-y", "-i", media_path, "-acodec", "libopus", "-b:a", "32k", "-vbr", "on", "-compression_level", "10", new_path], capture_output=True)
                    if os.path.exists(new_path):
                        media_path = new_path
                
                try:
                    res = subprocess.run(["curl", "-F", f"file=@{media_path}", "https://tmpfiles.org/api/v1/upload"], capture_output=True, text=True)
                    data = json.loads(res.stdout)
                    file_url = data["data"]["url"]
                    media_url = file_url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
                except Exception as e:
                    print(f"Failed to upload media: {e}")
        text = clean_text.replace("[[audio_as_voice]]", "").strip()

    if not text and media_url:
        text = " "

    if len(text) > 1500:
        text = text[:1497] + "..."
        
    data = {
        "number": USER_PHONE,
        "content": text,
        "from_number": SENDBLUE_PHONE
    }
    if media_url:
        data["media_url"] = media_url
        
    requests.post(url, headers=headers, json=data)

def transcribe_audio(media_url):
    try:
        audio_data = requests.get(media_url).content
        with open("/tmp/voice.caf", "wb") as f:
            f.write(audio_data)
        
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, info = model.transcribe("/tmp/voice.caf", beam_size=5)
        text = " ".join([segment.text for segment in segments])
        return text
    except Exception as e:
        return f"[Error transcribing audio locally: {e}]"

def run():
    print("Starting Sendblue SMS <-> Hermes CLI Daemon...")
    print(f"Monitoring messages from {USER_PHONE}")
    print(f"Attached to Session: {get_current_session()}")
    print("Press Ctrl+C to stop.")
    
    try:
        initial_msgs = fetch_messages()
        processed = get_processed()
        for msg in initial_msgs:
            handle = msg.get("message_handle")
            if handle and handle not in processed:
                mark_processed(handle)
    except Exception as e:
        print("Failed initial sweep:", e)
    
    while True:
        try:
            messages = fetch_messages()
            processed = get_processed()
            messages.sort(key=lambda x: x.get("date_sent", ""))
            
            for msg in messages:
                handle = msg.get("message_handle")
                if not handle or handle in processed:
                    continue
                
                if msg.get("is_outbound"):
                    mark_processed(handle)
                    continue
                    
                content = msg.get("content", "").strip()
                media_url = msg.get("media_url")
                
                if not content and media_url:
                    print(f"--> Transcribing audio message from {media_url}")
                    content = transcribe_audio(media_url)
                
                if not content:
                    mark_processed(handle)
                    continue

                print(f"\n[!] New message received: {content}")
                
                mark_processed(handle)
                
                if content.lower() in ["/new", "reset"]:
                    print("--> Creating fresh session...")
                    res = subprocess.run(["hermes", "chat", "-Q", "-q", "Hello! This is a brand new session. What's up?"], capture_output=True, text=True)
                    
                    new_session = None
                    for line in res.stdout.splitlines():
                        if "session_id:" in line:
                            new_session = line.split("session_id:")[1].strip()
                    
                    if new_session:
                        set_current_session(new_session)
                        send_message(f"Started a new session: {new_session}")
                    else:
                        send_message("Failed to create new session.")
                    continue
                
                current_session = get_current_session()
                print(f"--> Sending to Hermes (Session: {current_session})")
                
                cmd = ["hermes", "--resume", current_session, "--yolo", "chat", "-Q", "-q", content]
                res = subprocess.run(cmd, capture_output=True, text=True)
                
                final_response = "Done."
                try:
                    import sqlite3
                    db_path = os.path.expanduser("~/.hermes/state.db")
                    conn = sqlite3.connect(db_path)
                    c = conn.cursor()
                    c.execute("SELECT content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1", (current_session,))
                    row = c.fetchone()
                    if row and row[0]:
                        final_response = row[0].strip()
                    conn.close()
                except Exception as e:
                    print("DB fetch failed:", e)
                    output_lines = []
                    for line in res.stdout.splitlines():
                        line = re.sub(r'\x1B(?:[@-Z\\-_]|\\[[0-?]*[ -/]*[@-~])', '', line)
                        if "session_id:" in line or "🧠" in line or "╭─" in line or "╰─" in line or "│" in line or "┊" in line:
                            continue
                        output_lines.append(line)
                    final_response = "\n".join(output_lines).strip()
                if not final_response:
                    final_response = "Done."
                
                print(f"--> Replying: {final_response[:50]}...")
                send_message(final_response)
                
        except Exception as e:
            print(f"Error during polling: {e}")
            
        time.sleep(5)

if __name__ == '__main__':
    run()
