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
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import requests

from utils import REPO, gh_headers

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "evals" / "ci_status" / "cases"


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


def run_missing(cases: list[dict], runs_dir: Path) -> None:
    for case in cases:
        artifact = runs_dir / case["id"]
        if (artifact / "verdicts.json").exists():
            continue
        print(f"Running ci-status for {case['id']}...", flush=True)
        subprocess.run([sys.executable, str(ROOT / "scripts" / "check_ci_for_pr.py"), str(case["pr"]),
                        "--no-post", "--head-sha", case["head_sha"], "--as-of", case["as_of"], "--out-dir", str(artifact)],
                       check=False)


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
    parser.add_argument("--cases", default=str(CASES_DIR))
    parser.add_argument("--runs", help="directory with one ci-status artifact per case ID")
    parser.add_argument("--run-missing", action="store_true", help="run ci-status (no posting) for cases without an artifact")
    parser.add_argument("--list-candidates", type=int, nargs="?", const=30, metavar="N",
                        help="list N recent PRs with amd-bot ci-status reports")
    args = parser.parse_args()
    if args.list_candidates:
        print(list_candidates(os.environ.get("GH_PAT", os.environ.get("GITHUB_TOKEN", "")), args.list_candidates))
        return
    if not args.runs:
        sys.exit("--runs is required")
    cases = load_cases(Path(args.cases))
    if args.run_missing:
        run_missing(cases, Path(args.runs))
    print(format_score(score(cases, Path(args.runs))))


if __name__ == "__main__":
    main()
