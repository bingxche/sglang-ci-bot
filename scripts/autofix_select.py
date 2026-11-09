"""Which problems of the daily report ci-autofix tries, and when.

``build_plan()`` runs the checks (scheduled failure on main, fails for sure,
not infrastructure, a dispatch recipe, no fix PR) and returns what an attempt
needs; ``plan_hour()`` starts at most ``AUTOFIX_MAX_PER_HOUR`` problems per
hour and queues the rest."""

import re
from collections import Counter
from dataclasses import dataclass, field
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
    _cluster_ids,
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
        # Hidden `<!-- R386 -->` IDs; reports before 2026-11-06 wrote them as visible text.
        ids = _cluster_ids(s) if s.startswith(("### ", "|")) else []
        if s.startswith("### ") or ids:
            text = _HEADING_ID_RE.sub("", s.lstrip("#").strip()).strip()
            cells = [c.strip() for c in re.split(r" — | \| ", text.strip("| "))]
            name = next((c for c in cells if c and not re.fullmatch(r"(?:R\d+[\s·,]*)+", c)), "")
            name = re.sub(r"^(?:🆕|↩️)\s*", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", name)).strip()
            fix = re.search(r"✅ fix (?:open|merged)", s) is not None
            if s.startswith("### "):
                current = ids
            # A heading naming several problems names the group; their rows name each one.
            own_name = len(ids) == 1
            for pid in ids:
                entry = out.setdefault(pid, {"name": pid, "fix": False, "fix_pr": None, "named": False, "text": []})
                if own_name and name and not entry["named"]:
                    entry.update(name=name, named=True)
                entry["fix"] = entry["fix"] or (fix and own_name)
                if own_name or not s.startswith("### "):
                    entry["text"].append(s)
            continue
        if s.startswith("## "):
            current = []
        elif len(current) == 1:
            out[current[0]]["text"].append(s)
        m = _FIX_LINK_RE.search(s)
        if m and len(current) == 1:
            out.setdefault(current[0], {"name": current[0], "fix": False, "fix_pr": None, "named": False, "text": []})
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
    # The failing tests that are this problem's (``own_tests()``); a job's other failures are not.
    tests: list[tuple[str, str]] = field(default_factory=list)


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
    membership = Counter(j for job_ids in clusters.values() for j in job_ids)
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
            tests=own_tests(jobs, "\n".join(section.get("text") or []),
                            exclusive=all(membership[j["job_id"]] == 1 for j in jobs)),
        ))
    return problems


def split_functions(cell: str) -> list[str]:
    """Test function names in an analysis's Failed Tests cell, which can hold several and prose:
    ``test_a`, `test_b_many_rows[32/96/128]` (4 failed, 1 passed)`` → test_a, test_b_many_rows."""
    # The table parser strips a cell's outer backticks ("a`, `b"); put them back before pairing.
    tokens = re.findall(r"`([^`]+)`", f"`{cell.strip()}`")
    names = []
    for tok in tokens:
        m = re.match(r"^\s*([A-Za-z_]\w*(?:(?:\.|::)[A-Za-z_]\w*)*)(\[[^\]]*\])?", tok)
        if not m:
            continue
        name, param = m.group(1), m.group(2) or ""
        last = re.split(r"\.|::", name)[-1]
        if not (last.startswith("test") or last in ("setUpClass", "setUpModule", "tearDownClass")):
            continue
        if "/" in param or " " in param:  # "[32/96/128]" stands for several cases: match them all
            param = ""
        names.append(name + param)
    return names


def file_named(path: str, text: str) -> bool:
    """Whether *text* names the test file, in full or in the report's shorthand (`` `_fp8_tbo.py` ``)."""
    base = Path(path).name
    if base in text:
        return True
    return any(base.endswith(f"_{suffix}") for suffix in re.findall(r"`[_…]+([\w.]{4,}\.py)`", text))


def own_tests(jobs: list[dict], text: str, exclusive: bool) -> list[tuple[str, str]]:
    """The failing tests that are this problem's. A job can be in several problems, and its
    analysis lists all its failures, so only files the problem's own report text names count.
    Without such text, all of them count only when no other problem shares the jobs."""
    candidates = problem_tests(jobs)
    named = [t for t in candidates if file_named(t[0], text)] if text else []
    return named or (candidates if exclusive else [])


def run_id_of(run_url: str) -> int:
    return int(re.search(r"/runs/(\d+)", run_url).group(1))


def problem_tests(jobs: list[dict]) -> list[tuple[str, str]]:
    """Every failing (test file, function) in the jobs' analyses."""
    tests = []
    for row in deterministic_rows("", jobs):
        f = row["test_file"].strip("` ")
        if f.endswith(".py"):
            tests += [(f, fn) for fn in split_functions(row["test_function"])]
    tests = list(dict.fromkeys(tests))
    # `test_x` and `TestX.test_x` of one file are one test.
    qualified = {(f, re.split(r"\.|::", fn)[-1]) for f, fn in tests if re.search(r"\.|::", fn)}
    return [(f, fn) for f, fn in tests if re.search(r"\.|::", fn) or (f, fn) not in qualified]


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

    own = set(p.tests)
    tests = [t for t in problem_tests(jobs) if t in own]
    if not tests:
        return None, "no failing test function of this problem named in the analyses"
    files = {f for f, _ in tests}
    errors = [r["error"] for r in deterministic_rows("", jobs) if r["test_file"].strip("` ") in files]
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
    covered = {t for t in problem_tests(group_jobs) if t in own}
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
        job_tests = [t for t in problem_tests([j]) if t in own]
        if not job_tests:
            continue
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
        "jobs": plan_jobs(gh, group_jobs, own),
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


def plan_jobs(gh: GitHub, jobs: list[dict], own: set) -> list[dict]:
    """The failed jobs as a plan keeps them: links, runner labels, commit, and the problem's tests in each."""
    out = []
    for j in jobs:
        api_job = next((x for x in gh.jobs(run_id_of(j["run_url"])) if x["id"] == j["job_id"]), {})
        out.append({k: j[k] for k in ("job_id", "job_name", "run_url", "workflow")} | {
            "labels": api_job.get("labels") or [], "sha": j["run"]["head_sha"],
            "tests": [list(t) for t in problem_tests([j]) if t in own],
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
