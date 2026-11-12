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
from datetime import datetime, timezone
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

# State blob layout. Version 1 was a flat {key: entry} dict; it is upgraded
# on load by upgrade_state().
STATE_VERSION = 2
# Row IDs (A1, A2, ...) are never reused, so a PR can cite "AMD tracker A12".
ID_PREFIX = "A"

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
# State (version 2):
#   {"version": 2, "next_id": 13,
#    "active":  {"<test_file>::<test_function>": {id, test_file, test_function,
#                cluster, error, job_url, job_id, detail_url, fix_pr,
#                first_seen, last_seen, seen_days, streak_start, back_from}},
#    "archive": [[id, key, first_seen, last_seen, seen_days, error], ...]}
#
# ``seen_days`` counts the days the failure was seen; ``streak_start`` is the
# first day of the current run of consecutive days. ``back_from`` is the old
# last_seen of a failure that returned from the archive.

# "N/A", "unknown (likely test_x[17])", ... name no function.
_PLACEHOLDER_FN_RE = re.compile(
    r"^(?:n/?a|none|unknown|unidentified|unconfirmed|—|-|\?)(?=\s|\(|$)", re.IGNORECASE,
)
# A trailing " (...)" note the agent appends, e.g. "test_x[17] (most likely; ...)".
_ANNOTATION_RE = re.compile(r"\s+\(.*\)\s*$")


def canonical_test(test_file: str, test_function: str) -> tuple[str, str]:
    """Normalize the agent's test names so one failure keeps one key.

    The agent writes the same test as ``Class::fn`` one day and ``Class.fn``
    the next, and adds notes like ``(most likely ...)``; each variant used to
    start a new row with a new "Broken since".
    """
    # Test names never contain backticks; the agent's Markdown leaves stray ones.
    tf = (test_file or "").replace("`", "").strip()
    if tf.startswith("./"):
        tf = tf[2:]
    fn = _ANNOTATION_RE.sub("", (test_function or "").replace("`", "").strip())
    if _PLACEHOLDER_FN_RE.match(fn):
        fn = ""
    return tf, fn.replace("::", ".")


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


def _id_number(entry_id: str) -> int:
    try:
        return int(entry_id[len(ID_PREFIX):])
    except (TypeError, ValueError):
        return 10**9


def _absorb(active: dict, entry: dict) -> None:
    """Add ``entry`` under its canonical key, merging with an existing one."""
    tf, fn = canonical_test(entry.get("test_file") or "", entry.get("test_function") or "")
    if not tf and not fn:
        return
    entry = {**entry, "test_file": tf, "test_function": fn}
    key = f"{tf}::{fn}"
    old = active.get(key)
    if old is None:
        active[key] = entry
        return
    if entry.get("last_seen", "") >= old.get("last_seen", ""):
        newer, older = entry, old
    else:
        newer, older = old, entry
    merged = {**older, **newer}
    merged["first_seen"] = min(old.get("first_seen") or "9", entry.get("first_seen") or "9")
    merged["seen_days"] = max(old.get("seen_days", 1), entry.get("seen_days", 1))
    ids = [i for i in (old.get("id"), entry.get("id")) if i]
    if ids:
        merged["id"] = min(ids, key=_id_number)
    active[key] = merged


def upgrade_state(raw: dict) -> dict:
    """Return a version-2 state from any stored layout, keys re-normalized."""
    if raw.get("version") == STATE_VERSION:
        old_active = [e for e in (raw.get("active") or {}).values() if isinstance(e, dict)]
        archive = [a for a in raw.get("archive") or [] if isinstance(a, list) and len(a) >= 6]
        next_id = int(raw.get("next_id") or 1)
    else:
        # Version 1 kept only first/last seen: assume one day seen, or two if
        # they differ, and no streak.
        old_active = []
        for e in raw.values():
            if not isinstance(e, dict):
                continue
            first, last = e.get("first_seen", ""), e.get("last_seen", "")
            e = {k: v for k, v in e.items() if k not in ("status", "run_started_at")}
            e["seen_days"] = 1 if first == last else 2
            e["streak_start"] = last
            old_active.append(e)
        archive, next_id = [], 1
    active: dict = {}
    for e in old_active:
        _absorb(active, e)
    state = {"version": STATE_VERSION, "next_id": next_id, "active": active, "archive": archive}
    for e in sorted(active.values(), key=lambda e: (e.get("first_seen", ""), e.get("test_file", ""))):
        if not e.get("id"):
            e["id"] = _next_id(state)
    return state


def load_state(body: str) -> dict:
    """Recover the persisted tracker state (any version, legacy marker too)."""
    return upgrade_state(_load_raw_state(body))


def _next_id(state: dict) -> str:
    n = state["next_id"]
    state["next_id"] = n + 1
    return f"{ID_PREFIX}{n}"


def merge_state(state: dict, rows: list[dict], today: str, detail_base: str) -> dict:
    """Merge today's failures into the tracker; first_seen is kept while it fails.

    - Active key → keep ``first_seen``; on a new day add one to ``seen_days``
      and restart the streak unless it was also seen yesterday.
    - Archived key → back to active with its old ID, dated today, with
      ``back_from`` set to the day it was last seen before.
    - New key → new ID, ``first_seen = last_seen = today``.
    - Absent key → untouched → renders "quiet".
    """
    active, archive = state["active"], state["archive"]
    for r in rows:
        tf, fn = canonical_test(r.get("test_file") or "", r.get("test_function") or "")
        if not tf and not fn:
            continue
        key = f"{tf}::{fn}"
        job_id = r.get("job_id")
        latest = {
            "test_file": tf,
            "test_function": fn,
            "cluster": (r.get("cluster") or "").strip(),
            "error": _one_line(r.get("error") or ""),
            "job_url": (r.get("job_url") or "").strip(),
            "job_id": job_id,
            "detail_url": f"{detail_base}{job_id}" if detail_base and job_id else "",
        }
        if r.get("fix_pr"):
            latest["fix_pr"] = r["fix_pr"]
        e = active.get(key)
        if e is None:
            back = next((a for a in archive if a[1] == key), None)
            if back is not None:
                archive.remove(back)
                e = {"id": back[0], "back_from": back[3]}
            else:
                e = {"id": _next_id(state)}
            e.update(first_seen=today, last_seen="", seen_days=0, streak_start=today)
            active[key] = e
        prev = e.get("last_seen", "")
        if prev != today:
            e["seen_days"] = e.get("seen_days", 0) + 1
            if not (prev and prev < today and _days_between(prev, today) == 1):
                e["streak_start"] = today
        e.update(latest)
        e["last_seen"] = today
    return state


def _archive_row(entry: dict) -> list:
    key = f"{entry.get('test_file', '')}::{entry.get('test_function', '')}"
    err = entry.get("error") or "—"
    if len(err) > 80:
        err = err[:79].rstrip() + "…"
    return [entry.get("id", ""), key, entry.get("first_seen", ""),
            entry.get("last_seen", ""), entry.get("seen_days", 1), err]


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
        a for a in state["archive"] if _days_between(a[3], today) <= ARCHIVE_KEEP_DAYS
    ]
    return len(stale), before - len(state["archive"])


_FIX_RE = re.compile(
    r"In-flight fix:[*\s]*✅[`*\s]*\[([^\]]+)\]\((https://github\.com/[^)\s]+/pull/\d+)\)"
)


def fix_prs_by_job(analyses: list[dict]) -> dict[str, str]:
    """Map job ID → the in-flight fix PR its daily analysis found, as a link."""
    out: dict[str, str] = {}
    for ja in analyses:
        m = _FIX_RE.search(ja.get("analysis") or "")
        if m and ja.get("job_id"):
            out[str(ja["job_id"])] = f"[{m.group(1)}]({m.group(2)})"
    return out


# ---------------------------------------------------------------------------
# 4. Rendering
# ---------------------------------------------------------------------------

def _one_line(text: str, limit: int = 110) -> str:
    flat = " ".join((text or "").split()).replace("|", "\\|")
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    return flat or "—"


def _cell(text: str) -> str:
    return _one_line(text, limit=80)


def _days_between(start: str, end: str) -> int:
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
        return max((d1 - d0).days, 0)
    except (ValueError, TypeError):
        return 0


def classify(entry: dict, today: str) -> str:
    """``broken`` / ``intermittent`` / ``new`` / ``quiet`` (see render_body)."""
    if entry.get("last_seen") != today:
        return "quiet"
    if _days_between(entry.get("streak_start") or today, today) >= 1:
        return "broken"
    if entry.get("seen_days", 1) > 1 or entry.get("back_from"):
        return "intermittent"
    return "new"


def _state_cell(entry: dict, today: str) -> str:
    kind = classify(entry, today)
    if kind == "broken":
        return f"🔴 broken {_days_between(entry['streak_start'], today) + 1}d"
    if kind == "intermittent":
        return "🟡 intermittent"
    if kind == "new":
        return "🆕 new"
    return f"⚪ quiet {_days_between(entry.get('last_seen', ''), today)}d"


def _seen_cell(entry: dict) -> str:
    span = _days_between(entry.get("first_seen", ""), entry.get("last_seen", "")) + 1
    txt = f"{entry.get('seen_days', 1)}/{span}d"
    if entry.get("back_from"):
        txt += f" · back (was quiet since {entry['back_from']})"
    return txt


def _test_cell(test_file: str, test_function: str) -> str:
    name = test_file or "—"
    if test_function:
        name += f"::{test_function}"
    return f"`{name}`"


def _render_row(entry: dict, today: str) -> str:
    job_url = entry.get("job_url") or ""
    job = f"[job]({job_url})" if job_url else "—"
    detail = f"[详情]({entry['detail_url']})" if entry.get("detail_url") else "—"
    return (
        f"| {entry.get('id', '—')} | {_state_cell(entry, today)} "
        f"| {entry.get('first_seen', '?')} | {entry.get('last_seen', '?')} "
        f"| {_seen_cell(entry)} "
        f"| {_test_cell(entry.get('test_file', ''), entry.get('test_function', ''))} "
        f"| {_cell(entry.get('cluster') or '—')} | {entry.get('error') or '—'} "
        f"| {entry.get('fix_pr') or '—'} | {job} | {detail} |"
    )


_TABLE_HEADER = (
    "| ID | State | Broken since | Last seen | Seen | Test | Cluster | Error "
    "| Fix PR | Job | Detail |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|"
)

_ARCHIVE_HEADER = (
    "| ID | Test | Broken since | Last seen | Seen | Error |\n"
    "|---|---|---|---|---|---|"
)

_KIND_ORDER = {"broken": 0, "intermittent": 1, "new": 2}


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
    """Render a single workflow Failure Tracker issue body (state blob + tables)."""
    today = snapshot_utc.split(" ")[0]
    entries = list(state["active"].values())
    failing = [e for e in entries if e.get("last_seen") == today]
    quiet = [e for e in entries if e.get("last_seen") != today]
    failing.sort(key=lambda e: (
        _KIND_ORDER[classify(e, today)], e.get("streak_start", ""),
        -e.get("seen_days", 1), e.get("first_seen", ""), e.get("test_file", ""),
    ))
    quiet.sort(key=lambda e: (e.get("last_seen", ""), e.get("first_seen", "")), reverse=True)
    archive = sorted(state["archive"], key=lambda a: (a[3], a[2]), reverse=True)
    counts = {k: sum(1 for e in failing if classify(e, today) == k) for k in _KIND_ORDER}

    lines = [
        BODY_START,
        _state_blob(state),
        f"# {display} — Failure Tracker",
        "",
        f"_Maintained by amd-bot · workflow `{workflow}` (scheduled runs on `main`) · "
        f"last updated {snapshot_utc}_",
        "",
        f"**{counts['broken']} broken · {counts['intermittent']} intermittent · "
        f"{counts['new']} new · {len(quiet)} quiet · {len(archive)} archived**",
        "",
        f"This tracker lists the test failures of `{workflow}` and how long each "
        "has been red; a failure keeps its **Broken since** date for as long as it "
        f"keeps failing. It complements the bot's [daily report]({bot_daily_url}) "
        "(a rolling recent-days view) — click **Detail** on any row to jump to the "
        "full analysis (stack trace, suspected commits, in-flight fixes) in that "
        "day's daily report.",
        "",
        "- **State**: 🔴 broken = failed in the latest scan and on every day of the "
        "current streak (its length is shown) · 🟡 intermittent = failed in the "
        "latest scan, and on earlier days, but not the day before · 🆕 new = first "
        "seen in the latest scan · ⚪ quiet = not in the latest scan (fixed, or "
        "simply did not re-run — check **Last seen**).",
        "- **Seen** = days it failed / days from **Broken since** to **Last seen**.",
        "- **Fix PR** = the in-flight fix the daily analysis found, if any.",
        f"- **ID** is never reused, so it can be cited (\"AMD tracker {ID_PREFIX}12\"). "
        f"A failure quiet for over {RETENTION_DAYS} days moves to **Archived**, one "
        "line each, and keeps its ID if it comes back.",
        "",
        "## Currently failing (latest scan)",
        "",
    ]
    if failing:
        lines += [_TABLE_HEADER] + [_render_row(e, today) for e in failing] + [""]
    else:
        lines += [f"_No `{workflow}` failures observed in the latest scan._", ""]

    if quiet:
        lines += [
            "<details><summary><b>⚪ Quiet / no longer observed failing "
            f"({len(quiet)})</b> · click to expand</summary>",
            "",
            _TABLE_HEADER,
        ]
        lines += [_render_row(e, today) for e in quiet]
        lines += ["", "</details>", ""]

    if archive:
        lines += [
            f"<details><summary><b>🗄️ Archived: not seen for over {RETENTION_DAYS} days "
            f"({len(archive)})</b> · click to expand</summary>",
            "",
            _ARCHIVE_HEADER,
        ]
        for a_id, key, first, last, seen, err in (a[:6] for a in archive):
            tf, _, fn = key.partition("::")
            span = _days_between(first, last) + 1
            lines.append(
                f"| {a_id} | {_test_cell(tf, fn)} | {first} | {last} | {seen}/{span}d | {err} |"
            )
        lines += ["", "</details>", ""]

    lines += ["---", f"*Auto-generated by amd-bot · {snapshot_utc}*", BODY_END]
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
            state["archive"].sort(key=lambda a: (a[3], a[2]))
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
        headers=gh_headers(token), json={"title": title},
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


def update_one_workflow(
    token: str,
    workflow: str,
    cfg: dict,
    bot_repo: str,
    daily_issue_num: int | None,
    rows: list[dict],
    snapshot_utc: str,
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
    state = merge_state(state, rows, today, detail_base)
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
            timeout_secs=int(os.environ.get("TRACKER_AGENT_TIMEOUT_SECS", "900")),
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

    # The fix PR comes from the job's own analysis, by code, not the agent.
    for wf, wf_rows in by_wf.items():
        fixes = fix_prs_by_job(wf_analyses.get(wf, []))
        for r in wf_rows:
            fix = fixes.get(str(r.get("job_id")))
            if fix:
                r["fix_pr"] = fix

    results: dict[str, int | None] = {}
    for wf, cfg in TRACKED_WORKFLOWS.items():
        try:
            results[wf] = update_one_workflow(
                token, wf, cfg, bot_repo, issue_num, by_wf[wf], snapshot_utc,
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
