#!/usr/bin/env python3
"""Ensure today's daily CI-monitor issue exists.

Run this once before fanning out the matrix monitor jobs to avoid
concurrent ``find_or_create_daily_issue`` races that would otherwise
create duplicate issues.

Env:
  GH_PAT    - GitHub token (fallback: GITHUB_TOKEN, BOT_PAT)
  BOT_REPO  - e.g. ``bingxche/sglang-ci-bot``
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor_ci import find_or_create_daily_issue


def main() -> int:
    token = (
        os.environ.get("GH_PAT")
        or os.environ.get("BOT_PAT")
        or os.environ.get("GITHUB_TOKEN")
    )
    bot_repo = os.environ.get("BOT_REPO")
    if not token:
        print("ERROR: GH_PAT (or GITHUB_TOKEN) must be set", file=sys.stderr)
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
