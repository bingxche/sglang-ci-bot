#!/usr/bin/env python3
"""Build the Daily Status Board comment for today's CI monitor issue.

Aggregates per-job analyses from ALL monitored workflows into a single
top-of-issue rolling comment, replacing the old per-workflow ``cross-run``
narrative-style comments with one cross-workflow status board.

The board is identified in the daily issue by an HTML marker:

    <!-- ci-monitor-daily-status-board -->

On each invocation:

  - Find today's daily issue (created earlier by ``ensure_daily_issue.py``
    or ``monitor_ci.py``).
  - Read every per-workflow comment posted by ``monitor_ci.py`` and
    reconstruct the per-job analyses via ``parse_job_analyses_from_comment``.
  - Optionally pull yesterday's board comment for trend / NEW-cluster
    detection.
  - Run the agent (``Task: Daily Status Board``) which reads the
    methodology in ``agent/CLAUDE.md`` and produces a Markdown report.
  - PATCH the existing board comment if present, otherwise POST a new one.

Output methodology (cluster IDs, confidence labels, no-priority rule,
in-flight-fix lookup, completed-runs-only filter) lives entirely in
``agent/CLAUDE.md`` under ``## Daily Cross-Workflow Status Board``.
This script is a data-only harness.
"""

import argparse
import logging
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor_ci import (
    MONITORED_WORKFLOWS,
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
    ensure_sglang_repo,
    load_prompt_template,
    post_comment,
    update_comment,
)

log = logging.getLogger("daily-status-board")

DAILY_BOARD_MARKER = "<!-- ci-monitor-daily-status-board -->"
DEFAULT_BOARD_TIMEOUT = 1200
DEFAULT_AGENT_MAX_TURNS = 200


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


def find_board_comment(comments: list[dict]) -> dict | None:
    """Find an existing daily status board comment by its HTML marker."""
    for c in reversed(comments):
        if DAILY_BOARD_MARKER in c.get("body", ""):
            return c
    return None


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


def fetch_yesterday_board(
    token: str, bot_repo: str, today_str: str,
) -> str | None:
    """Best-effort fetch of yesterday's status board for trend / NEW detection.

    Returns the body text (without the marker line), or None if yesterday's
    issue or board comment cannot be found. Errors are logged and swallowed
    so that today's board build never fails because of yesterday lookup.
    """
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        yesterday = today - timedelta(days=1)
        y_str = yesterday.strftime("%Y-%m-%d")
        y_issue = find_daily_issue(token, bot_repo, y_str)
        if not y_issue:
            log.info("No daily issue for %s; trend section will be sparse", y_str)
            return None
        y_comments = get_issue_comments(token, bot_repo, y_issue)
        y_board = find_board_comment(y_comments)
        if not y_board:
            log.info("No board comment in yesterday's issue #%d", y_issue)
            return None
        return y_board.get("body", "")
    except Exception as exc:
        log.warning("Failed to fetch yesterday's board (%s); continuing without", exc)
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
    """Compose the data-only prompt for ``Task: Daily Status Board``.

    Methodology and output format live in ``agent/CLAUDE.md``. This prompt
    only routes the task and points to the context files written into
    ``.ci-context/`` by ``claude_code_analyze``.
    """
    yesterday_line = (
        "Yesterday's board: .ci-context/yesterday-board.md (for NEW vs carry-over detection)\n"
        if has_yesterday_context
        else "Yesterday's board: not available (treat all clusters as carry-over baseline)\n"
    )
    return (
        f"Task: Daily Status Board\n"
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
    """Invoke the Claude Code agent in the sglang repo to produce the board."""
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
        context_files["yesterday-board.md"] = yesterday_block

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
                os.environ.get("DAILY_BOARD_TIMEOUT_SECS", str(DEFAULT_BOARD_TIMEOUT))
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
            yesterday_block if yesterday_block else "(no prior board available)"
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
# Comment publishing
# ---------------------------------------------------------------------------

def render_board_body(snapshot_utc: str, agent_text: str, used_agent: bool) -> str:
    """Wrap the agent output with the marker + footer."""
    method = "Claude Code CLI" if used_agent else "Claude API"
    body = agent_text.strip()
    if body.startswith(DAILY_BOARD_MARKER):
        body = body[len(DAILY_BOARD_MARKER):].lstrip()
    return (
        f"{DAILY_BOARD_MARKER}\n"
        f"{body}\n"
        f"\n---\n"
        f"*Generated by amd-bot using {method} (last updated: {snapshot_utc})*\n"
    )


def publish_board(
    token: str, bot_repo: str, issue_num: int, body: str,
) -> int:
    """PATCH existing board comment in place, or POST a new one."""
    comments = get_issue_comments(token, bot_repo, issue_num)
    existing = find_board_comment(comments)
    if existing:
        update_comment(token, bot_repo, existing["id"], body)
        log.info("Updated daily board comment #%d", existing["id"])
        return existing["id"]
    resp = post_comment(token, bot_repo, issue_num, body)
    log.info("Posted new daily board comment #%d", resp["id"])
    return resp["id"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_and_publish_board(
    token: str,
    bot_repo: str,
    use_agent: bool = True,
    date_str: str | None = None,
) -> int | None:
    """Build today's daily status board comment.

    Returns the comment ID on success, or None if there was nothing to
    aggregate (no per-workflow comments yet) or the daily issue does
    not exist for today. Errors during agent / API calls are logged
    and swallowed so this never breaks the caller (typically
    ``monitor_ci.run_oneshot``).
    """
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    issue_num = find_daily_issue(token, bot_repo, date_str)
    if not issue_num:
        log.info("No daily issue for %s yet; skipping board build", date_str)
        return None

    log.info(
        "Building daily status board for issue #%d (%s, snapshot %s)",
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
        log.info("No per-workflow failures yet; skipping board build")
        return None

    workflows_block = build_workflows_block(wf_analyses)
    yesterday_block = fetch_yesterday_board(token, bot_repo, date_str)

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
        log.info("Falling back to API mode for board synthesis")
        agent_text = run_api_fallback(
            workflows_block, yesterday_block,
            date_str, snapshot_utc, issue_num,
        )

    if not agent_text:
        log.error("Both agent and API failed; cannot publish board")
        return None

    body = render_board_body(snapshot_utc, agent_text, used_agent)
    try:
        return publish_board(token, bot_repo, issue_num, body)
    except Exception as exc:
        log.error("Failed to publish board comment (%s)", exc)
        traceback.print_exc()
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the daily cross-workflow status board comment",
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

    cid = build_and_publish_board(
        args.github_token, args.bot_repo,
        use_agent=args.use_agent,
        date_str=args.date,
    )
    if cid is None:
        log.info("Daily status board not updated (see logs above)")
        return 0
    log.info("Daily status board comment id=%d", cid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
