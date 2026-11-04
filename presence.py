#!/usr/bin/env python3
"""
Keeps the Discord bot shown as online while the watcher runs.

Discord only shows a bot as online while it holds a Gateway connection, which
bot.py (REST only) never opens. GitHub Actions starts this next to the check
loop and stops it with SIGINT when the shift ends, so the bot goes offline
exactly when nothing is watching.

Usage:
  python presence.py                 stay online until Ctrl+C / SIGINT
"""
import json
import logging
import os
import sys
from pathlib import Path

import discord

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
PROJECTS_FILE = Path(__file__).resolve().parent / "projects.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("presence")


def activity_text() -> str:
    try:
        count = len(json.loads(PROJECTS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return "for feedback"
    return f"feedback on {count} mod{'s' if count != 1 else ''}"


def main() -> None:
    if not BOT_TOKEN:
        sys.exit("DISCORD_BOT_TOKEN is not set; nothing to show online.")
    # No intents: presence needs none, and it avoids the privileged-intent setup.
    client = discord.Client(
        intents=discord.Intents.none(),
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching, name=activity_text()),
    )

    @client.event
    async def on_ready() -> None:
        log.info("Online as %s", client.user)

    # log_handler=None keeps discord.py from installing a second log handler.
    client.run(BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
