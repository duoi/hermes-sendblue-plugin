# Hermes Sendblue Plugin

A powerful, **zero-modification** background daemon and plugin that securely bridges [Sendblue](https://sendblue.co) (iMessage / SMS) with the [Hermes CLI AI Agent](https://github.com/NousResearch/hermes-agent).

This plugin allows you to text a designated Sendblue number and have Hermes process your text, act as your personalized AI agent, and text you back the results—all while maintaining full conversation state. 

## 🚀 Features

- **Stateful Threading**: Hermes maintains conversation state. You can have multi-turn conversations over SMS/iMessage just like you do in the terminal.
- **Active LLM Tools**: The AI is equipped with `sendblue_send_message` and `sendblue_list_messages`. You can instruct the AI to proactively text other people autonomously.
- **Admin-Gated Security**: Active tools are restricted out-of-the-box. They will only execute if the AI is communicating with an explicitly authorized admin phone number.
- **Interactive Command Approvals over SMS**: Because this is a headless remote session, if the AI attempts to execute a dangerous terminal command or modify files, it is explicitly instructed via a silent prompt to text you a "Technical Plan" first and wait for you to reply "Yes" before proceeding.
- **Continuous Typing Indicators**: Displays the iMessage typing indicator (`...`) while Hermes is thinking. Our `pre_llm_call` hook ensures the bubble stays active continuously while the AI executes background tools.
- **Native Voice Routing**: If you send an iMessage Voice Note, the daemon passes it directly to Hermes's highly optimized, internal STT engine.
- **Secure Cloud Media Uploads**: If Hermes replies with an image or audio, the script natively uploads it to your private AWS S3 or Cloudflare R2 bucket to generate a secure pre-signed URL. (Includes a public `tmpfiles.org` fallback for quick testing).

## 🛠️ Prerequisites

1. **Sendblue Account**: Get your Sendblue API Key, Secret, and your assigned Sendblue Phone Number.
2. **Hermes**: Ensure Hermes is installed and available in your `$PATH`.
3. **Python Packages**: `pip install aiohttp boto3 requests python-dotenv`

## ⚙️ Installation

Do **not** clone this into your Hermes source tree. Clone it directly into your Hermes plugins directory:

```bash
mkdir -p ~/.hermes/plugins/sendblue
git clone https://github.com/duoi/hermes-sendblue-plugin.git ~/.hermes/plugins/sendblue
```

Verify the plugin is installed by running `hermes plugins list`. You should see `sendblue` enabled.

## 🔑 Environment Variables

The daemon and plugin look for environment variables in the system environment, or in `~/.hermes/plugins/sendblue/.env`, or `~/.hermes/.env`.

### Required
```env
SENDBLUE_API_KEY=your_sendblue_api_key_here
SENDBLUE_API_SECRET=your_sendblue_api_secret_here
USER_PHONE=+12345678901      # Your personal phone number (E.164 format)
SENDBLUE_PHONE=+19876543210  # Your Sendblue phone number (E.164 format)
```

### Optional Configuration
```env
# Admins who are allowed to trigger Active LLM Tools (comma-separated). 
# If empty, defaults to USER_PHONE.
SENDBLUE_ADMIN_PHONES=+12345678901,+19876543210

# Prefix incoming SMS messages with "[via SendBlue]" in the AI's memory (default: true)
SENDBLUE_PREFIX_ENABLED=true

# A specific Hermes session ID to bind to initially (default: "new_session")
INITIAL_SESSION_ID=20260403_020658_d765a7
```

### Optional: Secure Media Uploads (AWS S3 / Cloudflare R2)
If Hermes generates an image or audio file, you must provide a secure place to host it so Sendblue can download and deliver it to your phone. If you do not provide these, the plugin will loudly warn you and upload your media to a public, third-party temporary file host.

```env
S3_BUCKET_NAME=my-private-hermes-bucket
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
# R2_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com # (Only required for Cloudflare R2)
```

## 🏃 Running the Daemon

The plugin provides typing indicators and tools to the Hermes CLI, but you must run the background daemon to actually poll Sendblue for new text messages:

```bash
python ~/.hermes/plugins/sendblue/daemon.py
```

It is highly recommended to run this daemon via `systemd` or `pm2` so it runs continuously in the background and recovers from crashes.

## 🔒 Architecture & Security

This plugin operates via an **Isolated Asynchronous Polling Daemon**.

Our daemon sits outside the Hermes core architecture. It uses `aiohttp` to poll Sendblue, and `asyncio.create_subprocess_exec` to invoke the standard `hermes` CLI command. 

This guarantees:
1. **Update Safety**: When you update Hermes Agent, this plugin will not cause merge conflicts or affect the gateway.
2. **True Concurrency**: If the AI takes 60 seconds to execute a complex web-scraping tool, the daemon does not block. It continues processing incoming webhooks and queuing messages.
3. **Idempotency**: All processed message IDs are tracked in a Write-Ahead Logging (WAL) SQLite database (`~/.hermes/sendblue_daemon.db`), guaranteeing a message is never processed twice even if the daemon crashes mid-execution.

