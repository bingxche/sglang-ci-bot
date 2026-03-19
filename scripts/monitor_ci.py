#!/usr/bin/env python3
"""
amd-bot CI Failure Monitor for sglang.

Monitors specified CI workflows, fetches full logs from failed jobs,
uses progressive step-by-step analysis with Claude to build a cumulative
understanding across all steps (not just failed ones), then posts daily
summaries as GitHub issues.
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from utils import (
    REPO,
    create_anthropic_client,
    create_github_issue,
    cross_job_analysis,
    download_job_logs,
    final_job_analysis,
    gh_headers,
    parse_log_by_steps,
    post_comment,
    progressive_step_analysis,
)

MONITORED_WORKFLOWS = [
    "nightly-test-amd.yml",
    "nightly-test-amd-rocm720.yml",
    "release-docker-amd-nightly.yml",
    "release-docker-amd-rocm720-nightly.yml",
    "amd-aiter-scout.yml",
]

STATE_FILE = Path(__file__).parent.parent / ".state" / "ci_monitor.json"
MAX_STATE_IDS = 500


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_run_ids": []}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["processed_run_ids"] = state["processed_run_ids"][-MAX_STATE_IDS:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# GitHub API helpers (workflow-specific)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Daily issue output
# ---------------------------------------------------------------------------

def find_or_create_daily_issue(
    token: str, bot_repo: str, date_str: str
) -> tuple[int, bool]:
    """Find or create the daily CI monitoring issue. Returns (number, created)."""
    url = f"https://api.github.com/repos/{bot_repo}/issues"
    params = {"state": "open", "labels": "ci-monitor", "per_page": 50}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()

    title = f"[CI Monitor] Daily Report - {date_str}"
    for issue in resp.json():
        if issue["title"] == title:
            return issue["number"], False

    wf_list = "\n".join(f"- `{w}`" for w in MONITORED_WORKFLOWS)
    body = f"""## CI Monitor — {date_str}

**Repo**: [{REPO}](https://github.com/{REPO})

**Monitored Workflows**:
{wf_list}

*Failure reports are appended as comments below.*
"""
    issue = create_github_issue(
        token, title, body, labels=["ci-monitor"], repo=bot_repo
    )
    return issue["number"], True


# ---------------------------------------------------------------------------
# Main monitoring flow
# ---------------------------------------------------------------------------

def monitor_workflow(
    token: str,
    workflow_file: str,
    hours_back: int = 24,
    processed_run_ids: set[int] | None = None,
    job_name_filter: str | None = None,
) -> tuple[str | None, list[int]]:
    """Monitor a single workflow. Returns (report_body | None, new_run_ids)."""
    print(f"\n{'='*60}")
    print(f"Monitoring: {workflow_file}")
    print(f"{'='*60}")

    runs = get_workflow_runs(token, workflow_file, hours_back=hours_back)
    if not runs:
        print(f"  No failed runs in the last {hours_back} hours.")
        return None, []

    if processed_run_ids:
        runs = [r for r in runs if r["id"] not in processed_run_ids]
    if not runs:
        print("  All failed runs already processed.")
        return None, []

    new_run_ids = [r["id"] for r in runs]
    print(f"  Found {len(runs)} new failed run(s)")

    client = create_anthropic_client()
    all_job_analyses: list[dict] = []

    for run in runs:
        run_id = run["id"]
        run_url = run["html_url"]
        print(f"\n  Run {run_id}: {run_url}")

        failed_jobs = get_failed_jobs(token, run_id)
        if not failed_jobs:
            print("    No failed jobs (run may have been retried)")
            continue

        if job_name_filter:
            failed_jobs = [j for j in failed_jobs if job_name_filter in j["name"]]
            if not failed_jobs:
                print(f"    No jobs matching '{job_name_filter}'")
                continue

        for job in failed_jobs:
            job_name = job["name"]
            job_id = job["id"]
            print(f"\n    Job: {job_name} (ID: {job_id})")

            failed_step_names = {
                s["name"]
                for s in job.get("steps", [])
                if s.get("conclusion") == "failure"
            }

            print("    Downloading full job log...")
            raw_log = download_job_logs(token, job_id)
            print(f"    Log size: {len(raw_log):,} chars")

            steps = parse_log_by_steps(raw_log)
            print(f"    Parsed {len(steps)} step(s)")

            print("    Running progressive step analysis...")
            accumulated = progressive_step_analysis(
                client, job_name, steps, failed_step_names
            )

            print("    Generating final job analysis...")
            analysis = final_job_analysis(client, job_name, run_url, accumulated)

            all_job_analyses.append({
                "run_id": run_id,
                "run_url": run_url,
                "job_name": job_name,
                "job_id": job_id,
                "started_at": job.get("started_at"),
                "failed_steps": sorted(failed_step_names),
                "analysis": analysis,
            })

    if not all_job_analyses:
        print("  No actionable failures found.")
        return None, new_run_ids

    cross = ""
    if len(all_job_analyses) > 1:
        print(f"\n  Cross-job analysis ({len(all_job_analyses)} jobs)...")
        cross = cross_job_analysis(client, workflow_file, all_job_analyses)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    job_table_rows = "\n".join(
        f"| [`{ja['job_name']}`]({ja['run_url']}) | {', '.join(ja['failed_steps']) or 'N/A'} | {ja.get('started_at', 'N/A')[:16] if ja.get('started_at') else 'N/A'} |"
        for ja in all_job_analyses
    )

    per_job = ""
    for ja in all_job_analyses:
        per_job += f"""
<details>
<summary><b>{ja['job_name']}</b> — failed step(s): {', '.join(ja['failed_steps']) or 'N/A'}</summary>

{ja['analysis']}

</details>
"""

    body = f"""## `{workflow_file}` — {len(all_job_analyses)} failure(s)

**Scanned**: {now}

| Job | Failed Steps | Started |
|-----|-------------|---------|
{job_table_rows}

"""

    if cross:
        body += f"### Cross-Job Summary\n\n{cross}\n\n---\n\n"

    body += f"### Per-Job Analysis\n{per_job}\n"
    body += "\n---\n*Generated by amd-bot*\n"

    return body, new_run_ids


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
        choices=["stdout", "daily-issue"],
        default="stdout",
        help="Output mode (default: stdout)",
    )
    parser.add_argument(
        "--bot-repo",
        help="Bot repo for posting issues (e.g. 'user/sglang-ci-bot')",
    )
    parser.add_argument(
        "--job-name",
        help="Only analyze jobs whose name contains this string",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
        help="GitHub token",
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

    state = load_state()
    processed_run_ids = set(state.get("processed_run_ids", []))

    all_reports: list[dict] = []

    for wf in args.workflows:
        try:
            body, new_ids = monitor_workflow(
                args.github_token,
                wf,
                hours_back=args.hours_back,
                processed_run_ids=processed_run_ids,
                job_name_filter=args.job_name,
            )
            processed_run_ids.update(new_ids)
            if body:
                all_reports.append({"workflow": wf, "body": body})
        except Exception as e:
            print(f"Error monitoring {wf}: {e}", file=sys.stderr)
            traceback.print_exc()

    state["processed_run_ids"] = list(processed_run_ids)
    save_state(state)

    if not all_reports:
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a") as f:
                f.write("has_failures=false\n")
        print("\nDone. No new failures found.")
        return

    if args.output == "stdout":
        for r in all_reports:
            print(f"\n{'='*60}")
            print(r["body"])

    elif args.output == "daily-issue" and args.bot_repo:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        issue_num, created = find_or_create_daily_issue(
            args.github_token, args.bot_repo, date_str
        )
        print(f"\n{'Created' if created else 'Found'} daily issue #{issue_num}")

        for r in all_reports:
            comment = post_comment(
                args.github_token, args.bot_repo, issue_num, r["body"]
            )
            print(f"  Posted: {r['workflow']} -> {comment['html_url']}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write("has_failures=true\n")
            f.write(f"failure_count={len(all_reports)}\n")

    print(f"\nDone. {len(all_reports)} workflow(s) had failures.")


if __name__ == "__main__":
    main()
