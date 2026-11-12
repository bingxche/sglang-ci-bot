"""What ci-autofix reads from sglang CI: job names and their dispatch recipes
(``recipe_for()``), the errors it leaves to people (``classify_infra()``),
and test results in job logs (``test_outcome()``, ``verify_run()``)."""

import re
import subprocess
from dataclasses import dataclass, field

from autofix_config import (
    AUTOFIX_WORKFLOWS,
    DISAGG_WORKFLOW,
    NOT_FIXED,
    PR_TEST_EXTRA_WORKFLOW,
    PR_TEST_WORKFLOW,
    SPUR_GPU,
    SPUR_WORKFLOW,
)
from utils import SGLANG_REPO_PATH


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
    """The job without its partition or runner: two runs of it are comparable.
    Where it ran is checked separately (``verify_run()``), so a job that moved
    to another pool of the same GPU still matches."""
    j = parse_job_name(name)
    if j.base.startswith("nightly-mi355x-"):  # disagg: the config is the job
        return f"{j.base}|{j.params[0] if j.params else ''}"
    return f"{j.base}|{j.rocm}"


_RUNNER_LABEL_RE = re.compile(r"linux-[\w.-]+")


def _runner_gpu(label: str) -> tuple[str, str | None]:
    """``linux-mi35x-gpu-1`` → (``mi35x``, ``1``); a label naming no GPU count gives None for it."""
    arch = re.search(r"mi\d+x?", label)
    count = re.search(r"(\d+)-?gpu|gpu-(\d+)", label)
    return (arch.group() if arch else label), (next(g for g in count.groups() if g) if count else None)


def _same_gpu(x: str, y: str) -> bool:
    (gx, cx), (gy, cy) = _runner_gpu(x), _runner_gpu(y)
    return gx == gy and not (cx and cy and cx != cy)


def same_job(a: str, b: str, any_partition: bool = False) -> bool:
    """Two names of one job that may have run on different pools of one GPU: ``x (rocm10, linux-mi35x-gpu-1)``
    is ``x (rocm10, linux-sglang-mi35x-spur)``. GPUs, GPU counts (when both labels name one) and
    ROCm versions still differ; partitions (``…, 3)``) and parts (``part 2``) too, unless *any_partition*:
    suites that place tests by time move a file from one partition to another between runs."""
    if any_partition:
        ja, jb = parse_job_name(a), parse_job_name(b)

        def rest(j: JobName) -> tuple:
            return j.caller, j.base, tuple(p for p in j.params if p != j.runner and not p.isdigit())
        return rest(ja) == rest(jb) and bool(ja.runner) == bool(jb.runner) and _same_gpu(ja.runner, jb.runner)
    ra, rb = _RUNNER_LABEL_RE.findall(a or ""), _RUNNER_LABEL_RE.findall(b or "")
    if _RUNNER_LABEL_RE.sub("", a or "") != _RUNNER_LABEL_RE.sub("", b or "") or len(ra) != len(rb):
        return False
    return all(_same_gpu(x, y) for x, y in zip(ra, rb))


@dataclass(frozen=True)
class Recipe:
    workflow: str        # workflow file to dispatch
    group: tuple         # recipes with equal groups share one dispatch
    target: str          # job id or config the dispatch selects ("" = all)
    select_input: str    # input that takes the comma-joined targets
    inputs: tuple        # other inputs, as (name, value) pairs
    dispatch_ref: str = ""   # ref to dispatch on; "" = the autofix branch itself
    ref_input: str = ""      # input that names the autofix branch, when dispatch_ref is main
    verify_family: str = ""  # family of the dispatched job, when its name is not the failed job's


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


def crusoe_recipe_for(workflow: str, job_name: str, workflow_yaml) -> Recipe | None:
    """The same suite on Crusoe (``SPUR_WORKFLOW``), when that runs it on the failed job's GPU:
    an MI35x job whose suite the Spur workflow offers (its ``suite`` options).

    The Spur workflow is dispatched on main and checks out the branch named in
    its ``ref`` input (its runners' job-start hook only accepts runs from main).
    """
    j = parse_job_name(job_name)
    if workflow != PR_TEST_WORKFLOW or j.caller or not j.rocm or SPUR_GPU not in f"{j.base} {j.runner}":
        return None
    text = workflow_yaml(SPUR_WORKFLOW) or ""
    options = re.search(r"^\s+suite:.*?^\s+options:\s*\[([^\]]*)\]", text, re.M | re.S)
    if ("workflow_dispatch:" not in text or not re.search(r"^\s+ref:\s*$", text, re.M) or not options
            or j.base not in [o.strip() for o in options.group(1).split(",")]):
        return None
    inputs = (("suite", j.base), ("rocm_version", j.rocm))
    if re.search(r"^\s+continue_on_error:\s*$", text, re.M):
        inputs += (("continue_on_error", "true"),)
    return Recipe(SPUR_WORKFLOW, (SPUR_WORKFLOW, inputs), "", "", inputs, dispatch_ref="main", ref_input="ref",
                  # The Spur job is named after its id (`…-spur`), with no ROCm version in it;
                  # the version is the dispatch's own input.
                  verify_family=job_family(f"{j.base}-spur"))


def dispatch_inputs(recipes: list[Recipe]) -> dict:
    """The inputs of one dispatch covering *recipes* (all of one group)."""
    inputs = dict(recipes[0].inputs)
    if recipes[0].select_input:
        inputs[recipes[0].select_input] = ",".join(dict.fromkeys(r.target for r in recipes))
    return inputs


GPU_MEMORY = "GPU memory taken before the job"
# (kind, pattern): errors that are infrastructure, for people to handle.
INFRA_PATTERNS = (
    ("runner lost", r"lost communication with the server|runner has received a shutdown signal"),
    ("disk full", r"No space left on device|\bENOSPC\b|disk quota exceeded"),
    ("GPU unavailable or broken",
     r"hipErrorNoDevice|no ROCm-capable device|amdgpu: GPU (?:reset|hang)|GPU \d+ .*is faulty"),
    ("Hugging Face download",
     r"401 Client Error|Unauthorized for url: https://huggingface|GatedRepoError|RepositoryNotFoundError"
     r"|LocalEntryNotFoundError|couldn't connect to 'https://huggingface\.co'|HfHubHTTPError"),
    # Only failures reaching outside hosts: a test's own server that crashed also shows
    # "Connection refused" or a ReadTimeout, and that is a bug to fix.
    ("network",
     r"Temporary failure in name resolution|Could not resolve host|TLS handshake timeout"
     r"|(?:Read|Connect)Timeout.*(?:huggingface\.co|github\.com|pypi\.org|docker\.io)"),
    ("image pull or build",
     r"failed to (?:pull|resolve) image|manifest unknown|toomanyrequests|pull access denied"
     r"|Error response from daemon|ERROR: failed to solve"),
    # sglang's startup check: other processes already hold most of a GPU's memory (R422, R425).
    # A server an earlier test of the job leaked trips it too: build_plan() counts it only
    # when the failing file was the job's first (``ran_first()``).
    (GPU_MEMORY, r"memory capacity is unbalanced"),
    # A system binary loads a library the image shadows, e.g. ffmpeg picking up torch's
    # libtinfo (R207); the fix is in docker/, which autofix may not change. The analyses
    # retype quotes, so the GLIBC one allows any.
    ("image library conflict",
     r"undefined symbol: _nc_\w+|version\W{1,3}GLIBC(?:XX)?_[\d.]+\W{1,3}not found"),
)
_INFRA_RES = tuple((kind, re.compile(p, re.IGNORECASE)) for kind, p in INFRA_PATTERNS)
PERF_RE = re.compile(
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
    if any(re.search(r"\bperf\b|-perf-", n or "") for n in job_names):
        return True
    return any(PERF_RE.search(e or "") for e in errors)


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


def func_status(lines: list[str], want: str, test_file: str = "") -> str | None:
    """``passed`` / ``failed`` / ``skipped`` for test function *want*, or None when the lines never name it.
    With *test_file*, a pytest line of another file does not count (a log read whole, not per file)."""
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
        if m and test_file and not same_file(m.group(2), test_file):
            pending = None
            continue
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
    ran_file: bool = True  # False: the log has no run_suite segment for the file (read as a whole)


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
    whole_log = not segments
    if whole_log:
        mine = [Segment(None, lines)]
    retried = len(mine) > 1 or any(
        (m := _RETRY_RE.match(line.strip())) and same_file(m.group(1) or m.group(2), test_file)
        for line in lines
    )
    last = mine[-1]
    ran = not whole_log
    status = func_status(last.lines, test_func, test_file)
    if status:
        return Outcome(status, retried, ran_file=ran)
    _, name, _ = _split_func(test_func)
    passed = _segment_passed(last)
    if passed and name in ("setUpClass", "setUpModule"):
        return Outcome("passed", retried, ran_file=ran)
    if passed and not any("skipped=" in line for line in last.lines):
        return Outcome("passed", retried, note="file passed; the function is not named in the log", ran_file=ran)
    if passed:
        return Outcome("unknown", retried, note="file passed with skipped tests; this one may be among them",
                       ran_file=ran)
    return Outcome("unknown", retried, note=f"no result for {test_func} in the log", ran_file=ran)


def ran_first(raw_log: str, files) -> bool:
    """Whether the job's first test file is one of *files*, so no earlier test of the job could have
    left a server holding the GPUs. A log with no run_suite files (one test, or none read) counts."""
    segments = [s for s in file_segments(clean_log(raw_log)) if s.file]
    return not segments or any(same_file(segments[0].file, f) for f in files)


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


def fails_for_sure(history: list[str]) -> bool:
    """*history*: the test's status in the latest completed scheduled runs, newest first."""
    return len(history) >= 2 and history[0] == "failed" and history[1] == "failed"


def run_history(histories: list[list[tuple[str, dict]]]) -> list[tuple[str, dict]]:
    """One history out of several jobs' (one per family, e.g. rocm10 and rocm720), newest run first.

    Each family runs only in some scheduled runs (PR Test runs rocm720 once a day), so two
    failed runs of any of the problem's jobs make it sure, as long as no later run of another
    passed. A run where its jobs disagree counts as its first status that is not ``failed``.
    """
    runs: dict = {}
    for history in histories:
        for status, run in history:
            prev = runs.get(run["run_id"])
            if prev is None or (prev[0] == "failed" and status != "failed"):
                runs[run["run_id"]] = (status, run)
    return sorted(runs.values(), key=lambda x: x[1]["at"], reverse=True)


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


def job_label(name: str) -> str:
    """The job's name without the runner label or a caller prefix (Ground Rules: Job names)."""
    last = name.split(" / ")[-1]
    return re.sub(r",? ?linux-[\w.-]+", "", last).replace("(, ", "(").replace(" ()", "")


def verified_numbers(segment: str) -> tuple[list[str], list[str]]:
    """Accuracy and speed lines of a passing test's log, for the PR (code copies them, R4)."""
    acc, speed = [], []
    for line in segment.splitlines():
        s = line.strip()
        if not re.search(r"\d", s) or len(s) > 200:
            continue
        if re.search(r"\b(?:accuracy|score|gsm8k|mmlu|mmmu)\b", s, re.IGNORECASE):
            acc.append(s)
        elif PERF_RE.search(s):
            speed.append(s)
    return acc[-5:], speed[-5:]


def workflow_yaml_at(ref: str):
    """``workflow -> text`` read from the sglang clone at *ref*."""
    def read(workflow: str) -> str | None:
        result = subprocess.run(["git", "show", f"{ref}:.github/workflows/{workflow}"], cwd=SGLANG_REPO_PATH,
                                capture_output=True, text=True, timeout=60)
        return result.stdout if result.returncode == 0 else None
    return read


def verify_run(plan: dict, rec: dict, run: dict, jobs: list[dict], job_log) -> dict:
    """Did the dispatched run show the fix works? Returns ``{"done", "ok", "reasons", ...}``.

    ``done`` is False while a job the check needs is still running.
    """
    out = {"done": False, "ok": False, "reasons": [], "results": [], "unrelated": [], "inconclusive": False,
           "run_id": run.get("id"), "run_url": run.get("html_url", "")}
    recipe = plan["recipe"]
    if not run.get("path", "").endswith(recipe["workflow"]):
        out.update(done=True, inconclusive=True, reasons=[f"the run is {run.get('path')}, not {recipe['workflow']}"])
        return out
    # A run dispatched on main tests the branch in its `ref` input; its job log shows which commit.
    if not recipe.get("ref_input") and run.get("head_sha") != rec["head_sha"]:
        out.update(done=True, inconclusive=True, reasons=["the run did not test the pushed commit"])
        return out
    families = set(recipe["families"])
    mine = [j for j in jobs if job_family(j["name"]) in families]
    # A family with no job yet may be a later stage still waiting on `needs`: only a finished run says it is absent.
    all_listed = families <= {job_family(j["name"]) for j in mine}
    if any(j.get("status") != "completed" for j in mine) or (not all_listed and run.get("status") != "completed"):
        return out
    # A log GitHub could not serve yet (it often 404s just after a job ends): ask again next tick.
    if any(job_log(j["id"]).startswith("[Failed to fetch logs") for j in mine):
        return out
    out["done"] = True
    # Reasons that say the fix failed; the rest only say this run cannot tell.
    definite: list[str] = []
    if recipe.get("ref_input"):
        sha7 = rec["head_sha"][:7]
        if not mine or any(f"HEAD is now at {sha7}" not in job_log(j["id"]) for j in mine):
            out.update(inconclusive=True, reasons=["the run did not test the pushed commit"])
            return out
    # Where it ran: the failed job's runner labels, or (Crusoe and its ARC fallback) the same GPU.
    gpu = recipe.get("gpu") or ""
    want_labels = {job_family(j["job_name"]): sorted(j.get("labels") or []) for j in plan["jobs"]}
    for fam in families:
        got = [j for j in mine if job_family(j["name"]) == fam]
        if not got:
            out["reasons"].append(f"the run has no `{fam.split('|')[0]}` job")
        elif gpu:
            if any(not any(gpu in label for label in j.get("labels") or []) for j in got):
                out["reasons"].append(f"`{fam.split('|')[0]}` ran on {got[0].get('labels')}, not a {gpu} runner")
        elif want_labels.get(fam) and any(sorted(j.get("labels") or []) != want_labels[fam] for j in got):
            out["reasons"].append(f"`{fam.split('|')[0]}` ran on {got[0].get('labels')}, not {want_labels[fam]}")
    fix_kind = rec["fix"]["fix_kind"]
    before: dict[tuple, str] = {}
    for j in plan["jobs"]:
        for t in j.get("tests") or []:
            before.setdefault(tuple(t), f"{j['run_url']}/job/{j['job_id']}")
    accuracy, speed = [], []
    for f, fn in plan["tests"]:
        # The job that ran the file and says how the test ended; a job that died before running
        # any file, or does not name the test, says nothing.
        outcomes = [(test_outcome(job_log(j["id"]), f, fn), j) for j in mine]
        ran = [x for x in outcomes if x[0].status != "not-run"]
        best = min(ran, key=lambda x: (x[0].status == "unknown", not x[0].ran_file), default=None)
        if not best:
            out["reasons"].append(f"`{f}` did not run")
            continue
        o, j = best
        ok = (o.status == "passed" or (o.status == "skipped" and fix_kind == "workaround")) and not o.retried
        out["results"].append({"file": f, "func": fn, "status": o.status + (" after a retry" if o.retried else ""),
                               "job_url": j["html_url"], "before_url": before.get((f, fn))})
        if not ok:
            reason = f"`{f}::{fn}` {o.status}{' after a retry' if o.retried else ''}" + (f" ({o.note})" if o.note else "")
            out["reasons"].append(reason)
            if o.status != "unknown":
                definite.append(reason)
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
    out["inconclusive"] = bool(out["reasons"]) and not definite
    return out
