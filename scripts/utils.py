"""
Shared utilities for amd-bot scripts.

Provides GitHub API helpers, Anthropic client creation, log parsing,
CI log analysis, and Claude Code agent integration.
"""

import contextlib
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import anthropic
import httpx
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_OWNER = "sgl-project"
REPO_NAME = "sglang"
REPO = f"{REPO_OWNER}/{REPO_NAME}"

CLAUDE_MODEL = "claude-opus-4-8"

STEP_LOG_PREFILTER_THRESHOLD = 150_000

ERROR_PATTERNS = re.compile(
    r"|".join([
        r"ERROR",
        r"FAIL(?:ED)?",
        r"Exception",
        r"Traceback",
        r"assert(?:ion)?.*(?:error|fail)",
        r"exit\s+code\s+[1-9]",
        r"TIMEOUT",
        r"OOM|Out\s*[Oo]f\s*[Mm]emory",
        r"killed|KILLED",
        r"[Ss]egmentation\s+fault|segfault|SEGFAULT",
        r"FATAL",
        r"panic",
        r"cannot\s+find",
        r"No\s+such\s+file",
        r"Permission\s+denied",
        r"ModuleNotFoundError",
        r"ImportError",
        r"RuntimeError",
        r"ConnectionError",
        r"FileNotFoundError",
    ]),
    re.IGNORECASE,
)

# Structural error signals — instead of enumerating error keywords,
# rely on CI log structure and Python language conventions.
_GH_ANNOTATION_RE = re.compile(r"##\[error\](.*)")
_PYTHON_EXCEPTION_RE = re.compile(r"\w+(?:Error|Exception)\b\s*:.+")

# Test-result boundary: everything AFTER these lines is cleanup noise.
# Covers unittest, pytest, and sglang's wrapper.
_TEST_RESULT_RE = re.compile(
    r"^(?:FAILED\b|OK$|Ran \d+ tests? in |"
    r"=== SUITE RESULT|"
    r"={3,} \d+ (?:failed|passed)|"
    r"={3,} short test summary)"
)

GATE_STEP_PATTERNS = re.compile(
    r"Check all dependent job statuses|Wait for .* jobs to complete",
    re.IGNORECASE,
)

SKIP_JOB_CONCLUSIONS = {"success", "skipped"}

_GATE_JOB_NAME_RE = re.compile(r"finish|wait-for-|check-all", re.IGNORECASE)


def is_gate_job(job: dict) -> bool:
    """Return True if the job is a coordinator/gate job (e.g. pr-test-amd-finish).

    Detects by job name (e.g. pr-test-finish, wait-for-stage-b) or by
    step names (e.g. 'Check all dependent job statuses').
    """
    if _GATE_JOB_NAME_RE.search(job.get("name", "")):
        return True
    failed_steps = [
        s for s in job.get("steps", [])
        if s.get("conclusion") == "failure"
    ]
    if not failed_steps:
        return False
    return all(GATE_STEP_PATTERNS.search(s["name"]) for s in failed_steps)


def get_failed_jobs(token: str, run_id: int) -> list[dict]:
    """Get completed non-success, non-skipped jobs for a workflow run.

    Only returns jobs whose status is 'completed' so that still-running
    jobs in an in-progress workflow are not picked up prematurely.
    """
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
    params = {"filter": "latest", "per_page": 100}
    resp = requests.get(url, headers=gh_headers(token), params=params)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])
    return [
        j for j in jobs
        if j.get("status") == "completed"
        and j.get("conclusion") not in SKIP_JOB_CONCLUSIONS
    ]


# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

def create_anthropic_client() -> anthropic.Anthropic:
    """Create Anthropic client via AMD LLM Gateway.

    Env vars:
      - LLM_GATEWAY_KEY (required) — gateway subscription key
      - LLM_GATEWAY_URL (required) — gateway endpoint
    """
    return anthropic.Anthropic(
        base_url=os.environ["LLM_GATEWAY_URL"],
        api_key="dummy",
        http_client=httpx.Client(verify=False),
        default_headers={
            "Ocp-Apim-Subscription-Key": os.environ["LLM_GATEWAY_KEY"],
            "user": getpass.getuser(),
            "anthropic-version": "vertex-2023-10-16",
        },
    )


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def download_job_logs(token: str, job_id: int) -> str:
    """Download full logs for a GitHub Actions job."""
    url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    resp = requests.get(url, headers=gh_headers(token), allow_redirects=True)
    if resp.status_code == 200:
        return resp.text
    return f"[Failed to fetch logs: HTTP {resp.status_code}]"


def post_comment(
    token: str, repo: str, issue_number: int, body: str
) -> dict:
    """Post a comment on an issue or PR."""
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments"
    resp = requests.post(url, headers=gh_headers(token), json={"body": body})
    resp.raise_for_status()
    return resp.json()


def update_comment(token: str, repo: str, comment_id: int, body: str) -> dict:
    """Update an existing comment on an issue or PR."""
    url = f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}"
    resp = requests.patch(url, headers=gh_headers(token), json={"body": body})
    resp.raise_for_status()
    return resp.json()


def get_issue(token: str, repo: str, issue_number: int) -> dict:
    """Fetch an issue's full payload (title, body, labels, ...)."""
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    resp = requests.get(url, headers=gh_headers(token))
    resp.raise_for_status()
    return resp.json()


def update_issue_body(
    token: str, repo: str, issue_number: int, body: str,
) -> dict:
    """Replace an issue's body via the REST API.

    Used by the Daily Cross-Workflow Summary to PATCH the summary content into the
    daily issue body (so it appears pinned at the very top of the
    issue), instead of posting a separate comment that would land
    below the per-workflow comments.
    """
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    resp = requests.patch(url, headers=gh_headers(token), json={"body": body})
    resp.raise_for_status()
    return resp.json()


def delete_comment(token: str, repo: str, comment_id: int) -> None:
    """Delete a comment on an issue or PR. Silently no-op on 404."""
    url = f"https://api.github.com/repos/{repo}/issues/comments/{comment_id}"
    resp = requests.delete(url, headers=gh_headers(token))
    if resp.status_code == 404:
        return
    resp.raise_for_status()


def create_github_issue(
    token: str,
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> dict:
    """Create a GitHub issue."""
    target_repo = repo or REPO
    url = f"https://api.github.com/repos/{target_repo}/issues"
    data = {"title": title, "body": body}
    if labels:
        data["labels"] = labels
    resp = requests.post(url, headers=gh_headers(token), json=data)
    resp.raise_for_status()
    return resp.json()


def get_workflow_runs_for_sha(token: str, head_sha: str) -> list[dict]:
    """List all workflow runs triggered for a given commit SHA."""
    url = f"https://api.github.com/repos/{REPO}/actions/runs"
    all_runs: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=gh_headers(token),
            params={"head_sha": head_sha, "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
        if not runs:
            break
        all_runs.extend(runs)
        if len(runs) < 100:
            break
        page += 1
    return all_runs


def get_run_jobs(token: str, run_id: int) -> list[dict]:
    """Get all jobs for a workflow run."""
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
    all_jobs: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=gh_headers(token),
            params={"filter": "latest", "per_page": 100, "page": page},
        )
        resp.raise_for_status()
        jobs = resp.json().get("jobs", [])
        if not jobs:
            break
        all_jobs.extend(jobs)
        if len(jobs) < 100:
            break
        page += 1
    return all_jobs


def get_pr_diff(token: str, pr_number: int) -> str:
    """Fetch the unified diff for a PR."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.diff"
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        return resp.text
    return ""


def get_pr_changed_files(token: str, pr_number: int) -> list[dict]:
    """Fetch the list of files changed in a PR."""
    url = f"https://api.github.com/repos/{REPO}/pulls/{pr_number}/files"
    all_files: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=gh_headers(token),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        files = resp.json()
        if not files:
            break
        all_files.extend(files)
        if len(files) < 100:
            break
        page += 1
    return all_files


def get_file_content_at_ref(token: str, path: str, ref: str) -> str | None:
    """Fetch raw file content at a specific git ref. Returns None if missing."""
    url = f"https://api.github.com/repos/{REPO}/contents/{path}"
    headers = gh_headers(token)
    headers["Accept"] = "application/vnd.github.raw"
    resp = requests.get(url, headers=headers, params={"ref": ref})
    if resp.status_code == 200:
        return resp.text
    return None


def extract_error_lines(
    raw_log: str,
    job_steps_api: list[dict],
    run_id: int,
    job_id: int,
    failed_step_names: set[str] | None = None,
    max_errors_per_step: int = 10,
) -> list[dict]:
    """Extract errors from failed steps using structural signals.

    Uses three signals based on CI log structure:

    1. ``##[error]`` annotations — GitHub Actions' own error markers.
    2. Python exception lines (``\\w+Error:`` / ``\\w+Exception:``).
    3. Fallback: last non-empty line of the step.

    Applies test-result boundary filtering: exceptions appearing AFTER
    lines like ``FAILED``, ``=== SUITE RESULT`` are cleanup noise
    (e.g. ``resource_tracker`` / ``KeyError: '/loky-...'``) and are
    discarded.  Among remaining exceptions, the most informative one
    (longest preview) is the root cause.
    """
    parsed_steps = parse_log_by_steps(raw_log)

    step_num_map: dict[str, int] = {}
    for s in job_steps_api:
        step_num_map[s["name"]] = s["number"]

    errors: list[dict] = []
    for parsed_step in parsed_steps:
        step_name = parsed_step["name"]

        if failed_step_names is not None and step_name not in failed_step_names:
            continue

        step_num = step_num_map.get(step_name)
        lines = parsed_step["content"].split("\n")
        step_errors: list[dict] = []

        # Find the test-result boundary (scan backward for FAILED / SUITE RESULT / etc.)
        result_boundary = len(lines)
        for i in range(len(lines) - 1, -1, -1):
            clean = _TIMESTAMP_RE.sub("", lines[i]).strip()
            if _TEST_RESULT_RE.match(clean):
                result_boundary = i
                break

        for line_idx, line in enumerate(lines):
            clean = _TIMESTAMP_RE.sub("", line).strip()
            if not clean:
                continue

            ann = _GH_ANNOTATION_RE.search(clean)
            if ann:
                msg = ann.group(1).strip()
                if msg.startswith("Process completed with exit code"):
                    continue
                step_errors.append({
                    "step_name": step_name,
                    "preview": msg[:200],
                    "url": _step_url(run_id, job_id, step_num, line_idx),
                    "line_number": line_idx + 1,
                    "source": "annotation",
                })
                continue

            if _PYTHON_EXCEPTION_RE.search(clean) and line_idx < result_boundary:
                step_errors.append({
                    "step_name": step_name,
                    "preview": clean[:200],
                    "url": _step_url(run_id, job_id, step_num, line_idx),
                    "line_number": line_idx + 1,
                    "source": "exception",
                })

        if not step_errors:
            for line_idx in range(len(lines) - 1, max(0, len(lines) - 5) - 1, -1):
                clean = _TIMESTAMP_RE.sub("", lines[line_idx]).strip()
                if clean and not clean.startswith("##["):
                    step_errors.append({
                        "step_name": step_name,
                        "preview": clean[:200],
                        "url": _step_url(run_id, job_id, step_num, line_idx),
                        "line_number": line_idx + 1,
                        "source": "tail",
                    })
                    break

        errors.extend(step_errors[-max_errors_per_step:])

    return errors


def _step_url(
    run_id: int, job_id: int, step_num: int | None, line_idx: int,
) -> str:
    base = f"https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}"
    if step_num is not None:
        return f"{base}#step:{step_num}:{line_idx + 1}"
    return base


# ---------------------------------------------------------------------------
# Log parsing and pre-filtering
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*")


def parse_log_by_steps(raw_log: str) -> list[dict]:
    """Parse a GitHub Actions job log into per-step segments.

    GitHub Actions logs delimit steps with ``##[group]Step Name`` /
    ``##[endgroup]`` markers, each line prefixed by a UTC timestamp.

    Returns an ordered list of ``{"name": str, "content": str}`` dicts.
    Falls back to a single-entry list wrapping the whole log when no
    markers are detected.
    """
    lines = raw_log.split("\n")
    steps: list[dict] = []
    current_name: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = _TIMESTAMP_RE.sub("", line)

        group_match = re.match(r"##\[group\](.*)", stripped)
        if group_match:
            if current_name is not None or current_lines:
                steps.append({
                    "name": current_name or "(preamble)",
                    "content": "\n".join(current_lines),
                })
            current_name = group_match.group(1).strip()
            current_lines = []
            continue

        if stripped.strip() == "##[endgroup]":
            if current_name is not None:
                steps.append({
                    "name": current_name,
                    "content": "\n".join(current_lines),
                })
                current_name = None
                current_lines = []
            continue

        current_lines.append(line)

    if current_lines:
        steps.append({
            "name": current_name or "(trailing)",
            "content": "\n".join(current_lines),
        })

    if not steps:
        steps = [{"name": "(full log)", "content": raw_log}]

    return steps


def prefilter_large_step_log(
    log_text: str, max_chars: int = STEP_LOG_PREFILTER_THRESHOLD
) -> str:
    """Extract error-relevant sections from a very large step log.

    Keeps the first 100 lines (environment context), last 200 lines
    (exit status), and 30+10 lines of context around every line matching
    ``ERROR_PATTERNS``.  Overlapping ranges are merged.
    """
    if len(log_text) <= max_chars:
        return log_text

    lines = log_text.split("\n")
    total = len(lines)
    keep: set[int] = set()

    HEAD, TAIL = 100, 200
    for i in range(min(HEAD, total)):
        keep.add(i)
    for i in range(max(0, total - TAIL), total):
        keep.add(i)

    CTX_BEFORE, CTX_AFTER = 30, 10
    for i, line in enumerate(lines):
        if ERROR_PATTERNS.search(line):
            for j in range(max(0, i - CTX_BEFORE), min(total, i + CTX_AFTER + 1)):
                keep.add(j)

    sorted_idx = sorted(keep)
    parts: list[str] = []
    prev = -1
    for idx in sorted_idx:
        if prev >= 0 and idx > prev + 1:
            parts.append(f"\n... [{idx - prev - 1} lines omitted] ...\n")
        parts.append(lines[idx])
        prev = idx

    filtered = "\n".join(parts)

    if len(filtered) > max_chars:
        head_size = max_chars // 4
        tail_size = max_chars - head_size - 100
        filtered = (
            filtered[:head_size]
            + "\n\n... [FINAL TRUNCATION — log extremely large] ...\n\n"
            + filtered[-tail_size:]
        )

    return filtered


# ---------------------------------------------------------------------------
# Prompt template loading from CLAUDE.md
# ---------------------------------------------------------------------------

def _find_claude_md() -> Path | None:
    """Locate the CLAUDE.md file (checks multiple candidate paths)."""
    candidates = [
        Path(__file__).resolve().parent.parent / "agent" / "CLAUDE.md",
        Path("/tmp/bot/agent/CLAUDE.md"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


_TEMPLATE_HEADING_RE = re.compile(r"^### ([a-z][a-z0-9-]+)$", re.MULTILINE)


def load_prompt_template(section_name: str) -> str | None:
    """Load a prompt template from CLAUDE.md by section heading.

    Looks for ``### <section_name>`` (kebab-case) under ``## API Mode Prompts``
    and returns the text up to the next kebab-case ``### `` heading or
    section end.  Content headings like ``### Failure Summary`` (Title Case)
    inside the template are preserved.
    """
    path = _find_claude_md()
    if path is None:
        return None

    text = path.read_text()

    api_marker = "## API Mode Prompts"
    api_start = text.find(api_marker)
    if api_start == -1:
        return None

    api_text = text[api_start:]
    next_section = api_text.find("\n---\n\n## ", len(api_marker))
    if next_section != -1:
        api_text = api_text[:next_section]

    headings = list(_TEMPLATE_HEADING_RE.finditer(api_text))
    for i, m in enumerate(headings):
        if m.group(1) == section_name:
            body_start = m.end() + 1
            body_end = headings[i + 1].start() if i + 1 < len(headings) else len(api_text)
            return api_text[body_start:body_end].strip()

    return None


def analyze_job_with_agent(
    job: dict, run_url: str, repo_path: Path,
    workflow_file: str = "", head_sha: str = "",
    event_filter: str = "",
) -> dict:
    """Invoke Claude Code agent to fully analyze a CI failure.

    The agent handles everything autonomously: downloading logs via the
    GitHub API (using ``$GH_PAT`` from the environment), parsing errors,
    reading sglang source code, checking git history, and producing a
    root-cause analysis.  Investigation methodology is defined in
    ``CLAUDE.md`` which Claude Code reads automatically.
    """
    job_name = job["name"]
    job_id = job["id"]

    failed_step_names = {
        s["name"]
        for s in job.get("steps", [])
        if s.get("conclusion") not in ("success", "skipped", None)
    }

    event_line = f"Event filter: {event_filter}\n" if event_filter else ""
    prompt = (
        f"Task: Job Failure Analysis\n"
        f"Job: {job_name}\n"
        f"Run: {run_url}\n"
        f"Job ID: {job_id}\n"
        f"Commit SHA: {head_sha}\n"
        f"Workflow file: {workflow_file}\n"
        f"{event_line}"
        f"Log URL: https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs\n"
        f"Source: current directory\n"
        f"GitHub API token: $GH_PAT"
    )

    _log = logging.getLogger("ci-monitor")
    _log.info("  [%s] Running Claude Code agent...", job_name)
    analysis = claude_code_analyze(
        prompt=prompt,
        work_dir=repo_path,
        max_turns=int(os.environ.get("AGENT_MAX_TURNS", "1000")),
        timeout_secs=int(os.environ.get("AGENT_TIMEOUT_SECS", "1800")),
    )
    _log.info("  [%s] Agent analysis done.", job_name)

    return {
        "run_url": run_url,
        "job_name": job_name,
        "job_id": job_id,
        "head_sha": head_sha,
        "started_at": job.get("started_at"),
        "failed_steps": sorted(failed_step_names),
        "analysis": analysis,
    }


def analyze_job_api(
    client: anthropic.Anthropic,
    token: str,
    job: dict,
    run_url: str,
    head_sha: str = "",
) -> dict:
    """Download logs, extract errors, and run focused API analysis for one job."""
    _log = logging.getLogger("ci-monitor")
    job_name = job["name"]
    job_id = job["id"]
    run_id = int(run_url.rstrip("/").split("/")[-1])

    failed_step_names = {
        s["name"]
        for s in job.get("steps", [])
        if s.get("conclusion") not in ("success", "skipped", None)
    }

    _log.info("  [%s] Downloading logs...", job_name)
    raw_log = download_job_logs(token, job_id)
    _log.info("  [%s] Log: %s chars", job_name, f"{len(raw_log):,}")

    all_errors = extract_error_lines(
        raw_log, job.get("steps", []), run_id, job_id,
    )
    error_lines = [e for e in all_errors if e["source"] != "tail"]
    _log.info("  [%s] Extracted %d error signal(s)", job_name, len(error_lines))

    filtered_log = prefilter_large_step_log(raw_log)
    if len(filtered_log) < len(raw_log):
        _log.info(
            "  [%s] Pre-filtered log: %s -> %s chars",
            job_name, f"{len(raw_log):,}", f"{len(filtered_log):,}",
        )

    _log.info("  [%s] Analyzing...", job_name)
    analysis = focused_job_analysis(
        client, job_name, run_url, error_lines, filtered_log,
    )

    _log.info("  [%s] Done.", job_name)
    return {
        "run_url": run_url,
        "job_name": job_name,
        "job_id": job_id,
        "head_sha": head_sha,
        "started_at": job.get("started_at"),
        "failed_steps": sorted(failed_step_names),
        "analysis": analysis,
    }


def focused_job_analysis(
    client: anthropic.Anthropic,
    job_name: str,
    run_url: str,
    error_lines: list[dict],
    filtered_log: str,
) -> str:
    """Analyze a failed CI job using pre-extracted errors and pre-filtered logs.

    Prompt template is loaded from CLAUDE.md ``### focused-job-analysis``.
    """
    if error_lines:
        errors_section = "\n".join(
            f"- **[{e['source']}]** `{e['step_name']}` line {e['line_number']}: "
            f"`{e['preview']}`"
            for e in error_lines
        )
    else:
        errors_section = "(no errors extracted programmatically — check the log below)"

    template = load_prompt_template("focused-job-analysis")
    if template:
        prompt = template.format(
            job_name=job_name,
            run_url=run_url,
            errors_section=errors_section,
            filtered_log=filtered_log,
        )
    else:
        prompt = (
            f"You are a CI/CD expert analyzing a FAILED CI job in the sglang project "
            f"(LLM serving framework on AMD GPUs).\n\n"
            f"## Job: {job_name}\n## Run: {run_url}\n\n"
            f"## Pre-extracted Error Signals\n{errors_section}\n\n"
            f"## Log (error-relevant sections)\n```\n{filtered_log}\n```\n\n"
            f"Produce a CONCISE failure analysis with: Failure Summary, Failure Reasons, "
            f"Stack Traces, Suggested Fix Directions, Priority."
        )

    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            thinking={
                "type": "adaptive",
            },
            messages=[{"role": "user", "content": prompt}],
        )
        return "\n".join(
            block.text for block in msg.content
            if block.type == "text"
        )
    except Exception as exc:
        print(f"    Extended thinking unavailable ({exc}), using standard call")
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text


def cross_job_analysis(
    client: anthropic.Anthropic | None,
    workflow_name: str,
    job_analyses: list[dict],
    use_agent: bool = True,
    repo_path: Path | None = None,
) -> str:
    """Find common patterns across multiple failed jobs — concise summary.

    When *use_agent* is True, delegates to the Claude Code agent which can
    read sglang source code to verify patterns.  Falls back to API on failure.
    """
    _log = logging.getLogger("ci-monitor")

    jobs_text = "\n\n---\n\n".join(
        f"### Job: {ja['job_name']}\n"
        f"**Job ID:** `{ja.get('job_id', 'unknown')}`\n\n"
        f"{ja['analysis']}"
        for ja in job_analyses
    )

    if use_agent:
        try:
            work_dir = repo_path or SGLANG_REPO_PATH
            if not work_dir.exists():
                raise FileNotFoundError(f"Repo not found at {work_dir}")

            prompt = (
                f"Task: Cross-Job Summary\n"
                f"Workflow: {workflow_name}\n"
                f"Number of failed jobs: {len(job_analyses)}\n"
                f"Per-job analyses: .ci-context/per-job-analyses.md\n"
                f"Source: current directory\n"
                f"GitHub API token: $GH_PAT"
            )
            return claude_code_analyze(
                prompt=prompt,
                work_dir=work_dir,
                context_files={"per-job-analyses.md": jobs_text},
                max_turns=30,
                timeout_secs=300,
            )
        except Exception as exc:
            _log.warning("Agent cross-job analysis failed (%s), falling back to API", exc)

    if client is None:
        client = create_anthropic_client()

    template = load_prompt_template("cross-job-summary")
    if template:
        prompt = template.format(
            num_jobs=len(job_analyses),
            workflow_name=workflow_name,
            jobs_text=jobs_text,
        )
    else:
        prompt = (
            f"You are a CI/CD expert. {len(job_analyses)} jobs failed in workflow "
            f"`{workflow_name}` (sglang project, AMD GPUs).\n\n{jobs_text}\n\n"
            f"Write a SHORT cross-job summary (under 40 lines) with a summary table, "
            f"common root cause, distinct vs shared failures, and fix priority."
        )

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ---------------------------------------------------------------------------
# Claude Code agent integration
# ---------------------------------------------------------------------------

_agent_log = logging.getLogger("claude-agent")

AGENT_WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", "/workspace"))
SGLANG_REPO_PATH = AGENT_WORKSPACE / "sglang"


def claude_code_available() -> bool:
    """Check if the Claude Code CLI is installed and callable."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def create_agent_worktree(tag: str, head_sha: str = "") -> Path:
    """Create an isolated git worktree for a parallel agent.

    Requires the main repo at ``SGLANG_REPO_PATH`` to exist (call
    ``ensure_sglang_repo()`` first).  Each worktree lives at
    ``/workspace/sglang-wt-{tag}`` and is a cheap copy that shares
    the git object store with the main repo.

    If *head_sha* is provided, the worktree is checked out to that
    commit so the agent sees the exact code that was tested in CI.

    Returns the worktree path.  Call ``remove_agent_worktree()`` to
    clean up when done.
    """
    safe_tag = re.sub(r"[^\w\-]", "_", str(tag))
    wt_path = AGENT_WORKSPACE / f"sglang-wt-{safe_tag}"
    branch_name = f"wt-{safe_tag}"

    if wt_path.exists():
        _agent_log.info("Removing stale worktree %s", wt_path)
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_path)],
            cwd=SGLANG_REPO_PATH, capture_output=True, timeout=30,
        )
        if wt_path.exists():
            shutil.rmtree(wt_path)

    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=SGLANG_REPO_PATH, capture_output=True, timeout=10,
    )

    start_point = head_sha if head_sha else "HEAD"
    _agent_log.info("Creating worktree %s (at %s)", wt_path, start_point[:12])
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(wt_path), start_point],
        cwd=SGLANG_REPO_PATH, capture_output=True, timeout=60,
    )
    if result.returncode != 0:
        if head_sha:
            _agent_log.warning(
                "Checkout %s failed, falling back to HEAD", head_sha[:12],
            )
            result = subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, str(wt_path), "HEAD"],
                cwd=SGLANG_REPO_PATH, capture_output=True, timeout=60,
            )
        if result.returncode != 0:
            raise RuntimeError(
                f"git worktree add failed: {result.stderr.decode(errors='replace')}"
            )

    _deploy_claude_md(wt_path)
    _agent_log.info("Worktree ready at %s", wt_path)
    return wt_path


def remove_agent_worktree(wt_path: Path):
    """Remove a worktree created by ``create_agent_worktree()``."""
    if not wt_path.exists():
        return
    _agent_log.info("Removing worktree %s", wt_path)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=SGLANG_REPO_PATH, capture_output=True, timeout=30,
    )
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)


@contextlib.contextmanager
def agent_worktree(tag: str, pr_number: int | None = None):
    """Context manager: create an isolated worktree, optionally checkout a PR, and clean up.

    Usage::

        with agent_worktree("review-pr42", pr_number=42) as wt_path:
            analysis = claude_code_analyze(prompt, work_dir=wt_path)
    """
    ensure_sglang_repo()
    wt_path = create_agent_worktree(tag)
    try:
        if pr_number is not None:
            pr_ref = f"pull/{pr_number}/head"
            subprocess.run(
                ["git", "fetch", "origin", pr_ref, "--depth", "100"],
                cwd=wt_path, capture_output=True, timeout=120,
            )
            subprocess.run(
                ["git", "checkout", "FETCH_HEAD", "--force"],
                cwd=wt_path, capture_output=True, timeout=30,
            )
        yield wt_path
    finally:
        remove_agent_worktree(wt_path)


def ensure_sglang_repo(ref: str = "main") -> Path:
    """Clone or update the sglang repo for agent-based analysis.

    Args:
        ref: Git ref to checkout — ``"main"`` for CI monitor,
             or a PR branch like ``"pull/1234/head"`` for PR review.

    Returns:
        Path to the sglang repo root (``/workspace/sglang``).
    """
    AGENT_WORKSPACE.mkdir(parents=True, exist_ok=True)

    if SGLANG_REPO_PATH.exists() and (SGLANG_REPO_PATH / ".git").exists():
        _agent_log.info("Updating sglang repo at %s (ref: %s)...", SGLANG_REPO_PATH, ref)
        subprocess.run(
            ["git", "fetch", "origin", ref, "--depth", "100"],
            cwd=SGLANG_REPO_PATH, capture_output=True, timeout=120,
        )
        if ref == "main":
            subprocess.run(
                ["git", "reset", "--hard", "origin/main"],
                cwd=SGLANG_REPO_PATH, capture_output=True, timeout=30,
            )
        else:
            subprocess.run(
                ["git", "checkout", "FETCH_HEAD", "--force"],
                cwd=SGLANG_REPO_PATH, capture_output=True, timeout=30,
            )
    else:
        if SGLANG_REPO_PATH.exists():
            shutil.rmtree(SGLANG_REPO_PATH)
        _agent_log.info("Cloning sglang repo to %s...", SGLANG_REPO_PATH)
        clone_url = f"https://github.com/{REPO}.git"
        subprocess.run(
            ["git", "clone", "--depth", "100", "--single-branch", "--branch", "main",
             clone_url, str(SGLANG_REPO_PATH)],
            capture_output=True, timeout=300, check=True,
        )
        if ref != "main":
            subprocess.run(
                ["git", "fetch", "origin", ref, "--depth", "100"],
                cwd=SGLANG_REPO_PATH, capture_output=True, timeout=120,
            )
            subprocess.run(
                ["git", "checkout", "FETCH_HEAD", "--force"],
                cwd=SGLANG_REPO_PATH, capture_output=True, timeout=30,
            )

    _deploy_claude_md()
    _agent_log.info("sglang repo ready at %s", SGLANG_REPO_PATH)
    return SGLANG_REPO_PATH


def _deploy_claude_md(target_dir: Path | None = None):
    """Copy agent/CLAUDE.md to the workspace (or a specific directory).

    Checks multiple possible locations for the bot repo source tree
    (GitHub Actions checkout, daemon clone at /tmp/bot, relative to
    this script).  Always overwrites so the latest version is used.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / "agent" / "CLAUDE.md",
        Path("/tmp/bot/agent/CLAUDE.md"),
    ]
    dest_parent = target_dir.parent if target_dir else AGENT_WORKSPACE
    for src in candidates:
        if src.exists():
            dst = dest_parent / "CLAUDE.md"
            shutil.copy2(src, dst)
            _agent_log.info("Deployed CLAUDE.md to %s", dst)
            return
    _agent_log.warning("agent/CLAUDE.md not found, skipping deployment")


def _recover_text_from_session_log(
    work_dir: Path, must_contain: str | None = None
) -> str | None:
    """Extract the last assistant text from Claude Code session logs.

    When ``claude -p --output-format text`` exits 0 but returns empty
    stdout (e.g. because the final turn contained a tool call alongside
    the text), the actual report may still be in the session ``.jsonl``.

    If *must_contain* is set, only assistant messages whose text contains
    that substring are considered.  Used to salvage the canonical report
    when the agent writes it and then overwrites it with a short follow-up
    reply such as "already delivered above".

    Returns the recovered text, or ``None`` if nothing useful is found.
    """
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None

    escaped_cwd = str(work_dir.resolve()).replace("/", "-")
    project_dir = claude_dir / escaped_cwd
    if not project_dir.exists():
        return None

    jsonl_files = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not jsonl_files:
        return None

    latest = jsonl_files[-1]
    _agent_log.info("Attempting session log recovery from %s", latest)

    last_text = None
    try:
        with open(latest) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message", {})
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if block.get("type") == "text" and block.get("text", "").strip():
                        text = block["text"].strip()
                        if must_contain and must_contain not in text:
                            continue
                        last_text = text
    except Exception as exc:
        _agent_log.warning("Session log recovery failed: %s", exc)
        return None

    if last_text and len(last_text) > 100:
        _agent_log.info("Recovered %d chars from session log", len(last_text))
        return last_text

    return None


def claude_code_analyze(
    prompt: str,
    work_dir: Path,
    context_files: dict[str, str] | None = None,
    max_turns: int = 1000,
    timeout_secs: int = 1800,
    output_must_contain: str | None = None,
) -> str:
    """Run Claude Code CLI in non-interactive print mode.

    Writes *context_files* into a ``.ci-context/`` subdirectory of
    *work_dir* so the agent can read them, runs ``claude -p``, and
    returns the text output.

    If *output_must_contain* is set and the agent's final stdout does
    not contain that substring, scan the session log for the last
    assistant message that does — this catches the failure mode where
    the agent writes the canonical report and then overwrites it with
    a short reply like "already delivered above".

    Raises ``RuntimeError`` on failure so callers can fall back to
    the single-shot API approach.
    """
    context_dir = work_dir / ".ci-context"
    context_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    try:
        if context_files:
            for filename, content in context_files.items():
                p = context_dir / filename
                p.write_text(content)
                written.append(p)

        cmd = [
            "claude", "-p", prompt,
            "--output-format", "text",
            "--max-turns", str(max_turns),
            "--dangerously-skip-permissions",
        ]

        _agent_log.info(
            "Running Claude Code (max_turns=%d, timeout=%ds, cwd=%s)...",
            max_turns, timeout_secs, work_dir,
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(work_dir),
            timeout=timeout_secs,
            stdin=subprocess.DEVNULL,
        )

        _agent_log.info(
            "Claude Code exit=%d, stdout=%d chars, stderr=%d chars",
            result.returncode, len(result.stdout or ""), len(result.stderr or ""),
        )
        if result.returncode == 0 and result.stdout.strip():
            text = result.stdout.strip()
            if output_must_contain and output_must_contain not in text:
                salvaged = _recover_text_from_session_log(
                    work_dir, must_contain=output_must_contain
                )
                if salvaged:
                    _agent_log.info(
                        "Final stdout missing sentinel %r; salvaged %d-char "
                        "earlier message from session log",
                        output_must_contain, len(salvaged),
                    )
                    return salvaged
            return text

        if result.returncode == 0:
            recovered = _recover_text_from_session_log(work_dir)
            if recovered:
                _agent_log.info(
                    "stdout was empty but recovered %d chars from session log",
                    len(recovered),
                )
                return recovered

        stderr_snippet = (result.stderr or "")[:1000]
        stdout_snippet = (result.stdout or "")[:1000]
        raise RuntimeError(
            f"Claude Code exited {result.returncode}.\n"
            f"stderr: {stderr_snippet}\n"
            f"stdout: {stdout_snippet}"
        )

    except subprocess.TimeoutExpired:
        recovered = _recover_text_from_session_log(work_dir)
        if recovered:
            _agent_log.info(
                "Claude Code timed out after %ds; recovered %d chars from session log",
                timeout_secs, len(recovered),
            )
            return recovered + (
                f"\n\n---\n*Note: agent timed out after {timeout_secs}s; "
                f"showing the last assistant response captured before the kill. "
                f"Re-run the bot command to retry.*\n"
            )
        raise RuntimeError(f"Claude Code timed out after {timeout_secs}s")
    except FileNotFoundError:
        raise RuntimeError(
            "Claude Code CLI not found (install: npm install -g @anthropic-ai/claude-code)"
        )
    finally:
        for p in written:
            p.unlink(missing_ok=True)
        try:
            context_dir.rmdir()
        except OSError:
            pass
