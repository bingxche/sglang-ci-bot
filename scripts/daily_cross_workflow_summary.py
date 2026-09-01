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
    methodology in ``agent/CLAUDE.md`` and produces a Markdown report.
  - PATCH the issue body, replacing the content between the placeholder
    markers (matching the legacy ``ci-monitor-daily-status-board`` markers
    too, so older issues migrate in place). If the markers are missing,
    seed a fresh body and preserve the legacy content as a tail section.
  - Delete any legacy summary comments left behind by the pre-body code
    path so the summary doesn't appear twice.

Output methodology (cluster IDs, confidence labels, no-priority rule,
in-flight-fix lookup, completed-runs-only filter) lives entirely in
``agent/CLAUDE.md`` under ``## Daily Cross-Workflow Summary``.
This script is a data-only harness.
"""

import argparse
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

    Methodology and output format live in ``agent/CLAUDE.md``. This prompt
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
) -> str | None:
    """Invoke the Claude Code agent in the sglang repo to produce the summary."""
    if not claude_code_available():
        log.info("Claude Code CLI unavailable; agent mode skipped")
        return None
    try:
        repo_path = ensure_sglang_repo()
    except Exception as exc:
        log.warning("Could not prepare sglang repo (%s); agent mode skipped", exc)
        return None

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
    try:
        return claude_code_analyze(
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
    except Exception as exc:
        log.warning("Agent run failed (%s)", exc)
        return None


def run_api_fallback(
    workflows_block: str,
    yesterday_block: str | None,
    date_str: str,
    snapshot_utc: str,
    issue_num: int,
) -> str | None:
    """Single-shot Claude API call using the ``daily-cross-workflow-summary`` template."""
    template = load_prompt_template("daily-cross-workflow-summary")
    if not template:
        log.error("daily-cross-workflow-summary template not found in CLAUDE.md")
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
        log.error("API fallback failed (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def render_summary_body(snapshot_utc: str, agent_text: str, used_agent: bool) -> str:
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
    """
    method = "Claude Code CLI" if used_agent else "Claude API"
    body = agent_text.strip()
    if body.startswith(SUMMARY_CONTENT_MARKER):
        body = body[len(SUMMARY_CONTENT_MARKER):].lstrip()
    body = _extract_report(body, _DAILY_SUMMARY_ANCHOR_RE)
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
    issue does not exist for today. Errors during agent / API calls
    are logged and swallowed so this never breaks the caller
    (typically ``monitor_ci.run_oneshot``).
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

    agent_text: str | None = None
    used_agent = False
    if use_agent:
        agent_text = run_agent(
            workflows_block, yesterday_block,
            date_str, snapshot_utc, issue_num,
            MONITORED_WORKFLOWS,
        )
        used_agent = bool(agent_text)

    if not agent_text:
        log.info("Falling back to API mode for summary synthesis")
        agent_text = run_api_fallback(
            workflows_block, yesterday_block,
            date_str, snapshot_utc, issue_num,
        )

    if not agent_text:
        log.error("Both agent and API failed; cannot publish summary")
        return None

    body = render_summary_body(snapshot_utc, agent_text, used_agent)
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

    issue_id = build_and_publish_summary(
        args.github_token, args.bot_repo,
        use_agent=args.use_agent,
        date_str=args.date,
    )
    if issue_id is None:
        log.info("Daily Cross-Workflow Summary not updated (see logs above)")
        return 0
    log.info("Daily Cross-Workflow Summary pinned in issue #%d body", issue_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
