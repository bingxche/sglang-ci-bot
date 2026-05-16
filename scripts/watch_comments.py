#!/usr/bin/env python3
"""
amd-bot Comment Watcher for sglang PRs.

Polls for new comments mentioning @amd-bot trigger keyword,
then dispatches PR review or other actions.

Supports three modes:
  1. One-shot (default): poll once and exit (for GitHub Actions cron)
  2. Daemon (--daemon):   poll continuously in a loop (for self-hosted runner)
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger("comment-watcher")

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"

BOT_LOGIN = "amd-bot"
BOT_TRIGGER = f"@{BOT_LOGIN}"
# To add a new user: append their GitHub username here AND update README.md
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw", "1am9trash", "sogalin", "Kangyan-Zhou", "Fridge003", "BowenBao", "ColinZ22", "fxmarty-amd"]
AUTHORIZED_USER_LOGINS = {user.lower() for user in AUTHORIZED_USERS}
COMMANDS = {
    "review": "Perform a full code review of this PR",
    "review-focus": "Review with focus on specific areas (provide after the command)",
    "ci-status": "Check and summarize CI status for this PR",
    "help": "Show available commands",
}
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 3

STATE_FILE = Path(__file__).parent.parent / ".state" / "last_check.json"


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state() -> dict:
    """Load last check timestamp."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    """Save state to file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_recent_comments(token: str, since: str | None = None) -> list[dict]:
    """Get recent issue/PR comments from the repo."""
    url = f"https://api.github.com/repos/{REPO}/issues/comments"
    params = {"sort": "created", "direction": "desc", "per_page": COMMENT_PAGE_SIZE}
    if since:
        params["since"] = since

    comments = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        resp = requests.get(url, headers=gh_headers(token), params={**params, "page": page})
        resp.raise_for_status()
        page_comments = resp.json()
        comments.extend(page_comments)
        if len(page_comments) < COMMENT_PAGE_SIZE:
            break
    return comments


def parse_command(comment_body: str, trigger: str = BOT_TRIGGER) -> dict | None:
    """Parse a bot command from a comment."""
    body = comment_body.strip()
    pattern = re.escape(trigger) + r"\s+(\S+)\s*(.*)"
    match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
    if not match:
        if trigger.lower() in body.lower():
            return {"command": "review", "args": ""}
        return None

    command = match.group(1).lower().strip()
    args = match.group(2).strip()
    return {"command": command, "args": args}


def extract_pr_number_from_url(issue_url: str) -> int | None:
    """Extract PR number from GitHub API issue URL."""
    match = re.search(r"/issues/(\d+)$", issue_url)
    if match:
        return int(match.group(1))
    return None


def is_pull_request(token: str, issue_url: str) -> bool:
    """Check if an issue URL is actually a PR."""
    resp = requests.get(issue_url, headers=gh_headers(token))
    if resp.status_code != 200:
        return False
    data = resp.json()
    return "pull_request" in data


def dispatch_review(
    token: str, bot_repo: str, pr_number: int,
    focus: str = "", comment_author: str = "", comment_id: int = 0,
):
    """Trigger the PR review workflow via repository_dispatch."""
    url = f"https://api.github.com/repos/{bot_repo}/dispatches"
    payload = {
        "event_type": "pr-review",
        "client_payload": {
            "pr_number": str(pr_number),
            "comment_id": str(comment_id),
            "focus": focus,
            "comment_author": comment_author,
        },
    }
    resp = requests.post(url, headers=gh_headers(token), json=payload)
    resp.raise_for_status()
    log.info("Dispatched review for PR #%d (comment %d)", pr_number, comment_id)


def dispatch_ci_status(
    token: str, bot_repo: str, pr_number: int,
    comment_author: str = "", comment_id: int = 0,
):
    """Trigger CI status check workflow."""
    url = f"https://api.github.com/repos/{bot_repo}/dispatches"
    payload = {
        "event_type": "ci-status",
        "client_payload": {
            "pr_number": str(pr_number),
            "comment_id": str(comment_id),
            "comment_author": comment_author,
        },
    }
    resp = requests.post(url, headers=gh_headers(token), json=payload)
    resp.raise_for_status()
    log.info("Dispatched CI status check for PR #%d (comment %d)", pr_number, comment_id)


def post_help_comment(token: str, pr_number: int):
    """Post a help message listing available commands."""
    help_text = f"## amd-bot Help\n\nAvailable commands (mention `{BOT_TRIGGER}` followed by a command):\n\n"
    for cmd, desc in COMMANDS.items():
        help_text += f"- `{BOT_TRIGGER} {cmd}` - {desc}\n"
    help_text += f"\n### Examples\n"
    help_text += f"- `{BOT_TRIGGER} review` - Full PR review\n"
    help_text += f"- `{BOT_TRIGGER} review-focus AMD ROCm compatibility` - Focused review\n"
    help_text += f"- `{BOT_TRIGGER} ci-status` - Check CI failures\n"
    help_text += "\n---\n*Generated by amd-bot*\n"

    url = f"https://api.github.com/repos/{REPO}/issues/{pr_number}/comments"
    resp = requests.post(url, headers=gh_headers(token), json={"body": help_text})
    resp.raise_for_status()


def add_reaction(token: str, comment_id: int, reaction: str = "eyes"):
    """Add a reaction to acknowledge the command."""
    url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}/reactions"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.squirrel-girl-preview+json"
    resp = requests.post(url, headers=headers, json={"content": reaction})
    if resp.status_code not in (200, 201):
        log.warning("Could not add reaction: HTTP %d", resp.status_code)


def has_bot_claimed(token: str, comment_id: int, reaction: str = "rocket") -> bool:
    """Check if the bot has already claimed this comment via a reaction.

    Uses GitHub reactions as a distributed idempotency mechanism so that
    both the cron watcher and the daemon watcher can share state without
    needing a common filesystem or cache.
    """
    url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}/reactions"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.squirrel-girl-preview+json"
    resp = requests.get(url, headers=headers, params={"content": reaction, "per_page": 100})
    if resp.status_code != 200:
        log.warning("Could not check reactions: HTTP %d", resp.status_code)
        return False
    for r in resp.json():
        if r.get("content") == reaction and r.get("user", {}).get("login") == BOT_LOGIN:
            return True
    return False


def process_comments(token: str, bot_repo: str, since: str | None = None):
    """Process new comments and dispatch actions."""
    comments = get_recent_comments(token, since)
    state = load_state()
    processed_ids = set(state.get("processed_comment_ids", []))

    new_commands = []
    for comment in comments:
        comment_id = comment["id"]
        if comment_id in processed_ids:
            continue

        author = comment["user"]["login"]
        if author.lower() not in AUTHORIZED_USER_LOGINS:
            continue

        parsed = parse_command(comment["body"])
        if not parsed:
            continue

        issue_url = comment["issue_url"]
        pr_number = extract_pr_number_from_url(issue_url)
        if not pr_number:
            continue

        if not is_pull_request(token, issue_url):
            continue

        new_commands.append(
            {
                "comment_id": comment_id,
                "pr_number": pr_number,
                "command": parsed["command"],
                "args": parsed["args"],
                "author": author,
                "created_at": comment["created_at"],
            }
        )

    for cmd in new_commands:
        cid = cmd["comment_id"]

        if has_bot_claimed(token, cid, "rocket"):
            log.info("Skipping PR #%d - %s (already claimed by another watcher)", cmd["pr_number"], cmd["command"])
            processed_ids.add(cid)
            continue

        log.info("Processing: PR #%d - %s (by @%s)", cmd["pr_number"], cmd["command"], cmd["author"])

        add_reaction(token, cid, "rocket")
        add_reaction(token, cid, "eyes")

        if cmd["command"] == "review":
            dispatch_review(token, bot_repo, cmd["pr_number"], comment_author=cmd["author"], comment_id=cid)
        elif cmd["command"] == "review-focus":
            dispatch_review(token, bot_repo, cmd["pr_number"], focus=cmd["args"], comment_author=cmd["author"], comment_id=cid)
        elif cmd["command"] == "ci-status":
            dispatch_ci_status(token, bot_repo, cmd["pr_number"], comment_author=cmd["author"], comment_id=cid)
        elif cmd["command"] == "help":
            post_help_comment(token, cmd["pr_number"])
        else:
            log.warning("Unknown command: %s", cmd["command"])

        processed_ids.add(cid)

    state["processed_comment_ids"] = list(processed_ids)[-500:]
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    log.info("Processed %d new command(s)", len(new_commands))
    return new_commands


_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("Received signal %s, shutting down gracefully...", signal.Signals(signum).name)
    _shutdown = True


def run_daemon(token: str, bot_repo: str, poll_interval: int):
    """Run the comment watcher as a long-lived daemon process."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info(
        "Daemon started — polling %s every %ds, dispatching to %s",
        REPO, poll_interval, bot_repo,
    )

    consecutive_errors = 0
    max_backoff = 300  # 5 min cap on error backoff

    while not _shutdown:
        since = (datetime.now(timezone.utc) - timedelta(seconds=poll_interval * 3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        try:
            cmds = process_comments(token, bot_repo, since=since)
            consecutive_errors = 0
            if cmds:
                log.info("Dispatched %d command(s)", len(cmds))
        except requests.exceptions.RequestException as exc:
            consecutive_errors += 1
            backoff = min(poll_interval * (2 ** consecutive_errors), max_backoff)
            log.warning("API error (%d in a row): %s — retrying in %ds", consecutive_errors, exc, backoff)
            _interruptible_sleep(backoff)
            continue
        except Exception:
            consecutive_errors += 1
            log.exception("Unexpected error (%d in a row)", consecutive_errors)
            _interruptible_sleep(min(60 * consecutive_errors, max_backoff))
            continue

        _interruptible_sleep(poll_interval)

    log.info("Daemon stopped.")


def _interruptible_sleep(seconds: int):
    """Sleep that can be interrupted by SIGTERM/SIGINT."""
    end = time.monotonic() + seconds
    while not _shutdown and time.monotonic() < end:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1, remaining))


def main():
    parser = argparse.ArgumentParser(description="Watch for bot commands in sglang PR comments")
    parser.add_argument(
        "--bot-repo",
        required=True,
        help="Your bot repo (e.g., 'username/amd-bot')",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=1,
        help="How many hours back to check for one-shot mode (default: 1)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run as a long-lived daemon instead of one-shot",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between polls in daemon mode (default: 30)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("BOT_PAT", os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", ""))),
    )

    args = parser.parse_args()

    level = logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=level,
        stream=sys.stdout,
    )

    if not args.github_token:
        log.error("GitHub token required. Set GH_PAT env var.")
        sys.exit(1)

    if args.daemon:
        run_daemon(args.github_token, args.bot_repo, args.poll_interval)
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=args.since_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        process_comments(args.github_token, args.bot_repo, since=since)


if __name__ == "__main__":
    main()
