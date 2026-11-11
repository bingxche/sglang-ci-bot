"""One ci-autofix attempt in the fix job: the CI Autofix agent, the code
checks, and the CI Autofix Review agent (``start_attempt()``). It ends with a
patch for the publish job; nothing here writes to GitHub."""

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from autofix_ci import clean_log, job_label, parse_job_name, segment_text
from autofix_config import (
    AGENT_MAX_TURNS,
    AGENT_TIMEOUT_SECS,
    BRANCH_PREFIX,
    MAX_ATTEMPTS,
    MAX_STATE_DIFF,
    REVIEW_MAX_TURNS,
    REVIEW_TIMEOUT_SECS,
)
from autofix_github import REPO_LOCK, GitHub, git, staged_diff, staged_patch
from autofix_pr import (
    TEST_PATH_RE,
    build_title,
    check_pr_text,
    check_title,
    diff_files,
    gate_patch,
    parse_fix_report,
    parse_review,
)
from autofix_select import run_id_of
from utils import (
    SGLANG_REPO_PATH,
    ModelUnavailable,
    claude_code_analyze,
    collect_ci_runtime_env,
    create_agent_worktree,
    env_without_tokens,
    remove_agent_worktree,
)

log = logging.getLogger("autofix")


def render_problem_context(plan: dict) -> str:
    """``.ci-context/problem.md``: the facts code collected about the problem."""
    lines = [f"# Problem: {plan['name']}", "", "## Failing tests", ""]
    lines += [f"- `{f}` :: `{fn}`" for f, fn in plan["tests"]]
    lines += ["", "## Failed jobs (scheduled runs on main)", ""]
    for j in plan["jobs"]:
        lines.append(f"- `{job_label(j['job_name'])}` ({j['workflow']}): {j['run_url']}/job/{j['job_id']} "
                     f"at commit {j['sha']}")
    for j in plan.get("other_jobs") or []:
        lines.append(f"- also: `{job_label(j['job_name'])}` ({j['workflow']}): {j['run_url']}/job/{j['job_id']}")
    ff, lp = plan.get("first_fail") or {}, plan.get("last_pass") or {}
    lines += ["", "## When it started", ""]
    lines.append(f"- First failing scheduled run: {ff.get('run_url', 'unknown')} on {ff.get('date', '?')} "
                 f"at commit {ff.get('sha', '?')} ({plan.get('failing_runs', 0)} failing run(s) in a row)")
    lines.append(f"- Last passing scheduled run: {lp.get('run_url')} on {lp.get('date')} at commit {lp.get('sha')}"
                 if lp else "- Last passing scheduled run: not found in the runs still listed")
    lines += ["", "## Error lines from the daily report", ""] + [f"- {e}" for e in plan.get("errors") or []]
    if plan.get("suspect"):
        s = plan["suspect"]
        lines += ["", "## Suspect PR named by the daily report", "",
                  f"- #{s['pr']} (commit {s['sha']}), confidence {s['confidence']}: {s.get('evidence', '')}",
                  "- Its diff: `.ci-context/suspect-pr.diff`."]
    lines += ["", "## GPU re-run", "",
              f"- Workflow: `{plan['recipe']['workflow']}`",
              f"- Inputs: `{json.dumps(plan['recipe']['inputs'])}`"]
    return "\n".join(lines) + "\n"


def context_list(files: dict[str, str]) -> str:
    """The prompt's list of context files; the artifacts (up to ``MAX_ARTIFACT_FILES``) as one folder."""
    names = sorted(n for n in files if not n.startswith("artifacts/"))
    arts = sum(n.startswith("artifacts/") for n in files)
    return ", ".join([f".ci-context/{n}" for n in names] +
                     ([f".ci-context/artifacts/ ({arts} files)"] if arts else []))


def run_fix_agent(wt: Path, plan: dict, attempt: int, base_sha: str, context: dict[str, str],
                  feedback: str = "") -> dict:
    files = dict(context)
    if feedback:
        files["feedback.md"] = feedback
    prompt = "\n".join([
        "Task: CI Autofix",
        f"Problem: {plan['name']}",
        f"Attempt: {attempt} of {MAX_ATTEMPTS}",
        f"Base: main at {base_sha}",
        "Context: " + context_list(files),
    ])
    text = claude_code_analyze(prompt, work_dir=wt, context_files=files, max_turns=AGENT_MAX_TURNS,
                               timeout_secs=AGENT_TIMEOUT_SECS, output_must_contain="```json autofix")
    return parse_fix_report(text)


def run_review_agent(wt: Path, plan: dict, fix: dict, diff: str, title: str, context: dict[str, str],
                     previous_review: dict | None = None) -> dict:
    files = {
        "problem.md": context["problem.md"],
        "fix.json": json.dumps(fix, indent=2),
        "patch.diff": diff,
        "pr-draft.md": f"# {title}\n\n" + "\n\n".join(f"## {k}\n\n{v}" for k, v in fix["pr"].items()),
    }
    if previous_review:  # the fixer revised after this verdict
        files["previous-review.json"] = json.dumps(previous_review, indent=2)
    prompt = "\n".join([
        "Task: CI Autofix Review",
        f"Problem: {plan['name']}",
        "Context: " + ", ".join(f".ci-context/{n}" for n in sorted(files)),
    ])
    text = claude_code_analyze(prompt, work_dir=wt, context_files=files, max_turns=REVIEW_MAX_TURNS,
                               timeout_secs=REVIEW_TIMEOUT_SECS, output_must_contain="```json autofix-review")
    return parse_review(text)


def run_checks(wt: Path, files: list[str]) -> tuple[list[str], dict]:
    """pre-commit on the changed files (run twice: formatters may fix the first), and
    ``pytest --collect-only`` on changed test files; a test that needs a GPU-only
    package falls back to compiling it."""
    problems, checks = [], {"pre_commit": False, "tests_changed": False}
    # These run code the agent wrote (its tests, its hook config): no token may be in reach.
    env = env_without_tokens()
    existing = [f for f in files if (wt / f).exists()]
    if existing:
        out = ""
        for _ in range(2):
            # As a module: pip --user puts the `pre-commit` script in ~/.local/bin, often not on PATH.
            pc = subprocess.run([sys.executable, "-m", "pre_commit", "run", "--files", *existing], cwd=wt,
                                capture_output=True, text=True, timeout=1800, env=env)
            out = pc.stdout + pc.stderr
            if pc.returncode == 0:
                checks["pre_commit"] = True
                break
        if not checks["pre_commit"]:
            problems.append("pre-commit fails: " + " ".join(out.split())[-400:])
    tests = [f for f in existing if f.endswith(".py") and TEST_PATH_RE.search(f)]
    checks["tests_changed"] = bool(tests)
    for f in tests:
        r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", f], cwd=wt,
                           capture_output=True, text=True, timeout=600,
                           env={**env, "PYTHONPATH": str(wt / "python")})
        out = r.stdout + r.stderr
        if r.returncode == 0:
            continue
        missing = re.search(r"No module named '([\w.]+)'", out)
        if missing and not missing.group(1).startswith("sglang"):
            c = subprocess.run([sys.executable, "-m", "py_compile", f], cwd=wt, capture_output=True, text=True,
                               env=env)
            if c.returncode == 0:
                continue
        problems.append(f"{f} does not collect: " + " ".join(out.split())[-300:])
    return problems, checks


@dataclass
class AttemptResult:
    status: str                  # ready / failed / model-unavailable / no-fix
    reason: str = ""
    record: dict = field(default_factory=dict)


def deepen_clone(plans: list[dict]) -> None:
    """Fetch main back to the oldest failure window of *plans*, once, before the attempts start.

    ``--shallow-since`` can also make history shorter, so it must not run while
    an agent reads the clone's history.
    """
    dates = [d for p in plans for d in [(p.get("last_pass") or p.get("first_fail") or {}).get("date")] if d]
    if dates:
        git(SGLANG_REPO_PATH, "fetch", "-q", "origin", "main", f"--shallow-since={min(dates)}",
            timeout=900, check=False)


def start_attempt(plan: dict, attempt: int, gh: GitHub, base_ref: str = "origin/main",
                  previous: str = "", branch_tag: str = "") -> AttemptResult:
    """Run the fix and review agents and gate the result; ``ready`` carries the patch.

    Runs in the ``fix`` job, which holds no token that can write anywhere; the
    ``publish`` job pushes the patch (``push_and_dispatch()``). The caller has
    run ``ensure_sglang_repo()`` and ``deepen_clone()``; attempts run in
    parallel threads.
    """
    with REPO_LOCK:  # `git worktree add` on the shared clone is not safe to run concurrently
        base_sha = git(SGLANG_REPO_PATH, "rev-parse", base_ref).strip()
        wt = create_agent_worktree(f"autofix-{plan['id']}-{attempt}{branch_tag}", base_sha)
    try:
        if git(wt, "rev-parse", "HEAD").strip() != base_sha:
            raise RuntimeError(f"could not check out {base_sha}")
        context = {"problem.md": render_problem_context(plan), "ci-runtime-env.md": collect_ci_runtime_env(wt)}
        for j in plan["jobs"][:3]:
            text = "\n\n".join(segment_text(gh.job_log(j["job_id"]), f) for f, _ in plan["tests"]) or \
                "\n".join(clean_log(gh.job_log(j["job_id"]))[-400:])
            context[f"logs/{j['job_id']}.txt"] = text[-60_000:]
        for j in plan["jobs"][:3]:  # server-side logs that only the run's artifacts hold
            name = parse_job_name(j["job_name"])
            try:
                run_id = run_id_of(j["run_url"])
                arts = gh.log_artifacts(run_id, name.rocm, prefer=name.base)
            except Exception as exc:  # the job logs are still there; say what is missing
                log.warning("%s: artifacts of %s unreadable (%s)", plan["id"], j["run_url"], exc)
                continue
            for path, text in arts.items():
                context.setdefault(f"artifacts/{run_id}/{path}", text)
        if plan.get("suspect"):
            context["suspect-pr.diff"] = gh.pr_diff(plan["suspect"]["pr"])[:200_000]
        lp, ff = plan.get("last_pass") or {}, plan.get("first_fail") or {}
        if lp.get("sha") and ff.get("sha"):
            commits = gh.compare(lp["sha"], ff["sha"])
            context["window-commits.md"] = "\n".join(
                f"- {c['sha'][:10]} {c['commit']['message'].splitlines()[0]}" for c in commits[-250:]) or "(none)"
        if previous:
            context["previous-attempt.md"] = previous

        feedback = ""
        last_review: dict | None = None
        for turn in (1, 2):
            fix, review, checks, diff, title = None, {}, {}, "", ""
            try:
                fix = run_fix_agent(wt, plan, attempt, base_sha, context, feedback)
            except ValueError as exc:  # no report block, bad JSON, missing fields
                problems = [f"report: {exc}"]
            else:
                if fix.get("status") == "no-fix":
                    return AttemptResult("no-fix", fix.get("reason") or "the agent found no fix it could verify")
                diff = staged_diff(wt)
                files = list(diff_files(diff))
                title = build_title(fix, files) if files else ""
                problems = gate_patch(diff, fix)
                if not problems:
                    problems = check_title(title, fix["fix_kind"]) + check_pr_text(fix)
                if not problems:
                    problems, checks = run_checks(wt, files)
                    diff = staged_diff(wt)  # pre-commit may have reformatted
                if not problems:
                    try:
                        review = run_review_agent(wt, plan, fix, diff, title, context, last_review)
                        last_review = review
                    except ValueError as exc:
                        review = {"verdict": "FAIL", "reasons": [f"the review agent's report: {exc}"]}
                    if review["verdict"] != "PASS":
                        problems = [f"review: {r}" for r in review.get("reasons") or ["FAIL"]]
            if not problems:
                break
            log.info("%s attempt %d, turn %d rejected: %s", plan["id"], attempt, turn, problems)
            if turn == 2:
                return AttemptResult("failed", "; ".join(problems)[:500])
            feedback = "# Rejected\n\n" + "\n".join(f"- {p}" for p in problems) + "\n"

        final = staged_diff(wt)  # what is pushed is gated too, after pre-commit and pytest ran
        late = gate_patch(final, fix)
        if late:
            return AttemptResult("failed", "; ".join(late)[:500])
        message = f"{title}\n\n{_first_sentence(fix['pr']['motivation'])}\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
        return AttemptResult("ready", record={
            "branch": f"{BRANCH_PREFIX}{branch_tag}{plan['id'].lower()}-{_slug(plan['name'])}",
            "base_sha": base_sha, "fix": fix, "title": title, "message": message,
            "patch": staged_patch(wt), "diff": final[-MAX_STATE_DIFF:],
            "checks": {**checks, "review": review.get("verdict", "")},
        })
    except ModelUnavailable as exc:
        return AttemptResult("model-unavailable", str(exc)[:300])
    except RuntimeError as exc:  # the agent failed or timed out, or git did
        return AttemptResult("failed", f"agent or git failed: {exc}"[:300])
    finally:
        with REPO_LOCK:
            remove_agent_worktree(wt)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "fix"


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]


def previous_attempt_text(rec: dict, verification: dict, gh: GitHub, plan: dict) -> str:
    """``.ci-context/previous-attempt.md`` for a second attempt after the GPU run failed."""
    lines = ["# Previous attempt: the GPU re-run failed", "",
             f"Run: {verification.get('run_url')}", "", "## Failed checks", ""]
    lines += [f"- {r}" for r in verification.get("reasons") or []]
    lines += ["", "## The previous change", "", "```diff", rec.get("diff", "")[-15_000:], "```", ""]
    lines += ["## The previous root cause", "", str(rec.get("fix", {}).get("root_cause", "")), ""]
    for r in verification.get("results") or []:
        if not r["status"].startswith("passed"):
            job_id = int(r["job_url"].rstrip("/").rsplit("/", 1)[-1])
            lines += [f"## Log of `{r['file']}` in the run", "", "```",
                      segment_text(gh.job_log(job_id), r["file"])[-30_000:], "```", ""]
    return "\n".join(lines)
