#!/usr/bin/env python3
"""Build the Daily Cross-Workflow Summary pinned in the CI monitor issue body.

Aggregates per-job analyses from ALL monitored workflows into a single
**Daily Cross-Workflow Summary**, written DIRECTLY into the daily issue's
body (not as a comment) so it appears pinned at the very top of the
issue — above every per-workflow comment. It answers, in 5 seconds,
"Is CI healthy today, and what should I do?".

The summary lives between two placeholder markers in the issue body:

    <!-- daily-cross-workflow-summary:start -->
    ...rendered summary...
    <!-- daily-cross-workflow-summary:end -->

(``ensure_daily_issue.py`` / ``monitor_ci.find_or_create_daily_issue``
seed the placeholder block when the issue is first created.)

On each invocation:

  - Find today's daily issue (created earlier by ``ensure_daily_issue.py``
    or ``monitor_ci.py``).
  - Read every per-workflow comment posted by ``monitor_ci.py`` and
    reconstruct the per-job analyses via ``parse_job_analyses_from_comment``.
  - Best-effort fetch yesterday's summary (from yesterday's issue body, or
    the legacy summary comment for issues that pre-date the body-pinning
    move) for trend / NEW-cluster detection.
  - Run the agent (``Task: Daily Cross-Workflow Summary``) which reads the
    methodology in ``agent/tasks/daily-cross-workflow-summary.md`` and produces a Markdown report.
  - PATCH the issue body, replacing the content between the placeholder
    markers (matching the legacy ``ci-monitor-daily-status-board`` markers
    too, so older issues migrate in place). If the markers are missing,
    seed a fresh body and preserve the legacy content as a tail section.
  - Delete any legacy summary comments left behind by the pre-body code
    path so the summary doesn't appear twice.

Output methodology (cluster IDs, confidence labels, no-priority rule,
in-flight-fix lookup, completed-runs-only filter) lives entirely in
``agent/tasks/daily-cross-workflow-summary.md``.
This script is a data-only harness.
"""

import argparse
import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor_ci import (
    DAILY_SUMMARY_PLACEHOLDER_END,
    DAILY_SUMMARY_PLACEHOLDER_START,
    MONITORED_WORKFLOWS,
    _DAILY_SUMMARY_ANCHOR_RE,
    _extract_report,
    _initial_issue_body,
    find_daily_issue,
    find_workflow_comment_parts,
    get_issue_comments,
    parse_job_analyses_from_comment,
)
from utils import (
    REPO,
    CLAUDE_MODEL,
    agent_failure_notice,
    claude_code_analyze,
    claude_code_available,
    create_anthropic_client,
    delete_comment,
    ensure_sglang_repo,
    get_issue,
    load_prompt_template,
    update_issue_body,
)

log = logging.getLogger("daily-summary")

# Marker written at the top of the in-body summary content. Old daily issues
# (pre body-pinning) carried the summary as a *comment* with the legacy
# marker below; that legacy value is matched for cleanup / yesterday-lookup.
SUMMARY_CONTENT_MARKER = "<!-- daily-cross-workflow-summary -->"
LEGACY_SUMMARY_COMMENT_MARKER = "<!-- ci-monitor-daily-status-board -->"

DEFAULT_SUMMARY_TIMEOUT = 1200
DEFAULT_AGENT_MAX_TURNS = 200

# Matches the placeholder block under BOTH the new and the legacy marker
# pair, so daily issues created before this rename migrate in place on the
# next update (we always re-write the new markers).
_SUMMARY_BLOCK_RE = re.compile(
    r"<!-- (?:daily-cross-workflow-summary|ci-monitor-daily-status-board):start -->"
    r".*?"
    r"<!-- (?:daily-cross-workflow-summary|ci-monitor-daily-status-board):end -->",
    re.DOTALL,
)

# Tops a summary kept from an earlier run because the agent failed; the
# notice under it is a blockquote, so it ends at the first non-">" line.
AGENT_FAILED_MARKER = "<!-- daily-summary-agent-failed -->"
_AGENT_FAILED_RE = re.compile(re.escape(AGENT_FAILED_MARKER) + r"\n(?:>[^\n]*(?:\n|$))*\n*")


# ---------------------------------------------------------------------------
# Per-workflow context collection
# ---------------------------------------------------------------------------

def collect_workflow_analyses(
    token: str, bot_repo: str, issue_num: int,
) -> dict[str, list[dict]]:
    """Reconstruct per-job analyses for every monitored workflow.

    Reads all comments on the daily issue and groups them by workflow.
    Each workflow's main + overflow comments are concatenated and parsed
    via ``parse_job_analyses_from_comment`` (the inverse of the renderer
    in ``monitor_ci.render_workflow_comment_parts``).

    Returns a dict ``{workflow_file: [job_analysis, ...]}``. A workflow
    with no comment yet maps to an empty list.
    """
    comments = get_issue_comments(token, bot_repo, issue_num)
    by_workflow: dict[str, list[dict]] = {}
    for wf in MONITORED_WORKFLOWS:
        main, overflow = find_workflow_comment_parts(comments, wf)
        if not main:
            by_workflow[wf] = []
            continue
        combined = main["body"] + "\n" + "\n".join(c["body"] for c in overflow)
        by_workflow[wf] = parse_job_analyses_from_comment(combined)
    return by_workflow


def find_legacy_summary_comment(comments: list[dict]) -> dict | None:
    """Find a legacy Daily Summary *comment* by its HTML marker.

    Kept only for backwards compatibility and yesterday-summary lookup.
    The current code path writes the summary into the issue body
    (see ``publish_summary``), not as a comment.
    """
    for c in reversed(comments):
        if LEGACY_SUMMARY_COMMENT_MARKER in c.get("body", ""):
            return c
    return None


def extract_summary_from_body(body: str) -> str | None:
    """Pull the rendered summary content out of the issue body.

    Returns the text between the start/end placeholders (inclusive of
    inner content but excluding the markers themselves). Returns
    ``None`` if the markers are missing or the block is still the
    initial placeholder copy.
    """
    m = _SUMMARY_BLOCK_RE.search(body or "")
    if not m:
        return None
    inner = m.group(0)
    # Strip whichever start/end marker pair matched.
    inner = re.sub(r"^<!-- (?:daily-cross-workflow-summary|ci-monitor-daily-status-board):start -->", "", inner)
    inner = re.sub(r"<!-- (?:daily-cross-workflow-summary|ci-monitor-daily-status-board):end -->$", "", inner)
    return inner.strip() or None


def build_workflows_block(wf_analyses: dict[str, list[dict]]) -> str:
    """Render the per-workflow context that the agent reads.

    The block contains, for each workflow, a short header (failure count,
    job names) followed by the verbatim per-job analysis text recovered
    from the daily issue's comments. The agent uses this as the source
    of truth for clustering / Hypothesised Causes / In-flight Fix Check.
    """
    lines: list[str] = []
    for wf, jas in wf_analyses.items():
        if not jas:
            lines.append(f"## Workflow: `{wf}`")
            lines.append("- (no failures reported in today's lookback window)")
            lines.append("")
            continue
        lines.append(f"## Workflow: `{wf}`")
        lines.append(f"- {len(jas)} failed job(s) in today's daily issue")
        for ja in jas:
            started = ja.get("started_at") or "N/A"
            lines.append(
                f"  - **{ja.get('job_name', '?')}** "
                f"(job_id `{ja.get('job_id', '?')}`, "
                f"started {started[:16] if started != 'N/A' else 'N/A'})"
            )
        lines.append("")
        lines.append(f"### Per-job analyses for `{wf}`")
        for ja in jas:
            lines.append(f"#### Job: {ja.get('job_name', '?')}")
            lines.append(f"**Job ID:** `{ja.get('job_id', '?')}`")
            lines.append(f"**Run:** {ja.get('run_url', '?')}")
            steps = ja.get("failed_steps") or []
            if steps:
                lines.append(f"**Failed steps:** {', '.join(steps)}")
            lines.append("")
            analysis = (ja.get("analysis") or "").strip()
            if analysis:
                lines.append(analysis)
            else:
                lines.append("_(no per-job analysis text recovered)_")
            lines.append("")
    return "\n".join(lines)


def fetch_yesterday_summary(
    token: str, bot_repo: str, today_str: str,
) -> str | None:
    """Best-effort fetch of yesterday's summary for trend / NEW detection.

    Looks first in the issue body (current location) and falls back to
    the legacy summary comment for issues created before the body-pinning
    change. Returns the rendered summary text or ``None`` if nothing
    usable is found. Errors are swallowed so today's summary build never
    fails because of yesterday lookup.
    """
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        yesterday = today - timedelta(days=1)
        y_str = yesterday.strftime("%Y-%m-%d")
        y_issue = find_daily_issue(token, bot_repo, y_str)
        if not y_issue:
            log.info("No daily issue for %s; trend section will be sparse", y_str)
            return None
        try:
            y_meta = get_issue(token, bot_repo, y_issue)
            summary_inner = extract_summary_from_body(y_meta.get("body", ""))
            if summary_inner:
                return summary_inner
        except Exception as exc:
            log.warning("Could not fetch yesterday issue body (%s)", exc)
        y_comments = get_issue_comments(token, bot_repo, y_issue)
        y_summary = find_legacy_summary_comment(y_comments)
        if not y_summary:
            log.info("No summary content in yesterday's issue #%d", y_issue)
            return None
        return y_summary.get("body", "")
    except Exception as exc:
        log.warning("Failed to fetch yesterday's summary (%s); continuing without", exc)
        return None


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

def build_agent_prompt(
    date_str: str,
    snapshot_utc: str,
    issue_num: int,
    monitored_workflows: list[str],
    has_yesterday_context: bool,
) -> str:
    """Compose the data-only prompt for ``Task: Daily Cross-Workflow Summary``.

    Methodology and output format live in ``agent/tasks/daily-cross-workflow-summary.md``. This prompt
    only routes the task and points to the context files written into
    ``.ci-context/`` by ``claude_code_analyze``.
    """
    yesterday_line = (
        "Yesterday's summary: .ci-context/yesterday-summary.md (for NEW vs carry-over detection)\n"
        if has_yesterday_context
        else "Yesterday's summary: not available (treat all clusters as carry-over baseline)\n"
    )
    return (
        f"Task: Daily Cross-Workflow Summary\n"
        f"Date: {date_str}\n"
        f"Snapshot UTC: {snapshot_utc}\n"
        f"Issue: #{issue_num}\n"
        f"Monitored workflows: {', '.join(monitored_workflows)}\n"
        f"Per-workflow analyses: .ci-context/per-workflow-analyses.md\n"
        f"{yesterday_line}"
        f"Source: current directory\n"
        f"GitHub API token: $GH_PAT"
    )


def run_agent(
    workflows_block: str,
    yesterday_block: str | None,
    date_str: str,
    snapshot_utc: str,
    issue_num: int,
    monitored_workflows: list[str],
) -> str:
    """Invoke the Claude Code agent in the sglang repo to produce the summary.

    Raises when the agent cannot run or returns nothing.
    """
    if not claude_code_available():
        raise RuntimeError("Claude Code CLI not found on the runner")
    repo_path = ensure_sglang_repo()

    context_files: dict[str, str] = {
        "per-workflow-analyses.md": workflows_block,
    }
    if yesterday_block:
        context_files["yesterday-summary.md"] = yesterday_block

    prompt = build_agent_prompt(
        date_str, snapshot_utc, issue_num,
        monitored_workflows,
        has_yesterday_context=bool(yesterday_block),
    )
    text = claude_code_analyze(
        prompt=prompt,
        work_dir=repo_path,
        context_files=context_files,
        max_turns=int(
            os.environ.get("AGENT_MAX_TURNS", str(DEFAULT_AGENT_MAX_TURNS))
        ),
        timeout_secs=int(
            os.environ.get("DAILY_SUMMARY_TIMEOUT_SECS", str(DEFAULT_SUMMARY_TIMEOUT))
        ),
    )
    if not text.strip():
        raise RuntimeError("the agent returned no output")
    return text


def run_api(
    workflows_block: str,
    yesterday_block: str | None,
    date_str: str,
    snapshot_utc: str,
    issue_num: int,
) -> str | None:
    """Single-shot Claude API call using the ``daily-cross-workflow-summary`` template.

    API mode only (``USE_AGENT=false``); never a fallback for a failed agent.
    """
    template = load_prompt_template("daily-cross-workflow-summary")
    if not template:
        log.error("daily-cross-workflow-summary template not found in agent/api-prompts.md")
        return None

    prompt = template.format(
        date_str=date_str,
        snapshot_utc=snapshot_utc,
        issue_number=issue_num,
        workflows_block=workflows_block,
        yesterday_clusters_summary_or_none=(
            yesterday_block if yesterday_block else "(no prior summary available)"
        ),
    )

    try:
        client = create_anthropic_client()
    except Exception as exc:
        log.error("Could not create Anthropic client (%s)", exc)
        return None

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
        return "\n".join(b.text for b in msg.content if b.type == "text")
    except Exception as exc:
        log.error("API call failed (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# Job -> cluster map
# ---------------------------------------------------------------------------

# The agent ends its report with a fenced ```json job-clusters``` block
# mapping each cluster ID to the job IDs it covers. It is stripped from the
# rendered summary and re-emitted as a hidden comment so the dashboard can
# join jobs to clusters without scraping the prose tables, whose columns
# drift from day to day.
JOB_CLUSTERS_COMMENT_PREFIX = "<!-- ci-monitor-job-clusters:v1 "
UNCLUSTERED_SECTION_MAX_CHARS = 8_000
# GitHub rejects issue bodies over 65,536 chars; leave room for the footer.
ISSUE_BODY_SAFE_CHARS = 62_000
_JOB_CLUSTERS_BLOCK_RE = re.compile(
    r"^```json[ \t]+job-clusters[ \t]*\n(.*?)^```[ \t]*\n?",
    re.DOTALL | re.MULTILINE,
)
_JOB_LINK_RE = re.compile(r"/actions/runs/\d+/job/(\d+)")
# `R25`, but not the model names `DeepSeek-R1` or `R1-MXFP4`.
_CLUSTER_ID_RE = re.compile(r"(?<![\w-])R\d+(?![\w-])")
_MD_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
_HOW_GENERATED_RE = re.compile(r"^##\s+How this report is generated", re.MULTILINE)


def split_job_cluster_block(report: str) -> tuple[str, dict[int, list[str]] | None]:
    """Pull the agent's ``job-clusters`` block out of the report.

    Returns the report without the block and a ``{job_id: [cluster_id]}``
    map, or ``None`` when the block is missing or not valid JSON. The
    agent writes it cluster-first (``{"R25": [job_id, ...]}``) because
    that is how it thinks about the day; it is inverted here.
    """
    matches = list(_JOB_CLUSTERS_BLOCK_RE.finditer(report))
    if not matches:
        return report, None
    stripped = _JOB_CLUSTERS_BLOCK_RE.sub("", report).rstrip() + "\n"
    try:
        raw = json.loads(matches[-1].group(1))
    except ValueError as exc:
        log.warning("job-clusters block is not valid JSON (%s); ignoring it", exc)
        return stripped, None
    if not isinstance(raw, dict):
        log.warning("job-clusters block is not a JSON object; ignoring it")
        return stripped, None

    by_job: dict[int, list[str]] = {}
    for cluster_id, job_ids in raw.items():
        if not _CLUSTER_ID_RE.fullmatch(str(cluster_id)) or not isinstance(job_ids, list):
            continue
        for job_id in job_ids:
            try:
                job_id = int(job_id)
            except (TypeError, ValueError):
                continue
            ids = by_job.setdefault(job_id, [])
            if cluster_id not in ids:
                ids.append(cluster_id)
    return stripped, by_job


def job_clusters_from_markdown(report: str) -> dict[int, list[str]]:
    """Best-effort job -> cluster map read from the report's own tables.

    Fallback for jobs the agent left out of its ``job-clusters`` block. A
    table row's job links belong to the cluster IDs written in that row
    (the ``ID`` column, or a ``Cluster`` cell in a drill-down); a row
    without any belongs to the ``###`` cluster section it sits under. Job
    links outside a cluster section (TL;DR, run labels) are not assigned.
    """
    by_job: dict[int, list[str]] = {}
    section_ids: list[str] = []
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            section_ids = _CLUSTER_ID_RE.findall(_MD_LINK_TARGET_RE.sub("]", stripped))
            continue
        if stripped.startswith(("## ", "# ", "<details")):
            section_ids = []
            continue
        job_ids = _JOB_LINK_RE.findall(stripped)
        if not job_ids:
            continue
        ids = section_ids
        if stripped.startswith("|"):
            ids = _CLUSTER_ID_RE.findall(_MD_LINK_TARGET_RE.sub("]", stripped)) or section_ids
        for job_id in job_ids:
            entry = by_job.setdefault(int(job_id), [])
            entry.extend(i for i in dict.fromkeys(ids) if i not in entry)
    return {job_id: ids for job_id, ids in by_job.items() if ids}


def _per_job_cluster_name(analysis: str) -> str:
    """The first line under a per-job analysis's ``### Failure Cluster``."""
    lines = (analysis or "").splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^#{2,4}\s+Failure Cluster\b", line.strip()):
            for follow in lines[i + 1:]:
                text = follow.strip()
                if text.startswith("#"):
                    break
                if text:
                    return text
    return ""


def _job_url(ja: dict) -> str:
    run_url = (ja.get("run_url") or "").rstrip("/")
    return f"{run_url}/job/{ja['job_id']}" if run_url else ""


def _table_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_unclustered_section(
    unclustered: list[tuple[str, dict]], max_chars: int = UNCLUSTERED_SECTION_MAX_CHARS,
) -> str:
    """List the analysed jobs the summary did not put in any cluster.

    Jobs sharing a workflow and per-job cluster name share a row. Rows stop
    at *max_chars* so a bad day cannot push the issue body past GitHub's
    65,536-character limit; the hidden map still lists every job.
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for wf, ja in unclustered:
        cluster = _table_cell(_per_job_cluster_name(ja.get("analysis", ""))) or "—"
        groups.setdefault((wf.removesuffix(".yml"), cluster), []).append(ja)

    lines = [
        "<details><summary><b>⚠️ Not yet in a cluster</b> · "
        f"{len(unclustered)} analysed failure(s) this summary did not assign "
        "· click to expand</summary>",
        "",
        "| Workflow | Per-job failure cluster | Jobs |",
        "|---|---|---|",
    ]
    size = sum(len(line) + 1 for line in lines)
    shown = 0
    for (wf, cluster), jas in groups.items():
        row = f"| {wf} | {cluster} | "
        jobs = []
        for ja in jas:
            name = _table_cell(ja.get("job_name") or str(ja["job_id"]))
            url = _job_url(ja)
            job = f"[{name}]({url})" if url else name
            if size + len(row) + len(" · ".join(jobs + [job])) + 3 > max_chars:
                break
            jobs.append(job)
        if not jobs:
            break
        row += " · ".join(jobs) + " |"
        lines.append(row)
        size += len(row) + 1
        shown += len(jobs)
        if len(jobs) < len(jas):
            break
    if shown < len(unclustered):
        lines += [
            "",
            f"…and {len(unclustered) - shown} more; see the per-workflow comments below.",
        ]
    lines += ["", "</details>"]
    return "\n".join(lines)


def build_job_cluster_map(
    report: str,
    agent_map: dict[int, list[str]] | None,
    wf_analyses: dict[str, list[dict]],
) -> tuple[dict[int, list[str]], list[tuple[str, dict]]]:
    """Join the report's clusters to every job analysed today.

    The agent's block wins; the report's tables fill in what it left out.
    A job ID is kept when it was analysed today or is linked from the
    report (yesterday's late-finishing legs are), so a mistyped ID cannot
    land on an unrelated job. Returns the map and the analysed jobs that
    no cluster covers, in workflow order.
    """
    analysed = {
        ja["job_id"]: (wf, ja) for wf, jas in wf_analyses.items() for ja in jas
    }
    linked = {int(j) for j in _JOB_LINK_RE.findall(report)}

    mapping: dict[int, list[str]] = {}
    for source in (agent_map or {}, job_clusters_from_markdown(report)):
        for job_id, ids in source.items():
            if job_id in mapping or not ids:
                continue
            if job_id in analysed or job_id in linked:
                mapping[job_id] = ids

    unclustered = [entry for job_id, entry in analysed.items() if job_id not in mapping]
    return mapping, unclustered


def parse_job_cluster_comment(body: str) -> dict[str, list[int]] | None:
    """Read the ``clusters`` map back out of a published summary."""
    start = (body or "").rfind(JOB_CLUSTERS_COMMENT_PREFIX)
    if start < 0:
        return None
    end = body.find(" -->", start)
    try:
        payload = json.loads(body[start + len(JOB_CLUSTERS_COMMENT_PREFIX):end])
    except ValueError:
        return None
    clusters = payload.get("clusters") if isinstance(payload, dict) else None
    return clusters if isinstance(clusters, dict) else None


def render_job_cluster_comment(
    mapping: dict[int, list[str]], unclustered: list[tuple[str, dict]],
) -> str:
    """Hidden, machine-readable copy of the job -> cluster join."""
    clusters: dict[str, list[int]] = {}
    for job_id in sorted(mapping):
        for cluster_id in mapping[job_id]:
            clusters.setdefault(cluster_id, []).append(job_id)
    payload = {
        "clusters": dict(sorted(clusters.items(), key=lambda kv: int(kv[0][1:]))),
        "unclustered": sorted(ja["job_id"] for _, ja in unclustered),
    }
    return f"{JOB_CLUSTERS_COMMENT_PREFIX}{json.dumps(payload, separators=(',', ':'))} -->"


def attach_job_cluster_map(report: str, wf_analyses: dict[str, list[dict]]) -> str:
    """Strip the agent's block, list unassigned jobs, and embed the map."""
    report, agent_map = split_job_cluster_block(report)
    mapping, unclustered = build_job_cluster_map(report, agent_map, wf_analyses)
    analysed = sum(len(jas) for jas in wf_analyses.values())
    log.info(
        "Job->cluster map: %d/%d analysed job(s) assigned (agent block %s), %d unassigned",
        analysed - len(unclustered), analysed,
        "present" if agent_map is not None else "missing", len(unclustered),
    )

    report = report.rstrip()
    comment = render_job_cluster_comment(mapping, unclustered)
    room = ISSUE_BODY_SAFE_CHARS - len(report) - len(comment)
    if unclustered and room > 500:
        section = render_unclustered_section(
            unclustered, min(UNCLUSTERED_SECTION_MAX_CHARS, room)
        )
        m = _HOW_GENERATED_RE.search(report)
        if m:
            report = f"{report[:m.start()]}{section}\n\n{report[m.start():]}"
        else:
            report = f"{report}\n\n{section}"
    return f"{report}\n\n{comment}"


# ---------------------------------------------------------------------------
# Suspect PRs
# ---------------------------------------------------------------------------

# The agent also ends its report with a fenced ```json suspect-prs``` block:
# one entry per cluster it attributes to a merged sglang PR. It is stripped
# from the rendered summary and kept, validated, as a hidden comment that
# ``suspect_pr_comments.py`` reads in the ``finalize`` job. Deciding whether
# to comment on the PR is left to that script's checks, not to the agent.
SUSPECT_PRS_COMMENT_PREFIX = "<!-- ci-monitor-suspect-prs:v1 "
CONFIDENCE_LABELS = ("FACT", "HIGH", "MEDIUM", "LOW", "SPECULATION")
SUSPECT_EVIDENCE_MAX_CHARS = 400
_SUSPECT_PRS_BLOCK_RE = re.compile(
    r"^```json[ \t]+suspect-prs[ \t]*\n(.*?)^```[ \t]*\n?",
    re.DOTALL | re.MULTILINE,
)
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")


def _sha(value) -> str | None:
    value = str(value or "").strip().lower()
    return value if _SHA_RE.fullmatch(value) else None


def _pr_number(value) -> int | None:
    try:
        number = int(str(value).lstrip("#"))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def normalize_suspect(entry) -> dict | None:
    """Validate one ``suspect-prs`` entry; ``None`` if it is unusable."""
    if not isinstance(entry, dict):
        return None
    cluster = str(entry.get("cluster") or "").strip()
    pr = _pr_number(entry.get("pr"))
    sha = _sha(entry.get("sha"))
    confidence = str(entry.get("confidence") or "").strip().strip("[]`").upper()
    if not _CLUSTER_ID_RE.fullmatch(cluster) or not pr or not sha:
        return None
    if confidence not in CONFIDENCE_LABELS:
        return None
    evidence = " ".join(str(entry.get("evidence") or "").split())
    # It is embedded in an HTML comment, so it must not close one.
    evidence = evidence.replace("-->", "->")[:SUSPECT_EVIDENCE_MAX_CHARS]
    related = entry.get("related_prs") if isinstance(entry.get("related_prs"), list) else []
    return {
        "cluster": cluster,
        "pr": pr,
        "sha": sha,
        "confidence": confidence,
        "evidence": evidence,
        "last_pass_sha": _sha(entry.get("last_pass_sha")),
        "first_fail_sha": _sha(entry.get("first_fail_sha")),
        "fix_pr": _pr_number(entry.get("fix_pr")) if entry.get("fix_pr") else None,
        "related_prs": [n for n in map(_pr_number, related) if n and n != pr],
    }


def split_suspect_prs_block(report: str) -> tuple[str, list[dict] | None]:
    """Pull the agent's ``suspect-prs`` block out of the report.

    Returns the report without the block and the valid entries, or ``None``
    when the block is missing or not a JSON list. A missing block only
    means no PR gets a comment.
    """
    matches = list(_SUSPECT_PRS_BLOCK_RE.finditer(report))
    if not matches:
        return report, None
    stripped = _SUSPECT_PRS_BLOCK_RE.sub("", report).rstrip() + "\n"
    try:
        raw = json.loads(matches[-1].group(1))
    except ValueError as exc:
        log.warning("suspect-prs block is not valid JSON (%s); ignoring it", exc)
        return stripped, None
    if not isinstance(raw, list):
        log.warning("suspect-prs block is not a JSON list; ignoring it")
        return stripped, None
    suspects = [s for s in map(normalize_suspect, raw) if s]
    if len(suspects) < len(raw):
        log.warning("Dropped %d invalid suspect-prs entr(ies)", len(raw) - len(suspects))
    return stripped, suspects


def render_suspect_prs_comment(suspects: list[dict]) -> str:
    return f"{SUSPECT_PRS_COMMENT_PREFIX}{json.dumps(suspects, separators=(',', ':'))} -->"


def parse_suspect_prs_comment(body: str) -> list[dict]:
    """Read the suspect PRs back out of a published summary."""
    start = (body or "").rfind(SUSPECT_PRS_COMMENT_PREFIX)
    if start < 0:
        return []
    end = body.find(" -->", start)
    try:
        raw = json.loads(body[start + len(SUSPECT_PRS_COMMENT_PREFIX):end])
    except ValueError:
        return []
    return [s for s in map(normalize_suspect, raw) if s] if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def render_summary_body(
    snapshot_utc: str,
    agent_text: str,
    used_agent: bool,
    wf_analyses: dict[str, list[dict]] | None = None,
) -> str:
    """Wrap the agent output with the marker + footer.

    Defends against two LLM output failure modes:

    1. *Preamble*: scratchpad prose ("Now I have all the data, let me
       compose the final report.") emitted before the first heading.
       Stripped via ``_strip_llm_preamble``.
    2. *Multiple drafts*: the model writing the entire
       ``# Daily Cross-Workflow Summary …`` report several times in a row,
       drafting → critiquing → re-writing — with every draft retained in
       stdout. Discarded via ``_keep_last_section`` keyed on the
       ``# Daily Cross-Workflow Summary`` anchor, leaving only the last draft.

    With *wf_analyses*, the agent's ``job-clusters`` block is replaced by
    the hidden job -> cluster map (see ``attach_job_cluster_map``). The
    agent's ``suspect-prs`` block is always replaced by its hidden copy.
    """
    method = "Claude Code CLI" if used_agent else "Claude API"
    body = agent_text.strip()
    if body.startswith(SUMMARY_CONTENT_MARKER):
        body = body[len(SUMMARY_CONTENT_MARKER):].lstrip()
    body = _extract_report(body, _DAILY_SUMMARY_ANCHOR_RE)
    body, suspects = split_suspect_prs_block(body)
    if wf_analyses is not None:
        body = attach_job_cluster_map(body, wf_analyses)
    if suspects:
        body = f"{body.rstrip()}\n{render_suspect_prs_comment(suspects)}"
    return (
        f"{SUMMARY_CONTENT_MARKER}\n"
        f"{body}\n"
        f"\n---\n"
        f"*Generated by amd-bot using {method} (last updated: {snapshot_utc})*\n"
    )


def publish_summary(
    token: str,
    bot_repo: str,
    issue_num: int,
    body: str,
    date_str: str,
) -> int:
    """Replace the Daily Cross-Workflow Summary placeholder block in the body.

    Reads the current issue body, swaps the content between the summary
    placeholder markers (new or legacy) with the freshly rendered summary,
    and PATCHes the issue. Returns the issue number on success.

    Falls back to seeding a fresh body using ``_initial_issue_body``
    if the placeholder markers are missing (e.g. an issue created
    before this change shipped) so the summary still ends up at the top.
    """
    try:
        issue = get_issue(token, bot_repo, issue_num)
    except Exception as exc:
        log.error("Could not fetch issue #%d to PATCH body (%s)", issue_num, exc)
        raise

    current = issue.get("body", "") or ""

    block = (
        f"{DAILY_SUMMARY_PLACEHOLDER_START}\n"
        f"{body.strip()}\n"
        f"{DAILY_SUMMARY_PLACEHOLDER_END}"
    )

    if _SUMMARY_BLOCK_RE.search(current):
        new_body = _SUMMARY_BLOCK_RE.sub(lambda _m: block, current, count=1)
    else:
        log.info(
            "Issue #%d has no summary placeholder; seeding fresh body and "
            "preserving existing content as a tail section",
            issue_num,
        )
        seeded = _initial_issue_body(date_str)
        new_body = _SUMMARY_BLOCK_RE.sub(lambda _m: block, seeded, count=1)
        if current.strip():
            new_body = (
                new_body.rstrip()
                + "\n\n---\n\n"
                + "<!-- legacy issue body preserved below -->\n\n"
                + current.strip()
                + "\n"
            )

    body_changed = new_body != current
    if body_changed:
        update_issue_body(token, bot_repo, issue_num, new_body)
        log.info("Updated Daily Cross-Workflow Summary (issue body) for issue #%d", issue_num)
    else:
        log.info("Daily Cross-Workflow Summary unchanged for issue #%d; skipping PATCH",
                 issue_num)

    _cleanup_legacy_summary_comments(token, bot_repo, issue_num)
    return issue_num


def _cleanup_legacy_summary_comments(
    token: str, bot_repo: str, issue_num: int,
) -> None:
    """Delete any legacy summary comments that pre-date the body-pinning move.

    Issues created before the daily summary moved into the issue body have
    a comment with the legacy marker that would otherwise show stale data
    alongside the new in-body summary. Best-effort cleanup — failures are
    logged but never raised, so a flaky DELETE call cannot break the build.
    """
    try:
        comments = get_issue_comments(token, bot_repo, issue_num)
    except Exception as exc:
        log.warning("Could not list comments for legacy-summary cleanup (%s)", exc)
        return
    legacy = [c for c in comments if LEGACY_SUMMARY_COMMENT_MARKER in c.get("body", "")]
    for c in legacy:
        try:
            delete_comment(token, bot_repo, c["id"])
            log.info("Deleted legacy summary comment #%d", c["id"])
        except Exception as exc:
            log.warning("Failed to delete legacy summary comment #%d (%s)",
                        c["id"], exc)


def mark_summary_stale(
    token: str, bot_repo: str, issue_num: int, date_str: str, reason: object,
) -> None:
    """Keep the last good summary in the body, topped by an "agent failed" notice.

    A notice from an earlier failure is replaced, not stacked; the next
    successful build renders a fresh body without it.
    """
    try:
        current = get_issue(token, bot_repo, issue_num).get("body", "")
        previous = _AGENT_FAILED_RE.sub("", extract_summary_from_body(current) or "").strip()
        notice = agent_failure_notice("the refreshed Daily Cross-Workflow Summary", reason)
        if previous:
            notice += "\n> The summary below is from the last successful run."
        publish_summary(
            token, bot_repo, issue_num,
            f"{AGENT_FAILED_MARKER}\n{notice}\n\n{previous}", date_str,
        )
    except Exception as exc:
        log.error("Could not mark the summary stale (%s)", exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_and_publish_summary(
    token: str,
    bot_repo: str,
    use_agent: bool = True,
    date_str: str | None = None,
) -> int | None:
    """Build today's Daily Cross-Workflow Summary and PATCH it into the body.

    The rendered summary is written between the
    ``<!-- daily-cross-workflow-summary:start -->`` /
    ``...:end -->`` markers in the daily issue's body, so it appears
    pinned at the very top of the issue (above all per-workflow
    comments).

    Returns the issue number on success, or ``None`` if there was
    nothing to aggregate (no per-workflow comments yet) or the daily
    issue does not exist for today. If the agent fails, the last good
    summary is kept under an "agent did not work" notice and the error
    is re-raised, so the caller (typically ``monitor_ci.run_oneshot``)
    can fail its job.
    """
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    issue_num = find_daily_issue(token, bot_repo, date_str)
    if not issue_num:
        log.info("No daily issue for %s yet; skipping summary build", date_str)
        return None

    log.info(
        "Building Daily Cross-Workflow Summary for issue #%d (%s, snapshot %s)",
        issue_num, date_str, snapshot_utc,
    )

    wf_analyses = collect_workflow_analyses(token, bot_repo, issue_num)
    total = sum(len(v) for v in wf_analyses.values())
    workflows_with_failures = [wf for wf, v in wf_analyses.items() if v]
    log.info(
        "Aggregated %d failure(s) across %d/%d workflow(s)",
        total, len(workflows_with_failures), len(MONITORED_WORKFLOWS),
    )
    if total == 0:
        log.info("No per-workflow failures yet; skipping summary build")
        return None

    workflows_block = build_workflows_block(wf_analyses)
    yesterday_block = fetch_yesterday_summary(token, bot_repo, date_str)

    if use_agent:
        try:
            agent_text = run_agent(
                workflows_block, yesterday_block,
                date_str, snapshot_utc, issue_num,
                MONITORED_WORKFLOWS,
            )
        except Exception as exc:
            # No API fallback: keep the last good summary and say why it
            # was not refreshed.
            log.error("Summary agent failed (%s); not falling back to the API", exc)
            mark_summary_stale(token, bot_repo, issue_num, date_str, exc)
            raise
    else:
        agent_text = run_api(
            workflows_block, yesterday_block,
            date_str, snapshot_utc, issue_num,
        )
        if not agent_text:
            log.error("API call failed; cannot publish summary")
            return None

    body = render_summary_body(snapshot_utc, agent_text, use_agent, wf_analyses)
    try:
        return publish_summary(token, bot_repo, issue_num, body, date_str)
    except Exception as exc:
        log.error("Failed to publish summary to issue body (%s)", exc)
        traceback.print_exc()
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the Daily Cross-Workflow Summary "
            "(written into the issue body so it stays pinned at the top)"
        ),
    )
    parser.add_argument(
        "--bot-repo", required=True,
        help="Bot repo, e.g. bingxche/sglang-ci-bot",
    )
    parser.add_argument(
        "--date",
        help="Daily issue date (YYYY-MM-DD UTC). Defaults to today.",
    )
    parser.add_argument(
        "--use-agent", action=argparse.BooleanOptionalAction,
        default=os.environ.get("USE_AGENT", "").lower() not in ("false", "0", "no"),
        help="Use Claude Code agent (default: enabled, --no-use-agent to disable)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get(
            "BOT_PAT",
            os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
        ),
        help="GitHub token (default: BOT_PAT / GH_PAT / GITHUB_TOKEN)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        stream=sys.stdout,
    )

    if not args.github_token:
        log.error("GitHub token required. Set GH_PAT / BOT_PAT / GITHUB_TOKEN.")
        return 1

    if not args.use_agent and not os.environ.get("LLM_GATEWAY_KEY"):
        log.error(
            "API fallback requested but LLM_GATEWAY_KEY is not set. "
            "Set --use-agent or provide LLM_GATEWAY_KEY."
        )
        return 1

    try:
        issue_id = build_and_publish_summary(
            args.github_token, args.bot_repo,
            use_agent=args.use_agent,
            date_str=args.date,
        )
    except Exception as exc:
        log.error("Daily Cross-Workflow Summary not refreshed: %s", exc)
        return 1
    if issue_id is None:
        log.info("Daily Cross-Workflow Summary not updated (see logs above)")
        return 0
    log.info("Daily Cross-Workflow Summary pinned in issue #%d body", issue_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
