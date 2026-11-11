"""AGENTS.md is the only file with rules for agents changing this repo.

Tools look for rules in their own files (Claude Code: CLAUDE.md, Gemini CLI:
GEMINI.md, Cursor: .cursor/rules/, Copilot: .github/copilot-instructions.md,
...). Rules written into any of them would be a second copy that drifts, so
such a file may only point to AGENTS.md.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Where coding tools look for project rules (files, or directories of them).
TOOL_RULE_PATHS = [
    "CLAUDE.md", ".claude/CLAUDE.md", "GEMINI.md", "CONVENTIONS.md",
    ".cursorrules", ".cursor/rules", ".windsurfrules", ".windsurf/rules",
    ".clinerules", ".github/copilot-instructions.md", ".github/instructions",
    ".junie/guidelines.md", ".rules",
]

# A pointer file may hold only: an import of AGENTS.md, one sentence sending the
# reader there, front matter some tools require, and comments.
_ALLOWED_LINE = re.compile(
    r"^(@AGENTS\.md"
    r"|(Follow|See|Read) (the rules in )?`?AGENTS\.md`?( in the repo root)?\.?"
    r"|---|(description|alwaysApply|globs|applyTo): .*|<!--.*-->)$"
)


def is_pointer(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return any("AGENTS.md" in line for line in lines) and all(_ALLOWED_LINE.match(l) for l in lines)


def tool_rule_files() -> list[Path]:
    files = []
    for rel in TOOL_RULE_PATHS:
        path = ROOT / rel
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files += [p for p in path.rglob("*") if p.is_file()]
    return files


class AgentRulesTest(unittest.TestCase):
    def test_agents_md_holds_the_rules(self):
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for must in ("README", "Design rules", "Validated"):
            self.assertIn(must, text)

    def test_claude_code_is_pointed_at_agents_md(self):
        self.assertEqual((ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip(), "@AGENTS.md")

    def test_tool_files_only_point_to_agents_md(self):
        offenders = [str(p.relative_to(ROOT)) for p in tool_rule_files()
                     if not is_pointer(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [], "Rules belong in AGENTS.md only; these tool files must just point to it")

    def test_what_counts_as_a_pointer(self):
        self.assertTrue(is_pointer("@AGENTS.md\n"))
        self.assertTrue(is_pointer("---\ndescription: repo rules\nalwaysApply: true\n---\n\nFollow `AGENTS.md`.\n"))
        self.assertFalse(is_pointer("@AGENTS.md\nAlways update README.md in the same commit.\n"))
        self.assertFalse(is_pointer("Follow AGENTS.md. Also never rename files.\n"))
        self.assertFalse(is_pointer("# Rules\n- one concern per PR\n"))


if __name__ == "__main__":
    unittest.main()
