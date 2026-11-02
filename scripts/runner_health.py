#!/usr/bin/env python3
"""Check that the monitored workflows' jobs are getting runners.

A workflow whose self-hosted runners are offline has no failed jobs, so the
failure reports never mention it. Its jobs sit queued, its run stays
unfinished, and under ``cancel-in-progress: false`` every newer scheduled run
is cancelled before it starts. ``nightly-amd-mi355x-disagg.yml`` went four days
like that (both ``linux-sglang-mi35x-di`` runners offline from 2026-10-29)
without a word in the daily report.

For each monitored workflow this finds:
  - jobs waiting for a runner for ``QUEUE_STALL_HOURS``, or for
    ``OFFLINE_GRACE_MINUTES`` while every runner with their labels is offline;
  - runs cancelled in the lookback window before any job got a runner;
and writes a "Runner health" section at the top of today's daily issue body,
between ``RUNNER_HEALTH_START`` / ``RUNNER_HEALTH_END``. Code only, no agent.

Runner online/offline status needs a token that can list sgl-project/sglang's
runners (``RUNNER_STATUS_TOKEN``). Without one, only queue times are used.

Env:
  GH_PAT               - GitHub token (fallback: BOT_PAT, GITHUB_TOKEN)
  RUNNER_STATUS_TOKEN  - optional token for the runner list (fallback: GH_PAT)
  BOT_REPO             - e.g. ``bingxche/sglang-ci-bot``
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor_ci import (
    MONITORED_WORKFLOWS,
    UNFINISHED_RUN_STATUSES,
    find_daily_issue,
    workflow_event_filter,
)
from utils import REPO, get_issue, get_run_jobs, gh_get, gh_headers, update_issue_body

log = logging.getLogger("runner-health")

# MI35x jobs can queue for most of a day when the runners are busy, so a
# queue alone is reported only after this long.
QUEUE_STALL_HOURS = 24
# Every runner for the job's labels offline: reported once the job has waited
# this long, to ride out a runner restart.
OFFLINE_GRACE_MINUTES = 30
# Unfinished runs this old are still checked; a run stuck on a dead runner
# outlives the monitor's MAX_RUN_AGE_HOURS window.
LOOKBACK_DAYS = 14

RUNNER_HEALTH_START = "<!-- ci-monitor-runner-health:start -->"
RUNNER_HEALTH_END = "<!-- ci-monitor-runner-health:end -->"
_SECTION_RE = re.compile(
    re.escape(RUNNER_HEALTH_START) + r".*?" + re.escape(RUNNER_HEALTH_END) + r"\n*",
    re.DOTALL,
)

_HOSTED_LABEL_RE = re.compile(r"^(ubuntu|windows|macos)-", re.IGNORECASE)


# ---------------------------------------------------------------------------
# GitHub reads
# ---------------------------------------------------------------------------

def list_runners(token: str) -> list[dict] | None:
    """Every repo-level runner of sglang, or None when the token cannot list them."""
    url = f"https://api.github.com/repos/{REPO}/actions/runners"
    runners: list[dict] = []
    page = 1
    while True:
        resp = gh_get(url, headers=gh_headers(token), params={"per_page": 100, "page": page})
        if resp.status_code in (401, 403, 404):
            log.warning("Cannot list %s runners (HTTP %d); using queue times only",
                        REPO, resp.status_code)
            return None
        resp.raise_for_status()
        batch = resp.json().get("runners", [])
        runners += batch
        if len(batch) < 100:
            return runners
        page += 1


def get_runs(token: str, workflow_file: str, status: str, created_since: datetime,
             event: str | None = None) -> list[dict]:
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow_file}/runs"
    params = {
        "status": status,
        "per_page": 100,
        "created": f">={created_since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    }
    if event:
        params["event"] = event
    resp = gh_get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()
    return resp.json().get("workflow_runs", [])


# ---------------------------------------------------------------------------
# Decisions (pure)
# ---------------------------------------------------------------------------

def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def is_hosted(labels: list[str]) -> bool:
    """True for jobs on GitHub-hosted runners (ubuntu-latest, ...)."""
    return bool(labels) and all(_HOSTED_LABEL_RE.match(l) for l in labels)


def runner_status(labels: list[str], runners: list[dict] | None) -> dict | None:
    """Online / busy / offline counts of the runners that can take a job with *labels*.

    A runner matches when it carries every label. An autoscaled (ARC) runner
    set registers label-less runners named ``<set>-...``, so a single-label
    job also matches those; such a set scales to zero when idle, so having no
    online runner says nothing and ``autoscaled`` is set. None when the
    runner list is unknown.
    """
    if runners is None:
        return None
    want = {l.lower() for l in labels}
    matched = [
        r for r in runners
        if want and want <= {l["name"].lower() for l in r.get("labels", [])}
    ]
    autoscaled = False
    if not matched and len(labels) == 1:
        prefix = f"{labels[0].lower()}-"
        matched = [
            r for r in runners
            if not r.get("labels") and r.get("name", "").lower().startswith(prefix)
        ]
        autoscaled = True
    online = [r for r in matched if r.get("status") == "online"]
    return {
        "total": len(matched),
        "online": len(online),
        "busy": sum(1 for r in online if r.get("busy")),
        "offline": sorted(r["name"] for r in matched if r.get("status") != "online"),
        "autoscaled": autoscaled,
    }


def all_offline(status: dict | None) -> bool:
    """Every runner that could take the job is registered but offline."""
    return bool(status) and not status["autoscaled"] and status["total"] > 0 and status["online"] == 0


def is_stalled(waited: timedelta, status: dict | None) -> bool:
    if waited >= timedelta(hours=QUEUE_STALL_HOURS):
        return True
    return all_offline(status) and waited >= timedelta(minutes=OFFLINE_GRACE_MINUTES)


def never_started(jobs: list[dict]) -> bool:
    """No job of the run got a runner, or none of its self-hosted jobs did.

    A hosted ``setup`` job that ran does not count: the run's real work is on
    self-hosted runners, and they never picked it up.
    """
    self_hosted = [j for j in jobs if not is_hosted(j.get("labels") or [])]
    return not any(j.get("runner_name") for j in (self_hosted or jobs))


# ---------------------------------------------------------------------------
# Per-workflow check
# ---------------------------------------------------------------------------

def check_workflow(token: str, workflow_file: str, runners: list[dict] | None,
                   now: datetime, hours_back: int = 24) -> dict:
    """Stalled job groups and never-started cancelled runs of one workflow.

    Runs are filtered by event like the failure reports
    (``workflow_event_filter``): PR runs of pr-test-amd.yml queue for
    reasons of their own, and a stuck manual run that blocks the scheduled
    ones still shows, as scheduled runs cancelled before they started.
    """
    created_since = now - timedelta(days=LOOKBACK_DAYS)
    event = workflow_event_filter(workflow_file)
    unfinished: dict[int, dict] = {}
    for status in UNFINISHED_RUN_STATUSES:
        for run in get_runs(token, workflow_file, status, created_since, event=event):
            if run.get("status") != "completed":
                unfinished[run["id"]] = run

    groups: dict[tuple[str, ...], dict] = {}
    for run in sorted(unfinished.values(), key=lambda r: r.get("created_at") or ""):
        for job in get_run_jobs(token, run["id"]):
            if job.get("status") != "queued" or job.get("runner_name"):
                continue
            labels = sorted(job.get("labels") or [])
            if is_hosted(labels):
                continue
            queued_at = _parse_time(job.get("created_at")) or _parse_time(run.get("created_at"))
            if queued_at is None:
                continue
            key = tuple(labels)
            if key not in groups:
                groups[key] = {
                    "labels": labels,
                    "runners": runner_status(labels, runners),
                    "jobs": 0,
                    "queued_since": queued_at,
                    "runs": [],
                }
            g = groups[key]
            if not is_stalled(now - queued_at, g["runners"]):
                continue
            g["queued_since"] = min(g["queued_since"], queued_at) if g["jobs"] else queued_at
            g["jobs"] += 1
            if run["html_url"] not in [r["url"] for r in g["runs"]]:
                g["runs"].append({"url": run["html_url"], "created_at": run.get("created_at", "")})

    cancelled = []
    finished_since = now - timedelta(hours=hours_back)
    for run in get_runs(token, workflow_file, "cancelled", created_since, event=event):
        updated = _parse_time(run.get("updated_at"))
        if updated is None or updated < finished_since:
            continue
        if never_started(get_run_jobs(token, run["id"])):
            cancelled.append({"url": run["html_url"], "created_at": run.get("created_at", "")})

    return {
        "workflow": workflow_file,
        "stalled": [g for g in groups.values() if g["jobs"]],
        "cancelled": sorted(cancelled, key=lambda r: r["created_at"]),
    }


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

def _duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"


def _run_link(run: dict) -> str:
    return f"[{run['created_at'][:10] or 'run'}]({run['url']})"


def _runners_cell(status: dict | None) -> str:
    if status is None:
        return "not readable"
    if status["total"] == 0:
        return "none registered with these labels"
    text = f"{status['online']} of {status['total']} online, {status['busy']} busy"
    if all_offline(status):
        text = f"**{text}**"
    if status["autoscaled"]:
        text += " (autoscaled set)"
    if status["offline"] and status["online"] == 0:
        text += "; offline: " + ", ".join(f"`{n}`" for n in status["offline"][:4])
        if len(status["offline"]) > 4:
            text += f" +{len(status['offline']) - 4}"
    return text


def render_section(results: list[dict], now: datetime, runners_known: bool,
                   hours_back: int = 24) -> str:
    """The Runner health section, markers included."""
    checked = now.strftime("%Y-%m-%d %H:%M UTC")
    stalled = [(r["workflow"], g) for r in results for g in r["stalled"]]
    cancelled = [(r["workflow"], r["cancelled"]) for r in results if r["cancelled"]]

    if not stalled and not cancelled:
        body = (
            f"✅ **Runner health**: no job of a monitored workflow is stuck waiting "
            f"for a runner, and no run was cancelled before it started in the last "
            f"{hours_back} h. <sub>Checked {checked}.</sub>"
        )
        return f"{RUNNER_HEALTH_START}\n{body}\n{RUNNER_HEALTH_END}\n\n"

    lines = [
        "## ⚠️ Runner health",
        "",
        "These workflows are not getting runners, so they have no failures for the "
        "reports below to analyse.",
        "",
    ]
    if stalled:
        lines += [
            "| Workflow | Runner labels | Jobs waiting | Waiting since | Runners | Runs |",
            "|---|---|---|---|---|---|",
        ]
        for wf, g in stalled:
            since = g["queued_since"]
            lines.append(
                f"| `{wf}` "
                f"| {', '.join(f'`{l}`' for l in g['labels']) or '—'} "
                f"| {g['jobs']} "
                f"| {since.strftime('%Y-%m-%d %H:%M')} UTC ({_duration(now - since)}) "
                f"| {_runners_cell(g['runners'])} "
                f"| {', '.join(_run_link(r) for r in g['runs'])} |"
            )
        lines.append("")
    if cancelled:
        lines.append(f"**Runs cancelled before any job got a runner** (last {hours_back} h):")
        for wf, runs in cancelled:
            lines.append(f"- `{wf}`: {', '.join(_run_link(r) for r in runs)}")
        lines.append("")
    status_note = (
        "Runner status is from sglang's runner list."
        if runners_known else
        "Runner status is not readable with the bot's token (set `RUNNER_STATUS_TOKEN`), "
        "so only queue times are used."
    )
    lines.append(
        f"<sub>Checked {checked} by code. A job is listed after waiting "
        f"{QUEUE_STALL_HOURS} h for a runner, or {OFFLINE_GRACE_MINUTES} min while every "
        f"runner with its labels is offline. {status_note}</sub>"
    )
    return f"{RUNNER_HEALTH_START}\n" + "\n".join(lines) + f"\n{RUNNER_HEALTH_END}\n\n"


def replace_section(body: str, section: str) -> str:
    """*body* with its Runner health section replaced, or *section* put on top."""
    if _SECTION_RE.search(body):
        return _SECTION_RE.sub(lambda _m: section, body, count=1)
    return section + body


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", choices=["stdout", "daily-issue"], default="stdout")
    parser.add_argument("--bot-repo", default=os.environ.get("BOT_REPO"))
    parser.add_argument("--workflows", nargs="*", default=MONITORED_WORKFLOWS)
    parser.add_argument("--hours-back", type=int, default=24)
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
        stream=sys.stdout,
    )
    token = (
        os.environ.get("GH_PAT")
        or os.environ.get("BOT_PAT")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not token:
        print("ERROR: GH_PAT (or GITHUB_TOKEN) must be set", file=sys.stderr)
        return 1
    if args.output == "daily-issue" and not args.bot_repo:
        print("ERROR: --bot-repo (or BOT_REPO) is required for daily-issue", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    runners = list_runners(os.environ.get("RUNNER_STATUS_TOKEN") or token)
    results = []
    for wf in args.workflows:
        result = check_workflow(token, wf, runners, now, hours_back=args.hours_back)
        for g in result["stalled"]:
            log.warning("%s: %d job(s) on %s waiting since %s", wf, g["jobs"],
                        ",".join(g["labels"]), g["queued_since"].isoformat())
        if result["cancelled"]:
            log.warning("%s: %d run(s) cancelled before starting", wf, len(result["cancelled"]))
        results.append(result)

    section = render_section(results, now, runners is not None, hours_back=args.hours_back)
    if args.output == "stdout":
        print(section)
        return 0

    date_str = now.strftime("%Y-%m-%d")
    num = find_daily_issue(token, args.bot_repo, date_str)
    if num is None:
        log.info("No open daily issue for %s; nothing to update", date_str)
        return 0
    current = get_issue(token, args.bot_repo, num).get("body") or ""
    new_body = replace_section(current, section)
    if new_body != current:
        update_issue_body(token, args.bot_repo, num, new_body)
        log.info("Updated the Runner health section of issue #%d", num)
    return 0


if __name__ == "__main__":
    sys.exit(main())
