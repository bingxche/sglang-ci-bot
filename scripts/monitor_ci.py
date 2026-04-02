#!/usr/bin/env python3
"""
amd-bot Cron CI Monitor for sglang.

Monitors specified CI workflows, fetches full logs from failed jobs,
analyzes them with Claude, and posts/updates daily summary comments
on GitHub issues.

Each workflow gets ONE comment in the daily issue, updated via PATCH as
new failures are discovered.  In-progress runs are monitored so that
already-failed jobs can be analyzed immediately, without waiting for the
entire workflow to finish.

Deduplication is achieved by embedding processed job IDs in the comment
body as an HTML comment:
  <!-- processed_job_ids: 111,222,333 -->
Each cron run reads this metadata before analyzing, ensuring no job is
analyzed twice.

Runs as a one-shot process triggered by GitHub Actions cron (every 30min).
"""

import argparse
import json
import logging
import os
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from utils import (
    GATE_STEP_PATTERNS,
    REPO,
    claude_code_analyze,
    claude_code_available,
    create_anthropic_client,
    create_github_issue,
    cross_job_analysis,
    download_job_logs,
    ensure_sglang_repo,
    extract_error_lines,
    focused_job_analysis,
    gh_headers,
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

_PROCESSED_IDS_RE = re.compile(r"<!-- processed_job_ids: ([\d,]+) -->")
_GATE_JOB_NAME_RE = re.compile(r"finish|wait-for-|check-all", re.IGNORECASE)


def _is_gate_job(job: dict) -> bool:
    """Return True if the job is a coordinator/gate job (e.g. pr-test-amd-finish)."""
    if _GATE_JOB_NAME_RE.search(job.get("name", "")):
        return True
    failed_steps = [
        s for s in job.get("steps", [])
        if s.get("conclusion") == "failure"
    ]
    if not failed_steps:
        return False
    return all(GATE_STEP_PATTERNS.search(s["name"]) for s in failed_steps)


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
    """Scan ALL comments for a workflow and return the union of processed job IDs."""
    marker = f"## `{workflow_file}`"
    all_ids: set[int] = set()
    for comment in comments:
        body = comment.get("body", "")
        if marker in body:
            match = _PROCESSED_IDS_RE.search(body)
            if match:
                all_ids.update(int(x) for x in match.group(1).split(",") if x)
    return all_ids


_JOB_TABLE_ROW_RE = re.compile(
    r"\| \[`(.+?)`\]\((.+?)\) \| (.+?) \| (.+?) \|"
)
_DETAILS_BLOCK_RE = re.compile(
    r"<details>\s*<summary><b>(.+?)</b> — failed step\(s\): (.+?)</summary>"
    r"\s*\n(.*?)\n</details>",
    re.DOTALL,
)


def find_workflow_comment(
    comments: list[dict], workflow_file: str,
) -> dict | None:
    """Find the most recent comment for a workflow. Returns comment dict or None."""
    marker = f"## `{workflow_file}`"
    for comment in reversed(comments):
        if marker in comment.get("body", ""):
            return comment
    return None


def parse_job_analyses_from_comment(body: str) -> list[dict]:
    """Reconstruct job_analyses list from an existing comment body.

    Parses the processed_job_ids metadata, job table rows, and <details>
    blocks to recover the structured data needed for merging with new analyses.
    """
    ids_match = _PROCESSED_IDS_RE.search(body)
    job_ids = (
        [int(x) for x in ids_match.group(1).split(",") if x]
        if ids_match else []
    )

    table_rows = _JOB_TABLE_ROW_RE.findall(body)
    details_blocks = {m.group(1): m.group(3).strip() for m in _DETAILS_BLOCK_RE.finditer(body)}

    analyses: list[dict] = []
    for i, (job_name, run_url, failed_steps_str, started_at) in enumerate(table_rows):
        job_id = job_ids[i] if i < len(job_ids) else 0
        failed_steps = (
            [s.strip() for s in failed_steps_str.split(",")]
            if failed_steps_str.strip() != "N/A" else []
        )
        started = started_at.strip() if started_at.strip() != "N/A" else None
        analysis_text = details_blocks.get(job_name, "")

        analyses.append({
            "run_url": run_url,
            "job_name": job_name,
            "job_id": job_id,
            "started_at": started,
            "failed_steps": failed_steps,
            "analysis": analysis_text,
        })

    return analyses


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


def _analyze_job_with_agent(
    job: dict, run_url: str, repo_path: Path, workflow_file: str = "",
) -> dict:
    """Invoke Claude Code agent to fully analyze a CI failure.

    The agent handles everything autonomously: downloading logs via the
    GitHub API (using ``$GH_PAT`` from the environment), parsing errors,
    reading sglang source code, checking git history, and producing a
    root-cause analysis.  Investigation methodology is defined in
    ``/workspace/CLAUDE.md`` which Claude Code reads automatically.
    """
    job_name = job["name"]
    job_id = job["id"]

    failed_step_names = {
        s["name"]
        for s in job.get("steps", [])
        if s.get("conclusion") not in ("success", "skipped", None)
    }

    prompt = f"""Analyze this CI failure in sgl-project/sglang. The source code is in the current directory. GitHub API token is in $GH_PAT.

Job: {job_name}
Run: {run_url}
Job ID: {job_id}
Workflow file: {workflow_file}
Log URL: https://api.github.com/repos/sgl-project/sglang/actions/jobs/{job_id}/logs"""

    log.info("  [%s] Running Claude Code agent...", job_name)
    analysis = claude_code_analyze(
        prompt=prompt,
        work_dir=repo_path,
    )

    log.info("  [%s] Agent analysis done.", job_name)
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
    use_agent: bool = False,
) -> str:
    """Render the full markdown comment body for a workflow.

    Layout: results-first — cross-job summary at the top, then job table,
    then per-job details in collapsible sections.

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

"""

    if cross_summary:
        cleaned = re.sub(r"^#{1,3}\s+.*(?:Summary|Overview).*$", "", cross_summary, flags=re.MULTILINE).strip()
        body += f"### Summary\n\n{cleaned}\n\n---\n\n"

    body += f"""| Job | Failed Steps | Started |
|-----|-------------|---------|
{job_table_rows}

"""

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

    method = "Claude Code CLI" if use_agent else "Claude API"
    body += f"\n---\n*Generated by amd-bot using {method} (last updated: {now})*\n"
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
    use_agent: bool = False,
    agent_repo_path: Path | None = None,
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

        gate_jobs = [j for j in failed_jobs if _is_gate_job(j)]
        for gj in gate_jobs:
            log.info("    Skipping gate job: %s (ID: %d)", gj["name"], gj["id"])
        failed_jobs = [j for j in failed_jobs if not _is_gate_job(j)]

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
        max_workers = min(
            MAX_PARALLEL_JOBS if not use_agent else 2,
            len(jobs_to_analyze),
        )
        mode = "agent" if use_agent else "API"
        log.info("  Analyzing %d job(s) (%s mode, workers: %d)...",
                 len(jobs_to_analyze), mode, max_workers)

        if use_agent and agent_repo_path:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        _analyze_job_with_agent, job, run_url,
                        agent_repo_path, workflow_file,
                    ): job
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
            client = create_anthropic_client()
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
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
    gh_comments: list[dict] | None = None,
    use_agent: bool = False,
):
    """Publish or update the workflow comment in the daily issue.

    Adopts an existing comment for this workflow if one exists in the issue
    (recovering state from the comment body), ensuring one comment per workflow
    even across process restarts or cache eviction.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_daily_state(state, date_str)

    if not daily.get("issue_number"):
        issue_num, created = find_or_create_daily_issue(token, bot_repo, date_str)
        daily["issue_number"] = issue_num
        log.info("%s daily issue #%d", "Created" if created else "Found", issue_num)

    issue_num = daily["issue_number"]
    wf_state = get_workflow_state(daily, workflow_file)

    # Adopt existing comment if we don't own one yet
    if not (wf_state.get("comment_id") and wf_state.get("owned")):
        if gh_comments is None:
            try:
                gh_comments = get_issue_comments(token, bot_repo, issue_num)
            except Exception:
                gh_comments = []

        existing_comment = find_workflow_comment(gh_comments, workflow_file)
        if existing_comment:
            wf_state["comment_id"] = existing_comment["id"]
            wf_state["owned"] = True
            recovered = parse_job_analyses_from_comment(existing_comment["body"])
            if recovered:
                recovered_ids = {ja["job_id"] for ja in recovered}
                for ja in wf_state.get("job_analyses", []):
                    if ja["job_id"] not in recovered_ids:
                        recovered.append(ja)
                wf_state["job_analyses"] = recovered
                log.info("  Adopted comment %d for %s (%d existing analyses)",
                         existing_comment["id"], workflow_file, len(recovered))

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

    body = render_workflow_comment(
        workflow_file, all_analyses, pending_info, cross,
        use_agent=use_agent,
    )
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
# One-shot mode (triggered by GitHub Actions cron)
# ---------------------------------------------------------------------------

def run_oneshot(
    token: str,
    bot_repo: str | None,
    output: str,
    workflows: list[str],
    hours_back: int,
    branch: str,
    job_name_filter: str | None = None,
    use_agent: bool = False,
):
    """Run the CI monitor once and exit."""
    agent_repo_path = None
    if use_agent:
        if not claude_code_available():
            log.warning("--use-agent specified but Claude Code CLI not found, falling back to API mode")
            use_agent = False
        else:
            try:
                agent_repo_path = ensure_sglang_repo()
            except Exception:
                log.exception("Failed to clone sglang repo, falling back to API mode")
                use_agent = False

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
                use_agent=use_agent,
                agent_repo_path=agent_repo_path,
            )

            if not new_analyses:
                continue

            total_reports += 1

            if output == "stdout":
                cross = ""
                if len(new_analyses) > 1:
                    client = create_anthropic_client()
                    cross = cross_job_analysis(client, wf, new_analyses)
                body = render_workflow_comment(
                    wf, new_analyses, pending, cross,
                    use_agent=use_agent,
                )
                print(f"\n{'='*60}")
                print(body)

            elif output == "daily-issue" and bot_repo:
                publish_workflow_report(
                    token, bot_repo, wf, new_analyses, pending, state,
                    gh_comments=gh_comments,
                    use_agent=use_agent,
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
        "--branch", default="main",
        help="Only analyze runs triggered on this branch (default: main)",
    )
    parser.add_argument(
        "--use-agent", action="store_true",
        default=os.environ.get("USE_AGENT", "").lower() in ("true", "1", "yes"),
        help="Use Claude Code agent for deeper analysis (reads source code, git history)",
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

    if not args.use_agent:
        if not os.environ.get("LLM_GATEWAY_KEY"):
            log.error("LLM_GATEWAY_KEY env var required.")
            sys.exit(1)
        if not os.environ.get("LLM_GATEWAY_URL"):
            log.error("LLM_GATEWAY_URL env var required.")
            sys.exit(1)

    run_oneshot(
        args.github_token, args.bot_repo, args.output,
        args.workflows, args.hours_back, args.branch, args.job_name,
        use_agent=args.use_agent,
    )


if __name__ == "__main__":
    main()
