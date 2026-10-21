#!/usr/bin/env python3
"""Put the per-workflow reports in the daily CI-monitor issue in priority order.

The matrix jobs post each workflow's report whenever its analysis finishes,
so the comments land in whatever order the jobs happen to finish. Run this
once after the matrix (the ``finalize`` job) to rewrite them in
``MONITORED_WORKFLOWS`` order. Yesterday's issue is included because a tick
that starts before midnight UTC publishes to it.

Env:
  GH_PAT    - GitHub token (fallback: GITHUB_TOKEN, BOT_PAT)
  BOT_REPO  - e.g. ``bingxche/sglang-ci-bot``
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor_ci import find_daily_issue, order_workflow_comments


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )
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

    now = datetime.now(timezone.utc)
    for day in (now - timedelta(days=1), now):
        date_str = day.strftime("%Y-%m-%d")
        num = find_daily_issue(token, bot_repo, date_str)
        if num is None:
            print(f"No open daily issue for {date_str}")
            continue
        order_workflow_comments(token, bot_repo, num)
    return 0


if __name__ == "__main__":
    sys.exit(main())
