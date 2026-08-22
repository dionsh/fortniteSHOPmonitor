#!/usr/bin/env python3
"""Find your Telegram chat id.

Only needed if you want Telegram alerts. Steps:

  1. Message @BotFather -> /newbot -> copy the token
  2. Put it in .env as TELEGRAM_BOT_TOKEN
  3. Send your new bot any message (say "hi") in Telegram
  4. Run this script and copy the id into .env as TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src.config import Config
from src.console import enable_utf8_stdout


def main():
    enable_utf8_stdout()
    config = Config()
    token = config.telegram_token

    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env.", file=sys.stderr)
        return 1

    url = "https://api.telegram.org/bot{}/getUpdates".format(token)
    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException as exc:
        print("Could not reach Telegram: {}".format(exc), file=sys.stderr)
        return 1

    if resp.status_code == 401:
        print("Telegram rejected the token. Check TELEGRAM_BOT_TOKEN.", file=sys.stderr)
        return 1
    if resp.status_code != 200:
        print("Telegram returned HTTP {}.".format(resp.status_code), file=sys.stderr)
        return 1

    try:
        updates = resp.json().get("result") or []
    except ValueError:
        print("Telegram sent back something unreadable.", file=sys.stderr)
        return 1

    if not updates:
        print("No messages found.\n"
              "Open Telegram, send your bot any message, then run this again.")
        return 1

    seen = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])) \
                or chat.get("username") or "(unnamed)"
            seen[chat["id"]] = name

    if not seen:
        print("Found updates but no chat id. Send your bot a direct message.")
        return 1

    print("\nFound {} chat(s):\n".format(len(seen)))
    for chat_id, name in seen.items():
        print("  {}   {}".format(chat_id, name))

    print("\nAdd the one you want to .env:")
    print("  TELEGRAM_CHAT_ID={}".format(next(iter(seen))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
