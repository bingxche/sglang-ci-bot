#!/usr/bin/env python3
"""
amd-bot CI status checker for a specific PR.

Uses progressive step-by-step analysis (same as monitor_ci.py) to handle
arbitrarily large logs without exceeding token limits.
"""

import argparse
import os
import sys

import requests

from utils import (
    REPO,
    create_anthropic_client,
    cross_job_analysis,
    download_job_logs,
    final_job_analysis,
    gh_headers,
    parse_log_by_steps,
    post_comment,
    progressive_step_analysis,
)


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


def analyze_failed_job(client, check: dict, token: str) -> dict | None:
    """Analyze a single failed job using progressive step-by-step analysis."""
    job_name = check["name"]
    job_id = check.get("id")
    run_url = check.get("html_url", "N/A")

    if not job_id:
        return None

    print(f"\n  Job: {job_name} (ID: {job_id})")

    print("    Downloading job log...")
    raw_log = download_job_logs(token, job_id)
    print(f"    Log size: {len(raw_log):,} chars")

    steps = parse_log_by_steps(raw_log)
    print(f"    Parsed {len(steps)} step(s)")

    failed_step_names = set()
    for s in check.get("steps", []):
        if s.get("conclusion") == "failure":
            failed_step_names.add(s["name"])
    if not failed_step_names:
        failed_step_names = {"(unknown)"}

    print("    Running progressive step analysis...")
    accumulated = progressive_step_analysis(
        client, job_name, steps, failed_step_names
    )

    print("    Generating job analysis...")
    analysis = final_job_analysis(client, job_name, run_url, accumulated)

    return {
        "job_name": job_name,
        "job_id": job_id,
        "run_url": run_url,
        "failed_steps": sorted(failed_step_names),
        "analysis": analysis,
    }


def check_ci_for_pr(
    token: str,
    pr_number: int,
    post_comment_flag: bool = True,
) -> str:
    """Check CI status for a PR and analyze failures."""
    comment_author = os.environ.get("COMMENT_AUTHOR", "")
    print(f"Checking CI for PR #{pr_number}...")

    checks = get_pr_check_runs(token, pr_number)
    get_pr_statuses(token, pr_number)

    passed = [c for c in checks if c.get("conclusion") == "success"]
    failed = [c for c in checks if c.get("conclusion") == "failure"]
    pending = [c for c in checks if c.get("status") == "in_progress" or c.get("conclusion") is None]

    print(f"  Passed: {len(passed)}, Failed: {len(failed)}, Pending: {len(pending)}")

    requester_line = ""
    if comment_author:
        requester_line = f"> @{comment_author}\n\n"

    if not failed:
        pending_note = f" ({len(pending)} still pending)" if pending else ""
        body = f"{requester_line}## CI Status for PR #{pr_number}\n\nAll {len(passed)} checks passed!{pending_note}\n"
    else:
        client = create_anthropic_client()
        all_job_analyses: list[dict] = []

        for check in failed[:5]:
            result = analyze_failed_job(client, check, token)
            if result:
                all_job_analyses.append(result)

        cross = ""
        if len(all_job_analyses) > 1:
            print(f"\n  Cross-job analysis ({len(all_job_analyses)} jobs)...")
            cross = cross_job_analysis(client, f"PR #{pr_number}", all_job_analyses)

        failed_table_rows = "\n".join(
            f"| [`{ja['job_name']}`]({ja['run_url']}) | {', '.join(ja['failed_steps']) or 'N/A'} |"
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

        body = f"""{requester_line}## CI Status for PR #{pr_number}

Passed: {len(passed)} | Failed: {len(failed)} | Pending: {len(pending)}

| Failed Job | Failed Steps |
|-----------|-------------|
{failed_table_rows}

"""
        if cross:
            body += f"### Cross-Job Summary\n\n{cross}\n\n---\n\n"

        body += f"### Per-Job Analysis\n{per_job}\n"

    if post_comment_flag:
        result = post_comment(token, REPO, pr_number, body)
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
        post_comment_flag=not args.no_post,
    )


if __name__ == "__main__":
    main()
