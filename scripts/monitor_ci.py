#!/usr/bin/env python3
"""
amd-bot CI Failure Monitor for sglang.

Monitors specified CI workflows, fetches full logs from failed jobs,
uses progressive step-by-step analysis with Claude to build a cumulative
understanding across all steps (not just failed ones), then posts/updates
daily summary comments on GitHub issues.

Each workflow gets ONE comment in the daily issue, updated via PATCH as
new failures are discovered.  In-progress runs are monitored so that
already-failed jobs can be analyzed immediately, without waiting for the
entire workflow to finish.

Deduplication across processes (daemon + cron) is achieved by embedding
processed job IDs in the comment body as an HTML comment:
  <!-- processed_job_ids: 111,222,333 -->
Both daemon and cron read this metadata before analyzing, ensuring no
job is analyzed twice regardless of which process handles it.

Supports two modes:
  1. One-shot (default): check once and exit (for GitHub Actions cron)
  2. Daemon (--daemon):  poll continuously (for self-hosted runner)
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from utils import (
    REPO,
    STEP_LOG_PREFILTER_THRESHOLD,
    create_anthropic_client,
    create_github_issue,
    cross_job_analysis,
    download_job_logs,
    extract_error_lines,
    focused_job_analysis,
    gh_headers,
    parse_log_by_steps,
    post_comment,
    prefilter_large_step_log,
    update_comment,
)

log = logging.getLogger("ci-monitor")

MONITORED_WORKFLOWS = [
    "nightly-test-amd.yml",
    "nightly-test-amd-rocm720.yml",
    "release-docker-amd-nightly.yml",
    "release-docker-amd-rocm720-nightly.yml",
    "amd-aiter-scout.yml",
    "pr-test-amd-rocm720.yml",
]

SUCCESS_CONCLUSIONS = {"success"}
SKIP_JOB_CONCLUSIONS = {"success", "skipped"}

STATE_FILE = Path(__file__).parent.parent / ".state" / "ci_monitor.json"
MAX_PARALLEL_JOBS = 3

IDLE_POLL_INTERVAL = 300   # 5 min — no active runs
ACTIVE_POLL_INTERVAL = 60  # 60s  — tracking in-progress runs

_PROCESSED_IDS_RE = re.compile(r"<!-- processed_job_ids: ([\d,]+) -->")


# ---------------------------------------------------------------------------
# State management (local cache only — GitHub comment is source of truth)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"daily_comments": {}}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    dc = state.get("daily_comments", {})
    if len(dc) > 3:
        for d in sorted(dc.keys())[:-3]:
            del dc[d]
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_daily_state(state: dict, date_str: str) -> dict:
    dc = state.setdefault("daily_comments", {})
    if date_str not in dc:
        dc[date_str] = {"issue_number": None, "workflows": {}}
    return dc[date_str]


def get_workflow_state(daily: dict, workflow_file: str) -> dict:
    wfs = daily.setdefault("workflows", {})
    if workflow_file not in wfs:
        wfs[workflow_file] = {
            "comment_id": None,
            "owned": False,
            "job_analyses": [],
            "last_pending_count": 0,
        }
    return wfs[workflow_file]


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def get_workflow_runs(
    token: str,
    workflow_file: str,
    hours_back: int = 24,
    max_runs: int = 5,
    branch: str = "main",
) -> list[dict]:
    """Fetch recent non-success completed runs AND in-progress runs.

    In-progress runs are included so that already-failed jobs within them
    can be analyzed immediately, without waiting for the entire workflow
    to finish.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow_file}/runs"
    base_params = {
        "branch": branch,
        "per_page": min(max_runs * 5, 100),
        "created": f">={since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    }

    all_runs: list[dict] = []
    seen_ids: set[int] = set()
    for status in ("completed", "in_progress", "queued", "waiting", "requested"):
        params = {**base_params, "status": status}
        resp = requests.get(url, headers=gh_headers(token), params=params)
        resp.raise_for_status()
        for r in resp.json().get("workflow_runs", []):
            if r["id"] in seen_ids:
                continue
            if status != "completed" or r.get("conclusion") not in SUCCESS_CONCLUSIONS:
                seen_ids.add(r["id"])
                all_runs.append(r)

    return all_runs[:max_runs]


def get_failed_jobs(token: str, run_id: int) -> list[dict]:
    """Get completed non-success, non-skipped jobs for a workflow run.

    Only returns jobs whose status is 'completed' so that still-running
    jobs in an in-progress workflow are not picked up prematurely.
    """
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
    params = {"filter": "latest", "per_page": 100}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        j for j in jobs
        if j.get("status") == "completed"
        and j.get("conclusion") not in SKIP_JOB_CONCLUSIONS
    ]


def get_pending_job_info(token: str, run_id: int) -> dict:
    """Count still-running jobs in a workflow run."""
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
    params = {"filter": "latest", "per_page": 100}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    running = [j for j in jobs if j.get("status") != "completed"]
    return {"count": len(running), "run_id": run_id}


# ---------------------------------------------------------------------------
# GitHub comment dedup helpers
# ---------------------------------------------------------------------------

def get_issue_comments(token: str, bot_repo: str, issue_number: int) -> list[dict]:
    """Fetch all comments on an issue."""
    url = f"https://api.github.com/repos/{bot_repo}/issues/{issue_number}/comments"
    resp = requests.get(url, headers=gh_headers(token), params={"per_page": 100})
    resp.raise_for_status()
    return resp.json()


def extract_processed_ids_from_comments(
    comments: list[dict], workflow_file: str,
) -> set[int]:
    """Scan ALL comments for a workflow and return the union of processed job IDs.

    Multiple comments may exist for the same workflow (e.g. one from cron,
    one from daemon).  We merge their metadata so no job is ever re-analyzed.
    """
    marker = f"## `{workflow_file}`"
    all_ids: set[int] = set()
    for comment in comments:
        body = comment.get("body", "")
        if marker in body:
            match = _PROCESSED_IDS_RE.search(body)
            if match:
                all_ids.update(int(x) for x in match.group(1).split(",") if x)
    return all_ids


# ---------------------------------------------------------------------------
# Daily issue management
# ---------------------------------------------------------------------------

def find_daily_issue(token: str, bot_repo: str, date_str: str) -> int | None:
    """Find the daily CI monitoring issue if it exists. Returns issue number or None."""
    url = f"https://api.github.com/repos/{bot_repo}/issues"
    params = {"state": "open", "labels": "ci-monitor", "per_page": 50}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()

    title = f"[CI Monitor] Daily Report - {date_str}"
    for issue in resp.json():
        if issue["title"] == title:
            return issue["number"]
    return None


def find_or_create_daily_issue(
    token: str, bot_repo: str, date_str: str
) -> tuple[int, bool]:
    """Find or create the daily CI monitoring issue. Returns (number, created)."""
    existing = find_daily_issue(token, bot_repo, date_str)
    if existing is not None:
        return existing, False

    title = f"[CI Monitor] Daily Report - {date_str}"
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
# Single-job analysis (thread-safe)
# ---------------------------------------------------------------------------

def _analyze_job(client, token: str, job: dict, run_url: str) -> dict:
    """Download logs, extract errors, and run focused analysis for one job."""
    job_name = job["name"]
    job_id = job["id"]
    run_id = int(run_url.rstrip("/").split("/")[-1])

    failed_step_names = {
        s["name"]
        for s in job.get("steps", [])
        if s.get("conclusion") not in ("success", "skipped", None)
    }

    log.info("  [%s] Downloading logs...", job_name)
    raw_log = download_job_logs(token, job_id)
    log.info("  [%s] Log: %s chars", job_name, f"{len(raw_log):,}")

    all_errors = extract_error_lines(
        raw_log, job.get("steps", []), run_id, job_id,
    )
    error_lines = [e for e in all_errors if e["source"] != "tail"]
    log.info("  [%s] Extracted %d error signal(s)", job_name, len(error_lines))

    filtered_log = prefilter_large_step_log(raw_log)
    if len(filtered_log) < len(raw_log):
        log.info(
            "  [%s] Pre-filtered log: %s -> %s chars",
            job_name, f"{len(raw_log):,}", f"{len(filtered_log):,}",
        )

    log.info("  [%s] Analyzing...", job_name)
    analysis = focused_job_analysis(
        client, job_name, run_url, error_lines, filtered_log,
    )

    log.info("  [%s] Done.", job_name)
    return {
        "run_url": run_url,
        "job_name": job_name,
        "job_id": job_id,
        "started_at": job.get("started_at"),
        "failed_steps": sorted(failed_step_names),
        "analysis": analysis,
    }


# ---------------------------------------------------------------------------
# Comment rendering
# ---------------------------------------------------------------------------

def render_workflow_comment(
    workflow_file: str,
    job_analyses: list[dict],
    pending_info: list[dict] | None = None,
    cross_summary: str = "",
) -> str:
    """Render the full markdown comment body for a workflow.

    Embeds ``<!-- processed_job_ids: ... -->`` as the first line so that
    any process (daemon or cron) can extract already-processed IDs without
    parsing the human-readable markdown.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    job_ids_csv = ",".join(str(ja["job_id"]) for ja in job_analyses)
    metadata = f"<!-- processed_job_ids: {job_ids_csv} -->"

    job_table_rows = "\n".join(
        f"| [`{ja['job_name']}`]({ja['run_url']}) "
        f"| {', '.join(ja['failed_steps']) or 'N/A'} "
        f"| {ja.get('started_at', 'N/A')[:16] if ja.get('started_at') else 'N/A'} |"
        for ja in job_analyses
    )

    per_job = ""
    for ja in job_analyses:
        per_job += f"""
<details>
<summary><b>{ja['job_name']}</b> — failed step(s): {', '.join(ja['failed_steps']) or 'N/A'}</summary>

{ja['analysis']}

</details>
"""

    body = f"""{metadata}
## `{workflow_file}` — {len(job_analyses)} failure(s)

**Scanned**: {now}

| Job | Failed Steps | Started |
|-----|-------------|---------|
{job_table_rows}

"""

    if cross_summary:
        body += f"### Cross-Job Summary\n\n{cross_summary}\n\n---\n\n"

    body += f"### Per-Job Analysis\n{per_job}\n"

    total_pending = sum(p["count"] for p in pending_info) if pending_info else 0
    if total_pending > 0:
        run_links = ", ".join(
            f"[run](https://github.com/{REPO}/actions/runs/{p['run_id']})"
            for p in pending_info if p["count"] > 0
        )
        body += (
            f"\n---\n"
            f"\u23f3 **{total_pending} job(s) still running** "
            f"({run_links}) — will update when complete\n"
        )

    body += f"\n---\n*Generated by amd-bot (last updated: {now})*\n"
    return body


# ---------------------------------------------------------------------------
# Core monitoring logic
# ---------------------------------------------------------------------------

def monitor_workflow(
    token: str,
    workflow_file: str,
    hours_back: int = 24,
    processed_job_ids: set[int] | None = None,
    job_name_filter: str | None = None,
    branch: str = "main",
) -> tuple[list[dict], list[int], list[dict]]:
    """Monitor a single workflow.

    Returns (new_job_analyses, new_job_ids, pending_info).
    """
    log.info("Monitoring: %s (branch: %s)", workflow_file, branch)

    runs = get_workflow_runs(token, workflow_file, hours_back=hours_back, branch=branch)
    if not runs:
        log.info("  No actionable runs in the last %d hours.", hours_back)
        return [], [], []

    completed_runs = [r for r in runs if r.get("status") == "completed"]
    in_progress_runs = [r for r in runs if r.get("status") != "completed"]
    log.info(
        "  %d completed non-success + %d in-progress run(s)",
        len(completed_runs), len(in_progress_runs),
    )

    jobs_to_analyze: list[tuple[dict, str]] = []
    pending_info: list[dict] = []

    for run in runs:
        run_id = run["id"]
        run_url = run["html_url"]
        run_status = run.get("status", "unknown")
        run_conclusion = run.get("conclusion") or "in_progress"
        log.info("  Run %d [%s/%s]: %s", run_id, run_status, run_conclusion, run_url)

        failed_jobs = get_failed_jobs(token, run_id)
        if job_name_filter:
            failed_jobs = [j for j in failed_jobs if job_name_filter in j["name"]]
        if processed_job_ids:
            failed_jobs = [j for j in failed_jobs if j["id"] not in processed_job_ids]

        if failed_jobs:
            log.info("    %d new failed job(s) to analyze", len(failed_jobs))
            for job in failed_jobs:
                jobs_to_analyze.append((job, run_url))

        if run_status != "completed":
            pi = get_pending_job_info(token, run_id)
            if pi["count"] > 0:
                pending_info.append(pi)

    new_job_analyses: list[dict] = []
    new_job_ids: list[int] = []

    if jobs_to_analyze:
        client = create_anthropic_client()
        workers = min(MAX_PARALLEL_JOBS, len(jobs_to_analyze))
        log.info("  Analyzing %d job(s) (workers: %d)...", len(jobs_to_analyze), workers)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_analyze_job, client, token, job, run_url): job
                for job, run_url in jobs_to_analyze
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                    new_job_analyses.append(result)
                    new_job_ids.append(result["job_id"])
                except Exception as e:
                    log.error("  Error analyzing %s: %s", job["name"], e)
                    traceback.print_exc()
                    new_job_ids.append(job["id"])
    else:
        log.info("  No new failed jobs to analyze.")

    return new_job_analyses, new_job_ids, pending_info


# ---------------------------------------------------------------------------
# Publishing (daily-issue mode)
# ---------------------------------------------------------------------------

def publish_workflow_report(
    token: str,
    bot_repo: str,
    workflow_file: str,
    new_analyses: list[dict],
    pending_info: list[dict],
    state: dict,
):
    """Publish or update the workflow comment in the daily issue."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_daily_state(state, date_str)

    if not daily.get("issue_number"):
        issue_num, created = find_or_create_daily_issue(token, bot_repo, date_str)
        daily["issue_number"] = issue_num
        log.info("%s daily issue #%d", "Created" if created else "Found", issue_num)

    issue_num = daily["issue_number"]
    wf_state = get_workflow_state(daily, workflow_file)

    existing = wf_state.get("job_analyses", [])
    existing_ids = {ja["job_id"] for ja in existing}
    for ja in new_analyses:
        if ja["job_id"] not in existing_ids:
            existing.append(ja)
    wf_state["job_analyses"] = existing

    all_analyses = wf_state["job_analyses"]

    total_pending = sum(p["count"] for p in pending_info) if pending_info else 0
    cross = ""
    if len(all_analyses) > 1:
        client = create_anthropic_client()
        log.info("  Cross-job analysis (%d jobs)...", len(all_analyses))
        cross = cross_job_analysis(client, workflow_file, all_analyses)

    body = render_workflow_comment(workflow_file, all_analyses, pending_info, cross)
    wf_state["last_pending_count"] = total_pending

    comment_id = wf_state.get("comment_id")
    if comment_id and wf_state.get("owned"):
        update_comment(token, bot_repo, comment_id, body)
        log.info("  Updated comment %d for %s", comment_id, workflow_file)
    else:
        resp = post_comment(token, bot_repo, issue_num, body)
        wf_state["comment_id"] = resp["id"]
        wf_state["owned"] = True
        log.info("  Posted comment %d for %s", resp["id"], workflow_file)


# ---------------------------------------------------------------------------
# Building processed_job_ids from local state + GitHub comments
# ---------------------------------------------------------------------------

def build_processed_ids(
    wf_state: dict,
    gh_comments: list[dict],
    workflow_file: str,
) -> set[int]:
    """Merge processed job IDs from local cache and GitHub comments."""
    local_ids = {ja["job_id"] for ja in wf_state.get("job_analyses", [])}
    gh_ids = extract_processed_ids_from_comments(gh_comments, workflow_file)
    return local_ids | gh_ids


# ---------------------------------------------------------------------------
# Daemon mode
# ---------------------------------------------------------------------------

_shutdown = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("Received %s, shutting down...", signal.Signals(signum).name)
    _shutdown = True


def _interruptible_sleep(seconds: int):
    end = time.monotonic() + seconds
    while not _shutdown and time.monotonic() < end:
        time.sleep(min(1, end - time.monotonic()))


def run_daemon(
    token: str,
    bot_repo: str,
    workflows: list[str],
    hours_back: int,
    branch: str,
    job_name_filter: str | None = None,
):
    """Run the CI monitor as a long-lived daemon."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info(
        "Daemon started — monitoring %s, dispatching to %s "
        "(idle: %ds, active: %ds)",
        REPO, bot_repo, IDLE_POLL_INTERVAL, ACTIVE_POLL_INTERVAL,
    )

    consecutive_errors = 0
    max_backoff = 600

    while not _shutdown:
        has_in_progress = False
        state = load_state()
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = get_daily_state(state, date_str)

        if not daily.get("issue_number"):
            try:
                daily["issue_number"] = find_daily_issue(token, bot_repo, date_str)
            except Exception:
                pass

        gh_comments: list[dict] = []
        if daily.get("issue_number"):
            try:
                gh_comments = get_issue_comments(token, bot_repo, daily["issue_number"])
            except Exception:
                log.warning("Could not fetch issue comments, using local state only")

        for wf in workflows:
            if _shutdown:
                break
            try:
                wf_state = get_workflow_state(daily, wf)
                processed_job_ids = build_processed_ids(wf_state, gh_comments, wf)

                new_analyses, new_ids, pending = monitor_workflow(
                    token, wf,
                    hours_back=hours_back,
                    processed_job_ids=processed_job_ids,
                    job_name_filter=job_name_filter,
                    branch=branch,
                )

                if pending:
                    has_in_progress = True

                total_pending = sum(p["count"] for p in pending) if pending else 0
                last_pending = wf_state.get("last_pending_count", 0)

                needs_update = bool(new_analyses)
                if not needs_update and wf_state.get("comment_id") and wf_state.get("owned"):
                    if wf_state.get("job_analyses") and total_pending != last_pending:
                        needs_update = True

                if needs_update:
                    publish_workflow_report(
                        token, bot_repo, wf, new_analyses, pending, state,
                    )

                save_state(state)
                consecutive_errors = 0

            except requests.exceptions.RequestException as exc:
                consecutive_errors += 1
                backoff = min(60 * (2 ** consecutive_errors), max_backoff)
                log.warning(
                    "API error on %s (%d in a row): %s — retry in %ds",
                    wf, consecutive_errors, exc, backoff,
                )
                _interruptible_sleep(backoff)
            except Exception:
                consecutive_errors += 1
                log.exception("Error monitoring %s (%d in a row)", wf, consecutive_errors)
                _interruptible_sleep(min(60 * consecutive_errors, max_backoff))

        interval = ACTIVE_POLL_INTERVAL if has_in_progress else IDLE_POLL_INTERVAL
        log.debug("Sleeping %ds (active=%s)...", interval, has_in_progress)
        _interruptible_sleep(interval)

    log.info("Daemon stopped.")


# ---------------------------------------------------------------------------
# One-shot mode (for cron / manual use)
# ---------------------------------------------------------------------------

def run_oneshot(
    token: str,
    bot_repo: str | None,
    output: str,
    workflows: list[str],
    hours_back: int,
    branch: str,
    job_name_filter: str | None = None,
):
    """Run the CI monitor once and exit."""
    state = load_state()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_daily_state(state, date_str)
    total_reports = 0

    if bot_repo and not daily.get("issue_number"):
        try:
            daily["issue_number"] = find_daily_issue(token, bot_repo, date_str)
        except Exception:
            pass

    gh_comments: list[dict] = []
    if bot_repo and daily.get("issue_number"):
        try:
            gh_comments = get_issue_comments(token, bot_repo, daily["issue_number"])
        except Exception:
            log.warning("Could not fetch issue comments, using local state only")

    for wf in workflows:
        try:
            wf_state = get_workflow_state(daily, wf)
            local_ids = {ja["job_id"] for ja in wf_state.get("job_analyses", [])}
            gh_ids = extract_processed_ids_from_comments(gh_comments, wf) if gh_comments else set()
            processed_job_ids = local_ids | gh_ids

            new_analyses, new_ids, pending = monitor_workflow(
                token, wf,
                hours_back=hours_back,
                processed_job_ids=processed_job_ids,
                job_name_filter=job_name_filter,
                branch=branch,
            )

            if not new_analyses:
                continue

            total_reports += 1

            if output == "stdout":
                cross = ""
                if len(new_analyses) > 1:
                    client = create_anthropic_client()
                    cross = cross_job_analysis(client, wf, new_analyses)
                body = render_workflow_comment(wf, new_analyses, pending, cross)
                print(f"\n{'='*60}")
                print(body)

            elif output == "daily-issue" and bot_repo:
                publish_workflow_report(
                    token, bot_repo, wf, new_analyses, pending, state,
                )

            save_state(state)

        except Exception as e:
            log.error("Error monitoring %s: %s", wf, e)
            traceback.print_exc()

    save_state(state)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"has_failures={'true' if total_reports else 'false'}\n")
            if total_reports:
                f.write(f"failure_count={total_reports}\n")

    log.info("Done. %d workflow(s) had failures.", total_reports)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Monitor sglang CI failures")
    parser.add_argument(
        "--workflows", nargs="*", default=MONITORED_WORKFLOWS,
        help="Workflow files to monitor",
    )
    parser.add_argument(
        "--hours-back", type=int, default=24,
        help="How many hours back to check (default: 24)",
    )
    parser.add_argument(
        "--output", choices=["stdout", "daily-issue"], default="stdout",
        help="Output mode for one-shot (default: stdout)",
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
        "--branch", default="main",
        help="Only analyze runs triggered on this branch (default: main)",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run as a long-lived daemon instead of one-shot",
    )
    parser.add_argument(
        "--poll-interval", type=int,
        help="Override active poll interval in daemon mode (seconds)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("BOT_PAT", os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", ""))),
        help="GitHub token",
    )

    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stdout,
    )

    if not args.github_token:
        log.error("GitHub token required. Set GH_PAT.")
        sys.exit(1)
    if not os.environ.get("LLM_GATEWAY_KEY"):
        log.error("LLM_GATEWAY_KEY env var required.")
        sys.exit(1)
    if not os.environ.get("LLM_GATEWAY_URL"):
        log.error("LLM_GATEWAY_URL env var required.")
        sys.exit(1)

    if args.daemon:
        if not args.bot_repo:
            log.error("--bot-repo is required for daemon mode.")
            sys.exit(1)
        if args.poll_interval:
            global ACTIVE_POLL_INTERVAL
            ACTIVE_POLL_INTERVAL = args.poll_interval
        run_daemon(
            args.github_token, args.bot_repo, args.workflows,
            args.hours_back, args.branch, args.job_name,
        )
    else:
        run_oneshot(
            args.github_token, args.bot_repo, args.output,
            args.workflows, args.hours_back, args.branch, args.job_name,
        )


if __name__ == "__main__":
    main()
