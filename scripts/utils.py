"""
Shared utilities for amd-bot scripts.

Provides GitHub API helpers, Anthropic client creation, log parsing,
and progressive step-by-step CI log analysis.
"""

import getpass
import os
import re

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
    max_errors_per_step: int = 5,
) -> list[dict]:
    """Extract error lines from a job log with deep-link URLs.

    Maps parsed log steps to the GitHub API step numbers, scans for
    ``ERROR_PATTERNS``, and builds URLs of the form::

        https://github.com/{REPO}/actions/runs/{run_id}/job/{job_id}#step:{N}:{L}

    Returns a list of dicts with keys: step_name, preview, url, line_number.
    """
    parsed_steps = parse_log_by_steps(raw_log)

    step_num_map: dict[str, int] = {}
    for s in job_steps_api:
        step_num_map[s["name"]] = s["number"]

    errors: list[dict] = []
    for parsed_step in parsed_steps:
        step_name = parsed_step["name"]
        step_num = step_num_map.get(step_name)
        lines = parsed_step["content"].split("\n")

        step_error_count = 0
        for line_idx, line in enumerate(lines):
            if ERROR_PATTERNS.search(line):
                clean = _TIMESTAMP_RE.sub("", line).strip()
                preview = clean[:200]

                if step_num is not None:
                    url = (
                        f"https://github.com/{REPO}/actions/runs/"
                        f"{run_id}/job/{job_id}#step:{step_num}:{line_idx + 1}"
                    )
                else:
                    url = (
                        f"https://github.com/{REPO}/actions/runs/"
                        f"{run_id}/job/{job_id}"
                    )

                errors.append({
                    "step_name": step_name,
                    "preview": preview,
                    "url": url,
                    "line_number": line_idx + 1,
                })
                step_error_count += 1
                if step_error_count >= max_errors_per_step:
                    break

    return errors


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
# Progressive step-by-step analysis with Claude
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

Write a SHORT cross-job summary (under 30 lines). Include:
1. **Common Root Cause** (if any): one or two sentences.
2. **Distinct vs Shared Failures**: which jobs share the same root cause, and which have unique issues.
3. **Fix Priority**: one sentence on what to fix first and why.

Do NOT repeat per-job analysis. Do NOT write code. Be brief."""

    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text
