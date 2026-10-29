#!/usr/bin/env python3
"""Check a pull request against this repo's rules for changes (AGENTS.md).

Run by ``pr-checks.yml`` on every pull request. A PR fails when:

  - it changes behavior (``scripts/``, ``agent/``, ``runner/``,
    ``.github/workflows/``) but not ``README.md``, and its description has no
    ``README: not needed — <reason>`` line;
  - its description has no ``Design rules:`` line naming the rules it
    touches (``R1``..``Rn`` from README §3), or ``none``;
  - it changes an agent's instructions (``agent/``) or models (``AGENT_TASKS``)
    and its description has no ``Validated:`` line saying how the change
    was run on real data.

The description lines come from ``.github/pull_request_template.md``.

    python scripts/check_pr_rules.py --base <sha> --head <sha> --body-file body.md
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BEHAVIOR_PATHS = ("scripts/", "agent/", "runner/", ".github/workflows/")
AGENT_PATHS = ("agent/",)
_RULE_ID_RE = re.compile(r"\*\*(R\d+)\.")
_PLACEHOLDER_RE = re.compile(r"<[^>]*>")


def _field(body: str, name: str) -> str | None:
    """The text after ``<name>:`` on its own line, with template placeholders removed."""
    m = re.search(rf"(?im)^\s*[-*]?\s*\**{re.escape(name)}\**\s*:\s*(.*)$", body or "")
    if not m:
        return None
    return _PLACEHOLDER_RE.sub("", m.group(1)).strip(" -—*`")


def readme_rule_ids(readme: str) -> set[str]:
    return set(_RULE_ID_RE.findall(readme))


def check(changed: list[str], body: str, readme: str, agent_tasks_changed: bool) -> list[str]:
    """Every problem with the PR; empty when it follows the rules."""
    problems = []
    behavior = [f for f in changed if f.startswith(BEHAVIOR_PATHS)]

    readme_note = _field(body, "README")
    if behavior and "README.md" not in changed:
        if not readme_note or "not needed" not in readme_note.lower():
            problems.append(
                "This PR changes behavior (" + ", ".join(behavior[:5])
                + (" …" if len(behavior) > 5 else "") + ") but not README.md. "
                "Update the matching README section, or add a line "
                "'README: not needed — <reason>' to the description."
            )

    rules = _field(body, "Design rules")
    known = readme_rule_ids(readme)
    if not rules:
        problems.append(
            "The description needs a 'Design rules:' line: 'none', or the rules from README §3 "
            "this PR touches (e.g. 'R2, R4 — what changes and why')."
        )
    elif rules.lower() not in ("none", "none touched"):
        named = set(re.findall(r"\bR\d+\b", rules))
        if not named:
            problems.append("'Design rules:' must be 'none' or list rule IDs such as R2, R4.")
        elif named - known:
            problems.append(
                f"'Design rules:' names {', '.join(sorted(named - known))}, which README §3 "
                f"does not define (it has {', '.join(sorted(known, key=lambda r: int(r[1:])))})."
            )

    if any(f.startswith(AGENT_PATHS) for f in changed) or agent_tasks_changed:
        validated = _field(body, "Validated")
        if not validated:
            problems.append(
                "This PR changes agent instructions or models. Add a 'Validated:' line saying how "
                "the change was run on real data (scripts/validate_live.py, a CI-status replay, "
                "or a linked run)."
            )
    return problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--base", required=True, help="Base commit of the PR")
    parser.add_argument("--head", required=True, help="Head commit of the PR")
    parser.add_argument("--body-file", help="PR description (default: env PR_BODY)")
    args = parser.parse_args()

    body = Path(args.body_file).read_text() if args.body_file else os.environ.get("PR_BODY", "")
    changed = [f for f in _git("diff", "--name-only", f"{args.base}...{args.head}").splitlines() if f]
    utils_diff = _git("diff", f"{args.base}...{args.head}", "--", "scripts/utils.py")
    agent_tasks_changed = bool(re.search(r"^[+-].*AgentTask\(", utils_diff, re.M))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    problems = check(changed, body, readme, agent_tasks_changed)
    print(f"{len(changed)} changed file(s).")
    if not problems:
        print("PR follows the rules for changes (AGENTS.md).")
        return 0
    print("This PR does not follow the rules for changes (AGENTS.md):\n")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
