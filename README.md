# Hermes Sendblue Script

A background daemon that bridges [Sendblue](https://sendblue.co) (iMessage / SMS) with [Hermes CLI AI Agent](https://github.com/duoi/hermes).

This script allows you to text a designated Sendblue number and have Hermes process your text, act as your personalized AI agent, and text you back the results using the same context/session thread.

## Features

- **Stateful Threading**: Hermes maintains conversation state, meaning you can have multi-turn conversations over SMS/iMessage just like you do in the terminal.
- **Audio/Voice Memo Transcription**: If you send an iMessage Voice Note (audio), it will automatically download it and attempt to transcribe it using local `faster_whisper`, then forward the text to Hermes.
- **Auto-Session Reboot**: Text `/new` or `reset` to generate a fresh Hermes session at any point.
- **Media Support (Outbound)**: If Hermes replies with an image or audio (e.g., TTS output), the script converts it to a mobile-compatible format (`.caf` for audio) and sends it over Sendblue.

## Prerequisites

1. **Sendblue Account**: Get your Sendblue API Key, Secret, and your assigned Sendblue Phone Number.
2. **Hermes**: Ensure Hermes is installed and available in your `$PATH`.
3. **Python 3.10+**

## Environment Variables

The script looks for environment variables in the system environment, `.env` file in the current directory, or a `~/.hermes/.env` file. The required variables are:

```env
SENDBLUE_API_KEY=your_sendblue_api_key_here
SENDBLUE_API_SECRET=your_sendblue_api_secret_here
USER_PHONE=+12345678901   # Your personal phone number (E.164 format)
SENDBLUE_PHONE=+19876543210 # Your Sendblue phone number (E.164 format)
INITIAL_SESSION_ID=new_session # Optional: A specific Hermes session ID to bind to initially
```

## Installation & Running

1. Clone this repository.
2. Install the required Python packages:
   ```bash
   pip install requests python-dotenv faster_whisper
   ```
3. Run the daemon:
   ```bash
   python sendblue_sync.py
   ```

It's recommended to run this via `systemd` or `pm2` so it runs continuously in the background.

## API Documentation / Internals

The daemon essentially loops over Sendblue's `/api/v2/messages` endpoint every 5 seconds.

### `fetch_messages()`
Polls Sendblue. Expects standard headers `sb-api-key-id` and `sb-api-secret-key`.

### `send_message(text)`
Sends the payload back to your `USER_PHONE` via Sendblue's `/api/send-message` API. If `text` contains `MEDIA: /path/to/file`, it will automatically use `curl` to upload it to a temporary file host and use the resulting URL as the `media_url` property for Sendblue.

### `transcribe_audio(media_url)`
Uses `faster_whisper` to transcribe voice notes locally. Note: Make sure `ffmpeg` is installed on your system if you want to use the local voice note feature.

## Security Warning

Never commit `.env` files or hardcode API keys into the script!
