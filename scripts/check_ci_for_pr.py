#!/usr/bin/env python3
"""
amd-bot CI status checker for a specific PR.

Groups failures by workflow, extracts error previews with deep-link URLs,
and uses LLM analysis to assess whether failures correlate with the PR changes.
"""

import argparse
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from utils import (
    CLAUDE_MODEL,
    GATE_STEP_PATTERNS,
    REPO,
    analyze_failed_job_focused,
    create_anthropic_client,
    cross_job_analysis,
    download_job_logs,
    extract_env_context,
    extract_error_lines,
    get_pr_changed_files,
    get_pr_diff,
    get_run_jobs,
    get_workflow_runs_for_sha,
    gh_headers,
    parse_log_by_steps,
    post_comment,
)

MAX_FAILED_JOBS = 8
MAX_DIFF_CHARS = 50_000


# ---------------------------------------------------------------------------
# PR / Workflow helpers
# ---------------------------------------------------------------------------

def get_pr_head_sha(token: str, pr_number: int) -> str:
    """Get the head commit SHA for a PR."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    resp = requests.get(url, headers=gh_headers(token))
    resp.raise_for_status()
    return resp.json()["head"]["sha"]


def collect_workflow_status(token: str, head_sha: str) -> dict:
    """Collect all workflow runs for a SHA, grouped by workflow.

    Only fetches per-job details for failed workflow runs to minimize
    API calls.  Returns workflow-level counts and failure details.
    """
    wf_runs = get_workflow_runs_for_sha(token, head_sha)

    # Deduplicate: keep the latest run per workflow (by name)
    latest_by_wf: dict[str, dict] = {}
    for run in wf_runs:
        wf_name = run.get("name", run.get("path", "unknown"))
        existing = latest_by_wf.get(wf_name)
        if existing is None or run["id"] > existing["id"]:
            latest_by_wf[wf_name] = run

    wf_passed = 0
    wf_failed = 0
    wf_pending = 0
    failed_workflows: list[dict] = []

    for wf_name, run in sorted(latest_by_wf.items()):
        status = run.get("status")
        conclusion = run.get("conclusion")

        if conclusion == "success":
            wf_passed += 1
        elif status in ("in_progress", "queued", "waiting", "requested"):
            wf_pending += 1
        elif conclusion in ("failure", "timed_out", "action_required"):
            wf_failed += 1

            jobs = get_run_jobs(token, run["id"])
            failed_jobs = [
                j for j in jobs
                if j.get("conclusion") in ("failure", "timed_out")
            ]
            if failed_jobs:
                failed_workflows.append({
                    "name": wf_name,
                    "path": run.get("path", ""),
                    "run_id": run["id"],
                    "run_url": run["html_url"],
                    "failed_jobs": failed_jobs,
                })
        elif conclusion == "cancelled":
            pass
        else:
            wf_pending += 1

    return {
        "wf_passed": wf_passed,
        "wf_failed": wf_failed,
        "wf_pending": wf_pending,
        "failed_workflows": failed_workflows,
    }


# ---------------------------------------------------------------------------
# Gate job detection
# ---------------------------------------------------------------------------

def _is_gate_job(job: dict) -> bool:
    """Return True if the job is a coordinator/gate job.

    Gate jobs (e.g. pr-test-finish, wait-for-stage-b) only check whether
    dependent jobs passed. Their failure is always "a dependent job failed"
    and they carry no diagnostic value worth an LLM call.
    """
    failed_steps = [
        s for s in job.get("steps", [])
        if s.get("conclusion") == "failure"
    ]
    if not failed_steps:
        return False
    return all(GATE_STEP_PATTERNS.search(s["name"]) for s in failed_steps)


# ---------------------------------------------------------------------------
# Per-job analysis
# ---------------------------------------------------------------------------

def analyze_failed_job(
    client, job: dict, run_id: int, run_url: str, token: str,
) -> dict | None:
    """Analyze a single failed job: extract error previews + LLM analysis.

    Gate/coordinator jobs are returned with a minimal static analysis
    instead of consuming an LLM call.
    """
    job_name = job["name"]
    job_id = job["id"]
    job_url = job.get("html_url", run_url)
    api_steps = job.get("steps", [])

    failed_step_names: set[str] = set()
    for s in api_steps:
        if s.get("conclusion") == "failure":
            failed_step_names.add(s["name"])
    if not failed_step_names:
        failed_step_names = {"(unknown)"}

    if _is_gate_job(job):
        print(f"\n  Job: {job_name} (ID: {job_id}) — gate job, skipping LLM analysis")
        return {
            "job_name": job_name,
            "job_id": job_id,
            "run_id": run_id,
            "run_url": run_url,
            "job_url": job_url,
            "failed_steps": sorted(failed_step_names),
            "error_lines": [],
            "analysis": "Gate/coordinator job — failed because a dependent job failed. See the actual failing job(s) above.",
            "is_gate": True,
        }

    print(f"\n  Job: {job_name} (ID: {job_id})")

    print("    Downloading job log...")
    raw_log = download_job_logs(token, job_id)
    print(f"    Log size: {len(raw_log):,} chars")

    steps = parse_log_by_steps(raw_log)
    print(f"    Parsed {len(steps)} step(s)")

    print("    Extracting error lines (failed steps only)...")
    error_lines = extract_error_lines(
        raw_log, api_steps, run_id, job_id,
        failed_step_names=failed_step_names,
    )
    print(f"    Found {len(error_lines)} error line(s)")

    print("    Extracting environment context...")
    env_context = extract_env_context(steps)
    print(f"    {env_context}")

    failed_step_logs = [
        s for s in steps if s["name"] in failed_step_names
    ]
    if not failed_step_logs:
        failed_step_logs = steps[-2:] if len(steps) >= 2 else steps

    print(f"    Running focused analysis on {len(failed_step_logs)} failed step(s)...")
    analysis = analyze_failed_job_focused(
        client, job_name, run_url, failed_step_logs, env_context,
    )

    return {
        "job_name": job_name,
        "job_id": job_id,
        "run_id": run_id,
        "run_url": run_url,
        "job_url": job_url,
        "failed_steps": sorted(failed_step_names),
        "error_lines": error_lines,
        "analysis": analysis,
        "is_gate": False,
    }


# ---------------------------------------------------------------------------
# PR correlation analysis
# ---------------------------------------------------------------------------

def analyze_pr_correlation(
    client,
    pr_number: int,
    changed_files: list[dict],
    pr_diff: str,
    all_job_analyses: list[dict],
) -> str:
    """Ask LLM whether CI failures correlate with the PR changes."""
    files_summary = "\n".join(
        f"- `{f['filename']}` ({f.get('status', '?')}, "
        f"+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
        for f in changed_files[:50]
    )
    if len(changed_files) > 50:
        files_summary += f"\n- ... and {len(changed_files) - 50} more files"

    diff_text = pr_diff[:MAX_DIFF_CHARS]
    if len(pr_diff) > MAX_DIFF_CHARS:
        diff_text += "\n\n... [diff truncated] ..."

    errors_summary = ""
    for ja in all_job_analyses:
        if ja.get("is_gate"):
            continue
        errors_summary += f"\n#### Job: `{ja['job_name']}`\n"
        errors_summary += f"Failed step(s): {', '.join(ja['failed_steps'])}\n"
        if ja["error_lines"]:
            for el in ja["error_lines"][:3]:
                errors_summary += f"- `{el['preview']}`\n"
        errors_summary += f"\nAnalysis excerpt:\n{ja['analysis'][:1500]}\n"

    prompt = f"""You are a CI/CD expert. A developer submitted PR #{pr_number} to the sglang project (LLM serving framework). Some CI jobs failed. Assess whether each failure is likely caused by the PR changes or is a pre-existing / infrastructure issue.

## PR Changed Files
{files_summary}

## PR Diff (may be truncated)
```
{diff_text}
```

## CI Failures
{errors_summary}

For EACH failed job, provide your assessment in this exact markdown table format:

| Job | Verdict | Explanation |
|-----|---------|-------------|
| job_name / failed_step | :red_circle: **Likely related** | One sentence explanation |

Rules:
- Use ":red_circle: **Likely related**" = the error clearly involves code paths touched by the PR
- Use ":yellow_circle: **Possibly related**" = the error could be influenced by the PR but also has other explanations
- Use ":green_circle: **Unlikely related**" = the error is in unrelated code, infrastructure, or a known flaky test
- You MUST include both the emoji AND the bold text for every verdict
- Keep explanations to ONE concise sentence each
- Do NOT add any text outside the table"""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------

def _pick_best_error(ja: dict) -> dict | None:
    """Pick the most relevant error line.

    With structural extraction, errors are already ordered with the most
    specific at the end (root cause exception, not Traceback header).

    Priority: ##[error] annotations > Python exceptions > tail lines.
    Within each source, the last match wins (it's the most specific).
    """
    if not ja["error_lines"]:
        return None

    annotations = [e for e in ja["error_lines"] if e.get("source") == "annotation"]
    if annotations:
        return annotations[-1]

    exceptions = [e for e in ja["error_lines"] if e.get("source") == "exception"]
    if exceptions:
        return exceptions[-1]

    return ja["error_lines"][-1]


def _format_error_table(analyses: list[dict]) -> str:
    """Build a markdown table of failed jobs with error previews."""
    rows = "| Job | Failed Step | Error Message | Log |\n"
    rows += "|-----|-----------|---------------|-----|\n"

    for ja in analyses:
        failed_steps_str = ", ".join(ja["failed_steps"]) or "N/A"
        best = _pick_best_error(ja)

        if best:
            preview = best["preview"]
            if len(preview) > 200:
                preview = preview[:200] + "..."
            preview = preview.replace("|", "\\|")
            log_link = f"[View]({best['url']})"
        else:
            preview = "*(see detailed analysis)*"
            log_link = f"[View]({ja['job_url']})"

        rows += (
            f"| `{ja['job_name']}` | {failed_steps_str} "
            f"| `{preview}` | {log_link} |\n"
        )

    return rows


def _format_details(ja: dict) -> str:
    """Build a collapsible per-job detail section.

    Only shows error locations from failed steps (not preamble/passed
    step noise), limited to the 5 most relevant lines.
    """
    if ja.get("is_gate"):
        return f"""
<details>
<summary><b>{ja['job_name']}</b> — gate job (dependent job failed)</summary>

{ja['analysis']}

</details>
"""

    failed = set(ja["failed_steps"])
    relevant_errors = [
        el for el in ja["error_lines"]
        if el["step_name"] in failed
    ]

    error_listing = ""
    if relevant_errors:
        error_listing = "**Error locations:**\n"
        for el in relevant_errors[:5]:
            short = el["preview"][:150]
            error_listing += (
                f"- [{el['step_name']} L{el['line_number']}]({el['url']})"
                f": `{short}`\n"
            )
        error_listing += "\n"

    return f"""
<details>
<summary><b>{ja['job_name']}</b> — failed step(s): {', '.join(ja['failed_steps']) or 'N/A'}</summary>

{error_listing}{ja['analysis']}

</details>
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def check_ci_for_pr(
    token: str,
    pr_number: int,
    post_comment_flag: bool = True,
) -> str:
    """Check CI status for a PR: workflow grouping, error URLs, PR correlation."""
    comment_author = os.environ.get("COMMENT_AUTHOR", "")
    print(f"Checking CI for PR #{pr_number}...")

    head_sha = get_pr_head_sha(token, pr_number)
    print(f"  Head SHA: {head_sha[:12]}")

    status = collect_workflow_status(token, head_sha)
    wf_passed = status["wf_passed"]
    wf_failed = status["wf_failed"]
    wf_pending = status["wf_pending"]
    failed_workflows = status["failed_workflows"]

    print(
        f"  Workflows — Passed: {wf_passed}, Failed: {wf_failed}, "
        f"Pending: {wf_pending}"
    )

    requester_line = f"> @{comment_author}\n\n" if comment_author else ""

    if not failed_workflows:
        pending_note = f" ({wf_pending} still pending)" if wf_pending else ""
        body = (
            f"{requester_line}## CI Status for PR #{pr_number}\n\n"
            f"All {wf_passed} workflow(s) passed!{pending_note}\n"
        )
    else:
        client = create_anthropic_client()
        all_job_analyses: list[dict] = []

        job_queue: list[tuple[dict, dict]] = []
        for wf in failed_workflows:
            for job in wf["failed_jobs"]:
                job_queue.append((wf, job))

        jobs_to_analyze = job_queue[:MAX_FAILED_JOBS]
        n_jobs = len(jobs_to_analyze)
        max_workers = min(n_jobs + 2, 6)
        print(f"\n  Analyzing {n_jobs} job(s) concurrently (workers={max_workers})...")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            job_futures = {}
            for wf, job in jobs_to_analyze:
                fut = executor.submit(
                    analyze_failed_job, client, job,
                    wf["run_id"], wf["run_url"], token,
                )
                job_futures[fut] = wf

            diff_future = executor.submit(get_pr_diff, token, pr_number)
            files_future = executor.submit(get_pr_changed_files, token, pr_number)

            for fut in as_completed(job_futures):
                wf = job_futures[fut]
                try:
                    ja = fut.result()
                except Exception as exc:
                    print(f"  ERROR analyzing job in {wf['name']}: {exc}")
                    continue
                if ja:
                    ja["workflow_name"] = wf["name"]
                    all_job_analyses.append(ja)

            pr_diff = diff_future.result()
            changed_files = files_future.result()

        print(
            f"\n  PR diff: {len(pr_diff):,} chars, "
            f"{len(changed_files)} file(s) changed"
        )

        real_analyses = [ja for ja in all_job_analyses if not ja.get("is_gate")]

        correlation = ""
        if real_analyses and pr_diff:
            print("\n  Running PR correlation analysis...")
            correlation = analyze_pr_correlation(
                client, pr_number, changed_files, pr_diff, real_analyses,
            )

        cross = ""
        if len(real_analyses) > 1:
            print(f"\n  Cross-job analysis ({len(real_analyses)} jobs)...")
            cross = cross_job_analysis(
                client, f"PR #{pr_number}", real_analyses,
            )

        # --- Build comment body ---
        body = (
            f"{requester_line}## CI Status for PR #{pr_number}\n\n"
            f"**{wf_passed + wf_failed + wf_pending} workflow(s)**: "
            f"{wf_passed} passed, {wf_failed} failed"
        )
        if wf_pending:
            body += f", {wf_pending} pending"
        body += "\n\n### Failed Checks\n\n"

        # Group analyses by workflow
        wf_to_analyses: dict[str, list[dict]] = defaultdict(list)
        for ja in all_job_analyses:
            wf_to_analyses[ja["workflow_name"]].append(ja)

        for wf_name, analyses in sorted(wf_to_analyses.items()):
            body += f"#### `{wf_name}`\n\n"
            body += _format_error_table(analyses)
            body += "\n"

        if correlation:
            body += f"### PR Correlation Analysis\n\n{correlation}\n\n"

        if cross:
            body += f"### Cross-Job Summary\n\n{cross}\n\n---\n\n"

        body += "### Per-Job Detailed Analysis\n"
        for ja in all_job_analyses:
            body += _format_details(ja)

        remaining = len(job_queue) - MAX_FAILED_JOBS
        if remaining > 0:
            body += (
                f"\n> **Note**: {remaining} additional failed job(s) were not "
                f"analyzed (limit: {MAX_FAILED_JOBS}).\n"
            )

        body += "\n---\n*Generated by amd-bot — CI status check with PR correlation*\n"

    if post_comment_flag:
        result = post_comment(token, REPO, pr_number, body)
        print(f"\n  Posted: {result['html_url']}")
        return result["html_url"]

    print(body)
    return body


def main():
    parser = argparse.ArgumentParser(
        description="Check CI status for a sglang PR",
    )
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument(
        "--no-post", action="store_true", help="Print only, don't post",
    )
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
