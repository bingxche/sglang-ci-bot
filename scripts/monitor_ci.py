#!/usr/bin/env python3
"""
CI Failure Monitor for sglang.

Monitors specified CI workflows, fetches logs from failed jobs,
uses Claude to analyze failures and propose fixes, then posts
summaries as GitHub issues or comments.
"""

import argparse
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import anthropic
import requests

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"

MONITORED_WORKFLOWS = [
    "nightly-test-amd.yml",
    "nightly-test-amd-rocm720.yml",
    "nightly-test-nvidia.yml",
    "pr-test-amd.yml",
    "pr-test.yml",
    "release-docker-amd-nightly.yml",
]

MAX_LOG_CHARS = 80000
CLAUDE_MODEL = "claude-opus-4-6"


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_workflow_runs(
    token: str,
    workflow_file: str,
    status: str = "failure",
    hours_back: int = 24,
    max_runs: int = 5,
) -> list[dict]:
    """Fetch recent workflow runs with the given status."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow_file}/runs"
    params = {
        "status": status,
        "per_page": max_runs,
        "created": f">={since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    }
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()
    return resp.json().get("workflow_runs", [])


def get_failed_jobs(token: str, run_id: int) -> list[dict]:
    """Get all failed jobs for a workflow run."""
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
    params = {"filter": "latest", "per_page": 100}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [j for j in jobs if j["conclusion"] == "failure"]


def download_job_logs(token: str, job_id: int) -> str:
    """Download logs for a specific job."""
    url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    resp = requests.get(url, headers=gh_headers(token), allow_redirects=True)
    if resp.status_code == 200:
        return resp.text[:MAX_LOG_CHARS]
    return f"[Failed to fetch logs: HTTP {resp.status_code}]"


def download_run_logs(token: str, run_id: int) -> dict[str, str]:
    """Download all logs for a workflow run as a zip, return dict of job_name -> log_text."""
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/logs"
    resp = requests.get(url, headers=gh_headers(token), allow_redirects=True)
    if resp.status_code != 200:
        return {}
    logs = {}
    try:
        with zipfile.ZipFile(BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                with zf.open(name) as f:
                    logs[name] = f.read().decode("utf-8", errors="replace")[
                        :MAX_LOG_CHARS
                    ]
    except zipfile.BadZipFile:
        pass
    return logs


def truncate_log_smart(log: str, max_chars: int = MAX_LOG_CHARS) -> str:
    """Keep the most relevant parts of a log: beginning context + tail with errors."""
    if len(log) <= max_chars:
        return log

    head_size = max_chars // 5
    tail_size = max_chars - head_size - 200
    return (
        log[:head_size]
        + "\n\n... [TRUNCATED - showing last portion] ...\n\n"
        + log[-tail_size:]
    )


def analyze_failures_with_claude(
    api_key: str, workflow_name: str, failures: list[dict]
) -> str:
    """Send failure info to Claude for analysis."""
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    client = anthropic.Anthropic(api_key=api_key, **({"base_url": base_url} if base_url else {}))

    failure_details = []
    for f in failures:
        detail = f"### Job: {f['job_name']}\n"
        detail += f"- Run ID: {f['run_id']}\n"
        detail += f"- Run URL: {f['run_url']}\n"
        detail += f"- Started: {f.get('started_at', 'N/A')}\n"
        detail += f"- Failed steps: {', '.join(f.get('failed_steps', []))}\n"
        detail += f"\n#### Log (truncated):\n```\n{f['log']}\n```\n"
        failure_details.append(detail)

    prompt = f"""You are a CI/CD expert analyzing failures in the sglang project (a fast serving framework for large language models).

## Workflow: {workflow_name}
## Number of failures: {len(failures)}

{chr(10).join(failure_details)}

Please provide:

1. **Summary**: A concise summary of each failure (1-2 sentences each).
2. **Root Cause Analysis**: What likely caused each failure? Group related failures.
3. **Potential Fixes**: Specific, actionable suggestions to fix each issue.
4. **Priority**: Rate each failure as Critical/High/Medium/Low based on impact.
5. **Patterns**: Any recurring patterns across failures.

Format your response in clear Markdown suitable for a GitHub issue."""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def create_github_issue(token: str, title: str, body: str, labels: list[str] | None = None, repo: str | None = None) -> dict:
    """Create a GitHub issue."""
    target_repo = repo or REPO
    url = f"https://api.github.com/repos/{target_repo}/issues"
    data = {"title": title, "body": body}
    if labels:
        data["labels"] = labels
    resp = requests.post(url, headers=gh_headers(token), json=data)
    resp.raise_for_status()
    return resp.json()


def post_comment_on_issue(token: str, issue_number: int, body: str, repo: str | None = None) -> dict:
    """Post a comment on an existing issue or PR."""
    target_repo = repo or REPO
    url = f"https://api.github.com/repos/{target_repo}/issues/{issue_number}/comments"
    resp = requests.post(url, headers=gh_headers(token), json={"body": body})
    resp.raise_for_status()
    return resp.json()


def find_or_create_tracking_issue(token: str, workflow_name: str, bot_repo: str) -> int:
    """Find existing tracking issue or create one in the bot repo."""
    url = f"https://api.github.com/repos/{bot_repo}/issues"
    params = {"state": "open", "labels": "ci-monitor", "per_page": 100}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()

    title_prefix = f"[CI Monitor] {workflow_name}"
    for issue in resp.json():
        if issue["title"].startswith(title_prefix):
            return issue["number"]

    issue = create_github_issue(
        token,
        f"{title_prefix} - Failure Tracking",
        f"Automated tracking issue for CI failures in `{workflow_name}` workflow.\n\n"
        f"Repo: [{REPO}](https://github.com/{REPO})\n",
        labels=["ci-monitor"],
        repo=bot_repo,
    )
    return issue["number"]


def monitor_workflow(
    token: str,
    anthropic_key: str,
    workflow_file: str,
    hours_back: int = 24,
    bot_repo: str | None = None,
    output_mode: str = "issue",
) -> str | None:
    """Monitor a single workflow for failures."""
    print(f"\n{'='*60}")
    print(f"Monitoring: {workflow_file}")
    print(f"{'='*60}")

    runs = get_workflow_runs(token, workflow_file, hours_back=hours_back)
    if not runs:
        print(f"  No failed runs in the last {hours_back} hours.")
        return None

    print(f"  Found {len(runs)} failed run(s)")

    all_failures = []
    for run in runs:
        run_id = run["id"]
        run_url = run["html_url"]
        print(f"  Processing run {run_id}: {run_url}")

        failed_jobs = get_failed_jobs(token, run_id)
        if not failed_jobs:
            print(f"    No failed jobs found (run may have been retried)")
            continue

        for job in failed_jobs:
            log = download_job_logs(token, job["id"])
            log = truncate_log_smart(log)

            failed_steps = [
                s["name"]
                for s in job.get("steps", [])
                if s.get("conclusion") == "failure"
            ]

            all_failures.append(
                {
                    "run_id": run_id,
                    "run_url": run_url,
                    "job_name": job["name"],
                    "job_id": job["id"],
                    "started_at": job.get("started_at"),
                    "failed_steps": failed_steps,
                    "log": log,
                }
            )

    if not all_failures:
        print("  No actionable failures found.")
        return None

    print(f"  Analyzing {len(all_failures)} failure(s) with Claude...")
    analysis = analyze_failures_with_claude(anthropic_key, workflow_file, all_failures)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"[CI Monitor] {workflow_file} - {len(all_failures)} failure(s) - {today}"

    run_links = "\n".join(
        f"- [{f['job_name']}]({f['run_url']}) (Run #{f['run_id']})"
        for f in all_failures
    )

    body = f"""## CI Failure Report: `{workflow_file}`

**Date**: {today}
**Failures found**: {len(all_failures)}
**Time window**: Last {hours_back} hours

### Failed Runs
{run_links}

---

## Claude Analysis

{analysis}

---
*Generated automatically by sglang-ci-bot*
"""

    if output_mode == "stdout":
        print(body)
        return body

    if output_mode == "issue" and bot_repo:
        issue = create_github_issue(token, title, body, labels=["ci-monitor"], repo=bot_repo)
        print(f"  Created issue: {issue['html_url']}")
        return issue["html_url"]

    if output_mode == "comment" and bot_repo:
        issue_num = find_or_create_tracking_issue(token, workflow_file, bot_repo)
        comment = post_comment_on_issue(token, issue_num, body, repo=bot_repo)
        print(f"  Posted comment: {comment['html_url']}")
        return comment["html_url"]

    print(body)
    return body


def main():
    parser = argparse.ArgumentParser(description="Monitor sglang CI failures")
    parser.add_argument(
        "--workflows",
        nargs="*",
        default=MONITORED_WORKFLOWS,
        help="Workflow files to monitor",
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=24,
        help="How many hours back to check (default: 24)",
    )
    parser.add_argument(
        "--output",
        choices=["stdout", "issue", "comment"],
        default="stdout",
        help="Where to output results",
    )
    parser.add_argument(
        "--bot-repo",
        help="Your bot repo (e.g., 'username/sglang-ci-bot') for posting issues",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
        help="GitHub token",
    )
    parser.add_argument(
        "--anthropic-key",
        default=os.environ.get("ANTHROPIC_API_KEY", ""),
        help="Anthropic API key",
    )

    args = parser.parse_args()

    if not args.github_token:
        print("Error: GitHub token required. Set GH_PAT or GITHUB_TOKEN env var.", file=sys.stderr)
        sys.exit(1)
    if not args.anthropic_key:
        print("Error: Anthropic API key required. Set ANTHROPIC_API_KEY env var.", file=sys.stderr)
        sys.exit(1)

    results = []
    for wf in args.workflows:
        try:
            result = monitor_workflow(
                args.github_token,
                args.anthropic_key,
                wf,
                hours_back=args.hours_back,
                bot_repo=args.bot_repo,
                output_mode=args.output,
            )
            if result:
                results.append(result)
        except Exception as e:
            print(f"Error monitoring {wf}: {e}", file=sys.stderr)

    if results:
        summary = json.dumps(results, indent=2)
        output_file = os.environ.get("GITHUB_OUTPUT")
        if output_file:
            with open(output_file, "a") as f:
                f.write(f"has_failures=true\n")
                f.write(f"failure_count={len(results)}\n")
        print(f"\nDone. {len(results)} workflow(s) had failures.")
    else:
        output_file = os.environ.get("GITHUB_OUTPUT")
        if output_file:
            with open(output_file, "a") as f:
                f.write(f"has_failures=false\n")
        print("\nDone. No failures found.")


if __name__ == "__main__":
    main()
