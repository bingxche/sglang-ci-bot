"""The multi-agent review's deterministic half: parse, check, merge, vote, render."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_findings as rf  # noqa: E402

DIFF = """diff --git a/python/sglang/srt/a.py b/python/sglang/srt/a.py
--- a/python/sglang/srt/a.py
+++ b/python/sglang/srt/a.py
@@ -10,3 +10,4 @@ def f():
     x = 1
-    y = 2
+    y = 3
+    z = 4
     return x
diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
"""


def finding(**kw) -> dict:
    raw = {"file": "python/sglang/srt/a.py", "line": 11, "severity": "important",
           "pre_existing": False, "category": "correctness", "title": "y is off by one",
           "explanation": "Because.", "evidence": ["a.py:11"], "rule": "", "other_locations": []}
    raw.update(kw)
    return raw


def block(mark: str, payload: dict) -> str:
    return f"{mark}\n{json.dumps(payload)}\n-->"


class ParseTest(unittest.TestCase):
    def test_last_block_wins_and_prose_is_ignored(self):
        text = ("thinking...\n" + block(rf.FINDINGS_MARK, {"findings": []}) + "\nmore\n"
                + block(rf.FINDINGS_MARK, {"findings": [finding(file="./python/sglang/srt/a.py")]}))
        [f] = rf.parse_findings(text, "amd")
        self.assertEqual((f.file, f.line, f.angles), ("python/sglang/srt/a.py", 11, ["amd"]))

    def test_missing_or_broken_block_is_an_error(self):
        for text in ("no block", f"{rf.FINDINGS_MARK} {{not json -->", block(rf.FINDINGS_MARK, {"x": 1})):
            with self.assertRaises(ValueError):
                rf.parse_findings(text, "all")

    def test_empty_findings_is_fine(self):
        self.assertEqual(rf.parse_findings(block(rf.FINDINGS_MARK, {"findings": []}), "all"), [])

    def test_diff_hunk_lines(self):
        lines = rf.diff_hunk_lines(DIFF)
        self.assertEqual(lines["python/sglang/srt/a.py"], {10, 11, 12, 13})
        self.assertEqual(lines["new.py"], {1, 2})
        self.assertNotIn("gone.py", lines)


class CheckTest(unittest.TestCase):
    def check(self, *raws):
        found = rf.parse_findings(block(rf.FINDINGS_MARK, {"findings": list(raws)}), "all")
        return rf.check_findings(
            found, {"python/sglang/srt/a.py", "new.py"}, rf.diff_hunk_lines(DIFF),
            {"upstream/rules/no-getattr-defensive.md"},
        )

    def test_location_decides_inline(self):
        kept, dropped = self.check(finding(), finding(line=40, title="far away"))
        self.assertEqual([f.inline for f in kept], [True, False])
        self.assertEqual(dropped, [])

    def test_unchanged_file_needs_pre_existing(self):
        kept, dropped = self.check(finding(file="other.py"), finding(file="other.py", pre_existing=True))
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].pre_existing)
        self.assertFalse(kept[0].inline)
        self.assertIn("not changed by the PR", dropped[0][1])

    def test_style_and_unbacked_conventions_are_dropped(self):
        kept, dropped = self.check(
            finding(category="style"),
            finding(category="convention", rule=""),
            finding(category="convention", rule="upstream/rules/no-getattr-defensive.md"),
            finding(severity="blocker"),
            finding(title=""),
        )
        self.assertEqual([f.category for f in kept], ["convention"])
        self.assertEqual(len(dropped), 4)


class MergeVoteTest(unittest.TestCase):
    def findings(self, *raws, angle="all"):
        return rf.parse_findings(block(rf.FINDINGS_MARK, {"findings": list(raws)}), angle)

    def test_same_bug_from_two_angles_merges(self):
        a = self.findings(finding(severity="nit", evidence=["e1"]), angle="correctness")
        b = self.findings(finding(line=12, title="Y is off by one!", evidence=["e2"]), angle="amd")
        c = self.findings(finding(file="new.py", line=1, title="other"), angle="amd")
        merged = rf.merge_findings(a + b + c)
        self.assertEqual([f.id for f in merged], ["F1", "F2"])
        [bug] = [f for f in merged if f.file.endswith("a.py")]
        self.assertEqual(bug.severity, "important")
        self.assertEqual(bug.angles, ["amd", "correctness"])
        self.assertEqual(bug.evidence, ["e2", "e1"])

    def test_repeats_of_an_earlier_round_are_dropped(self):
        found = self.findings(finding(line=13), finding(file="new.py", line=2, title="new one"))
        kept, dropped = rf.drop_repeats(found, [rf.fingerprint(found[0])])
        self.assertEqual([f.title for f in kept], ["new one"])
        self.assertEqual(dropped[0][1], "posted in an earlier round")

    def test_cap_keeps_important_first_and_three_nits(self):
        raws = [finding(file="new.py", line=i, title=f"nit {i}", severity="nit") for i in range(1, 6)]
        raws.append(finding(title="the bug"))
        kept, dropped = rf.cap_findings(self.findings(*raws), 3)
        self.assertEqual([f.title for f in kept], ["the bug", "nit 1", "nit 2"])
        self.assertEqual(len(dropped), 3)
        kept, dropped = rf.cap_findings(self.findings(*raws), 10)
        self.assertEqual(len(kept), 1 + rf.MAX_NITS)
        self.assertEqual({reason for _, reason in dropped}, {f"over {rf.MAX_NITS} nits"})

    def test_tally(self):
        found = rf.merge_findings(self.findings(
            finding(), finding(file="new.py", line=1, title="maybe", severity="nit"),
        ))
        ballots = [
            rf.parse_votes(block(rf.VOTES_MARK, {"votes": [
                {"id": "F1", "verdict": "confirmed", "severity": "nit", "reason": "r1"},
                {"id": "F2", "verdict": "refuted", "reason": "guarded"}]})),
            rf.parse_votes(block(rf.VOTES_MARK, {"votes": [
                {"id": "F1", "verdict": "confirmed", "severity": "nit", "pre_existing": True},
                {"id": "F2", "verdict": "confirmed"}]})),
            rf.parse_votes(block(rf.VOTES_MARK, {"votes": [
                {"id": "F1", "verdict": "uncertain"}]})),
        ]
        kept, dropped = rf.tally(found, ballots, 2)
        self.assertEqual([f.id for f in kept], ["F1"])
        self.assertEqual((kept[0].severity, kept[0].confirmations, kept[0].voters), ("nit", 2, 3))
        self.assertFalse(kept[0].pre_existing)  # 1 of 2 confirmers said so: no majority
        self.assertEqual(dropped[0][1], "confirmed by 1/3 verifiers")

    def test_too_few_verifiers_is_an_error(self):
        with self.assertRaises(RuntimeError):
            rf.tally([], [{}], 2)

    def test_bad_votes_block(self):
        with self.assertRaises(ValueError):
            rf.parse_votes("nothing")
        votes = rf.parse_votes(block(rf.VOTES_MARK, {"votes": [{"id": "F1", "verdict": "maybe"}]}))
        self.assertEqual(votes, {})


class RenderTest(unittest.TestCase):
    def test_count_line(self):
        self.assertEqual(rf.count_line([]), "**No issues found** in this PR's changes.")
        f = rf.Finding("a.py", 1, "important", "amd", "t")
        n = rf.Finding("a.py", 2, "nit", "tests", "t2")
        p = rf.Finding("b.py", 3, "important", "correctness", "t3", pre_existing=True)
        self.assertEqual(rf.count_line([f, n, p]),
                         "**2 issues** · 1 Important · 1 Nit · 1 pre-existing (not introduced here)")
        self.assertTrue(rf.count_line([p]).startswith("**No issues found**"))

    def test_inline_body(self):
        f = rf.Finding("a.py", 1, "important", "amd", "Breaks ROCm @someone",
                       explanation="Calls a CUDA kernel.", evidence=["a.py:1"],
                       other_locations=["b.py:2"], confirmations=2, voters=3)
        body = rf.render_inline(f)
        self.assertTrue(body.startswith("🔴 **Important** · Breaks ROCm `@someone`"))
        self.assertIn("Same pattern also at: `b.py:2`", body)
        self.assertIn("confirmed by 2/3 verifiers", body)
        self.assertNotIn("Approve", body)

    def test_marker_round_trip(self):
        meta = {"v": 2, "sha": "abc", "round": 2, "findings": [{"file": "a.py", "line": 1}]}
        self.assertEqual(rf.parse_marker("x\n" + rf.render_marker(meta) + "\n---"), meta)


if __name__ == "__main__":
    unittest.main()
