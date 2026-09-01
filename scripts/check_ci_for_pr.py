#!/usr/bin/env python3
"""
amd-bot CI status checker for a specific PR.

Extracts error messages structurally, uses a single LLM call to assess
PR correlation, and outputs separate AMD / Other CI tables for developers
to scan in 5 seconds.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from utils import (
    CLAUDE_MODEL,
    REPO,
    SGLANG_REPO_PATH,
    agent_worktree,
    claude_code_analyze,
    claude_code_available,
    create_anthropic_client,
    download_job_logs,
    ensure_sglang_repo,
    extract_error_lines,
    get_file_content_at_ref,
    get_pr_changed_files,
    get_pr_diff,
    get_run_jobs,
    get_workflow_runs_for_sha,
    gh_headers,
    is_gate_job,
    load_prompt_template,
    post_comment,
)

MAX_DIFF_CHARS = 50_000


# ---------------------------------------------------------------------------
# PR / Workflow helpers
# ---------------------------------------------------------------------------

def get_pr_head_sha(token: str, pr_number: int) -> str:
    """Get the head commit SHA for a PR."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    resp = requests.get(url, headers=gh_headers(token))
    resp.raise_for_status()
    return resp.json()["head"]["sha"]


def collect_workflow_status(token: str, head_sha: str) -> dict:
    """Collect all workflow runs for a SHA, grouped by workflow."""
    wf_runs = get_workflow_runs_for_sha(token, head_sha)

    latest_by_wf: dict[str, dict] = {}
    for run in wf_runs:
        wf_name = run.get("name", run.get("path", "unknown"))
        existing = latest_by_wf.get(wf_name)
        if existing is None or run["id"] > existing["id"]:
            latest_by_wf[wf_name] = run

    passed_names: list[str] = []
    pending_names: list[str] = []
    failed_workflows: list[dict] = []

    for wf_name, run in sorted(latest_by_wf.items()):
        status = run.get("status")
        conclusion = run.get("conclusion")

        if conclusion == "success":
            passed_names.append(wf_name)
        elif status in ("in_progress", "queued", "waiting", "requested"):
            pending_names.append(wf_name)
        elif conclusion in ("failure", "timed_out", "action_required"):
            jobs = get_run_jobs(token, run["id"])
            failed_jobs = [
                j for j in jobs
                if j.get("conclusion") in ("failure", "timed_out")
            ]
            if failed_jobs:
                failed_workflows.append({
                    "name": wf_name,
                    "run_id": run["id"],
                    "run_url": run["html_url"],
                    "failed_jobs": failed_jobs,
                })
        elif conclusion == "cancelled":
            pass
        else:
            pending_names.append(wf_name)

    return {
        "passed_names": passed_names,
        "pending_names": pending_names,
        "failed_workflows": failed_workflows,
    }


def _is_amd_workflow(name: str) -> bool:
    """Return True if the workflow name indicates an AMD CI workflow."""
    return "amd" in name.lower()


# ---------------------------------------------------------------------------
# Per-job error collection (no LLM — just log download + structural extraction)
# ---------------------------------------------------------------------------

def collect_job_errors(
    job: dict, run_id: int, run_url: str, token: str,
) -> dict | None:
    """Download log and extract errors structurally. No LLM call."""
    job_name = job["name"]
    job_id = job["id"]
    job_url = job.get("html_url", run_url)
    api_steps = job.get("steps", [])

    failed_step_names: set[str] = set()
    for s in api_steps:
        if s.get("conclusion") == "failure":
            failed_step_names.add(s["name"])
    if not failed_step_names:
        failed_step_names = {"(unknown)"}

    if is_gate_job(job):
        print(f"\n  Job: {job_name} (ID: {job_id}) — gate job, skipped")
        return {
            "job_name": job_name,
            "job_id": job_id,
            "run_id": run_id,
            "run_url": run_url,
            "job_url": job_url,
            "failed_steps": sorted(failed_step_names),
            "error_lines": [],
            "is_gate": True,
        }

    print(f"\n  Job: {job_name} (ID: {job_id})")
    print("    Downloading job log...")
    raw_log = download_job_logs(token, job_id)
    print(f"    Log size: {len(raw_log):,} chars")

    print("    Extracting errors...")
    error_lines = extract_error_lines(raw_log, api_steps, run_id, job_id)
    print(f"    Found {len(error_lines)} error line(s)")

    return {
        "job_name": job_name,
        "job_id": job_id,
        "run_id": run_id,
        "run_url": run_url,
        "job_url": job_url,
        "failed_steps": sorted(failed_step_names),
        "error_lines": error_lines,
        "is_gate": False,
    }


# ---------------------------------------------------------------------------
# Pick the best error message for the summary table
# ---------------------------------------------------------------------------

def _pick_best_error(ja: dict) -> dict | None:
    """Pick the most relevant error line like a human expert.

    Priority: ##[error] annotations > Python exceptions > tail lines.
    Among exceptions, the LONGEST preview wins — root cause errors have
    detailed messages while cascading/cleanup errors are terse.
    """
    if not ja["error_lines"]:
        return None

    annotations = [e for e in ja["error_lines"] if e.get("source") == "annotation"]
    if annotations:
        return max(annotations, key=lambda e: len(e["preview"]))

    exceptions = [e for e in ja["error_lines"] if e.get("source") == "exception"]
    if exceptions:
        return max(exceptions, key=lambda e: len(e["preview"]))

    return ja["error_lines"][-1]


# ---------------------------------------------------------------------------
# PR correlation analysis (single LLM call, returns structured data)
# ---------------------------------------------------------------------------

def _parse_correlation_json(raw: str) -> list[dict]:
    """Extract a JSON array from LLM/agent output, stripping markdown fences."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"  WARNING: Failed to parse correlation JSON")
        return []


def analyze_pr_correlation(
    client,
    pr_number: int,
    changed_files: list[dict],
    pr_diff: str,
    job_analyses: list[dict],
    use_agent: bool = True,
    repo_path: "Path | None" = None,
) -> list[dict]:
    """Assess whether each CI failure correlates with the PR.

    When *use_agent* is True, delegates to the Claude Code agent which can
    read sglang source code to trace call chains and verify correlation.
    Falls back to API on agent failure.

    Returns a list of dicts: [{job, verdict, explanation}, ...].
    """
    files_summary = "\n".join(
        f"- `{f['filename']}` ({f.get('status', '?')}, "
        f"+{f.get('additions', 0)}/-{f.get('deletions', 0)})"
        for f in changed_files[:50]
    )
    if len(changed_files) > 50:
        files_summary += f"\n- ... and {len(changed_files) - 50} more files"

    diff_text = pr_diff[:MAX_DIFF_CHARS]
    if len(pr_diff) > MAX_DIFF_CHARS:
        diff_text += "\n\n... [diff truncated] ..."

    errors_text = ""
    for ja in job_analyses:
        errors_text += f"\n#### Job: `{ja['job_name']}`\n"
        errors_text += f"Failed step(s): {', '.join(ja['failed_steps'])}\n"
        if ja["error_lines"]:
            for el in ja["error_lines"][:5]:
                errors_text += f"- `{el['preview']}`\n"

    job_names = [ja["job_name"] for ja in job_analyses]
    job_list = "\n".join(f'  - "{name}"' for name in job_names)

    if use_agent:
        try:
            work_dir = repo_path or SGLANG_REPO_PATH
            if not work_dir.exists():
                raise FileNotFoundError(f"Repo not found at {work_dir}")

            prompt = (
                f"Task: PR Correlation\n"
                f"PR: #{pr_number}\n"
                f"Job list:\n{job_list}\n"
                f"Context files: .ci-context/pr-diff.txt, "
                f".ci-context/pr-files.txt, .ci-context/ci-errors.md\n"
                f"Source: current directory\n"
                f"GitHub API token: $GH_PAT"
            )
            raw = claude_code_analyze(
                prompt=prompt,
                work_dir=work_dir,
                context_files={
                    "pr-diff.txt": diff_text,
                    "pr-files.txt": files_summary,
                    "ci-errors.md": errors_text,
                },
                max_turns=50,
                timeout_secs=300,
            )
            return _parse_correlation_json(raw)
        except Exception as exc:
            print(f"  WARNING: Agent correlation failed ({exc}), falling back to API")

    if client is None:
        client = create_anthropic_client()

    template = load_prompt_template("pr-correlation")
    if template:
        prompt = template.format(
            pr_number=pr_number,
            files_summary=files_summary,
            diff_text=diff_text,
            errors_text=errors_text,
            job_list=job_list,
        )
    else:
        prompt = (
            f"You are a CI/CD expert. PR #{pr_number} to sglang has CI failures.\n\n"
            f"## PR Changed Files\n{files_summary}\n\n"
            f"## PR Diff\n```\n{diff_text}\n```\n\n"
            f"## CI Failures\n{errors_text}\n\n"
            f"For each job:\n{job_list}\n\n"
            f"Return JSON: [{{\"job\": \"name\", \"test_file\": \"test/path/test_foo.py\", "
            f"\"test_function\": \"test_func_name\", \"verdict\": \"likely|possibly|unlikely\", "
            f"\"explanation\": \"one sentence\"}}]. One entry per failing test file."
        )

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_correlation_json(msg.content[0].text)


# ---------------------------------------------------------------------------
# Comment formatting — AMD and Other tables
# ---------------------------------------------------------------------------

_VERDICT_DISPLAY = {
    "likely": ":red_circle: **Likely**",
    "possibly": ":yellow_circle: **Possibly**",
    "unlikely": ":green_circle: **Unlikely**",
}


_VERDICT_SORT_ORDER = {"likely": 0, "possibly": 1, "unlikely": 2}


def _render_table(
    jobs: list[dict],
    corr_by_job: dict[str, dict],
) -> str:
    """Render a single markdown table for a list of job analyses."""
    rows = "| Workflow | Job | Error | Related? | Log |\n"
    rows += "|----------|-----|-------|----------|-----|\n"

    for ja in jobs:
        best = _pick_best_error(ja)
        if best:
            preview = best["preview"]
            if len(preview) > 200:
                preview = preview[:200] + "..."
            preview = preview.replace("|", "\\|")
            log_link = f"[View]({best['url']})"
        else:
            preview = "*(no error extracted)*"
            log_link = f"[View]({ja['job_url']})"

        corr = corr_by_job.get(ja["job_name"], {})
        verdict = corr.get("verdict", "")
        explanation = corr.get("explanation", "")
        display = _VERDICT_DISPLAY.get(verdict, "")

        if display and explanation:
            related_cell = f"{display} -- {explanation}"
        elif display:
            related_cell = display
        else:
            related_cell = "*(pending)*"

        related_cell = related_cell.replace("|", "\\|")
        wf = ja.get("workflow_name", "")

        rows += f"| {wf} | `{ja['job_name']}` | `{preview}` | {related_cell} | {log_link} |\n"

    return rows


def _format_grouped_tables(
    analyses: list[dict],
    correlation: list[dict],
) -> str:
    """Build two tables — AMD CI Failures and Other CI Failures.

    Each table is sorted: likely-related first, then possibly, then unlikely.
    Groups with zero failures are omitted.
    """
    corr_by_job: dict[str, dict] = {}
    for c in correlation:
        corr_by_job[c.get("job", "")] = c

    real = [ja for ja in analyses if not ja.get("is_gate")]

    amd_jobs = [ja for ja in real if _is_amd_workflow(ja.get("workflow_name", ""))]
    other_jobs = [ja for ja in real if not _is_amd_workflow(ja.get("workflow_name", ""))]

    def _sort_key(ja):
        return (
            _VERDICT_SORT_ORDER.get(
                corr_by_job.get(ja["job_name"], {}).get("verdict", ""), 3
            ),
            ja.get("workflow_name", ""),
        )

    amd_jobs.sort(key=_sort_key)
    other_jobs.sort(key=_sort_key)

    body = ""
    if amd_jobs:
        body += "### AMD CI Failures\n\n"
        body += _render_table(amd_jobs, corr_by_job)
        body += "\n"
    if other_jobs:
        body += "### Other CI Failures\n\n"
        body += _render_table(other_jobs, corr_by_job)
        body += "\n"
    return body


# ---------------------------------------------------------------------------
# Untested-change detection (AMD coverage gap)
# ---------------------------------------------------------------------------

_AMD_REGISTER_RE = re.compile(r"register_amd_ci\s*\(([^)]*)\)", re.DOTALL)
_SUITE_KW_RE = re.compile(r"""suite\s*=\s*["']([^"']+)["']""")
_NIGHTLY_KW_RE = re.compile(r"nightly\s*=\s*True")
_STAGE_RE = re.compile(r"stage-([abc])")


def _extract_amd_pr_suite(content: str) -> str | None:
    """Return the per-commit AMD suite a test registers to, or None.

    Skips nightly-only registrations (``nightly=True``) since those never run
    on PR CI, and files that don't register an AMD suite at all.
    """
    m = _AMD_REGISTER_RE.search(content)
    if not m:
        return None
    args = m.group(1)
    if _NIGHTLY_KW_RE.search(args):
        return None
    sm = _SUITE_KW_RE.search(args)
    return sm.group(1) if sm else None


def _stage_label(suite: str) -> str:
    """Human stage label from a suite name, e.g. ``"stage B"`` (or ``""``)."""
    m = _STAGE_RE.search(suite)
    return f"stage {m.group(1).upper()}" if m else ""


def _job_matches_suite(job_name: str, suite: str) -> bool:
    """Match an Actions job name to a suite (exact, or ``suite (matrix...)``)."""
    return job_name == suite or job_name.startswith(suite + " ")


def _collect_amd_runs(token: str, head_sha: str) -> list[dict]:
    """Latest AMD workflow run per workflow name for this SHA."""
    latest_by_wf: dict[str, dict] = {}
    for run in get_workflow_runs_for_sha(token, head_sha):
        name = run.get("name", "")
        if "amd" not in name.lower():
            continue
        existing = latest_by_wf.get(name)
        if existing is None or run["id"] > existing["id"]:
            latest_by_wf[name] = run
    return list(latest_by_wf.values())


def _collect_amd_jobs(token: str, runs: list[dict]) -> list[dict]:
    """All jobs across the given AMD workflow runs."""
    jobs: list[dict] = []
    for run in runs:
        jobs.extend(get_run_jobs(token, run["id"]))
    return jobs


def _suite_run_state(jobs: list[dict], suite: str) -> tuple[str, str]:
    """Classify whether ``suite``'s AMD job(s) actually executed.

    Returns ``(state, reason)`` where state is ``"executed"``, ``"pending"``,
    or ``"not_run"``. A matrix suite counts as executed if any shard executed.
    """
    matched = [j for j in jobs if _job_matches_suite(j.get("name", ""), suite)]
    if not matched:
        return "not_run", "not reached"
    executed = pending = False
    conclusions: set[str] = set()
    for j in matched:
        status = j.get("status")
        conclusion = j.get("conclusion")
        if conclusion:
            conclusions.add(conclusion)
        if conclusion in ("success", "failure", "timed_out"):
            executed = True
        elif conclusion is None or status in (
            "in_progress", "queued", "waiting", "requested", "pending",
        ):
            pending = True
    if executed:
        return "executed", ""
    if pending:
        return "pending", "still running / queued"
    if "cancelled" in conclusions:
        return "not_run", "cancelled"
    if "skipped" in conclusions:
        return "not_run", "skipped"
    return "not_run", "did not complete"


def detect_untested_amd_changes(
    token: str,
    pr_number: int,
    changed_files: list[dict],
    head_sha: str,
) -> list[dict]:
    """Find changed AMD test files whose covering AMD job did not run.

    Deterministic signal: a changed ``test/**`` file declares its suite via
    ``register_amd_ci(suite=...)``; that suite maps to an AMD CI job, and we
    check whether it executed for this commit. Nightly-only tests are skipped
    (they never run on PR CI). If AMD CI was never triggered at all, every
    relevant suite is reported as ``not_triggered`` (the most dangerous case).
    """
    mapped: list[tuple[str, str]] = []
    for f in changed_files:
        filename = f.get("filename", "")
        if (
            f.get("status") == "removed"
            or not filename.startswith("test/")
            or not filename.endswith(".py")
        ):
            continue
        content = get_file_content_at_ref(token, filename, head_sha)
        if not content:
            continue
        suite = _extract_amd_pr_suite(content)
        if suite:
            mapped.append((filename, suite))
    if not mapped:
        return []

    amd_runs = _collect_amd_runs(token, head_sha)
    jobs = _collect_amd_jobs(token, amd_runs) if amd_runs else []
    any_amd_failure = any(
        j.get("conclusion") in ("failure", "timed_out") for j in jobs
    )

    entries: list[dict] = []
    for filename, suite in mapped:
        if not amd_runs:
            state, reason = "not_triggered", "AMD CI was not triggered for this commit"
        else:
            state, reason = _suite_run_state(jobs, suite)
            if state == "not_run" and reason == "skipped" and any_amd_failure:
                reason = "blocked by an earlier stage"
        if state == "executed":
            continue
        entries.append({
            "test_file": filename,
            "suite": suite,
            "stage": _stage_label(suite),
            "state": state,
            "reason": reason,
        })
    return entries


def _format_untested_warning(entries: list[dict]) -> str:
    """Render a short, scannable banner for changes not tested on AMD.

    One headline (what + action) plus one short bullet per file. All entries
    are homogeneous: either every relevant suite is ``not_triggered`` (AMD CI
    never ran) or none are.
    """
    if not entries:
        return ""
    not_triggered = any(e["state"] == "not_triggered" for e in entries)

    if not_triggered:
        lines = [
            "> [!CAUTION]",
            "> **AMD CI did not run for this PR — these changes are untested on "
            "AMD. Trigger / re-run AMD CI before merging.**",
        ]
    else:
        lines = [
            "> [!WARNING]",
            "> **A code path changed by this PR was not tested on AMD — re-run "
            'AMD CI before merging.** A green / "Unlikely related" status below '
            "does **not** mean it's verified.",
        ]

    for e in entries:
        loc = f", {e['stage']}" if e["stage"] else ""
        if e["state"] == "not_triggered":
            note = "did not run"
        elif e["state"] == "pending":
            note = "not finished yet"
        else:
            note = f"did not run ({e['reason']})"
        lines.append(f"> - `{e['test_file']}` → `{e['suite']}`{loc}: {note}")

    return "\n".join(lines) + "\n\n"


def _strip_preamble(report: str) -> str:
    """Drop any 'thinking aloud' prose the agent emitted before the title.

    The PR CI Status report must start at its ``## CI Status for PR`` heading.
    Agents sometimes prepend "I have enough to compose the report..." which
    otherwise leaks verbatim into the GitHub comment. The agent owns the whole
    report (including the coverage banner) in agent mode, so the only thing we
    enforce deterministically here is that nothing precedes the title.
    """
    idx = report.find("## CI Status for PR")
    return report[idx:] if idx > 0 else report


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _check_ci_with_agent(pr_number: int, repo_path) -> str:
    """Let the agent check PR CI status (methodology defined in CLAUDE.md)."""
    prompt = (
        f"Task: PR CI Status Check\n"
        f"PR: #{pr_number}\n"
        f"Repo: sgl-project/sglang\n"
        f"Source: current directory (checked out to PR branch)\n"
        f"GitHub API token: $GH_PAT"
    )
    return claude_code_analyze(
        prompt=prompt,
        work_dir=repo_path,
        timeout_secs=1200,
        output_must_contain="## CI Status for PR",
    )


def check_ci_for_pr(
    token: str,
    pr_number: int,
    post_comment_flag: bool = True,
    use_agent: bool = True,
) -> str:
    """Check CI status for a PR: one table with errors + PR correlation."""
    comment_author = os.environ.get("COMMENT_AUTHOR", "")
    requester_line = f"> @{comment_author}\n\n" if comment_author else ""

    if use_agent:
        if not claude_code_available():
            print("  WARNING: --use-agent but Claude Code not found, falling back to API")
            use_agent = False
        else:
            try:
                print(f"Checking CI for PR #{pr_number} (agent mode, worktree)...")
                with agent_worktree(f"ci-status-pr{pr_number}") as wt_path:
                    report = _check_ci_with_agent(pr_number, wt_path)
                    report = _strip_preamble(report)
                    body = requester_line + report
                    body += "\n---\n*Generated by amd-bot using Claude Code CLI*\n"

                    if post_comment_flag:
                        result = post_comment(token, REPO, pr_number, body)
                        print(f"\n  Posted: {result['html_url']}")
                        return result["html_url"]
                    print(body)
                    return body
            except Exception as exc:
                print(f"  WARNING: Full agent mode failed ({exc}), using hybrid approach")

    print(f"Checking CI for PR #{pr_number}...")

    head_sha = get_pr_head_sha(token, pr_number)
    print(f"  Head SHA: {head_sha[:12]}")

    coverage_changed_files = get_pr_changed_files(token, pr_number)
    untested_block = ""
    try:
        untested = detect_untested_amd_changes(
            token, pr_number, coverage_changed_files, head_sha
        )
        untested_block = _format_untested_warning(untested)
        if untested:
            print(f"  Untested AMD change(s): {len(untested)}")
    except Exception as exc:
        print(f"  WARNING: untested-change detection failed ({exc})")

    status = collect_workflow_status(token, head_sha)
    passed_names = status["passed_names"]
    pending_names = status["pending_names"]
    failed_workflows = status["failed_workflows"]

    print(
        f"  Workflows — Passed: {len(passed_names)}, "
        f"Failed: {len(failed_workflows)}, Pending: {len(pending_names)}"
    )

    requester_line = f"> @{comment_author}\n\n" if comment_author else ""

    if not failed_workflows:
        pending_note = f" ({len(pending_names)} still pending)" if pending_names else ""
        body = (
            f"{requester_line}## CI Status for PR #{pr_number}\n\n"
            f"{untested_block}"
            f"All {len(passed_names)} workflow(s) passed!{pending_note}\n"
            f"\n---\n*Generated by amd-bot using Claude API*\n"
        )
    else:
        all_job_data: list[dict] = []

        jobs_to_analyze: list[tuple[dict, dict]] = [
            (wf, job)
            for wf in failed_workflows
            for job in wf["failed_jobs"]
            if not is_gate_job(job)
        ]
        n_jobs = len(jobs_to_analyze)
        max_workers = min(n_jobs + 2, 6)
        print(f"\n  Collecting errors from {n_jobs} job(s) (workers={max_workers})...")

        # Phase 1: download logs + extract errors (parallel, no LLM)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            job_futures = {}
            for wf, job in jobs_to_analyze:
                fut = executor.submit(
                    collect_job_errors, job,
                    wf["run_id"], wf["run_url"], token,
                )
                job_futures[fut] = wf

            diff_future = executor.submit(get_pr_diff, token, pr_number)

            for fut in as_completed(job_futures):
                wf = job_futures[fut]
                try:
                    ja = fut.result()
                except Exception as exc:
                    print(f"  ERROR collecting job in {wf['name']}: {exc}")
                    continue
                if ja:
                    ja["workflow_name"] = wf["name"]
                    all_job_data.append(ja)

            pr_diff = diff_future.result()
            changed_files = coverage_changed_files

        print(
            f"\n  PR diff: {len(pr_diff):,} chars, "
            f"{len(changed_files)} file(s) changed"
        )

        real_jobs = [ja for ja in all_job_data if not ja.get("is_gate")]

        # Phase 2: PR correlation (agent when available, API fallback)
        correlation: list[dict] = []
        if real_jobs and pr_diff:
            agent_repo = None
            if use_agent:
                print("\n  Running PR correlation analysis (agent)...")
                try:
                    agent_repo = ensure_sglang_repo()
                except Exception:
                    pass
            else:
                print("\n  Running PR correlation analysis (1 LLM call)...")

            correlation = analyze_pr_correlation(
                None, pr_number, changed_files, pr_diff, real_jobs,
                use_agent=use_agent and agent_repo is not None,
                repo_path=agent_repo,
            )
            print(f"  Got {len(correlation)} correlation verdict(s)")

        # Phase 3: build header + verdict summary + grouped tables
        body = f"{requester_line}## CI Status for PR #{pr_number}\n\n"
        body += untested_block

        corr_by_job: dict[str, dict] = {}
        for c in correlation:
            corr_by_job[c.get("job", "")] = c

        amd_jobs = [ja for ja in real_jobs if _is_amd_workflow(ja.get("workflow_name", ""))]
        other_jobs = [ja for ja in real_jobs if not _is_amd_workflow(ja.get("workflow_name", ""))]

        def _group_summary(jobs: list[dict], label: str) -> str:
            n = len(jobs)
            if n == 0:
                return f"**{label}: 0 failures**"
            n_related = sum(
                1 for ja in jobs
                if corr_by_job.get(ja["job_name"], {}).get("verdict") in ("likely", "possibly")
            )
            return f"**{label}: {n} failure{'s' if n != 1 else ''} ({n_related} likely related)**"

        amd_passed = [n for n in passed_names if _is_amd_workflow(n)]
        amd_pending = [n for n in pending_names if _is_amd_workflow(n)]
        other_passed = [n for n in passed_names if not _is_amd_workflow(n)]
        other_pending = [n for n in pending_names if not _is_amd_workflow(n)]

        body += _group_summary(amd_jobs, "AMD") + " · " + _group_summary(other_jobs, "Others") + "\n\n"

        status_parts = []
        if amd_passed or amd_pending:
            s = f"AMD: {len(amd_passed)} passed"
            if amd_pending:
                s += f", {len(amd_pending)} pending"
            status_parts.append(s)
        if other_passed or other_pending:
            s = f"Others: {len(other_passed)} passed"
            if other_pending:
                s += f", {len(other_pending)} pending"
            status_parts.append(s)
        if status_parts:
            body += " | ".join(status_parts) + "\n\n"

        body += _format_grouped_tables(all_job_data, correlation)
        method = "Claude Code CLI" if use_agent else "Claude API"
        body += f"\n---\n*Generated by amd-bot using {method}*\n"

    if post_comment_flag:
        result = post_comment(token, REPO, pr_number, body)
        print(f"\n  Posted: {result['html_url']}")
        return result["html_url"]

    print(body)
    return body


def main():
    parser = argparse.ArgumentParser(
        description="Check CI status for a sglang PR",
    )
    parser.add_argument("pr_number", type=int, help="PR number")
    parser.add_argument(
        "--no-post", action="store_true", help="Print only, don't post",
    )
    parser.add_argument(
        "--use-agent", action=argparse.BooleanOptionalAction,
        default=os.environ.get("USE_AGENT", "").lower() not in ("false", "0", "no"),
        help="Use Claude Code agent (default: enabled, use --no-use-agent to disable)",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")),
    )

    args = parser.parse_args()

    if not args.github_token:
        print("Error: GitHub token required. Set GH_PAT.", file=sys.stderr)
        sys.exit(1)

    if not args.use_agent:
        if not os.environ.get("LLM_GATEWAY_KEY"):
            print("Error: LLM_GATEWAY_KEY env var required.", file=sys.stderr)
            sys.exit(1)
        if not os.environ.get("LLM_GATEWAY_URL"):
            print("Error: LLM_GATEWAY_URL env var required.", file=sys.stderr)
            sys.exit(1)

    check_ci_for_pr(
        args.github_token,
        args.pr_number,
        post_comment_flag=not args.no_post,
        use_agent=args.use_agent,
    )


if __name__ == "__main__":
    main()
