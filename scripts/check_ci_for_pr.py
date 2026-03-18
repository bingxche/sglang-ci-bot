#!/usr/bin/env python3
"""
amd-bot CI status checker for a specific PR.
Triggered via repository_dispatch from the comment watcher.
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import anthropic
import httpx
import requests

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"
CLAUDE_MODEL = "claude-opus-4-6"


def _create_anthropic_client() -> anthropic.Anthropic:
    """Create Anthropic client via AMD LLM Gateway.

    Env vars:
      - LLM_GATEWAY_KEY (required) — gateway subscription key
      - LLM_GATEWAY_URL (required) — gateway endpoint
    """
    import getpass

    return anthropic.Anthropic(
        base_url=os.environ["LLM_GATEWAY_URL"],
        api_key="dummy",
        http_client=httpx.Client(verify=False),
        default_headers={
            "Ocp-Apim-Subscription-Key": os.environ["LLM_GATEWAY_KEY"],
            "user": getpass.getuser(),
            "anthropic-version": "vertex-2023-10-16",
        },
    )


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_pr_check_runs(token: str, pr_number: int) -> list[dict]:
    """Get check runs for a PR's head SHA."""
    pr_url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    resp = requests.get(pr_url, headers=gh_headers(token))
    resp.raise_for_status()
    head_sha = resp.json()["head"]["sha"]

    checks_url = f"https://api.github.com/repos/{REPO}/commits/{head_sha}/check-runs"
    resp = requests.get(checks_url, headers=gh_headers(token), params={"per_page": 100})
    resp.raise_for_status()
    return resp.json().get("check_runs", [])


def get_pr_statuses(token: str, pr_number: int) -> list[dict]:
    """Get commit statuses for a PR."""
    pr_url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    resp = requests.get(pr_url, headers=gh_headers(token))
    resp.raise_for_status()
    head_sha = resp.json()["head"]["sha"]

    status_url = f"https://api.github.com/repos/{REPO}/commits/{head_sha}/status"
    resp = requests.get(status_url, headers=gh_headers(token))
    resp.raise_for_status()
    return resp.json().get("statuses", [])


def download_job_logs(token: str, job_id: int) -> str:
    """Download full logs for a job — no truncation."""
    url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    resp = requests.get(url, headers=gh_headers(token), allow_redirects=True)
    if resp.status_code == 200:
        return resp.text
    return f"[Could not fetch logs: HTTP {resp.status_code}]"


def analyze_ci_with_claude(pr_number: int, checks_summary: str, failure_logs: str) -> str:
    """Ask Claude to summarize CI status."""
    client = _create_anthropic_client()

    prompt = f"""You are a CI/CD expert analyzing the CI status for PR #{pr_number} in the sglang project.

## Check Runs Summary
{checks_summary}

## Failure Logs
{failure_logs}

Please provide:
1. **Overall Status**: How many checks passed/failed/pending?
2. **Failure Summary**: For each failed check, explain what went wrong in 1-2 sentences.
3. **Root Causes**: What are the likely root causes?
4. **Suggested Fixes**: Actionable steps to fix each failure.
5. **Are failures related to this PR or pre-existing?**

Format as clear Markdown for a GitHub comment."""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def check_ci_for_pr(
    token: str,
    pr_number: int,
    post_comment: bool = True,
) -> str:
    """Main function to check CI and summarize."""
    comment_author = os.environ.get("COMMENT_AUTHOR", "")
    print(f"Checking CI for PR #{pr_number}...")

    checks = get_pr_check_runs(token, pr_number)
    statuses = get_pr_statuses(token, pr_number)

    passed = [c for c in checks if c.get("conclusion") == "success"]
    failed = [c for c in checks if c.get("conclusion") == "failure"]
    pending = [c for c in checks if c.get("status") == "in_progress" or c.get("conclusion") is None]

    checks_summary = f"Total: {len(checks)} checks\n"
    checks_summary += f"- Passed: {len(passed)}\n"
    checks_summary += f"- Failed: {len(failed)}\n"
    checks_summary += f"- Pending: {len(pending)}\n"

    if failed:
        checks_summary += "\n### Failed Checks:\n"
        for c in failed:
            checks_summary += f"- **{c['name']}** ({c.get('html_url', 'N/A')})\n"

    failure_logs = ""
    for check in failed[:5]:
        job_id = check.get("id")
        if job_id:
            log = download_job_logs(token, job_id)
            failure_logs += f"\n### {check['name']} (Job ID: {job_id})\n```\n{log}\n```\n"

    print(f"  Passed: {len(passed)}, Failed: {len(failed)}, Pending: {len(pending)}")

    requester_line = ""
    if comment_author:
        requester_line = f"> @{comment_author} requested CI status check\n\n"

    if not failed:
        body = f"{requester_line}## CI Status for PR #{pr_number}\n\nAll {len(passed)} checks passed! "
        if pending:
            body += f"({len(pending)} still pending)"
        body += "\n\n---\n*Automated check by amd-bot*"
    else:
        print("  Analyzing failures with Claude...")
        analysis = analyze_ci_with_claude(pr_number, checks_summary, failure_logs)
        body = f"""{requester_line}## CI Status for PR #{pr_number}

{checks_summary}

---

## Analysis

{analysis}

---
*Automated CI analysis by amd-bot using Claude*
"""

    if post_comment:
        url = f"https://api.github.com/repos/{REPO}/issues/{pr_number}/comments"
        resp = requests.post(url, headers=gh_headers(token), json={"body": body})
        resp.raise_for_status()
        result = resp.json()
        print(f"  Posted: {result['html_url']}")
        return result["html_url"]

    print(body)
    return body


def main():
    parser = argparse.ArgumentParser(description="Check CI status for a sglang PR")
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument("--no-post", action="store_true", help="Print only, don't post")
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
    )

    args = parser.parse_args()

    if not args.github_token:
        print("Error: GitHub token required. Set GH_PAT.", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("LLM_GATEWAY_KEY"):
        print("Error: LLM_GATEWAY_KEY env var required.", file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("LLM_GATEWAY_URL"):
        print("Error: LLM_GATEWAY_URL env var required.", file=sys.stderr)
        sys.exit(1)

    check_ci_for_pr(
        args.github_token,
        args.pr_number,
        post_comment=not args.no_post,
    )


if __name__ == "__main__":
    main()
