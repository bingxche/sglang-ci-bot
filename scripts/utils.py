"""
Shared utilities for amd-bot scripts.

Provides GitHub API helpers, Anthropic client creation, log parsing,
progressive step-by-step CI log analysis, and Claude Code agent integration.
"""

import getpass
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

CLAUDE_MODEL = "claude-opus-4-6"

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
# Progressive step-by-step analysis with Claude (used by monitor_ci)
# ---------------------------------------------------------------------------

def progressive_step_analysis(
    client: anthropic.Anthropic,
    job_name: str,
    steps_with_logs: list[dict],
    failed_step_names: set[str],
) -> str:
    """Analyze every step of a job progressively, accumulating shared context.

    Simulates how a human engineer reads CI logs: step by step, in order,
    building up understanding of the environment, dependencies, and build
    state before reaching the failed step.  Each step is summarized with
    the accumulated summary of all prior steps as context.
    """
    accumulated = ""
    n = len(steps_with_logs)

    for i, step in enumerate(steps_with_logs):
        name = step["name"]
        log = step["content"]
        is_failed = name in failed_step_names
        label = "FAILED" if is_failed else "PASSED"

        if len(log) > STEP_LOG_PREFILTER_THRESHOLD:
            orig = len(log)
            log = prefilter_large_step_log(log)
            print(
                f"      Pre-filtered '{name}': {orig:,} -> {len(log):,} chars"
            )

        print(f"    [{i+1}/{n}] Summarizing: {name} ({label}, {len(log):,} chars)")

        prompt = f"""You are analyzing step {i+1} of {n} in CI job "{job_name}" for the sglang project (LLM serving framework on AMD GPUs).

## Context from previous steps
{accumulated if accumulated else "(first step — no prior context)"}

## Current Step: {name}
**Status**: {label}

```
{log}
```

{"This step FAILED. Extract: (1) the full error message and stack trace verbatim, (2) which test(s) failed with pass/fail counts, (3) exit code. Be thorough on errors but skip non-error details." if is_failed else "This step passed. In 2-4 bullet points, note ONLY information that could be relevant to understanding a later failure (e.g. key package versions, dependency conflicts/warnings, GPU/Docker info). Skip routine output."}"""

        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024 if not is_failed else 2048,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = msg.content[0].text
        accumulated += f"\n### Step {i+1}: {name} [{label}]\n{summary}\n"

    return accumulated


def final_job_analysis(
    client: anthropic.Anthropic,
    job_name: str,
    run_url: str,
    accumulated_summary: str,
) -> str:
    """Produce a concise, results-first analysis for a failed job."""
    prompt = f"""You are a CI/CD expert analyzing a FAILED CI job in the sglang project (LLM serving framework on AMD GPUs).

## Job: {job_name}
## Run: {run_url}

{accumulated_summary}

Produce a CONCISE report in the following format. Be brief — engineers will read this quickly and then go look at the logs themselves.

### Failure Summary
One or two sentences: what failed and why.

### Failure Reasons
List ALL distinct failure reasons as bullet points. Do NOT omit any. Each bullet should be one concise sentence.

### Stack Traces
Include the key error messages and stack traces verbatim (in code blocks). Engineers need these to locate the issue. Only include the relevant portions — not the entire log.

### Suggested Fix Directions
List potential fix directions as bullet points. Only state the DIRECTION (e.g. "pin transformers to <5.0.0", "add default value to vision_config field"). Do NOT write out code implementations or detailed steps.

### Priority
One word: Critical / High / Medium / Low — with a single sentence justification.

IMPORTANT RULES:
- Do NOT include environment tables, version tables, or lengthy context sections.
- Do NOT write code examples for fixes.
- Do NOT include "Environment Context" sections.
- Keep the entire output under 300 lines of markdown.
- Be direct and factual — no filler."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def focused_job_analysis(
    client: anthropic.Anthropic,
    job_name: str,
    run_url: str,
    error_lines: list[dict],
    filtered_log: str,
) -> str:
    """Analyze a failed CI job using pre-extracted errors and pre-filtered logs.

    Replaces the progressive_step_analysis + final_job_analysis pipeline
    with a single LLM call.  Pre-extracted error messages are placed at
    the top of the prompt so the model starts from actual errors rather
    than accumulating noise from passing steps.
    """
    if error_lines:
        errors_section = "\n".join(
            f"- **[{e['source']}]** `{e['step_name']}` line {e['line_number']}: "
            f"`{e['preview']}`"
            for e in error_lines
        )
    else:
        errors_section = "(no errors extracted programmatically — check the log below)"

    prompt = f"""You are a CI/CD expert analyzing a FAILED CI job in the sglang project (LLM serving framework on AMD GPUs).

## Job: {job_name}
## Run: {run_url}

## Pre-extracted Error Signals
These errors were programmatically extracted from the log. Start your analysis from these:

{errors_section}

## Log (error-relevant sections)
```
{filtered_log}
```

Produce a CONCISE report. Be brief — engineers will read this quickly then check the logs themselves.

### Failure Summary
One or two sentences: what failed and why.

### Failure Reasons
List ALL distinct failure reasons as bullet points. Each bullet: one concise sentence.

### Stack Traces
Include key error messages and stack traces verbatim (in code blocks). Only the relevant portions.

### Suggested Fix Directions
Bullet points with fix DIRECTIONS only (e.g. "pin transformers to <5.0.0"). No code.

### Priority
Critical / High / Medium / Low — with one sentence justification.

IMPORTANT:
- Focus on actual error messages and stack traces, not warnings from passing steps.
- Do NOT include environment tables or version lists.
- Do NOT write code examples.
- Keep output under 300 lines.
- Be direct and factual."""

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
    client: anthropic.Anthropic,
    workflow_name: str,
    job_analyses: list[dict],
) -> str:
    """Find common patterns across multiple failed jobs — concise summary."""
    jobs_text = "\n\n---\n\n".join(
        f"### Job: {ja['job_name']}\n{ja['analysis']}" for ja in job_analyses
    )

    prompt = f"""You are a CI/CD expert. {len(job_analyses)} jobs failed in workflow `{workflow_name}` (sglang project, AMD GPUs).

{jobs_text}

Write a SHORT cross-job summary (under 40 lines). Start with a summary table, then brief analysis.

1. **Summary Table** (MUST be first): a markdown table with these columns:
   | # | Job | Root Cause | Type | Priority |
   Type examples: Threshold too tight, Infra flake, Server crash, Build error, Timeout, Flaky test.
   Priority: Critical / High / Medium / Low.

2. **Common Root Cause** (if any): one or two sentences.
3. **Distinct vs Shared Failures**: which jobs share the same root cause, and which have unique issues.
4. **Fix Priority**: one sentence on what to fix first and why.

Do NOT repeat per-job analysis. Do NOT write code. Be brief."""

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


def _deploy_claude_md():
    """Copy agent/CLAUDE.md from the bot repo to /workspace/CLAUDE.md.

    Checks multiple possible locations for the bot repo source tree
    (GitHub Actions checkout, daemon clone at /tmp/bot, relative to
    this script).  Always overwrites so the latest version is used.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / "agent" / "CLAUDE.md",
        Path("/tmp/bot/agent/CLAUDE.md"),
    ]
    for src in candidates:
        if src.exists():
            dst = AGENT_WORKSPACE / "CLAUDE.md"
            shutil.copy2(src, dst)
            _agent_log.info("Deployed CLAUDE.md to %s", dst)
            return
    _agent_log.warning("agent/CLAUDE.md not found, skipping deployment")


def claude_code_analyze(
    prompt: str,
    work_dir: Path,
    context_files: dict[str, str] | None = None,
    max_turns: int = 1000,
    timeout_secs: int = 600,
) -> str:
    """Run Claude Code CLI in non-interactive print mode.

    Writes *context_files* into a ``.ci-context/`` subdirectory of
    *work_dir* so the agent can read them, runs ``claude -p``, and
    returns the text output.

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
            return result.stdout.strip()

        stderr_snippet = (result.stderr or "")[:1000]
        stdout_snippet = (result.stdout or "")[:1000]
        raise RuntimeError(
            f"Claude Code exited {result.returncode}.\n"
            f"stderr: {stderr_snippet}\n"
            f"stdout: {stdout_snippet}"
        )

    except subprocess.TimeoutExpired:
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
