#!/usr/bin/env python3
"""
amd-bot CI status checker for a specific PR.

Extracts error messages structurally, uses a single LLM call to assess
PR correlation, and outputs ONE merged table for developers to scan
in 5 seconds.
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from utils import (
    CLAUDE_MODEL,
    GATE_STEP_PATTERNS,
    REPO,
    create_anthropic_client,
    download_job_logs,
    extract_error_lines,
    get_pr_changed_files,
    get_pr_diff,
    get_run_jobs,
    get_workflow_runs_for_sha,
    gh_headers,
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
    """Collect all workflow runs for a SHA, grouped by workflow."""
    wf_runs = get_workflow_runs_for_sha(token, head_sha)

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
    """Return True if the job is a coordinator/gate job."""
    failed_steps = [
        s for s in job.get("steps", [])
        if s.get("conclusion") == "failure"
    ]
    if not failed_steps:
        return False
    return all(GATE_STEP_PATTERNS.search(s["name"]) for s in failed_steps)


# ---------------------------------------------------------------------------
# Per-job error collection (no LLM — just log download + structural extraction)
# ---------------------------------------------------------------------------

def collect_job_errors(
    job: dict, run_id: int, run_url: str, token: str,
) -> dict | None:
    """Download log and extract errors structurally. No LLM call."""
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
        print(f"\n  Job: {job_name} (ID: {job_id}) — gate job, skipped")
        return {
            "job_name": job_name,
            "job_id": job_id,
            "run_id": run_id,
            "run_url": run_url,
            "job_url": job_url,
            "failed_steps": sorted(failed_step_names),
            "error_lines": [],
            "is_gate": True,
        }

    print(f"\n  Job: {job_name} (ID: {job_id})")
    print("    Downloading job log...")
    raw_log = download_job_logs(token, job_id)
    print(f"    Log size: {len(raw_log):,} chars")

    print("    Extracting errors...")
    error_lines = extract_error_lines(raw_log, api_steps, run_id, job_id)
    print(f"    Found {len(error_lines)} error line(s)")

    return {
        "job_name": job_name,
        "job_id": job_id,
        "run_id": run_id,
        "run_url": run_url,
        "job_url": job_url,
        "failed_steps": sorted(failed_step_names),
        "error_lines": error_lines,
        "is_gate": False,
    }


# ---------------------------------------------------------------------------
# Pick the best error message for the summary table
# ---------------------------------------------------------------------------

def _pick_best_error(ja: dict) -> dict | None:
    """Pick the most relevant error line like a human expert.

    Priority: ##[error] annotations > Python exceptions > tail lines.
    Among exceptions, the LONGEST preview wins — root cause errors have
    detailed messages while cascading/cleanup errors are terse.
    """
    if not ja["error_lines"]:
        return None

    annotations = [e for e in ja["error_lines"] if e.get("source") == "annotation"]
    if annotations:
        return max(annotations, key=lambda e: len(e["preview"]))

    exceptions = [e for e in ja["error_lines"] if e.get("source") == "exception"]
    if exceptions:
        return max(exceptions, key=lambda e: len(e["preview"]))

    return ja["error_lines"][-1]


# ---------------------------------------------------------------------------
# PR correlation analysis (single LLM call, returns structured data)
# ---------------------------------------------------------------------------

def analyze_pr_correlation(
    client,
    pr_number: int,
    changed_files: list[dict],
    pr_diff: str,
    job_analyses: list[dict],
) -> list[dict]:
    """Single LLM call: assess whether each failure correlates with the PR.

    Returns a list of dicts: [{job, verdict, emoji, explanation}, ...].
    Falls back to an empty list on parse failure.
    """
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

    errors_text = ""
    for ja in job_analyses:
        errors_text += f"\n#### Job: `{ja['job_name']}`\n"
        errors_text += f"Failed step(s): {', '.join(ja['failed_steps'])}\n"
        if ja["error_lines"]:
            for el in ja["error_lines"][:5]:
                errors_text += f"- `{el['preview']}`\n"

    job_names = [ja["job_name"] for ja in job_analyses]
    job_list = "\n".join(f'  - "{name}"' for name in job_names)

    prompt = f"""You are a CI/CD expert. A developer submitted PR #{pr_number} to the sglang project (LLM serving framework). Some CI jobs failed. Assess whether each failure is likely caused by the PR changes or is a pre-existing / infrastructure issue.

## PR Changed Files
{files_summary}

## PR Diff (may be truncated)
```
{diff_text}
```

## CI Failures
{errors_text}

## Instructions

For EACH of these exact job names:
{job_list}

Return a JSON array with your assessment. Output ONLY the raw JSON, no markdown fences, no extra text:

[
  {{"job": "exact job name from list above", "verdict": "likely", "explanation": "one sentence"}},
  {{"job": "exact job name from list above", "verdict": "unlikely", "explanation": "one sentence"}}
]

Rules for the "verdict" field — use EXACTLY one of these strings:
- "likely" = the error clearly involves code paths touched by the PR
- "possibly" = the error could be influenced by the PR but also has other explanations
- "unlikely" = the error is in unrelated code, infrastructure, or a known flaky test"""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"  WARNING: Failed to parse correlation JSON, falling back")
        return []


# ---------------------------------------------------------------------------
# Comment formatting — ONE merged table
# ---------------------------------------------------------------------------

_VERDICT_DISPLAY = {
    "likely": ":red_circle: **Likely**",
    "possibly": ":yellow_circle: **Possibly**",
    "unlikely": ":green_circle: **Unlikely**",
}


def _format_merged_table(
    analyses: list[dict],
    correlation: list[dict],
) -> str:
    """Build ONE table merging error messages + PR correlation verdicts."""
    corr_by_job: dict[str, dict] = {}
    for c in correlation:
        corr_by_job[c.get("job", "")] = c

    rows = "| Job | Error | Related? | Log |\n"
    rows += "|-----|-------|----------|-----|\n"

    for ja in analyses:
        if ja.get("is_gate"):
            continue

        best = _pick_best_error(ja)
        if best:
            preview = best["preview"]
            if len(preview) > 200:
                preview = preview[:200] + "..."
            preview = preview.replace("|", "\\|")
            log_link = f"[View]({best['url']})"
        else:
            preview = "*(no error extracted)*"
            log_link = f"[View]({ja['job_url']})"

        corr = corr_by_job.get(ja["job_name"], {})
        verdict = corr.get("verdict", "")
        explanation = corr.get("explanation", "")
        display = _VERDICT_DISPLAY.get(verdict, "")

        if display and explanation:
            related_cell = f"{display} -- {explanation}"
        elif display:
            related_cell = display
        else:
            related_cell = "*(pending)*"

        related_cell = related_cell.replace("|", "\\|")

        rows += f"| `{ja['job_name']}` | `{preview}` | {related_cell} | {log_link} |\n"

    return rows


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def check_ci_for_pr(
    token: str,
    pr_number: int,
    post_comment_flag: bool = True,
) -> str:
    """Check CI status for a PR: one table with errors + PR correlation."""
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
        all_job_data: list[dict] = []

        job_queue: list[tuple[dict, dict]] = []
        for wf in failed_workflows:
            for job in wf["failed_jobs"]:
                job_queue.append((wf, job))

        jobs_to_analyze = job_queue[:MAX_FAILED_JOBS]
        n_jobs = len(jobs_to_analyze)
        max_workers = min(n_jobs + 2, 6)
        print(f"\n  Collecting errors from {n_jobs} job(s) (workers={max_workers})...")

        # Phase 1: download logs + extract errors (parallel, no LLM)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            job_futures = {}
            for wf, job in jobs_to_analyze:
                fut = executor.submit(
                    collect_job_errors, job,
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
                    print(f"  ERROR collecting job in {wf['name']}: {exc}")
                    continue
                if ja:
                    ja["workflow_name"] = wf["name"]
                    all_job_data.append(ja)

            pr_diff = diff_future.result()
            changed_files = files_future.result()

        print(
            f"\n  PR diff: {len(pr_diff):,} chars, "
            f"{len(changed_files)} file(s) changed"
        )

        real_jobs = [ja for ja in all_job_data if not ja.get("is_gate")]

        # Phase 2: single LLM call for PR correlation
        correlation: list[dict] = []
        if real_jobs and pr_diff:
            print("\n  Running PR correlation analysis (1 LLM call)...")
            client = create_anthropic_client()
            correlation = analyze_pr_correlation(
                client, pr_number, changed_files, pr_diff, real_jobs,
            )
            print(f"  Got {len(correlation)} correlation verdict(s)")

        # Phase 3: build ONE merged table
        body = (
            f"{requester_line}## CI Status for PR #{pr_number}\n\n"
            f"**{wf_passed + wf_failed + wf_pending} workflow(s)**: "
            f"{wf_passed} passed, {wf_failed} failed"
        )
        if wf_pending:
            body += f", {wf_pending} pending"
        body += "\n\n"

        body += _format_merged_table(all_job_data, correlation)

        remaining = len(job_queue) - MAX_FAILED_JOBS
        if remaining > 0:
            body += (
                f"\n> {remaining} additional failed job(s) not shown "
                f"(limit: {MAX_FAILED_JOBS}).\n"
            )

        body += "\n---\n*Generated by amd-bot*\n"

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
