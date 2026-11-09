"""Which problems of the daily report ci-autofix tries, and when.

``build_plan()`` runs the checks (scheduled failure on main, fails for sure,
not infrastructure, a dispatch recipe, no fix PR) and returns what an attempt
needs; ``plan_hour()`` starts at most ``AUTOFIX_MAX_PER_HOUR`` problems per
hour and queues the rest."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from autofix_ci import (
    Recipe,
    classify_infra,
    dispatch_inputs,
    fails_for_sure,
    failure_window,
    is_perf,
    job_family,
    job_label,
    recipe_for,
    test_outcome,
)
from autofix_config import AUTOFIX_WORKFLOWS, HISTORY_RUNS, NOT_FIXED
from autofix_github import GitHub
from daily_cross_workflow_summary import (
    collect_workflow_analyses,
    extract_summary_from_body,
    parse_job_cluster_comment,
    parse_suspect_prs_comment,
)
from failure_tracker import deterministic_rows
from utils import get_issue


def take_for_this_hour(
    eligible: list[dict], entered: list[dict], now: datetime, cap: int,
) -> tuple[list[dict], list[dict]]:
    """Split *eligible* plans into those that start now and those that wait.

    Plans are ranked by failed jobs (most first), then by how long they have
    failed (oldest first). *entered* holds ``{"id", "at"}`` for every problem
    that started; those of the last hour use up the cap.
    """
    hour_ago = now - timedelta(hours=1)
    used = sum(1 for e in entered if datetime.fromisoformat(e["at"]) > hour_ago)
    ranked = sorted(eligible, key=lambda p: (-p["failed_jobs"], p.get("failing_since") or "9999"))
    room = max(0, cap - used)
    return ranked[:room], ranked[room:]


_HEADING_ID_RE = re.compile(r"<!--\s*(R\d+(?:\s*·\s*R\d+)*)\s*-->")
_FIX_LINK_RE = re.compile(r"\*\*In-flight fix\*\*:\s*✅\s*\[#(\d+)\]")


def problem_sections(summary: str) -> dict[str, dict]:
    """Per problem ID: its plain-language name and whether the report shows a fix."""
    out: dict[str, dict] = {}
    current: list[str] = []
    for line in summary.splitlines():
        s = line.strip()
        if s.startswith("### ") or (s.startswith("|") and _HEADING_ID_RE.search(s)):
            m = _HEADING_ID_RE.search(s)
            ids = re.findall(r"R\d+", m.group(1)) if m else []
            text = _HEADING_ID_RE.sub("", s.lstrip("#").strip()).strip()
            name = re.split(r" — | \| ", text.strip("| "))[0]
            name = re.sub(r"^(?:🆕|↩️)\s*", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", name)).strip()
            fix = re.search(r"✅ fix (?:open|merged)", s) is not None
            if s.startswith("### "):
                current = ids
            # A heading naming several problems names the group; their rows name each one.
            own_name = len(ids) == 1
            for pid in ids:
                entry = out.setdefault(pid, {"name": pid, "fix": False, "fix_pr": None, "named": False})
                if own_name and not entry["named"]:
                    entry.update(name=name, named=True)
                entry["fix"] = entry["fix"] or (fix and own_name)
            continue
        if s.startswith("## "):
            current = []
        m = _FIX_LINK_RE.search(s)
        if m and len(current) == 1:
            out.setdefault(current[0], {"name": current[0], "fix": False, "fix_pr": None, "named": False})
            out[current[0]]["fix_pr"] = int(m.group(1))
    return out


@dataclass
class Problem:
    id: str
    name: str
    jobs: list[dict]                 # per-job analyses, with "workflow"
    suspect: dict | None = None
    fix_pr: int | None = None
    report_fix: bool = False


def load_problems(token: str, bot_repo: str, issue_num: int) -> list[Problem]:
    summary = extract_summary_from_body(get_issue(token, bot_repo, issue_num).get("body", "")) or ""
    clusters = parse_job_cluster_comment(summary) or {}
    suspects = {s["cluster"]: s for s in parse_suspect_prs_comment(summary)}
    sections = problem_sections(summary)
    job_info = {}
    for wf, jas in collect_workflow_analyses(token, bot_repo, issue_num).items():
        for ja in jas:
            job_info[ja["job_id"]] = {**ja, "workflow": wf}
    problems = []
    for pid, job_ids in clusters.items():
        jobs = [job_info[j] for j in job_ids if j in job_info]
        if not jobs:
            continue
        section = sections.get(pid, {})
        suspect = suspects.get(pid)
        problems.append(Problem(
            pid, section.get("name") or pid, jobs, suspect,
            fix_pr=(suspect or {}).get("fix_pr") or section.get("fix_pr"),
            report_fix=section.get("fix", False),
        ))
    return problems


def run_id_of(run_url: str) -> int:
    return int(re.search(r"/runs/(\d+)", run_url).group(1))


def problem_tests(jobs: list[dict]) -> list[tuple[str, str]]:
    tests = []
    for row in deterministic_rows("", jobs):
        f, fn = row["test_file"].strip("` "), row["test_function"].strip("` ")
        if f.endswith(".py") and fn and fn not in ("-", "—", "N/A", "n/a", "unknown"):
            tests.append((f, fn))
    return list(dict.fromkeys(tests))


def build_plan(p: Problem, gh: GitHub, workflow_yaml, now: datetime) -> tuple[dict | None, str]:
    """The fix plan for *p*, or ``(None, why it is not fixed)``. The order of the
    checks puts the cheap ones first; ``find_fix_pr()`` and the history come last."""
    jobs = []
    for ja in p.jobs:
        if ja["workflow"] not in AUTOFIX_WORKFLOWS:
            continue
        run = gh.run(run_id_of(ja["run_url"]))
        if run.get("event") == "schedule" and run.get("head_branch") == "main":
            jobs.append({**ja, "run": run})
    if not jobs:
        wf = {ja["workflow"] for ja in p.jobs}
        why = "; ".join(NOT_FIXED[w] for w in wf if w in NOT_FIXED)
        return None, "not a scheduled failure on main of an autofix workflow" + (f" ({why})" if why else "")

    tests = problem_tests(jobs)
    if not tests:
        return None, "no failing test function named in the analyses"
    errors = [r["error"] for r in deterministic_rows("", jobs)]
    infra = classify_infra(errors)
    if infra:
        return None, f"infrastructure ({infra})"
    if is_perf([j["job_name"] for j in jobs], errors):
        return None, "performance regression"

    groups: dict[tuple, list[tuple[Recipe, dict]]] = {}
    reasons = []
    for j in jobs:
        recipe, why = recipe_for(j["workflow"], j["job_name"], workflow_yaml)
        if recipe:
            groups.setdefault(recipe.group, []).append((recipe, j))
        else:
            reasons.append(why)
    if not groups:
        return None, "no dispatch recipe: " + "; ".join(dict.fromkeys(reasons))
    # One dispatch per attempt: the group with the most failed jobs, and it must cover every test.
    group = max(groups.values(), key=len)
    group_jobs = [j for _, j in group]
    covered = set(problem_tests(group_jobs))
    if set(tests) - covered:
        return None, "its tests failed in jobs no single dispatch can re-run"

    if p.fix_pr or p.report_fix:
        return None, f"the report shows a fix PR{f' (#{p.fix_pr})' if p.fix_pr else ''}"

    # History: each job of the group, newest scheduled runs first.
    first_fail = last_pass = None
    streak = 0
    by_family: dict[str, dict] = {}
    for j in sorted(group_jobs, key=lambda j: j["run"]["created_at"], reverse=True):
        by_family.setdefault(job_family(j["job_name"]), j)
    for fam, j in list(by_family.items())[:4]:
        job_tests = problem_tests([j])
        statuses: list[tuple[str, dict]] = []
        for run in gh.scheduled_runs(j["workflow"]):
            if len(statuses) >= HISTORY_RUNS:
                break
            match = next((x for x in gh.jobs(run["id"]) if x["name"] == j["job_name"]), None)
            if not match or match.get("conclusion") in (None, "skipped", "cancelled"):
                continue
            if match["conclusion"] == "success":
                status = "passed"
            else:
                outs = [test_outcome(gh.job_log(match["id"]), f, fn) for f, fn in job_tests]
                status = "failed" if outs and all(o.status == "failed" for o in outs) else "other"
            statuses.append((status, {"run_id": run["id"], "run_url": run["html_url"], "sha": run["head_sha"],
                                      "date": run["created_at"][:10], "job_url": match["html_url"]}))
            if status != "failed":
                break
        if not fails_for_sure([s for s, _ in statuses]):
            return None, (f"not failing for sure: `{job_label(j['job_name'])}` was "
                          + ", ".join(s for s, _ in statuses[:3]) + " in its latest scheduled runs")
        ff, lp, n = failure_window(statuses)
        if ff and (first_fail is None or ff["date"] < first_fail["date"]):
            first_fail, last_pass, streak = ff, lp, n

    since = (first_fail or {}).get("date") or (now - timedelta(days=14)).strftime("%Y-%m-%d")
    fix = find_fix_pr(gh, tests, since)
    if fix:
        return None, f"fix PR #{fix} names the test file"

    in_group = {j["job_id"] for j in group_jobs}
    return {
        "id": p.id,
        "name": p.name,
        "jobs": plan_jobs(gh, group_jobs),
        "other_jobs": [{k: ja[k] for k in ("job_id", "job_name", "run_url", "workflow")}
                       for ja in p.jobs if ja["job_id"] not in in_group],
        "tests": [list(t) for t in tests],
        "errors": list(dict.fromkeys(errors))[:10],
        "recipe": plan_recipe(group),
        "first_fail": first_fail,
        "last_pass": last_pass,
        "failing_runs": streak,
        "failing_since": (first_fail or {}).get("date"),
        "failed_jobs": len(p.jobs),
        "suspect": p.suspect,
    }, ""


def plan_jobs(gh: GitHub, jobs: list[dict]) -> list[dict]:
    """The failed jobs as a plan keeps them: links, runner labels, commit, and their tests."""
    out = []
    for j in jobs:
        api_job = next((x for x in gh.jobs(run_id_of(j["run_url"])) if x["id"] == j["job_id"]), {})
        out.append({k: j[k] for k in ("job_id", "job_name", "run_url", "workflow")} | {
            "labels": api_job.get("labels") or [], "sha": j["run"]["head_sha"],
            "tests": [list(t) for t in problem_tests([j])],
        })
    return out


def plan_recipe(group: list[tuple[Recipe, dict]]) -> dict:
    return {"workflow": group[0][0].workflow, "inputs": dispatch_inputs([r for r, _ in group]),
            "families": list(dict.fromkeys(job_family(j["job_name"]) for _, j in group))}


def find_fix_pr(gh: GitHub, tests: list[tuple[str, str]], since: str) -> int | None:
    """An open or merged sglang PR created since *since* whose title, body or diff names a test file.

    Title and body: the search API. Diff: the files and patches of open PRs
    created since then, and the commits on main that touched the test file.
    """
    names = sorted({Path(f).name for f, _ in tests})
    for name in names:
        for item in gh.search_prs(f'"{Path(name).stem}" created:>={since}'):
            pr = item.get("pull_request") or {}
            if item.get("state") == "open" or pr.get("merged_at"):
                return item["number"]
    for pr in gh.open_prs(since):
        for f in gh.pr_files(pr["number"]):
            if Path(f.get("filename", "")).name in names or any(n in (f.get("patch") or "") for n in names):
                return pr["number"]
    for f, _ in tests:
        for c in gh.commits_touching(f, since):
            m = re.search(r"\(#(\d+)\)\s*$", c["commit"]["message"].split("\n", 1)[0])
            if m:
                return int(m.group(1))
    return None


def plan_hour(state: dict, eligible: list[dict], now: datetime, cap: int) -> tuple[list[dict], list[dict]]:
    """Start what fits in this hour, queue the rest; returns (start, waiting not yet notified)."""
    start, waiting = take_for_this_hour(eligible, state["entered"], now, cap)
    at = now.isoformat(timespec="seconds")
    state["entered"] += [{"id": p["id"], "at": at} for p in start]
    state["queue"] = [{"id": p["id"], "name": p["name"], "jobs": p["failed_jobs"],
                       "failing_since": p.get("failing_since"), "position": i} for i, p in enumerate(waiting)]
    new_waiting = [q for q in state["queue"] if q["id"] not in state["notified"]]
    if new_waiting:
        state["notified"] = (state["notified"] + [q["id"] for q in new_waiting])[-500:]
        state["notices"].append({"at": at, "ids": [q["id"] for q in new_waiting]})
    return start, new_waiting
