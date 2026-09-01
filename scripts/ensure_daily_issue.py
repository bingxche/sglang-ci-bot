#!/usr/bin/env python3
"""Ensure today's daily CI-monitor issue exists.

Run this once before fanning out the matrix monitor jobs to avoid
concurrent ``find_or_create_daily_issue`` races that would otherwise
create duplicate issues.

Env:
  BOT_REPO_TOKEN - token limited to the bot repository (or GITHUB_TOKEN)
  BOT_REPO  - e.g. ``bingxche/sglang-ci-bot``
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_auth import bot_repo_token_from_env
from monitor_ci import find_or_create_daily_issue


def main() -> int:
    token = bot_repo_token_from_env()
    bot_repo = os.environ.get("BOT_REPO")
    if not token:
        print("ERROR: BOT_REPO_TOKEN (or GITHUB_TOKEN) must be set", file=sys.stderr)
        return 1
    if not bot_repo:
        print("ERROR: BOT_REPO must be set", file=sys.stderr)
        return 1

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    num, created = find_or_create_daily_issue(token, bot_repo, date_str)
    action = "Created" if created else "Found"
    print(f"{action} daily issue #{num} for {date_str}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
