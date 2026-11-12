"""Suggested extra CI: skills from main, the hidden block, the rendered section, mention cleanup."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_ci_for_pr  # noqa: E402
import suggested_tests as ts  # noqa: E402
from suspect_pr_comments import neutralize_mentions  # noqa: E402


class MainContextTest(unittest.TestCase):
    def test_skills_come_from_main(self):
        seen = []

        def fetch(token, path, ref):
            seen.append((path, ref))
            return f"main:{path}"

        files = ts.main_context("t", fetch=fetch)
        self.assertEqual({ref for _, ref in seen}, {"main"})
        self.assertEqual(files, {"sglang-skills/rerun-test.md": "main:.agents/skills/rerun-test/SKILL.md",
                                 "sglang-skills/write-sglang-test.md": "main:.agents/skills/write-sglang-test/SKILL.md"})

    def test_a_github_error_leaves_the_file_out(self):
        def fetch(token, path, ref):
            raise ts.requests.ConnectionError("reset")

        self.assertEqual(ts.main_context("t", fetch=fetch), {})


class BlockTest(unittest.TestCase):
    def test_parse_check_and_strip(self):
        block = {"nv": {"extra_needed": True, "reason": "python/sglang/srt/x.py\n changes HiCache"},
                 "amd": {"extra_needed": "yes", "reason": None}}
        report = "## CI Status for PR #1\nok\n<!-- test-selection\n" + json.dumps(block) + "\n-->\n"
        self.assertEqual(ts.parse_block(report), block)
        self.assertNotIn("test-selection", ts.strip_block(report))
        result = ts.check(ts.parse_block(report))
        self.assertEqual(result["platforms"], {
            "nv": {"extra_needed": True, "reason": "python/sglang/srt/x.py changes HiCache"},
            "amd": {"extra_needed": None, "reason": ""}})  # a non-bool answer counts as not stated

    def test_a_broken_block_never_shows_its_json(self):
        report = '## CI Status for PR #1\nok\n<!-- test-selection {"nv": {"reason": "a} --> b"}} -->\n'
        self.assertIsNone(ts.parse_block(report))
        self.assertEqual(ts.strip_block(report), "## CI Status for PR #1\nok\n")

    def test_malformed_blocks_do_not_crash(self):
        for block in (None, {"nv": []}, {"nv": {"extra_needed": 1}}, {"amd": "x"}):
            result = ts.check(block)
            self.assertEqual(set(result["platforms"]), {"nv", "amd"})
        self.assertIsNone(ts.parse_block("<!-- test-selection {not json} -->"))


class RenderTest(unittest.TestCase):
    def test_section(self):
        text = ts.render(ts.check({"nv": {"extra_needed": True, "reason": "r1"},
                                   "amd": {"extra_needed": False, "reason": "r2"}}))
        lines = [line for line in text.splitlines() if line]
        self.assertEqual(lines[1], f"> {ts.DISCLAIMER}")
        self.assertEqual(lines[-1], f"> {ts.DISCLAIMER}")
        self.assertIn("- **NVIDIA**: extra CI needed: **yes**. r1", lines)
        self.assertIn("  - To run it, comment `/run-extra-ci nv`.", lines)
        self.assertIn("- **AMD**: extra CI needed: **no**. r2", lines)
        self.assertNotIn("/run-extra-ci amd", text)  # no trigger when not needed
        self.assertNotIn("/rerun-test", text)

    def test_no_block(self):
        self.assertIn("no readable suggestion", ts.render(ts.check(None)))


class MentionTest(unittest.TestCase):
    def test_code_fences_are_left_alone(self):
        text = "ping @dev\n```python\n@torch.no_grad()\ndef f(): pass\n```\nand @other\n"
        self.assertEqual(ts.neutralize_outside_code(text, neutralize_mentions),
                         "ping `@dev`\n```python\n@torch.no_grad()\ndef f(): pass\n```\nand `@other`\n")


class CiStatusIntegrationTest(unittest.TestCase):
    """The ci-status harness: skills passed as context, the section rendered, mentions neutralized."""

    def test_report_gets_the_section(self):
        block = {"nv": {"extra_needed": True, "reason": "ping @someone about python/sglang/srt/x.py"},
                 "amd": {"extra_needed": False, "reason": "no ROCm path"}}
        agent_out = ("## CI Status for PR #7\n\n**Verdict:** no failures.\n"
                     "<!-- ci-status-verdicts {\"verdicts\": []} -->\n"
                     f"<!-- test-selection {json.dumps(block)} -->\n")
        seen = {}

        def fake_analyze(prompt, work_dir, context_files, **kwargs):
            seen.update(prompt=prompt, context=context_files)
            return agent_out

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(check_ci_for_pr, "build_evidence_pack", return_value={
                    "checkout": {"sha": "abc", "what": "PR head"}, "request": {"head_sha": "abc"},
                    "failures": [], "evidence": {}, "pr": {"files": []}}), \
                mock.patch.object(check_ci_for_pr, "collect_ci_runtime_env", return_value=""), \
                mock.patch.object(check_ci_for_pr, "claude_code_analyze", side_effect=fake_analyze), \
                mock.patch.object(ts, "get_file_content_at_ref", side_effect=lambda t, p, r: f"main:{p}"):
            (Path(tmp) / ".ci-context").mkdir()
            report = check_ci_for_pr._check_ci_with_agent("t", 7, Path(tmp), as_of="2026-11-01T00:00:00Z",
                                                          out_dir=Path(tmp) / "out")
            saved = json.loads((Path(tmp) / "out" / "selection.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["context"]["sglang-skills/rerun-test.md"], "main:.agents/skills/rerun-test/SKILL.md")
        self.assertIn("As of: 2026-11-01T00:00:00Z", seen["prompt"])
        self.assertIn("### Suggested extra CI", report)
        self.assertIn("`/run-extra-ci nv`", report)
        self.assertNotIn("test-selection", report)
        self.assertIn("`@someone`", report)
        self.assertEqual(saved["platforms"]["nv"]["extra_needed"], True)
        self.assertEqual(saved["as_of"], "2026-11-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
