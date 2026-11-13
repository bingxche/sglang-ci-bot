"""Which problems of the daily report ci-autofix tries, and when.

``build_plan()`` runs the checks (scheduled failure on main, fails for sure,
not infrastructure, a dispatch recipe, no fix PR but a stalled one) and returns what an attempt
needs; ``plan_hour()`` starts at most ``AUTOFIX_MAX_PER_HOUR`` problems per
hour and queues the rest."""

import itertools
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from autofix_ci import (
    GPU_MEMORY,
    Recipe,
    classify_infra,
    crusoe_recipe_for,
    dispatch_inputs,
    fails_for_sure,
    failure_window,
    find_outcome,
    is_perf,
    job_family,
    job_label,
    ran_first,
    recipe_for,
    run_history,
    same_job,
)
from autofix_config import (
    AUTOFIX_WORKFLOWS,
    BRANCH_PREFIX,
    FIX_PR_STALE_HOURS,
    HISTORY_RUNS,
    HISTORY_SCAN,
    NOT_FIXED,
    SPUR_GPU,
)
from autofix_github import GitHub
from autofix_pr import PR_MARKER
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
_FIX_LINK_RE = re.compile(r"\*\*In-flight fix\*\*:\s*✅\s*\[#(\d+)\](?:\(([^)\s]*)\))?")


def problem_sections(summary: str) -> dict[str, dict]:
    """Per problem ID: its plain-language name and whether the report shows a fix."""
    out: dict[str, dict] = {}
    current: list[str] = []
    # Hidden `<!-- R386 -->` IDs; reports before 2026-11-06 wrote them as visible text. A report
    # with hidden IDs is never read for visible ones: there `R1` can be a model name.
    hidden = _HEADING_ID_RE.search(summary) is not None
    for line in summary.splitlines():
        s = line.strip()
        if not s.startswith(("### ", "|")):
            ids = []
        elif hidden:
            m = _HEADING_ID_RE.search(s)
            ids = re.findall(r"R\d+", m.group(1)) if m else []
        else:
            ids = _cluster_ids(s)
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
            entry = out.setdefault(current[0], {"name": current[0], "fix": False, "fix_pr": None, "named": False,
                                                "text": []})
            # A fix in another repo (AITER, Miles) is a fix, but its number is not an sglang PR.
            if m.group(2) and "github.com/sgl-project/sglang/pull/" not in m.group(2):
                entry["elsewhere"] = True
            else:
                entry["fix_pr"] = int(m.group(1))
    return out


@dataclass
class Problem:
    id: str
    name: str
    jobs: list[dict]                 # per-job analyses, with "workflow"
    suspect: dict | None = None
    fix_pr: int | None = None
    report_fix: bool = False
    fix_elsewhere: bool = False      # the report's fix is a PR in another repo (AITER, Miles)
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
            fix_elsewhere=section.get("elsewhere", False),
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
    # A server an earlier test of the job leaked looks the same: only a job whose first test
    # file failed this way found the GPUs taken before it started.
    if infra == GPU_MEMORY and not all(ran_first(gh.job_log(j["job_id"]), files) for j in jobs):
        infra = None
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

    if p.fix_elsewhere:
        return None, "the report shows a fix in another repo"
    # A report fix PR that cannot be read is taken at the report's word.
    report_pr = gh.pull(p.fix_pr) if p.fix_pr else None
    if p.fix_pr and (report_pr is None or blocks_now(report_pr, now)):
        return None, f"the report shows a fix PR (#{p.fix_pr})"
    if p.report_fix and not p.fix_pr:
        return None, "the report shows a fix PR"

    # History: one job per family (each ROCm version) of the dispatch's workflow, the dispatch
    # group's first, newest scheduled runs first.
    workflow = group_jobs[0]["workflow"]
    dispatched = {job_family(j["job_name"]) for j in group_jobs}
    newest = sorted((j for j in jobs if j["workflow"] == workflow), key=lambda j: j["run"]["created_at"], reverse=True)
    by_family: dict[str, dict] = {}
    for j in sorted(newest, key=lambda j: job_family(j["job_name"]) not in dispatched):
        by_family.setdefault(job_family(j["job_name"]), j)
    histories: list[tuple[list, list]] = []  # (the problem's tests in the job, its statuses)
    for j in list(by_family.values())[:4]:
        job_tests = [t for t in problem_tests([j]) if t in own]
        if job_tests:
            histories.append((job_tests, job_history(gh, workflow, j, job_tests)))
    history = run_history([statuses for _, statuses in histories])
    if not fails_for_sure([s for s, _ in history]):
        seen = ", ".join(f"{s} (`{r['job']}`)" for s, r in history[:3]) or "none"
        return None, f"not failing for sure: its latest scheduled runs were {seen}"
    # The runs failing in a row; every test must have failed in two of them, not one each.
    failing = {r["run_id"] for _, r in itertools.takewhile(lambda x: x[0] == "failed", history)}
    for t in tests:
        runs = {r["run_id"] for job_tests, statuses in histories if t in job_tests
                for s, r in statuses if s == "failed" and r["run_id"] in failing}
        if len(runs) < 2:
            return None, f"not failing for sure: `{t[1]}` failed in {len(runs)} of its latest runs"
    # The failure window is each job's own: the earliest first failure is where it broke.
    first_fail = last_pass = None
    for _, statuses in histories:
        ff, lp, _ = failure_window(statuses)
        if ff and (first_fail is None or ff["at"] < first_fail["at"]):
            first_fail, last_pass = ff, lp
    streak = len(failing)

    # An old open report fix PR is stalled only if it was open while the test still passed.
    passed_at = (last_pass or {}).get("at")
    stalled = []
    if report_pr and report_pr.get("state") == "open":
        if not passed_at or report_pr["created_at"] >= passed_at:
            return None, (f"the report shows a fix PR (#{p.fix_pr}), "
                          + ("opened after the test last passed" if passed_at else "and no passing run is known"))
        stalled.append(p.fix_pr)
    since = (first_fail or {}).get("at") or (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fix = find_fix_pr(gh, tests, since, exclude={(p.suspect or {}).get("pr")})
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
        **plan_recipes(group, workflow_yaml),
        "first_fail": first_fail,
        "last_pass": last_pass,
        "failing_runs": streak,
        "failing_since": (first_fail or {}).get("date"),
        "failed_jobs": len(p.jobs),
        "suspect": p.suspect,
        "stalled_fix_prs": stalled,
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
    r = group[0][0]
    return {"workflow": r.workflow, "inputs": dispatch_inputs([r for r, _ in group]),
            "families": list(dict.fromkeys(r.verify_family or job_family(j["job_name"]) for r, j in group)),
            "dispatch_ref": r.dispatch_ref, "ref_input": r.ref_input, "gpu": ""}


def plan_recipes(group: list[tuple[Recipe, dict]], workflow_yaml) -> dict:
    """``recipe``, plus ``fallback_recipe`` when the jobs can be re-run on Crusoe first.

    Crusoe has no queue; ARC (the failed job's own workflow) is the fallback
    when Crusoe is busy or its run cannot tell (the test did not run, the run
    was cancelled).
    """
    arc = plan_recipe(group)
    crusoe = [(crusoe_recipe_for(j["workflow"], j["job_name"], workflow_yaml), j) for _, j in group]
    if all(r for r, _ in crusoe) and len({r.group for r, _ in crusoe}) == 1:
        # Crusoe and ARC are two pools of one GPU: the check is the GPU, not the runner labels.
        return {"recipe": {**plan_recipe(crusoe), "gpu": SPUR_GPU}, "fallback_recipe": {**arc, "gpu": SPUR_GPU}}
    return {"recipe": arc}


def job_history(gh: GitHub, workflow: str, j: dict, job_tests: list) -> list[tuple[str, dict]]:
    """Job *j*'s status in the latest scheduled runs of *workflow*, newest first, up to its last pass.

    ``failed`` needs every one of *job_tests* to fail in the job's log. Past ``HISTORY_RUNS``
    failures only the last pass is looked for, so a failed job counts without reading its log:
    a fix PR is stalled only if it was open while the test still passed, which needs that pass.
    """
    statuses: list[tuple[str, dict]] = []
    for run in gh.scheduled_runs(workflow, running=True)[:HISTORY_SCAN]:
        status, match = run_status(gh, gh.jobs(run["id"]), j, job_tests, read_logs=len(statuses) < HISTORY_RUNS)
        if not status:
            continue  # no job of this run ran the tests: it says nothing
        entry = {"run_id": run["id"], "run_url": run["html_url"], "sha": run["head_sha"],
                 "date": run["created_at"][:10], "at": run["created_at"], "job_url": match["html_url"],
                 "job": job_label(j["job_name"])}
        statuses.append((status, entry))
        if status != "failed":
            break
    return statuses


def run_status(gh: GitHub, run_jobs: list[dict], j: dict, job_tests: list,
               read_logs: bool = True) -> tuple[str | None, dict | None]:
    """How *job_tests* ended in one run, and the job that says so: ``failed`` (all failed),
    ``other`` (some failed, or one ran with no clear result), ``passed`` (a log shows each
    pass), or ``(None, None)`` when the run says nothing: no partition shows the tests ran.

    The tests are looked for in every finished partition and runner pool of job *j*
    (``find_outcome()``), its own and the failed ones first: suites that place tests by time
    move a file between partitions (R317's on Nov 9), and a run may list the job once per
    pool. Without *read_logs* (past ``HISTORY_RUNS``) only the job's own partition decides,
    by its conclusion.
    """
    done = [x for x in run_jobs if x.get("conclusion") not in (None, "skipped", "cancelled")]
    if not read_logs:
        own = next((x for x in done if same_job(x["name"], j["job_name"])), None)
        return ((("passed" if own["conclusion"] == "success" else "failed"), own) if own else (None, None))
    jobs = sorted((x for x in done if same_job(x["name"], j["job_name"], any_partition=True)),
                  key=lambda x: (x["conclusion"] == "success", not same_job(x["name"], j["job_name"])))
    found = [r for t in job_tests if (r := find_outcome(jobs, gh.job_log, *t, first_known=True))]
    statuses = [o.status for o, _ in found]
    if "failed" in statuses:
        status = "failed" if len(found) == len(job_tests) and set(statuses) == {"failed"} else "other"
        return status, next(x for o, x in found if o.status == "failed")
    if not found:
        return None, None
    # Every test that ran passed; one that ran with no clear result (a timeout, a log that
    # could not be read) leaves the run undecided, which stops the history.
    return ("passed" if set(statuses) == {"passed"} else "other"), found[0][1]


def blocks_now(pr: dict, now: datetime) -> bool:
    """Whether fix PR *pr* holds autofix back whatever the history says: merged, the bot's own,
    or open for less than ``FIX_PR_STALE_HOURS``. An older open one is stalled only if it was
    already open while the test still passed (``build_plan()``): the fix rides in a change made
    for something else, like #48701 for R427. A fix opened after the break waits for its review
    however long that takes (#46945 for R332)."""
    if pr.get("merged_at"):
        return True
    if pr.get("state") != "open":
        return False
    # The bot's drafts stay open until people act on them: never a reason to try again.
    if PR_MARKER.split("{")[0] in (pr.get("body") or "") \
            or ((pr.get("head") or {}).get("ref") or "").startswith(BRANCH_PREFIX):
        return True
    opened = datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
    return now - opened < timedelta(hours=FIX_PR_STALE_HOURS)


def find_fix_pr(gh: GitHub, tests: list[tuple[str, str]], since: str, exclude=frozenset()) -> int | None:
    """An open or merged sglang PR since the first failure (*since*, the failing run's ISO start time)
    whose title, body or diff names a test file. PRs in *exclude* (the suspect PR) do not count.

    Cheapest first: the commits on main that touched the test file after the
    failing run started (the commit that broke it came before), then the
    search API for titles and bodies, then the files of open PRs.
    """
    names = sorted({Path(f).name for f, _ in tests})
    day = since[:10]
    for f, _ in tests:
        for c in gh.commits_touching(f, since):
            m = re.search(r"\(#(\d+)\)\s*$", c["commit"]["message"].split("\n", 1)[0])
            if m and int(m.group(1)) not in exclude:
                return int(m.group(1))
    for name in names:
        for item in gh.search_prs(f'"{Path(name).stem}" created:>={day}'):
            pr = item.get("pull_request") or {}
            if item["number"] not in exclude and (item.get("state") == "open" or pr.get("merged_at")):
                return item["number"]
    for pr in gh.open_prs(day):
        if pr["number"] in exclude:
            continue
        for f in gh.pr_files(pr["number"]):
            if Path(f.get("filename", "")).name in names or any(n in (f.get("patch") or "") for n in names):
                return pr["number"]
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
