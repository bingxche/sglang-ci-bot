"""Evidence pack for PR CI failure attribution (ci-status).

Collected by code and frozen before the LLM runs. Every fact gets a stable
evidence ID that verdicts cite and validation checks:

- ``D1..Dn``   hunks of the PR diff (head against its merge base)
- ``F1..Fn``   failed tests (or failed non-test jobs), with
  ``Fk.failure`` (log excerpt), ``Fk.metrics`` (raw values per attempt),
  ``Fk.env`` (commit actually tested, image tag and digest, GPU, image ENV,
  exec-script env), ``Fk.launch`` (server launch arguments),
  ``Fk.markers`` (backend selection lines, AITER kernel modules loaded)
- ``Fk.M1..``  the same job in recent ``main`` runs, with comparability flags
- ``G1..``     what could not be collected, and why

Environment values record their source: ``ci_fingerprint`` (an
``SGLANG_CI_FINGERPRINT`` line), ``log``, or ``reconstructed`` (looked up after
the fact, such as image ENV by tag). Files are written to
``<repo>/.ci-context/``: ``evidence.json``, ``evidence.md``, ``pr.diff``, and
the logs under ``logs/`` and ``logs/main/``.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import time
from collections import Counter
from pathlib import Path

import requests

from utils import REPO, checkout_commit, download_job_logs, extract_error_lines, gh_headers, is_gate_job

SCHEMA_VERSION = 1
BASELINE_RUNS = 6
MAX_BASELINE_LOGS = 40
_FAILED = {"failure", "timed_out"}

_TS_RE = re.compile(r"^\ufeff?\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
_CHECKOUT_RE = re.compile(r"git checkout --progress --force ([0-9a-f]{40})")
_HEAD_RE = re.compile(r"HEAD is now at ([0-9a-f]{7,40}) ")
_MERGE_RE = re.compile(r"HEAD is now at [0-9a-f]+ Merge ([0-9a-f]{40}) into ([0-9a-f]{40})")
_IMAGE_RE = re.compile(r"(?:Found available image|Found cached image locally|Using custom image): (\S+)")
_DIGEST_RE = re.compile(r"^Digest: (sha256:[0-9a-f]{64})")
_GPU_RE = re.compile(r"\b(mi\d{2,3}[a-z]?)\b", re.IGNORECASE)
_FINGERPRINT_RE = re.compile(r"SGLANG_CI_FINGERPRINT (\{.*\})")
_FAST_FAIL_RE = re.compile(r"##\[error\]Fail-fast: skipping \S+ root cause job\(s\): ([^\n]*)")
_OPT_IN_GATE_RE = re.compile(r"\b(?:call-gate|pr-gate)\b")
_FILE_START_RE = re.compile(r"^python3? (\S*?(test/\S+?\.py))\s*$")
_FILE_END_RE = re.compile(r"^filename='(\S*?(test/\S+?\.py))'")
_FILE_FAILED_RE = re.compile(r"^✗ FAILED: (\S*?(test/\S+?\.py))")
_UNITTEST_FAIL_RE = re.compile(r"^(?:ERROR|FAIL): (\w+) \(([\w.]+)\)")
_PYTEST_FAIL_RE = re.compile(r"^FAILED \S+?\.py::(\S+)")
_EXCEPTION_RE = re.compile(r"^(?:E\s+)?(?:\w+\.)*\w+(?:Error|Exception): ")
_THRESHOLD_RE = re.compile(r"not (?:greater|less) than|not less than or equal|not greater than or equal")
_CRASH_RE = re.compile(r"Memory access fault|HSA_STATUS_ERROR|Segmentation fault|core dumped|exit code 137")
_TIMEOUT_RE = re.compile(r"timed out|TimeoutError|TimeoutExpired")
_ACCURACY_RE = re.compile(r"^Accuracy: ([0-9.]+)\s*$")
_INVALID_RE = re.compile(r"^Invalid: ([0-9.]+)\s*$")
_RETRY_RE = re.compile(r"retry\(\) failed once")
_CASE_MARKER_RE = re.compile(r"\[CI Test Method\] (\S+)")
_SERVER_ARGS_RE = re.compile(r"server_args=\{")
_LAUNCH_KEYS = ("model_path", "tp_size", "pp_size", "dp_size", "ep_size", "attention_backend",
                "disaggregation_mode", "quantization", "kv_cache_dtype", "enable_dp_attention",
                "disable_custom_all_reduce", "speculative_algorithm", "page_size")
_LAUNCH_KV_RE = re.compile(r"'(" + "|".join(_LAUNCH_KEYS) + r")': ('[^']*'|[^,}]+)")
_BACKEND_RE = re.compile(r"(\[AR\][^\n]*|Using [A-Z][\w-]*(?: [\w-]+){0,3} ?(?:AllReduce|Allreduce|Backend|backend)\b[^\n]*"
                         r"|[Aa]ttention backend[^\n]*)")
_LOG_PREFIX_RE = re.compile(r"^\[[^\]]*\]\s*")
_AITER_MODULE_RE = re.compile(r"\[aiter\] import \[(module_\w+)\]")
_ENV_MAP_RES = (re.compile(r"^\s*\[([A-Z][A-Z0-9_]*)\]=(\S*)"),
                re.compile(r"ENV_MAP\[([A-Z][A-Z0-9_]*)\]=\"?([^\"\s]+)\"?"))
_GPU_ARCH_PREFIX = {"mi30": "gfx942", "mi32": "gfx942", "mi35": "gfx950", "mi45": "gfx1250"}


# ---------------------------------------------------------------------------
# GitHub and registry access
# ---------------------------------------------------------------------------

def _retry(fn, *args, **kwargs):
    """Call *fn*, retrying GitHub 5xx and network errors with backoff."""
    attempts = 5
    for i in range(attempts):
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, str) and result.startswith("[Failed to fetch logs: HTTP 5"):
                raise requests.ConnectionError(result)
            return result
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if i == attempts - 1 or (status is not None and status < 500):
                raise
            time.sleep(2 ** (i + 1))


def _get(token: str, path: str, **params) -> dict:
    resp = requests.get(f"https://api.github.com/repos/{REPO}/{path}",
                        headers=gh_headers(token), params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _paged(token: str, path: str, key: str, **params) -> list[dict]:
    items, page = [], 1
    while True:
        batch = _retry(_get, token, path, per_page=100, page=page, **params).get(key, [])
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def _attempt_as_of(token: str, run: dict, as_of: str) -> dict:
    """The run attempt that was current at *as_of* (the latest one by default)."""
    attempt = run.get("run_attempt") or 1
    while attempt > 1 and (run.get("run_started_at") or "") > as_of:
        attempt -= 1
        run = _retry(_get, token, f"actions/runs/{run['id']}/attempts/{attempt}")
    return run


def _pr_diff(token: str, base_ref: str, head_sha: str) -> tuple[str, list[dict]]:
    """Diff and files of *head_sha* against its merge base with *base_ref*."""
    compare = _get(token, f"compare/{base_ref}...{head_sha}")
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/compare/{base_ref}...{head_sha}",
        headers={**gh_headers(token), "Accept": "application/vnd.github.v3.diff"},
        timeout=120,
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text, compare.get("files", [])


def _main_runs(token: str, workflow_file: str, as_of: str, limit: int) -> list[dict]:
    """Recent completed runs of *workflow_file* on main itself (not PRs from forks)."""
    runs = _get(token, f"actions/workflows/{workflow_file}/runs", branch="main", status="completed",
                created=f"<={as_of}", per_page=100).get("workflow_runs", [])
    return [
        r for r in runs
        if r.get("event") != "pull_request"
        and (r.get("head_repository") or {}).get("full_name") == REPO
        and r.get("created_at", "") <= as_of
    ][:limit]


def fetch_image_env(image: str, digest: str | None) -> tuple[dict[str, str] | None, str]:
    """Read a Docker Hub image's ``ENV`` from the registry without pulling it.

    Returns ``(env, source)``: by digest when the CI log recorded one (the exact
    image that ran), otherwise by tag, which may have been re-pushed since.
    """
    repo, _, tag = image.rpartition(":")
    if not repo or "/" not in repo or "." in repo.split("/")[0]:
        return None, "unavailable (not a Docker Hub image)"
    try:
        token = requests.get(
            "https://auth.docker.io/token",
            params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
            timeout=30,
        ).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        accept = ", ".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        base = f"https://registry-1.docker.io/v2/{repo}"
        manifest = requests.get(f"{base}/manifests/{digest or tag}",
                                headers={**auth, "Accept": accept}, timeout=30).json()
        if "manifests" in manifest:
            ref = next(m["digest"] for m in manifest["manifests"]
                       if m.get("platform", {}).get("os") == "linux"
                       and m.get("platform", {}).get("architecture") == "amd64")
            manifest = requests.get(f"{base}/manifests/{ref}",
                                    headers={**auth, "Accept": accept}, timeout=30).json()
        config = requests.get(f"{base}/blobs/{manifest['config']['digest']}",
                              headers=auth, timeout=60).json()
        env = dict(e.split("=", 1) for e in config.get("config", {}).get("Env", []) if "=" in e)
        return env, ("registry by digest" if digest else "reconstructed (registry by tag)")
    except Exception as exc:
        return None, f"unavailable ({exc.__class__.__name__})"


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def _clean_lines(log: str) -> list[str]:
    return [_TS_RE.sub("", line).rstrip() for line in log.splitlines()]


def _file_sections(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Map each test file run by run_suite to its (start, end) line indices."""
    sections: dict[str, tuple[int, int]] = {}
    current, start = None, 0
    for i, line in enumerate(lines):
        m = _FILE_START_RE.match(line)
        if m:
            current, start = m.group(2), i
            sections[current] = (i, len(lines))
            continue
        if current and _FILE_END_RE.match(line):
            sections[current] = (start, i + 1)
            current = None
    return sections


def _gpu_arch(gpu: str | None) -> str | None:
    return _GPU_ARCH_PREFIX.get((gpu or "")[:4].lower())


def _launch_configs(lines: list[str]) -> list[dict]:
    configs = []
    for line in lines:
        if _SERVER_ARGS_RE.search(line):
            cfg = {k: v.strip("'") for k, v in _LAUNCH_KV_RE.findall(line)}
            if cfg and cfg not in configs:
                configs.append(cfg)
    return configs


def _case_bounds(lines: list[str], start: int, end: int, case: str) -> tuple[int, int, int] | None:
    """``(setup_start, case_start, case_end)`` of one test case inside a file section.

    sglang's test base class logs ``[CI Test Method] Class.method`` when a test
    starts. A class launches its servers (and logs ``server_args``) once, after
    the previous class's last test, so the setup of every method of the class
    starts at the last marker of a different class. The test's own output
    follows its marker.
    """
    markers = [(i, m.group(1)) for i in range(start, end) for m in [_CASE_MARKER_RE.search(lines[i])] if m]
    test_class = case.split(".")[0]
    for k, (i, name) in enumerate(markers):
        if name == case:
            setup = next((j for j, other in reversed(markers[:k]) if other.split(".")[0] != test_class), start)
            return setup, i, (markers[k + 1][0] if k + 1 < len(markers) else end)
    return None


def _failed_cases(section: list[str]) -> list[str]:
    cases = []
    for line in section:
        m = _UNITTEST_FAIL_RE.match(line)
        if m:
            cases.append(m.group(2).split(".", 1)[-1] if "." in m.group(2) else m.group(1))
        m = _PYTEST_FAIL_RE.match(line)
        if m:
            cases.append(m.group(1))
    return list(dict.fromkeys(cases))


def _mode(text: str, has_metrics: bool, has_error: bool) -> str:
    if has_metrics and _THRESHOLD_RE.search(text):
        return "metric_threshold"
    if _CRASH_RE.search(text):
        return "crash"
    if _TIMEOUT_RE.search(text):
        return "timeout"
    return "assertion_or_exception" if has_error else "unknown"


def _attempts(lines: list[str], start: int, end: int, metrics: list[dict]) -> list[dict]:
    """Each attempt of a retried test (split at ``retry() failed once``), described on its own.

    An attempt's mode comes from its own errors, which are logged in order.
    Printed metrics (stdout) can land after the next retry line, so they are
    handed, in order, to the attempts whose error is a metric threshold, and
    only when the counts match.
    """
    cuts = [i + 1 for i in range(start, end) if _RETRY_RE.search(lines[i])]
    attempts = []
    for n, (lo, hi) in enumerate(zip([start, *cuts], [*cuts, end]), 1):
        errors = [line.strip()[:200] for line in lines[lo:hi]
                  if _EXCEPTION_RE.match(line.strip()) or _CRASH_RE.search(line) or _RETRY_RE.search(line)]
        text = "\n".join(errors)
        mode = ("metric_threshold" if _THRESHOLD_RE.search(text)
                else _mode(text, False, bool(errors)))
        first = next((e for e in errors if not _RETRY_RE.search(e)), errors[0] if errors else None)
        attempts.append({"attempt": n, "lines": [lo + 1, hi], "metrics": [], "first_error": first, "mode": mode})
    evaluated = [a for a in attempts if a["mode"] == "metric_threshold"]
    if len(evaluated) == len(metrics):
        for attempt, m in zip(evaluated, metrics):
            attempt["metrics"] = [m]
    return attempts


def _summarize_test(lines: list[str], start: int, end: int, case: str | None = None) -> dict:
    """Facts about one test file's section of a job log (line numbers are 1-based).

    Metrics, attempts, mode, and launch config are taken from *case* (default:
    the first failed case) when the log marks test cases, so a run that executed
    more of the file is still compared case for case.
    """
    cases = _failed_cases(lines[start:end])
    bounds = _case_bounds(lines, start, end, case or (cases[0] if cases else ""))
    setup_lines = lines[bounds[0]:bounds[1]] if bounds else lines[start:end]
    if bounds:
        start, end = bounds[1], bounds[2]
    section = lines[start:end]
    metrics, accuracy = [], None
    first_error = None
    for offset, line in enumerate(section):
        m = _ACCURACY_RE.match(line)
        if m:
            accuracy = float(m.group(1))
        m = _INVALID_RE.match(line)
        if m and accuracy is not None:
            metrics.append({"accuracy": accuracy, "invalid": float(m.group(1))})
            accuracy = None
        if first_error is None and (_EXCEPTION_RE.match(line.strip()) or _CRASH_RE.search(line)):
            first_error = start + offset
    text = "\n".join(section)
    mode = _mode(text, bool(metrics), first_error is not None)
    lo = max(start, (first_error if first_error is not None else end) - 15)
    hi = min(end, lo + 60)
    # Backends are selected while the servers start, i.e. in the setup lines.
    runtime_lines = setup_lines + section if bounds else section
    backends = []
    for line in runtime_lines:
        m = _BACKEND_RE.search(line)
        if m:
            entry = _LOG_PREFIX_RE.sub("", m.group(1)).strip()[:160]
            if entry not in backends:
                backends.append(entry)
    return {
        "failed_cases": cases,
        "attempts": 1 + len(_RETRY_RE.findall(text)),
        "attempt_details": _attempts(lines, start, end, metrics),
        "metrics": metrics,
        "mode": mode,
        "excerpt": {"start_line": lo + 1, "end_line": hi,
                    "text": "\n".join(lines[lo:hi])},
        # Class-level servers start before the marker. Otherwise the test starts
        # its own; the case's lines may also hold the next class's servers.
        "launch_configs": (_launch_configs(setup_lines) or _launch_configs(section)) if bounds
        else _launch_configs(section),
        "backend_lines": backends[:15],
        "aiter_modules": sorted(set(_AITER_MODULE_RE.findall("\n".join(runtime_lines)))),
    }


def parse_job_log(log: str) -> dict:
    """Job-level facts plus a summary of every test file the job reported failed."""
    lines = _clean_lines(log)
    # Start at actions/checkout's "Checking out the ref": on a reused runner
    # workspace, earlier "HEAD is now at" lines describe the previous job's
    # checkout. A checkout of a ref (not a SHA) only shows the abbreviated SHA.
    ref_start = next((i for i, line in enumerate(lines) if "Checking out the ref" in line), 0)
    checkout_lines = lines[ref_start:]
    tested = next((m.group(1) for m in map(_CHECKOUT_RE.search, checkout_lines) if m), None) \
        or next((m.group(1) for m in map(_HEAD_RE.search, checkout_lines) if m), None)
    merge = next((m for m in map(_MERGE_RE.search, checkout_lines) if m), None)
    image, image_idx = None, None
    for i, line in enumerate(lines):
        m = _IMAGE_RE.search(line)
        if m:
            image, image_idx = m.group(1), i
            break
    digest = None
    if image_idx is not None:
        digest = next((m.group(1) for m in map(_DIGEST_RE.match, lines[image_idx:]) if m), None)
    gpu = next((m.group(1).lower() for line in lines
                if "GPU" in line and "hostname" in line for m in [_GPU_RE.search(line)] if m), None)
    fingerprints = []
    for m in map(_FINGERPRINT_RE.search, lines):
        if m:
            try:
                fingerprints.append(json.loads(m.group(1)))
            except json.JSONDecodeError:
                pass
    sections = _file_sections(lines)
    failed_files = list(dict.fromkeys(m.group(2) for m in map(_FILE_FAILED_RE.match, lines) if m))
    fast_fail = _FAST_FAIL_RE.search(log)
    return {
        "tested_sha": tested,
        "merge": {"head": merge.group(1), "base": merge.group(2)} if merge else None,
        "image": image, "image_digest": digest, "gpu": gpu, "fingerprints": fingerprints,
        "ran_files": list(sections), "failed_files": failed_files,
        "tests": {path: _summarize_test(lines, *sections.get(path, (max(0, len(lines) - 400), len(lines))))
                  for path in failed_files},
        "cascade_of": fast_fail.group(1).strip() if fast_fail else None,
        "tail": "\n".join(lines[-40:]),
    }


def split_hunks(diff: str) -> list[dict]:
    """Index the unified diff into hunks with line spans (1-based) in ``pr.diff``."""
    hunks, current_file = [], None
    lines = diff.splitlines()
    for i, line in enumerate(lines):
        starts_file = line.startswith("diff --git ")
        starts_hunk = line.startswith("@@") and current_file is not None
        if (starts_file or starts_hunk) and hunks and hunks[-1]["end_line"] is None:
            hunks[-1]["end_line"] = i
        if starts_file:
            current_file = line.split(" b/", 1)[-1]
        elif starts_hunk:
            hunks.append({"file": current_file, "header": line[:160], "start_line": i + 1, "end_line": None})
    if hunks and hunks[-1]["end_line"] is None:
        hunks[-1]["end_line"] = len(lines)
    return hunks


def _exec_env(repo_path: Path) -> dict[str, str]:
    path = repo_path / "scripts" / "ci" / "amd" / "amd_ci_exec.sh"
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            if line.lstrip().startswith("#"):
                continue
            for pattern in _ENV_MAP_RES:
                for m in pattern.finditer(line):
                    env[m.group(1)] = m.group(2)
    return env


# ---------------------------------------------------------------------------
# Evidence pack
# ---------------------------------------------------------------------------

def _test_status(test_file: str | None, job: dict | None, parsed: dict | None) -> str:
    if job is None:
        return "not in run"
    conclusion = job.get("conclusion")
    if test_file is None:
        return conclusion or job.get("status") or "unknown"
    if conclusion == "success":
        return "passed"
    if conclusion not in _FAILED:
        return conclusion or "unknown"
    if parsed is None:
        return "unknown (log unavailable)"
    if test_file in parsed["failed_files"]:
        return "failed"
    if test_file in parsed["ran_files"]:
        return "passed"
    return "not reached"


def _image_line(tag: str | None) -> str | None:
    """The image tag without its build date (``…-mi35x-20261013`` → ``…-mi35x``)."""
    return re.sub(r"-\d{8}(?:[-.].*)?$", "", tag) if tag else None


def _comparable(pr: dict, main: dict) -> dict:
    """How a ``main`` execution compares with the PR's: GPU, image, launch config."""
    def match(a, b):
        return None if not a or not b else a == b
    if pr.get("image_digest") and pr.get("image_digest") == main.get("image_digest"):
        image = "same build"
    elif _image_line(pr.get("image")) and _image_line(main.get("image")):
        image = "same line" if _image_line(pr["image"]) == _image_line(main["image"]) else "different"
    else:
        image = None
    return {
        "gpu": match(pr.get("gpu"), main.get("gpu")),
        "image": image,
        "launch_configs": match(
            sorted(json.dumps(c, sort_keys=True) for c in pr.get("launch_configs") or []),
            sorted(json.dumps(c, sort_keys=True) for c in main.get("launch_configs") or []),
        ),
    }


def build_evidence_pack(
    token: str,
    pr_number: int,
    repo_path: Path,
    head_sha: str | None = None,
    as_of: str | None = None,
    baseline_runs: int = BASELINE_RUNS,
) -> dict:
    """Collect the evidence for PR *pr_number* into ``<repo_path>/.ci-context/``.

    Also checks *repo_path* out at the commit CI actually tested (falling back
    to the PR head, recorded as a gap) so the LLM reads the code that ran.
    """
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    as_of = as_of or now
    ctx = repo_path / ".ci-context"
    logs_dir = ctx / "logs"
    (logs_dir / "main").mkdir(parents=True, exist_ok=True)
    evidence: dict[str, dict] = {}
    gaps: list[dict] = []

    def gap(what: str, why: str) -> None:
        gid = f"G{len(gaps) + 1}"
        gaps.append({"id": gid, "what": what, "why": why})
        evidence[gid] = {"kind": "gap", "what": what, "why": why}

    pr = _retry(_get, token, f"pulls/{pr_number}")
    head_sha = head_sha or pr["head"]["sha"]
    diff, files = _retry(_pr_diff, token, pr["base"]["ref"], head_sha)
    (ctx / "pr.diff").write_text(diff)
    for i, hunk in enumerate(split_hunks(diff), 1):
        evidence[f"D{i}"] = {"kind": "diff", **hunk}

    # Runs for the head commit, each at the attempt that was current at as_of.
    latest: dict[str, dict] = {}
    for run in _paged(token, "actions/runs", "workflow_runs", head_sha=head_sha):
        if run.get("created_at", "") > as_of:
            continue
        key = run.get("path") or run.get("name")
        if key not in latest or run["id"] > latest[key]["id"]:
            latest[key] = run

    runs, failed, gates = [], [], []
    for path, run in sorted(latest.items()):
        workflow_file = Path(path).name
        try:
            run = _attempt_as_of(token, run, as_of)
            attempt = run.get("run_attempt") or 1
            jobs = _paged(token, f"actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs")
        except requests.RequestException as exc:
            gap(f"jobs of `{workflow_file}` run {run['id']}", f"GitHub API error ({exc.__class__.__name__})")
            jobs, attempt = [], run.get("run_attempt") or 1
        runs.append({
            "workflow_file": workflow_file, "run_id": run["id"], "attempt": attempt,
            "event": run.get("event"), "url": run.get("html_url"),
            "status": run.get("status"), "conclusion": run.get("conclusion"),
            "job_counts": dict(Counter(j.get("conclusion") or j.get("status") for j in jobs)),
        })
        for job in jobs:
            if job.get("conclusion") not in _FAILED:
                continue
            entry = {
                "workflow_file": workflow_file, "run_id": run["id"], "attempt": attempt,
                "vendor": "AMD" if "amd" in workflow_file.lower() else "Other",
                "id": job["id"], "name": job["name"], "url": job.get("html_url"),
                "labels": job.get("labels", []), "runner": job.get("runner_name"),
                "steps": job.get("steps", []),
            }
            (gates if is_gate_job(job) or _OPT_IN_GATE_RE.search(job["name"]) else failed).append(entry)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        logs = dict(zip((j["id"] for j in failed),
                        pool.map(lambda j: _retry(download_job_logs, token, j["id"]), failed)))
    parsed_jobs = {}
    for job in failed:
        (logs_dir / f"{job['id']}.log").write_text(logs[job["id"]])
        parsed_jobs[job["id"]] = parse_job_log(logs[job["id"]])

    # Resolve abbreviated tested SHAs against full ones seen in other jobs.
    full = {p["tested_sha"] for p in parsed_jobs.values() if p["tested_sha"] and len(p["tested_sha"]) == 40}
    for p in parsed_jobs.values():
        if p["tested_sha"] and len(p["tested_sha"]) < 40:
            p["tested_sha"] = next((s for s in full if s.startswith(p["tested_sha"])), p["tested_sha"])

    # Check out the commit CI tested, so the LLM reads the code that ran.
    tested = Counter(p["tested_sha"] for p in parsed_jobs.values() if p["tested_sha"] and len(p["tested_sha"]) == 40)
    checkout = {"sha": head_sha, "what": "PR head"}
    if tested:
        sha = tested.most_common(1)[0][0]
        if checkout_commit(repo_path, sha, fetch_ref=f"pull/{pr_number}/head"):
            checkout = {"sha": sha, "what": "commit tested by CI"}
            if len(tested) > 1:
                gap("single tested commit", f"failed jobs tested {len(tested)} different commits; "
                    f"the checkout is the most common one ({sha[:12]})")
        else:
            gap(f"checkout of tested commit {sha[:12]}", "could not fetch it; reading the PR head instead")
    if checkout["what"] == "PR head" and not checkout_commit(repo_path, head_sha, f"pull/{pr_number}/head"):
        raise RuntimeError(f"could not check out PR #{pr_number} at {head_sha[:12]}")
    exec_env = _exec_env(repo_path)

    images: dict[tuple, tuple] = {}
    failures = []
    for job in failed:
        p = parsed_jobs[job["id"]]
        gpu = p["gpu"] or next((m.group(1).lower() for s in [*job["labels"], job["runner"] or ""]
                                for m in [_GPU_RE.search(s)] if m), None)
        if p["image"] and (p["image"], p["image_digest"]) not in images:
            images[(p["image"], p["image_digest"])] = fetch_image_env(p["image"], p["image_digest"])
        image_env, image_env_source = images.get((p["image"], p["image_digest"]), (None, "no image in log"))
        error_lines = extract_error_lines(
            logs[job["id"]], job["steps"], job["run_id"], job["id"],
            {s["name"] for s in job["steps"] if s.get("conclusion") == "failure"} or None)[:3]
        tests = p["tests"] or {None: None}
        for test_file, t in tests.items():
            fid = f"F{len(failures) + 1}"
            env = {
                "kind": "environment",
                "tested_sha": {"value": p["tested_sha"] or head_sha,
                               "source": ("log" if p["tested_sha"] and len(p["tested_sha"]) == 40
                                          else "log (abbreviated)" if p["tested_sha"] else "assumed (PR head)")},
                "merge": p["merge"],
                "image": {"tag": p["image"], "digest": p["image_digest"],
                          "source": "log" if p["image"] else "not in log"},
                "gpu": {"value": gpu, "arch": _gpu_arch(gpu), "source": "log" if p["gpu"] else "runner labels"},
                "image_env": {"source": image_env_source, "values": image_env or {}},
                "exec_env": ({"source": "reconstructed (scripts/ci/amd/amd_ci_exec.sh at checkout)",
                              "values": exec_env} if job["vendor"] == "AMD" and exec_env else None),
                "fingerprints": p["fingerprints"],
            }
            failure = {
                "id": fid, "job": {k: job[k] for k in ("id", "name", "url", "workflow_file", "run_id",
                                                       "attempt", "vendor")},
                "test_file": test_file,
                "cascade_of": p["cascade_of"],
                "failed_steps": [s["name"] for s in job["steps"] if s.get("conclusion") == "failure"],
                "error_lines": [{"text": e["preview"], "url": e["url"]} for e in error_lines],
            }
            if t:
                failure.update(failed_cases=t["failed_cases"], attempts=t["attempts"], mode=t["mode"])
                evidence[f"{fid}.failure"] = {"kind": "failure", "failure_id": fid,
                                              "log": f"logs/{job['id']}.log", **t["excerpt"],
                                              "attempts": t["attempt_details"]}
                evidence[f"{fid}.metrics"] = {"kind": "metrics", "failure_id": fid, "values": t["metrics"]}
                evidence[f"{fid}.launch"] = {"kind": "launch", "failure_id": fid, "configs": t["launch_configs"],
                                             "source": "log"}
                evidence[f"{fid}.markers"] = {"kind": "marker", "failure_id": fid, "source": "log",
                                              "backend_lines": t["backend_lines"],
                                              "aiter_modules": t["aiter_modules"]}
            else:
                failure.update(failed_cases=[], attempts=1, mode="cascade" if p["cascade_of"] else "no_test_failure")
                evidence[f"{fid}.failure"] = {"kind": "failure", "failure_id": fid, "log": f"logs/{job['id']}.log",
                                              "text": p["tail"]}
            evidence[f"{fid}.env"] = {**env, "failure_id": fid}
            failures.append(failure)

    # The same job on main: status, raw metrics, and environment of each run.
    main_runs: dict[str, list[dict]] = {}
    main_jobs: dict[int, list[dict]] = {}
    main_logs: dict[int, dict] = {}
    main_lines: dict[int, list[str]] = {}
    for failure in failures:
        if failure["cascade_of"]:
            continue
        wf = failure["job"]["workflow_file"]
        if wf not in main_runs:
            try:
                main_runs[wf] = _retry(_main_runs, token, wf, as_of, baseline_runs)
            except requests.RequestException as exc:
                main_runs[wf] = []
                gap(f"`main` runs of `{wf}`", f"GitHub API error ({exc.__class__.__name__})")
        matches = []
        for run in main_runs[wf]:
            try:
                if run["id"] not in main_jobs:
                    main_jobs[run["id"]] = _paged(token, f"actions/runs/{run['id']}/jobs", "jobs", filter="latest")
            except requests.RequestException:
                main_jobs[run["id"]] = []
            job = next((j for j in main_jobs[run["id"]] if j["name"] == failure["job"]["name"]), None)
            matches.append((run, job))
        to_fetch = [j["id"] for _, j in matches if j and j.get("conclusion") in (*_FAILED, "success")
                    and j["id"] not in main_logs][:max(0, MAX_BASELINE_LOGS - len(main_logs))]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for job_id, log in zip(to_fetch, pool.map(lambda i: _retry(download_job_logs, token, i), to_fetch)):
                (logs_dir / "main" / f"{job_id}.log").write_text(log)
                main_logs[job_id] = parse_job_log(log)
        pr_env = evidence[f"{failure['id']}.env"]
        pr_facts = {"gpu": pr_env["gpu"]["value"], "image": pr_env["image"]["tag"],
                    "image_digest": pr_env["image"]["digest"],
                    "launch_configs": evidence.get(f"{failure['id']}.launch", {}).get("configs")}
        case = (failure.get("failed_cases") or [None])[0]
        for k, (run, job) in enumerate(matches, 1):
            parsed = main_logs.get(job["id"]) if job else None
            t, status = None, _test_status(failure["test_file"], job, parsed)
            if parsed and failure["test_file"] in parsed["ran_files"]:
                if job["id"] not in main_lines:
                    main_lines[job["id"]] = _clean_lines((logs_dir / "main" / f"{job['id']}.log").read_text())
                lines = main_lines[job["id"]]
                section = _file_sections(lines)[failure["test_file"]]
                t = _summarize_test(lines, *section, case=case)
                if case and _case_bounds(lines, *section, case):
                    status = "failed" if case in t["failed_cases"] else "passed"
            main_gpu = (parsed or {}).get("gpu") or (next(
                (m.group(1).lower() for s in (job or {}).get("labels", []) for m in [_GPU_RE.search(s)] if m), None))
            facts = {"gpu": main_gpu, "image": (parsed or {}).get("image"),
                     "image_digest": (parsed or {}).get("image_digest"),
                     "launch_configs": (t or {}).get("launch_configs")}
            evidence[f"{failure['id']}.M{k}"] = {
                "kind": "main_record", "failure_id": failure["id"],
                "run_id": run["id"], "run_url": run["html_url"], "created_at": run["created_at"],
                "tested_sha": (parsed or {}).get("tested_sha") or run["head_sha"],
                "job_url": (job or {}).get("html_url"),
                "job_result": (job.get("conclusion") or job.get("status")) if job else "not in run",
                "test_result": status,
                "mode": (t or {}).get("mode") if status == "failed" else None,
                "metrics": (t or {}).get("metrics", []),
                "attempts": (t or {}).get("attempt_details", []) if status == "failed" else [],
                "excerpt": ((t or {}).get("excerpt") or {}).get("text", "")[-1500:] if status == "failed" else "",
                "image": (parsed or {}).get("image"), "image_digest": facts["image_digest"],
                "gpu": main_gpu, "launch_configs": facts["launch_configs"],
                "comparable": _comparable(pr_facts, facts),
            }
        if not matches:
            gap(f"`main` baseline for {failure['id']}", f"no completed non-PR runs of `{wf}` before {as_of}")

    pack = {
        "schema_version": SCHEMA_VERSION,
        "request": {"pr": pr_number, "head_sha": head_sha, "as_of": as_of, "collected_at": now,
                    "baseline_runs": baseline_runs},
        "pr": {"title": pr["title"], "author": pr["user"]["login"], "url": pr["html_url"],
               "base": pr["base"]["ref"], "current_head": pr["head"]["sha"],
               "files": [{"file": f["filename"], "status": f.get("status"),
                          "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)} for f in files]},
        "checkout": checkout,
        "runs": runs,
        "gates": [{k: g[k] for k in ("name", "url", "workflow_file", "run_id")} for g in gates],
        "failures": failures,
        "evidence": evidence,
        "gaps": gaps,
    }
    (ctx / "evidence.json").write_text(json.dumps(pack, indent=1))
    (ctx / "evidence.md").write_text(render_evidence_md(pack))
    return pack


# ---------------------------------------------------------------------------
# Rendering for the LLM
# ---------------------------------------------------------------------------

def _fmt_metrics(values: list[dict]) -> str:
    return "; ".join(", ".join(f"{k} {v:.3f}" for k, v in m.items()) for m in values) or "—"


def _flag(value) -> str:
    return {True: "yes", False: "**no**", None: "unknown"}[value]


def render_evidence_md(pack: dict) -> str:
    """Readable view of the pack; every fact is labelled with its evidence ID."""
    ev, req, pr = pack["evidence"], pack["request"], pack["pr"]
    out = [
        f"# Evidence pack — PR #{req['pr']}",
        "",
        f"- PR [#{req['pr']}]({pr['url']}) \"{pr['title']}\" by {pr['author']}, base `{pr['base']}`",
        f"- PR head under analysis: `{req['head_sha']}`"
        + ("" if req["head_sha"] == pr["current_head"] else f" (the PR head is now `{pr['current_head'][:12]}`)"),
        f"- Current directory is checked out at `{pack['checkout']['sha']}` ({pack['checkout']['what']})",
        f"- As of {req['as_of']}; collected {req['collected_at']}. Diff: `pr.diff`; logs: `logs/`, `logs/main/`.",
        "",
        "## Workflow runs for the PR head",
        "",
        "| Workflow | Run | Attempt | Event | Result | Jobs |",
        "|---|---|---|---|---|---|",
    ]
    for r in pack["runs"]:
        counts = ", ".join(f"{k}: {v}" for k, v in sorted(r["job_counts"].items(), key=str)) or "—"
        out.append(f"| `{r['workflow_file']}` | [{r['run_id']}]({r['url']}) | {r['attempt']} | {r['event']} | "
                   f"{r['conclusion'] or r['status']} | {counts} |")
    if pack["gates"]:
        out += ["", "Gate / coordinator / opt-in jobs that failed (not test failures): "
                + ", ".join(f"[{g['name']}]({g['url']})" for g in pack["gates"])]
    out += ["", "## Failures", ""]
    for f in pack["failures"]:
        fid, env = f["id"], ev[f"{f['id']}.env"]
        out.append(f"### {fid} — [{f['job']['name']}]({f['job']['url']}) ({f['job']['vendor']})")
        out.append("")
        if f["cascade_of"]:
            out += [f"- **Cascade:** failed only because of `{f['cascade_of']}`; it needs no verdict of its own.", ""]
            continue
        out.append(f"- Test: `{f['test_file'] or 'none (no failed test file in the log)'}`"
                   + (f" — cases: {', '.join(f'`{c}`' for c in f['failed_cases'])}" if f["failed_cases"] else "")
                   + f" · mode: `{f['mode']}` · attempts: {f['attempts']}")
        fail = ev[f"{fid}.failure"]
        span = f"lines {fail['start_line']}–{fail['end_line']} of `{fail['log']}`" if "start_line" in fail \
            else f"log tail, `{fail['log']}`"
        out.append(f"- [{fid}.failure] {span}:")
        out += ["", "```", (fail.get("text") or "")[-3000:], "```", ""]
        for a in fail.get("attempts", []):
            out.append(f"  - attempt {a['attempt']} (lines {a['lines'][0]}–{a['lines'][1]}): `{a['mode']}`"
                       + (f", {_fmt_metrics(a['metrics'])}" if a["metrics"] else ", no eval metrics")
                       + (f", first error `{a['first_error']}`" if a["first_error"] else ""))
        if f"{fid}.metrics" in ev:
            out.append(f"- [{fid}.metrics] {_fmt_metrics(ev[f'{fid}.metrics']['values'])}")
        merge = env.get("merge")
        out.append(f"- [{fid}.env] tested commit `{env['tested_sha']['value'][:12]}` ({env['tested_sha']['source']})"
                   + (f", a merge of PR head `{merge['head'][:12]}` into `{merge['base'][:12]}`" if merge else "")
                   + f"; image `{env['image']['tag']}`"
                   + (f" @ `{env['image']['digest'][:19]}…` (log)" if env["image"]["digest"] else " (no digest in log)")
                   + f"; GPU {env['gpu']['value']} / {env['gpu']['arch']} ({env['gpu']['source']})")
        img = env["image_env"]
        if img["values"]:
            out.append(f"  - image ENV ({img['source']}): "
                       + ", ".join(f"`{k}={v}`" for k, v in sorted(img["values"].items())))
        else:
            out.append(f"  - image ENV: {img['source']}")
        if env.get("exec_env"):
            out.append(f"  - exec env ({env['exec_env']['source']}): "
                       + ", ".join(f"`{k}={v}`" for k, v in env["exec_env"]["values"].items()))
        if env["fingerprints"]:
            out.append(f"  - CI fingerprint: `{json.dumps(env['fingerprints'])[:1500]}`")
        if f"{fid}.launch" in ev:
            configs = ev[f"{fid}.launch"]["configs"]
            out.append(f"- [{fid}.launch] " + ("; ".join(json.dumps(c) for c in configs) or "no `server_args` lines"))
        if f"{fid}.markers" in ev:
            mk = ev[f"{fid}.markers"]
            out.append(f"- [{fid}.markers] backend lines: "
                       + ("; ".join(f"`{b}`" for b in mk["backend_lines"]) or "none")
                       + " · AITER modules: " + (", ".join(f"`{m}`" for m in mk["aiter_modules"]) or "none"))
        records = [(k, v) for k, v in ev.items() if v.get("failure_id") == fid and v["kind"] == "main_record"]
        if records:
            out += ["", "Same job on `main` (newest first). Same GPU / image (same build, same line = same tag "
                    "without date, or different) / same launch config:", "",
                    "| ID | Run | Created | Tested | Job | Test | Mode | Metrics | GPU | Image | Launch |",
                    "|---|---|---|---|---|---|---|---|---|---|---|"]
            for rid, r in records:
                c = r["comparable"]
                out.append(f"| {rid} | [{r['run_id']}]({r['job_url'] or r['run_url']}) | {r['created_at'][:16]} | "
                           f"`{(r['tested_sha'] or '')[:10]}` | {r['job_result']} | {r['test_result']} | "
                           f"{r['mode'] or '—'} | {_fmt_metrics(r['metrics'])} | {_flag(c['gpu'])} | "
                           f"{c['image'] or 'unknown'} | {_flag(c['launch_configs'])} |")
            failing = [(rid, r) for rid, r in records if r["excerpt"]]
            for rid, r in failing[:3]:
                attempts = "; ".join(f"attempt {a['attempt']}: `{a['mode']}`"
                                     + (f" {_fmt_metrics(a['metrics'])}" if a["metrics"] else "")
                                     + (f", `{a['first_error']}`" if a["first_error"] else "")
                                     for a in r.get("attempts", []))
                out += ["", f"{rid} attempts: {attempts or '—'}", "```", r["excerpt"][-1200:], "```"]
        out.append("")
    hunks = [(k, v) for k, v in ev.items() if v["kind"] == "diff"]
    out += ["## Diff hunks (`pr.diff`)", ""]
    out += [f"- [{k}] `{h['file']}` {h['header']} (lines {h['start_line']}–{h['end_line']})" for k, h in hunks[:300]]
    if len(hunks) > 300:
        out.append(f"- … {len(hunks) - 300} more hunks in `pr.diff`")
    if pack["gaps"]:
        out += ["", "## Gaps", ""] + [f"- [{g['id']}] {g['what']}: {g['why']}" for g in pack["gaps"]]
    return "\n".join(out) + "\n"
