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

from github_auth import (
    bot_repo_token_from_env,
    require_distinct_tokens,
    sglang_token_from_env,
    validate_sglang_token,
)

log = logging.getLogger("comment-watcher")

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"

BOT_TRIGGER_LOGIN = os.environ.get("BOT_TRIGGER_LOGIN", "amd-bot").strip()
BOT_TRIGGER = f"@{BOT_TRIGGER_LOGIN}"
# To add a new user: append their GitHub username here AND update README.md
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw", "1am9trash", "sogalin", "Kangyan-Zhou", "Fridge003", "BowenBao", "ColinZ22", "fxmarty-amd", "hubertlu-tw", "RolaoDenthu", "Duyi-Wang", "amd-danli103", "akao-amd", "jonahbernard", "At1a8", "chuyeh", "mqhc2020", "chien-an-chen", "yuychang", "jiaryang", "Emmanuel0612"]
AUTHORIZED_USER_LOGINS = {user.lower() for user in AUTHORIZED_USERS}
COMMANDS = {
    "review": "Perform a full code review of this PR",
    "review-focus": "Review with focus on specific areas (provide after the command)",
    "ci-status": "Check and summarize CI status for this PR",
    "help": "Show available commands",
}
QUOTED_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
COMMENT_PAGE_SIZE = 100
MAX_COMMENT_PAGES = 3
# requests blocks forever without this; a black-holed socket then wedges the daemon.
HTTP_TIMEOUT = (10, 30)
INITIAL_LOOKBACK_HOURS = 24

STATE_FILE = Path(
    os.environ.get(
        "WATCHER_STATE_FILE",
        str(Path(__file__).parent.parent / ".state" / "last_check.json"),
    )
)


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


def daemon_since(poll_interval: int) -> str:
    """Resume from the last successful poll, with overlap for boundary comments."""
    last_check = load_state().get("last_check")
    checkpoint = (
        datetime.fromisoformat(last_check)
        if last_check
        else datetime.now(timezone.utc) - timedelta(hours=INITIAL_LOOKBACK_HOURS)
    )
    return (checkpoint - timedelta(seconds=poll_interval)).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_recent_comments(token: str, since: str | None = None) -> list[dict]:
    """Get recent issue/PR comments from the repo."""
    url = f"https://api.github.com/repos/{REPO}/issues/comments"
    params = {"sort": "created", "direction": "desc", "per_page": COMMENT_PAGE_SIZE}
    if since:
        params["since"] = since

    comments = []
    for page in range(1, MAX_COMMENT_PAGES + 1):
        resp = requests.get(
            url, headers=gh_headers(token), params={**params, "page": page}, timeout=HTTP_TIMEOUT
        )
        resp.raise_for_status()
        page_comments = resp.json()
        comments.extend(page_comments)
        if len(page_comments) < COMMENT_PAGE_SIZE:
            break
    return comments


def parse_command(comment_body: str, trigger: str = BOT_TRIGGER) -> dict | None:
    """Parse a bot command from a comment.

    The trigger only counts at the start of an unquoted line. Quote-reply lines
    and in-prose mentions repeat the trigger without requesting anything, and
    treating those as commands re-runs whatever the quoted comment asked for.
    """
    body = QUOTED_LINE_RE.sub("", comment_body.replace("\r\n", "\n")).strip()
    escaped = re.escape(trigger)
    match = re.search(
        rf"^[ \t]*{escaped}[ \t]+(\S+)[ \t]*(.*)",
        body,
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    if not match:
        if re.search(rf"^[ \t]*{escaped}[ \t]*$", body, re.IGNORECASE | re.MULTILINE):
            return {"command": "review", "args": ""}
        return None

    command = match.group(1).lower().strip()
    args = match.group(2).strip()

    # Accept the space-separated spelling of hyphenated commands ("ci status").
    if args:
        parts = args.split(None, 1)
        candidate = f"{command}-{parts[0].lower()}"
        if candidate in COMMANDS:
            command = candidate
            args = parts[1].strip() if len(parts) > 1 else ""

    return {"command": command, "args": args}


def extract_pr_number_from_url(issue_url: str) -> int | None:
    """Extract PR number from GitHub API issue URL."""
    match = re.search(r"/issues/(\d+)$", issue_url)
    if match:
        return int(match.group(1))
    return None


def is_pull_request(token: str, issue_url: str) -> bool:
    """Check if an issue URL is actually a PR."""
    resp = requests.get(issue_url, headers=gh_headers(token), timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        return False
    data = resp.json()
    return "pull_request" in data


def dispatch_review(
    token: str, bot_repo: str, pr_number: int,
    focus: str = "", comment_author: str = "", comment_id: int = 0,
):
    """Trigger the PR review workflow with a bot-repo-only credential."""
    url = f"https://api.github.com/repos/{bot_repo}/actions/workflows/pr-review.yml/dispatches"
    payload = {
        "ref": "main",
        "inputs": {
            "pr_number": str(pr_number),
            "comment_id": str(comment_id),
            "focus": focus,
            "comment_author": comment_author,
            "no_post": "false",
        },
    }
    resp = requests.post(url, headers=gh_headers(token), json=payload, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    log.info("Dispatched review for PR #%d (comment %d)", pr_number, comment_id)


def dispatch_ci_status(
    token: str, bot_repo: str, pr_number: int,
    comment_author: str = "", comment_id: int = 0,
):
    """Trigger CI status check with a bot-repo-only credential."""
    url = f"https://api.github.com/repos/{bot_repo}/actions/workflows/ci-status-check.yml/dispatches"
    payload = {
        "ref": "main",
        "inputs": {
            "pr_number": str(pr_number),
            "comment_id": str(comment_id),
            "comment_author": comment_author,
        },
    }
    resp = requests.post(url, headers=gh_headers(token), json=payload, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    log.info("Dispatched CI status check for PR #%d (comment %d)", pr_number, comment_id)


def post_help_comment(token: str, pr_number: int, unknown_command: str = ""):
    """Post a help message listing available commands."""
    help_text = ""
    if unknown_command:
        help_text += f"Unrecognized command `{BOT_TRIGGER} {unknown_command}` — nothing was run.\n\n"
    help_text += f"## amd-bot Help\n\nAvailable commands (mention `{BOT_TRIGGER}` followed by a command):\n\n"
    for cmd, desc in COMMANDS.items():
        help_text += f"- `{BOT_TRIGGER} {cmd}` - {desc}\n"
    help_text += f"\n### Examples\n"
    help_text += f"- `{BOT_TRIGGER} review` - Full PR review\n"
    help_text += f"- `{BOT_TRIGGER} review-focus AMD ROCm compatibility` - Focused review\n"
    help_text += f"- `{BOT_TRIGGER} ci-status` - Check CI failures\n"
    help_text += "\n---\n*Generated by amd-bot*\n"

    url = f"https://api.github.com/repos/{REPO}/issues/{pr_number}/comments"
    resp = requests.post(
        url, headers=gh_headers(token), json={"body": help_text}, timeout=HTTP_TIMEOUT
    )
    resp.raise_for_status()


def add_reaction(
    token: str, comment_id: int, reaction: str = "eyes"
) -> tuple[bool, int | None]:
    """Add a reaction and return ``(created, reaction_id)``.

    GitHub returns 201 when this caller created the reaction and 200 when the
    same reaction already exists.  The distinction makes the rocket an atomic
    cross-watcher claim instead of merely a decorative acknowledgement.
    """
    url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}/reactions"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.squirrel-girl-preview+json"
    resp = requests.post(url, headers=headers, json={"content": reaction}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    return resp.status_code == 201, payload.get("id")


def delete_reaction(token: str, comment_id: int, reaction_id: int) -> None:
    """Release a claim after a definitive dispatch failure."""
    url = (
        f"https://api.github.com/repos/{REPO}/issues/comments/"
        f"{comment_id}/reactions/{reaction_id}"
    )
    resp = requests.delete(url, headers=gh_headers(token), timeout=HTTP_TIMEOUT)
    if resp.status_code not in (204, 404):
        resp.raise_for_status()


def has_bot_claimed(
    token: str,
    comment_id: int,
    claim_logins: set[str],
    reaction: str = "rocket",
) -> bool:
    """Check if the bot has already claimed this comment via a reaction.

    Uses GitHub reactions as a distributed idempotency mechanism so that
    both the cron watcher and the daemon watcher can share state without
    needing a common filesystem or cache.
    """
    url = f"https://api.github.com/repos/{REPO}/issues/comments/{comment_id}/reactions"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.squirrel-girl-preview+json"
    resp = requests.get(
        url, headers=headers, params={"content": reaction, "per_page": 100}, timeout=HTTP_TIMEOUT
    )
    if resp.status_code != 200:
        log.warning("Could not check reactions: HTTP %d", resp.status_code)
        return False
    normalized_logins = {login.lower() for login in claim_logins}
    for r in resp.json():
        login = r.get("user", {}).get("login", "").lower()
        if r.get("content") == reaction and login in normalized_logins:
            return True
    return False


def process_comments(
    upstream_token: str,
    dispatch_token: str,
    bot_repo: str,
    claim_logins: set[str],
    since: str | None = None,
):
    """Process new comments and dispatch actions."""
    comments = get_recent_comments(upstream_token, since)
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

        if not is_pull_request(upstream_token, issue_url):
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

        if has_bot_claimed(upstream_token, cid, claim_logins, "rocket"):
            log.info("Skipping PR #%d - %s (already claimed by another watcher)", cmd["pr_number"], cmd["command"])
            processed_ids.add(cid)
            continue

        log.info("Processing: PR #%d - %s (by @%s)", cmd["pr_number"], cmd["command"], cmd["author"])

        claim_created, claim_id = add_reaction(upstream_token, cid, "rocket")
        if not claim_created:
            log.info(
                "Skipping PR #%d - %s (claim won by another watcher)",
                cmd["pr_number"],
                cmd["command"],
            )
            processed_ids.add(cid)
            continue

        try:
            if cmd["command"] == "review":
                dispatch_review(dispatch_token, bot_repo, cmd["pr_number"], comment_author=cmd["author"], comment_id=cid)
            elif cmd["command"] == "review-focus":
                dispatch_review(dispatch_token, bot_repo, cmd["pr_number"], focus=cmd["args"], comment_author=cmd["author"], comment_id=cid)
            elif cmd["command"] == "ci-status":
                dispatch_ci_status(dispatch_token, bot_repo, cmd["pr_number"], comment_author=cmd["author"], comment_id=cid)
            elif cmd["command"] == "help":
                post_help_comment(upstream_token, cmd["pr_number"])
            else:
                log.warning("Unknown command: %s", cmd["command"])
                post_help_comment(upstream_token, cmd["pr_number"], unknown_command=cmd["command"])
        except requests.exceptions.HTTPError:
            # A concrete 4xx/5xx means no workflow was accepted.  Remove only
            # the rocket created by this watcher so a later poll can retry.
            if claim_id is not None:
                try:
                    delete_reaction(upstream_token, cid, claim_id)
                except requests.exceptions.RequestException:
                    log.exception("Could not release failed claim for comment %d", cid)
            raise

        # Eyes means dispatch/comment succeeded.  Failure to add this cosmetic
        # acknowledgement must not undo the successful rocket claim.
        try:
            add_reaction(upstream_token, cid, "eyes")
        except requests.exceptions.RequestException:
            log.warning("Could not add eyes reaction for comment %d", cid)

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


def run_daemon(
    upstream_token: str,
    dispatch_token: str,
    bot_repo: str,
    poll_interval: int,
    claim_logins: set[str],
):
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
        try:
            since = daemon_since(poll_interval)
            cmds = process_comments(
                upstream_token,
                dispatch_token,
                bot_repo,
                claim_logins,
                since=since,
            )
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
        "--sglang-token", "--github-token", dest="sglang_token",
        default=sglang_token_from_env(),
        help="bingxche token for upstream reads/comments",
    )
    parser.add_argument(
        "--dispatch-token",
        default=bot_repo_token_from_env(),
        help="Token limited to the bot repo with Actions: write",
    )
    parser.add_argument(
        "--actor-login",
        default=os.environ.get("BOT_ACTOR_LOGIN", "bingxche"),
        help="Expected identity of SGLANG_PAT (default: bingxche)",
    )
    parser.add_argument(
        "--claim-logins",
        default=os.environ.get("BOT_CLAIM_LOGINS", "amd-bot,bingxche"),
        help="Comma-separated reaction owners accepted as prior claims",
    )

    args = parser.parse_args()

    level = logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=level,
        stream=sys.stdout,
    )

    if not args.sglang_token:
        log.error("Upstream token required. Set SGLANG_PAT.")
        sys.exit(1)
    if not args.dispatch_token:
        log.error("Bot repository dispatch token required. Set BOT_REPO_TOKEN.")
        sys.exit(1)

    try:
        require_distinct_tokens(args.sglang_token, args.dispatch_token)
        actor_login = validate_sglang_token(
            args.sglang_token, expected_login=args.actor_login,
        )
    except (ValueError, requests.RequestException) as exc:
        log.error("Invalid SGLANG_PAT: %s", exc)
        sys.exit(1)

    claim_logins = {
        login.strip().lower()
        for login in args.claim_logins.split(",")
        if login.strip()
    }
    claim_logins.update({BOT_TRIGGER_LOGIN.lower(), actor_login.lower()})
    log.info(
        "Authenticated upstream actor @%s; trigger remains %s",
        actor_login,
        BOT_TRIGGER,
    )

    if args.daemon:
        run_daemon(
            args.sglang_token,
            args.dispatch_token,
            args.bot_repo,
            args.poll_interval,
            claim_logins,
        )
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=args.since_hours)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        process_comments(
            args.sglang_token,
            args.dispatch_token,
            args.bot_repo,
            claim_logins,
            since=since,
        )


if __name__ == "__main__":
    main()
