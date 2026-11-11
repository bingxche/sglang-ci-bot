#!/usr/bin/env python3
"""
Re-run failed CI jobs that hit a transient infrastructure error.

Called by ``monitor_ci.monitor_workflow`` for every failed run it looks at.
A failed job counts as transient when the tail of its log, just before the
step's ``##[error]`` line, matches one of ``TRANSIENT_PATTERNS`` (network
resets, DNS, timeouts, upstream 5xx) and shows no assertion failure. Only
the failed jobs are re-run, never the whole workflow:

- every failed job is transient: ``POST /runs/{id}/rerun-failed-jobs``
- exactly one of several failed jobs is transient: ``POST /jobs/{id}/rerun``
- otherwise nothing is re-run

A run is re-run at most once (first attempt only), only for ``schedule``
runs, and only while the run finished less than ``RERUN_WINDOW_HOURS`` ago.
Every decision is kept in the monitor state, keyed by run and attempt, so a
run's logs are read once.

Modes (``AUTO_RERUN``): ``on`` (default), ``dry-run``, ``off``. ``on``
needs ``AUTO_RERUN_TOKEN``, a token with Actions write access on sglang
(amd-bot only has read access there); without it ``on`` acts as ``dry-run``.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from utils import REPO, download_job_logs, gh_headers

log = logging.getLogger("ci-monitor")

MODES = ("on", "dry-run", "off")
DEFAULT_MODE = "on"

# Re-run only the first attempt, so a run is re-run at most once.
MAX_RUN_ATTEMPT = 1
RERUN_WINDOW_HOURS = 6
# Lines before the last ``##[error]`` line that are searched for a cause.
TAIL_LINES = 400
STATE_RETENTION_DAYS = 3

TRANSIENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("HTTP/2 stream reset", re.compile(r"stream error: stream ID \d+; INTERNAL_ERROR")),
    ("connection reset", re.compile(r"[Cc]onnection reset by peer")),
    ("DNS failure", re.compile(
        r"Could not resolve host|Temporary failure in name resolution"
    )),
    ("network timeout", re.compile(
        r"TLS handshake timeout|i/o timeout|Read timed out|"
        r"Connection timed out|Operation timed out after \d+ milliseconds"
    )),
    ("git transfer error", re.compile(
        r"RPC failed; curl|fetch-pack: unexpected disconnect|"
        r"early EOF|fatal: unable to access 'https://"
    )),
    ("upstream 5xx", re.compile(
        r"\b(?:502 Bad Gateway|503 Service Unavailable|504 Gateway Time-?out)\b"
    )),
    ("download cut off", re.compile(
        r"(?:failed to (?:copy|fetch|do request)|httpReadSeeker).*unexpected EOF"
    )),
]

# The job ran its checks and they failed: re-running would not help.
NOT_TRANSIENT_RE = re.compile(r"AssertionError|FAILED \(failures=")

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")


def resolve_mode(mode: str | None, rerun_token: str) -> str:
    mode = (mode or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        log.warning("Unknown AUTO_RERUN=%r; using %s", mode, DEFAULT_MODE)
        mode = DEFAULT_MODE
    if mode == "on" and not rerun_token:
        log.warning("AUTO_RERUN=on but AUTO_RERUN_TOKEN is not set; dry run only")
        mode = "dry-run"
    return mode


def error_tail(raw_log: str, lines: int = TAIL_LINES) -> str:
    """The *lines* log lines before the last ``##[error]``, timestamps removed."""
    all_lines = [_TIMESTAMP_RE.sub("", ln) for ln in raw_log.split("\n")]
    end = len(all_lines)
    for i in range(len(all_lines) - 1, -1, -1):
        if all_lines[i].startswith("##[error]"):
            end = i + 1
            break
    return "\n".join(all_lines[max(0, end - lines):end])


def transient_cause(raw_log: str) -> str | None:
    """Return "<label>: <matching line>" if the log ends in a transient error."""
    tail = error_tail(raw_log)
    if NOT_TRANSIENT_RE.search(tail):
        return None
    for label, pattern in TRANSIENT_PATTERNS:
        m = pattern.search(tail)
        if m:
            start = tail.rfind("\n", 0, m.start()) + 1
            end = tail.find("\n", m.end())
            line = tail[start:end if end != -1 else None].strip()
            return f"{label}: {line[:200]}"
    return None


def _run_finished_at(run: dict) -> datetime | None:
    ts = run.get("updated_at")
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def skip_reason(run: dict, now: datetime | None = None) -> str | None:
    """Why *run* is not eligible for an automatic re-run, or None."""
    now = now or datetime.now(timezone.utc)
    if run.get("status") != "completed" or run.get("conclusion") != "failure":
        return "run not finished as failure"
    if run.get("event") != "schedule":
        return f"{run.get('event')} run"
    if (run.get("run_attempt") or 1) > MAX_RUN_ATTEMPT:
        return f"already on attempt {run.get('run_attempt')}"
    finished = _run_finished_at(run)
    if finished and now - finished > timedelta(hours=RERUN_WINDOW_HOURS):
        return f"finished more than {RERUN_WINDOW_HOURS}h ago"
    return None


def _post(url: str, token: str) -> None:
    resp = requests.post(url, headers=gh_headers(token), json={})
    if resp.status_code not in (201, 204):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")


def prune_records(records: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=STATE_RETENTION_DAYS)).isoformat()
    for key in [k for k, r in records.items() if r.get("at", "") < cutoff]:
        del records[key]


def consider_run(
    token: str,
    run: dict,
    failed_jobs: list[dict],
    workflow_file: str,
    records: dict,
    mode: str,
    rerun_token: str = "",
) -> dict | None:
    """Re-run *run*'s transient failed jobs if it qualifies; return the record.

    *failed_jobs* are the run's failed non-gate jobs. *records* is the
    persistent decision log, keyed ``"<run_id>:<attempt>"``; a run already
    in it is not looked at again.
    """
    if mode == "off" or not failed_jobs:
        return None
    key = f"{run['id']}:{run.get('run_attempt') or 1}"
    if key in records:
        return records[key]

    record = {
        "workflow": workflow_file,
        "run_id": run["id"],
        "run_url": run.get("html_url", ""),
        "attempt": run.get("run_attempt") or 1,
        "at": datetime.now(timezone.utc).isoformat(),
        "jobs": [],
    }
    reason = skip_reason(run)
    if reason:
        # Not stored: an in-progress run becomes eligible once it finishes.
        log.info("    Auto-rerun: skip run %d (%s)", run["id"], reason)
        return None

    transient: list[dict] = []
    for job in failed_jobs:
        if job.get("conclusion") != "failure":
            continue
        cause = transient_cause(download_job_logs(token, job["id"]))
        if cause:
            transient.append({"id": job["id"], "name": job["name"], "cause": cause})

    record["jobs"] = transient
    if not transient:
        record["decision"] = "skip"
        record["reason"] = "no transient failure"
    elif len(transient) == len(failed_jobs):
        record["decision"] = "rerun-failed-jobs"
    elif len(transient) == 1:
        record["decision"] = "rerun-job"
    else:
        record["decision"] = "skip"
        record["reason"] = (
            f"{len(transient)} of {len(failed_jobs)} failed jobs are transient"
        )

    if record["decision"] != "skip":
        if mode == "dry-run":
            record["result"] = "dry-run"
        else:
            if record["decision"] == "rerun-failed-jobs":
                url = f"https://api.github.com/repos/{REPO}/actions/runs/{run['id']}/rerun-failed-jobs"
            else:
                url = f"https://api.github.com/repos/{REPO}/actions/jobs/{transient[0]['id']}/rerun"
            try:
                _post(url, rerun_token)
                record["result"] = "rerun"
            except Exception as exc:
                record["result"] = "error"
                record["reason"] = " ".join(str(exc).split())[:200]

    log.info(
        "    Auto-rerun: run %d -> %s %s %s",
        run["id"], record["decision"], record.get("result", ""),
        record.get("reason", ""),
    )
    records[key] = record
    return record


def render_records(records: list[dict]) -> str:
    """Markdown block for the workflow's daily report (empty if none)."""
    shown = [r for r in records if r.get("decision") != "skip"]
    if not shown:
        return ""
    labels = {
        "rerun": "🔁 re-run",
        "dry-run": "would re-run (dry run)",
        "error": "⚠️ re-run failed",
    }
    rows = []
    for r in shown:
        for job in r["jobs"]:
            run_url = r["run_url"].rstrip("/")
            rows.append(
                f"| [`{job['name']}`]({run_url}/job/{job['id']}) "
                f"| {labels.get(r.get('result'), r.get('result', ''))} "
                f"| `{job['cause'].replace('|', '/').replace('`', '')}` |"
            )
        if r.get("result") == "error":
            rows.append(f"| | {r.get('reason', '')} | |")
    return (
        "### Auto-rerun\n\n"
        "Failed jobs whose log ends in a transient infrastructure error; "
        "only those jobs are re-run, once per run.\n\n"
        "| Failed job | Action | Cause |\n"
        "|-----|--------|-------|\n"
        + "\n".join(rows)
        + "\n\n---\n\n"
    )
