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
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import httpx
import requests

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"

MONITORED_WORKFLOWS = [
    "nightly-test-amd.yml",
    "nightly-test-amd-rocm720.yml",
    "release-docker-amd-nightly.yml",
    "release-docker-amd-rocm720-nightly.yml",
    "amd-aiter-scout.yml",
]

STATE_FILE = Path(__file__).parent.parent / ".state" / "ci_monitor.json"
MAX_STATE_IDS = 500

CLAUDE_MODEL = "claude-opus-4-6"
STEP_LOG_PREFILTER_THRESHOLD = 150_000

ERROR_PATTERNS = re.compile(
    r"|".join([
        r"ERROR",
        r"FAIL(?:ED)?",
        r"Exception",
        r"Traceback",
        r"assert(?:ion)?.*(?:error|fail)",
        r"exit\s+code\s+[1-9]",
        r"TIMEOUT",
        r"OOM|Out\s*[Oo]f\s*[Mm]emory",
        r"killed|KILLED",
        r"[Ss]egmentation\s+fault|segfault|SEGFAULT",
        r"FATAL",
        r"panic",
        r"cannot\s+find",
        r"No\s+such\s+file",
        r"Permission\s+denied",
        r"ModuleNotFoundError",
        r"ImportError",
        r"RuntimeError",
        r"ConnectionError",
        r"FileNotFoundError",
    ]),
    re.IGNORECASE,
)


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
# Anthropic client
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

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
    """Download full logs for a job — no truncation."""
    url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    resp = requests.get(url, headers=gh_headers(token), allow_redirects=True)
    if resp.status_code == 200:
        return resp.text
    return f"[Failed to fetch logs: HTTP {resp.status_code}]"


def create_github_issue(
    token: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> dict:
    """Create a GitHub issue."""
    target_repo = repo or REPO
    url = f"https://api.github.com/repos/{target_repo}/issues"
    data = {"title": title, "body": body}
    if labels:
        data["labels"] = labels
    resp = requests.post(url, headers=gh_headers(token), json=data)
    resp.raise_for_status()
    return resp.json()


def post_comment_on_issue(
    token: str, issue_number: int, body: str, repo: str | None = None
) -> dict:
    """Post a comment on an existing issue or PR."""
    target_repo = repo or REPO
    url = f"https://api.github.com/repos/{target_repo}/issues/{issue_number}/comments"
    resp = requests.post(url, headers=gh_headers(token), json={"body": body})
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Log parsing and pre-filtering
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*")


def parse_log_by_steps(raw_log: str) -> list[dict]:
    """Parse a GitHub Actions job log into per-step segments.

    GitHub Actions logs delimit steps with ``##[group]Step Name`` /
    ``##[endgroup]`` markers, each line prefixed by a UTC timestamp.

    Returns an ordered list of ``{"name": str, "content": str}`` dicts.
    Falls back to a single-entry list wrapping the whole log when no
    markers are detected.
    """
    lines = raw_log.split("\n")
    steps: list[dict] = []
    current_name: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = _TIMESTAMP_RE.sub("", line)

        group_match = re.match(r"##\[group\](.*)", stripped)
        if group_match:
            if current_name is not None or current_lines:
                steps.append({
                    "name": current_name or "(preamble)",
                    "content": "\n".join(current_lines),
                })
            current_name = group_match.group(1).strip()
            current_lines = []
            continue

        if stripped.strip() == "##[endgroup]":
            if current_name is not None:
                steps.append({
                    "name": current_name,
                    "content": "\n".join(current_lines),
                })
                current_name = None
                current_lines = []
            continue

        current_lines.append(line)

    if current_lines:
        steps.append({
            "name": current_name or "(trailing)",
            "content": "\n".join(current_lines),
        })

    if not steps:
        steps = [{"name": "(full log)", "content": raw_log}]

    return steps


def prefilter_large_step_log(
    log_text: str, max_chars: int = STEP_LOG_PREFILTER_THRESHOLD
) -> str:
    """Extract error-relevant sections from a very large step log.

    Keeps the first 100 lines (environment context), last 200 lines
    (exit status), and 30+10 lines of context around every line matching
    ``ERROR_PATTERNS``.  Overlapping ranges are merged.
    """
    if len(log_text) <= max_chars:
        return log_text

    lines = log_text.split("\n")
    total = len(lines)
    keep: set[int] = set()

    HEAD, TAIL = 100, 200
    for i in range(min(HEAD, total)):
        keep.add(i)
    for i in range(max(0, total - TAIL), total):
        keep.add(i)

    CTX_BEFORE, CTX_AFTER = 30, 10
    for i, line in enumerate(lines):
        if ERROR_PATTERNS.search(line):
            for j in range(max(0, i - CTX_BEFORE), min(total, i + CTX_AFTER + 1)):
                keep.add(j)

    sorted_idx = sorted(keep)
    parts: list[str] = []
    prev = -1
    for idx in sorted_idx:
        if prev >= 0 and idx > prev + 1:
            parts.append(f"\n... [{idx - prev - 1} lines omitted] ...\n")
        parts.append(lines[idx])
        prev = idx

    filtered = "\n".join(parts)

    if len(filtered) > max_chars:
        head_size = max_chars // 4
        tail_size = max_chars - head_size - 100
        filtered = (
            filtered[:head_size]
            + "\n\n... [FINAL TRUNCATION — log extremely large] ...\n\n"
            + filtered[-tail_size:]
        )

    return filtered


# ---------------------------------------------------------------------------
# Progressive step-by-step analysis with Claude
# ---------------------------------------------------------------------------

def progressive_step_analysis(
    client: anthropic.Anthropic,
    job_name: str,
    steps_with_logs: list[dict],
    failed_step_names: set[str],
) -> str:
    """Analyze every step of a job progressively, accumulating shared context.

    Each step is summarized with the accumulated summary of all prior steps
    as context.  This ensures information flows across steps (e.g. dependency
    versions from an install step inform the analysis of a later test step)
    while keeping each API call within context-window limits.
    """
    accumulated = ""
    n = len(steps_with_logs)

    for i, step in enumerate(steps_with_logs):
        name = step["name"]
        log = step["content"]
        is_failed = name in failed_step_names
        label = "FAILED" if is_failed else "PASSED"

        if len(log) > STEP_LOG_PREFILTER_THRESHOLD:
            orig = len(log)
            log = prefilter_large_step_log(log)
            print(
                f"      Pre-filtered '{name}': {orig:,} -> {len(log):,} chars"
            )

        print(f"    [{i+1}/{n}] Summarizing: {name} ({label}, {len(log):,} chars)")

        prompt = f"""You are analyzing step {i+1} of {n} in CI job "{job_name}" for the sglang project (LLM serving framework on AMD GPUs).

## Context from previous steps
{accumulated if accumulated else "(first step — no prior context)"}

## Current Step: {name}
**Status**: {label}

```
{log}
```

Summarize this step's key information concisely. Focus on:
- **Environment / Config**: Docker image tags, GPU info, OS version, env vars
- **Dependencies**: Package versions (PyTorch, Triton, Aiter, ROCm, vLLM, etc.)
- **Build output**: Compilation results, warnings
- **Test results**: Pass/fail counts, specific failures with error messages
- **Errors**: Full error messages, stack traces, exit codes
- **Anything relevant for understanding subsequent steps or failures**

{"This step FAILED — provide detailed error analysis including full error messages and stack traces." if is_failed else "This step passed — extract key contextual information briefly."}
Keep the summary concise but do NOT omit version numbers or error details."""

        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = msg.content[0].text
        accumulated += f"\n### Step {i+1}: {name} [{label}]\n{summary}\n"

    return accumulated


def final_job_analysis(
    client: anthropic.Anthropic,
    job_name: str,
    run_url: str,
    accumulated_summary: str,
) -> str:
    """Produce a root-cause analysis for a job from its accumulated step summaries."""
    prompt = f"""You are a CI/CD expert. Below is a complete step-by-step summary of a FAILED CI job in the sglang project (LLM serving framework on AMD GPUs).

## Job: {job_name}
## Run: {run_url}

{accumulated_summary}

Based on ALL information gathered from every step, provide:

1. **Root Cause Analysis**: Most likely root cause, considering the full pipeline (image version, dependency versions, environment).
2. **Failure Details**: Specific error messages, stack traces, which tests failed.
3. **Suggested Fixes**: Concrete, actionable steps. Reference specific versions, configs, or code.
4. **Priority**: Critical / High / Medium / Low.
5. **Environment Context**: Key environment details relevant to the failure.

Format in clear Markdown suitable for a GitHub issue."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def cross_job_analysis(
    client: anthropic.Anthropic,
    workflow_name: str,
    job_analyses: list[dict],
) -> str:
    """Find common patterns across multiple failed jobs in the same workflow."""
    jobs_text = "\n\n---\n\n".join(
        f"## Job: {ja['job_name']}\n{ja['analysis']}" for ja in job_analyses
    )

    prompt = f"""You are a CI/CD expert. Multiple jobs failed in workflow `{workflow_name}` of the sglang project.

Failed jobs: {len(job_analyses)}

{jobs_text}

Provide:
1. **Common Patterns**: Are the failures related? Shared root cause?
2. **Cross-Job Dependencies**: Did one failure cause or relate to another?
3. **Unified Root Cause**: Is there a single underlying issue (broken dep update, infra problem)?
4. **Priority Ranking**: Which failures to fix first?
5. **Overall Recommendation**: One-paragraph executive summary.

Format in clear Markdown."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


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

    wf_rows = "\n".join(f"| `{w}` | pending | - |" for w in MONITORED_WORKFLOWS)
    body = f"""## CI Monitor Daily Report — {date_str}

**Repo**: [{REPO}](https://github.com/{REPO})
**Monitored Workflows**: {", ".join(f"`{w}`" for w in MONITORED_WORKFLOWS)}

### Summary
| Workflow | Status | Failures |
|----------|--------|----------|
{wf_rows}

---
*Each cron run appends detailed analysis as comments below.*
*Updated automatically by amd-bot*
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

    client = _create_anthropic_client()
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
    run_links = "\n".join(
        f"- [{ja['job_name']}]({ja['run_url']}) (Run #{ja['run_id']})"
        for ja in all_job_analyses
    )

    per_job = ""
    for ja in all_job_analyses:
        failed_str = ", ".join(ja["failed_steps"]) or "N/A"
        per_job += f"""
### Job: `{ja['job_name']}`
- **Run**: [{ja['run_id']}]({ja['run_url']})
- **Started**: {ja.get('started_at', 'N/A')}
- **Failed Steps**: {failed_str}

{ja['analysis']}

---
"""

    body = f"""## CI Failure Report: `{workflow_file}`

**Time**: {now}
**Jobs analyzed**: {len(all_job_analyses)}
**Method**: Progressive step-by-step analysis (all steps examined)

### Failed Jobs
{run_links}

---
{per_job}
"""
    if cross:
        body += f"\n## Cross-Job Analysis\n\n{cross}\n\n---\n"

    body += "\n*Generated by amd-bot — progressive CI analysis*\n"

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
            comment = post_comment_on_issue(
                args.github_token, issue_num, r["body"], repo=args.bot_repo
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
