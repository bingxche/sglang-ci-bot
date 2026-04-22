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

Runs as a one-shot process triggered by GitHub Actions workflow_dispatch
(every 30min from runner-1's entrypoint.sh dispatch loop).
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
    REPO,
    analyze_job_api,
    analyze_job_with_agent,
    claude_code_available,
    create_agent_worktree,
    create_anthropic_client,
    create_github_issue,
    cross_job_analysis,
    delete_comment,
    ensure_sglang_repo,
    get_failed_jobs,
    gh_headers,
    is_gate_job,
    post_comment,
    remove_agent_worktree,
    update_comment,
)

log = logging.getLogger("ci-monitor")

MONITORED_WORKFLOWS = [
    "nightly-test-amd.yml",
    "nightly-test-amd-rocm720.yml",
    "release-docker-amd-nightly.yml",
    "release-docker-amd-rocm720-nightly.yml",
    "amd-aiter-scout.yml",
    "pr-test-amd.yml",
    "pr-test-amd-rocm720.yml",
]

# All monitored workflows are analyzed only when triggered by ``schedule``.
# Manually-dispatched (``workflow_dispatch``) and PR-triggered runs are
# intentionally excluded so the daily report is not polluted by ad-hoc /
# debug runs. Use ``analyze-ci.yml`` (Actions tab → "Analyze CI") if you
# need on-demand analysis of a specific manual run.
SCHEDULE_ONLY_WORKFLOWS = set(MONITORED_WORKFLOWS)

SUCCESS_CONCLUSIONS = {"success"}

STATE_FILE = Path(__file__).parent.parent / ".state" / "ci_monitor.json"
MAX_PARALLEL_JOBS = 3


def _agent_parallel() -> int:
    """Max parallel Claude Code agents per run. Configurable via AGENT_PARALLEL."""
    try:
        return max(1, int(os.environ.get("AGENT_PARALLEL", "3")))
    except ValueError:
        return 3

_PROCESSED_IDS_RE = re.compile(r"<!-- processed_job_ids: ([\d,]+) -->")
_PART_RE = re.compile(r"<!-- ci-monitor-part: (\d+)/(\d+) -->")

COMMENT_MAX_BYTES = 60000


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
            "overflow_ids": [],
            "cross_run_comment_id": None,
            "owned": False,
            "job_analyses": [],
            "last_pending_count": 0,
        }
    wfs[workflow_file].setdefault("overflow_ids", [])
    wfs[workflow_file].setdefault("cross_run_comment_id", None)
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
    event: str | None = None,
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
    if event:
        base_params["event"] = event

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


# Strict per-job details block — every recoverable block MUST be emitted by
# ``_render_per_job_block`` and carries the job_id / run_url / started_at as
# HTML attributes on the <details> tag. We deliberately do NOT scan loose
# markdown table rows (the previous heuristic over-matched 4-column tables
# inside agent-generated analysis text — cluster summaries, hypothesis
# tables, failed-test lists — and produced fake job entries with job_id=0
# that snowballed into 800+ comment parts; see issue 42 postmortem).
_DETAILS_BLOCK_RE = re.compile(
    r'<details\s+data-job-id="(\d+)"\s+data-run-url="([^"]*)"\s+'
    r'data-started-at="([^"]*)">\s*'
    r'<summary><b>(.+?)</b> — failed step\(s\): (.+?)</summary>'
    r'\s*\n(.*?)\n</details>',
    re.DOTALL,
)


def find_workflow_comment(
    comments: list[dict], workflow_file: str,
) -> dict | None:
    """Find the most recent main (part 1) comment for a workflow.

    Returns comment dict or None. Prefers explicit part-1 markers; falls back
    to any workflow comment (legacy single-comment format) when no part
    markers are found.
    """
    marker = f"## `{workflow_file}`"
    legacy_fallback: dict | None = None
    for comment in reversed(comments):
        body = comment.get("body", "")
        if marker not in body:
            continue
        part_m = _PART_RE.search(body)
        if part_m is None:
            if legacy_fallback is None:
                legacy_fallback = comment
            continue
        if int(part_m.group(1)) == 1:
            return comment
    return legacy_fallback


def find_workflow_comment_parts(
    comments: list[dict], workflow_file: str,
) -> tuple[dict | None, list[dict]]:
    """Return (main_comment, overflow_comments) for a workflow.

    - main_comment: part 1 (or legacy single comment with no part marker).
    - overflow_comments: parts 2..N, sorted by part index.
    """
    marker = f"## `{workflow_file}`"
    main: dict | None = None
    legacy: dict | None = None
    overflows: list[tuple[int, dict]] = []
    for comment in comments:
        body = comment.get("body", "")
        if marker not in body:
            continue
        part_m = _PART_RE.search(body)
        if part_m is None:
            if legacy is None:
                legacy = comment
            continue
        part_idx = int(part_m.group(1))
        if part_idx == 1:
            if main is None or comment["id"] < main["id"]:
                main = comment
        else:
            overflows.append((part_idx, comment))
    if main is None:
        main = legacy
    overflows.sort(key=lambda t: (t[0], t[1]["id"]))
    return main, [c for _, c in overflows]


def parse_job_analyses_from_comment(body: str) -> list[dict]:
    """Reconstruct job_analyses list from one or more concatenated comment bodies.

    Strictly iterates ``<details data-job-id="...">`` blocks emitted by
    ``_render_per_job_block``. Each block is self-contained: ``job_id``,
    ``run_url`` and ``started_at`` come from HTML attributes on the
    ``<details>`` tag itself (not from a sibling table row), and the
    ``analysis`` text is the inner content. Blocks without the
    ``data-job-id`` attribute (legacy comments emitted before the strict
    format) are intentionally ignored — they are not recoverable, but
    ``extract_processed_ids_from_comments`` continues to dedup against
    their ``processed_job_ids`` marker so no job is re-analyzed; the next
    cron run simply re-renders today's analyses in the new format.

    Callers with multi-part comments should concatenate their bodies
    before invoking this helper.
    """
    analyses: list[dict] = []
    seen_ids: set[int] = set()
    for m in _DETAILS_BLOCK_RE.finditer(body):
        job_id_str, run_url, started_at, job_name, failed_steps_str, analysis = m.groups()
        try:
            job_id = int(job_id_str)
        except ValueError:
            continue
        if job_id == 0 or job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        failed_steps = (
            [s.strip() for s in failed_steps_str.split(",")]
            if failed_steps_str.strip() and failed_steps_str.strip() != "N/A"
            else []
        )
        started = started_at.strip() or None
        analyses.append({
            "run_url": run_url.strip(),
            "job_name": job_name.strip(),
            "job_id": job_id,
            "started_at": started,
            "failed_steps": failed_steps,
            "analysis": analysis.strip(),
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


DAILY_BOARD_PLACEHOLDER_START = "<!-- ci-monitor-daily-status-board:start -->"
DAILY_BOARD_PLACEHOLDER_END = "<!-- ci-monitor-daily-status-board:end -->"


def _initial_issue_body(date_str: str) -> str:
    """Render the initial daily-issue body, with a board placeholder block.

    The placeholder is delimited by HTML markers so that
    ``build_daily_status_board.py`` can PATCH the rendered status board
    in place without disturbing the rest of the body. This is what
    lets the daily report be pinned at the very top of the issue
    (as opposed to being posted as a comment that always lands below
    earlier per-workflow comments).
    """
    wf_list = "\n".join(f"- `{w}`" for w in MONITORED_WORKFLOWS)
    return (
        f"{DAILY_BOARD_PLACEHOLDER_START}\n"
        f"_The daily cross-workflow status board will appear here once the "
        f"first scan completes (typically within 30 minutes of the first run "
        f"finishing)._\n"
        f"{DAILY_BOARD_PLACEHOLDER_END}\n\n"
        f"---\n\n"
        f"## CI Monitor — {date_str}\n\n"
        f"**Repo**: [{REPO}](https://github.com/{REPO})\n\n"
        f"**Monitored Workflows**:\n"
        f"{wf_list}\n\n"
        f"*Per-workflow failure reports are appended as comments below; "
        f"the cross-workflow daily status board is rendered above this section.*\n"
    )


def find_or_create_daily_issue(
    token: str, bot_repo: str, date_str: str
) -> tuple[int, bool]:
    """Find or create the daily CI monitoring issue. Returns (number, created)."""
    existing = find_daily_issue(token, bot_repo, date_str)
    if existing is not None:
        return existing, False

    title = f"[CI Monitor] Daily Report - {date_str}"
    body = _initial_issue_body(date_str)
    issue = create_github_issue(
        token, title, body, labels=["ci-monitor"], repo=bot_repo
    )
    return issue["number"], True


# ---------------------------------------------------------------------------
# Comment rendering
# ---------------------------------------------------------------------------

_FIRST_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)

# Anchors that mark the START of the canonical report body for each
# generated artefact. Used by ``_extract_report`` to discard LLM
# preamble and intermediate drafts and keep only the final version,
# even when the model writes its report multiple times in the same
# turn (a real production failure mode — see issue 41 comment 4277196502
# which contained 12 copies of `**Counts**:` in one cross-summary).
_CROSS_SUMMARY_ANCHOR_RE = re.compile(r"^\*\*Counts\*\*:\s", re.MULTILINE)
_PER_JOB_ANCHOR_RE = re.compile(r"^###\s+Commit Info\b", re.MULTILINE)
_DAILY_BOARD_ANCHOR_RE = re.compile(r"^#\s+CI Daily Health\b", re.MULTILINE)


def _strip_llm_preamble(text: str) -> str:
    """Drop any LLM "thinking aloud" prose before the first markdown heading.

    Used as a *fallback* by ``_extract_report`` when no anchor is found.
    Claude Code occasionally streams reasoning text (e.g. "Now I have
    all the evidence, let me write the report.") before the actual
    report. Stripping anything before the first ``^#{1,6}\\s`` heading
    removes that noise.

    NOTE: This function does NOT recognise ``**Counts**:`` (bold marker,
    not a heading) as a valid start of report. For outputs that start
    with ``**Counts**:`` rather than a ``#`` heading, prefer
    ``_extract_report`` with ``_CROSS_SUMMARY_ANCHOR_RE`` so the
    Counts line + table aren't accidentally trimmed when a later
    ``### Cluster:`` heading is found first.

    Returns the input untouched when no heading is present so very
    short outputs are preserved.
    """
    if not text:
        return text
    body = text.lstrip()
    m = _FIRST_HEADING_RE.search(body)
    if m and m.start() > 0:
        return body[m.start():]
    return body


def _keep_last_section(text: str, anchor_re: re.Pattern) -> str:
    """Trim text to start at the LAST occurrence of ``anchor_re``.

    Discards every byte before the final anchor match — that captures
    BOTH (a) LLM "thinking aloud" preamble and (b) intermediate drafts
    that the model emitted before re-writing the report cleanly. The
    final draft is always the last one, so keeping from the last anchor
    to end-of-text yields exactly the canonical report (including
    everything that follows the anchor: tables, cluster details,
    suggested triage order, …).

    Returns text unchanged if the anchor is absent — callers should
    use ``_extract_report`` to combine this with a heading-based
    fallback for malformed outputs.
    """
    if not text:
        return text
    matches = list(anchor_re.finditer(text))
    if not matches:
        return text
    return text[matches[-1].start():]


def _extract_report(text: str, anchor_re: re.Pattern) -> str:
    """Extract the canonical report body from raw LLM output.

    The combined recipe used at every render site:

      1. If the anchor is present anywhere in *text*, trim to the LAST
         occurrence. Handles both the well-behaved single-pass case
         (anchor preceded by a small preamble) and the multi-draft
         pathological case (anchor appearing 12+ times — see
         ``_CROSS_SUMMARY_ANCHOR_RE`` docstring above).
      2. If the anchor is absent (LLM didn't follow the required
         format), fall back to ``_strip_llm_preamble`` which drops
         everything before the first ``#`` heading.

    This ordering is crucial: applying ``_strip_llm_preamble`` first
    can lose the ``**Counts**:`` line (it's not a heading) and trim
    past the first ``### Cluster:`` heading instead, dropping the
    Summary table.
    """
    if anchor_re.search(text):
        return _keep_last_section(text, anchor_re)
    return _strip_llm_preamble(text)


def _render_per_job_block(ja: dict) -> str:
    """Render a single <details> block for one job analysis."""
    job_id = ja.get("job_id", 0)
    run_url = ja.get("run_url", "")
    job_log_url = (
        f"{run_url.rstrip('/')}/job/{job_id}" if run_url and job_id else ""
    )

    raw_analysis = (ja.get("analysis") or "").strip()
    analysis_text = _extract_report(raw_analysis, _PER_JOB_ANCHOR_RE)
    stub_marker = (
        not analysis_text
        or len(analysis_text) < 200
        or analysis_text.lower().startswith(
            ("stub", "analysis failed", "agent timed out")
        )
    )

    if stub_marker:
        summary_suffix = " — ⚠️ analysis failed"
        started = ja.get("started_at") or "N/A"
        failed_steps_line = ", ".join(ja["failed_steps"]) or "N/A"
        details_body = (
            "**Analysis did not complete.** The per-job agent produced no "
            "usable output (likely a timeout, log download failure, or "
            "subprocess crash). Manual triage required.\n\n"
            f"- **Run**: [{run_url}]({run_url})\n"
            f"- **Job log**: [{job_log_url}]({job_log_url})\n"
            f"- **Job ID**: `{job_id}`\n"
            f"- **Failed step(s)**: {failed_steps_line}\n"
            f"- **Started (UTC)**: {started[:16].replace('T', ' ') if started != 'N/A' else 'N/A'}\n"
        )
    else:
        summary_suffix = ""
        details_body = analysis_text

    started_at = ja.get("started_at") or ""
    return (
        f"\n<a id=\"job-{job_id}\"></a>\n"
        f"<details data-job-id=\"{job_id}\" "
        f"data-run-url=\"{run_url}\" "
        f"data-started-at=\"{started_at}\">\n"
        f"<summary><b>{ja['job_name']}</b> — failed step(s): "
        f"{', '.join(ja['failed_steps']) or 'N/A'}{summary_suffix}</summary>\n\n"
        f"{details_body}\n\n"
        f"</details>\n"
    )


def render_workflow_comment_parts(
    workflow_file: str,
    job_analyses: list[dict],
    pending_info: list[dict] | None = None,
    cross_summary: str = "",
    use_agent: bool = True,
    max_bytes: int = COMMENT_MAX_BYTES,
) -> list[str]:
    """Render the workflow report as a list of comment bodies.

    Produces part 1 (with header / cross summary / job table) plus zero or
    more overflow parts carrying the remaining per-job ``<details>`` blocks.
    Every part embeds the full ``processed_job_ids`` metadata and a
    ``ci-monitor-part: i/N`` marker so that downstream readers
    (dedup + adoption logic) can recognise and reassemble the full report.

    Each part is kept under *max_bytes* (default ~60 KB, well below
    GitHub's 65 536-char comment limit).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    started_ats = [ja.get("started_at") for ja in job_analyses if ja.get("started_at")]
    run_started = (
        min(started_ats)[:16].replace("T", " ") + " UTC"
        if started_ats else "N/A"
    )

    job_ids_csv = ",".join(str(ja["job_id"]) for ja in job_analyses)
    metadata_ids = f"<!-- processed_job_ids: {job_ids_csv} -->"

    def _job_link(ja: dict) -> str:
        run_url = (ja.get("run_url") or "").rstrip("/")
        job_id = ja.get("job_id") or 0
        if run_url and job_id:
            return f"{run_url}/job/{job_id}"
        return run_url

    job_table_rows = "\n".join(
        f"| [`{ja['job_name']}`]({_job_link(ja)}) "
        f"| {', '.join(ja['failed_steps']) or 'N/A'} "
        f"| {ja.get('started_at', 'N/A')[:16] if ja.get('started_at') else 'N/A'} |"
        for ja in job_analyses
    )

    unique_shas = dict.fromkeys(
        ja.get("head_sha", "") for ja in job_analyses
    )
    commit_parts = []
    for sha in unique_shas:
        if sha:
            short = sha[:7]
            commit_parts.append(
                f"[`{short}`](https://github.com/{REPO}/commit/{sha})"
            )
    commits_line = (
        f"**Commits**: sglang {', '.join(commit_parts)}\n\n"
        if commit_parts else ""
    )

    method = "Claude Code CLI" if use_agent else "Claude API"
    footer = f"\n---\n*Generated by amd-bot using {method} (last updated: {now})*\n"

    total_pending = sum(p["count"] for p in pending_info) if pending_info else 0
    pending_block = ""
    if total_pending > 0:
        run_links = ", ".join(
            f"[run](https://github.com/{REPO}/actions/runs/{p['run_id']})"
            for p in pending_info if p["count"] > 0
        )
        pending_block = (
            f"\n---\n"
            f"\u23f3 **{total_pending} job(s) still running** "
            f"({run_links}) — will update when complete\n"
        )

    part1_header = f"""## `{workflow_file}` — {len(job_analyses)} failure(s)

**Run started (UTC)**: {run_started}
**Last scanned (UTC)**: {now}
{commits_line}"""

    if cross_summary:
        extracted = _extract_report(cross_summary, _CROSS_SUMMARY_ANCHOR_RE)
        cleaned = re.sub(
            r"^#{1,3}\s+.*(?:Summary|Overview).*$", "",
            extracted, flags=re.MULTILINE,
        ).strip()
        part1_header += f"### Summary\n\n{cleaned}\n\n---\n\n"

    part1_header += (
        f"| Job | Failed Steps | Started |\n"
        f"|-----|-------------|---------|\n"
        f"{job_table_rows}\n\n"
        f"### Per-Job Analysis\n"
    )

    detail_blocks = [_render_per_job_block(ja) for ja in job_analyses]

    def part_prefix(idx: int, total: int) -> str:
        return (
            f"{metadata_ids}\n"
            f"<!-- ci-monitor-part: {idx}/{total} -->\n"
        )

    def overflow_header(idx: int, total: int) -> str:
        return (
            f"## `{workflow_file}` — Per-Job Analysis "
            f"(continued {idx}/{total})\n\n"
        )

    slack = 400

    def _try_pack(total_parts: int) -> list[str] | None:
        bodies: list[str] = []
        remaining = list(detail_blocks)
        for idx in range(1, total_parts + 1):
            prefix = part_prefix(idx, total_parts)
            header = part1_header if idx == 1 else overflow_header(idx, total_parts)
            is_last = (idx == total_parts)
            tail = (pending_block if is_last else "") + footer
            budget = max_bytes - len(prefix) - len(header) - len(tail) - slack
            if budget <= 0:
                return None
            body_mid = ""
            while remaining:
                blk = remaining[0]
                if len(body_mid) + len(blk) > budget and body_mid:
                    break
                body_mid += blk
                remaining.pop(0)
                if not body_mid and len(blk) > budget:
                    body_mid = blk
                    break
            if idx < total_parts and not body_mid:
                return None
            bodies.append(prefix + header + body_mid + tail)
        if remaining:
            return None
        return bodies

    if not detail_blocks:
        only = (
            part_prefix(1, 1) + part1_header
            + "\n_(no per-job analyses yet)_\n"
            + pending_block + footer
        )
        return [only]

    for total in range(1, len(detail_blocks) + 2):
        packed = _try_pack(total)
        if packed is not None:
            return packed

    chunks = []
    for i, blk in enumerate(detail_blocks, start=1):
        header = part1_header if i == 1 else overflow_header(i, len(detail_blocks))
        prefix = part_prefix(i, len(detail_blocks))
        tail = (pending_block if i == len(detail_blocks) else "") + footer
        chunks.append(prefix + header + blk + tail)
    return chunks


def render_workflow_comment(
    workflow_file: str,
    job_analyses: list[dict],
    pending_info: list[dict] | None = None,
    cross_summary: str = "",
    use_agent: bool = True,
) -> str:
    """Back-compat: render all parts concatenated (used by stdout mode)."""
    parts = render_workflow_comment_parts(
        workflow_file, job_analyses,
        pending_info=pending_info,
        cross_summary=cross_summary,
        use_agent=use_agent,
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Cross-run pattern summary (multiple scheduled runs in the lookback window)
# ---------------------------------------------------------------------------

CROSS_RUN_MARKER_TMPL = "<!-- ci-monitor-cross-run-summary: {wf} -->"
_CROSS_RUN_RE_TMPL = r"<!-- ci-monitor-cross-run-summary: {wf} -->"


def _runs_from_analyses(job_analyses: list[dict]) -> list[dict]:
    """Group job analyses by their run_url and return per-run summaries."""
    by_run: dict[str, list[dict]] = {}
    for ja in job_analyses:
        by_run.setdefault(ja.get("run_url", ""), []).append(ja)

    runs: list[dict] = []
    for run_url, jas in by_run.items():
        if not run_url:
            continue
        run_id = run_url.rstrip("/").split("/")[-1]
        starts = [ja.get("started_at") for ja in jas if ja.get("started_at")]
        started = min(starts) if starts else ""
        sha = next((ja.get("head_sha") for ja in jas if ja.get("head_sha")), "")
        runs.append({
            "run_url": run_url,
            "run_id": run_id,
            "started_at": started,
            "head_sha": sha,
            "job_names": sorted(ja["job_name"] for ja in jas),
            "n_jobs": len(jas),
        })
    runs.sort(key=lambda r: r.get("started_at") or r.get("run_id"))
    return runs


def _compute_cross_run_patterns(runs: list[dict]) -> dict:
    """Bucket job names into persistent / regression / flaky across runs."""
    job_to_runs: dict[str, list[str]] = {}
    for run in runs:
        for jn in run["job_names"]:
            job_to_runs.setdefault(jn, []).append(run["run_id"])

    n_runs = len(runs)
    latest_id = runs[-1]["run_id"] if runs else None
    persistent: list[tuple[str, list[str]]] = []
    regression: list[tuple[str, list[str]]] = []
    flaky: list[tuple[str, list[str]]] = []

    for jn, ids in sorted(job_to_runs.items()):
        unique = sorted(set(ids))
        if n_runs > 1 and len(unique) == n_runs:
            persistent.append((jn, unique))
        elif latest_id and unique == [latest_id]:
            regression.append((jn, unique))
        else:
            flaky.append((jn, unique))

    return {
        "n_runs": n_runs,
        "persistent": persistent,
        "regression": regression,
        "flaky": flaky,
    }


def _format_pattern_table(
    workflow_file: str,
    runs: list[dict],
    patterns: dict,
) -> str:
    """Format the deterministic (no-LLM) portion of the cross-run summary."""
    run_id_to_url = {r["run_id"]: r["run_url"] for r in runs}

    runs_table_rows = "\n".join(
        f"| [`{r['run_id']}`]({r['run_url']}) "
        f"| {(r.get('started_at') or 'N/A')[:16].replace('T', ' ')} "
        f"| {r['n_jobs']} |"
        for r in runs
    )

    def _fmt_bucket(bucket: list[tuple[str, list[str]]]) -> str:
        if not bucket:
            return "_(none)_\n"
        out = ""
        for jn, ids in bucket:
            run_links = ", ".join(
                f"[`{rid[-7:]}`]({run_id_to_url.get(rid, '#')})" for rid in ids
            )
            out += f"- `{jn}` — in {len(ids)}/{patterns['n_runs']} runs ({run_links})\n"
        return out

    return (
        f"### Runs analysed\n\n"
        f"| Run | Started (UTC) | Failed jobs |\n"
        f"|-----|--------------|-------------|\n"
        f"{runs_table_rows}\n\n"
        f"### Persistent failures (in every run)\n\n"
        f"{_fmt_bucket(patterns['persistent'])}\n"
        f"### Latest-only failures (potential regressions)\n\n"
        f"{_fmt_bucket(patterns['regression'])}\n"
        f"### Flaky / intermittent failures\n\n"
        f"{_fmt_bucket(patterns['flaky'])}"
    )


def _render_cross_run_summary(
    workflow_file: str,
    runs: list[dict],
    patterns: dict,
    agent_text: str = "",
    use_agent: bool = True,
) -> str:
    """Render the full cross-run summary comment body."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    marker = CROSS_RUN_MARKER_TMPL.format(wf=workflow_file)
    pattern_block = _format_pattern_table(workflow_file, runs, patterns)
    method = "Claude Code CLI" if use_agent else "Claude API"

    body = (
        f"{marker}\n"
        f"## `{workflow_file}` — Cross-Run Pattern Summary "
        f"({patterns['n_runs']} runs in lookback window)\n\n"
        f"**Last scanned (UTC)**: {now}\n\n"
    )
    if agent_text:
        # Cross-Run Pattern Analysis output has no fixed anchor (starts
        # with a free-form "headline sentence" per CLAUDE.md), so use
        # _strip_llm_preamble directly. If the LLM ever drafts the report
        # multiple times here too, we'd need an anchor; for now, only the
        # plain preamble strip applies.
        stripped = _strip_llm_preamble(agent_text)
        cleaned = re.sub(
            r"^#{1,3}\s+.*(?:Summary|Overview|Cross-Run).*$", "",
            stripped, flags=re.MULTILINE,
        ).strip()
        body += f"### Agent assessment\n\n{cleaned}\n\n---\n\n"
    body += pattern_block
    body += f"\n---\n*Generated by amd-bot using {method} (last updated: {now})*\n"
    return body


def _build_cross_run_prompt(
    workflow_file: str,
    runs: list[dict],
    patterns: dict,
    job_analyses: list[dict],
) -> str:
    """Compose a data-only prompt for ``Task: Cross-Run Pattern Analysis``.

    Methodology and output format live in ``agent/CLAUDE.md`` under
    ``## Cross-Run Pattern Analysis``. This function only assembles the
    deterministic data the agent needs (per-run summary + pre-computed
    persistent / regression / flaky buckets) so the same instructions
    don't have to be maintained in two places.
    """
    runs_lines = []
    for r in runs:
        runs_lines.append(
            f"- run {r['run_id']} ({(r.get('started_at') or 'N/A')[:16]}): "
            f"{r['n_jobs']} failed jobs, sha {r.get('head_sha', '')[:7]}"
        )

    def _bucket_text(name: str, bucket: list[tuple[str, list[str]]]) -> str:
        if not bucket:
            return f"{name}: (none)\n"
        rows = "\n".join(
            f"  - {jn} (runs: {', '.join(ids)})" for jn, ids in bucket
        )
        return f"{name}:\n{rows}\n"

    persistent_block = _bucket_text(
        "Persistent (every completed run in window)", patterns["persistent"],
    )
    regression_block = _bucket_text(
        "Regression candidates (latest completed run only)", patterns["regression"],
    )
    flaky_block = _bucket_text(
        "Flaky / intermittent", patterns["flaky"],
    )

    return (
        f"Task: Cross-Run Pattern Analysis\n"
        f"Workflow: {workflow_file}\n"
        f"Runs in window: {patterns['n_runs']}\n\n"
        f"Per-run summary:\n" + "\n".join(runs_lines) + "\n\n"
        f"Failure buckets (computed):\n"
        f"{persistent_block}\n"
        f"{regression_block}\n"
        f"{flaky_block}\n"
        f"Per-job analyses are available in .ci-context/per-job-analyses.md.\n\n"
        f"Source: current directory\n"
        f"GitHub API token: $GH_PAT"
    )


def maybe_publish_cross_run_summary(
    token: str,
    bot_repo: str,
    issue_num: int,
    workflow_file: str,
    job_analyses: list[dict],
    wf_state: dict,
    gh_comments: list[dict],
    use_agent: bool = True,
    agent_repo_path: "Path | None" = None,
) -> None:
    """Post or update a cross-run pattern summary if multiple runs are present.

    Triggered for SCHEDULE_ONLY workflows (e.g. pr-test-amd.yml) where the
    24-hour lookback typically includes 2-4 scheduled runs. Skipped silently
    when only one run's worth of analyses are available.
    """
    runs = _runs_from_analyses(job_analyses)
    if len(runs) < 2:
        return

    patterns = _compute_cross_run_patterns(runs)

    agent_text = ""
    if use_agent and agent_repo_path is not None:
        try:
            from utils import claude_code_analyze
            jobs_text = _format_per_job_dump(job_analyses)
            prompt = _build_cross_run_prompt(
                workflow_file, runs, patterns, job_analyses,
            )
            agent_text = claude_code_analyze(
                prompt=prompt,
                work_dir=agent_repo_path,
                context_files={"per-job-analyses.md": jobs_text},
                max_turns=int(os.environ.get("AGENT_MAX_TURNS", "150")),
                timeout_secs=int(os.environ.get("CROSS_RUN_TIMEOUT_SECS", "600")),
            )
        except Exception as exc:
            log.warning("  Cross-run agent analysis failed (%s)", exc)

    body = _render_cross_run_summary(
        workflow_file, runs, patterns, agent_text=agent_text, use_agent=use_agent,
    )

    cross_marker = CROSS_RUN_MARKER_TMPL.format(wf=workflow_file)
    existing = next(
        (c for c in gh_comments if cross_marker in c.get("body", "")),
        None,
    )
    cached_id = wf_state.get("cross_run_comment_id")
    target_id = (existing or {}).get("id") or cached_id

    if target_id:
        try:
            update_comment(token, bot_repo, target_id, body)
            wf_state["cross_run_comment_id"] = target_id
            log.info(
                "  Updated cross-run summary comment %d for %s (%d runs)",
                target_id, workflow_file, patterns["n_runs"],
            )
            return
        except Exception as exc:
            log.warning(
                "  Cross-run summary update for %d failed (%s); reposting",
                target_id, exc,
            )

    resp = post_comment(token, bot_repo, issue_num, body)
    wf_state["cross_run_comment_id"] = resp["id"]
    log.info(
        "  Posted cross-run summary comment %d for %s (%d runs)",
        resp["id"], workflow_file, patterns["n_runs"],
    )


def _format_per_job_dump(job_analyses: list[dict]) -> str:
    """Compact per-job dump used as context for cross-run analysis."""
    out = []
    for ja in job_analyses:
        out.append(
            f"### Run {ja.get('run_url', '?')}\n"
            f"**Job**: {ja.get('job_name', '?')} (id {ja.get('job_id', '?')})\n"
            f"**Failed steps**: {', '.join(ja.get('failed_steps', [])) or 'N/A'}\n"
            f"**Analysis**:\n{(ja.get('analysis') or '').strip()}\n"
        )
    return "\n---\n".join(out)


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
    use_agent: bool = True,
    agent_repo_path: Path | None = None,
    event: str | None = None,
) -> tuple[list[dict], list[int], list[dict]]:
    """Monitor a single workflow.

    Returns (new_job_analyses, new_job_ids, pending_info).
    """
    log.info("Monitoring: %s (branch: %s, event: %s)", workflow_file, branch, event or "all")

    runs = get_workflow_runs(token, workflow_file, hours_back=hours_back, branch=branch, event=event)
    if not runs:
        log.info("  No actionable runs in the last %d hours.", hours_back)
        return [], [], []

    completed_runs = [r for r in runs if r.get("status") == "completed"]
    in_progress_runs = [r for r in runs if r.get("status") != "completed"]
    log.info(
        "  %d completed non-success + %d in-progress run(s)",
        len(completed_runs), len(in_progress_runs),
    )

    jobs_to_analyze: list[tuple[dict, str, str]] = []
    pending_info: list[dict] = []

    for run in runs:
        run_id = run["id"]
        run_url = run["html_url"]
        head_sha = run.get("head_sha", "")
        run_status = run.get("status", "unknown")
        run_conclusion = run.get("conclusion") or "in_progress"
        log.info("  Run %d [%s/%s]: %s", run_id, run_status, run_conclusion, run_url)

        failed_jobs = get_failed_jobs(token, run_id)
        if job_name_filter:
            failed_jobs = [j for j in failed_jobs if job_name_filter in j["name"]]
        if processed_job_ids:
            failed_jobs = [j for j in failed_jobs if j["id"] not in processed_job_ids]

        gate_jobs = [j for j in failed_jobs if is_gate_job(j)]
        for gj in gate_jobs:
            log.info("    Skipping gate job: %s (ID: %d)", gj["name"], gj["id"])
        failed_jobs = [j for j in failed_jobs if not is_gate_job(j)]

        if failed_jobs:
            log.info("    %d new failed job(s) to analyze", len(failed_jobs))
            for job in failed_jobs:
                jobs_to_analyze.append((job, run_url, head_sha))

        if run_status != "completed":
            pi = get_pending_job_info(token, run_id)
            if pi["count"] > 0:
                pending_info.append(pi)

    new_job_analyses: list[dict] = []
    new_job_ids: list[int] = []

    if jobs_to_analyze:
        max_workers = min(
            _agent_parallel() if use_agent else MAX_PARALLEL_JOBS,
            len(jobs_to_analyze),
        )
        mode = "agent" if use_agent else "API"
        log.info("  Analyzing %d job(s) (%s mode, workers: %d)...",
                 len(jobs_to_analyze), mode, max_workers)

        if use_agent and agent_repo_path:
            worktrees: dict[int, Path] = {}
            try:
                for job, _, sha in jobs_to_analyze:
                    wt = create_agent_worktree(job["id"], head_sha=sha)
                    worktrees[job["id"]] = wt

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(
                            analyze_job_with_agent, job, run_url,
                            worktrees[job["id"]], workflow_file,
                            head_sha=sha,
                            event_filter=event or "",
                        ): job
                        for job, run_url, sha in jobs_to_analyze
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
            finally:
                for wt in worktrees.values():
                    try:
                        remove_agent_worktree(wt)
                    except Exception:
                        log.warning("Failed to clean up worktree %s", wt)
        else:
            client = create_anthropic_client()
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(analyze_job_api, client, token, job, run_url, head_sha=sha): job
                    for job, run_url, sha in jobs_to_analyze
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
    use_agent: bool = True,
    agent_repo_path: "Path | None" = None,
):
    """Publish or update the workflow comment(s) in the daily issue.

    Supports multi-part comments: the primary (part 1) comment carries the
    header / cross summary / job table; overflow comments (parts 2..N) carry
    the remaining per-job ``<details>`` blocks. Existing comments are
    adopted so a re-run patches in place instead of duplicating content.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = get_daily_state(state, date_str)

    if not daily.get("issue_number"):
        issue_num, created = find_or_create_daily_issue(token, bot_repo, date_str)
        daily["issue_number"] = issue_num
        log.info("%s daily issue #%d", "Created" if created else "Found", issue_num)

    issue_num = daily["issue_number"]
    wf_state = get_workflow_state(daily, workflow_file)
    wf_state.setdefault("overflow_ids", [])

    if gh_comments is None:
        try:
            gh_comments = get_issue_comments(token, bot_repo, issue_num)
        except Exception:
            gh_comments = []

    main_comment, overflow_comments = find_workflow_comment_parts(
        gh_comments, workflow_file,
    )

    if main_comment is not None:
        if wf_state.get("comment_id") != main_comment["id"]:
            log.info(
                "  Adopting main comment %d for %s",
                main_comment["id"], workflow_file,
            )
        wf_state["comment_id"] = main_comment["id"]
        wf_state["owned"] = True
        combined_body = main_comment["body"] + "\n" + "\n".join(
            c["body"] for c in overflow_comments
        )
        recovered = parse_job_analyses_from_comment(combined_body)
        if recovered:
            recovered_ids = {ja["job_id"] for ja in recovered}
            for ja in wf_state.get("job_analyses", []):
                if ja["job_id"] not in recovered_ids:
                    recovered.append(ja)
            wf_state["job_analyses"] = recovered
            log.info(
                "  Recovered %d analyses from %d comment part(s) for %s",
                len(recovered), 1 + len(overflow_comments), workflow_file,
            )
    wf_state["overflow_ids"] = [c["id"] for c in overflow_comments]

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
        log.info("  Cross-job analysis (%d jobs, agent=%s)...", len(all_analyses), use_agent)
        cross = cross_job_analysis(
            None, workflow_file, all_analyses,
            use_agent=use_agent, repo_path=agent_repo_path,
        )

    parts = render_workflow_comment_parts(
        workflow_file, all_analyses, pending_info, cross,
        use_agent=use_agent,
    )
    wf_state["last_pending_count"] = total_pending

    main_id = wf_state.get("comment_id") if wf_state.get("owned") else None
    if main_id:
        try:
            update_comment(token, bot_repo, main_id, parts[0])
            log.info("  Updated main comment %d for %s (part 1/%d)",
                     main_id, workflow_file, len(parts))
        except Exception as exc:
            log.warning(
                "  Main comment %d update failed (%s); posting fresh main",
                main_id, exc,
            )
            main_id = None
    if not main_id:
        resp = post_comment(token, bot_repo, issue_num, parts[0])
        main_id = resp["id"]
        wf_state["comment_id"] = main_id
        wf_state["owned"] = True
        log.info("  Posted main comment %d for %s (part 1/%d)",
                 main_id, workflow_file, len(parts))

    old_overflow_ids = list(wf_state.get("overflow_ids") or [])
    new_overflow_ids: list[int] = []
    for i, body in enumerate(parts[1:], start=1):
        if i - 1 < len(old_overflow_ids):
            cid = old_overflow_ids[i - 1]
            try:
                update_comment(token, bot_repo, cid, body)
                new_overflow_ids.append(cid)
                log.info("  Updated overflow comment %d for %s (part %d/%d)",
                         cid, workflow_file, i + 1, len(parts))
                continue
            except Exception as exc:
                log.warning(
                    "  Overflow comment %d update failed (%s); reposting",
                    cid, exc,
                )
                try:
                    delete_comment(token, bot_repo, cid)
                except Exception:
                    pass
        resp = post_comment(token, bot_repo, issue_num, body)
        new_overflow_ids.append(resp["id"])
        log.info("  Posted overflow comment %d for %s (part %d/%d)",
                 resp["id"], workflow_file, i + 1, len(parts))

    for leftover_id in old_overflow_ids[len(parts) - 1:]:
        try:
            delete_comment(token, bot_repo, leftover_id)
            log.info("  Deleted stale overflow comment %d for %s",
                     leftover_id, workflow_file)
        except Exception as exc:
            log.warning(
                "  Failed to delete stale overflow comment %d (%s)",
                leftover_id, exc,
            )

    wf_state["overflow_ids"] = new_overflow_ids

    if workflow_file in SCHEDULE_ONLY_WORKFLOWS:
        try:
            maybe_publish_cross_run_summary(
                token, bot_repo, issue_num, workflow_file,
                wf_state["job_analyses"], wf_state, gh_comments,
                use_agent=use_agent, agent_repo_path=agent_repo_path,
            )
        except Exception as exc:
            log.warning("  Cross-run summary publish failed for %s: %s",
                        workflow_file, exc)


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
    use_agent: bool = True,
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
                event="schedule",
            )

            if not new_analyses:
                continue

            total_reports += 1

            if output == "stdout":
                cross = ""
                if len(new_analyses) > 1:
                    cross = cross_job_analysis(
                        None, wf, new_analyses,
                        use_agent=use_agent, repo_path=agent_repo_path,
                    )
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
                    agent_repo_path=agent_repo_path,
                )

            save_state(state)

        except Exception as e:
            log.error("Error monitoring %s: %s", wf, e)
            traceback.print_exc()

    save_state(state)

    if (
        output == "daily-issue"
        and bot_repo
        and total_reports > 0
        and os.environ.get("BUILD_DAILY_BOARD", "true").lower() not in ("false", "0", "no")
    ):
        try:
            from build_daily_status_board import build_and_publish_board
            log.info(
                "Building daily cross-workflow status board (%d workflow(s) updated)",
                total_reports,
            )
            build_and_publish_board(token, bot_repo, use_agent=use_agent)
        except Exception as exc:
            log.warning("Daily status board build failed: %s", exc)
            traceback.print_exc()

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
        "--use-agent", action=argparse.BooleanOptionalAction,
        default=os.environ.get("USE_AGENT", "").lower() not in ("false", "0", "no"),
        help="Use Claude Code agent (default: enabled, use --no-use-agent to disable)",
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
