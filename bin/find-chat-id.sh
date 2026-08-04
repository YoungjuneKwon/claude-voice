#!/usr/bin/env bash
# Print the chat_id(s) that have talked to the bot.
#
# Telegram only tells you a chat_id after that chat sends the bot a message, so
# open the bot in Telegram and send it anything first, then run this.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${CLAUDE_VOICE_CONFIG:-$ROOT/config.env}"
[ -f "$CONFIG" ] || { echo "missing config: $CONFIG" >&2; exit 1; }

# shellcheck disable=SC1090
set -a; . "$CONFIG"; set +a
# shellcheck disable=SC1090
set -a; . "$CLAUDE_VOICE_TELEGRAM_ENV"; set +a

curl -sS "https://api.telegram.org/bot${token}/getUpdates" | python3 -c '
import json, sys
updates = json.load(sys.stdin).get("result", [])
chats = {}
for update in updates:
    message = update.get("message") or update.get("channel_post") or {}
    chat = message.get("chat", {})
    if chat.get("id"):
        chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("type")
if not chats:
    sys.exit("no chats yet — send the bot a message in Telegram, then re-run")
for chat_id, who in chats.items():
    print(f"CLAUDE_VOICE_TELEGRAM_CHAT_ID={chat_id}   # {who}")
'
