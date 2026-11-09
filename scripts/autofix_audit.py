#!/usr/bin/env python3
"""Check ci-autofix against its done criteria.

``recipes`` (E2): of the AMD scheduled jobs that failed in the daily reports
of the last N days, how many have a dispatch recipe (``autofix.recipe_for()``),
and which do not.

``prs`` (D0–D4): every draft PR the bot opened on sgl-project/sglang, and
the autofix state on the daily issues:
  - D0: no hour started more than ``AUTOFIX_MAX_PER_HOUR`` problems, and
    every waiting problem got a queue notice;
  - D1: a dispatched run on a commit of the PR, of the same workflow and
    job, shows every test of the problem passed without a retry;
  - D2: the Motivation names a confidence label; a diff with skips has a
    ``### Workaround`` section (and the ``workaround`` label, if the repo has one);
  - D2b: the title passes the sglang title check, every template section
    is there, and the prose is short;
  - D3: no forbidden path, and the review agent said PASS;
  - D4: a merged PR's tests passed in the next two scheduled runs.

Exit status 1 when anything is violated.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autofix  # noqa: E402
from daily_cross_workflow_summary import collect_workflow_analyses  # noqa: E402
from monitor_ci import MONITORED_WORKFLOWS, get_issue_comments  # noqa: E402
from utils import SGLANG_REPO_PATH, gh_get, gh_headers  # noqa: E402

log = logging.getLogger("autofix-audit")


def daily_issues(token: str, bot_repo: str, days: int, now: datetime) -> list[tuple[str, int]]:
    resp = gh_get(f"https://api.github.com/repos/{bot_repo}/issues", headers=gh_headers(token),
                  params={"state": "all", "labels": "ci-monitor", "per_page": 100})
    resp.raise_for_status()
    wanted = {(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days)}
    out = []
    for issue in resp.json():
        m = re.match(r"^\[CI Monitor\] Daily Report - (\d{4}-\d{2}-\d{2})$", issue["title"])
        if m and m.group(1) in wanted:
            out.append((m.group(1), issue["number"]))
    return sorted(out)


# ---------------------------------------------------------------------------
# E2: recipe coverage
# ---------------------------------------------------------------------------

def recipe_coverage(jobs: list[tuple[str, str]], workflow_yaml) -> dict:
    """*jobs*: (workflow, job name) of every failed scheduled job. Counted once per job family."""
    families: dict[tuple[str, str], str] = {}
    for wf, name in jobs:
        families.setdefault((wf, autofix.job_family(name)), name)
    covered, missing = [], []
    for (wf, _), name in sorted(families.items()):
        recipe, why = autofix.recipe_for(wf, name, workflow_yaml)
        (covered if recipe else missing).append((wf, name, why))
    in_scope = [x for x in covered + missing if x[0] in autofix.AUTOFIX_WORKFLOWS]
    return {
        "families": len(families),
        "covered": covered,
        "missing": missing,
        "in_scope": len(in_scope),
        "in_scope_covered": sum(1 for x in in_scope if x in covered),
    }


def render_coverage(cov: dict, days: int, instances: Counter) -> str:
    total, n = cov["families"], len(cov["covered"])
    lines = [
        f"# Dispatch recipe coverage, last {days} days of daily reports",
        "",
        f"- All monitored AMD scheduled workflows: {n}/{total} failed jobs have a recipe "
        f"({100 * n / max(total, 1):.0f}%).",
        f"- Workflows autofix fixes ({', '.join(autofix.AUTOFIX_WORKFLOWS)}): "
        f"{cov['in_scope_covered']}/{cov['in_scope']} "
        f"({100 * cov['in_scope_covered'] / max(cov['in_scope'], 1):.0f}%).",
        "",
        "A job is counted once however many days and partitions it failed in.",
        "",
        "## Without a recipe",
        "",
        "| Workflow | Job | Failures | Why |",
        "|---|---|---|---|",
    ]
    for wf, name, why in cov["missing"]:
        lines.append(f"| {wf} | `{name}` | {instances[(wf, autofix.job_family(name))]} | {why} |")
    lines += ["", "## With a recipe", ""]
    by_wf = Counter(wf for wf, _, _ in cov["covered"])
    lines += [f"- {wf}: {k} jobs" for wf, k in sorted(by_wf.items())]
    return "\n".join(lines) + "\n"


def cmd_recipes(args, token: str) -> int:
    now = datetime.now(timezone.utc)
    sglang = Path(args.sglang)
    ref = args.ref

    def workflow_yaml(workflow: str) -> str | None:
        r = subprocess.run(["git", "show", f"{ref}:.github/workflows/{workflow}"], cwd=sglang,
                           capture_output=True, text=True, encoding="utf-8")
        return r.stdout if r.returncode == 0 else None

    jobs: list[tuple[str, str]] = []
    for date, num in daily_issues(token, args.bot_repo, args.days, now):
        log.info("Reading %s (#%d)", date, num)
        for wf, jas in collect_workflow_analyses(token, args.bot_repo, num).items():
            jobs += [(wf, ja["job_name"]) for ja in jas if wf in MONITORED_WORKFLOWS]
    instances = Counter((wf, autofix.job_family(name)) for wf, name in jobs)
    cov = recipe_coverage(jobs, workflow_yaml)
    print(render_coverage(cov, args.days, instances))
    return 0


# ---------------------------------------------------------------------------
# D0–D4: the bot's PRs and the hourly cap
# ---------------------------------------------------------------------------

def hourly_violations(entered: list[dict], cap: int) -> list[str]:
    """Any 60-minute window with more than *cap* starts."""
    times = sorted({(e["id"], e["at"]) for e in entered}, key=lambda x: x[1])
    out = []
    for i, (_, at) in enumerate(times):
        t0 = datetime.fromisoformat(at)
        window = [x for x in times[i:] if datetime.fromisoformat(x[1]) < t0 + timedelta(hours=1)]
        if len(window) > cap:
            out.append(f"{len(window)} problems started within an hour of {at}: "
                       + ", ".join(x[0] for x in window))
    return list(dict.fromkeys(out))


def audit_states(states: list[dict], cap: int) -> list[str]:
    entered = [e for s in states for e in s.get("entered") or []]
    notified = {pid for s in states for n in s.get("notices") or [] for pid in n.get("ids") or []}
    violations = [f"D0: {v}" for v in hourly_violations(entered, cap)]
    for s in states:
        for q in s.get("queue") or []:
            if q["id"] not in notified:
                violations.append(f"D0: {q['id']} waited on {s.get('date')} without a queue notice")
    return list(dict.fromkeys(violations))


def pr_sections(body: str) -> dict[str, str]:
    parts = re.split(r"^## (.+?)\s*$", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def audit_pr_text(pr: dict, files: list[dict], template: str, label_exists: bool) -> list[str]:
    """D2, D2b and the path part of D3, from the PR alone."""
    n = pr["number"]
    body = pr.get("body") or ""
    v = []
    sections = pr_sections(body)
    motivation = sections.get("Motivation", "")
    if not re.search(r"\[(?:" + "|".join(autofix.CONFIDENCE_LABELS) + r")\]", motivation):
        v.append(f"D2: #{n} Motivation names no confidence label")
    diff = "\n".join(f"diff --git a/{f['filename']} b/{f['filename']}\n{f.get('patch') or ''}" for f in files)
    if autofix.workaround_signals(diff):
        if "### Workaround" not in body:
            v.append(f"D2: #{n} skips or loosens a test but has no ### Workaround section")
        labels = {lb["name"] for lb in pr.get("labels") or []}
        if label_exists and "workaround" not in labels:
            v.append(f"D2: #{n} is a workaround without the workaround label")
    if autofix.check_title(pr["title"], "root-cause"):
        v.append(f"D2b: #{n} title fails the sglang title check")
    missing = [s for s in autofix.template_sections(template) if s not in sections]
    if missing:
        v.append(f"D2b: #{n} lacks template sections {missing}")
    prose = "\n".join(sections.get(s, "") for s in ("Motivation", "Modifications", "Accuracy Tests",
                                                    "Speed Tests and Profiling"))
    prose = re.sub(r"^Failing on `main`.*$", "", prose, flags=re.MULTILINE)  # code-written job list
    words = autofix.count_words(prose)
    if words > autofix.MAX_BODY_WORDS:
        v.append(f"D2b: #{n} prose has {words} words (at most {autofix.MAX_BODY_WORDS})")
    forbidden = [f["filename"] for f in files if any(p.search(f["filename"]) for p in autofix.FORBIDDEN_PATHS)]
    if forbidden:
        v.append(f"D3: #{n} touches {forbidden}")
    return v


def pr_data(body: str) -> dict:
    start = (body or "").rfind(autofix.PR_DATA_PREFIX)
    if start < 0:
        return {}
    end = body.find(" -->", start)
    try:
        return json.loads(body[start + len(autofix.PR_DATA_PREFIX):end])
    except ValueError:
        return {}


def audit_pr_runs(pr: dict, data: dict, gh: autofix.GitHub) -> list[str]:
    """D1 and D4 for one PR, from its hidden data block."""
    n = pr["number"]
    v = []
    if not data:
        return [f"D1: #{n} has no autofix data block"]
    if data.get("review") != "PASS":
        v.append(f"D3: #{n} review verdict is {data.get('review') or 'missing'}")
    commits = {c["sha"] for c in gh._get(f"pulls/{n}/commits", per_page=100) or []}
    run = gh.run(data["run_id"]) if data.get("run_id") else {}
    if not run or run.get("head_sha") not in commits:
        v.append(f"D1: #{n} has no dispatched run on one of its commits")
    else:
        plan = {"recipe": {"workflow": data["workflow"], "families": data["families"]},
                "tests": data["tests"], "jobs": []}
        rec = {"head_sha": run["head_sha"], "fix": {"fix_kind": data.get("fix_kind")}}
        result = autofix.verify_run(plan, rec, run, gh.jobs(run["id"]), gh.job_log)
        if not result["ok"]:
            v.append(f"D1: #{n} run {run['html_url']}: " + "; ".join(result["reasons"])[:300])
    if pr.get("merged_at"):
        merged = pr["merged_at"]
        families = set(data.get("families") or [])
        checked = 0
        for r in reversed(gh.scheduled_runs(data["workflow"])):
            if r["created_at"] <= merged or checked >= 2:
                continue
            jobs = [j for j in gh.jobs(r["id"]) if autofix.job_family(j["name"]) in families]
            if not jobs:
                continue
            checked += 1
            for f, fn in data.get("tests") or []:
                outcome = next((o for o in (autofix.test_outcome(gh.job_log(j["id"]), f, fn) for j in jobs)
                                if o.status != "not-run"), None)
                if not outcome or outcome.status != "passed":
                    v.append(f"D4: #{n} `{f}::{fn}` {outcome.status if outcome else 'did not run'} "
                             f"in {r['html_url']} after the merge")
    return v


def cmd_prs(args, token: str) -> int:
    now = datetime.now(timezone.utc)
    gh = autofix.GitHub(token)
    violations: list[str] = []

    states = []
    for _, num in daily_issues(token, args.bot_repo, args.days, now):
        for c in get_issue_comments(token, args.bot_repo, num):
            if autofix.LOG_MARKER in (c.get("body") or ""):
                states.append(autofix.load_state(c["body"]))
    violations += audit_states(states, args.max_per_hour)

    template_path = Path(args.sglang) / ".github" / "pull_request_template.md"
    template = template_path.read_text(encoding="utf-8") if template_path.exists() else ""
    label_exists = gh._get("labels/workaround") is not None
    prs = gh.search_prs(f'in:body "amd-bot-autofix:" author:{args.author}')
    for item in prs:
        pr = gh.pull(item["number"])
        if not pr or not autofix._PR_MARKER_RE.search(pr.get("body") or ""):
            continue
        files = gh.pr_files(pr["number"])
        violations += audit_pr_text(pr, files, template, label_exists)
        violations += audit_pr_runs(pr, pr_data(pr.get("body") or ""), gh)

    print(f"# Autofix audit: {len(prs)} bot PR(s), {len(states)} day(s) of state\n")
    print("\n".join(f"- {v}" for v in violations) if violations else "No violations.")
    return 1 if violations else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bot-repo", required=True, help="Bot repo, e.g. bingxche/sglang-ci-bot")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--sglang", default=str(SGLANG_REPO_PATH), help="An sglang clone")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("recipes", help="E2: dispatch recipe coverage")
    rec.add_argument("--ref", default="origin/main", help="Read the workflows at this ref of --sglang")
    prs = sub.add_parser("prs", help="D0–D4: every bot PR")
    prs.add_argument("--author", default=autofix.NOTIFY_USER)
    prs.add_argument("--max-per-hour", type=int,
                     default=int(os.environ.get("AUTOFIX_MAX_PER_HOUR") or autofix.DEFAULT_MAX_PER_HOUR))
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO, stream=sys.stderr)
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        log.error("GitHub token required. Set GH_PAT.")
        return 1
    return cmd_recipes(args, token) if args.cmd == "recipes" else cmd_prs(args, token)


if __name__ == "__main__":
    sys.exit(main())
