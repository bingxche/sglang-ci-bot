"""ci-autofix's state, kept in one comment on the daily issue (R8), and the
queue notice that @-mentions ``NOTIFY_USER``."""

import json
import logging
import math
from datetime import datetime, timedelta

from autofix_config import MAX_ATTEMPTS, MAX_COMMENT_CHARS, MAX_PREVIOUS, MAX_STATE_DIFF, NOTIFY_USER
from monitor_ci import get_issue_comments
from utils import gh_get, gh_headers

log = logging.getLogger("autofix")


LOG_MARKER = "<!-- ci-autofix-log -->"
STATE_PREFIX = "<!-- ci-autofix-state:v1 "
QUEUE_NOTICE_MARKER = "<!-- ci-autofix-queue-notice -->"


def hidden_json(data) -> str:
    """JSON safe inside an HTML comment."""
    return json.dumps(data, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")


def load_state(body: str) -> dict:
    start = (body or "").rfind(STATE_PREFIX)
    if start < 0:
        return {}
    end = body.find(" -->", start)
    try:
        state = json.loads(body[start + len(STATE_PREFIX):end])
    except ValueError:
        log.warning("autofix state is not valid JSON; starting over")
        return {}
    return state if isinstance(state, dict) else {}


def new_day_state(state: dict, today: str, now: datetime) -> dict:
    """Carry *state* into *today*: keep problems and recent starts, start a new event log."""
    state = {
        "date": today,
        "problems": dict(state.get("problems") or {}),
        "entered": [e for e in state.get("entered") or []
                    if datetime.fromisoformat(e["at"]) > now - timedelta(hours=48)],
        "notified": list(state.get("notified") or []),
        "notices": [n for n in state.get("notices") or []
                    if datetime.fromisoformat(n["at"]) > now - timedelta(days=30)],
        "queue": list(state.get("queue") or []),
        "events": list(state.get("events") or []) if state.get("date") == today else [],
    }
    for pid, p in list(state["problems"].items()):
        age = now - datetime.fromisoformat(p["updated"])
        # Finished problems stay a month (attempt counts); skipped ones only while the report has them.
        if (p.get("status") in ("opened", "gave-up") and age > timedelta(days=30)) or \
                (not p.get("status") and age > timedelta(days=2)):
            del state["problems"][pid]
    return state


def add_event(state: dict, now: datetime, pid: str, event: str, detail: str = "") -> None:
    state["events"].append({"at": now.isoformat(timespec="seconds"), "id": pid,
                            "event": event, "detail": detail[:300]})
    state["events"] = state["events"][-150:]


_STATUS_TEXT = {
    "ready": "🧪 fix passed the checks; pushing it",
    "dispatched": "🔄 fix on a branch, re-running the job",
    "opened": "✅ draft PR opened",
    "gave-up": "❌ gave up",
    "retry": "🔁 will try again",
    "waiting": "⏳ waiting for its hour",
}


def render_log_comment(state: dict, mode: str, cap: int, now: datetime) -> str:
    """The log comment with the state; shortened (old events, long texts) to fit GitHub's limit."""
    for p in state["problems"].values():
        if p.get("previous"):
            p["previous"] = p["previous"][-MAX_PREVIOUS:]
        if p.get("attempt", {}).get("diff"):
            p["attempt"]["diff"] = p["attempt"]["diff"][-MAX_STATE_DIFF:]
    body = _render_log_comment(state, mode, cap, now)
    while len(body) > MAX_COMMENT_CHARS and state["events"]:
        del state["events"][: max(1, len(state["events"]) // 4)]
        body = _render_log_comment(state, mode, cap, now)
    if len(body) > MAX_COMMENT_CHARS:
        # Second attempts lose their notes and diffs (only shown to the agent), never their state.
        for p in state["problems"].values():
            p.pop("previous", None)
            if p.get("attempt", {}).get("diff"):
                p["attempt"]["diff"] = p["attempt"]["diff"][-1_000:]
        body = _render_log_comment(state, mode, cap, now)
    if len(body) > MAX_COMMENT_CHARS:
        log.error("The autofix log comment is %d characters, over GitHub's limit", len(body))
    return body


def _render_log_comment(state: dict, mode: str, cap: int, now: datetime) -> str:
    lines = [
        LOG_MARKER,
        "## AMD CI autofix",
        "",
        f"Mode `{mode}`: at most {cap} problems start per hour; each gets {MAX_ATTEMPTS} attempts. "
        f"A draft PR is opened only after the failing job passes on AMD GPUs. "
        f"Last updated {now.strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
    ]
    # Problems autofix worked on; the ones it skipped are in the decisions below.
    active = {pid: p for pid, p in state["problems"].items() if p.get("status") and (
              p["status"] in ("ready", "dispatched", "retry") or p.get("updated", "").startswith(state["date"]))}
    if active:
        lines += ["| Problem | Status | Attempts | Detail |", "|---|---|---|---|"]
        for pid, p in sorted(active.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True):
            detail = p.get("pr") or p.get("run_url") or p.get("reason") or ""
            if detail.startswith("http"):
                detail = f"[link]({detail})"
            lines.append(f"| <!-- {pid} --> {_cell(p.get('name') or pid)} | {_STATUS_TEXT.get(p.get('status'), p.get('status', ''))} "
                         f"| {p.get('attempts', 0)} | {_cell(detail)} |")
        lines.append("")
    if state.get("queue"):
        lines.append(f"**Waiting** ({len(state['queue'])}): "
                     + "; ".join(f"<!-- {q['id']} -->{_cell(q.get('name') or q['id'])} ({q['jobs']} failed jobs)"
                                 for q in state["queue"]))
        lines.append("")
    if state["events"]:
        lines += ["<details><summary>Today's decisions</summary>", ""]
        for e in state["events"]:
            lines.append(f"- {e['at'][11:16]} <!-- {e['id']} --> {_cell(state['problems'].get(e['id'], {}).get('name') or e['id'])}: "
                         f"{e['event']}" + (f" — {_cell(e['detail'])}" if e.get("detail") else ""))
        lines += ["", "</details>", ""]
    lines.append(f"{STATE_PREFIX}{hidden_json(state)} -->")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def render_queue_notice(waiting: list[dict], cap: int, now: datetime, issue_url: str) -> str:
    """The comment that @-mentions NOTIFY_USER when problems first wait (GitHub emails the mention)."""
    lines = [
        QUEUE_NOTICE_MARKER,
        f"@{NOTIFY_USER} AMD CI autofix: {len(waiting)} problem(s) are waiting, because at most "
        f"{cap} start per hour.",
        "",
        "| Problem | Failed jobs | Failing since | Expected start |",
        "|---|---|---|---|",
    ]
    for q in waiting:
        start = now + timedelta(hours=math.ceil((q["position"] + 1) / max(cap, 1)))
        lines.append(f"| <!-- {q['id']} --> {_cell(q.get('name') or q['id'])} | {q['jobs']} "
                     f"| {q.get('failing_since') or 'unknown'} | ~{start.strftime('%H:00 UTC')} |")
    lines += [
        "",
        f"Expected start assumes no new problem ranks higher. [Daily report]({issue_url}). "
        "Raise `AUTOFIX_MAX_PER_HOUR` to clear the queue sooner, or set `AUTOFIX_MODE=off` to stop autofix.",
    ]
    return "\n".join(lines)


def find_issue(token: str, bot_repo: str, date_str: str) -> int | None:
    """The daily issue of *date_str*, open or closed."""
    resp = gh_get(f"https://api.github.com/repos/{bot_repo}/issues", headers=gh_headers(token),
                  params={"state": "all", "labels": "ci-monitor", "per_page": 30})
    resp.raise_for_status()
    title = f"[CI Monitor] Daily Report - {date_str}"
    return next((i["number"] for i in resp.json() if i["title"] == title), None)


def find_log_comment(token: str, bot_repo: str, issue_num: int) -> dict | None:
    return next((c for c in get_issue_comments(token, bot_repo, issue_num)
                 if LOG_MARKER in (c.get("body") or "")), None)


def read_state(token: str, bot_repo: str, issue_num: int, now: datetime) -> tuple[dict, dict | None]:
    """(state, today's log comment). With no comment today, the latest earlier day's state."""
    comment = find_log_comment(token, bot_repo, issue_num)
    if comment:
        return load_state(comment["body"]), comment
    for back in range(1, 8):
        num = find_issue(token, bot_repo, (now - timedelta(days=back)).strftime("%Y-%m-%d"))
        earlier = find_log_comment(token, bot_repo, num) if num else None
        if earlier:
            return load_state(earlier["body"]), None
    return {}, None
