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
  (``Task: Failure Tracker Data`` in ``agent/CLAUDE.md``). It reads the SAME
  per-job analyses the daily report is built from, and emits ONLY a compact
  JSON array (one row per failing test, tagged with its ``workflow``). Because
  its output is tiny and singular — not appended to the giant Daily
  Cross-Workflow Summary prose — the model does not drop it (the earlier
  "append to the summary output" approach was observed to silently omit the
  block under output pressure).

- **State (first_seen / duration / dedup)** is owned by deterministic code
  here. Per ``(test_file, test_function)`` within each workflow's issue we
  keep ``first_seen`` verbatim in a hidden JSON blob in the issue body and
  NEVER recompute it. An LLM is never asked to remember dates across days.

Decoupling from the daily report
---------------------------------

The tracker is NOT run inline by each per-workflow monitor job (that would
race: up to 7 summary rebuilds per tick, each seeing partial data). Instead a
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
    # "pr-test-amd-rocm720.yml": {
    #     "title": "[Failure Tracker] PR Test ROCm 7.2 (AMD)",
    #     "legacy_titles": [],
    #     "display": "PR Test ROCm 7.2 (AMD)",
    # },
    # "nightly-test-amd.yml": {
    #     "title": "[Failure Tracker] Nightly Test (AMD)",
    #     "legacy_titles": [],
    #     "display": "Nightly Test (AMD)",
    # },
}

TRACKER_LABEL = "ci-failure-tracker"

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

def _row_key(r: dict) -> str:
    return f"{(r.get('test_file') or '').strip()}::{(r.get('test_function') or '').strip()}"


def load_state(body: str) -> dict:
    """Recover the persisted tracker state, with legacy-marker read-compat."""
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


def merge_state(state: dict, rows: list[dict], today: str, detail_base: str) -> dict:
    """Merge today's failures into the tracker; first_seen is preserved forever.

    - Existing key → keep ``first_seen``, bump ``last_seen`` to today, refresh
      latest-known fields.
    - New key → ``first_seen = last_seen = today``.
    - Absent key → untouched (never deleted, never re-dated) → renders "quiet".
    """
    for r in rows:
        key = _row_key(r)
        if key == "::":
            continue
        job_id = r.get("job_id")
        detail_url = f"{detail_base}{job_id}" if detail_base and job_id else ""
        latest = {
            "test_file": (r.get("test_file") or "").strip(),
            "test_function": (r.get("test_function") or "").strip(),
            "cluster": (r.get("cluster") or "").strip(),
            "error": _one_line(r.get("error") or ""),
            "status": (r.get("status") or "").strip(),
            "job_url": (r.get("job_url") or "").strip(),
            "job_id": job_id,
            "detail_url": detail_url,
            "run_started_at": (r.get("run_started_at") or "").strip(),
            "last_seen": today,
        }
        if key in state:
            state[key].update(latest)
        else:
            latest["first_seen"] = today
            state[key] = latest
    return state


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


def _render_row(entry: dict, today: str) -> str:
    first = entry.get("first_seen", "?")
    last = entry.get("last_seen", "?")
    dur = _days_between(first, last)
    dur_txt = "new" if dur == 0 and last == today else f"{dur}d"
    if last == today:
        state = "🔴 failing"
    else:
        state = f"⚪ quiet {_days_between(last, today)}d"
    tf = entry.get("test_file") or "—"
    fn = entry.get("test_function") or "—"
    cluster = _cell(entry.get("cluster") or "—")
    err = entry.get("error") or "—"
    job_url = entry.get("job_url") or ""
    job = f"[job]({job_url})" if job_url else "—"
    detail = f"[详情]({entry['detail_url']})" if entry.get("detail_url") else "—"
    return (
        f"| {first} | {last} | {dur_txt} | {state} "
        f"| `{tf}` | `{fn}` | {cluster} | {err} | {job} | {detail} |"
    )


_TABLE_HEADER = (
    "| Broken since | Last seen | Duration | State "
    "| Test File | Test Function | Cluster | Error | Job | Detail |\n"
    "|---|---|---|---|---|---|---|---|---|---|"
)


def render_body(
    workflow: str,
    display: str,
    state: dict,
    snapshot_utc: str,
    bot_daily_url: str,
) -> str:
    """Render a single workflow Failure Tracker issue body (state blob + tables)."""
    today = snapshot_utc.split(" ")[0]
    entries = list(state.values())
    failing = [e for e in entries if e.get("last_seen") == today]
    quiet = [e for e in entries if e.get("last_seen") != today]
    failing.sort(key=lambda e: (e.get("first_seen", ""), e.get("test_file", "")))
    quiet.sort(key=lambda e: (e.get("last_seen", ""), e.get("first_seen", "")), reverse=True)

    state_blob = (
        f"{STATE_PREFIX}\n{json.dumps(state, ensure_ascii=False, indent=0)}\n{STATE_SUFFIX}"
    )

    lines = [
        BODY_START,
        state_blob,
        f"# {display} — Failure Tracker",
        "",
        f"_Maintained by amd-bot · workflow `{workflow}` · last updated "
        f"{snapshot_utc} · {len(failing)} failing in latest scan · "
        f"{len(entries)} tracked total_",
        "",
        f"This is a **long-lived failure tracker** for `{workflow}`: once a "
        "test failure is recorded its **Broken since** date is preserved "
        "indefinitely (even months), so you can always see how long something "
        "has been red. It complements the bot's "
        f"[daily report]({bot_daily_url}) (a rolling recent-days view) — click "
        "**Detail** on any row to jump to the full analysis (stack trace, "
        "suspected commits, in-flight fixes) in that day's daily report.",
        "",
        "- **Duration** = observed broken span (`Last seen − Broken since`), not time-since-first-seen.",
        "- **State** 🔴 = seen failing in the latest scan · ⚪ = not seen in the latest scan "
        "(may be fixed, or simply did not re-run — check **Last seen**).",
        "",
    ]

    if failing:
        lines += ["## 🔴 Currently failing (latest scan)", "", _TABLE_HEADER]
        lines += [_render_row(e, today) for e in failing]
        lines.append("")
    else:
        lines += [
            "## 🔴 Currently failing (latest scan)",
            "",
            f"_No `{workflow}` failures observed in the latest scan._",
            "",
        ]

    if quiet:
        lines += [
            "<details><summary><b>⚪ Quiet / no longer observed failing "
            f"({len(quiet)})</b> · click to expand</summary>",
            "",
            _TABLE_HEADER,
        ]
        lines += [_render_row(e, today) for e in quiet]
        lines += ["", "</details>", ""]

    lines += ["---", f"*Auto-generated by amd-bot · {snapshot_utc}*", BODY_END]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Upstream issue find / create / update (per workflow)
# ---------------------------------------------------------------------------

def find_tracker_issue(token: str, title: str) -> int | None:
    """Find an open issue with EXACTLY this title on the upstream repo."""
    q = f'repo:{REPO} is:issue is:open in:title "{title}"'
    resp = requests.get(
        "https://api.github.com/search/issues",
        headers=gh_headers(token), params={"q": q, "per_page": 20},
    )
    resp.raise_for_status()
    for item in resp.json().get("items", []):
        if item.get("title") == title:
            return item["number"]
    return None


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

    new_body = render_body(
        workflow, cfg["display"], state, snapshot_utc, bot_daily_url,
    )
    if new_body.strip() == (issue.get("body") or "").strip():
        log.info("[%s] tracker #%d unchanged; skipping PATCH", workflow, issue_num)
        return issue_num
    update_issue_body(token, REPO, issue_num, new_body)
    log.info(
        "[%s] updated tracker #%d (%d tracked, %d failing this scan)",
        workflow, issue_num, len(state), len(rows),
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

    results: dict[str, int | None] = {}
    for wf, cfg in TRACKED_WORKFLOWS.items():
        try:
            results[wf] = update_one_workflow(
                token, wf, cfg, bot_repo, issue_num, by_wf[wf], snapshot_utc,
            )
        except Exception as exc:
            log.warning("[%s] tracker update failed (%s)", wf, exc)
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
        log.info("%s → issue %s", wf, f"#{num}" if num else "(skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
