#!/usr/bin/env python3
"""
Send the daily CI monitor report to a Microsoft Teams channel.

Finds today's daily issue in bingxche/sglang-ci-bot, extracts the
summary tables and cross-job analysis from each workflow comment,
builds an Adaptive Card, and posts it via an incoming webhook URL.

Requires:
  - GH_PAT or GITHUB_TOKEN env var (read access to the bot repo)
  - TEAMS_WEBHOOK_URL env var (Teams incoming webhook endpoint)
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import requests

log = logging.getLogger("teams-report")

BOT_REPO = "bingxche/sglang-ci-bot"

_SUMMARY_TABLE_RE = re.compile(
    r"\| #.*?\n\|[-| ]+\n((?:\|.*\n)+)", re.MULTILINE
)
_TABLE_ROW_RE = re.compile(
    r"\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|"
)
_WORKFLOW_HEADER_RE = re.compile(
    r"## `(.+?)`\s*—\s*(\d+)\s*failure"
)
_COMMON_ROOT_RE = re.compile(
    r"\*\*Common Root Cause[s]?[.:]*\*\*\s*(.+?)(?:\n\n|\n\*\*)",
    re.DOTALL,
)
_FIX_PRIORITY_RE = re.compile(
    r"\*\*Fix Priority[.:]*\*\*\s*(.+?)(?:\n\n|\Z)",
    re.DOTALL,
)


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def find_daily_issue(token: str, date_str: str) -> dict | None:
    """Find the daily CI monitor issue by title convention."""
    url = f"https://api.github.com/repos/{BOT_REPO}/issues"
    params = {"state": "open", "labels": "ci-monitor", "per_page": 50}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()

    title = f"[CI Monitor] Daily Report - {date_str}"
    for issue in resp.json():
        if issue["title"] == title:
            return issue
    return None


def get_issue_comments(token: str, issue_number: int) -> list[dict]:
    url = f"https://api.github.com/repos/{BOT_REPO}/issues/{issue_number}/comments"
    comments = []
    page = 1
    while True:
        resp = requests.get(
            url, headers=gh_headers(token),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments


def parse_workflow_comment(body: str) -> dict | None:
    """Extract structured data from a workflow comment."""
    header = _WORKFLOW_HEADER_RE.search(body)
    if not header:
        return None

    workflow = header.group(1)
    failure_count = int(header.group(2))

    rows = []
    table_match = _SUMMARY_TABLE_RE.search(body)
    if table_match:
        for row in _TABLE_ROW_RE.finditer(table_match.group(1)):
            rows.append({
                "num": row.group(1),
                "job": row.group(2).strip(),
                "root_cause": row.group(3).strip(),
                "type": row.group(4).strip(),
                "priority": row.group(5).strip(),
            })

    common_root = ""
    m = _COMMON_ROOT_RE.search(body)
    if m:
        common_root = m.group(1).strip()

    fix_priority = ""
    m = _FIX_PRIORITY_RE.search(body)
    if m:
        fix_priority = m.group(1).strip()

    return {
        "workflow": workflow,
        "failure_count": failure_count,
        "rows": rows,
        "common_root": common_root,
        "fix_priority": fix_priority,
    }


def priority_color(p: str) -> str:
    p = p.lower().strip()
    if "critical" in p:
        return "attention"
    if "high" in p:
        return "warning"
    if "medium" in p:
        return "accent"
    return "default"


def priority_emoji(p: str) -> str:
    p = p.lower().strip()
    if "critical" in p:
        return "🔴"
    if "high" in p:
        return "🟠"
    if "medium" in p:
        return "🟡"
    return "🟢"


def build_adaptive_card(
    issue: dict,
    workflows: list[dict],
    date_str: str,
) -> dict:
    """Build a Teams Adaptive Card payload from parsed workflow data."""
    total_failures = sum(w["failure_count"] for w in workflows)
    critical_count = sum(
        1 for w in workflows for r in w["rows"]
        if "critical" in r["priority"].lower()
    )
    high_count = sum(
        1 for w in workflows for r in w["rows"]
        if "high" in r["priority"].lower()
    )

    body_items: list[dict] = []

    # Header
    body_items.append({
        "type": "TextBlock",
        "text": f"CI Monitor — {date_str}",
        "size": "large",
        "weight": "bolder",
        "style": "heading",
    })

    # Stats row
    stats_parts = [f"**{total_failures}** failure(s) across **{len(workflows)}** workflow(s)"]
    if critical_count:
        stats_parts.append(f"🔴 **{critical_count}** critical")
    if high_count:
        stats_parts.append(f"🟠 **{high_count}** high")
    body_items.append({
        "type": "TextBlock",
        "text": " · ".join(stats_parts),
        "wrap": True,
    })

    body_items.append({"type": "TextBlock", "text": " ", "spacing": "small"})

    for wf in workflows:
        body_items.append({
            "type": "TextBlock",
            "text": f"**`{wf['workflow']}`** — {wf['failure_count']} failure(s)",
            "weight": "bolder",
            "spacing": "medium",
            "wrap": True,
        })

        if wf["rows"]:
            # Build a compact fact-set for top-priority items (Critical + High first)
            sorted_rows = sorted(
                wf["rows"],
                key=lambda r: (
                    0 if "critical" in r["priority"].lower()
                    else 1 if "high" in r["priority"].lower()
                    else 2 if "medium" in r["priority"].lower()
                    else 3
                ),
            )

            table_lines = []
            for r in sorted_rows[:15]:
                emoji = priority_emoji(r["priority"])
                cause = r["root_cause"]
                if len(cause) > 80:
                    cause = cause[:77] + "..."
                table_lines.append(
                    f"{emoji} **{r['job']}** — {cause} ({r['type']})"
                )

            remaining = len(sorted_rows) - 15
            if remaining > 0:
                table_lines.append(f"... and {remaining} more")

            body_items.append({
                "type": "TextBlock",
                "text": "\n\n".join(table_lines),
                "wrap": True,
                "size": "small",
            })

        if wf["common_root"]:
            text = wf["common_root"]
            if len(text) > 300:
                text = text[:297] + "..."
            body_items.append({
                "type": "TextBlock",
                "text": f"**Root Causes:** {text}",
                "wrap": True,
                "size": "small",
                "isSubtle": True,
            })

        if wf["fix_priority"]:
            text = wf["fix_priority"]
            if len(text) > 200:
                text = text[:197] + "..."
            body_items.append({
                "type": "TextBlock",
                "text": f"**Fix Priority:** {text}",
                "wrap": True,
                "size": "small",
                "isSubtle": True,
            })

    # "No failures" case
    if not workflows:
        body_items.append({
            "type": "TextBlock",
            "text": "✅ No CI failures reported today.",
            "size": "medium",
            "color": "good",
            "wrap": True,
        })

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body_items,
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "View Full Report",
                "url": issue["html_url"],
            },
            {
                "type": "Action.OpenUrl",
                "title": "sglang Actions",
                "url": "https://github.com/sgl-project/sglang/actions",
            },
        ],
    }

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }


def send_to_teams(webhook_url: str, payload: dict) -> bool:
    """Post an Adaptive Card payload to a Teams incoming webhook."""
    resp = requests.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code in (200, 202):
        log.info("Message sent to Teams (HTTP %d)", resp.status_code)
        return True

    log.error(
        "Teams webhook returned HTTP %d: %s",
        resp.status_code, resp.text[:500],
    )
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Send daily CI report to Microsoft Teams",
    )
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="Report date (YYYY-MM-DD, default: today UTC)",
    )
    parser.add_argument(
        "--webhook-url",
        default=os.environ.get("TEAMS_WEBHOOK_URL", ""),
        help="Teams incoming webhook URL (or set TEAMS_WEBHOOK_URL env var)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get(
            "GH_PAT", os.environ.get("GITHUB_TOKEN", "")
        ),
        help="GitHub token for API access",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the Adaptive Card JSON instead of sending",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stdout,
    )

    if not args.github_token:
        log.error("GitHub token required. Set GH_PAT or GITHUB_TOKEN.")
        sys.exit(1)

    if not args.webhook_url and not args.dry_run:
        log.error("Teams webhook URL required. Set TEAMS_WEBHOOK_URL.")
        sys.exit(1)

    date_str = args.date
    log.info("Fetching daily report for %s...", date_str)

    issue = find_daily_issue(args.github_token, date_str)
    if not issue:
        log.warning("No daily issue found for %s", date_str)
        # Still send a "no report" card so the team knows the bot is alive
        issue = {
            "html_url": f"https://github.com/{BOT_REPO}/issues",
            "number": 0,
            "title": f"[CI Monitor] Daily Report - {date_str}",
        }
        payload = build_adaptive_card(issue, [], date_str)
    else:
        log.info("Found issue #%d: %s", issue["number"], issue["title"])
        comments = get_issue_comments(args.github_token, issue["number"])
        log.info("Fetched %d comment(s)", len(comments))

        workflows = []
        for comment in comments:
            parsed = parse_workflow_comment(comment["body"])
            if parsed:
                workflows.append(parsed)
                log.info(
                    "  Parsed %s: %d failures",
                    parsed["workflow"], parsed["failure_count"],
                )

        payload = build_adaptive_card(issue, workflows, date_str)

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return

    payload_size = len(json.dumps(payload))
    log.info("Adaptive Card payload: %d bytes", payload_size)
    if payload_size > 28_000:
        log.warning("Payload exceeds 28KB — Teams may reject it")

    ok = send_to_teams(args.webhook_url, payload)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
