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
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
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

CLAUDE_MODEL = "claude-opus-5-5"

STEP_LOG_PREFILTER_THRESHOLD = 150_000

MI355X_DISAGG_WORKFLOW = "nightly-amd-mi355x-disagg.yml"
MI355X_ARTIFACT_PREFIX = "mi355x-"
MI355X_ARTIFACT_CONTEXT_MAX_CHARS = 120_000

# Miles (radixark/miles) RL-training workflows. Their runs are on sglang main,
# but what they test is /root/miles inside a dated image that the build
# workflows assemble from radixark/miles and an sglang branch (sglang-miles),
# so the run's own commit says nothing about the code that failed.
MILES_REPO = "radixark/miles"
MILES_WORKFLOWS = (
    "nightly-test-amd-miles-rocm10.yml",
    "nightly-test-amd-miles-rocm10-mi30x.yml",
    "release-docker-amd-miles-rocm10-nightly.yml",
    "release-docker-amd-miles-rocm724-nightly.yml",
)
# The sglang-ci dashboard's Miles Cause & Fix PRs payload: for every failing
# job, the image's commits, the failing tests and traceback, when each test
# last passed, the commit ranges in between and the commits in them that
# touch the traceback. It is parsed from logs, not judged, so it is handed to
# the agent as evidence to verify.
MILES_CAUSE_FIX_URL = os.environ.get(
    "MILES_CAUSE_FIX_URL",
    "https://rocm.github.io/sglang-ci/api/miles/cause-fix.json",
)

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


RUNNER_LOST_ANNOTATION = "lost communication with the server"
_STEP_FAILED = ("failure", "cancelled", "timed_out")


def is_runner_lost(job: dict) -> bool:
    """True when a failed job's runner went away mid-job rather than a step failing.

    GitHub reports this as "The self-hosted runner lost communication with the
    server" -- a check-run annotation, which the jobs endpoint does not carry --
    and usually keeps no log. The step list gives it away: the job is
    ``failure`` but no step is, because the step that was running is left
    ``in_progress``, the list stops short, or (most often) it is empty. A real
    failure always fails a step, and ``timeout-minutes`` cancels one.

    Checked against the annotation on 1,384 failed jobs of the four AMD
    workflows: it matched all 22 annotated jobs and one more from the same
    outage that GitHub never annotated. The same rule drives the "Runner lost"
    column on the ROCm/sglang-ci Runner Health page.
    """
    if job.get("status") != "completed" or job.get("conclusion") != "failure":
        return False
    # A job that never got a runner (startup failure) has nothing to lose.
    if not job.get("runner_name"):
        return False
    return not any(s.get("conclusion") in _STEP_FAILED for s in job.get("steps") or [])


def get_job_annotations(token: str, job_id: int) -> list[dict] | None:
    """The check-run annotations of a job, or None when they could not be read."""
    url = f"https://api.github.com/repos/{REPO}/check-runs/{job_id}/annotations"
    try:
        resp = requests.get(url, headers=gh_headers(token), timeout=30)
    except requests.RequestException:
        return None
    return resp.json() if resp.status_code == 200 else None


def _lost_at_step(job: dict) -> str:
    steps = job.get("steps") or []
    running = next((s for s in steps if s.get("status") == "in_progress"), None)
    if running:
        return f"`{running['name']}` (step {running['number']}, left in progress)"
    if not steps:
        return "before any step was reported (the job has no steps)"
    last = steps[-1]
    return f"after `{last['name']}` (step {last['number']}); the step list stops there"


def runner_lost_evidence(
    token: str | None, job: dict, run_jobs: list[dict] | None = None,
) -> str:
    """Facts about a lost-runner job, for the analysis prompt and evidence packs.

    *run_jobs* are the other jobs of the same run; the lost ones among them
    tell a host outage (several jobs, one runner, one window) from a job that
    takes its own runner down.
    """
    lines = [
        f"- Runner: `{job.get('runner_name')}` (labels: "
        + ", ".join(f"`{x}`" for x in job.get("labels") or []) + ")",
        f"- Started {job.get('started_at')}, marked failed {job.get('completed_at')}",
        f"- Lost during: {_lost_at_step(job)}",
    ]
    anns = get_job_annotations(token, job["id"]) if token else None
    msg = next((a.get("message", "") for a in anns or []
                if RUNNER_LOST_ANNOTATION in a.get("message", "")), None)
    if msg:
        lines.append(f"- GitHub annotation: \"{msg.splitlines()[0]}\"")
    elif anns is not None:
        lines.append("- GitHub annotation: none (GitHub does not always annotate a lost runner)")
    others = [j for j in run_jobs or [] if j.get("id") != job["id"] and is_runner_lost(j)]
    if others:
        others.sort(key=lambda j: j.get("completed_at") or "")
        lines.append(f"- Other jobs in this run that lost their runner: {len(others)}")
        lines += [
            f"  - `{j.get('runner_name')}` · {j.get('completed_at')} · "
            f"[{j['name']}]({j.get('html_url')})"
            for j in others
        ]
    else:
        lines.append("- Other jobs in this run that lost their runner: none")
    return "\n".join(lines)


def get_failed_jobs(token: str, run_id: int) -> list[dict]:
    """Get completed non-success, non-skipped jobs for a workflow run.

    Only returns jobs whose status is 'completed' so that still-running
    jobs in an in-progress workflow are not picked up prematurely.
    """
    jobs = get_run_jobs(token, run_id)
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
        # The log response has no charset, so requests would decode it as
        # Latin-1 and garble markers such as "✗ FAILED".
        resp.encoding = "utf-8"
        return resp.text
    return f"[Failed to fetch logs: HTTP {resp.status_code}]"


def _run_id_from_url(run_url: str) -> int | None:
    m = re.search(r"/actions/runs/(\d+)", run_url)
    return int(m.group(1)) if m else None


def _is_mi355x_disagg_workflow(workflow_file: str) -> bool:
    return workflow_file.split("/")[-1] == MI355X_DISAGG_WORKFLOW


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
    return cleaned or "artifact"


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _extract_zip_safely(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Extract a GitHub artifact zip, skipping unsafe paths."""
    extracted: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = dest_dir / member.filename
            if not _path_within(target, dest_dir):
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    return extracted


def _extract_tar_safely(tar_path: Path, dest_dir: Path) -> list[Path]:
    """Extract a log tarball, skipping symlinks and unsafe paths."""
    extracted: list[Path] = []
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            target = dest_dir / member.name
            if not _path_within(target, dest_dir):
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            src = tf.extractfile(member)
            if src is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    return extracted


def _list_run_artifacts(token: str, run_id: int) -> list[dict]:
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/artifacts"
    artifacts: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=gh_headers(token),
            params={"per_page": 100, "page": page},
        )
        resp.raise_for_status()
        items = resp.json().get("artifacts", [])
        if not items:
            break
        artifacts.extend(items)
        if len(items) < 100:
            break
        page += 1
    return artifacts


def _download_artifact_archive(token: str, artifact: dict, target: Path) -> None:
    url = artifact["archive_download_url"]
    with requests.get(
        url,
        headers=gh_headers(token),
        allow_redirects=True,
        stream=True,
        timeout=120,
    ) as resp:
        resp.raise_for_status()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def prepare_mi355x_artifact_context(
    token: str | None,
    run_id: int,
    work_dir: Path,
) -> Path | None:
    """Download and unpack MI355X disagg workflow artifacts for an agent.

    The nightly MI355X disagg workflow uploads the useful server-side evidence
    as run artifacts, not only as GitHub job logs.  This helper materializes
    those artifacts under ``.ci-context/mi355x-artifacts`` and writes a manifest
    that the agent can read before diagnosing the failure.
    """
    _log = logging.getLogger("ci-monitor")
    if not token:
        _log.warning("No GitHub token available; cannot pre-download MI355X artifacts")
        return None

    context_dir = work_dir / ".ci-context"
    artifact_root = context_dir / "mi355x-artifacts"
    if artifact_root.exists():
        shutil.rmtree(artifact_root)
    artifact_root.mkdir(parents=True, exist_ok=True)

    try:
        run_artifacts = _list_run_artifacts(token, run_id)
    except Exception as exc:
        _log.warning("Failed to list MI355X artifacts for run %s: %s", run_id, exc)
        shutil.rmtree(artifact_root, ignore_errors=True)
        return None

    artifacts = [
        a for a in run_artifacts
        if not a.get("expired") and a.get("name", "").startswith(MI355X_ARTIFACT_PREFIX)
    ]
    if not artifacts:
        _log.warning("No MI355X artifacts found for run %s", run_id)
        shutil.rmtree(artifact_root, ignore_errors=True)
        return None

    manifest: list[str] = [
        "# MI355X Disagg Artifact Context",
        "",
        f"Workflow run: https://github.com/{REPO}/actions/runs/{run_id}",
        "",
        "These files were downloaded from run artifacts before analysis. "
        "For this workflow, prefill/decode/router evidence usually lives here.",
        "",
        "## Downloaded Artifacts",
        "",
    ]
    important_files: list[Path] = []

    for artifact in artifacts:
        artifact_name = artifact.get("name", "unnamed")
        artifact_id = artifact.get("id", "unknown")
        artifact_dir = artifact_root / f"{_safe_name(artifact_name)}-{artifact_id}"
        archive_dir = artifact_dir / "archive"
        logs_root = artifact_dir / "logs"
        zip_path = artifact_dir / "artifact.zip"

        manifest.append(
            f"- `{artifact_name}` (id `{artifact_id}`, "
            f"{artifact.get('size_in_bytes', 0):,} bytes)"
        )

        try:
            _download_artifact_archive(token, artifact, zip_path)
            extracted = _extract_zip_safely(zip_path, archive_dir)
            zip_path.unlink(missing_ok=True)
        except Exception as exc:
            manifest.append(f"  - download/extract failed: `{exc}`")
            _log.warning("Failed to download artifact %s: %s", artifact_name, exc)
            continue

        rel_archive = archive_dir.relative_to(work_dir)
        manifest.append(f"  - archive contents: `{rel_archive}`")
        for p in sorted(extracted):
            rel = p.relative_to(work_dir)
            manifest.append(f"    - `{rel}`")

        tarballs = sorted(archive_dir.rglob("*_logs.tar.gz"))
        for tarball in tarballs:
            bundle_name = tarball.name.removesuffix(".tar.gz")
            bundle_dir = logs_root / _safe_name(bundle_name)
            try:
                extracted_logs = _extract_tar_safely(tarball, bundle_dir)
            except Exception as exc:
                manifest.append(f"  - `{tarball.relative_to(work_dir)}` extract failed: `{exc}`")
                _log.warning("Failed to extract log bundle %s: %s", tarball, exc)
                continue

            manifest.append(f"  - extracted log bundle: `{bundle_dir.relative_to(work_dir)}`")
            important_files.extend(extracted_logs)

    if important_files:
        manifest.extend(["", "## Important Extracted Files", ""])
        for p in sorted(important_files)[:300]:
            manifest.append(f"- `{p.relative_to(work_dir)}`")

    manifest_path = artifact_root / "MANIFEST.md"
    manifest_path.write_text("\n".join(manifest) + "\n")
    _log.info(
        "Prepared MI355X artifact context for run %s: %d artifact(s), %d extracted file(s)",
        run_id, len(artifacts), len(important_files),
    )
    return artifact_root


# ---------------------------------------------------------------------------
# Miles context
# ---------------------------------------------------------------------------

def is_miles_workflow(workflow_file: str) -> bool:
    return workflow_file.split("/")[-1] in MILES_WORKFLOWS


_miles_payload: dict = {}
_miles_payload_lock = threading.Lock()
_MILES_PAYLOAD_TTL_SECS = 600


def fetch_miles_cause_fix(url: str = MILES_CAUSE_FIX_URL) -> dict | None:
    """The dashboard's Miles Cause & Fix payload, or None if unavailable.

    Fetched once per run of the monitor (the jobs of one workflow are
    analysed in parallel, and the payload is a few hundred KB).
    """
    _log = logging.getLogger("ci-monitor")
    with _miles_payload_lock:
        cached = _miles_payload.get(url)
        if cached and time.monotonic() - cached[0] < _MILES_PAYLOAD_TTL_SECS:
            return cached[1]
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            _log.warning("Miles cause-fix payload unavailable (%s): %s", url, exc)
            data = None
        if not isinstance(data, dict) or not data.get("available"):
            data = None
        _miles_payload[url] = (time.monotonic(), data)
        return data


def miles_rows_for_job(payload: dict | None, job_id: int) -> list[dict]:
    """The payload's rows (failures) that include ``job_id``, failing first."""
    rows = [
        row for row in (payload or {}).get("rows", [])
        if any(j.get("id") == job_id for j in row.get("jobs", []))
    ]
    return sorted(rows, key=lambda r: r.get("state") != "failing")


def miles_job_image(rows: list[dict], job_id: int) -> dict:
    """``{"image", "commits", "refs", "forks"}`` of the image ``job_id`` ran on.

    A manual build can take miles from a fork (``<owner>/miles``). The
    dashboard files that commit under radixark/miles and names the fork in
    ``forks``; payloads from before it did key the commit by the fork, which
    is mapped back here so the checkout still finds it.
    """
    for row in rows:
        for job in row.get("jobs", []):
            if job.get("id") != job_id:
                continue
            commits = dict(job.get("commits") or {})
            refs = dict(job.get("refs") or {})
            forks = dict(job.get("forks") or {})
            if MILES_REPO not in commits:
                fork = next((k for k in commits if k.lower().endswith("/miles")), "")
                if fork:
                    commits[MILES_REPO] = commits.pop(fork)
                    if fork in refs:
                        refs[MILES_REPO] = refs.pop(fork)
                    forks[MILES_REPO] = fork
            return {
                "image": job.get("image") or "",
                "commits": commits,
                "refs": refs,
                "forks": forks,
            }
    return {"image": "", "commits": {}, "refs": {}, "forks": {}}


def _md_link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def _miles_pr_line(pr: dict) -> str:
    repo = pr.get("repo", "")
    sha = (pr.get("sha") or "")[:10]
    commit = _md_link(f"`{sha}`", pr.get("commit_url", "")) if sha else ""
    label = (
        _md_link(f"{repo}#{pr['number']}", pr.get("url", ""))
        if pr.get("number") else repo
    )
    kind = "strong" if pr.get("strong") else "weak"
    state = f", {pr['pr_state']}" if pr.get("pr_state") else ""
    title = (pr.get("title") or "").replace("\n", " ")[:120]
    ref = " ".join(p for p in (label, commit) if p)
    return f"- {ref} — {title} ({kind}{state}): {pr.get('reason', '')}"


def _miles_point(point: dict | None) -> str:
    if not point:
        return "none found"
    run = _md_link(point.get("date", "?"), point.get("job_url") or point.get("run_url", ""))
    image = f" on `{point['image']}`" if point.get("image") else ""
    return f"{run}{image}"


def render_miles_evidence(
    job: dict, rows: list[dict], image: dict, checkout: dict, payload: dict | None,
    docker: list[str] | None = None,
) -> str:
    """The evidence file a Miles job's agent reads first. ``docker`` is what
    Docker Hub said about the image (``check_miles_image``)."""
    lines = [
        "# Miles failure evidence",
        "",
        f"Job: {_md_link(job.get('name', ''), job.get('html_url', ''))} (ID `{job.get('id')}`)",
        "",
        "Collected by the sglang-ci dashboard's Miles Cause & Fix PRs card "
        f"({MILES_CAUSE_FIX_URL}), which parses job logs and image build logs. "
        "Nothing below is a verdict: the candidate commits only touch the "
        "failing test or its traceback inside the regression window. Verify "
        "each against its diff before citing it.",
        "",
        "## Code under test",
        "",
    ]
    commits, refs = image.get("commits") or {}, image.get("refs") or {}
    forks = image.get("forks") or {}
    lines.append(f"- Image: `{image['image']}`" if image.get("image") else "- Image: unknown")
    for repo, where in ((MILES_REPO, checkout.get("miles_src")), (REPO, checkout.get("sglang_src"))):
        sha = commits.get(repo)
        if not sha:
            lines.append(f"- {repo}: unknown (the dashboard has no {repo} commit for this image)")
            continue
        fork = forks.get(repo)
        ref = f"branch `{refs[repo]}`" if refs.get(repo) else ""
        ref = f" ({f'fork `{fork}`, ' if fork else ''}{ref})" if ref or fork else ""
        url = f"https://github.com/{repo}/commit/{sha}"
        place = f" — checked out at `{where}`" if where else " — NOT checked out"
        lines.append(f"- {repo}: [`{sha[:12]}`]({url}){ref}{place}")
        if fork:
            lines.append(
                f"  - **Built from the fork `{fork}`, not from {repo} main** (a manual "
                "build replaced that day's nightly image). The failure may come from "
                "the fork's own commits; it is not a regression in "
                f"{repo} until it reproduces on an image built from main."
            )
    if docker:
        lines += ["", "## Docker Hub", "", *docker]
    if payload is None:
        lines += [
            "",
            "The dashboard payload could not be fetched, so there is no evidence "
            "beyond the job itself. Follow the Miles workflow fallback in your task instructions.",
        ]
    elif not rows:
        lines += [
            "",
            "The dashboard has no row for this job yet (its log may still be "
            "queued there). Follow the Miles workflow fallback in your task instructions.",
        ]

    for n, row in enumerate(rows, 1):
        lines += [
            "",
            f"## Failure {n}: {row.get('kind', 'test')} — state `{row.get('state', '')}`",
            "",
            f"- Error: `{(row.get('error') or '').strip()[:400]}`",
        ]
        for t in row.get("tests", []):
            code = f", exit code {t['exit_code']}" if t.get("exit_code") is not None else ""
            lines.append(f"- Test: `{t['file']}` — {t.get('status', '')}{code}: `{(t.get('error') or '')[:300]}`")
        if row.get("frames"):
            lines.append("- Traceback frames in miles / sglang (innermost last):")
            for f in row["frames"]:
                code = f" — `{f['code']}`" if f.get("code") else ""
                lines.append(f"  - `{f['repo']}:{f['path']}:{f['line']}` in `{f['func']}`{code}")
        for s in row.get("since", []):
            lines.append(
                f"- {s.get('label', '')}: failing since {_miles_point(s.get('first_failed'))} "
                f"({s.get('failed_runs', '?')} failed run(s)); last passed {_miles_point(s.get('last_passed'))}"
            )
            if s.get("lower_bound"):
                lines.append("  - lower bound: no passing run within the dashboard's history window")
            if s.get("same_image") or s.get("image_unchanged"):
                lines.append(
                    "  - the last pass ran on the same code (same image / same build "
                    "commits): this is not a code change — flaky, runner or data"
                )
            if s.get("window_unknown"):
                lines.append("  - commit range unknown: a build log of one side was not readable")
            for w in s.get("windows", []):
                span = _md_link(f"{w['base'][:10]}...{w['head'][:10]}", w.get("url", ""))
                built = "; ".join(
                    f"{side} built from fork `{w[key]}`"
                    for side, key in (("last pass", "base_fork"), ("failure", "head_fork"))
                    if w.get(key)
                )
                lines.append(
                    f"  - {w['repo']}: {span} — {w.get('commits', '?')} commit(s) "
                    f"(branch `{w.get('ref', '')}`"
                    + (f"; {built}, so the range includes the fork's own commits" if built else "")
                    + ")"
                )
        causes, fixes = row.get("cause_prs") or [], row.get("fix_prs") or []
        lines.append("- Candidate causes (in the window, touch the test or traceback):")
        lines += [f"  {_miles_pr_line(p)}" for p in causes] or ["  - none"]
        lines.append("- Candidate fixes (later commits on the image's branch, or open PRs naming the test):")
        lines += [f"  {_miles_pr_line(p)}" for p in fixes] or ["  - none"]
    return "\n".join(lines) + "\n"


# Docker Hub, asked rather than guessed: whether the image a Miles job ran on
# is still the one under its tag, and whether that day's image was published
# at all (a nightly falls back to an older tag when the day's build failed).
# The tag API needs no auth; it allows ~180 calls a window, and a job costs 3.
MILES_IMAGE_REPO = "rocm/sgl-dev"
DOCKERHUB_API = "https://hub.docker.com/v2"
MILES_NIGHTLY_TRACKS = {
    "nightly-test-amd-miles-rocm10.yml": "miles-rocm10-mi35x",
    "nightly-test-amd-miles-rocm10-mi30x.yml": "miles-rocm10-mi30x",
}
_BUILD_LEG_RE = re.compile(r"\((rocm\w+-mi\d+x)\b")
_DATED_TAG_RE = re.compile(r"^(.+)-(\d{8})$")
_DOCKERHUB_RECENT = 5
_dockerhub_cache: dict = {}
_dockerhub_lock = threading.Lock()


def _dockerhub_get(path: str, params: dict | None = None) -> tuple[int, dict]:
    """``(status, json)`` for a Docker Hub API path; ``(0, {})`` if unreachable.
    Answers are kept for the run: three jobs on one image ask once."""
    key = (path, tuple(sorted((params or {}).items())))
    with _dockerhub_lock:
        if key in _dockerhub_cache:
            return _dockerhub_cache[key]
    try:
        resp = requests.get(f"{DOCKERHUB_API}{path}", params=params, timeout=30)
        out = (resp.status_code, resp.json() if resp.status_code == 200 else {})
    except (requests.RequestException, ValueError):
        return 0, {}
    with _dockerhub_lock:
        _dockerhub_cache[key] = out
    return out


def dockerhub_tag(repository: str, tag: str) -> dict | None:
    """``{"exists", "pushed", "digest"}`` for one tag, or None if Docker Hub
    could not be asked (which is not the same as the tag being missing)."""
    ns, repo = repository.split("/", 1)
    status, data = _dockerhub_get(f"/namespaces/{ns}/repositories/{repo}/tags/{tag}")
    if status == 404:
        return {"exists": False, "pushed": "", "digest": ""}
    if status != 200:
        return None
    return {
        "exists": True,
        "pushed": data.get("tag_last_pushed") or data.get("last_updated") or "",
        "digest": data.get("digest") or "",
    }


def dockerhub_recent_tags(repository: str, prefix: str, n: int = _DOCKERHUB_RECENT) -> list[dict] | None:
    """The newest dated tags of one image track (``<prefix>-YYYYMMDD``)."""
    ns, repo = repository.split("/", 1)
    status, data = _dockerhub_get(
        f"/namespaces/{ns}/repositories/{repo}/tags",
        {"name": prefix, "page_size": 25, "ordering": "last_updated"},
    )
    if status != 200:
        return None
    out = []
    for t in data.get("results") or []:
        m = _DATED_TAG_RE.match(t.get("name", ""))
        if m and m.group(1) == prefix:
            out.append({
                "tag": t["name"],
                "pushed": t.get("tag_last_pushed") or t.get("last_updated") or "",
                "digest": t.get("digest") or "",
            })
    return out[:n]


def miles_image_track(workflow_file: str, job: dict, image: str) -> str:
    """The job's image track, e.g. ``miles-rocm10-mi35x``: from the image it
    ran on, else its workflow (nightlies) or job name (``publish (rocm724-mi35x)``)."""
    m = _DATED_TAG_RE.match(image.split(":", 1)[-1]) if image else None
    if m:
        return m.group(1)
    track = MILES_NIGHTLY_TRACKS.get(workflow_file.split("/")[-1])
    if track:
        return track
    m = _BUILD_LEG_RE.search(job.get("name", ""))
    return f"miles-{m.group(1)}" if m else ""


def _hub_time(ts: str) -> str:
    return ts[:16].replace("T", " ") + " UTC" if ts else "?"


def check_miles_image(workflow_file: str, job: dict, image: str) -> list[str]:
    """Evidence lines on what Docker Hub has for this job's image track."""
    track = miles_image_track(workflow_file, job, image)
    if not track:
        return ["- Could not tell this job's image track; Docker Hub was not asked."]
    repo = MILES_IMAGE_REPO
    started = job.get("started_at") or ""
    day = started[:10].replace("-", "")
    building = workflow_file.split("/")[-1].startswith("release-docker-")
    lines = []

    ran_tag = image.split(":", 1)[-1] if image else ""
    if ran_tag:
        ran = dockerhub_tag(repo, ran_tag)
        if ran is None:
            lines.append(f"- `{repo}:{ran_tag}` (the image this job ran): Docker Hub did not answer.")
        elif not ran["exists"]:
            lines.append(f"- `{repo}:{ran_tag}` (the image this job ran): **no longer on Docker Hub**.")
        else:
            lines.append(
                f"- `{repo}:{ran_tag}` (the image this job ran): pushed {_hub_time(ran['pushed'])}, "
                f"digest `{ran['digest'][:19]}`."
            )
            if started and ran["pushed"] > started:
                lines.append(
                    "  - **Re-pushed after this job started**: the tag now names a different "
                    "image than the job pulled. Compare the `Digest: sha256:...` the job's log "
                    "printed before reading anything about this image off Docker Hub."
                )

    if day and (not ran_tag or not ran_tag.endswith(day)):
        want = f"{track}-{day}"
        found = dockerhub_tag(repo, want)
        if found is None:
            lines.append(f"- `{repo}:{want}` (this run's day): Docker Hub did not answer.")
        elif found["exists"]:
            lines.append(f"- `{repo}:{want}` (this run's day): pushed {_hub_time(found['pushed'])}.")
            if ran_tag and not building:
                lines.append(
                    f"  - The job ran on `{ran_tag}` although that day's image exists: it was "
                    "pushed after the job pulled, or the workflow pinned an older tag."
                )
        elif building:
            lines.append(f"- `{repo}:{want}` (this build's image): **not on Docker Hub** — the build did not publish it.")
        else:
            lines.append(
                f"- `{repo}:{want}` (this run's day): **not on Docker Hub** — that day's build did "
                f"not publish, so the run fell back to an older image"
                + (f" (`{ran_tag}`)." if ran_tag else ".")
            )

    recent = dockerhub_recent_tags(repo, track)
    if recent is None:
        lines.append(f"- Newest `{track}-*` tags: Docker Hub did not answer.")
    elif not recent:
        lines.append(f"- No `{track}-*` tags on Docker Hub.")
    else:
        lines.append(
            f"- Newest `{track}-*` tags: "
            + ", ".join(f"`{t['tag'].rsplit('-', 1)[-1]}` ({_hub_time(t['pushed'])})" for t in recent)
        )
        missing = _missing_days([t["tag"].rsplit("-", 1)[-1] for t in recent])
        if missing:
            lines.append(
                f"  - **No image published on {', '.join(missing)}**: nightlies on those "
                "days ran on the newest image before them."
            )
    return lines


def _missing_days(days: list[str]) -> list[str]:
    """``YYYY-MM-DD`` days with no tag between the newest and oldest of ``days``
    (``YYYYMMDD`` strings)."""
    have = {d for d in days if len(d) == 8}
    if len(have) < 2:
        return []
    day = datetime.strptime(min(have), "%Y%m%d")
    newest = datetime.strptime(max(have), "%Y%m%d")
    out = []
    while day < newest:
        day += timedelta(days=1)
        if day.strftime("%Y%m%d") not in have:
            out.append(day.strftime("%Y-%m-%d"))
    return out


_miles_repo_lock = threading.Lock()


def ensure_miles_repo() -> Path | None:
    """Clone or update radixark/miles at ``MILES_REPO_PATH`` (full history,
    so the agent can ``git log`` a regression window). None on failure."""
    _log = logging.getLogger("ci-monitor")
    with _miles_repo_lock:
        try:
            if (MILES_REPO_PATH / ".git").exists():
                subprocess.run(["git", "worktree", "prune"], cwd=MILES_REPO_PATH,
                               capture_output=True, timeout=30)
                subprocess.run(["git", "fetch", "origin", "--prune"], cwd=MILES_REPO_PATH,
                               capture_output=True, timeout=300)
            else:
                if MILES_REPO_PATH.exists():
                    shutil.rmtree(MILES_REPO_PATH)
                AGENT_WORKSPACE.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    ["git", "clone", f"https://github.com/{MILES_REPO}.git", str(MILES_REPO_PATH)],
                    capture_output=True, timeout=600, check=True,
                )
        except (subprocess.SubprocessError, OSError) as exc:
            _log.warning("Could not prepare the %s clone: %s", MILES_REPO, exc)
            return None
    return MILES_REPO_PATH


def add_miles_worktree(dest: Path, sha: str, fork: str = "") -> bool:
    """Check radixark/miles out at ``sha`` into ``dest`` (a detached worktree).

    GitHub serves a fork's commits through the parent repo, so ``origin`` is
    tried first; ``fork`` (``<owner>/miles``) is the fallback.
    """
    repo = ensure_miles_repo()
    if repo is None:
        return False
    with _miles_repo_lock:
        def git(*args: str, cwd: Path = repo) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                  text=True, timeout=300)

        if git("cat-file", "-t", sha).returncode != 0:
            git("fetch", "origin", sha)
        if git("cat-file", "-t", sha).returncode != 0 and fork:
            git("fetch", f"https://github.com/{fork}.git", sha)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if git("worktree", "add", "--detach", str(dest), sha).returncode != 0:
            return False
        return git("rev-parse", "HEAD", cwd=dest).stdout.strip() == sha


def checkout_sglang_for_miles(
    wt_path: Path, sha: str, ref: str, token: str | None = None,
) -> bool:
    """Move an sglang worktree to the (possibly abbreviated) commit an image
    was built from.

    The build log prints the commit abbreviated, and the branch it came from
    (``sglang-miles``) is force-pushed, so the commit is often no longer on
    it. The branch is tried first; failing that, the full SHA is looked up and
    fetched on its own (GitHub still serves it).
    """
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=wt_path, capture_output=True,
                              text=True, timeout=300)

    if ref:
        git("fetch", "origin", ref, "--depth", "300")
    if git("cat-file", "-t", sha).returncode != 0 and token:
        resp = requests.get(
            f"https://api.github.com/repos/{REPO}/commits/{sha}",
            headers=gh_headers(token), timeout=60,
        )
        full = resp.json().get("sha", "") if resp.status_code == 200 else ""
        if full:
            git("fetch", "origin", full, "--depth", "1")
    if git("cat-file", "-t", sha).returncode != 0:
        return False
    git("checkout", "--force", "--detach", sha)
    return git("rev-parse", "HEAD").stdout.strip().startswith(sha)


def prepare_miles_context(
    job: dict, work_dir: Path, token: str | None = None, workflow_file: str = "",
) -> dict:
    """Lay out what a Miles job's agent needs under ``.ci-context/miles``.

    - ``EVIDENCE.md``: the dashboard's facts about this job's failure.
    - ``src/``: radixark/miles at the commit the job's image was built from.

    and moves ``work_dir`` (the sglang worktree) to the image's sglang commit.
    Every step degrades: what could not be prepared is said in EVIDENCE.md.
    Returns the paths and commits, relative to ``work_dir``.
    """
    _log = logging.getLogger("ci-monitor")
    payload = fetch_miles_cause_fix()
    rows = miles_rows_for_job(payload, job["id"])
    image = miles_job_image(rows, job["id"])
    commits, refs = image["commits"], image["refs"]

    checkout: dict = {"miles_src": "", "sglang_src": ""}
    sglang_sha = commits.get(REPO, "")
    if sglang_sha and checkout_sglang_for_miles(
        work_dir, sglang_sha, refs.get(REPO, ""), token,
    ):
        checkout["sglang_src"] = "."
    elif sglang_sha:
        _log.warning("Could not check out sglang %s for Miles job %s", sglang_sha, job["id"])

    context_dir = work_dir / ".ci-context" / "miles"
    if context_dir.exists():
        shutil.rmtree(context_dir)
    context_dir.mkdir(parents=True, exist_ok=True)

    miles_sha = commits.get(MILES_REPO, "")
    miles_fork = (image.get("forks") or {}).get(MILES_REPO, "")
    if miles_sha and add_miles_worktree(context_dir / "src", miles_sha, miles_fork):
        checkout["miles_src"] = ".ci-context/miles/src"
    elif miles_sha:
        _log.warning("Could not check out %s %s for job %s", MILES_REPO, miles_sha, job["id"])

    try:
        docker = check_miles_image(workflow_file, job, image["image"])
    except Exception as exc:  # the rest of the evidence still stands
        _log.warning("Docker Hub check failed for job %s: %s", job["id"], exc)
        docker = [f"- Docker Hub check failed: `{exc}`"]

    evidence = context_dir / "EVIDENCE.md"
    evidence.write_text(
        render_miles_evidence(job, rows, image, checkout, payload, docker),
        encoding="utf-8",
    )
    _log.info(
        "Prepared Miles context for job %s: %d row(s), miles %s, sglang %s",
        job["id"], len(rows), checkout["miles_src"] or "-", checkout["sglang_src"] or "-",
    )
    return {
        "evidence": ".ci-context/miles/EVIDENCE.md",
        "image": image["image"],
        "miles_commit": miles_sha if checkout["miles_src"] else "",
        "miles_fork": miles_fork,
        "sglang_commit": sglang_sha if checkout["sglang_src"] else "",
        **checkout,
    }


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
    """Get every latest-attempt job for a workflow run across all API pages."""
    url = f"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/jobs"
    all_jobs: list[dict] = []
    seen_job_ids: set[int] = set()
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
        for job in jobs:
            job_id = job.get("id")
            if isinstance(job_id, int):
                if job_id in seen_job_ids:
                    continue
                seen_job_ids.add(job_id)
            all_jobs.append(job)
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


def build_artifact_context_excerpt(
    artifact_root: Path,
    max_chars: int = MI355X_ARTIFACT_CONTEXT_MAX_CHARS,
) -> str:
    """Build a compact text bundle from downloaded artifact logs for API mode."""
    if not artifact_root.exists():
        return ""

    parts: list[str] = []
    manifest = artifact_root / "MANIFEST.md"
    if manifest.exists():
        parts.append(f"### {manifest.relative_to(artifact_root.parent.parent)}\n")
        parts.append(manifest.read_text(errors="replace")[:20_000])

    def priority(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        if name == "bench.log":
            return (0, str(path))
        if name.startswith("prefill_"):
            return (1, str(path))
        if name.startswith("decode_"):
            return (2, str(path))
        if name.startswith("server_exit") or name == "bench_exit":
            return (3, str(path))
        if name.endswith(".log"):
            return (4, str(path))
        return (5, str(path))

    files = [
        p for p in artifact_root.rglob("*")
        if p.is_file()
        and p.name != "MANIFEST.md"
        and (
            p.suffix == ".log"
            or p.name.startswith("server_exit")
            or p.name == "bench_exit"
            or p.name.endswith(".json")
        )
    ]

    for path in sorted(files, key=priority):
        if sum(len(p) for p in parts) >= max_chars:
            parts.append("\n... [artifact context truncated] ...\n")
            break
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        remaining = max_chars - sum(len(p) for p in parts)
        if remaining <= 0:
            break
        snippet = prefilter_large_step_log(text, max_chars=min(20_000, remaining))
        rel = path.relative_to(artifact_root.parent.parent)
        parts.append(f"\n\n### {rel}\n```text\n{snippet}\n```")

    return "".join(parts)[:max_chars]


# ---------------------------------------------------------------------------
# Agent instructions (agent/) and API-mode prompt templates
# ---------------------------------------------------------------------------

def _find_agent_file(name: str) -> Path | None:
    """Locate ``agent/<name>`` in the bot repo (checks multiple candidate roots)."""
    candidates = [
        Path(__file__).resolve().parent.parent / "agent" / name,
        Path("/tmp/bot/agent") / name,
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


@dataclass(frozen=True)
class AgentTask:
    doc: str          # methodology file under agent/tasks/
    model: str = ""   # "" = the runner's default (ANTHROPIC_MODEL)


# Every agent task, keyed by the prompt's ``Task:`` line. The model is matched
# to what a wrong answer costs: tasks whose output is published or decides what
# gets published keep the runner's default (Opus); extraction and second-opinion
# votes use Sonnet. Override one with AGENT_MODEL_<TASK> (e.g.
# AGENT_MODEL_FAILURE_TRACKER_DATA=opus).
AGENT_TASKS: dict[str, AgentTask] = {
    "Job Failure Analysis": AgentTask("job-failure-analysis.md"),
    "Cross-Job Summary": AgentTask("cross-job-summary.md"),
    "Cross-Run Pattern Analysis": AgentTask("cross-run-pattern-analysis.md"),
    "Daily Cross-Workflow Summary": AgentTask("daily-cross-workflow-summary.md"),
    "Failure Tracker Data": AgentTask("failure-tracker-data.md", model="sonnet"),
    "SGLang AMD CI Staging Candidates": AgentTask("staging-candidates.md"),
    "PR CI Status Check": AgentTask("pr-ci-status-check.md"),
    "PR Code Review": AgentTask("pr-code-review.md"),
    "PR Code Review Verification": AgentTask("pr-code-review-verify.md", model="sonnet"),
    "Suspect PR Verification": AgentTask("suspect-pr-verify.md"),
}

_MODEL_ALIASES = ("opus", "sonnet", "haiku")
# The gateway's answer for a model name it has no deployment of, e.g.
# 'API Error: 400 Deployment of "Claude-Sonnet-4.7" for "..." is not found!'
_MODEL_UNAVAILABLE_RE = re.compile(
    r"API Error: 40[04].*(?:Deployment of .* not found|model.*(?:not found|not_found|does not exist))",
    re.IGNORECASE,
)


def task_name(prompt: str) -> str:
    """The task named by the prompt's first line (``Task: <name>``)."""
    first = prompt.split("\n", 1)[0]
    return first.removeprefix("Task:").strip() if first.startswith("Task:") else ""


def agent_instructions(task: str) -> str:
    """Shared rules (agent/CLAUDE.md) followed by the task's methodology."""
    spec = AGENT_TASKS.get(task)
    if spec is None:
        raise RuntimeError(f"Unknown agent task {task!r}; add it to AGENT_TASKS")
    parts = []
    for name in ("CLAUDE.md", f"tasks/{spec.doc}"):
        path = _find_agent_file(name)
        if path is None:
            raise RuntimeError(f"agent/{name} not found")
        parts.append(path.read_text().strip())
    return "\n\n---\n\n".join(parts) + "\n"


def task_model(task: str) -> str | None:
    """The ``--model`` value for *task*, or None for the runner's default.

    Through the LLM gateway an alias only works when the runner maps it
    (ANTHROPIC_DEFAULT_<ALIAS>_MODEL); an unmapped alias would name a model
    the gateway does not serve, so the task keeps the default instead.
    """
    spec = AGENT_TASKS.get(task)
    slug = re.sub(r"[^A-Z0-9]+", "_", task.upper()).strip("_")
    model = os.environ.get(f"AGENT_MODEL_{slug}") or (spec.model if spec else "")
    if not model:
        return None
    if (model in _MODEL_ALIASES and os.environ.get("ANTHROPIC_BASE_URL")
            and not os.environ.get(f"ANTHROPIC_DEFAULT_{model.upper()}_MODEL")):
        logging.getLogger("claude-agent").warning(
            "%s: model alias %r is not mapped on this runner; using the default model",
            task, model,
        )
        return None
    return model


_TEMPLATE_HEADING_RE = re.compile(r"^### ([a-z][a-z0-9-]+)$", re.MULTILINE)


def load_prompt_template(section_name: str) -> str | None:
    """Load a prompt template from agent/api-prompts.md by section heading.

    Looks for ``### <section_name>`` (kebab-case) under ``## API Mode Prompts``
    and returns the text up to the next kebab-case ``### `` heading or
    section end.  Content headings like ``### Failure Summary`` (Title Case)
    inside the template are preserved.
    """
    path = _find_agent_file("api-prompts.md")
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
    github_token: str | None = None,
    run_jobs: list[dict] | None = None,
) -> dict:
    """Invoke Claude Code agent to fully analyze a CI failure.

    The agent handles everything autonomously: downloading logs via the
    GitHub API (using ``$GH_PAT`` from the environment), parsing errors,
    reading sglang source code, checking git history, and producing a
    root-cause analysis.  Investigation methodology is defined in
    ``agent/tasks/job-failure-analysis.md``, appended to the system prompt.

    *run_jobs* (the failed jobs of the same run) only matter when this job's
    runner was lost: the other lost jobs are listed for the agent.
    """
    job_name = job["name"]
    job_id = job["id"]

    failed_step_names = {
        s["name"]
        for s in job.get("steps", [])
        if s.get("conclusion") not in ("success", "skipped", None)
    }

    artifact_lines = ""
    if _is_mi355x_disagg_workflow(workflow_file):
        run_id = _run_id_from_url(run_url)
        if run_id is not None:
            artifact_token = (
                github_token
                or os.environ.get("GH_PAT")
                or os.environ.get("BOT_PAT")
                or os.environ.get("GITHUB_TOKEN")
            )
            artifact_root = prepare_mi355x_artifact_context(
                artifact_token, run_id, repo_path,
            )
            manifest = (
                f"{artifact_root.relative_to(repo_path)}/MANIFEST.md"
                if artifact_root is not None else "unavailable"
            )
            artifact_lines = (
                f"Artifact API URL: https://api.github.com/repos/{REPO}/actions/runs/{run_id}/artifacts\n"
                f"Artifact name prefix: {MI355X_ARTIFACT_PREFIX}\n"
                f"Artifact context: {manifest}\n"
            )
    elif is_miles_workflow(workflow_file):
        try:
            miles = prepare_miles_context(
                job, repo_path,
                github_token or os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN"),
                workflow_file,
            )
        except Exception as exc:  # the agent can still work from the job log
            logging.getLogger("ci-monitor").warning(
                "Miles context failed for job %s: %s", job_id, exc,
            )
            miles = {}
        miles_src = (
            f"{miles['miles_src']} ({MILES_REPO} @ {miles['miles_commit']}"
            + (f", built from the fork {miles['miles_fork']}" if miles.get("miles_fork") else "")
            + ")"
            if miles.get("miles_src") else "unavailable (see the evidence file)"
        )
        sglang_src = (
            f"current directory ({REPO} @ {miles['sglang_commit']}, the image's sglang)"
            if miles.get("sglang_src")
            else "current directory, at the run's commit — NOT the image's sglang"
        )
        artifact_lines = (
            f"Miles evidence: {miles.get('evidence') or 'unavailable'}\n"
            f"Miles image: {miles.get('image') or 'unknown'}\n"
            f"Miles source: {miles_src}\n"
            f"sglang source: {sglang_src}\n"
        )

    event_line = f"Event filter: {event_filter}\n" if event_filter else ""
    runner_lost_lines = ""
    if is_runner_lost(job):
        token = github_token or os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN")
        runner_lost_lines = (
            "Runner lost: yes\n"
            f"{runner_lost_evidence(token, job, run_jobs)}\n"
        )
    prompt = (
        f"Task: Job Failure Analysis\n"
        f"Job: {job_name}\n"
        f"Run: {run_url}\n"
        f"Job ID: {job_id}\n"
        f"Commit SHA: {head_sha}\n"
        f"Workflow file: {workflow_file}\n"
        f"{event_line}"
        f"Log URL: https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs\n"
        f"{runner_lost_lines}"
        f"{artifact_lines}"
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
    workflow_file: str = "",
    run_jobs: list[dict] | None = None,
) -> dict:
    """Download logs, extract errors, and run focused API analysis for one job."""
    _log = logging.getLogger("ci-monitor")
    job_name = job["name"]
    job_id = job["id"]
    run_id = _run_id_from_url(run_url) or int(run_url.rstrip("/").split("/")[-1])

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
    if is_runner_lost(job):
        _log.info("  [%s] Runner lost mid-job", job_name)
        filtered_log = (
            "## Runner lost\n"
            f"{runner_lost_evidence(token, job, run_jobs)}\n\n{filtered_log}"
        )
    if len(filtered_log) < len(raw_log):
        _log.info(
            "  [%s] Pre-filtered log: %s -> %s chars",
            job_name, f"{len(raw_log):,}", f"{len(filtered_log):,}",
        )

    if _is_mi355x_disagg_workflow(workflow_file):
        _log.info("  [%s] Downloading MI355X run artifacts...", job_name)
        with tempfile.TemporaryDirectory(prefix="mi355x-artifacts-") as tmp:
            artifact_root = prepare_mi355x_artifact_context(token, run_id, Path(tmp))
            if artifact_root is not None:
                artifact_excerpt = build_artifact_context_excerpt(artifact_root)
                if artifact_excerpt:
                    filtered_log = (
                        f"{filtered_log}\n\n"
                        f"## MI355X Run Artifact Logs\n"
                        f"{artifact_excerpt}"
                    )
                    _log.info(
                        "  [%s] Added %s chars of MI355X artifact context",
                        job_name, f"{len(artifact_excerpt):,}",
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

    Prompt template is loaded from agent/api-prompts.md ``### focused-job-analysis``.
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
    read sglang source code to verify patterns. Agent failures are raised,
    not answered by the API: a single-shot API summary of dozens of per-job
    analyses is too shallow to publish, so callers keep the last good one.

    Env vars (agent mode):
      - CROSS_JOB_TIMEOUT_SECS (default 1200) — a busy pr-test-amd day has
        60+ analyses of ~20 KB each; 300 s timed out on every tick.
      - CROSS_JOB_MAX_TURNS (default 100)
    """
    jobs_text = "\n\n---\n\n".join(
        f"### Job: {ja['job_name']}\n"
        f"**Job ID:** `{ja.get('job_id', 'unknown')}`\n\n"
        f"{ja['analysis']}"
        for ja in job_analyses
    )

    if use_agent:
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
            max_turns=int(os.environ.get("CROSS_JOB_MAX_TURNS", "100")),
            timeout_secs=int(os.environ.get("CROSS_JOB_TIMEOUT_SECS", "1200")),
        )

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
MILES_REPO_PATH = AGENT_WORKSPACE / "miles"


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


def agent_failure_notice(what: str, reason: object) -> str:
    """Markdown notice published in place of output the agent failed to produce.

    Agent mode never falls back to a single-shot API call: its answers are
    too shallow to publish, so readers are told the agent did not work.
    """
    reason = " ".join(str(reason).replace("`", "'").split())[:300] or "unknown error"
    return (
        "> [!WARNING]\n"
        f"> **The Claude Code agent did not work**, so {what} could not be produced.\n"
        f"> Error: `{reason}`\n"
        "> No API fallback is used. Re-run the command, or check the amd-bot runners."
    )


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

    _remove_deployed_claude_md(wt_path)
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


def checkout_commit(wt_path: Path, sha: str, fetch_ref: str | None = None) -> bool:
    """Check out *sha* in *wt_path* and return whether HEAD is now exactly *sha*.

    Fetches *fetch_ref* (e.g. ``pull/123/head``) and then *sha* itself when the
    commit is not already present; GitHub serves old merge commits by SHA.
    """
    def git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=wt_path, capture_output=True,
                              text=True, timeout=timeout)

    if fetch_ref:
        git("fetch", "origin", fetch_ref, "--depth", "100")
    if git("cat-file", "-t", sha).returncode != 0:
        git("fetch", "origin", sha, "--depth", "1")
    git("checkout", "--force", sha, timeout=60)
    return git("rev-parse", "HEAD").stdout.strip() == sha


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
        # Seen on runner-7: HEAD stopped resolving ("invalid reference: HEAD"),
        # so every worktree failed. A fresh clone is cheap; a broken one stays broken.
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            cwd=SGLANG_REPO_PATH, capture_output=True, text=True, timeout=30,
        )
        if head.returncode != 0:
            _agent_log.warning("sglang repo at %s has no valid HEAD; cloning it again", SGLANG_REPO_PATH)
            shutil.rmtree(SGLANG_REPO_PATH)

    if not (SGLANG_REPO_PATH / ".git").exists():
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

    _remove_deployed_claude_md()
    _agent_log.info("sglang repo ready at %s", SGLANG_REPO_PATH)
    return SGLANG_REPO_PATH


def _remove_deployed_claude_md(target_dir: Path | None = None):
    """Delete a CLAUDE.md an older bot version copied above the worktrees.

    The agent's instructions now arrive through ``--append-system-prompt``
    (see ``agent_instructions()``); a stale full copy left in the workspace
    would be loaded alongside them.
    """
    stale = (target_dir.parent if target_dir else AGENT_WORKSPACE) / "CLAUDE.md"
    if stale.exists():
        stale.unlink()
        _agent_log.info("Removed stale %s", stale)


_SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS", "GH_PAT", "GH_TOKEN",
               "GITHUB_TOKEN", "AGENT_GH_TOKEN", "LLM_GATEWAY_KEY", "BOT_PAT")
_TOKEN_RE = re.compile(r"\b(ghp_|gho_|ghs_|github_pat_|sk-)[A-Za-z0-9_\-]{16,}")


def redact_secrets(text: str) -> str:
    """Replace this process's tokens and keys (and token-shaped strings) with ``[redacted]``."""
    secrets = []
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        # ANTHROPIC_CUSTOM_HEADERS is "Header-Name: <key>"; the key alone can leak too.
        secrets += [value, value.split(":", 1)[-1].strip()]
    for secret in sorted({s for s in secrets if len(s) >= 8}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return _TOKEN_RE.sub("[redacted]", text)


def _archive_agent_session(work_dir: Path, started_at: float, prompt: str) -> None:
    """Copy the agent's session log and prompt to ``$AGENT_ARCHIVE_DIR`` (if set).

    Workflows upload that directory; without it a bad report cannot be traced
    back to what the agent actually read and ran.
    """
    archive_root = os.environ.get("AGENT_ARCHIVE_DIR")
    if not archive_root:
        return
    try:
        task = prompt.split("\n", 1)[0].removeprefix("Task:").strip() or "agent"
        # Parallel agents (review verifiers) can finish in the same second.
        stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}"
        dest = Path(archive_root) / f"{stamp}-{_safe_name(task)}"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "prompt.txt").write_text(redact_secrets(prompt))
        cwd = str(work_dir.resolve())
        projects = Path.home() / ".claude" / "projects"
        for project in {projects / cwd.replace("/", "-"), projects / re.sub(r"[^A-Za-z0-9]", "-", cwd)}:
            for session in project.glob("*.jsonl"):
                # File mtimes come from a coarse clock and can trail time.time().
                if session.stat().st_mtime >= started_at - 5:
                    (dest / session.name).write_text(redact_secrets(session.read_text(errors="replace")))
    except Exception as exc:
        _agent_log.warning("Could not archive agent session: %s", exc)


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


# Files that set the environment AMD PR CI tests actually run with. PR CI pulls
# a prebuilt rocm/sgl-dev image (built from docker/rocm.Dockerfile), starts it
# with amd_ci_start_container.sh, and runs every test through amd_ci_exec.sh.
# Anything set here overrides code-level defaults like
# os.environ.get("FOO", "NONE"), so the agent must see it before calling a code
# path "default-off".
CI_RUNTIME_DOCKERFILES = ("docker/rocm.Dockerfile",)
CI_RUNTIME_SCRIPTS = (
    "scripts/ci/amd/amd_ci_start_container.sh",
    "scripts/ci/amd/amd_ci_exec.sh",
)
CI_RUNTIME_WORKFLOW_GLOB = ".github/workflows/pr-test-amd*.yml"

_ENV_NAME = r"[A-Z][A-Z0-9_]*"
_SHELL_ENV_RES = (
    # amd_ci_exec.sh: `declare -A ENV_MAP=( [KEY]=VAL ... )` entries
    re.compile(rf"^\s*\[({_ENV_NAME})\]=(\S*)"),
    # amd_ci_exec.sh: conditional `ENV_MAP[KEY]="VAL"` assignments
    re.compile(rf"ENV_MAP\[({_ENV_NAME})\]=(\S+)"),
    # `docker run/exec -e KEY=VAL` and `amd_ci_exec.sh -e KEY=VAL`
    re.compile(rf"(?:^|\s)-e\s+[\"']?({_ENV_NAME})=(\"[^\"]*\"|'[^']*'|[^\s\"']*)"),
)


def _parse_dockerfile_env(text: str) -> list[tuple[str, int, str, str]]:
    """Return ``(stage, line, key, value)`` for every ``ENV`` instruction."""
    entries: list[tuple[str, int, str, str]] = []
    stage = "(first stage)"
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start = i
        logical = lines[i].rstrip()
        while logical.endswith("\\") and i + 1 < len(lines):
            i += 1
            logical = logical[:-1] + " " + lines[i].strip()
        i += 1

        # Skip `RUN <<EOF ... EOF` heredoc bodies; an embedded script line
        # such as `from pathlib import Path` is not a Dockerfile instruction.
        heredoc = re.search(r"(?<!<)<<(?!<)-?\s*[\"']?(\w+)[\"']?", logical)
        if heredoc:
            end = next(
                (j for j in range(i, len(lines)) if lines[j].strip() == heredoc.group(1)),
                None,
            )
            if end is not None:
                i = end + 1

        # Instructions are matched in upper case only (the sglang convention),
        # so stray lower-case shell or Python text is never misread.
        parts = logical.strip().split(None, 1)
        if not parts:
            continue
        instruction = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        if instruction == "FROM":
            m = re.search(r"\s+AS\s+(\S+)", rest, re.IGNORECASE)
            stage = m.group(1) if m else f"FROM {rest.strip()}"
            continue
        if instruction != "ENV":
            continue
        try:
            tokens = shlex.split(rest)
        except ValueError:
            tokens = rest.split()
        if not tokens:
            continue
        if "=" in tokens[0]:
            for tok in tokens:
                key, _, value = tok.partition("=")
                entries.append((stage, start + 1, key, value))
        else:
            # Legacy `ENV KEY value with spaces` form.
            entries.append((stage, start + 1, tokens[0], " ".join(tokens[1:])))
    return entries


def _parse_shell_env(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line, key, value)`` for env assignments passed into CI containers."""
    entries: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for pattern in _SHELL_ENV_RES:
            for m in pattern.finditer(line):
                entries.append((lineno, m.group(1), m.group(2).strip("\"'")))
    return entries


def collect_ci_runtime_env(repo_path: Path) -> str:
    """Render the env vars AMD PR CI sets, as a markdown context file.

    Read from *repo_path* (the sglang checkout the agent works in). The agent
    uses this to tell whether an env-gated code path is actually live in CI,
    instead of judging from the default in the source code.
    """
    out = [
        "# AMD PR CI runtime environment",
        "",
        "Env vars that AMD PR CI sets for its tests, extracted from the files "
        "below in this checkout. They override code-level defaults such as "
        '`os.environ.get("FOO", "NONE")`.',
        "",
        "- The `docker/rocm.Dockerfile` ENV lines are baked into the prebuilt "
        "`rocm/sgl-dev` image, which is built nightly from `main`. If this PR edits that "
        "file, CI still runs with the image's (main) values.",
        "- The container start/exec scripts apply to every AMD test job and run "
        "from the PR checkout, so their values are the PR's.",
        "- Workflow `-e` flags apply only to the job that contains that line.",
        "- Individual tests can still override any of these in their own env.",
        "",
    ]

    for rel in CI_RUNTIME_DOCKERFILES:
        path = repo_path / rel
        out.append(f"## `{rel}` (image ENV, per build stage)")
        out.append("")
        if not path.exists():
            out.append("*(file not found in this checkout)*")
        else:
            for stage, lineno, key, value in _parse_dockerfile_env(path.read_text(errors="replace")):
                out.append(f"- `{key}={value}` (stage `{stage}`, L{lineno})")
        out.append("")

    workflow_paths = sorted(
        str(p.relative_to(repo_path)).replace(os.sep, "/")
        for p in repo_path.glob(CI_RUNTIME_WORKFLOW_GLOB)
    )
    for rel in (*CI_RUNTIME_SCRIPTS, *workflow_paths):
        path = repo_path / rel
        out.append(f"## `{rel}`")
        out.append("")
        if not path.exists():
            out.append("*(file not found in this checkout)*")
            out.append("")
            continue
        entries = _parse_shell_env(path.read_text(errors="replace"))
        if not entries:
            out.append("*(no env assignments found)*")
        for lineno, key, value in entries:
            out.append(f"- `{key}={value}` (L{lineno})")
        out.append("")

    return "\n".join(out)


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

    The prompt's ``Task:`` line picks the instructions (``agent/CLAUDE.md``
    plus that task's file under ``agent/tasks/``), which are appended to the
    system prompt, and the model (``task_model()``). Only user-level settings
    are loaded: the working directory is an sglang checkout, often a PR's,
    whose ``.claude/`` (settings, hooks, rules) anyone can write.

    If *output_must_contain* is set and the agent's final stdout does
    not contain that substring, scan the session log for the last
    assistant message that does — this catches the failure mode where
    the agent writes the canonical report and then overwrites it with
    a short reply like "already delivered above".

    Raises ``RuntimeError`` on failure, including an unknown task.
    """
    task = task_name(prompt)
    instructions = agent_instructions(task)
    model = task_model(task)

    context_dir = work_dir / ".ci-context"
    context_dir.mkdir(parents=True, exist_ok=True)

    # The agent reads PR content written by anyone; when a read-only token is
    # configured, it replaces the bot's posting token in the agent's env.
    env = dict(os.environ)
    if env.get("AGENT_GH_TOKEN"):
        env["GH_PAT"] = env["GH_TOKEN"] = env["AGENT_GH_TOKEN"]
        env.pop("GITHUB_TOKEN", None)
    for name in ("BOT_PAT", "LLM_GATEWAY_KEY"):
        env.pop(name, None)

    started_at = time.time()
    written: list[Path] = []
    try:
        if context_files:
            for filename, content in context_files.items():
                p = context_dir / filename
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
                written.append(p)

        cmd = [
            "claude", "-p", prompt,
            "--append-system-prompt", instructions,
            "--setting-sources", "user",
            "--output-format", "text",
            "--max-turns", str(max_turns),
            "--dangerously-skip-permissions",
            # The sglang checkout ships developer skills (e.g. ones that push
            # fixes or rerun CI); none of the bot's tasks use skills.
            "--disable-slash-commands",
        ]
        if model:
            cmd += ["--model", model]

        _agent_log.info(
            "Running Claude Code (task=%s, model=%s, max_turns=%d, timeout=%ds, cwd=%s)...",
            task, model or "default", max_turns, timeout_secs, work_dir,
        )

        def run_cli() -> subprocess.CompletedProcess:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(work_dir),
                timeout=timeout_secs,
                stdin=subprocess.DEVNULL,
                env=env,
            )

        result = run_cli()
        if (model and result.returncode != 0
                and _MODEL_UNAVAILABLE_RE.search(f"{result.stdout}\n{result.stderr}")):
            # A task model the gateway does not serve (e.g. a stale
            # ANTHROPIC_DEFAULT_SONNET_MODEL) must not fail the task.
            _agent_log.warning(
                "%s: model %r is not served by the gateway (%s); retrying with the default model",
                task, model, " ".join((result.stdout or result.stderr).split())[:200],
            )
            i = cmd.index("--model")
            del cmd[i:i + 2]
            result = run_cli()

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
        _archive_agent_session(work_dir, started_at, prompt)
        for p in written:
            p.unlink(missing_ok=True)
        subdirs = {d for p in written for d in p.parents if context_dir in d.parents}
        for d in sorted(subdirs, key=lambda d: len(d.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        try:
            context_dir.rmdir()
        except OSError:
            pass
