#!/usr/bin/env python3
"""ci-autofix: fix AMD scheduled-CI failures on sglang ``main`` and open draft PRs.

The feature is these modules, switched by one repository variable,
``AUTOFIX_MODE`` (``autofix_config.mode()``): ``off`` (the default) does
nothing at all, ``live`` runs.

  autofix.py         the two jobs of a tick, the smoke test, the command line
  autofix_config.py  the switch and every limit
  autofix_select.py  which problems of the daily report to try, and when
  autofix_ci.py      sglang CI: job names, dispatch recipes, test results in logs
  autofix_agents.py  one attempt: fix agent, code checks, review agent
  autofix_pr.py      code gates on the patch, the draft PR's title and body
  autofix_state.py   state on the daily issue, the queue notice
  autofix_github.py  GitHub reads and the guarded writes, git

Each tick (``ci-autofix.yml``, after every CI-monitor run) is two jobs.

``fix`` (``fix_phase()``) runs the agents. Code an agent runs (a test it
wrote, under pytest) can read its parent processes' environment, so this job
holds only a read token and writes nothing to GitHub. It picks problems
(``autofix_select.build_plan()``, at most ``AUTOFIX_MAX_PER_HOUR`` starts per
hour), lets the agents fix and review them (``autofix_agents.start_attempt()``),
and hands its state and patches to the next job as an artifact.

``publish`` (``publish_phase()``) runs no agent and does every write, as the
owner of ``AUTOFIX_TOKEN``: it commits each patch, pushes
``bingxche/autofix-<id>-<slug>`` to sgl-project/sglang and dispatches the
failed job there (``push_and_dispatch()``). On later ticks it reads the run
(``autofix_ci.verify_run()``): every test of the problem must PASS, without a
retry, on the same workflow, job, ROCm version and runner. Then it opens a
draft PR; a failed run gives the agent a second attempt, and after
``MAX_ATTEMPTS`` the branch is deleted. It also writes the daily issue.

``--smoke-*`` runs the chain for one past problem without opening a PR or
writing the daily issue (E3 in the spec).
"""

import argparse
import concurrent.futures
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autofix_agents import AttemptResult, deepen_clone, previous_attempt_text, start_attempt  # noqa: E402
from autofix_ci import (  # noqa: E402
    Recipe,
    recipe_for,
    verify_run,
    workflow_yaml_at,
)
from autofix_config import (  # noqa: E402
    AUTOFIX_WORKFLOWS,
    DISPATCH_TIMEOUT_HOURS,
    MAX_ATTEMPTS,
    MAX_UNAVAILABLE,
    MODES,
    max_per_hour_from_env,
    mode_from_env,
)
from autofix_github import (  # noqa: E402
    GitHub,
    check_branch,
    clean_checkout,
    clean_git_env,
    find_dispatched_run,
    git,
    push_branch,
)
from autofix_pr import open_pr, pr_template, render_pr_body  # noqa: E402
from autofix_select import (  # noqa: E402
    build_plan,
    find_fix_pr,
    load_problems,
    plan_hour,
    plan_jobs,
    plan_recipes,
    problem_tests,
    run_id_of,
)
from autofix_state import (  # noqa: E402
    add_event,
    find_issue,
    find_log_comment,
    new_day_state,
    read_state,
    render_log_comment,
    render_queue_notice,
)
from failure_tracker import deterministic_rows  # noqa: E402
from utils import (  # noqa: E402
    REPO,
    SGLANG_REPO_PATH,
    ensure_sglang_repo,
    post_comment,
    update_comment,
)

log = logging.getLogger("autofix")


STATE_FILE = "state.json"
SMOKE_FILE = "smoke.json"


def save_handoff(handoff: Path, issue_num: int, state: dict) -> None:
    handoff.mkdir(parents=True, exist_ok=True)
    (handoff / STATE_FILE).write_text(json.dumps({"issue": issue_num, "state": state}))


def patch_path(handoff: Path, pid: str) -> Path:
    return handoff / "patches" / f"{pid}.patch"


def poll_dispatched(pid: str, entry: dict, gh: GitHub, now: datetime) -> tuple[str, dict | None]:
    """``("waiting" | "verified" | "failed", verification)`` for a dispatched attempt."""
    rec, plan = entry["attempt"], entry["plan"]
    if not entry.get("run_id"):
        run = find_dispatched_run(gh, plan["recipe"], rec["branch"], rec["head_sha"],
                                  datetime.fromisoformat(rec["dispatched_at"]))
        if not run:
            if now - datetime.fromisoformat(rec["dispatched_at"]) > timedelta(hours=1):
                return "failed", {"reasons": ["the dispatched run never appeared"], "results": [], "inconclusive": True}
            return "waiting", None
        entry["run_id"], entry["run_url"] = run["id"], run["html_url"]
    run = gh.run(entry["run_id"], fresh=True)
    v = verify_run(plan, rec, run, gh.jobs(entry["run_id"], fresh=True), gh.job_log)
    if not v["done"]:
        if now - datetime.fromisoformat(rec["dispatched_at"]) > timedelta(hours=DISPATCH_TIMEOUT_HOURS):
            return "failed", {**v, "reasons": [f"the run did not finish in {DISPATCH_TIMEOUT_HOURS} h"],
                              "inconclusive": True}
        return "waiting", None
    return ("verified" if v["ok"] else "failed"), v


def push_and_dispatch(plan: dict, rec: dict, patch: bytes, gh: GitHub) -> None:
    """Commit the fix job's *patch* on its base as the token's owner, push the branch, dispatch the job.

    Sets ``rec["head_sha"]`` as soon as the branch is pushed (so a failed
    dispatch still leaves a record of the branch to delete) and
    ``rec["dispatched_at"]`` after the dispatch. The commit is made in a
    fresh clone with no hooks and no user or system git config: the shared
    clone and the home directory are writable by code the fix job's agent ran.
    """
    check_branch(rec["branch"])
    with tempfile.TemporaryDirectory() as tmp:
        env = clean_git_env(Path(tmp))
        repo = Path(tmp) / "sglang"
        clean_checkout(repo, rec["base_sha"], env)
        git(repo, "-c", "core.hooksPath=/dev/null", "apply", "--binary", "--index", "-", input=patch,
            timeout=120, env=env)
        rec["head_sha"] = push_branch(repo, rec["branch"], rec["message"], gh, env=env)
    if plan.get("fallback_recipe") and gh.dispatch_busy(plan["recipe"]["workflow"]):
        # Someone's run is in Crusoe's dispatch group; ours could cancel it. Use ARC.
        plan["recipe"] = plan.pop("fallback_recipe")
    gh.dispatch(rec["branch"], plan["recipe"])
    rec["dispatched_at"] = datetime.now(timezone.utc).isoformat()


def try_fallback(plan: dict, rec: dict, entry: dict, v: dict, gh: GitHub) -> bool:
    """When Crusoe could not judge the fix, re-run it on ARC (same branch, same attempt)."""
    if not (v.get("inconclusive") and plan.get("fallback_recipe")):
        return False
    plan["recipe"] = plan.pop("fallback_recipe")
    gh.dispatch(rec["branch"], plan["recipe"])
    rec["dispatched_at"] = datetime.now(timezone.utc).isoformat()
    entry.update(run_id=None, run_url="")
    return True


def delete_pushed(entry: dict, gh: GitHub) -> None:
    """Delete every branch this problem pushed (the publish job holds the token for it)."""
    for branch in entry.get("pushed") or []:
        gh.delete_branch(branch)
    entry.pop("pushed", None)


def give_up(state: dict, pid: str, entry: dict, now: datetime, event: str, reason: str) -> None:
    """The one way a problem ends without a PR. Its branches are deleted by the next publish job."""
    entry.update(status="gave-up", reason=reason, updated=now.isoformat(timespec="seconds"))
    add_event(state, now, pid, event, reason)
    _slim(entry)


def end_attempt(state: dict, pid: str, entry: dict, now: datetime, what: str, reason: str,
                gh: GitHub, previous: str = "") -> None:
    """An attempt failed: try again, or after ``MAX_ATTEMPTS`` give up and delete the branch."""
    entry["updated"] = now.isoformat(timespec="seconds")
    if entry["attempts"] >= MAX_ATTEMPTS:
        give_up(state, pid, entry, now, f"{what} on attempt {entry['attempts']}; gave up", f"{what}: {reason}")
        delete_pushed(entry, gh)
    else:
        entry.update(status="retry", reason=f"{what}: {reason}",
                     previous=previous or f"# Previous attempt: {what}\n\n{reason}\n")
        add_event(state, now, pid, f"{what} on attempt {entry['attempts']}; trying again", reason)


def check_dispatched(pid: str, entry: dict, gh: GitHub, state: dict, now: datetime, template: str) -> None:
    """Read a dispatched attempt's run; open the PR, retry, or give up."""
    result, v = poll_dispatched(pid, entry, gh, now)
    if result == "waiting":
        return
    entry["updated"] = now.isoformat(timespec="seconds")
    plan, rec = entry["plan"], entry["attempt"]
    if result == "verified":
        if not gh.pr_for_branch(rec["branch"], rec["head_sha"]):
            since = (plan.get("first_fail") or {}).get("at") or rec["dispatched_at"]
            other = find_fix_pr(gh, [tuple(t) for t in plan["tests"]], since,
                                exclude={(plan.get("suspect") or {}).get("pr")})
            if other is not None:
                give_up(state, pid, entry, now, "fix passed, but another fix PR came first", f"#{other} came first")
                delete_pushed(entry, gh)
                return
        pr = open_pr(plan, rec, v, gh, template)
        entry.pop("pushed", None)  # the PR's branch stays
        entry.update(status="opened", pr=pr["html_url"])
        add_event(state, now, pid, "fix passed on AMD GPUs; draft PR opened", pr["html_url"])
        _slim(entry)
        return
    if try_fallback(plan, rec, entry, v, gh):
        add_event(state, now, pid, "the Crusoe run could not tell; re-running the job on ARC",
                  "; ".join(v.get("reasons") or [])[:300])
        return
    end_attempt(state, pid, entry, now, "GPU check failed", "; ".join(v.get("reasons") or [])[:400], gh,
                previous=previous_attempt_text(rec, v, gh, plan))


def mark_running(state: dict, plan: dict, n: int, now: datetime) -> None:
    """Count attempt *n* before it runs: a fix job that is killed mid-attempt has still used it."""
    entry = state["problems"].setdefault(plan["id"], {"attempts": 0})
    entry.update(status="running", attempts=n, name=plan["name"], plan=plan, updated=now.isoformat(timespec="seconds"))
    entry.pop("skip", None)


def finish_interrupted(state: dict, now: datetime) -> None:
    """Attempts a killed fix job left `running` count as failed: try again, or give up."""
    for pid, entry in state["problems"].items():
        if entry.get("status") != "running":
            continue
        reason = "the fix job stopped before the attempt finished"
        if entry["attempts"] >= MAX_ATTEMPTS:
            give_up(state, pid, entry, now, f"attempt {entry['attempts']} did not finish; gave up", reason)
        else:
            entry.update(status="retry", reason=reason, updated=now.isoformat(timespec="seconds"),
                         previous=f"# Previous attempt: did not finish\n\n{reason}\n")
            add_event(state, now, pid, f"attempt {entry['attempts']} did not finish; trying again", reason)


def record_attempt(state: dict, pid: str, plan: dict, n: int, result: AttemptResult, now: datetime) -> None:
    entry = state["problems"].setdefault(pid, {"attempts": n})
    entry.update(name=plan["name"], plan=plan, attempts=n, updated=now.isoformat(timespec="seconds"))
    entry.pop("skip", None)
    if result.status == "model-unavailable":
        # Not an attempt: the agent never ran. Back to where the problem was.
        entry["attempts"] = n - 1
        entry["unavailable"] = entry.get("unavailable", 0) + 1
        if entry["unavailable"] >= MAX_UNAVAILABLE:
            give_up(state, pid, entry, now, "model unavailable again; gave up",
                    f"model unavailable {entry['unavailable']} times: {result.reason}")
        elif n > 1:
            entry["status"] = "retry"
            add_event(state, now, pid, "model unavailable; not fixed this hour", result.reason)
        else:
            entry.pop("status", None)
            add_event(state, now, pid, "model unavailable; not fixed this hour", result.reason)
        return
    entry.pop("previous", None)
    if result.status == "ready":
        entry.update(status="ready", attempt={k: v for k, v in result.record.items() if k != "patch"}, reason="")
        add_event(state, now, pid, f"attempt {n}: the fix passed the checks and the review")
    elif n >= MAX_ATTEMPTS or result.status == "no-fix":
        give_up(state, pid, entry, now, f"attempt {n}: gave up", result.reason)
    else:
        entry.update(status="retry", reason=result.reason,
                     previous=f"# Previous attempt: rejected before the GPU re-run\n\n{result.reason}\n")
        add_event(state, now, pid, f"attempt {n}: rejected before the GPU run", result.reason)


def write_log(token: str, bot_repo: str, issue_num: int, comment: dict | None, state: dict,
              mode: str, cap: int, now: datetime) -> dict:
    body = render_log_comment(state, mode, cap, now)
    if comment:
        return update_comment(token, bot_repo, comment["id"], body)
    return post_comment(token, bot_repo, issue_num, body)


def fix_phase(token: str, bot_repo: str, mode: str, cap: int, gh: GitHub, handoff: Path,
              now: datetime | None = None) -> int:
    """The fix job: pick problems, run the agents, gate the patches. Writes only *handoff*."""
    if mode != "live":
        log.info("AUTOFIX_MODE=%s; nothing to do", mode)
        return 0
    now = now or datetime.now(timezone.utc)
    issue_num = find_issue(token, bot_repo, now.strftime("%Y-%m-%d"))
    if not issue_num:
        log.info("No daily issue for %s", now.strftime("%Y-%m-%d"))
        return 0
    old, _ = read_state(token, bot_repo, issue_num, now)
    state = new_day_state(old, now.strftime("%Y-%m-%d"), now)
    finish_interrupted(state, now)
    ensure_sglang_repo()
    yaml = workflow_yaml_at("origin/main")

    eligible, retries = [], []
    for p in load_problems(token, bot_repo, issue_num):
        entry = state["problems"].get(p.id, {})
        if entry.get("status") in ("ready", "dispatched", "opened", "gave-up"):
            continue
        try:
            plan, why = build_plan(p, gh, yaml, now)
        except requests.RequestException as exc:
            plan, why = None, f"GitHub API error ({exc.__class__.__name__})"
        except Exception as exc:  # one malformed problem must not stop the others
            log.exception("%s: planning failed", p.id)
            plan, why = None, f"could not plan it ({exc.__class__.__name__}: {exc})"[:300]
        if plan:
            (retries if entry.get("status") == "retry" else eligible).append(plan)
            continue
        entry = state["problems"].setdefault(p.id, {"attempts": 0})
        if entry.get("skip") != why:
            add_event(state, now, p.id, "not fixed", why)
        entry.update(skip=why, name=p.name)
        if not entry.get("status"):  # a waiting retry keeps its time, so a stale one ends after 3 days
            entry["updated"] = now.isoformat(timespec="seconds")
    start, new_waiting = plan_hour(state, eligible, now, cap)
    state["pending_notices"] = new_waiting
    # Saved before the long attempts, so a crash cannot forget who started this hour.
    save_handoff(handoff, issue_num, state)

    # Second attempts (outside the cap) and this hour's new problems, in parallel.
    work = [(plan, state["problems"][plan["id"]]["attempts"] + 1, state["problems"][plan["id"]].get("previous", ""))
            for plan in retries]
    work += [(plan, state["problems"].get(plan["id"], {}).get("attempts", 0) + 1, "") for plan in start]
    if not work:
        return 0
    for plan, n, _ in work:
        mark_running(state, plan, n, now)
    save_handoff(handoff, issue_num, state)
    deepen_clone([plan for plan, _, _ in work])
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(work)) as pool:
        futures = {pool.submit(start_attempt, plan, n, gh, "origin/main", prev): (plan, n)
                   for plan, n, prev in work}
        for fut in concurrent.futures.as_completed(futures):
            plan, n = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # one problem's crash must not lose the others
                log.exception("%s: attempt %d crashed", plan["id"], n)
                result = AttemptResult("failed", f"attempt crashed: {exc}"[:300])
            if result.status == "ready":
                path = patch_path(handoff, plan["id"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(result.record["patch"])
            record_attempt(state, plan["id"], plan, n, result, now)
            save_handoff(handoff, issue_num, state)
    return 0


def publish_phase(token: str, bot_repo: str, mode: str, cap: int, gh: GitHub, handoff: Path,
                  now: datetime | None = None) -> int:
    """The publish job: push and dispatch ready fixes, check dispatched runs, open PRs, write the daily issue."""
    if mode != "live":
        log.info("AUTOFIX_MODE=%s; nothing to do", mode)
        return 0
    now = now or datetime.now(timezone.utc)
    if (handoff / STATE_FILE).exists():
        data = json.loads((handoff / STATE_FILE).read_text())
        issue_num, state = data["issue"], data["state"]
        comment = find_log_comment(token, bot_repo, issue_num)
    else:  # the fix job found no issue, or did not get far
        issue_num = find_issue(token, bot_repo, now.strftime("%Y-%m-%d"))
        if not issue_num:
            return 0
        old, comment = read_state(token, bot_repo, issue_num, now)
        state = new_day_state(old, now.strftime("%Y-%m-%d"), now)
    issue_url = f"https://github.com/{bot_repo}/issues/{issue_num}"
    ensure_sglang_repo()
    template = pr_template()

    waiting = state.pop("pending_notices", [])
    if waiting:
        post_comment(token, bot_repo, issue_num, render_queue_notice(waiting, cap, now, issue_url))

    for pid, entry in state["problems"].items():
        try:
            status = entry.get("status")
            if status == "ready":
                path = patch_path(handoff, pid)
                if not path.exists():
                    end_attempt(state, pid, entry, now, "lost the patch", "the fix job's artifact has no patch", gh)
                    continue
                rec = entry["attempt"]
                try:
                    push_and_dispatch(entry["plan"], rec, path.read_bytes(), gh)
                except Exception as exc:  # git timeouts too: the entry must not stay `ready`
                    if rec.get("head_sha"):
                        entry["pushed"] = sorted({*entry.get("pushed", []), rec["branch"]})
                    end_attempt(state, pid, entry, now, "push or dispatch failed", str(exc)[:300], gh)
                    continue
                entry["pushed"] = sorted({*entry.get("pushed", []), rec["branch"]})
                entry.update(status="dispatched", run_id=None, run_url="", updated=now.isoformat(timespec="seconds"))
                add_event(state, now, pid, f"attempt {entry['attempts']}: fix pushed, job dispatched", rec["branch"])
            elif status == "dispatched":
                check_dispatched(pid, entry, gh, state, now, template)
            elif status == "retry" and now - datetime.fromisoformat(entry["updated"]) > timedelta(days=3):
                give_up(state, pid, entry, now, "waited 3 days for another attempt; gave up",
                        entry.get("skip") or "no second attempt started")
            if entry.get("status") == "gave-up" and entry.get("pushed"):
                delete_pushed(entry, gh)
        except Exception as exc:  # one problem's error must not lose the others' state
            log.exception("%s: publishing failed", pid)
            add_event(state, now, pid, "publishing failed", str(exc))
    write_log(token, bot_repo, issue_num, comment, state, mode, cap, now)
    return 0


def _slim(entry: dict) -> None:
    """A finished problem keeps only what the log and the audit read (and `pushed`, for clean-up)."""
    for key in ("plan", "attempt", "previous"):
        entry.pop(key, None)


def smoke_plan(token: str, bot_repo: str, gh: GitHub, date_str: str, pid: str, base_sha: str,
               job_filter: str = "") -> dict | None:
    """The plan for problem *pid* of *date_str*'s report, without the selection checks
    (the problem has long been fixed)."""
    issue_num = find_issue(token, bot_repo, date_str)
    problem = next((p for p in load_problems(token, bot_repo, issue_num) if p.id == pid), None) if issue_num else None
    if not problem:
        log.error("No problem %s in the %s report", pid, date_str)
        return None
    yaml = workflow_yaml_at(base_sha)
    jobs = [{**ja, "run": gh.run(run_id_of(ja["run_url"]))} for ja in problem.jobs
            if ja["workflow"] in AUTOFIX_WORKFLOWS and job_filter in ja["job_name"]]
    groups: dict[tuple, list[tuple[Recipe, dict]]] = {}
    for j in jobs:
        recipe, _ = recipe_for(j["workflow"], j["job_name"], yaml)
        if recipe:
            groups.setdefault(recipe.group, []).append((recipe, j))
    if not groups:
        log.error("%s has no dispatch recipe", pid)
        return None
    group = max(groups.values(), key=len)
    group_jobs = [j for _, j in group]
    own = set(problem.tests)
    return {
        "id": pid, "name": problem.name, "tests": [list(t) for t in problem_tests(group_jobs) if t in own],
        "jobs": plan_jobs(gh, group_jobs, own), "other_jobs": [],
        "errors": [r["error"] for r in deterministic_rows("", group_jobs)][:10],
        **plan_recipes(group, yaml),
        "first_fail": {"date": min(j["run"]["created_at"][:10] for j in group_jobs),
                       "run_url": group_jobs[0]["run_url"], "sha": group_jobs[0]["run"]["head_sha"]},
        "last_pass": None, "failing_runs": len({j["run_url"] for j in group_jobs}),
        "failed_jobs": len(problem.jobs), "suspect": problem.suspect,
    }


def clone_up_to(base_sha: str) -> None:
    """Make the agents' sglang clone hold history only up to *base_sha*.

    The smoke test fixes a problem that people have since fixed; a clone of
    today's main would show the agents that fix (``git log origin/main``).
    """
    if SGLANG_REPO_PATH.exists():
        shutil.rmtree(SGLANG_REPO_PATH)
    SGLANG_REPO_PATH.mkdir(parents=True)
    git(SGLANG_REPO_PATH, "init", "-q")
    git(SGLANG_REPO_PATH, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    git(SGLANG_REPO_PATH, "fetch", "-q", "--depth", "500", "origin", base_sha, timeout=1800)
    git(SGLANG_REPO_PATH, "update-ref", "refs/remotes/origin/main", base_sha)
    git(SGLANG_REPO_PATH, "checkout", "-q", "--detach", base_sha)


def smoke_fix(token: str, bot_repo: str, gh: GitHub, date_str: str, pid: str, base_sha: str,
              handoff: Path, job_filter: str = "") -> int:
    """Smoke test, fix job: the agents' first attempt on *base_sha*, handed to the publish job."""
    clone_up_to(base_sha)
    plan = smoke_plan(token, bot_repo, gh, date_str, pid, base_sha, job_filter)
    if not plan:
        return 1
    t0 = time.time()
    result = start_attempt(plan, 1, gh, base_sha, branch_tag="smoke-")
    handoff.mkdir(parents=True, exist_ok=True)
    if result.status == "ready":
        patch_path(handoff, pid).parent.mkdir(parents=True, exist_ok=True)
        patch_path(handoff, pid).write_bytes(result.record["patch"])
    (handoff / SMOKE_FILE).write_text(json.dumps({
        "plan": plan, "status": result.status, "reason": result.reason, "agent_secs": round(time.time() - t0),
        "record": {k: v for k, v in result.record.items() if k != "patch"},
    }))
    log.info("Smoke test of %s, fix job: %s %s", pid, result.status, result.reason)
    return 0


def smoke_publish(gh: GitHub, handoff: Path, out_dir: Path) -> int:
    """Smoke test, publish job: push, dispatch, wait for the GPU run, verify, write the PR it
    would open to *out_dir*, delete the branch. Opens no PR, writes no daily issue."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if not (handoff / SMOKE_FILE).exists():
        (out_dir / "summary.json").write_text(json.dumps({"status": "the fix job ended before an attempt"}))
        log.error("No %s from the fix job; see its log", SMOKE_FILE)
        return 1
    data = json.loads((handoff / SMOKE_FILE).read_text())
    plan, rec = data["plan"], data["record"]
    summary = {"problem": plan["id"], "status": data["status"], "reason": data["reason"],
               "agent_secs": data["agent_secs"]}
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    if data["status"] == "ready":
        ensure_sglang_repo()
        (out_dir / "attempt-1.diff").write_text(rec["diff"])
        t0 = time.time()
        entry = {"plan": plan, "attempt": rec, "run_id": None}
        try:
            push_and_dispatch(plan, rec, patch_path(handoff, plan["id"]).read_bytes(), gh)
            while True:
                status, v = poll_dispatched(plan["id"], entry, gh, datetime.now(timezone.utc))
                if status == "failed" and try_fallback(plan, rec, entry, v, gh):
                    summary.setdefault("fell_back", []).append(v.get("reasons"))
                    continue
                if status != "waiting":
                    break
                time.sleep(300)
        finally:
            gh.delete_branch(rec["branch"])
        summary.update(run_url=entry.get("run_url"), verified=status == "verified",
                       reasons=(v or {}).get("reasons"), results=(v or {}).get("results"),
                       gpu_secs=round(time.time() - t0))
        if status == "verified":
            (out_dir / "pr.md").write_text(f"# {rec['title']}\n\n"
                                           + render_pr_body(pr_template(), plan, rec["fix"], v, rec["checks"]))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("Smoke test of %s: %s", plan["id"], json.dumps(summary))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("phase", choices=("fix", "publish"),
                        help="fix: run the agents (read-only token); publish: every write (no agents)")
    parser.add_argument("--bot-repo", required=True, help="Bot repo, e.g. bingxche/sglang-ci-bot")
    parser.add_argument("--handoff", default="autofix-handoff", help="State and patches from fix to publish")
    parser.add_argument("--mode", choices=MODES, default=mode_from_env(),
                        help="off / live (default: the AUTOFIX_MODE switch, off when unset)")
    parser.add_argument("--max-per-hour", type=int, default=max_per_hour_from_env())
    parser.add_argument("--smoke-problem", help="E3: problem ID (R…) to fix end to end without opening a PR")
    parser.add_argument("--smoke-date", help="E3: date (YYYY-MM-DD) of the daily report holding the problem")
    parser.add_argument("--smoke-base", help="E3: sglang commit to build the fix on (before the human fix)")
    parser.add_argument("--smoke-job", default="",
                        help="E3: only the problem's jobs whose name contains this (e.g. a 1-GPU job of a problem "
                             "that also failed on 8 GPUs)")
    parser.add_argument("--smoke-out", default="autofix-smoke", help="E3: output directory")
    args = parser.parse_args()
    logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO, stream=sys.stdout)
    handoff = Path(args.handoff)
    write_token = os.environ.get("AUTOFIX_TOKEN") or ""
    if args.phase == "fix":
        if write_token:
            log.error("The fix job runs agents and must not hold AUTOFIX_TOKEN")
            return 1
        token = os.environ.get("AGENT_GH_TOKEN") or os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN") or ""
    else:
        token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        log.error("GitHub token required. Set GH_PAT.")
        return 1
    gh = GitHub(token, write_token)
    if args.smoke_problem:
        if args.phase == "fix":
            if not (args.smoke_date and args.smoke_base):
                log.error("--smoke-problem needs --smoke-date and --smoke-base")
                return 1
            return smoke_fix(token, args.bot_repo, gh, args.smoke_date, args.smoke_problem, args.smoke_base,
                             handoff, args.smoke_job)
        if not write_token:
            log.error("The smoke test's publish job needs AUTOFIX_TOKEN")
            return 1
        return smoke_publish(gh, handoff, Path(args.smoke_out))
    if args.phase == "fix":
        return fix_phase(token, args.bot_repo, args.mode, args.max_per_hour, gh, handoff)
    if args.mode == "live" and not write_token:
        log.error("AUTOFIX_MODE=live needs AUTOFIX_TOKEN in the publish job")
        return 1
    return publish_phase(token, args.bot_repo, args.mode, args.max_per_hour, gh, handoff)


if __name__ == "__main__":
    sys.exit(main())
