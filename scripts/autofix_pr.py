"""What ci-autofix publishes, and the code checks on it: the patch
(``gate_patch()``), the agents' reports, and the draft PR's title and body,
in sglang's PR template (``render_pr_body()``)."""

import json
import logging
import re

import requests

from autofix_ci import job_label
from autofix_config import MAX_BODY_WORDS
from autofix_github import GitHub
from autofix_state import hidden_json
from utils import SGLANG_REPO_PATH

log = logging.getLogger("autofix")


PR_MARKER = "<!-- amd-bot-autofix: {problem} -->"
PR_DATA_PREFIX = "<!-- amd-bot-autofix-data:v1 "
PR_MARKER_RE = re.compile(r"<!-- amd-bot-autofix: (R\d+) -->")
FORBIDDEN_PATHS = (re.compile(r"^docker/"), re.compile(r"^python/pyproject[^/]*\.toml$"),
                   re.compile(r"(^|/)CODEOWNERS$"))
TEST_PATH_RE = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*\.py$")
_SKIP_RE = re.compile(
    r"\bunittest\.skip|\bskipIf\b|\bskipUnless\b|\bpytest\.mark\.skip|\bpytest\.skip\(|\bxfail\b"
    r"|\bskip_tests\b|\bSkipTest\b|\bself\.skipTest\(|@skip\b"
)
_HIP_RE = re.compile(r"\bis_hip\(\)|\b_is_hip\b|\bis_rocm\(|torch\.version\.hip")
_SUITE_RE = re.compile(r"register_\w*_ci\(|\bsuite\s*=")


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
        is_test = bool(TEST_PATH_RE.search(path))
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
    junk = [f for f in files if re.search(r"(^|/)(__pycache__|\.pytest_cache)/|\.pyc$", f)]
    if junk:
        problems.append(f"includes build or cache files: {', '.join(junk[:5])}")
    signals = workaround_signals(diff)
    if signals and fix.get("fix_kind") != "workaround":
        problems.append("looks like a workaround (" + "; ".join(signals) + ") but fix_kind is "
                        f"{fix.get('fix_kind')!r}")
    return problems


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
    if all(TEST_PATH_RE.search(f) or f.startswith((".github/", "scripts/ci/")) for f in files):
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
    job_links = ", ".join(f"[`{job_label(j['job_name'])}`]({j['run_url']}/job/{j['job_id']})"
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
        f"{PR_DATA_PREFIX}{hidden_json(data)} -->",
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
