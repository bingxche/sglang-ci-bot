#!/usr/bin/env python3
"""Fix AMD scheduled-CI failures on sglang ``main`` and open draft PRs (ci-autofix).

Runs after every CI-monitor tick (``ci-autofix.yml``). Each problem in
today's daily report (a cluster ``R…`` of failed jobs) is checked in code
(``build_plan()``); a fix is tried only when all of these hold:

  - it failed in a scheduled run on ``main`` of an ``AUTOFIX_WORKFLOWS`` workflow;
  - it fails for sure: every one of its tests failed in the last two
    completed scheduled runs of its job, retries included (``fails_for_sure()``);
  - no error matches ``INFRA_PATTERNS`` and it is not a performance regression;
  - nobody has a fix: no fix PR in the report, and no open or merged sglang
    PR since the first failure names the test file (``find_fix_pr()``);
  - one dispatch can re-run all of its tests (``recipe_for()``);
  - the bot tried it fewer than ``MAX_ATTEMPTS`` times.

At most ``AUTOFIX_MAX_PER_HOUR`` problems start in any hour, the most failed
jobs first (``take_for_this_hour()``). The rest wait for a later tick, and
amd-bot @-mentions ``NOTIFY_USER`` on the daily issue the first time a
problem waits.

Each tick is two jobs. ``fix`` (``fix_phase()``) holds only a read token:
  1. the ``CI Autofix`` agent fixes the problem in a worktree of ``main``
     (pinned Opus 5.5 1M, effort max, no token, a tool allowlist);
  2. code checks the patch and the PR text (``gate_patch()``,
     ``check_pr_text()``), then the read-only ``CI Autofix Review`` agent
     judges it; a rejection goes back to the fix agent once.
``publish`` (``publish_phase()``) runs no agent and does every write:
  3. it commits the patch as the owner of ``AUTOFIX_TOKEN``, pushes
     ``bingxche/autofix-<id>-<slug>`` to sgl-project/sglang and dispatches
     the failed job on that branch;
  4. on later ticks it reads the run (``verify_run()``): every test of the
     problem must PASS, without a retry, on the same workflow, job, ROCm
     version and runner. Then it opens a draft PR. A failed run gives the
     agent a second attempt; after ``MAX_ATTEMPTS`` the branch is deleted.

State lives in one comment on the daily issue (``LOG_MARKER``) and is carried
over from the latest earlier day; the fix job hands it to the publish job in
an artifact. ``AUTOFIX_MODE``: ``off`` (the default: no reads, no writes) or
``live``. ``--smoke-*`` runs the whole chain for one past problem without
opening a PR or writing the daily issue (E3 in the spec).
"""

import argparse
import base64
import concurrent.futures
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_cross_workflow_summary import (
    collect_workflow_analyses,
    extract_summary_from_body,
    parse_job_cluster_comment,
    parse_suspect_prs_comment,
)
from failure_tracker import deterministic_rows
from monitor_ci import get_issue_comments
from utils import (
    REPO,
    SGLANG_REPO_PATH,
    ModelUnavailable,
    claude_code_analyze,
    collect_ci_runtime_env,
    create_agent_worktree,
    download_job_logs,
    ensure_sglang_repo,
    get_issue,
    get_pr_diff,
    get_run_jobs,
    gh_get,
    gh_headers,
    post_comment,
    remove_agent_worktree,
    update_comment,
)

log = logging.getLogger("autofix")

MODES = ("off", "live")
DEFAULT_MODE = "off"
DEFAULT_MAX_PER_HOUR = 3
MAX_ATTEMPTS = 2
# The only person the queue notice @-mentions: the owner of the switch (R2).
NOTIFY_USER = "bingxche"
# Every branch the bot pushes, dispatches on or deletes starts with this.
BRANCH_PREFIX = "bingxche/autofix-"
API = f"https://api.github.com/repos/{REPO}"

# Agent budgets: effort max is slow, so these are generous (R9).
AGENT_TIMEOUT_SECS = int(os.environ.get("AUTOFIX_AGENT_TIMEOUT_SECS", "7200"))
AGENT_MAX_TURNS = int(os.environ.get("AUTOFIX_AGENT_MAX_TURNS", "400"))
REVIEW_TIMEOUT_SECS = int(os.environ.get("AUTOFIX_REVIEW_TIMEOUT_SECS", "3600"))
REVIEW_MAX_TURNS = int(os.environ.get("AUTOFIX_REVIEW_MAX_TURNS", "150"))
# A dispatched run that has not finished its jobs by then is given up.
DISPATCH_TIMEOUT_HOURS = 24
# Scheduled runs read back per job when checking that a failure is not flaky.
HISTORY_RUNS = 6
# Open PRs whose files are scanned for the test file, newest first.
MAX_OPEN_PRS_SCANNED = 300
# Words the PR's prose may have (links and code blocks not counted).
MAX_BODY_WORDS = 250

LOG_MARKER = "<!-- ci-autofix-log -->"
STATE_PREFIX = "<!-- ci-autofix-state:v1 "
QUEUE_NOTICE_MARKER = "<!-- ci-autofix-queue-notice -->"
PR_MARKER = "<!-- amd-bot-autofix: {problem} -->"
PR_DATA_PREFIX = "<!-- amd-bot-autofix-data:v1 "
_PR_MARKER_RE = re.compile(r"<!-- amd-bot-autofix: (R\d+) -->")

# ---------------------------------------------------------------------------
# Workflows and dispatch recipes
# ---------------------------------------------------------------------------

PR_TEST_WORKFLOW = "pr-test-amd.yml"
PR_TEST_EXTRA_WORKFLOW = "pr-test-amd-extra.yml"
NIGHTLY_WORKFLOW = "nightly-test-amd.yml"
DISAGG_WORKFLOW = "nightly-amd-mi355x-disagg.yml"
# Workflows whose scheduled failures the bot fixes.
AUTOFIX_WORKFLOWS = (PR_TEST_WORKFLOW, NIGHTLY_WORKFLOW, DISAGG_WORKFLOW)
# Monitored workflows the bot leaves alone, and why.
NOT_FIXED = {
    "nightly-test-amd-miles-rocm10.yml": "tests the sglang inside the Miles image, not a branch",
    "nightly-test-amd-miles-rocm10-mi30x.yml": "tests the sglang inside the Miles image, not a branch",
    "amd-aiter-scout.yml": "tests AITER main; not a scheduled failure of sglang main",
}
# The pr-test-amd.yml job that calls pr-test-amd-extra.yml on schedules.
_EXTRA_CALLER = "call-pr-test-amd-extra"
_ROCM_RE = re.compile(r"^rocm\d+$")
_ARCH_RE = re.compile(r"^linux-(mi300|mi325)-")


@dataclass(frozen=True)
class JobName:
    caller: str   # reusable-workflow caller, e.g. "call-pr-test-amd-extra"
    base: str     # job id, e.g. "stage-b-test-1-gpu-small-amd"
    rocm: str
    runner: str   # runner label in the name, e.g. "linux-mi300-1gpu-sglang"
    params: tuple[str, ...]


def parse_job_name(name: str) -> JobName:
    """``call-x (rocm10) / stage-b (rocm10, linux-mi300-1gpu-sglang, 3)`` → its parts."""
    parts = [p.strip() for p in (name or "").split(" / ")]
    last = parts[-1]
    m = re.match(r"^([\w.-]+)(?: \((.*?)\)?)?(?: part \d+)?$", last)
    base = m.group(1) if m else last
    params = tuple(p.strip() for p in (m.group(2) or "").split(",") if p.strip()) if m else ()
    caller = parts[0].split(" (")[0] if len(parts) > 1 else ""
    rocm = next((p for p in params if _ROCM_RE.match(p)), "")
    runner = next((p for p in params if p.startswith("linux-")), "")
    return JobName(caller, base, rocm, runner, params)


def job_family(name: str) -> str:
    """The job without its partition: two runs of it are comparable."""
    j = parse_job_name(name)
    if j.base.startswith("nightly-mi355x-"):  # disagg: the config is the job
        return f"{j.base}|{j.params[0] if j.params else ''}"
    return f"{j.base}|{j.rocm}|{j.runner}"


@dataclass(frozen=True)
class Recipe:
    workflow: str        # workflow file to dispatch
    group: tuple         # recipes with equal groups share one dispatch
    target: str          # job id or config the dispatch selects ("" = all)
    select_input: str    # input that takes the comma-joined targets
    inputs: tuple        # other inputs, as (name, value) pairs


def recipe_for(workflow: str, job_name: str, workflow_yaml) -> tuple[Recipe | None, str]:
    """How to re-run *job_name* of *workflow* on a branch, or ``(None, why not)``.

    *workflow_yaml* maps a workflow file to its text on the branch's base; a
    target counts only if that workflow selects jobs by it.
    """
    if workflow in NOT_FIXED:
        return None, NOT_FIXED[workflow]
    if workflow not in AUTOFIX_WORKFLOWS:
        return None, f"{workflow} is not an autofix workflow"
    j = parse_job_name(job_name)
    arch_m = _ARCH_RE.match(j.runner)
    arch = (("runner_arch", arch_m.group(1)),) if arch_m else ()

    if workflow == PR_TEST_WORKFLOW and j.caller == _EXTRA_CALLER:
        text = workflow_yaml(PR_TEST_EXTRA_WORKFLOW) or ""
        if "workflow_dispatch:" not in text or not re.search(rf"^  {re.escape(j.base)}:\s*$", text, re.M):
            return None, f"{PR_TEST_EXTRA_WORKFLOW} cannot run {j.base} by hand"
        inputs = (("rocm_version", j.rocm), *arch, ("continue_on_error", "true"))
        return Recipe(PR_TEST_EXTRA_WORKFLOW, (PR_TEST_EXTRA_WORKFLOW, inputs), "", "", inputs), ""

    text = workflow_yaml(workflow) or ""
    if workflow == DISAGG_WORKFLOW:
        if not j.params or "configs:" not in text:
            return None, f"no config in {job_name!r}"
        return Recipe(DISAGG_WORKFLOW, (DISAGG_WORKFLOW,), j.params[0], "configs", ()), ""
    if not j.rocm:
        return None, f"no ROCm version in {job_name!r}"
    # Both workflows gate each job on `contains(format(',{0},', <input>), ',<job>,')`.
    if f",{j.base}," not in text:
        return None, f"{workflow} cannot select {j.base} by hand"
    if workflow == PR_TEST_WORKFLOW:
        inputs = (("rocm_version", j.rocm), *arch, ("continue_on_error", "true"))
        return Recipe(workflow, (workflow, inputs), j.base, "target_stage", inputs), ""
    inputs = (("rocm_version", j.rocm), ("continue_on_error", "true"))
    return Recipe(workflow, (workflow, inputs), j.base, "job_filter", inputs), ""


def dispatch_inputs(recipes: list[Recipe]) -> dict:
    """The inputs of one dispatch covering *recipes* (all of one group)."""
    inputs = dict(recipes[0].inputs)
    if recipes[0].select_input:
        inputs[recipes[0].select_input] = ",".join(dict.fromkeys(r.target for r in recipes))
    return inputs


# ---------------------------------------------------------------------------
# What the bot does not fix
# ---------------------------------------------------------------------------

# (kind, pattern): errors that are infrastructure, for people to handle.
INFRA_PATTERNS = (
    ("runner lost", r"lost communication with the server|runner has received a shutdown signal"),
    ("disk full", r"No space left on device|\bENOSPC\b|disk quota exceeded"),
    ("GPU unavailable or broken",
     r"hipErrorNoDevice|no ROCm-capable device|amdgpu: GPU (?:reset|hang)|GPU \d+ .*is faulty"),
    ("Hugging Face download",
     r"401 Client Error|Unauthorized for url: https://huggingface|GatedRepoError|RepositoryNotFoundError"
     r"|LocalEntryNotFoundError|couldn't connect to 'https://huggingface\.co'|HfHubHTTPError"),
    ("network",
     r"(?:Read|Connect)Timeout(?:Error)?\b|Connection (?:reset by peer|refused|timed out)"
     r"|Temporary failure in name resolution|Could not resolve host|TLS handshake timeout"),
    ("image pull or build",
     r"failed to (?:pull|resolve) image|manifest unknown|toomanyrequests|pull access denied"
     r"|Error response from daemon|ERROR: failed to solve"),
)
_INFRA_RES = tuple((kind, re.compile(p, re.IGNORECASE)) for kind, p in INFRA_PATTERNS)
_PERF_RE = re.compile(
    r"\b(?:throughput|latency|tok(?:ens)?/s|ttft|tpot|itl|speedup|perf(?:ormance)? regression)\b",
    re.IGNORECASE,
)


def classify_infra(texts: list[str]) -> str | None:
    """The infrastructure kind the error lines match, or None."""
    for text in texts:
        for kind, pattern in _INFRA_RES:
            if pattern.search(text or ""):
                return kind
    return None


def is_perf(job_names: list[str], errors: list[str]) -> bool:
    if any(re.search(r"\bperf\b|-perf-|benchmark", n or "") for n in job_names):
        return True
    return any(_PERF_RE.search(e or "") for e in errors)


# ---------------------------------------------------------------------------
# Reading test results from a job log
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z ?")
_BEGIN_RE = re.compile(r"^Begin \(\d+/\d+\):\s*$")
_FILE_LINE_RE = re.compile(r"^python3 (\S+\.py)\b")
_SUMMARY_RE = re.compile(r"^Test Summary: ")
_RETRY_RE = re.compile(r"^\[CI Retry\] Attempt \d+/\d+ for (\S+)|^✓ PASSED on retry \(attempt \d+\): (\S+)")
# unittest: "FAIL: test_x (__main__.TestX.test_x)", optionally a subtest suffix.
_UNIT_FAIL_RE = re.compile(r"^(FAIL|ERROR): (\w+) \(([\w.]+)\)")
# unittest -v: "test_x (__main__.TestX.test_x) ... ok", the status maybe on a later line.
_UNIT_VERBOSE_RE = re.compile(r"^(\w+) \(([\w.]+)\)(?: \(.*?\))? \.\.\. ?(.*)$")
_UNIT_STATUS = {"ok": "passed", "FAIL": "failed", "ERROR": "failed", "skipped": "skipped",
                "expected failure": "skipped", "unexpected success": "failed"}
# pytest -v: "path.py::Cls::test[p] PASSED", or the status on a later line.
_PYTEST_RE = re.compile(r"^(?:(PASSED|FAILED|ERROR|SKIPPED) )?(\S+\.py)::(\S+?)(?: (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS))?(?: |$)")
_PYTEST_STATUS = {"PASSED": "passed", "FAILED": "failed", "ERROR": "failed", "SKIPPED": "skipped",
                  "XFAIL": "skipped", "XPASS": "failed"}
_STANDALONE_STATUS_RE = re.compile(r"^(ok|FAIL|ERROR|skipped\b.*|PASSED|FAILED|SKIPPED)\s*$")


def clean_log(raw: str) -> list[str]:
    return [_TS_RE.sub("", line.rstrip("\r")) for line in (raw or "").splitlines()]


@dataclass
class Segment:
    file: str | None
    lines: list[str] = field(default_factory=list)


def file_segments(lines: list[str]) -> list[Segment]:
    """Split a run_suite log into one segment per file attempt (``Begin (i/n):``)."""
    segments: list[Segment] = []
    current: Segment | None = None
    for i, line in enumerate(lines):
        if _BEGIN_RE.match(line.strip()):
            path = next((m.group(1) for m in (_FILE_LINE_RE.match(n.strip()) for n in lines[i + 1:i + 3]) if m), None)
            current = Segment(path)
            segments.append(current)
            continue
        if _SUMMARY_RE.match(line.strip()):
            current = None
        if current is not None:
            current.lines.append(line)
    return segments


def same_file(a: str, b: str) -> bool:
    """``/sglang-checkout/test/registered/x/test_y.py`` is ``test/registered/x/test_y.py``."""
    pa = [p for p in (a or "").replace("\\", "/").split("/") if p]
    pb = [p for p in (b or "").replace("\\", "/").split("/") if p]
    if not pa or not pb:
        return False
    n = min(len(pa), len(pb))
    return pa[-n:] == pb[-n:]


def _split_func(want: str) -> tuple[str, str, str]:
    """``TestX.test_y[p]`` → (``TestX``, ``test_y``, ``[p]``)."""
    m = re.match(r"^(.*?)(\[.*\])?$", (want or "").strip().strip("`"))
    head, param = m.group(1), m.group(2) or ""
    parts = [p for p in re.split(r"::|\.", head) if p]
    name = parts[-1] if parts else ""
    cls = parts[-2] if len(parts) > 1 else ""
    return cls, name, param


def _func_matches(want: str, cls: str, name: str, param: str = "") -> bool:
    wcls, wname, wparam = _split_func(want)
    return bool(wname) and name == wname and (not wcls or cls == wcls) and (not wparam or param == wparam)


def _unittest_ids(inner: str, name: str) -> str:
    """The class of ``(__main__.TestX.test_y)`` or ``(__main__.TestX)``."""
    comps = inner.split(".")
    if comps[-1] == name and len(comps) > 1:
        return comps[-2]
    return comps[-1]


def func_status(lines: list[str], want: str) -> str | None:
    """``passed`` / ``failed`` / ``skipped`` for test function *want*, or None when the lines never name it."""
    statuses: list[str] = []
    pending: tuple[str, str, str] | None = None
    for raw in lines:
        line = raw.strip()
        m = _UNIT_FAIL_RE.match(line)
        if m:
            if _func_matches(want, _unittest_ids(m.group(3), m.group(2)), m.group(2)):
                statuses.append("failed")
            continue
        m = _UNIT_VERBOSE_RE.match(line)
        if m:
            cls = _unittest_ids(m.group(2), m.group(1))
            status = m.group(3).strip()
            key = next((k for k in _UNIT_STATUS if status.startswith(k)), None) if status else None
            if key:
                if _func_matches(want, cls, m.group(1)):
                    statuses.append(_UNIT_STATUS[key])
                pending = None
            else:
                pending = (cls, m.group(1), "")
            continue
        m = _PYTEST_RE.match(line)
        if m:
            cls_name = m.group(3).split("::")
            cls = cls_name[-2] if len(cls_name) > 1 else ""
            pm = re.match(r"^(.*?)(\[.*\])?$", cls_name[-1])
            status = m.group(1) or m.group(4)
            if status:
                if _func_matches(want, cls, pm.group(1), pm.group(2) or ""):
                    statuses.append(_PYTEST_STATUS[status])
                pending = None
            else:
                pending = (cls, pm.group(1), pm.group(2) or "")
            continue
        m = _STANDALONE_STATUS_RE.match(line)
        if m and pending:
            word = m.group(1).split()[0]
            status = _UNIT_STATUS.get(word) or _PYTEST_STATUS.get(word)
            if status and _func_matches(want, *pending):
                statuses.append(status)
            pending = None
    if not statuses:
        return None
    if "failed" in statuses:
        return "failed"
    return "skipped" if all(s == "skipped" for s in statuses) else "passed"


def _segment_passed(seg: Segment) -> bool | None:
    """Whether the file attempt ended in ``OK`` (True), ``FAILED (`` (False), or neither (None)."""
    for line in reversed(seg.lines):
        s = line.strip()
        if s == "OK" or s.startswith("OK ("):
            return True
        if s.startswith("FAILED (") or s.startswith("✗ FAILED:") or s.startswith("✗ TIMEOUT:"):
            return False
        if re.match(r"^=+ .*\b\d+ (?:passed|failed)\b.* =+$", s):
            return " failed" not in s and " error" not in s
    return None


@dataclass
class Outcome:
    status: str           # passed / failed / skipped / not-run / unknown
    retried: bool = False
    note: str = ""


def test_outcome(raw_log: str, test_file: str, test_func: str) -> Outcome:
    """How *test_func* of *test_file* ended in one job log.

    The last attempt of the file decides; more than one attempt (a run_suite
    retry, or the workflow running the suite twice) sets ``retried``. A file
    that passed with no line naming the function counts as passed only when
    nothing in it was skipped.
    """
    lines = clean_log(raw_log)
    segments = file_segments(lines)
    mine = [s for s in segments if s.file and same_file(s.file, test_file)]
    if segments and not mine:
        return Outcome("not-run", note=f"{test_file} did not run in this job")
    if not segments:
        mine = [Segment(None, lines)]
    retried = len(mine) > 1 or any(
        (m := _RETRY_RE.match(line.strip())) and same_file(m.group(1) or m.group(2), test_file)
        for line in lines
    )
    last = mine[-1]
    status = func_status(last.lines, test_func)
    if status:
        return Outcome(status, retried)
    _, name, _ = _split_func(test_func)
    passed = _segment_passed(last)
    if passed and name in ("setUpClass", "setUpModule"):
        return Outcome("passed", retried)
    if passed and not any("skipped=" in line for line in last.lines):
        return Outcome("passed", retried, note="file passed; the function is not named in the log")
    if passed:
        return Outcome("unknown", retried, note="file passed with skipped tests; this one may be among them")
    return Outcome("unknown", retried, note=f"no result for {test_func} in the log")


def unrelated_failures(raw_log: str, tests: list[tuple[str, str]]) -> list[str]:
    """Files that failed in the job other than the problem's own."""
    failed = []
    for line in clean_log(raw_log):
        m = re.match(r"^✗ (?:FAILED|TIMEOUT): (\S+\.py)", line.strip())
        if m and not any(same_file(m.group(1), f) for f, _ in tests):
            failed.append(m.group(1).split("/sglang-checkout/")[-1])
    return list(dict.fromkeys(failed))


def segment_text(raw_log: str, test_file: str, max_lines: int = 300) -> str:
    """The tail of the last attempt of *test_file* in the log (for the agent and the PR)."""
    mine = [s for s in file_segments(clean_log(raw_log)) if s.file and same_file(s.file, test_file)]
    if not mine:
        return ""
    return "\n".join(mine[-1].lines[-max_lines:])


# ---------------------------------------------------------------------------
# Is it a real, unfixed failure?
# ---------------------------------------------------------------------------

def fails_for_sure(history: list[str]) -> bool:
    """*history*: the test's status in the latest completed scheduled runs, newest first."""
    return len(history) >= 2 and history[0] == "failed" and history[1] == "failed"


def failure_window(history: list[tuple[str, dict]]) -> tuple[dict | None, dict | None, int]:
    """(first failing run, last passing run, failing runs in a row) from *history*, newest first."""
    first_fail, last_pass, streak = None, None, 0
    for status, run in history:
        if status == "failed":
            first_fail, streak = run, streak + 1
            continue
        if status == "passed":
            last_pass = run
        break
    return first_fail, last_pass, streak


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


# ---------------------------------------------------------------------------
# Code gates on the patch
# ---------------------------------------------------------------------------

FORBIDDEN_PATHS = (re.compile(r"^docker/"), re.compile(r"^python/pyproject[^/]*\.toml$"),
                   re.compile(r"(^|/)CODEOWNERS$"))
_TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$")
_SKIP_RE = re.compile(
    r"\bunittest\.skip|\bskipIf\b|\bskipUnless\b|\bpytest\.mark\.skip|\bpytest\.skip\(|\bxfail\b"
    r"|\bskip_tests\b|\bSkipTest\b|\bself\.skipTest\(|@skip\b"
)
_HIP_RE = re.compile(r"\bis_hip\(\)|\b_is_hip\b|\bis_rocm\(|torch\.version\.hip")
_SUITE_RE = re.compile(r"register_\w*_ci\(|\bsuite\s*=|nightly")


def diff_files(diff: str) -> dict[str, dict[str, list[str]]]:
    """``{path: {"added": [...], "removed": [...]}}`` of a unified diff."""
    files: dict[str, dict[str, list[str]]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
            current = files.setdefault(m.group(2) if m else line, {"added": [], "removed": []})
        elif current is None or line.startswith(("+++", "---")):
            continue
        elif line.startswith("+"):
            current["added"].append(line[1:])
        elif line.startswith("-"):
            current["removed"].append(line[1:])
    return files


def workaround_signals(diff: str) -> list[str]:
    """Why the patch looks like a workaround (skip, xfail, a looser threshold, a moved test)."""
    signals = []
    for path, change in diff_files(diff).items():
        is_test = bool(_TEST_PATH_RE.search(path))
        for line in change["added"]:
            if _SKIP_RE.search(line):
                signals.append(f"{path}: adds a skip/xfail")
                break
        if is_test and any(_HIP_RE.search(line) for line in change["added"]):
            signals.append(f"{path}: adds a ROCm check to a test")
        if is_test:
            shape = lambda s: re.sub(r"\d+(?:\.\d+)?(?:e-?\d+)?", "N", s.strip())  # noqa: E731
            removed = {shape(r): r for r in change["removed"] if re.search(r"\d", r)}
            if any(shape(a) in removed and removed[shape(a)].strip() != a.strip()
                   for a in change["added"] if re.search(r"\d", a)):
                signals.append(f"{path}: changes a number in a test (threshold?)")
            if any(_SUITE_RE.search(r) for r in change["removed"]):
                signals.append(f"{path}: changes which CI suite runs a test")
    return list(dict.fromkeys(signals))


def gate_patch(diff: str, fix: dict) -> list[str]:
    """Code checks on the patch before it goes anywhere; [] when it passes."""
    problems = []
    files = list(diff_files(diff))
    if not files:
        return ["the agent changed nothing"]
    forbidden = [f for f in files if any(p.search(f) for p in FORBIDDEN_PATHS)]
    if forbidden:
        problems.append(f"touches paths autofix may not change: {', '.join(forbidden)}")
    if any(f.startswith(".ci-context/") for f in files):
        problems.append("includes .ci-context files")
    signals = workaround_signals(diff)
    if signals and fix.get("fix_kind") != "workaround":
        problems.append("looks like a workaround (" + "; ".join(signals) + ") but fix_kind is "
                        f"{fix.get('fix_kind')!r}")
    return problems


# ---------------------------------------------------------------------------
# The agent's report and the PR text
# ---------------------------------------------------------------------------

PR_TYPES = ("Feat", "Fix", "Refactor", "Perf", "Doc", "Test", "CI", "Chore")
# sgl-project/sglang .github/workflows/pr-title.yml; the hardware tag is always AMD here.
TITLE_RE = re.compile(
    r"^\[(" + "|".join(PR_TYPES) + r")\] (?:\[([A-Za-z0-9][A-Za-z0-9._+-]*)\] )?"
    r"[A-Za-z0-9]+(?:[._/-][A-Za-z0-9]+)*: \S(?:[^\r\n]*\S)?$"
)
_WORKAROUND_TITLE_RE = re.compile(r"^(?:Skip |Work around )", re.IGNORECASE)
CONFIDENCE_LABELS = ("FACT", "HIGH", "MEDIUM", "LOW", "SPECULATION")
_FIX_BLOCK_RE = re.compile(r"^```json[ \t]+autofix[ \t]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
_REVIEW_BLOCK_RE = re.compile(r"^```json[ \t]+autofix-review[ \t]*\n(.*?)^```", re.DOTALL | re.MULTILINE)


def parse_fix_report(text: str) -> dict:
    """The agent's ``autofix`` block, checked; raises ValueError naming what is wrong."""
    matches = list(_FIX_BLOCK_RE.finditer(text or ""))
    if not matches:
        raise ValueError("no ```json autofix``` block in the agent's report")
    fix = json.loads(matches[-1].group(1))
    if not isinstance(fix, dict):
        raise ValueError("the autofix block is not a JSON object")
    if fix.get("status") == "no-fix":
        return fix
    missing = [k for k in ("root_cause", "confidence", "fix_kind", "why_minimal", "tests",
                           "cuda_impact", "pr") if not fix.get(k)]
    if missing:
        raise ValueError(f"autofix block lacks {', '.join(missing)}")
    if fix["fix_kind"] not in ("root-cause", "workaround"):
        raise ValueError(f"fix_kind {fix['fix_kind']!r} is not root-cause or workaround")
    if str(fix["confidence"]).strip("[]`").upper() not in CONFIDENCE_LABELS:
        raise ValueError(f"confidence {fix['confidence']!r} is not a confidence label")
    pr = fix["pr"]
    need = ["scope", "title", "motivation", "modifications", "accuracy", "speed"]
    if fix["fix_kind"] == "workaround":
        need.append("workaround")
    missing = [k for k in need if not str(pr.get(k) or "").strip()]
    if missing:
        raise ValueError(f"autofix block's pr lacks {', '.join(missing)}")
    return fix


def parse_review(text: str) -> dict:
    matches = list(_REVIEW_BLOCK_RE.finditer(text or ""))
    if not matches:
        raise ValueError("no ```json autofix-review``` block in the reviewer's report")
    review = json.loads(matches[-1].group(1))
    if not isinstance(review, dict) or review.get("verdict") not in ("PASS", "FAIL"):
        raise ValueError("the review block has no PASS/FAIL verdict")
    return review


def pr_type(files: list[str]) -> str:
    """``Fix`` for product code, ``Test`` for tests only, ``CI`` for workflows and CI scripts only."""
    if all(f.startswith((".github/", "scripts/ci/")) for f in files):
        return "CI"
    if all(_TEST_PATH_RE.search(f) or f.startswith((".github/", "scripts/ci/")) for f in files):
        return "Test"
    return "Fix"


def build_title(fix: dict, files: list[str]) -> str:
    pr = fix["pr"]
    scope = re.sub(r"[^A-Za-z0-9._/-]+", "-", str(pr["scope"]).strip()).strip("-._/")
    text = " ".join(str(pr["title"]).split())
    return f"[{pr_type(files)}] [AMD] {scope}: {text}"


def check_title(title: str, fix_kind: str) -> list[str]:
    problems = []
    m = TITLE_RE.match(title)
    if not m or title != title.strip() or m.group(2) != "AMD":
        problems.append(f"title {title!r} fails the sglang PR title check")
    text = title.split(": ", 1)[-1]
    if fix_kind == "workaround" and not _WORKAROUND_TITLE_RE.match(text):
        problems.append("a workaround's title must start with 'Skip … when …' or 'Work around …'")
    return problems


def count_words(markdown: str) -> int:
    """Words of prose: code blocks, URLs and HTML comments are not counted."""
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"\]\([^)]*\)", "]", text)
    text = re.sub(r"https?://\S+", " ", text)
    return len(re.findall(r"[A-Za-z0-9][\w'.-]*", text))


def check_pr_text(fix: dict) -> list[str]:
    """Checks on the agent's PR prose (R4: code checks what it can count)."""
    pr = fix["pr"]
    problems = []
    motivation = str(pr["motivation"])
    if not re.search(r"\[(?:" + "|".join(CONFIDENCE_LABELS) + r")\]", motivation):
        problems.append("Motivation names no confidence label for the root cause")
    if not re.search(r"would be wrong if", motivation, re.IGNORECASE):
        problems.append("Motivation does not end with 'This would be wrong if …'")
    prose = "\n".join(str(pr.get(k) or "") for k in
                      ("motivation", "modifications", "workaround", "accuracy", "speed"))
    words = count_words(prose)
    if words > MAX_BODY_WORDS:
        problems.append(f"the PR text has {words} words (at most {MAX_BODY_WORDS})")
    if re.search(r"(?<![\w-])R\d+(?![\w-])|\bcluster\b|\blane\b|\bleg\b", prose):
        problems.append("the PR text uses the bot's internal words (R123, cluster, leg, lane)")
    if re.search(r"(?<![\w`])@[A-Za-z0-9]", prose):
        problems.append("the PR text @-mentions someone")
    return problems


def template_sections(template: str) -> list[str]:
    return re.findall(r"^## (.+?)\s*$", template, re.MULTILINE)


def render_pr_body(template: str, plan: dict, fix: dict, verification: dict, checks: dict) -> str:
    """The sglang PR template with every section kept and filled in; ``## Test plan`` added."""
    pr = fix["pr"]
    ff = plan.get("first_fail") or {}
    jobs = plan["jobs"]
    job_links = ", ".join(f"[`{_job_label(j['job_name'])}`]({j['run_url']}/job/{j['job_id']})"
                          for j in jobs[:6])
    if len(jobs) > 6:
        job_links += f" and {len(jobs) - 6} more"
    since = (f"since [{ff['date']}]({ff['run_url']}), {plan['failing_runs']} scheduled run(s) in a row"
             if ff.get("date") else "in the latest scheduled runs")
    motivation = f"{pr['motivation'].strip()}\n\nFailing on `main` {since}: {job_links}."
    modifications = pr["modifications"].strip()
    if fix["fix_kind"] == "workaround":
        modifications += f"\n\n### Workaround\n\n{pr['workaround'].strip()}"
    accuracy = pr["accuracy"].strip()
    if verification.get("accuracy_lines"):
        accuracy += "\n\n```\n" + "\n".join(verification["accuracy_lines"]) + "\n```"
    speed = pr["speed"].strip()
    if verification.get("speed_lines"):
        speed += "\n\n```\n" + "\n".join(verification["speed_lines"]) + "\n```"
    test_plan = render_test_plan(plan, verification)
    filled = {"Motivation": motivation, "Modifications": modifications,
              "Accuracy Tests": accuracy, "Speed Tests and Profiling": speed}

    out: list[str] = []
    section = ""
    checklist_item = 0
    for line in template.splitlines():
        heading = re.match(r"^## (.+?)\s*$", line)
        if heading:
            if section in filled:
                out += ["", filled[section], ""]
            if heading.group(1) == "Accuracy Tests":
                out += ["## Test plan", "", test_plan, ""]
            section = heading.group(1)
            out.append(line)
            continue
        if section == "Checklist" and line.startswith("- [ ] "):
            ticked = (checklist_item == 0 and checks.get("pre_commit")) or \
                     (checklist_item == 1 and checks.get("tests_changed"))
            if ticked:
                line = "- [x] " + line[len("- [ ] "):]
            checklist_item += 1
        out.append(line)
    if section in filled:
        out += ["", filled[section]]
    data = {"problem": plan["id"], "fix_kind": fix["fix_kind"], "workflow": plan["recipe"]["workflow"],
            "families": plan["recipe"]["families"], "tests": plan["tests"], "run_id": verification.get("run_id"),
            "review": checks.get("review", "")}
    out += [
        "",
        "---",
        "Opened by the AMD CI bot after the scheduled AMD CI job above failed on `main`; "
        "the fix was re-run on AMD GPUs before this PR was opened.",
        "",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
        "",
        PR_MARKER.format(problem=plan["id"]),
        f"{PR_DATA_PREFIX}{_hidden_json(data)} -->",
    ]
    return "\n".join(out).strip() + "\n"


def render_test_plan(plan: dict, verification: dict) -> str:
    run_url = verification.get("run_url", "")
    lines = [f"Re-ran the failing job on this branch on AMD GPUs: [{plan['recipe']['workflow']} run]({run_url})."
             if run_url else "Not re-run yet.", ""]
    if verification.get("results"):
        lines += ["| Test | `main` | This PR |", "|---|---|---|"]
        for r in verification["results"]:
            before = f"[failed]({r['before_url']})" if r.get("before_url") else "failed"
            lines.append(f"| `{r['file']}::{r['func']}` | {before} | [{r['status']}]({r['job_url']}) |")
    if verification.get("unrelated"):
        lines += ["", "Also failed in the same job(s), unrelated to this change: "
                  + ", ".join(f"`{f}`" for f in verification["unrelated"]) + "."]
    return "\n".join(lines)


def _job_label(name: str) -> str:
    """The job's name without the runner label or a caller prefix (Ground Rules: Job names)."""
    last = name.split(" / ")[-1]
    return re.sub(r",? ?linux-[\w.-]+", "", last).replace("(, ", "(").replace(" ()", "")


def _hidden_json(data) -> str:
    """JSON safe inside an HTML comment."""
    return json.dumps(data, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")


def verified_numbers(segment: str) -> tuple[list[str], list[str]]:
    """Accuracy and speed lines of a passing test's log, for the PR (code copies them, R4)."""
    acc, speed = [], []
    for line in segment.splitlines():
        s = line.strip()
        if not re.search(r"\d", s) or len(s) > 200:
            continue
        if re.search(r"\b(?:accuracy|score|gsm8k|mmlu|mmmu)\b", s, re.IGNORECASE):
            acc.append(s)
        elif _PERF_RE.search(s):
            speed.append(s)
    return acc[-5:], speed[-5:]


# ---------------------------------------------------------------------------
# State on the daily issue
# ---------------------------------------------------------------------------

def load_state(body: str) -> dict:
    start = (body or "").rfind(STATE_PREFIX)
    if start < 0:
        return {}
    end = body.find(" -->", start)
    try:
        state = json.loads(body[start + len(STATE_PREFIX):end])
    except ValueError:
        log.warning("autofix state is not valid JSON; starting over")
        return {}
    return state if isinstance(state, dict) else {}


def new_day_state(state: dict, today: str, now: datetime) -> dict:
    """Carry *state* into *today*: keep problems and recent starts, start a new event log."""
    state = {
        "date": today,
        "problems": dict(state.get("problems") or {}),
        "entered": [e for e in state.get("entered") or []
                    if datetime.fromisoformat(e["at"]) > now - timedelta(hours=48)],
        "notified": list(state.get("notified") or []),
        "notices": [n for n in state.get("notices") or []
                    if datetime.fromisoformat(n["at"]) > now - timedelta(days=30)],
        "queue": list(state.get("queue") or []),
        "events": list(state.get("events") or []) if state.get("date") == today else [],
    }
    for pid, p in list(state["problems"].items()):
        if p.get("status") in ("opened", "gave-up") and \
                datetime.fromisoformat(p["updated"]) < now - timedelta(days=30):
            del state["problems"][pid]
    return state


def add_event(state: dict, now: datetime, pid: str, event: str, detail: str = "") -> None:
    state["events"].append({"at": now.isoformat(timespec="seconds"), "id": pid,
                            "event": event, "detail": detail[:300]})
    state["events"] = state["events"][-150:]


_STATUS_TEXT = {
    "ready": "🧪 fix passed the checks; pushing it",
    "dispatched": "🔄 fix on a branch, re-running the job",
    "opened": "✅ draft PR opened",
    "gave-up": "❌ gave up",
    "retry": "🔁 will try again",
    "waiting": "⏳ waiting for its hour",
}


def render_log_comment(state: dict, mode: str, cap: int, now: datetime) -> str:
    lines = [
        LOG_MARKER,
        "## AMD CI autofix",
        "",
        f"Mode `{mode}`: at most {cap} problems start per hour; each gets {MAX_ATTEMPTS} attempts. "
        f"A draft PR is opened only after the failing job passes on AMD GPUs. "
        f"Last updated {now.strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
    ]
    active = {pid: p for pid, p in state["problems"].items()
              if p.get("status") in ("ready", "dispatched", "retry") or p.get("updated", "").startswith(state["date"])}
    if active:
        lines += ["| Problem | Status | Attempts | Detail |", "|---|---|---|---|"]
        for pid, p in sorted(active.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True):
            detail = p.get("pr") or p.get("run_url") or p.get("reason") or ""
            if detail.startswith("http"):
                detail = f"[link]({detail})"
            lines.append(f"| <!-- {pid} --> {_cell(p.get('name') or pid)} | {_STATUS_TEXT.get(p.get('status'), p.get('status', ''))} "
                         f"| {p.get('attempts', 0)} | {_cell(detail)} |")
        lines.append("")
    if state.get("queue"):
        lines.append(f"**Waiting** ({len(state['queue'])}): "
                     + "; ".join(f"<!-- {q['id']} -->{_cell(q.get('name') or q['id'])} ({q['jobs']} failed jobs)"
                                 for q in state["queue"]))
        lines.append("")
    if state["events"]:
        lines += ["<details><summary>Today's decisions</summary>", ""]
        for e in state["events"]:
            lines.append(f"- {e['at'][11:16]} <!-- {e['id']} --> {_cell(state['problems'].get(e['id'], {}).get('name') or e['id'])}: "
                         f"{e['event']}" + (f" — {_cell(e['detail'])}" if e.get("detail") else ""))
        lines += ["", "</details>", ""]
    lines.append(f"{STATE_PREFIX}{_hidden_json(state)} -->")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def render_queue_notice(waiting: list[dict], cap: int, now: datetime, issue_url: str) -> str:
    """The comment that @-mentions NOTIFY_USER when problems first wait (GitHub emails the mention)."""
    lines = [
        QUEUE_NOTICE_MARKER,
        f"@{NOTIFY_USER} AMD CI autofix: {len(waiting)} problem(s) are waiting, because at most "
        f"{cap} start per hour.",
        "",
        "| Problem | Failed jobs | Failing since | Expected start |",
        "|---|---|---|---|",
    ]
    for q in waiting:
        start = now + timedelta(hours=math.ceil((q["position"] + 1) / max(cap, 1)))
        lines.append(f"| <!-- {q['id']} --> {_cell(q.get('name') or q['id'])} | {q['jobs']} "
                     f"| {q.get('failing_since') or 'unknown'} | ~{start.strftime('%H:00 UTC')} |")
    lines += [
        "",
        f"Expected start assumes no new problem ranks higher. [Daily report]({issue_url}). "
        "Raise `AUTOFIX_MAX_PER_HOUR` to clear the queue sooner, or set `AUTOFIX_MODE=off` to stop autofix.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub and git
# ---------------------------------------------------------------------------

class GitHub:
    """sglang reads (amd-bot's token) and writes (``AUTOFIX_TOKEN``); tests swap in a fake."""

    def __init__(self, token: str, write_token: str = ""):
        self.token = token
        self.write_token = write_token
        self._runs: dict[int, dict] = {}
        self._jobs: dict[int, list[dict]] = {}
        self._logs: dict[int, str] = {}
        self._scheduled: dict[str, list[dict]] = {}
        self._files: dict[int, list[dict]] = {}

    # reads ---------------------------------------------------------------
    def _get(self, endpoint: str, **params):
        resp = gh_get(f"{API}/{endpoint}", headers=gh_headers(self.token), params=params or None)
        if resp.status_code in (404, 422):
            return None
        resp.raise_for_status()
        return resp.json()

    def run(self, run_id: int, fresh: bool = False) -> dict:
        if fresh or run_id not in self._runs:
            self._runs[run_id] = self._get(f"actions/runs/{run_id}") or {}
        return self._runs[run_id]

    def jobs(self, run_id: int, fresh: bool = False) -> list[dict]:
        if fresh or run_id not in self._jobs:
            self._jobs[run_id] = get_run_jobs(self.token, run_id)
        return self._jobs[run_id]

    def job_log(self, job_id: int) -> str:
        if job_id not in self._logs:
            self._logs[job_id] = download_job_logs(self.token, job_id)
        return self._logs[job_id]

    def scheduled_runs(self, workflow: str) -> list[dict]:
        """Completed scheduled runs of *workflow* on main, newest first."""
        if workflow not in self._scheduled:
            data = self._get(f"actions/workflows/{workflow}/runs", event="schedule", branch="main",
                             status="completed", per_page=30) or {}
            self._scheduled[workflow] = data.get("workflow_runs") or []
        return self._scheduled[workflow]

    def search_prs(self, query: str) -> list[dict]:
        resp = gh_get("https://api.github.com/search/issues", headers=gh_headers(self.token),
                      params={"q": f"repo:{REPO} is:pr {query}", "per_page": 50})
        if resp.status_code == 422:
            return []
        resp.raise_for_status()
        return resp.json().get("items") or []

    def open_prs(self, since: str) -> list[dict]:
        """Open PRs created on or after *since* (YYYY-MM-DD), newest first."""
        prs: list[dict] = []
        page = 1
        while len(prs) < MAX_OPEN_PRS_SCANNED:
            batch = self._get("pulls", state="open", sort="created", direction="desc",
                              per_page=100, page=page) or []
            prs += [p for p in batch if p["created_at"][:10] >= since]
            if len(batch) < 100 or batch[-1]["created_at"][:10] < since:
                break
            page += 1
        return prs[:MAX_OPEN_PRS_SCANNED]

    def pr_files(self, number: int) -> list[dict]:
        if number not in self._files:
            self._files[number] = self._get(f"pulls/{number}/files", per_page=100) or []
        return self._files[number]

    def commits_touching(self, path: str, since: str) -> list[dict]:
        return self._get("commits", path=path, since=f"{since}T00:00:00Z", sha="main", per_page=50) or []

    def pull(self, number: int) -> dict | None:
        return self._get(f"pulls/{number}")

    def compare(self, base: str, head: str) -> list[dict]:
        data = self._get(f"compare/{base}...{head}") or {}
        return data.get("commits") or []

    def pr_diff(self, number: int) -> str:
        return get_pr_diff(self.token, number)

    def branch_runs(self, workflow: str, branch: str) -> list[dict]:
        data = self._get(f"actions/workflows/{workflow}/runs", branch=branch,
                         event="workflow_dispatch", per_page=20) or {}
        return data.get("workflow_runs") or []

    def pr_for_branch(self, branch: str) -> dict | None:
        """A PR already opened from *branch* (a tick that died after opening it)."""
        prs = self._get("pulls", head=f"{REPO.split('/')[0]}:{branch}", state="all", per_page=5) or []
        return prs[0] if prs else None

    # writes (AUTOFIX_TOKEN) ------------------------------------------------
    def _write(self, method: str, path: str, **kwargs) -> requests.Response:
        if not self.write_token:
            raise RuntimeError("AUTOFIX_TOKEN is not set")
        resp = requests.request(method, f"{API}/{path}", headers=gh_headers(self.write_token),
                                timeout=60, **kwargs)
        resp.raise_for_status()
        return resp

    def identity(self) -> tuple[str, str]:
        """(login, noreply email) of the token's owner, for commits."""
        resp = gh_get("https://api.github.com/user", headers=gh_headers(self.write_token))
        resp.raise_for_status()
        user = resp.json()
        return user["login"], f"{user['id']}+{user['login']}@users.noreply.github.com"

    def dispatch(self, workflow: str, branch: str, inputs: dict) -> None:
        _check_branch(branch)
        self._write("POST", f"actions/workflows/{workflow}/dispatches", json={"ref": branch, "inputs": inputs})

    def create_draft_pr(self, title: str, body: str, branch: str) -> dict:
        _check_branch(branch)
        return self._write("POST", "pulls", json={"title": title, "body": body, "head": branch,
                                                  "base": "main", "draft": True}).json()

    def add_label_if_exists(self, number: int, label: str) -> bool:
        if self._get(f"labels/{label}") is None:
            return False
        self._write("POST", f"issues/{number}/labels", json={"labels": [label]})
        return True

    def delete_branch(self, branch: str) -> None:
        _check_branch(branch)
        resp = requests.delete(f"{API}/git/refs/heads/{branch}", headers=gh_headers(self.write_token), timeout=60)
        if resp.status_code not in (204, 404, 422):
            resp.raise_for_status()


def _check_branch(branch: str) -> None:
    if not branch.startswith(BRANCH_PREFIX):
        raise RuntimeError(f"refusing to touch {branch!r}: autofix branches start with {BRANCH_PREFIX}")


def git(wt: Path, *args: str, timeout: int = 300, check: bool = True, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True, timeout=timeout,
                            env=env)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def staged_diff(wt: Path, binary: bool = False) -> str:
    """Everything the agent changed, new files included, without .ci-context."""
    git(wt, "add", "-A", "--", ".", ":(exclude).ci-context")
    diff = git(wt, "diff", "--cached", *(["--binary"] if binary else []))
    git(wt, "reset", "-q")
    return diff


def push_branch(wt: Path, branch: str, message: str, gh: GitHub) -> str:
    """Commit the worktree's staged changes as the token's owner and push *branch*; returns the commit SHA."""
    _check_branch(branch)
    login, email = gh.identity()
    git(wt, "-c", f"user.name={login}", "-c", f"user.email={email}", "commit", "-q", "-m", message)
    sha = git(wt, "rev-parse", "HEAD").strip()
    auth = base64.b64encode(f"x-access-token:{gh.write_token}".encode()).decode()
    git(wt, "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {auth}",
        "push", "--force", f"https://github.com/{REPO}.git", f"HEAD:refs/heads/{branch}", timeout=600)
    return sha


def find_dispatched_run(gh: GitHub, workflow: str, branch: str, sha: str, after: datetime) -> dict | None:
    for run in gh.branch_runs(workflow, branch):
        created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        if run.get("head_sha") == sha and created >= after - timedelta(minutes=2):
            return run
    return None


def workflow_yaml_at(ref: str):
    """``workflow -> text`` read from the sglang clone at *ref*."""
    def read(workflow: str) -> str | None:
        result = subprocess.run(["git", "show", f"{ref}:.github/workflows/{workflow}"], cwd=SGLANG_REPO_PATH,
                                capture_output=True, text=True, timeout=60)
        return result.stdout if result.returncode == 0 else None
    return read


# ---------------------------------------------------------------------------
# Problems from the daily report
# ---------------------------------------------------------------------------

_HEADING_ID_RE = re.compile(r"<!--\s*(R\d+(?:\s*·\s*R\d+)*)\s*-->")
_FIX_LINK_RE = re.compile(r"\*\*In-flight fix\*\*:\s*✅\s*\[#(\d+)\]")


def problem_sections(summary: str) -> dict[str, dict]:
    """Per problem ID: its plain-language name and whether the report shows a fix."""
    out: dict[str, dict] = {}
    current: list[str] = []
    for line in summary.splitlines():
        s = line.strip()
        if s.startswith("### ") or (s.startswith("|") and _HEADING_ID_RE.search(s)):
            m = _HEADING_ID_RE.search(s)
            ids = re.findall(r"R\d+", m.group(1)) if m else []
            text = _HEADING_ID_RE.sub("", s.lstrip("#").strip()).strip()
            name = re.split(r" — | \| ", text.strip("| "))[0]
            name = re.sub(r"^(?:🆕|↩️)\s*", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", name)).strip()
            fix = re.search(r"✅ fix (?:open|merged)", s) is not None
            if s.startswith("### "):
                current = ids
            # A heading naming several problems names the group; their rows name each one.
            own_name = len(ids) == 1
            for pid in ids:
                entry = out.setdefault(pid, {"name": pid, "fix": False, "fix_pr": None, "named": False})
                if own_name and not entry["named"]:
                    entry.update(name=name, named=True)
                entry["fix"] = entry["fix"] or (fix and own_name)
            continue
        if s.startswith("## "):
            current = []
        m = _FIX_LINK_RE.search(s)
        if m and len(current) == 1:
            out.setdefault(current[0], {"name": current[0], "fix": False, "fix_pr": None, "named": False})
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
        ))
    return problems


def _run_id(run_url: str) -> int:
    return int(re.search(r"/runs/(\d+)", run_url).group(1))


def problem_tests(jobs: list[dict]) -> list[tuple[str, str]]:
    tests = []
    for row in deterministic_rows("", jobs):
        f, fn = row["test_file"].strip("` "), row["test_function"].strip("` ")
        if f.endswith(".py") and fn and fn not in ("-", "—", "N/A", "n/a", "unknown"):
            tests.append((f, fn))
    return list(dict.fromkeys(tests))


def build_plan(p: Problem, gh: GitHub, workflow_yaml, now: datetime) -> tuple[dict | None, str]:
    """The fix plan for *p*, or ``(None, why it is not fixed)``. The order of the
    checks puts the cheap ones first; ``find_fix_pr()`` and the history come last."""
    jobs = []
    for ja in p.jobs:
        if ja["workflow"] not in AUTOFIX_WORKFLOWS:
            continue
        run = gh.run(_run_id(ja["run_url"]))
        if run.get("event") == "schedule" and run.get("head_branch") == "main":
            jobs.append({**ja, "run": run})
    if not jobs:
        wf = {ja["workflow"] for ja in p.jobs}
        why = "; ".join(NOT_FIXED[w] for w in wf if w in NOT_FIXED)
        return None, "not a scheduled failure on main of an autofix workflow" + (f" ({why})" if why else "")

    tests = problem_tests(jobs)
    if not tests:
        return None, "no failing test function named in the analyses"
    errors = [r["error"] for r in deterministic_rows("", jobs)]
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
    covered = set(problem_tests(group_jobs))
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
        job_tests = problem_tests([j])
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
            return None, (f"not failing for sure: `{_job_label(j['job_name'])}` was "
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
        "jobs": plan_jobs(gh, group_jobs),
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


def plan_jobs(gh: GitHub, jobs: list[dict]) -> list[dict]:
    """The failed jobs as a plan keeps them: links, runner labels, commit, and their tests."""
    out = []
    for j in jobs:
        api_job = next((x for x in gh.jobs(_run_id(j["run_url"])) if x["id"] == j["job_id"]), {})
        out.append({k: j[k] for k in ("job_id", "job_name", "run_url", "workflow")} | {
            "labels": api_job.get("labels") or [], "sha": j["run"]["head_sha"],
            "tests": [list(t) for t in problem_tests([j])],
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


# ---------------------------------------------------------------------------
# One attempt: agent → gates → review → push → dispatch
# ---------------------------------------------------------------------------

def render_problem_context(plan: dict) -> str:
    """``.ci-context/problem.md``: the facts code collected about the problem."""
    lines = [f"# Problem: {plan['name']}", "", "## Failing tests", ""]
    lines += [f"- `{f}` :: `{fn}`" for f, fn in plan["tests"]]
    lines += ["", "## Failed jobs (scheduled runs on main)", ""]
    for j in plan["jobs"]:
        lines.append(f"- `{_job_label(j['job_name'])}` ({j['workflow']}): {j['run_url']}/job/{j['job_id']} "
                     f"at commit {j['sha']}")
    for j in plan.get("other_jobs") or []:
        lines.append(f"- also: `{_job_label(j['job_name'])}` ({j['workflow']}): {j['run_url']}/job/{j['job_id']}")
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
                  "- Its diff is in `.ci-context/suspect-pr.diff`. Treat the attribution as a hypothesis."]
    lines += ["", "## How the fix is verified", "",
              f"Code pushes your change to a branch and runs `{plan['recipe']['workflow']}` with "
              f"`{json.dumps(plan['recipe']['inputs'])}` on AMD GPUs. Every test above must PASS without a retry."]
    return "\n".join(lines) + "\n"


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
        "Context: " + ", ".join(f".ci-context/{n}" for n in sorted(files)),
    ])
    text = claude_code_analyze(prompt, work_dir=wt, context_files=files, max_turns=AGENT_MAX_TURNS,
                               timeout_secs=AGENT_TIMEOUT_SECS, output_must_contain="```json autofix")
    return parse_fix_report(text)


def run_review_agent(wt: Path, plan: dict, fix: dict, diff: str, title: str, context: dict[str, str]) -> dict:
    files = {
        "problem.md": context["problem.md"],
        "fix.json": json.dumps(fix, indent=2),
        "patch.diff": diff,
        "pr-draft.md": f"# {title}\n\n" + "\n\n".join(f"## {k}\n\n{v}" for k, v in fix["pr"].items()),
    }
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
    existing = [f for f in files if (wt / f).exists()]
    if existing:
        out = ""
        for _ in range(2):
            try:
                pc = subprocess.run(["pre-commit", "run", "--files", *existing], cwd=wt, capture_output=True,
                                    text=True, timeout=1800)
            except FileNotFoundError:
                out = "pre-commit is not installed on the runner"
                break
            out = pc.stdout + pc.stderr
            if pc.returncode == 0:
                checks["pre_commit"] = True
                break
        if not checks["pre_commit"]:
            problems.append("pre-commit fails: " + " ".join(out.split())[-400:])
    tests = [f for f in existing if f.endswith(".py") and _TEST_PATH_RE.search(f)]
    checks["tests_changed"] = bool(tests)
    for f in tests:
        r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", f], cwd=wt,
                           capture_output=True, text=True, timeout=600,
                           env={**os.environ, "PYTHONPATH": str(wt / "python")})
        out = r.stdout + r.stderr
        if r.returncode == 0:
            continue
        missing = re.search(r"No module named '([\w.]+)'", out)
        if missing and not missing.group(1).startswith("sglang"):
            c = subprocess.run([sys.executable, "-m", "py_compile", f], cwd=wt, capture_output=True, text=True)
            if c.returncode == 0:
                continue
        problems.append(f"{f} does not collect: " + " ".join(out.split())[-300:])
    return problems, checks


_REPO_LOCK = threading.Lock()


@dataclass
class AttemptResult:
    status: str                  # dispatched / failed / model-unavailable / no-fix
    reason: str = ""
    record: dict = field(default_factory=dict)


def start_attempt(plan: dict, attempt: int, gh: GitHub, base_ref: str = "origin/main",
                  previous: str = "", branch_tag: str = "") -> AttemptResult:
    """Run the fix and review agents and gate the result; ``ready`` carries the patch.

    Runs in the ``fix`` job, which holds no token that can write anywhere; the
    ``publish`` job pushes the patch (``push_and_dispatch()``). The caller has
    run ``ensure_sglang_repo()``; attempts run in parallel threads.
    """
    with _REPO_LOCK:  # `git worktree add` on the shared clone is not safe to run concurrently
        since = (plan.get("last_pass") or plan.get("first_fail") or {}).get("date")
        if since:  # the shallow clone must reach back to the failure window for `git show`
            git(SGLANG_REPO_PATH, "fetch", "-q", "origin", "main", f"--shallow-since={since}",
                timeout=600, check=False)
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
                        review = run_review_agent(wt, plan, fix, diff, title, context)
                    except ValueError as exc:
                        review = {"verdict": "FAIL", "reasons": [f"the review agent's report: {exc}"]}
                    if review["verdict"] != "PASS":
                        problems = [f"review: {r}" for r in review.get("reasons") or ["FAIL"]]
            if not problems:
                break
            log.info("%s attempt %d, turn %d rejected: %s", plan["id"], attempt, turn, problems)
            if turn == 2:
                return AttemptResult("failed", "; ".join(problems)[:500])
            feedback = ("# Your previous change was rejected\n\n"
                        + "\n".join(f"- {p}" for p in problems)
                        + "\n\nFix these and write the report again. Your change is still in the worktree "
                          "(`git diff`).\n")

        message = f"{title}\n\n{_first_sentence(fix['pr']['motivation'])}\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
        return AttemptResult("ready", record={
            "branch": f"{BRANCH_PREFIX}{branch_tag}{plan['id'].lower()}-{_slug(plan['name'])}",
            "base_sha": base_sha, "fix": fix, "title": title, "message": message,
            "patch": staged_diff(wt, binary=True), "diff": diff[-20_000:],
            "checks": {**checks, "review": review.get("verdict", "")},
        })
    except ModelUnavailable as exc:
        return AttemptResult("model-unavailable", str(exc)[:300])
    except RuntimeError as exc:  # the agent failed or timed out, or git did
        return AttemptResult("failed", f"agent or git failed: {exc}"[:300])
    finally:
        with _REPO_LOCK:
            remove_agent_worktree(wt)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "fix"


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?])\s", " ".join(text.split()), maxsplit=1)[0]


# ---------------------------------------------------------------------------
# Verifying the dispatched run
# ---------------------------------------------------------------------------

def verify_run(plan: dict, rec: dict, run: dict, jobs: list[dict], job_log) -> dict:
    """Did the dispatched run show the fix works? Returns ``{"done", "ok", "reasons", ...}``.

    ``done`` is False while a job the check needs is still running.
    """
    out = {"done": False, "ok": False, "reasons": [], "results": [], "unrelated": [],
           "run_id": run.get("id"), "run_url": run.get("html_url", "")}
    if not run.get("path", "").endswith(plan["recipe"]["workflow"]):
        out.update(done=True, reasons=[f"the run is {run.get('path')}, not {plan['recipe']['workflow']}"])
        return out
    if run.get("head_sha") != rec["head_sha"]:
        out.update(done=True, reasons=["the run did not test the pushed commit"])
        return out
    families = set(plan["recipe"]["families"])
    mine = [j for j in jobs if job_family(j["name"]) in families]
    if any(j.get("status") != "completed" for j in mine) or (not mine and run.get("status") != "completed"):
        return out
    out["done"] = True
    want_labels = {job_family(j["job_name"]): sorted(j.get("labels") or []) for j in plan["jobs"]}
    for fam in families:
        got = [j for j in mine if job_family(j["name"]) == fam]
        if not got:
            out["reasons"].append(f"the run has no `{fam.split('|')[0]}` job")
        elif want_labels.get(fam) and any(sorted(j.get("labels") or []) != want_labels[fam] for j in got):
            out["reasons"].append(f"`{fam.split('|')[0]}` ran on {got[0].get('labels')}, not {want_labels[fam]}")
    fix_kind = rec["fix"]["fix_kind"]
    before: dict[tuple, str] = {}
    for j in plan["jobs"]:
        for t in j.get("tests") or []:
            before.setdefault(tuple(t), f"{j['run_url']}/job/{j['job_id']}")
    accuracy, speed = [], []
    for f, fn in plan["tests"]:
        best = None
        for j in mine:
            o = test_outcome(job_log(j["id"]), f, fn)
            if o.status != "not-run":
                best = (o, j)
                break
        if not best:
            out["reasons"].append(f"`{f}` did not run")
            continue
        o, j = best
        ok = (o.status == "passed" or (o.status == "skipped" and fix_kind == "workaround")) and not o.retried
        out["results"].append({"file": f, "func": fn, "status": o.status + (" after a retry" if o.retried else ""),
                               "job_url": j["html_url"], "before_url": before.get((f, fn))})
        if not ok:
            out["reasons"].append(f"`{f}::{fn}` {o.status}{' after a retry' if o.retried else ''}"
                                  + (f" ({o.note})" if o.note else ""))
        a, s = verified_numbers(segment_text(job_log(j["id"]), f, max_lines=2000))
        accuracy += a
        speed += s
    for j in mine:
        out["unrelated"] += unrelated_failures(job_log(j["id"]), [tuple(t) for t in plan["tests"]])
    out["unrelated"] = list(dict.fromkeys(out["unrelated"]))
    fix = rec["fix"]
    out["accuracy_lines"] = accuracy[:5] if fix.get("affects_accuracy") else []
    out["speed_lines"] = speed[:5] if fix.get("affects_speed") else []
    out["ok"] = not out["reasons"]
    return out


def previous_attempt_text(rec: dict, verification: dict, gh: GitHub, plan: dict) -> str:
    """``.ci-context/previous-attempt.md`` for a second attempt after the GPU run failed."""
    lines = ["# Your previous attempt did not fix the problem on the GPU", "",
             f"Run: {verification.get('run_url')}", "", "Why it failed:", ""]
    lines += [f"- {r}" for r in verification.get("reasons") or []]
    lines += ["", "## The previous change", "", "```diff", rec.get("diff", "")[-15_000:], "```", ""]
    lines += ["## The previous root cause", "", str(rec.get("fix", {}).get("root_cause", "")), ""]
    for r in verification.get("results") or []:
        if not r["status"].startswith("passed"):
            job_id = int(r["job_url"].rstrip("/").rsplit("/", 1)[-1])
            lines += [f"## Log of `{r['file']}` in the run", "", "```",
                      segment_text(gh.job_log(job_id), r["file"])[-30_000:], "```", ""]
    return "\n".join(lines)


def open_pr(plan: dict, rec: dict, verification: dict, gh: GitHub, template: str) -> dict:
    """Open the draft PR, unless an earlier tick already did (R8)."""
    existing = gh.pr_for_branch(rec["branch"])
    if existing:
        return existing
    body = render_pr_body(template, plan, rec["fix"], verification, rec["checks"])
    pr = gh.create_draft_pr(rec["title"], body, rec["branch"])
    if rec["fix"]["fix_kind"] == "workaround":
        try:
            gh.add_label_if_exists(pr["number"], "workaround")
        except requests.RequestException as exc:
            log.warning("Could not label #%s workaround (%s)", pr["number"], exc)
    return pr


def pr_template() -> str:
    path = SGLANG_REPO_PATH / ".github" / "pull_request_template.md"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# One tick: the fix job, then the publish job
# ---------------------------------------------------------------------------
#
# The agents run in the `fix` job, whose token can only read: code the agent
# runs (a test it wrote, under pytest) can read every environment variable of
# its parent processes, so a write token must never be in that job. The
# `publish` job runs no agent; it holds AUTOFIX_TOKEN and amd-bot's token and
# does every write. The fix job hands over its state and patches as an artifact.

STATE_FILE = "state.json"
SMOKE_FILE = "smoke.json"


def find_issue(token: str, bot_repo: str, date_str: str) -> int | None:
    """The daily issue of *date_str*, open or closed."""
    resp = gh_get(f"https://api.github.com/repos/{bot_repo}/issues", headers=gh_headers(token),
                  params={"state": "all", "labels": "ci-monitor", "per_page": 30})
    resp.raise_for_status()
    title = f"[CI Monitor] Daily Report - {date_str}"
    return next((i["number"] for i in resp.json() if i["title"] == title), None)


def find_log_comment(token: str, bot_repo: str, issue_num: int) -> dict | None:
    return next((c for c in get_issue_comments(token, bot_repo, issue_num)
                 if LOG_MARKER in (c.get("body") or "")), None)


def read_state(token: str, bot_repo: str, issue_num: int, now: datetime) -> tuple[dict, dict | None]:
    """(state, today's log comment). With no comment today, the latest earlier day's state."""
    comment = find_log_comment(token, bot_repo, issue_num)
    if comment:
        return load_state(comment["body"]), comment
    for back in range(1, 8):
        num = find_issue(token, bot_repo, (now - timedelta(days=back)).strftime("%Y-%m-%d"))
        earlier = find_log_comment(token, bot_repo, num) if num else None
        if earlier:
            return load_state(earlier["body"]), None
    return {}, None


def save_handoff(handoff: Path, issue_num: int, state: dict) -> None:
    handoff.mkdir(parents=True, exist_ok=True)
    (handoff / STATE_FILE).write_text(json.dumps({"issue": issue_num, "state": state}))


def patch_path(handoff: Path, pid: str) -> Path:
    return handoff / "patches" / f"{pid}.patch"


def poll_dispatched(pid: str, entry: dict, gh: GitHub, now: datetime) -> tuple[str, dict | None]:
    """``("waiting" | "verified" | "failed", verification)`` for a dispatched attempt."""
    rec, plan = entry["attempt"], entry["plan"]
    if not entry.get("run_id"):
        run = find_dispatched_run(gh, plan["recipe"]["workflow"], rec["branch"], rec["head_sha"],
                                  datetime.fromisoformat(rec["dispatched_at"]))
        if not run:
            if now - datetime.fromisoformat(rec["dispatched_at"]) > timedelta(hours=1):
                return "failed", {"reasons": ["the dispatched run never appeared"], "results": []}
            return "waiting", None
        entry["run_id"], entry["run_url"] = run["id"], run["html_url"]
    run = gh.run(entry["run_id"], fresh=True)
    v = verify_run(plan, rec, run, gh.jobs(entry["run_id"], fresh=True), gh.job_log)
    if not v["done"]:
        if now - datetime.fromisoformat(rec["dispatched_at"]) > timedelta(hours=DISPATCH_TIMEOUT_HOURS):
            return "failed", {**v, "reasons": [f"the run did not finish in {DISPATCH_TIMEOUT_HOURS} h"]}
        return "waiting", None
    return ("verified" if v["ok"] else "failed"), v


def push_and_dispatch(plan: dict, rec: dict, patch: str, gh: GitHub) -> dict:
    """Commit the fix job's *patch* on its base as the token's owner, push the branch, dispatch the job."""
    _check_branch(rec["branch"])
    with _REPO_LOCK:
        git(SGLANG_REPO_PATH, "fetch", "-q", "origin", rec["base_sha"], "--depth", "1", check=False)
        wt = create_agent_worktree(f"publish-{plan['id']}", rec["base_sha"])
    try:
        if git(wt, "rev-parse", "HEAD").strip() != rec["base_sha"]:
            raise RuntimeError(f"could not check out {rec['base_sha']}")
        applied = subprocess.run(["git", "apply", "--binary", "--index", "-"], cwd=wt, input=patch,
                                 capture_output=True, text=True, timeout=120)
        if applied.returncode != 0:
            raise RuntimeError(f"the patch does not apply: {applied.stderr.strip()[:300]}")
        sha = push_branch(wt, rec["branch"], rec["message"], gh)
    finally:
        with _REPO_LOCK:
            remove_agent_worktree(wt)
    dispatched_at = datetime.now(timezone.utc)
    gh.dispatch(plan["recipe"]["workflow"], rec["branch"], plan["recipe"]["inputs"])
    return {"head_sha": sha, "dispatched_at": dispatched_at.isoformat()}


def end_attempt(state: dict, pid: str, entry: dict, now: datetime, what: str, reason: str,
                gh: GitHub, previous: str = "") -> None:
    """An attempt failed: try again, or after ``MAX_ATTEMPTS`` give up and delete the branch."""
    entry["updated"] = now.isoformat(timespec="seconds")
    if entry["attempts"] >= MAX_ATTEMPTS:
        if entry.get("attempt", {}).get("head_sha"):
            gh.delete_branch(entry["attempt"]["branch"])
        entry.update(status="gave-up", reason=f"{what}: {reason}")
        add_event(state, now, pid, f"{what} on attempt {entry['attempts']}; gave up", reason)
        _slim(entry)
    else:
        entry.update(status="retry", reason=f"{what}: {reason}",
                     previous=previous or f"# Your previous attempt failed\n\n{what}: {reason}\n")
        add_event(state, now, pid, f"{what} on attempt {entry['attempts']}; trying again", reason)


def check_dispatched(pid: str, entry: dict, gh: GitHub, state: dict, now: datetime, template: str) -> None:
    """Read a dispatched attempt's run; open the PR, retry, or give up."""
    result, v = poll_dispatched(pid, entry, gh, now)
    if result == "waiting":
        return
    entry["updated"] = now.isoformat(timespec="seconds")
    plan, rec = entry["plan"], entry["attempt"]
    if result == "verified":
        if not gh.pr_for_branch(rec["branch"]):
            other = find_fix_pr(gh, [tuple(t) for t in plan["tests"]],
                                plan.get("failing_since") or now.strftime("%Y-%m-%d"))
            if other is not None:
                gh.delete_branch(rec["branch"])
                entry.update(status="gave-up", reason=f"#{other} came first")
                add_event(state, now, pid, "fix passed, but another fix PR came first", f"#{other}")
                _slim(entry)
                return
        pr = open_pr(plan, rec, v, gh, template)
        entry.update(status="opened", pr=pr["html_url"])
        add_event(state, now, pid, "fix passed on AMD GPUs; draft PR opened", pr["html_url"])
        _slim(entry)
        return
    end_attempt(state, pid, entry, now, "GPU check failed", "; ".join(v.get("reasons") or [])[:400], gh,
                previous=previous_attempt_text(rec, v, gh, plan))


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


def record_attempt(state: dict, pid: str, plan: dict, n: int, result: AttemptResult, now: datetime) -> None:
    entry = state["problems"].setdefault(pid, {"attempts": 0})
    entry.update(name=plan["name"], plan=plan, updated=now.isoformat(timespec="seconds"))
    entry.pop("skip", None)
    if result.status == "model-unavailable":
        add_event(state, now, pid, "model unavailable; not fixed this hour", result.reason)
        return
    entry["attempts"] = n
    entry.pop("previous", None)
    if result.status == "ready":
        entry.update(status="ready", attempt={k: v for k, v in result.record.items() if k != "patch"}, reason="")
        add_event(state, now, pid, f"attempt {n}: the fix passed the checks and the review")
    elif n >= MAX_ATTEMPTS or result.status == "no-fix":
        entry.update(status="gave-up", reason=result.reason)
        add_event(state, now, pid, f"attempt {n}: gave up", result.reason)
        _slim(entry)
    else:
        entry.update(status="retry", reason=result.reason,
                     previous=f"# Your previous attempt was rejected before the GPU run\n\n{result.reason}\n")
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
        if plan:
            (retries if entry.get("status") == "retry" else eligible).append(plan)
            continue
        entry = state["problems"].setdefault(p.id, {"attempts": 0, "updated": now.isoformat(timespec="seconds")})
        if entry.get("skip") != why:
            add_event(state, now, p.id, "not fixed", why)
        entry.update(skip=why, name=p.name)
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
                path.write_text(result.record["patch"])
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
                try:
                    entry["attempt"].update(push_and_dispatch(entry["plan"], entry["attempt"], path.read_text(), gh))
                except (RuntimeError, requests.RequestException) as exc:
                    end_attempt(state, pid, entry, now, "push or dispatch failed", str(exc)[:300], gh)
                    continue
                entry.update(status="dispatched", run_id=None, run_url="", updated=now.isoformat(timespec="seconds"))
                add_event(state, now, pid, f"attempt {entry['attempts']}: fix pushed, job dispatched",
                          entry["attempt"]["branch"])
            elif status == "dispatched":
                check_dispatched(pid, entry, gh, state, now, template)
            elif status == "retry" and now - datetime.fromisoformat(entry["updated"]) > timedelta(days=3):
                if entry.get("attempt", {}).get("head_sha"):
                    gh.delete_branch(entry["attempt"]["branch"])
                entry.update(status="gave-up", updated=now.isoformat(timespec="seconds"))
                add_event(state, now, pid, "no longer in the daily report; gave up")
                _slim(entry)
        except Exception as exc:  # one problem's error must not lose the others' state
            log.exception("%s: publishing failed", pid)
            add_event(state, now, pid, "publishing failed", str(exc))
    write_log(token, bot_repo, issue_num, comment, state, mode, cap, now)
    return 0


def _slim(entry: dict) -> None:
    """A finished problem keeps only what the log and the audit read."""
    for key in ("plan", "attempt", "previous"):
        entry.pop(key, None)


# ---------------------------------------------------------------------------
# Smoke test (E3): one past problem, end to end, no PR
# ---------------------------------------------------------------------------

def smoke_plan(token: str, bot_repo: str, gh: GitHub, date_str: str, pid: str, base_sha: str) -> dict | None:
    """The plan for problem *pid* of *date_str*'s report, without the selection checks
    (the problem has long been fixed)."""
    issue_num = find_issue(token, bot_repo, date_str)
    problem = next((p for p in load_problems(token, bot_repo, issue_num) if p.id == pid), None) if issue_num else None
    if not problem:
        log.error("No problem %s in the %s report", pid, date_str)
        return None
    yaml = workflow_yaml_at(base_sha)
    jobs = [{**ja, "run": gh.run(_run_id(ja["run_url"]))} for ja in problem.jobs
            if ja["workflow"] in AUTOFIX_WORKFLOWS]
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
    return {
        "id": pid, "name": problem.name, "tests": [list(t) for t in problem_tests(group_jobs)],
        "jobs": plan_jobs(gh, group_jobs), "other_jobs": [],
        "errors": [r["error"] for r in deterministic_rows("", group_jobs)][:10],
        "recipe": plan_recipe(group),
        "first_fail": {"date": min(j["run"]["created_at"][:10] for j in group_jobs),
                       "run_url": group_jobs[0]["run_url"], "sha": group_jobs[0]["run"]["head_sha"]},
        "last_pass": None, "failing_runs": len({j["run_url"] for j in group_jobs}),
        "failed_jobs": len(problem.jobs), "suspect": problem.suspect,
    }


def smoke_fix(token: str, bot_repo: str, gh: GitHub, date_str: str, pid: str, base_sha: str,
              handoff: Path) -> int:
    """Smoke test, fix job: the agents' first attempt on *base_sha*, handed to the publish job."""
    ensure_sglang_repo()
    git(SGLANG_REPO_PATH, "fetch", "-q", "origin", base_sha, "--depth", "100", check=False)
    plan = smoke_plan(token, bot_repo, gh, date_str, pid, base_sha)
    if not plan:
        return 1
    t0 = time.time()
    result = start_attempt(plan, 1, gh, base_sha, branch_tag="smoke-")
    handoff.mkdir(parents=True, exist_ok=True)
    if result.status == "ready":
        patch_path(handoff, pid).parent.mkdir(parents=True, exist_ok=True)
        patch_path(handoff, pid).write_text(result.record["patch"])
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
    data = json.loads((handoff / SMOKE_FILE).read_text())
    plan, rec = data["plan"], data["record"]
    summary = {"problem": plan["id"], "status": data["status"], "reason": data["reason"],
               "agent_secs": data["agent_secs"]}
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    if data["status"] == "ready":
        ensure_sglang_repo()
        (out_dir / "attempt-1.diff").write_text(rec["diff"])
        t0 = time.time()
        rec.update(push_and_dispatch(plan, rec, patch_path(handoff, plan["id"]).read_text(), gh))
        entry = {"plan": plan, "attempt": rec, "run_id": None}
        try:
            while True:
                status, v = poll_dispatched(plan["id"], entry, gh, datetime.now(timezone.utc))
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
    parser.add_argument("--mode", choices=MODES,
                        default=(os.environ.get("AUTOFIX_MODE") or DEFAULT_MODE).strip().lower(),
                        help=f"off / live (default: AUTOFIX_MODE or {DEFAULT_MODE})")
    parser.add_argument("--max-per-hour", type=int,
                        default=int(os.environ.get("AUTOFIX_MAX_PER_HOUR") or DEFAULT_MAX_PER_HOUR))
    parser.add_argument("--smoke-problem", help="E3: problem ID (R…) to fix end to end without opening a PR")
    parser.add_argument("--smoke-date", help="E3: date (YYYY-MM-DD) of the daily report holding the problem")
    parser.add_argument("--smoke-base", help="E3: sglang commit to build the fix on (before the human fix)")
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
            return smoke_fix(token, args.bot_repo, gh, args.smoke_date, args.smoke_problem, args.smoke_base, handoff)
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
