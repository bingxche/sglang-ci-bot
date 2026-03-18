#!/usr/bin/env python3
"""
amd-bot Comment Watcher for sglang PRs.

Polls for new comments mentioning @amd-bot trigger keyword,
then dispatches PR review or other actions.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"

BOT_TRIGGER = "@amd-bot"
AUTHORIZED_USERS = ["bingxche"]
COMMANDS = {
    "review": "Perform a full code review of this PR",
    "review-focus": "Review with focus on specific areas (provide after the command)",
    "ci-status": "Check and summarize CI status for this PR",
    "help": "Show available commands",
}

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
    params = {"sort": "created", "direction": "desc", "per_page": 50}
    if since:
        params["since"] = since

    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()
    return resp.json()


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


def dispatch_review(token: str, bot_repo: str, pr_number: int, focus: str = "", comment_author: str = ""):
    """Trigger the PR review workflow via repository_dispatch."""
    url = f"https://api.github.com/repos/{bot_repo}/dispatches"
    payload = {
        "event_type": "pr-review",
        "client_payload": {
            "pr_number": str(pr_number),
            "focus": focus,
            "comment_author": comment_author,
        },
    }
    resp = requests.post(url, headers=gh_headers(token), json=payload)
    resp.raise_for_status()
    print(f"  Dispatched review for PR #{pr_number}")


def dispatch_ci_status(token: str, bot_repo: str, pr_number: int, comment_author: str = ""):
    """Trigger CI status check workflow."""
    url = f"https://api.github.com/repos/{bot_repo}/dispatches"
    payload = {
        "event_type": "ci-status",
        "client_payload": {
            "pr_number": str(pr_number),
            "comment_author": comment_author,
        },
    }
    resp = requests.post(url, headers=gh_headers(token), json=payload)
    resp.raise_for_status()
    print(f"  Dispatched CI status check for PR #{pr_number}")


def post_help_comment(token: str, pr_number: int):
    """Post a help message listing available commands."""
    help_text = f"## amd-bot Help\n\nAvailable commands (mention `{BOT_TRIGGER}` followed by a command):\n\n"
    for cmd, desc in COMMANDS.items():
        help_text += f"- `{BOT_TRIGGER} {cmd}` - {desc}\n"
    help_text += f"\n### Examples\n"
    help_text += f"- `{BOT_TRIGGER} review` - Full PR review\n"
    help_text += f"- `{BOT_TRIGGER} review-focus AMD ROCm compatibility` - Focused review\n"
    help_text += f"- `{BOT_TRIGGER} ci-status` - Check CI failures\n"

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
        print(f"  Warning: Could not add reaction: {resp.status_code}")


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
        if author not in AUTHORIZED_USERS:
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
        print(f"Processing: PR #{cmd['pr_number']} - {cmd['command']} (by @{cmd['author']})")

        add_reaction(token, cmd["comment_id"], "eyes")

        if cmd["command"] == "review":
            dispatch_review(token, bot_repo, cmd["pr_number"], comment_author=cmd["author"])
        elif cmd["command"] == "review-focus":
            dispatch_review(token, bot_repo, cmd["pr_number"], focus=cmd["args"], comment_author=cmd["author"])
        elif cmd["command"] == "ci-status":
            dispatch_ci_status(token, bot_repo, cmd["pr_number"], comment_author=cmd["author"])
        elif cmd["command"] == "help":
            post_help_comment(token, cmd["pr_number"])
        else:
            print(f"  Unknown command: {cmd['command']}")

        processed_ids.add(cmd["comment_id"])

    state["processed_comment_ids"] = list(processed_ids)[-500:]
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print(f"Processed {len(new_commands)} new command(s)")
    return new_commands


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
        help="How many hours back to check (default: 1)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
    )

    args = parser.parse_args()

    if not args.github_token:
        print("Error: GitHub token required.", file=sys.stderr)
        sys.exit(1)

    since = (datetime.now(timezone.utc) - timedelta(hours=args.since_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    process_comments(args.github_token, args.bot_repo, since=since)


if __name__ == "__main__":
    main()
