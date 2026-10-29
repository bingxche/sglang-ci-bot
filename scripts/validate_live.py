#!/usr/bin/env python3
"""Run one bot use case on a runner without writing anything to GitHub.

Every non-GET request through ``requests`` is refused, so reports are printed
instead of posted. Used to validate a branch on a runner container:

    python scripts/validate_live.py monitor|crossrun|daily|tracker|staging|cistatus|suspect|review
"""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

_request = requests.Session.request


def _read_only(self, method, url, *args, **kwargs):
    if method.upper() not in ("GET", "HEAD"):
        raise RuntimeError(f"validate_live: blocked {method} {url}")
    return _request(self, method, url, *args, **kwargs)


requests.Session.request = _read_only

sys.path.insert(0, str(Path(__file__).resolve().parent))

BOT_REPO = "bingxche/sglang-ci-bot"
TOKEN = os.environ.get("GH_PAT") or os.environ.get("BOT_PAT") or ""
DATE = os.environ.get("VALIDATE_DATE") or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def monitor():
    import monitor_ci
    wf = os.environ.get("VALIDATE_WORKFLOW", "nightly-amd-mi355x-disagg.yml")
    hours = int(os.environ.get("VALIDATE_HOURS", "24"))
    failed = monitor_ci.run_oneshot(TOKEN, None, "stdout", [wf], hours, "main", use_agent=True,
                                    job_name_filter=os.environ.get("VALIDATE_JOB_FILTER") or None)
    print("FAILED WORKFLOWS:", failed)


def crossrun():
    """Cross-Run Pattern Analysis on today's analyses (it only runs in the publishing path)."""
    import daily_cross_workflow_summary as s
    import monitor_ci as m
    from utils import claude_code_analyze, ensure_sglang_repo
    wf = os.environ.get("VALIDATE_WORKFLOW", "pr-test-amd.yml")
    issue = s.find_daily_issue(TOKEN, BOT_REPO, DATE)
    analyses = s.collect_workflow_analyses(TOKEN, BOT_REPO, issue).get(wf, [])
    runs = m._runs_from_analyses(analyses)
    print(f"{wf}: {len(analyses)} analyses in {len(runs)} runs")
    if len(runs) < 2:
        return
    patterns = m._compute_cross_run_patterns(runs)
    text = claude_code_analyze(
        prompt=m._build_cross_run_prompt(wf, runs, patterns, analyses),
        work_dir=ensure_sglang_repo(),
        context_files={"per-job-analyses.md": m._format_per_job_dump(analyses)},
        max_turns=150, timeout_secs=600,
    )
    print(m._render_cross_run_summary(wf, runs, patterns, agent_text=text, use_agent=True))


def daily():
    import daily_cross_workflow_summary as s
    issue = s.find_daily_issue(TOKEN, BOT_REPO, DATE)
    analyses = s.collect_workflow_analyses(TOKEN, BOT_REPO, issue)
    print(f"issue #{issue}: {sum(len(v) for v in analyses.values())} analyses")
    snapshot = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = s.run_agent(
        s.build_workflows_block(analyses), s.fetch_yesterday_summary(TOKEN, BOT_REPO, DATE),
        DATE, snapshot, issue, s.MONITORED_WORKFLOWS,
    )
    print(s.render_summary_body(snapshot, text, True, analyses))
    print("SUSPECTS:", s.parse_suspect_prs_comment(s.render_summary_body(snapshot, text, True, analyses)))


def tracker():
    import failure_tracker as t
    from monitor_ci import find_daily_issue
    issue = find_daily_issue(TOKEN, BOT_REPO, DATE)
    analyses = t.collect_tracked_analyses(TOKEN, BOT_REPO, issue)
    print(f"issue #{issue}: {sum(len(v) for v in analyses.values())} tracked analyses")
    rows = t.run_agent_extraction(analyses)
    print("AGENT ROWS:", None if rows is None else len(rows))
    for r in rows or []:
        print(" ", r)


def staging():
    import stage_notion
    out = Path("/tmp/val/staging-candidates.json")
    result = stage_notion.analyze(TOKEN, BOT_REPO, int(os.environ.get("VALIDATE_LOOKBACK", "3")), out)
    print(result)


def cistatus():
    import check_ci_for_pr
    check_ci_for_pr.check_ci_for_pr(TOKEN, int(os.environ.get("VALIDATE_PR", "47179")), post_comment_flag=False)


def suspect():
    import suspect_pr_comments as spc
    from daily_cross_workflow_summary import (
        collect_workflow_analyses, extract_summary_from_body, parse_job_cluster_comment,
        parse_suspect_prs_comment,
    )
    from monitor_ci import find_daily_issue
    from utils import get_issue
    issue = find_daily_issue(TOKEN, BOT_REPO, DATE)
    summary = extract_summary_from_body(get_issue(TOKEN, BOT_REPO, issue).get("body", ""))
    suspects = parse_suspect_prs_comment(summary or "")
    clusters = parse_job_cluster_comment(summary or "") or {}
    job_info = {ja["job_id"]: ja for jas in collect_workflow_analyses(TOKEN, BOT_REPO, issue).values() for ja in jas}
    decisions = spc.decide_all(suspects, clusters, job_info, spc.GitHub(TOKEN), datetime.now(timezone.utc))
    # Suspects already commented on pass every check; verify those too, to see
    # whether the comments already posted would have survived the vote.
    want = [int(p) for p in os.environ.get("VALIDATE_SUSPECT_PRS", "").split(",") if p]
    picked = [d for d in decisions if d.result in ("post", "already") and (not want or d.suspect["pr"] in want)]
    for d in picked[: int(os.environ.get("VALIDATE_MAX_SUSPECTS", "2"))]:
        d.result = "post"
        d.full_sha = d.full_sha or spc.GitHub(TOKEN).full_sha(d.suspect["sha"]) or d.suspect["sha"]
        print(f"=== #{d.suspect['pr']} ({d.suspect['cluster']}, {d.suspect['confidence']}, {len(d.jobs)} jobs)")
        spc.verify_decisions([d], lambda x: spc.agent_verify(x, job_info), {})
        print("RESULT:", d.result, d.reason)
        print("VOTES:", d.votes)


def review():
    import review_pr
    review_pr.review_pr(TOKEN, int(os.environ.get("VALIDATE_PR", "47179")), post_comment_flag=False)


CASES = {f.__name__: f for f in (monitor, crossrun, daily, tracker, staging, cistatus, suspect, review)}

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        level=logging.INFO, stream=sys.stdout)
    CASES[sys.argv[1]]()
