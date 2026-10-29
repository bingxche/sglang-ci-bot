"""The rules for changes that PR Checks enforces on every pull request."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_pr_rules as c  # noqa: E402

README = "**R1. Agents read, code writes.**\n**R2. Writes are gated.**\n**R10. Measured.**\n"
TEMPLATE = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")


def body(readme="", rules="none", validated=""):
    return f"## What\n\nx\n\nREADME: {readme}\nDesign rules: {rules}\nValidated: {validated}\n"


class CheckTest(unittest.TestCase):
    def test_docs_only_change_passes(self):
        self.assertEqual(c.check(["README.md"], body(), README, False), [])

    def test_behavior_change_without_readme_fails(self):
        [p] = c.check(["scripts/monitor_ci.py"], body(), README, False)
        self.assertIn("but not README.md", p)

    def test_behavior_change_with_readme_or_a_reason_passes(self):
        self.assertEqual(c.check(["scripts/monitor_ci.py", "README.md"], body(), README, False), [])
        ok = body(readme="not needed — log message only")
        self.assertEqual(c.check(["scripts/monitor_ci.py"], ok, README, False), [])

    def test_design_rules_line_is_required_and_checked(self):
        self.assertIn("'Design rules:' line", c.check(["README.md"], "no lines", README, False)[0])
        self.assertEqual(c.check(["README.md"], body(rules="R2, R10 — new writer"), README, False), [])
        [p] = c.check(["README.md"], body(rules="R7"), README, False)
        self.assertIn("names R7", p)
        [p] = c.check(["README.md"], body(rules="the gating one"), README, False)
        self.assertIn("list rule IDs", p)

    def test_agent_changes_need_validation(self):
        changed = ["agent/tasks/pr-code-review.md", "README.md"]
        [p] = c.check(changed, body(), README, False)
        self.assertIn("'Validated:' line", p)
        self.assertEqual(c.check(changed, body(validated="validate_live review on #47179"), README, False), [])
        [p] = c.check(["scripts/utils.py", "README.md"], body(), README, True)  # an AgentTask model changed
        self.assertIn("'Validated:' line", p)

    def test_the_unfilled_template_fails(self):
        problems = c.check(["agent/ground-rules.md", "scripts/utils.py"], TEMPLATE, README, False)
        self.assertEqual(len(problems), 3)

    def test_rule_ids_come_from_the_real_readme(self):
        ids = c.readme_rule_ids((ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(sorted(ids, key=lambda r: int(r[1:])), [f"R{i}" for i in range(1, len(ids) + 1)])
        self.assertGreaterEqual(len(ids), 8)


if __name__ == "__main__":
    unittest.main()
