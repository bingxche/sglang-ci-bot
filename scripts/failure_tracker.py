#!/usr/bin/env python3
"""Long-lived per-workflow Failure Trackers on the upstream sglang repo.

This is the **persistence half** of the CI transparency feature. It maintains
one long-lived GitHub issue *per tracked workflow* on ``sgl-project/sglang``,
each a **Failure Tracker** recording every test failure that workflow has ever
shown — so a test that has been red for two months keeps an accurate "Broken
since" date forever, independent of GitHub's log-retention window or the daily
report's short rolling lookback. The goal is transparency: one unified place
where anyone can see every AMD CI failure and exactly how long it has been red.

Design — content (agent) vs state (code), and why they are decoupled
--------------------------------------------------------------------

- **Content (what failed)** is found by a small, dedicated agent task
  (``Task: Failure Tracker Data`` in ``agent/tasks/failure-tracker-data.md``). It reads the SAME
  per-job analyses the daily report is built from, and emits ONLY a compact
  JSON array (one row per failing test, tagged with its ``workflow``). Because
  its output is tiny and singular — not appended to the giant Daily
  Cross-Workflow Summary prose — the model does not drop it (the earlier
  "append to the summary output" approach was observed to silently omit the
  block under output pressure).

- **State (first_seen / days seen / dedup)** is owned by deterministic code
  here. Per normalized ``(test_file, test_function)`` within each workflow's
  issue we keep ``first_seen`` verbatim in a hidden JSON blob in the issue
  body for as long as the failure keeps recurring; one quiet for 30 days
  moves to a one-line archive. An LLM is never asked to remember dates.

Decoupling from the daily report
---------------------------------

The tracker is NOT run inline by each per-workflow monitor job (that would
race: up to 8 summary rebuilds per tick, each seeing partial data). Instead a
single ``finalize`` job in ``ci-monitor.yml`` runs this module ONCE after the
whole matrix completes, when today's daily issue is fully populated.

Extensibility
-------------

Add a workflow to ``TRACKED_WORKFLOWS`` and it gets its own Failure Tracker
issue. The engine is fully generic over the workflow; nothing else changes.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monitor_ci import (
    find_daily_issue,
    find_workflow_comment_parts,
    get_issue_comments,
    parse_job_analyses_from_comment,
)
from utils import (
    GH_TIMEOUT,
    REPO,
    claude_code_analyze,
    claude_code_available,
    create_github_issue,
    ensure_sglang_repo,
    get_issue,
    gh_get,
    gh_headers,
    update_issue_body,
)

log = logging.getLogger("failure-tracker")


# ---------------------------------------------------------------------------
# Config — add a workflow here and it gets its own upstream tracker issue.
# ---------------------------------------------------------------------------

TRACKED_WORKFLOWS: dict[str, dict] = {
    "pr-test-amd.yml": {
        "title": "[Failure Tracker] PR Test (AMD)",
        # Older titles this tracker issue may already exist under, so we
        # migrate (rename) the existing issue instead of creating a duplicate.
        "legacy_titles": ["[CI Tracker] PR Test (AMD) — Persistent Failure Ledger"],
        "display": "PR Test (AMD)",
    },
    # To extend, e.g.:
    # "nightly-test-amd.yml": {
    #     "title": "[Failure Tracker] Nightly Test (AMD)",
    #     "legacy_titles": [],
    #     "display": "Nightly Test (AMD)",
    # },
}

TRACKER_LABEL = "ci-failure-tracker"

# A failure not seen for this many days moves to the one-line Archived list.
# One still failing keeps its first_seen however old it is.
RETENTION_DAYS = 30
# An archived failure is dropped after this many days without being seen.
ARCHIVE_KEEP_DAYS = 180

# Body budget, kept below GitHub's 262,144-byte cap (GITHUB_ISSUE_BODY_MAX_BYTES):
# over the cap GitHub answers 200 but keeps the old body, which froze
# sgl-project/sglang#45847 from 2026-10-25 and #37451 from 2026-09-03. Past
# it, the oldest archived entries go first, then the oldest quiet ones.
MAX_BODY_BYTES = 250_000
READ_BACK_DELAY_SECS = 5

# State blob layout. Versions 1 (a flat {key: entry} dict) and 2 (with row
# IDs) are upgraded on load by upgrade_state().
STATE_VERSION = 3

# Body markers (generic, shared across all per-workflow tracker issues — they
# are scoped inside each issue's own body). We always rewrite the FULL body,
# so these markers exist only for recovering the persisted state blob.
BODY_START = "<!-- ci-failure-tracker:start -->"
BODY_END = "<!-- ci-failure-tracker:end -->"
STATE_PREFIX = "<!-- ci-failure-tracker-state:"
STATE_SUFFIX = "-->"

# Legacy marker from the first single-workflow prototype (issue #27937 was
# created with it). Read-compat only, so that issue migrates seamlessly to
# the generic markers on its next update without losing first_seen history.
_LEGACY_STATE_PREFIX = "<!-- pr-test-amd-tracker-state:"

# Markers the agent wraps its JSON data block in.
DATA_START = "<!-- ci-failure-tracker-data:start -->"
DATA_END = "<!-- ci-failure-tracker-data:end -->"

_DATA_BLOCK_RE = re.compile(
    re.escape(DATA_START) + r"(.*?)" + re.escape(DATA_END), re.DOTALL,
)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _state_re(prefix: str) -> re.Pattern:
    return re.compile(re.escape(prefix) + r"(.*?)" + re.escape(STATE_SUFFIX), re.DOTALL)


# ---------------------------------------------------------------------------
# 1. Parse the agent's JSON data block (content handoff)
# ---------------------------------------------------------------------------

def extract_tracker_rows(agent_text: str) -> list[dict] | None:
    """Pull the JSON failure array out of the agent's data block.

    Returns the parsed list, or ``None`` if no parseable block was found
    (caller then falls back to deterministic extraction). An explicitly
    empty ``[]`` returns ``[]`` (a valid "no failures" answer).
    """
    if not agent_text:
        return None
    m = _DATA_BLOCK_RE.search(agent_text)
    inner = m.group(1) if m else agent_text
    inner = _FENCE_RE.sub("", inner).strip()
    if not inner:
        return [] if m else None
    # Tolerate prose around a bare array when markers are missing.
    if not m:
        lb, rb = inner.find("["), inner.rfind("]")
        if lb == -1 or rb == -1 or rb < lb:
            return None
        inner = inner[lb : rb + 1]
    try:
        rows = json.loads(inner)
    except json.JSONDecodeError as exc:
        log.warning("Could not parse tracker data block (%s)", exc)
        return None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# 2. Deterministic fallback — parse the per-job `### Failed Tests` table.
# ---------------------------------------------------------------------------

_CELL = r"\s*`?(.*?)`?\s*"
_FAILED_ROW_RE = re.compile(
    r"^\|" + _CELL + r"\|" + _CELL + r"\|(.*?)\|(.*)$",
)
_CLUSTER_RE = re.compile(r"^###\s+Failure Cluster\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL)
_REGRESSION_RE = re.compile(r"^###\s+Regression Status\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL)
_FAILED_TESTS_SECTION_RE = re.compile(
    r"^###\s+Failed Tests\s*$(.*?)(?=^###|\Z)", re.MULTILINE | re.DOTALL,
)


def _first_line(text: str) -> str:
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln:
            return ln
    return ""


def deterministic_rows(workflow: str, analyses: list[dict]) -> list[dict]:
    """Fallback extractor: scope-parse each per-job analysis block.

    Scoped strictly to the ``### Failed Tests`` section of a SINGLE already
    isolated per-job analysis (never a loose whole-comment scan), so it does
    not repeat the over-matching that produced fake rows in the past.
    """
    rows: list[dict] = []
    for ja in analyses:
        text = ja.get("analysis") or ""
        run_url = (ja.get("run_url") or "").rstrip("/")
        job_id = ja.get("job_id")
        job_url = f"{run_url}/job/{job_id}" if run_url and job_id else run_url
        cluster_m = _CLUSTER_RE.search(text)
        cluster = _first_line(cluster_m.group(1)) if cluster_m else ""
        reg_m = _REGRESSION_RE.search(text)
        status = _first_line(reg_m.group(1)) if reg_m else ""
        sect = _FAILED_TESTS_SECTION_RE.search(text)
        if not sect:
            continue
        for line in sect.group(1).splitlines():
            line = line.strip()
            if not line.startswith("|") or "---" in line:
                continue
            m = _FAILED_ROW_RE.match(line)
            if not m:
                continue
            tf, fn, err = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            if not tf or tf.lower() in ("test file", "test_file"):
                continue
            rows.append({
                "workflow": workflow,
                "test_file": tf,
                "test_function": fn,
                "cluster": cluster,
                "error": err,
                "status": status,
                "job_url": job_url,
                "job_id": job_id,
                "run_started_at": ja.get("started_at") or "",
            })
    return rows


# ---------------------------------------------------------------------------
# 3. State persistence (the part the agent must NOT own)
# ---------------------------------------------------------------------------
#
# State (version 3):
#   {"version": 3,
#    "active":  {"<test_file>::<test_function>": {test_file, test_function,
#                cluster, error, job_url, job_name, job_id, detail_url, fix_pr,
#                first_seen, last_seen, fail_runs, failed, observed, streak, variants,
#                back_from}},
#    "archive": [[key, first_seen, last_seen, failed, observed, error], ...],
#    "runs":    {"<run id>": [created_at, "rocm10"], ...}}
#
# ``fail_runs`` lists the scheduled runs (IDs) the test failed in. ``failed``
# (those inside the run window), ``observed`` (completed scheduled runs of
# the same ROCm variant since its first failing run) and ``streak`` (failing
# runs in a row, newest first) are recomputed every tick by
# apply_run_stats(). ``runs`` caches each finished run's creation time and
# variant (collect_runs()). ``back_from`` is the old last_seen of a failure
# that returned from the archive. Version 2 counted days and carried row
# IDs; both were dropped to follow sgl-project/sglang#17050, which counts runs.

# "N/A", "unknown (likely test_x[17])", ... name no function.
_PLACEHOLDER_FN_RE = re.compile(
    r"^(?:n/?a|none|unknown|unidentified|unconfirmed|—|-|\?)(?=\s|\(|$)", re.IGNORECASE,
)
# A trailing " (...)" note the agent appends, e.g. "test_x[17] (most likely; ...)".
_ANNOTATION_RE = re.compile(r"\s+\(.*\)\s*$")
# A parenthesised job name the agent appends to a step, e.g.
# "(multimodal-gen-test-1-gpu-amd (rocm724, 2))".
_JOB_HINT_RE = re.compile(r"amd|rocm\d|mi3\d|not a test", re.IGNORECASE)
# The agent's many ways of naming a failed step: "step: X", "step timeout: X",
# "step 'X' timeout", "pre-flight step X failed", "(step-level) X", ...
_STEP_RE = re.compile(
    r"^(?:n/a:\s*)?(?:\((?:step|step-level)\)\s*|(?:pre-flight\s+)?step(?:\s+timeout)?\b\s*:?\s*)(.+)$",
    re.IGNORECASE,
)
_STEP_OUTCOME_RE = re.compile(r"\s+(?:failed|timed out|timeout)\s*$", re.IGNORECASE)
_LOST_PREFIX = "runner lost during "
# One test name: Class.method or function, an optional parametrize suffix.
_TEST_NAME_RE = re.compile(r"^[A-Za-z_][\w.*]*(?:\[.*\])?$")
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")


def _strip_job_parens(text: str) -> str:
    """Drop trailing "(...)" groups that name a job, keeping ones like "(1-GPU)"."""
    text = text.strip()
    while text.endswith(")"):
        depth = 0
        for i in range(len(text) - 1, -1, -1):
            depth += {")": 1, "(": -1}.get(text[i], 0)
            if depth == 0:
                break
        else:
            return text
        if not _JOB_HINT_RE.search(text[i + 1 : -1]):
            return text
        text = text[:i].rstrip()
    return text


def _step_name(name: str) -> str:
    name = name.split(", ", 1)[0].strip()
    name = _STEP_OUTCOME_RE.sub("", name).strip()
    return name.strip("'\"‘’").strip()


def _canonical_descriptor(text: str) -> str:
    """One form for a failure outside a test: ``step: X`` or ``runner lost during X``."""
    text = _strip_job_parens(text)
    if text.lower().startswith(_LOST_PREFIX):
        return _LOST_PREFIX + _step_name(text[len(_LOST_PREFIX):])
    m = _STEP_RE.match(text)
    return f"step: {_step_name(m.group(1))}" if m else text


def canonical_test(test_file: str, test_function: str) -> tuple[str, str]:
    """Normalize the agent's test names so one failure keeps one key.

    The agent writes the same test as ``Class::fn`` one day and ``Class.fn``
    the next, and adds notes like ``(most likely ...)``; each variant used to
    start a new row with a new "Broken since". A failure outside a test gets
    one descriptor form (``_canonical_descriptor``).
    """
    # Test names never contain backticks; the agent's Markdown leaves stray ones.
    tf = (test_file or "").replace("`", "").strip()
    if tf.startswith("./"):
        tf = tf[2:]
    fn = _ANNOTATION_RE.sub("", (test_function or "").replace("`", "").strip())
    if _PLACEHOLDER_FN_RE.match(fn):
        fn = ""
    if not fn:
        tf = _canonical_descriptor(tf)
    return tf, fn.replace("::", ".")


def _split_top_level(text: str, sep: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(text):
        depth += {"[": 1, "]": -1, "(": 1, ")": -1}.get(ch, 0)
        if depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            start = i + len(sep)
    parts.append(text[start:])
    return parts


def split_tests(test_file: str, test_function: str) -> list[tuple[str, str]]:
    """Canonical (file, function) pairs; one agent row may name several tests.

    The analysis's Failed Tests table sometimes puts several tests in one cell
    (``test_a, test_b[x / y]``, ``test_c[1], [2]``); each becomes its own row.
    Text that is not a test name (``10 tests``, ``subtest ...``) is dropped.
    """
    tf, fn = canonical_test(test_file, test_function)
    if not fn:
        return [(tf, fn)]
    fn = _split_top_level(fn, ": ")[0]
    names: list[str] = []
    base = ""
    for part in _split_top_level(fn, ","):
        part = part.strip()
        if part.startswith("[") and base:
            part = base + part
        if not _TEST_NAME_RE.match(part):
            continue
        stem, bracket, params = part.partition("[")
        base = stem
        variants = params[:-1].split(" / ") if bracket and " / " in params else None
        if variants:
            names += [f"{stem}[{v.strip()}]" for v in variants if v.strip()]
        else:
            names.append(part)
    # "test_x[*]" means every variant, the same as the bare name.
    names = [n[:-3] if n.endswith("[*]") else n for n in names]
    names = list(dict.fromkeys(names)) or [fn]
    return [(tf, n) for n in names]


def _run_id(url: str) -> str:
    m = _RUN_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _load_raw_state(body: str) -> dict:
    for prefix in (STATE_PREFIX, _LEGACY_STATE_PREFIX):
        m = _state_re(prefix).search(body or "")
        if not m:
            continue
        try:
            state = json.loads(m.group(1).strip())
            if isinstance(state, dict):
                return state
        except json.JSONDecodeError as exc:
            log.warning("State blob (%s) corrupt (%s); ignoring", prefix, exc)
    return {}


def _absorb(active: dict, entry: dict) -> None:
    """Add ``entry`` under its canonical key(s), merging with existing ones."""
    for tf, fn in split_tests(entry.get("test_file") or "", entry.get("test_function") or ""):
        if not tf and not fn:
            continue
        new = {**entry, "test_file": tf, "test_function": fn}
        key = f"{tf}::{fn}"
        old = active.get(key)
        if old is None:
            active[key] = new
            continue
        if new.get("last_seen", "") >= old.get("last_seen", ""):
            newer, older = new, old
        else:
            newer, older = old, new
        merged = {**older, **newer}
        firsts = [d for d in (old.get("first_seen"), new.get("first_seen")) if d]
        if firsts:
            merged["first_seen"] = min(firsts)
        if "fail_runs" in old or "fail_runs" in new:
            merged["fail_runs"] = sorted(set(old.get("fail_runs", [])) | set(new.get("fail_runs", [])))
        active[key] = merged


def upgrade_state(raw: dict) -> dict:
    """Return a version-3 state from any stored layout, keys re-normalized.

    Entries from older versions have no ``fail_runs``; needs_backfill() spots
    them so the caller can rebuild their runs from the daily issues once.
    """
    version = raw.get("version")
    if version == STATE_VERSION:
        old_active = [e for e in (raw.get("active") or {}).values() if isinstance(e, dict)]
        archive_rows = [a for a in raw.get("archive") or [] if isinstance(a, list) and len(a) == 6]
    else:
        if version == 2:
            old_active = [e for e in (raw.get("active") or {}).values() if isinstance(e, dict)]
        else:  # version 1: a flat {key: entry} dict
            old_active = [e for e in raw.values() if isinstance(e, dict)]
        old_active = [
            {k: v for k, v in e.items()
             if k not in ("id", "seen_days", "streak_start", "status", "run_started_at")}
            for e in old_active
        ]
        # Version 2 archive rows: [id, key, first, last, seen_days, error]; the
        # day count is not a run count, so it is dropped.
        archive_rows = [
            [a[1], a[2], a[3], 0, 0, a[5]] for a in raw.get("archive") or []
            if version == 2 and isinstance(a, list) and len(a) >= 6
        ]
    active: dict = {}
    for e in old_active:
        _absorb(active, e)
    archive: dict = {}
    for key, first, last, failed, observed, err in archive_rows:
        tf, _, fn = key.partition("::")
        for ctf, cfn in split_tests(tf, fn):
            ckey = f"{ctf}::{cfn}"
            if ckey in active:
                continue
            old = archive.get(ckey)
            if old is None or last > old[2]:
                archive[ckey] = [ckey, min(first, old[1]) if old else first, last, failed, observed, err]
    runs = raw.get("runs") if version == STATE_VERSION and isinstance(raw.get("runs"), dict) else {}
    return {"version": STATE_VERSION, "active": active, "archive": list(archive.values()),
            "runs": runs}


def load_state(body: str) -> dict:
    """Recover the persisted tracker state (any version, legacy marker too)."""
    return upgrade_state(_load_raw_state(body))


def needs_backfill(state: dict) -> bool:
    """True if some entry predates run counting (no ``fail_runs`` yet)."""
    return any("fail_runs" not in e for e in state["active"].values())


def merge_state(state: dict, rows: list[dict], today: str, detail_base: str) -> dict:
    """Merge today's failures into the tracker; first_seen is kept while it fails.

    - Active key → keep ``first_seen``; record the row's run in ``fail_runs``.
    - Archived key → back to active, dated today, with ``back_from`` set to
      the day it was last seen before; its run history starts again.
    - New key → ``first_seen = last_seen = today``.
    - Absent key → untouched → renders under "No longer failing".
    """
    active, archive = state["active"], state["archive"]
    for r in rows:
        job_id = r.get("job_id")
        latest = {
            "cluster": (r.get("cluster") or "").strip(),
            "error": _one_line(r.get("error") or ""),
            "job_url": (r.get("job_url") or "").strip(),
            "job_name": (r.get("job_name") or "").strip(),
            "job_id": job_id,
            "detail_url": f"{detail_base}{job_id}" if detail_base and job_id else "",
        }
        if "fix_pr" in r:  # "" = the analysis found none
            latest["fix_pr"] = r["fix_pr"]
        run_id = _run_id(latest["job_url"])
        for tf, fn in split_tests(r.get("test_file") or "", r.get("test_function") or ""):
            if not tf and not fn:
                continue
            key = f"{tf}::{fn}"
            e = active.get(key)
            if e is None:
                back = next((a for a in archive if a[0] == key), None)
                e = {"back_from": back[2]} if back is not None else {}
                if back is not None:
                    archive.remove(back)
                e.update(first_seen=today, fail_runs=[])
                active[key] = e
            if run_id and run_id not in e.setdefault("fail_runs", []):
                e["fail_runs"].append(run_id)
            e.update(latest, test_file=tf, test_function=fn, last_seen=today)
    return state


_SAME_FILE = {"same file", "same", "same as above", "ditto", "〃"}


def record_table_runs(state: dict, analyses: list[dict]) -> int:
    """Add each analysis's run to the tracked tests its Failed Tests table names.

    The agent reports one row per test, from its latest run, so a test that
    failed in two runs of the same day would be counted once. The tables list
    every failing job; only tests already tracked are touched. Returns how
    many (test, run) pairs were added.
    """
    added = 0
    for ja in analyses:
        run_id = _run_id(ja.get("run_url") or "")
        if not run_id:
            continue
        prev_file = ""
        for row in deterministic_rows("", [ja]):
            # Tables often write "same file" for the row above's file.
            if row["test_file"].lower().strip(" .") in _SAME_FILE and prev_file:
                row["test_file"] = prev_file
            prev_file = row["test_file"]
            for tf, fn in split_tests(row["test_file"], row["test_function"]):
                e = state["active"].get(f"{tf}::{fn}")
                if e is not None and run_id not in e.setdefault("fail_runs", []):
                    e["fail_runs"].append(run_id)
                    added += 1
    return added


def apply_run_stats(state: dict, runs: dict[str, list], since: str) -> None:
    """Set each active entry's ``failed``, ``observed``, ``streak`` and ``variants``.

    ``runs`` maps run ID → [created_at, variant, listed]: every run in the
    window, plus every run an entry failed in (``listed`` is False for those
    not in the completed-scheduled list, e.g. still running or cancelled).
    Each scheduled run tests one ROCm image, so a test is measured only
    against runs of the variants it failed on: a rocm724-only failure is
    "failed 3 of 3 runs on rocm724", not "3 of 9 runs". A test's runs are
    counted from its first failing run, but not before ``since``. Runs newer
    than the newest failing run anywhere in the tracker are left out, because
    their analyses may not have arrived yet. ``first_seen`` moves back to the
    first failing run's date.
    """
    def created(r):
        return runs[r][0] if r in runs else ""

    known = [created(r) for e in state["active"].values() for r in e.get("fail_runs", []) if r in runs]
    horizon = max(known) if known else ""
    ordered = sorted((r for r, v in runs.items() if v[0] <= horizon), key=created)
    for e in state["active"].values():
        fails = {r for r in e.get("fail_runs", []) if r in runs and created(r) >= since}
        variants = sorted({runs[r][1] for r in fails if runs[r][1]})
        e["variants"] = variants
        if not fails:
            n = len(e.get("fail_runs", [])) or 1
            e["failed"], e["observed"], e["streak"] = n, n, 0
            continue
        first = min(created(r) for r in fails)
        window = [r for r in ordered
                  if created(r) >= first and (runs[r][2] or r in fails)
                  and (not variants or runs[r][1] in variants)]
        streak = 0
        for r in reversed(window):
            if r not in fails:
                break
            streak += 1
        e["failed"], e["observed"], e["streak"] = len(fails), len(window), streak
        e["first_seen"] = min(e.get("first_seen") or "9", first[:10])


def _archive_row(entry: dict) -> list:
    key = f"{entry.get('test_file', '')}::{entry.get('test_function', '')}"
    err = entry.get("error") or "—"
    if len(err) > 80:
        err = _truncate(err, 80)
    failed = entry.get("failed", len(entry.get("fail_runs", [])))
    return [key, entry.get("first_seen", ""), entry.get("last_seen", ""),
            failed, max(entry.get("observed", failed), failed), err]


def archive_quiet(state: dict, today: str, retention_days: int = RETENTION_DAYS) -> tuple[int, int]:
    """Archive failures quiet for over ``retention_days``; drop old archive rows.

    Returns (archived, dropped). Entries with an unparseable ``last_seen`` stay.
    """
    stale = [
        key for key, e in state["active"].items()
        if _days_between(e.get("last_seen", ""), today) > retention_days
    ]
    for key in stale:
        state["archive"].append(_archive_row(state["active"].pop(key)))
    before = len(state["archive"])
    state["archive"] = [
        a for a in state["archive"] if _days_between(a[2], today) <= ARCHIVE_KEEP_DAYS
    ]
    return len(stale), before - len(state["archive"])


_FIX_RE = re.compile(
    r"In-flight fix:[*\s]*✅[`*\s]*\[([^\]]+)\]\((https://github\.com/[^)\s]+/pull/\d+)\)"
)


_NO_FIX_RE = re.compile(r"In-flight fix:[*\s]*❌")


def fix_prs_by_job(analyses: list[dict]) -> dict[str, str]:
    """Map job ID → the in-flight fix PR its daily analysis found, as a link.

    A job whose analysis says ``In-flight fix: ❌`` maps to "", which clears
    an earlier fix PR; a job that says nothing is left out.
    """
    out: dict[str, str] = {}
    for ja in analyses:
        if not ja.get("job_id"):
            continue
        text = ja.get("analysis") or ""
        m = _FIX_RE.search(text)
        if m:
            out[str(ja["job_id"])] = f"[{m.group(1)}]({m.group(2)})"
        elif _NO_FIX_RE.search(text):
            out[str(ja["job_id"])] = ""
    return out


# ---------------------------------------------------------------------------
# 4. Rendering
# ---------------------------------------------------------------------------

def _one_line(text: str, limit: int = 110) -> str:
    flat = " ".join((text or "").split()).replace("|", "\\|")
    if len(flat) > limit:
        flat = _truncate(flat, limit)
    return flat or "—"


def _truncate(text: str, limit: int) -> str:
    # A cut inside an escaped "|" would leave a backslash escaping the cell pipe.
    return text[: limit - 1].rstrip().rstrip("\\") + "…"


def _days_between(start: str, end: str) -> int:
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
        return max((d1 - d0).days, 0)
    except (ValueError, TypeError):
        return 0


def classify(entry: dict, today: str) -> str:
    """``broken`` / ``flaky`` / ``new`` / ``fixed``, counted in runs (see render_body)."""
    if entry.get("last_seen") != today:
        return "fixed"
    if entry.get("streak", 0) >= 2:
        return "broken"
    if len(entry.get("fail_runs", [])) <= 1 and not entry.get("back_from"):
        return "new"
    return "flaky"


def _runs_text(failed: int, observed: int, variants: list[str] | None = None) -> str:
    on = f" on {', '.join(variants)}" if variants else ""
    return f"failed {failed} of {max(observed, failed)} runs{on}"


def _notes_cell(entry: dict, today: str) -> str:
    kind = classify(entry, today)
    failed = entry.get("failed") or len(entry.get("fail_runs", [])) or 1
    streak = entry.get("streak", 0)
    if kind == "broken" and streak < failed:
        kind = f"broken, last {streak} runs in a row"
    txt = f"{kind} · {_runs_text(failed, entry.get('observed', failed), entry.get('variants'))}"
    if entry.get("back_from"):
        txt += f"; failing again, the failure before was on {entry['back_from']}"
    return txt


def _test_cell(test_file: str, test_function: str) -> str:
    # The file name alone, like sgl-project/sglang#17050; the job log has the path.
    name = test_file or "—"
    if test_function or name.endswith(".py"):  # a path, not a step descriptor
        name = name.rsplit("/", 1)[-1]
    if test_function:
        name += f"::{test_function}"
    return f"`{name}`"


def _job_cell(entry: dict) -> str:
    url, name = entry.get("job_url") or "", (entry.get("job_name") or "").replace("|", "\\|")
    if url:
        return f"[{name or 'job'}]({url})"
    return name or "—"


def _render_row(entry: dict, today: str) -> str:
    detail = f"[Detail]({entry['detail_url']})" if entry.get("detail_url") else "—"
    return (
        f"| {entry.get('first_seen', '?')} "
        f"| {_test_cell(entry.get('test_file', ''), entry.get('test_function', ''))} "
        f"| {_job_cell(entry)} | {entry.get('error') or '—'} "
        f"| {_notes_cell(entry, today)} | {entry.get('fix_pr') or '-'} | {detail} |"
    )


_TABLE_HEADER = (
    "| Broken since | Test | Latest failing job | Error | Notes "
    "| Fix PR (from AI analysis, may be wrong) | Detail |\n"
    "|------|------|---------|-------|-------|--------|-------|"
)

_FIXED_HEADER = (
    "| Last failed | Issue | Fix PR (from AI analysis, may be wrong) |\n"
    "|------|-------|-----|"
)

_KIND_ORDER = {"broken": 0, "flaky": 1, "new": 2}

CI_FAILURE_MONITOR_URL = (
    "https://github.com/sgl-project/sglang/actions/workflows/ci-failure-monitor.yml"
)


def _state_blob(state: dict) -> str:
    blob = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    # Text inside the blob must not close the HTML comment that holds it.
    blob = blob.replace("-->", "--\\u003e")
    return f"{STATE_PREFIX}\n{blob}\n{STATE_SUFFIX}"


def render_body(
    workflow: str,
    display: str,
    state: dict,
    snapshot_utc: str,
    bot_daily_url: str,
) -> str:
    """Render one workflow's Failure Tracker body, laid out like sgl-project/sglang#17050."""
    today = snapshot_utc.split(" ")[0]
    entries = list(state["active"].values())
    failing = [e for e in entries if e.get("last_seen") == today]
    # Newest failures first.
    failing.sort(key=lambda e: (e.get("first_seen", ""), e.get("test_file", ""),
                                e.get("test_function", "")), reverse=True)
    counts = {k: sum(1 for e in failing if classify(e, today) == k) for k in _KIND_ORDER}

    # Quiet active entries and the archive share one compact list, newest first.
    fixed = [
        (e.get("last_seen", ""), e.get("test_file", ""), e.get("test_function", ""),
         e.get("first_seen", ""), e.get("failed") or len(e.get("fail_runs", [])) or 1,
         e.get("observed", 0), e.get("error") or "—", e.get("fix_pr") or "-")
        for e in entries if e.get("last_seen") != today
    ]
    for key, first, last, failed, observed, err in state["archive"]:
        tf, _, fn = key.partition("::")
        fixed.append((last, tf, fn, first, failed, observed, err, "-"))
    fixed.sort(key=lambda f: (f[0], f[1], f[2]), reverse=True)

    lines = [
        BODY_START,
        _state_blob(state),
        f"# [Failure Tracker] {display}",
        "",
        f"This issue tracks test failures in the scheduled `{workflow}` runs on `main`, "
        "kept up to date by amd-bot. A failure keeps its **Broken since** date for as "
        "long as it keeps failing.",
        "",
        f"**Last auto-update:** {snapshot_utc} — {counts['broken']} broken, "
        f"{counts['flaky']} flaky, {counts['new']} new, {len(fixed)} no longer failing",
        "",
        "## Quick Links",
        "",
        f"- **[Daily report]({bot_daily_url})** - today's per-job analyses "
        "(stack traces, suspected commits, in-flight fixes)",
        f"- **[CI Failure Monitor]({CI_FAILURE_MONITOR_URL})** - sglang's own "
        "failure-streak report across backends",
        "",
        "---",
        "",
        "## Currently failing",
        "",
    ]
    if failing:
        lines += [_TABLE_HEADER] + [_render_row(e, today) for e in failing] + [""]
    else:
        lines += [f"_No `{workflow}` failures in the latest scan._", ""]
    lines += [
        "**Notes:** *broken* = failed in the latest runs one after another; *flaky* = "
        "failed again, but not in every run; *new* = failed in one run so far. "
        "\"Failed 5 of 12 runs on rocm724\" counts the completed scheduled runs on `main` "
        f"since the first failing one (at most {RUN_WINDOW_DAYS} days back), among runs of "
        "the ROCm images it failed on (each "
        "scheduled run tests one image). A test can fail in a different job each time "
        "(suites are re-partitioned), so runs, not jobs, are counted.",
        "",
        "---",
        "",
    ]
    if fixed:
        lines += [
            "<details>",
            f"<summary><h2>No longer failing ({len(fixed)}, click to expand)</h2></summary>",
            "",
            f"Not seen in the latest scan: fixed, or not run again. After {RETENTION_DAYS} "
            f"quiet days a failure is kept here in short form for {ARCHIVE_KEEP_DAYS} days.",
            "",
            _FIXED_HEADER,
        ]
        for last, tf, fn, first, failed, observed, err, fix in fixed:
            err = _truncate(err, 70) if len(err) > 70 else err
            runs = f"{_runs_text(failed, observed)} since {first}" if observed else f"since {first}"
            lines.append(f"| {last} | {_test_cell(tf, fn)} — {err} ({runs}) | {fix} |")
        lines += ["", "</details>", ""]

    lines += [f"*Auto-generated by amd-bot · {snapshot_utc}*", BODY_END]
    return "\n".join(lines)


def render_bounded_body(
    workflow: str,
    display: str,
    state: dict,
    snapshot_utc: str,
    bot_daily_url: str,
    max_bytes: int = MAX_BODY_BYTES,
) -> tuple[str, int]:
    """Render the body within ``max_bytes``.

    Over budget, the oldest archived entries are dropped first; once the
    archive is empty, the oldest quiet entries are archived (and so become
    droppable). Mutates ``state`` and returns the body and how many entries
    were dropped. Raises ``ValueError`` if today's failures alone do not fit.
    """
    today = snapshot_utc.split(" ")[0]
    dropped = 0
    while True:
        body = render_body(workflow, display, state, snapshot_utc, bot_daily_url)
        size = len(body.encode("utf-8"))
        if size <= max_bytes:
            return body, dropped
        # An entry is a few hundred bytes: shed in batches, not one per render.
        batch = max(1, (size - max_bytes) // 300)
        if state["archive"]:
            state["archive"].sort(key=lambda a: (a[2], a[1]))
            n = min(batch, len(state["archive"]))
            del state["archive"][:n]
            dropped += n
            continue
        quiet = sorted(
            (k for k, e in state["active"].items() if e.get("last_seen") != today),
            key=lambda k: (state["active"][k].get("last_seen", ""),
                           state["active"][k].get("first_seen", "")),
        )
        if not quiet:
            raise ValueError(
                f"tracker body is {size} bytes with only today's failures "
                f"(budget {max_bytes})"
            )
        for key in quiet[:batch]:
            state["archive"].append(_archive_row(state["active"].pop(key)))


# ---------------------------------------------------------------------------
# 5. Upstream issue find / create / update (per workflow)
# ---------------------------------------------------------------------------

def find_tracker_issue(token: str, title: str) -> int | None:
    """Find an open issue with EXACTLY this title on the upstream repo.

    Lists the labelled issues first: the search index can lag or miss, and on
    2026-10-22 two ticks in a row found nothing and created duplicates. Search
    stays as a fallback for an issue that lost its label (or a legacy title).
    If several match, the oldest one wins.
    """
    found: dict[int, dict] = {}
    resp = gh_get(
        f"https://api.github.com/repos/{REPO}/issues",
        headers=gh_headers(token),
        params={"labels": TRACKER_LABEL, "state": "open", "per_page": 100},
    )
    resp.raise_for_status()
    for item in resp.json():
        if "pull_request" not in item:
            found[item["number"]] = item
    if not any(i.get("title") == title for i in found.values()):
        q = f'repo:{REPO} is:issue is:open in:title "{title}"'
        resp = gh_get(
            "https://api.github.com/search/issues",
            headers=gh_headers(token), params={"q": q, "per_page": 20},
        )
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            found[item["number"]] = item
    matches = [
        i for i in found.values()
        if i.get("title") == title and i.get("state", "open") == "open"
    ]
    if not matches:
        return None
    # The oldest wins: a stable choice, unlike updated_at, which any comment
    # on a stale duplicate would change.
    matches.sort(key=lambda i: i["number"])
    if len(matches) > 1:
        log.warning(
            "Several open issues titled %r: %s; using #%d",
            title, ", ".join(f"#{i['number']}" for i in matches), matches[0]["number"],
        )
    return matches[0]["number"]


def _rename_issue(token: str, issue_num: int, title: str) -> None:
    resp = requests.patch(
        f"https://api.github.com/repos/{REPO}/issues/{issue_num}",
        headers=gh_headers(token), json={"title": title}, timeout=GH_TIMEOUT,
    )
    resp.raise_for_status()


def find_or_create_tracker_issue(token: str, cfg: dict) -> int:
    """Find this workflow's tracker issue (migrating legacy titles) or create it."""
    title, display = cfg["title"], cfg["display"]
    existing = find_tracker_issue(token, title)
    if existing is not None:
        return existing
    # Migrate an issue that exists under an older title rather than duplicating.
    for legacy in cfg.get("legacy_titles", []):
        found = find_tracker_issue(token, legacy)
        if found is not None:
            log.info("Migrating tracker issue #%d title → %r", found, title)
            try:
                _rename_issue(token, found, title)
            except Exception as exc:
                log.warning("Could not rename issue #%d (%s); using as-is", found, exc)
            return found
    placeholder = (
        f"{BODY_START}\n{STATE_PREFIX}\n{{}}\n{STATE_SUFFIX}\n"
        f"# {display} — Failure Tracker\n\n"
        f"_Initializing… the first scan will populate this tracker._\n{BODY_END}\n"
    )
    issue = create_github_issue(token, title, placeholder, labels=[TRACKER_LABEL], repo=REPO)
    log.info("Created upstream tracker issue #%d (%s)", issue["number"], display)
    return issue["number"]


def scheduled_runs(token: str, workflow: str, since: str) -> list[tuple[str, str]]:
    """Completed scheduled runs of ``workflow`` on ``main`` since ``since``, as (id, created_at).

    Cancelled runs are left out; a re-run keeps its run ID and creation time.
    """
    out: list[tuple[str, str]] = []
    page = 1
    while True:
        resp = gh_get(
            f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/runs",
            headers=gh_headers(token),
            params={"event": "schedule", "branch": "main", "status": "completed",
                    "created": f">={since}", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json().get("workflow_runs", [])
        out += [(str(r["id"]), r["created_at"]) for r in batch
                if r.get("conclusion") in ("success", "failure")]
        if len(batch) < 100:
            return out
        page += 1


_VARIANT_RE = re.compile(r"\((rocm\w+)")
# How far back run counts reach; older failing runs are not counted.
RUN_WINDOW_DAYS = 45


def _run_variant(token: str, run_id: str) -> str:
    resp = gh_get(
        f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs",
        headers=gh_headers(token), params={"per_page": 30},
    )
    resp.raise_for_status()
    names = " ".join(j.get("name") or "" for j in resp.json().get("jobs", []))
    m = _VARIANT_RE.search(names)
    return m.group(1) if m else ""


def collect_runs(token: str, state: dict, completed: list[tuple[str, str]]) -> dict[str, list]:
    """Every run the counts need, as run ID → [created_at, variant, listed].

    ``completed`` are the completed scheduled runs in the window. Runs some
    entry failed in but that are not listed (still running, cancelled, or
    older than the window) are fetched once. Each run's ROCm variant is read
    from its job names. Finished runs are cached in ``state["runs"]`` as
    [created_at, variant], so later ticks only look up new runs; the cache
    keeps only the runs still needed.
    """
    cache = state.get("runs", {})
    listed = dict(completed)
    referenced = {r for e in state["active"].values() for r in e.get("fail_runs", [])}
    out: dict[str, list] = {}
    keep: dict[str, list] = {}
    for run_id in set(listed) | referenced:
        if run_id in cache:
            made, variant = cache[run_id]
            done = True
        elif run_id in listed:
            made, variant, done = listed[run_id], _run_variant(token, run_id), True
        else:
            resp = gh_get(f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}",
                          headers=gh_headers(token))
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            run = resp.json()
            made, variant = run["created_at"], _run_variant(token, run_id)
            done = run.get("status") == "completed"
        out[run_id] = [made, variant, run_id in listed]
        if done:
            keep[run_id] = [made, variant]
    state["runs"] = keep
    return out


def _runs_since(today: str) -> str:
    start = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=RUN_WINDOW_DAYS)
    return start.strftime("%Y-%m-%d")


_DAILY_TITLE = "[CI Monitor] Daily Report - "


def backfill_runs(token: str, bot_repo: str, state: dict, workflow: str) -> int:
    """Rebuild ``fail_runs`` once for entries stored before runs were counted.

    Reads the Failed Tests tables of every daily issue since the earliest such
    entry's first_seen. Returns how many (test, run) pairs were found.
    """
    old = [e for e in state["active"].values() if "fail_runs" not in e]
    if not old:
        return 0
    since = min((e.get("first_seen") or "9") for e in old)
    added = 0
    page = 1
    while True:
        resp = gh_get(
            f"https://api.github.com/repos/{bot_repo}/issues",
            headers=gh_headers(token),
            params={"labels": "ci-monitor", "state": "all", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        batch = resp.json()
        for issue in batch:
            title = issue.get("title") or ""
            if title.startswith(_DAILY_TITLE) and title[len(_DAILY_TITLE):] >= since:
                analyses = collect_tracked_analyses(token, bot_repo, issue["number"]).get(workflow, [])
                added += record_table_runs(state, analyses)
        if len(batch) < 100:
            break
        page += 1
    for e in old:
        e.setdefault("fail_runs", [])
        run_id = _run_id(e.get("job_url") or "")
        if run_id and run_id not in e["fail_runs"]:
            e["fail_runs"].append(run_id)
    log.info("[%s] backfilled %d failing runs for %d entries since %s",
             workflow, added, len(old), since)
    return added


def update_one_workflow(
    token: str,
    workflow: str,
    cfg: dict,
    bot_repo: str,
    daily_issue_num: int | None,
    rows: list[dict],
    snapshot_utc: str,
    analyses: list[dict] | None = None,
) -> int | None:
    """Merge one workflow's failures into its own upstream tracker issue."""
    today = snapshot_utc.split(" ")[0]
    detail_base = ""
    bot_daily_url = f"https://github.com/{bot_repo}"
    if bot_repo and daily_issue_num:
        bot_daily_url = f"https://github.com/{bot_repo}/issues/{daily_issue_num}"
        detail_base = f"{bot_daily_url}#job-"

    issue_num = find_or_create_tracker_issue(token, cfg)
    issue = get_issue(token, REPO, issue_num)
    state = load_state(issue.get("body", ""))
    if needs_backfill(state):
        backfill_runs(token, bot_repo, state, workflow)
    state = merge_state(state, rows, today, detail_base)
    record_table_runs(state, analyses or [])
    try:
        since = _runs_since(today)
        runs = collect_runs(token, state, scheduled_runs(token, workflow, since))
        apply_run_stats(state, runs, since)
    except Exception as exc:  # the counts wait for the next tick; the rows still update
        log.warning("[%s] could not list scheduled runs (%s); run counts not refreshed",
                    workflow, exc)
    archived, expired = archive_quiet(state, today)

    new_body, dropped = render_bounded_body(
        workflow, cfg["display"], state, snapshot_utc, bot_daily_url,
    )
    if archived or expired or dropped:
        log.info(
            "[%s] archived %d entries quiet over %d days; dropped %d archived over "
            "%d days and %d more to fit %d bytes",
            workflow, archived, RETENTION_DAYS, expired, ARCHIVE_KEEP_DAYS,
            dropped, MAX_BODY_BYTES,
        )
    if new_body.strip() == (issue.get("body") or "").strip():
        log.info("[%s] tracker #%d unchanged; skipping PATCH", workflow, issue_num)
        return issue_num
    update_issue_body(token, REPO, issue_num, new_body)
    # A 2xx PATCH is not proof the body was saved (see MAX_BODY_BYTES). Read
    # back twice before failing, in case the first read hits a lagging replica.
    for delay in (0, READ_BACK_DELAY_SECS):
        time.sleep(delay)
        saved = (get_issue(token, REPO, issue_num).get("body") or "").strip()
        if saved == new_body.strip():
            break
    else:
        raise RuntimeError(
            f"tracker #{issue_num} body was not saved "
            f"({len(new_body.encode('utf-8'))} bytes sent, {len(saved.encode('utf-8'))} read back)"
        )
    log.info(
        "[%s] updated tracker #%d (%d active, %d archived, %d failing this scan)",
        workflow, issue_num, len(state["active"]), len(state["archive"]), len(rows),
    )
    return issue_num


# ---------------------------------------------------------------------------
# 6. Content collection + agent extraction
# ---------------------------------------------------------------------------

def collect_tracked_analyses(
    token: str, bot_repo: str, issue_num: int,
) -> dict[str, list[dict]]:
    """Recover per-job analyses for the tracked workflows from the daily issue."""
    comments = get_issue_comments(token, bot_repo, issue_num)
    out: dict[str, list[dict]] = {}
    for wf in TRACKED_WORKFLOWS:
        main, overflow = find_workflow_comment_parts(comments, wf)
        if not main:
            out[wf] = []
            continue
        combined = main["body"] + "\n" + "\n".join(c["body"] for c in overflow)
        out[wf] = parse_job_analyses_from_comment(combined)
    return out


def _build_agent_context(wf_analyses: dict[str, list[dict]]) -> str:
    blocks: list[str] = []
    for wf, analyses in wf_analyses.items():
        blocks.append(f"# Workflow: {wf}")
        if not analyses:
            blocks.append("_(no failures in today's daily issue)_\n")
            continue
        for ja in analyses:
            blocks.append(
                f"## Job: {ja.get('job_name', '?')}\n"
                f"Workflow: {wf}\n"
                f"Job ID: {ja.get('job_id', '?')}\n"
                f"Run URL: {ja.get('run_url', '?')}\n"
                f"Started: {ja.get('started_at', '?')}\n\n"
                f"{(ja.get('analysis') or '').strip()}\n"
            )
    return "\n\n---\n\n".join(blocks)


def _build_agent_prompt(workflows: list[str]) -> str:
    return (
        "Task: Failure Tracker Data\n"
        f"Workflows: {', '.join(workflows)}\n"
        "Per-workflow per-job analyses: .ci-context/failure-tracker-analyses.md\n"
        "Source: current directory\n"
        "GitHub API token: $GH_PAT"
    )


def run_agent_extraction(
    wf_analyses: dict[str, list[dict]],
) -> list[dict] | None:
    """Run the dedicated small agent task that emits the tracker JSON."""
    if not claude_code_available():
        log.info("Claude Code CLI unavailable; agent extraction skipped")
        return None
    try:
        repo_path = ensure_sglang_repo()
    except Exception as exc:
        log.warning("Could not prepare sglang repo (%s); agent extraction skipped", exc)
        return None
    prompt = _build_agent_prompt(list(wf_analyses.keys()))
    try:
        out = claude_code_analyze(
            prompt=prompt,
            work_dir=repo_path,
            context_files={"failure-tracker-analyses.md": _build_agent_context(wf_analyses)},
            max_turns=int(os.environ.get("TRACKER_AGENT_MAX_TURNS", "80")),
            timeout_secs=int(os.environ.get("TRACKER_AGENT_TIMEOUT_SECS", "1800")),
        )
    except Exception as exc:
        log.warning("Agent extraction failed (%s)", exc)
        return None
    return extract_tracker_rows(out)


# ---------------------------------------------------------------------------
# 7. Orchestrator
# ---------------------------------------------------------------------------

def update_trackers(
    token: str,
    bot_repo: str,
    date_str: str | None = None,
    use_agent: bool = True,
) -> dict[str, int | None]:
    """Update every tracked workflow's Failure Tracker from today's daily issue.

    Runs ONCE (from the ci-monitor ``finalize`` job) after the matrix
    completes. Content is found by the dedicated agent task (with a
    deterministic fallback); state/persistence is owned here.
    """
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    snapshot_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    issue_num = find_daily_issue(token, bot_repo, date_str)
    if not issue_num:
        log.info("No daily issue for %s; nothing to track", date_str)
        return {}

    wf_analyses = collect_tracked_analyses(token, bot_repo, issue_num)
    total = sum(len(v) for v in wf_analyses.values())
    log.info(
        "Tracking %d workflow(s); %d per-job analyses recovered from issue #%d",
        len(TRACKED_WORKFLOWS), total, issue_num,
    )

    rows: list[dict] | None = None
    if use_agent and total > 0:
        rows = run_agent_extraction(wf_analyses)
    if rows is None:
        if total > 0:
            log.info("Falling back to deterministic extraction")
        rows = []
        for wf, analyses in wf_analyses.items():
            rows.extend(deterministic_rows(wf, analyses))

    by_wf: dict[str, list[dict]] = {wf: [] for wf in TRACKED_WORKFLOWS}
    for r in rows:
        wf = (r.get("workflow") or "").strip()
        if wf in by_wf:
            by_wf[wf].append(r)
        elif len(TRACKED_WORKFLOWS) == 1:
            # Single tracked workflow: tolerate a missing/blank workflow tag.
            by_wf[next(iter(TRACKED_WORKFLOWS))].append(r)

    # The fix PR and job name come from the job's own analysis, by code, not the agent.
    for wf, wf_rows in by_wf.items():
        fixes = fix_prs_by_job(wf_analyses.get(wf, []))
        names = {str(ja.get("job_id")): ja.get("job_name") or "" for ja in wf_analyses.get(wf, [])}
        for r in wf_rows:
            r["job_name"] = names.get(str(r.get("job_id")), "")
            fix = fixes.get(str(r.get("job_id")))
            if fix is not None:
                r["fix_pr"] = fix

    results: dict[str, int | None] = {}
    for wf, cfg in TRACKED_WORKFLOWS.items():
        try:
            results[wf] = update_one_workflow(
                token, wf, cfg, bot_repo, issue_num, by_wf[wf], snapshot_utc,
                analyses=wf_analyses.get(wf, []),
            )
        except Exception as exc:
            log.error("[%s] tracker update failed (%s)", wf, exc)
            results[wf] = None
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update upstream per-workflow persistent Failure Trackers",
    )
    parser.add_argument("--bot-repo", required=True, help="e.g. bingxche/sglang-ci-bot")
    parser.add_argument("--date", help="Daily issue date YYYY-MM-DD (UTC). Default: today.")
    parser.add_argument(
        "--use-agent", action=argparse.BooleanOptionalAction,
        default=os.environ.get("USE_AGENT", "").lower() not in ("false", "0", "no"),
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get(
            "BOT_PAT", os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO, stream=sys.stdout,
    )
    if not args.github_token:
        log.error("GitHub token required (BOT_PAT / GH_PAT / GITHUB_TOKEN).")
        return 1

    results = update_trackers(
        args.github_token, args.bot_repo,
        date_str=args.date, use_agent=args.use_agent,
    )
    for wf, num in results.items():
        log.info("%s → issue %s", wf, f"#{num}" if num else "(failed)")
    # Fail the step so a tracker that stops updating shows up as a red run.
    return 1 if any(num is None for num in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
