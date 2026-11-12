#!/usr/bin/env python3
"""Score ci-status verdicts against hand-labeled cases.

Cases are ``evals/ci_status/cases/*.json``. Each pins a PR head and an as-of
time and labels failures (matched by job name and test file) with ``truth``,
the ``basis`` for it, and ``status`` (``proposed`` until a reviewer confirms
it). The legacy bot's verdict for the same failure can be recorded as
``legacy_label`` to score it the same way.

A runs directory holds one ci-status artifact per case (written by
``check_ci_for_pr.py --out-dir``), named by case ID::

    python scripts/eval_ci_status.py --runs RUNS                # score
    python scripts/eval_ci_status.py --runs RUNS --run-missing  # run the bot (no posting) for cases without one
    python scripts/eval_ci_status.py --list-candidates          # past bot reports to label

``--selection`` scores the "Suggested tests" section instead, against
``evals/test_selection/cases/*.json``: per platform, whether the PR needs the
extra tier (``extra_needed``) and the files it must run (``must_run``). A case
whose agent session read anything dated after the case's ``as_of`` (a later
comment, run or commit could give the answer away) is reported as leaked and
not scored.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from suggested_tests import PLATFORMS, registrations
from utils import REPO, gh_headers

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "evals" / "ci_status" / "cases"
SELECTION_CASES_DIR = ROOT / "evals" / "test_selection" / "cases"
# Timestamps in what a tool returned (gh JSON, git log) that date the content itself.
# updated_at is left out: fetching an old object today still shows a recent update.
_DATED_FIELD_RE = re.compile(
    r'"(created_at|createdAt|submitted_at|submittedAt|merged_at|mergedAt|closed_at|closedAt|committedDate|'
    r'authoredDate|publishedAt|published_at|run_started_at)"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)"')
_GIT_DATE_RE = re.compile(r"^(?:Date|CommitDate|AuthorDate):\s+(\w{3} \w{3} +\d+ [\d:]+ \d{4} [+-]\d{4})", re.M)


def load_cases(cases_dir: Path = CASES_DIR) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(cases_dir.glob("*.json"))]


def predicted_labels(artifact_dir: Path) -> dict[tuple, str]:
    """``(job name, test file) → label`` from a ci-status artifact."""
    pack = json.loads((artifact_dir / "pack" / "evidence.json").read_text())
    verdicts = json.loads((artifact_dir / "verdicts.json").read_text())["verdicts"]
    failures = {f["id"]: f for f in pack["failures"]}
    return {(failures[v["failure_id"]]["job"]["name"], failures[v["failure_id"]]["test_file"]): v["label"]
            for v in verdicts}


def metrics(pairs: list[tuple[str, str | None]]) -> dict:
    """Counts over ``(truth, predicted)`` pairs; ``predicted`` is None when no verdict matched."""
    related = [p for t, p in pairs if t == "related"]
    unrelated = [p for t, p in pairs if t == "unrelated"]
    return {
        "n": len(pairs),
        "truth_related": len(related),
        "wrong_unrelated": sum(p == "unrelated" for p in related),
        "flagged": sum(p in ("related", "possibly_related") for p in related),
        "truth_unrelated": len(unrelated),
        "correct_unrelated": sum(p == "unrelated" for p in unrelated),
        "false_related": sum(p == "related" for p in unrelated),
        "unknown": sum(p == "unknown" for _, p in pairs),
        "missing": sum(p is None for _, p in pairs),
    }


def score(cases: list[dict], runs_dir: Path) -> dict:
    rows = []
    for case in cases:
        artifact = runs_dir / case["id"]
        predicted = predicted_labels(artifact) if (artifact / "verdicts.json").exists() else {}
        for label in case["labels"]:
            key = (label["job"], label.get("test_file"))
            rows.append({"case": case["id"], "job": label["job"], "test_file": label.get("test_file"),
                         "truth": label["truth"], "status": label["status"],
                         "bot": predicted.get(key), "legacy": label.get("legacy_label")})
    result = {"rows": rows}
    for scope, selected in (("all", rows), ("confirmed", [r for r in rows if r["status"] == "confirmed"])):
        result[scope] = {
            "bot": metrics([(r["truth"], r["bot"]) for r in selected]),
            "legacy": metrics([(r["truth"], r["legacy"]) for r in selected if r["legacy"]]),
        }
    return result


def _rate(k: int, n: int) -> str:
    return f"{k}/{n}" + (f" ({100 * k / n:.0f}%)" if n else "")


def format_score(result: dict) -> str:
    out = ["| Case | Job | Test | Truth | Status | Bot | Legacy |", "|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        out.append(f"| {r['case']} | {r['job']} | `{r['test_file']}` | {r['truth']} | {r['status']} | "
                   f"{r['bot'] or 'missing'} | {r['legacy'] or '—'} |")
    for scope in ("all", "confirmed"):
        out += ["", f"**{scope} labels**", "", "| | n | wrong unrelated (of truly related) | flagged related/possibly "
                "| wrong related (of truly unrelated) | unknown | missing |", "|---|---|---|---|---|---|---|"]
        for system in ("bot", "legacy"):
            m = result[scope][system]
            out.append(f"| {system} | {m['n']} | {_rate(m['wrong_unrelated'], m['truth_related'])} | "
                       f"{_rate(m['flagged'], m['truth_related'])} | {_rate(m['false_related'], m['truth_unrelated'])} | "
                       f"{_rate(m['unknown'], m['n'])} | {m['missing']} |")
    return "\n".join(out)


def run_missing(cases: list[dict], runs_dir: Path, parallel: int = 1, marker: str = "verdicts.json") -> None:
    """Run ci-status (no posting) for each case whose artifact lacks *marker*; sessions are archived in it.

    With ``parallel`` > 1 each case gets its own sglang clone, so concurrent
    runs never share a git index.
    """
    def run(case: dict) -> None:
        artifact = runs_dir / case["id"]
        env = {**os.environ, "AGENT_ARCHIVE_DIR": str(artifact / "sessions")}
        if parallel > 1:
            env["AGENT_WORKSPACE"] = str(runs_dir / ".workspaces" / case["id"])
        print(f"Running ci-status for {case['id']}...", flush=True)
        with open(runs_dir / f"{case['id']}.log", "w") as log:
            subprocess.run([sys.executable, str(ROOT / "scripts" / "check_ci_for_pr.py"), str(case["pr"]),
                            "--no-post", "--head-sha", case["head_sha"], "--as-of", case["as_of"],
                            "--out-dir", str(artifact)], check=False, env=env, stdout=log, stderr=subprocess.STDOUT)

    runs_dir.mkdir(parents=True, exist_ok=True)
    todo = [c for c in cases if not (runs_dir / c["id"] / marker).exists()]
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        list(pool.map(run, todo))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _tool_results(session: Path):
    """``(tool_use_id, text)`` of every tool result in a Claude Code session log."""
    for line in session.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = (rec.get("message") or {}).get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            body = item.get("content")
            if isinstance(body, list):
                body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
            yield item.get("tool_use_id", ""), str(body or "")


def _read_texts(artifact: Path):
    """``(file name, text)`` of everything a session read: tool results in the logs (subagents' too)
    and the large ones Claude Code keeps in ``<session>/tool-results/``."""
    for path in sorted((artifact / "sessions").glob("**/*")):
        if path.suffix == ".jsonl":
            for _, text in _tool_results(path):
                yield path.name, text
        elif path.is_file() and path.parent.name == "tool-results":
            yield path.name, path.read_text(errors="replace")


def leaks(artifact: Path, as_of: str) -> list[str]:
    """What the agent read that is dated after ``as_of`` (gh JSON dates, git log dates)."""
    limit = _parse_time(as_of)
    found = []
    for name, text in _read_texts(artifact):
        dates = [(f, _parse_time(t)) for f, t in _DATED_FIELD_RE.findall(text)]
        for raw in _GIT_DATE_RE.findall(text):
            try:
                dates.append(("git date", parsedate_to_datetime(raw)))
            except (TypeError, ValueError):
                pass
        for field, when in dates:
            if when > limit:
                found.append(f"{name}: {field} {when.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    return found


def session_cost(artifact: Path) -> dict:
    """Wall time (first to last event, summed over sessions) and tokens of the archived sessions."""
    seconds, tokens = 0.0, 0
    for session in sorted((artifact / "sessions").glob("**/*.jsonl")):
        stamps, seen = [], set()
        for line in session.read_text(errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("timestamp"):
                stamps.append(_parse_time(rec["timestamp"]))
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            if rec.get("type") == "assistant" and isinstance(usage, dict) and msg.get("id") not in seen:
                seen.add(msg.get("id"))
                tokens += sum(v for k, v in usage.items() if k.endswith("_tokens") and isinstance(v, int))
        if stamps and "subagents" not in session.parts:  # subagents run inside the main session's time
            seconds += (max(stamps) - min(stamps)).total_seconds()
    return {"seconds": seconds, "tokens": tokens}


def _registered(sglang: Path, commit: str, path: str, backend: str) -> bool:
    """Whether *path* has an enabled registration for *backend* at *commit* of the sglang clone.

    The PR's head, not main: a PR's own new test may be gone from main (a revert).
    """
    shown = subprocess.run(["git", "-C", str(sglang), "show", f"{commit}:{path}"],
                           capture_output=True, text=True, errors="replace")
    return shown.returncode == 0 and any(r["backend"] == backend and not r["disabled"]
                                         for r in registrations(shown.stdout))


def score_selection(cases: list[dict], runs_dir: Path, sglang: Path | None = None) -> dict:
    """Score the suggested tests per case and platform; leaked or missing runs are not scored."""
    rows, costs = [], []
    for case in cases:
        artifact = runs_dir / case["id"]
        sel_file = artifact / "selection.json"
        sel = json.loads(sel_file.read_text()) if sel_file.exists() else None
        leaked = leaks(artifact, case["as_of"]) if sel else []
        if sel:
            costs.append(session_cost(artifact))
        for key, (_, backend) in PLATFORMS.items():
            truth = case["platforms"][key]
            got = (sel or {}).get("platforms", {}).get(key, {})
            files = [f["file"] for f in got.get("files", [])]
            must = truth.get("must_run", [])
            rows.append({
                "case": case["id"], "platform": key, "status": case.get("status", "proposed"),
                "run": "missing" if sel is None else "leaked" if leaked else "ok", "leaks": leaked[:3],
                "truth_extra": truth["extra_needed"], "bot_extra": got.get("extra_needed"),
                "must_run": must, "hits": [f for f in must if f in files], "suggested": files,
                "commands": got.get("commands", []),
                "invalid": None if sglang is None else [
                    f for f in files if not _registered(sglang, case["head_sha"], f, backend)],
            })
    result = {"rows": rows}
    for scope, selected in (("all", rows), ("confirmed", [r for r in rows if r["status"] == "confirmed"])):
        ok = [r for r in selected if r["run"] == "ok"]
        must = sum(len(r["must_run"]) for r in ok)
        result[scope] = {
            "scored": len(ok), "of": len(selected),
            "extra_correct": sum(r["bot_extra"] == r["truth_extra"] for r in ok),
            "must_run": must, "must_run_hits": sum(len(r["hits"]) for r in ok),
            "invalid_files": None if sglang is None else sum(len(r["invalid"]) for r in ok),
            "avg_files": sum(len(r["suggested"]) for r in ok) / len(ok) if ok else 0.0,
            "leaked": sum(r["run"] == "leaked" for r in selected), "missing": sum(r["run"] == "missing" for r in selected),
        }
    secs = sorted(c["seconds"] for c in costs)
    result["cost"] = {
        "runs": len(costs), "p90_seconds": secs[max(0, math.ceil(0.9 * len(secs)) - 1)] if secs else 0.0,
        "max_seconds": secs[-1] if secs else 0.0, "tokens": sum(c["tokens"] for c in costs),
    }
    return result


def format_selection_score(result: dict) -> str:
    out = ["| Case | Platform | Status | Run | Extra (truth / bot) | must_run hit | Suggested | Commands |",
           "|---|---|---|---|---|---|---|---|"]
    for r in result["rows"]:
        hit = f"{len(r['hits'])}/{len(r['must_run'])}"
        missed = [f for f in r["must_run"] if f not in r["hits"]]
        out.append(f"| {r['case']} | {r['platform']} | {r['status']} | {r['run']} | {r['truth_extra']} / "
                   f"{r['bot_extra']} | {hit}{' missed: ' + ', '.join(missed) if missed else ''} | "
                   f"{len(r['suggested'])} | {'<br>'.join(r['commands'])} |")
    for r in result["rows"]:
        if r["leaks"]:
            out.append(f"\nLeak in {r['case']} ({r['platform']}): " + "; ".join(r["leaks"]))
    for scope in ("all", "confirmed"):
        m = result[scope]
        invalid = "not checked (no --sglang)" if m["invalid_files"] is None else m["invalid_files"]
        out += ["", f"**{scope} labels**: scored {m['scored']} of {m['of']} case-platforms "
                f"(leaked {m['leaked']}, missing {m['missing']})",
                f"- extra_needed correct: {_rate(m['extra_correct'], m['scored'])}",
                f"- must_run recall: {_rate(m['must_run_hits'], m['must_run'])}",
                f"- suggested files that do not exist or are not registered: {invalid}",
                f"- average files suggested per platform: {m['avg_files']:.1f}"]
    c = result["cost"]
    out.append(f"\n**cost**: {c['runs']} runs, p90 {c['p90_seconds']:.0f} s, max {c['max_seconds']:.0f} s, "
               f"{c['tokens']:,} tokens")
    return "\n".join(out)


def list_candidates(token: str, limit: int) -> str:
    """Recent PRs with an amd-bot ci-status report, as a starting point for labeling."""
    resp = requests.get("https://api.github.com/search/issues", headers=gh_headers(token), timeout=60,
                        params={"q": f'repo:{REPO} is:pr commenter:amd-bot "CI Status for PR"',
                                "sort": "updated", "per_page": min(limit, 100)})
    resp.raise_for_status()
    out = ["| PR | Title | Updated |", "|---|---|---|"]
    for item in resp.json().get("items", [])[:limit]:
        out.append(f"| [#{item['number']}]({item['html_url']}) | {item['title'][:80]} | {item['updated_at'][:10]} |")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score ci-status verdicts against labeled cases")
    parser.add_argument("--cases", help=f"default: {CASES_DIR}, or {SELECTION_CASES_DIR} with --selection")
    parser.add_argument("--runs", help="directory with one ci-status artifact per case ID")
    parser.add_argument("--run-missing", action="store_true", help="run ci-status (no posting) for cases without an artifact")
    parser.add_argument("--parallel", type=int, default=1, help="cases run at once by --run-missing")
    parser.add_argument("--selection", action="store_true", help="score the Suggested tests section")
    parser.add_argument("--sglang", type=Path, help="sglang clone with the cases' head commits, to re-check every "
                        "suggested file at the PR head (--selection)")
    parser.add_argument("--list-candidates", type=int, nargs="?", const=30, metavar="N",
                        help="list N recent PRs with amd-bot ci-status reports")
    args = parser.parse_args()
    if args.list_candidates:
        print(list_candidates(os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")), args.list_candidates))
        return
    if not args.runs:
        sys.exit("--runs is required")
    cases = load_cases(Path(args.cases or (SELECTION_CASES_DIR if args.selection else CASES_DIR)))
    if args.run_missing:
        run_missing(cases, Path(args.runs), args.parallel, "selection.json" if args.selection else "verdicts.json")
    if args.selection:
        print(format_selection_score(score_selection(cases, Path(args.runs), args.sglang)))
    else:
        print(format_score(score(cases, Path(args.runs))))


if __name__ == "__main__":
    main()
