"""Check the ci-status agent's verdicts against the evidence pack.

The agent ends its report with a ``<!-- ci-status-verdicts {...} -->`` block.
Each verdict is checked against ``.ci-context/evidence.json``: cited IDs exist,
the label is backed by the kind of evidence it needs, and an "unrelated" from
``main`` recurrence rests on comparable records that failed the same way.
Rows whose verdict fails are marked unverified in the posted report.

This proves a verdict is well formed and grounded in collected facts; it does
not prove the reasoning is right.
"""

from __future__ import annotations

import json
import re

from utils import redact_secrets

LABELS = ("related", "possibly_related", "unrelated", "unknown")
BASIS_FOR_LABEL = {
    "related": {"mechanism_chain"},
    "possibly_related": {"suspicion"},
    "unrelated": {"comparable_recurrence", "independence"},
    "unknown": {"insufficient_evidence"},
}
_EMOJI = {"related": "🔴", "possibly_related": "🟡", "unrelated": "🟢", "unknown": "⚪"}
_BLOCK_RE = re.compile(r"<!--\s*ci-status-verdicts\s*(\{.*?\})\s*-->", re.DOTALL)
# Evidence kinds that can show a changed code path actually ran in a job.
_EXECUTION_KINDS = {"environment", "launch", "marker", "failure"}
# Changes here can break images, dependencies, or CI itself, so a failure that
# looks like infrastructure can still be caused by the PR.
_BUILD_PATH_RE = re.compile(
    r"^(docker/|\.github/|scripts/ci/)|(^|/)(requirements[^/]*\.txt|pyproject[^/]*\.toml|setup\.py|"
    r"CMakeLists\.txt|[^/]*Dockerfile[^/]*)$"
)
_METRIC_TOLERANCE = 0.02


def parse_verdict_block(report: str) -> dict | None:
    """The agent's machine-readable verdicts, or None when absent or not JSON."""
    matches = _BLOCK_RE.findall(report)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


def _normalize(verdict) -> dict | None:
    if not isinstance(verdict, dict) or verdict.get("label") not in LABELS:
        return None
    mech = verdict.get("mechanism") if isinstance(verdict.get("mechanism"), dict) else {}

    def ids(value):
        return [str(i) for i in value] if isinstance(value, list) else []

    return {
        "failure_id": str(verdict.get("failure_id", "")),
        "label": verdict["label"],
        "basis": str(verdict.get("basis", "")),
        "mechanism": {step: ids(mech.get(step)) for step in ("change", "execution", "failure")},
        "evidence": ids(verdict.get("evidence")),
        "counter_evidence": ids(verdict.get("counter_evidence")),
        "next_step": str(verdict.get("next_step", "")),
    }


def _worse(pr: list[dict], rec: list[dict]) -> bool:
    """Whether the PR's best attempt is worse than a ``main`` record's best attempt."""
    if not pr or not rec:
        return False
    pr_acc, rec_acc = max(m.get("accuracy", 0.0) for m in pr), max(m.get("accuracy", 0.0) for m in rec)
    pr_inv, rec_inv = min(m.get("invalid", 0.0) for m in pr), min(m.get("invalid", 0.0) for m in rec)
    return pr_acc < rec_acc - _METRIC_TOLERANCE or pr_inv > rec_inv + _METRIC_TOLERANCE


def _recurrence_problems(verdict: dict, failure: dict, ev: dict) -> list[str]:
    fid = failure["id"]
    cited = [i for i in verdict["evidence"]
             if ev.get(i, {}).get("kind") == "main_record" and ev[i]["failure_id"] == fid]
    if not cited:
        return ["the recurrence argument cites no `main` record of this failure"]
    problems = []
    for i in cited:
        r, c = ev[i], ev[i]["comparable"]
        if r["test_result"] != "failed":
            problems.append(f"{i}: the test did not fail there ({r['test_result']})")
            continue
        if c["gpu"] is not True or c["image"] not in ("same build", "same line") or c["launch_configs"] is False:
            problems.append(f"{i}: not comparable (GPU {c['gpu']}, image {c['image']}, "
                            f"launch config {c['launch_configs']})")
        if r["mode"] != failure.get("mode"):
            problems.append(f"{i}: failure mode `{r['mode']}` differs from the PR's `{failure.get('mode')}`")
    pr_metrics = ev.get(f"{fid}.metrics", {}).get("values", [])
    rec_metrics = [ev[i]["metrics"] for i in cited if ev[i]["metrics"]]
    if pr_metrics and rec_metrics and all(_worse(pr_metrics, m) for m in rec_metrics):
        problems.append("the PR's metrics are worse than every cited `main` record")
    records = [r for r in ev.values() if r.get("kind") == "main_record" and r["failure_id"] == fid
               and r["test_result"] in ("passed", "failed")]
    failed = sum(r["test_result"] == "failed" for r in records)
    if failure.get("attempts", 1) >= 2 and records and failed / len(records) < 0.5:
        problems.append(f"the PR failed in all {failure['attempts']} attempts, but `main` failed in only "
                        f"{failed} of {len(records)} runs that executed the test")
    return problems


def _problems(verdict: dict, failure: dict, pack: dict) -> list[str]:
    ev, fid = pack["evidence"], failure["id"]
    label, basis, mech = verdict["label"], verdict["basis"], verdict["mechanism"]
    problems = []
    if basis not in BASIS_FOR_LABEL[label]:
        problems.append(f"basis `{basis}` does not support label `{label}`")
    cited = verdict["evidence"] + verdict["counter_evidence"] + mech["change"] + mech["execution"] + mech["failure"]
    unknown_ids = sorted({i for i in cited if i not in ev})
    if unknown_ids:
        problems.append("cites IDs that are not in the evidence pack: " + ", ".join(unknown_ids))
    if label != "unrelated" and not verdict["next_step"].strip():
        problems.append("no next step")

    def kind(i):
        return ev.get(i, {}).get("kind")

    def own(i):
        return ev.get(i, {}).get("failure_id") == fid

    if label == "related":
        if not any(kind(i) == "diff" for i in mech["change"]):
            problems.append("the mechanism cites no diff hunk for the change")
        if not any(own(i) and kind(i) in _EXECUTION_KINDS for i in mech["execution"]):
            problems.append("the mechanism cites no evidence from this job that the changed path ran")
        if not any(own(i) and kind(i) in ("failure", "metrics") for i in mech["failure"]):
            problems.append("the mechanism cites no log or metric of this failure")
    elif label == "possibly_related" and not verdict["evidence"]:
        problems.append("no supporting evidence cited")
    elif label == "unrelated" and basis == "comparable_recurrence":
        problems += _recurrence_problems(verdict, failure, ev)
    elif label == "unrelated":
        cited_files = {ev[i]["file"] for i in verdict["evidence"] if kind(i) == "diff"}
        if not cited_files:
            problems.append("the independence argument cites no diff hunk")
        if not any(own(i) and kind(i) == "failure" for i in verdict["evidence"]):
            problems.append("the independence argument cites no log of this failure")
        build_files = sorted({f["file"] for f in pack["pr"]["files"] if _BUILD_PATH_RE.search(f["file"])})
        if build_files and not cited_files & set(build_files):
            problems.append("the PR changes CI/build/dependency files (" + ", ".join(build_files[:5])
                            + "); an independence argument must cite and address them")
    return problems


def validate(pack: dict, block: dict | None) -> tuple[list[dict], list[str]]:
    """Check one verdict per failure that needs one; return (checked verdicts, report-level problems).

    A checked verdict keeps the agent's ``label`` and lists ``validation``
    reasons when it is not supported; a missing verdict has label ``None``.
    """
    need = [f for f in pack["failures"] if not f["cascade_of"]]
    need_ids = {f["id"] for f in need}
    by_id, problems = {}, []
    if block is None:
        problems.append("the report has no readable `ci-status-verdicts` block, so no verdict could be checked")
    for raw in (block or {}).get("verdicts", []) if isinstance(block, dict) else []:
        verdict = _normalize(raw)
        fid = verdict["failure_id"] if verdict else str((raw or {}).get("failure_id", "?"))
        if verdict is None:
            problems.append(f"malformed verdict for `{fid}`")
        elif fid not in need_ids:
            problems.append(f"verdict for `{fid}`, which is not a failure needing a verdict")
        elif fid in by_id:
            problems.append(f"duplicate verdict for `{fid}`; the first one is used")
        else:
            by_id[fid] = verdict
    checked = []
    for failure in need:
        verdict = by_id.get(failure["id"])
        if verdict is None:
            checked.append({"failure_id": failure["id"], "label": None, "validation": ["no verdict"]})
        else:
            checked.append({**verdict, "validation": _problems(verdict, failure, pack)})
    return checked, problems


def annotate_report(report: str, pack: dict, checked: list[dict], problems: list[str]) -> str:
    """Mark table rows whose verdict is unsupported and say why, right under the title."""
    failures = {f["id"]: f for f in pack["failures"]}
    lines = report.split("\n")
    notes = list(problems)
    for v in checked:
        f = failures[v["failure_id"]]
        name = f"[{f['job']['name']}]({f['job']['url']})" + (f" `{f['test_file']}`" if f["test_file"] else "")
        row = next((i for i, line in enumerate(lines)
                    if line.startswith("|") and f"/job/{f['job']['id']}" in line
                    and (not f["test_file"] or f["test_file"].rsplit("/", 1)[-1] in line)), None)
        reasons = list(v["validation"])
        if row is not None and v["label"]:
            shown = next((label for label, emoji in _EMOJI.items() if emoji in lines[row]), None)
            if shown and shown != v["label"]:
                reasons.append(f"the table says {_EMOJI[shown]} but the verdict block says {_EMOJI[v['label']]}")
        if not reasons:
            continue
        if row is not None:
            lines[row] = re.sub("🔴|🟡|🟢", "⚪ unverified", lines[row], count=1)
        said = f"the report says {_EMOJI[v['label']]} {v['label']}, " if v["label"] else ""
        notes.append(f"{name}: {said}not supported because " + "; ".join(reasons))
    if notes:
        warning = ["> [!WARNING]", "> **Verdicts the evidence does not support (treat them as unknown):**"]
        warning += [f"> - {note}" for note in notes]
        title = next((i for i, line in enumerate(lines) if line.startswith("## ")), -1)
        lines[title + 1:title + 1] = ["", *warning]
    return redact_secrets("\n".join(lines))
