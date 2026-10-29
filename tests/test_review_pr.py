"""The agent-mode PR review: tiers, upstream rules, the pipeline, the posted review."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_findings as rf  # noqa: E402
import review_pr  # noqa: E402

SHA = "1" * 40
OLD = "0" * 40
DIFF = """diff --git a/python/sglang/srt/a.py b/python/sglang/srt/a.py
--- a/python/sglang/srt/a.py
+++ b/python/sglang/srt/a.py
@@ -10,2 +10,3 @@ def f():
     x = 1
+    y = 3
     return x
"""


def findings_text(*raws) -> str:
    return f"{rf.FINDINGS_MARK}\n{json.dumps({'findings': list(raws)})}\n-->"


def votes_text(*votes) -> str:
    return f"{rf.VOTES_MARK}\n{json.dumps({'votes': list(votes)})}\n-->"


BUG = {"file": "python/sglang/srt/a.py", "line": 11, "severity": "important", "category": "amd",
       "title": "y breaks ROCm", "explanation": "Because."}
NIT = {"file": "python/sglang/srt/a.py", "line": 30, "severity": "nit", "category": "tests",
       "title": "no test for y"}


class TierTest(unittest.TestCase):
    def test_small_prs_get_one_reviewer(self):
        small = [{"additions": 50, "deletions": 10}] * 2
        big = [{"additions": 500, "deletions": 10}]
        self.assertEqual(review_pr.pick_tier(small), "quick")
        self.assertEqual(review_pr.angles_for("quick"), ("all",))
        self.assertEqual(review_pr.pick_tier(big), "standard")
        self.assertEqual(review_pr.pick_tier(big, "quick"), "quick")
        self.assertEqual(review_pr.pick_tier([{"additions": 1, "deletions": 0}] * 7), "standard")


class UpstreamContextTest(unittest.TestCase):
    def test_rules_skills_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".claude" / "rules").mkdir(parents=True)
            (repo / ".claude" / "rules" / "py-style.md").write_text(
                '---\npaths:\n  - "**/*.py"\n---\n\n# Style\n')
            (repo / ".claude" / "rules" / "tests.md").write_text(
                '---\npaths:\n  - "test/**/*.py"\n---\n\n# Tests\n')
            (repo / ".claude" / "rules" / "general.md").write_text("# No front matter\n")
            (repo / ".agents" / "skills" / "env-vars").mkdir(parents=True)
            (repo / ".agents" / "skills" / "env-vars" / "SKILL.md").write_text(
                "---\nname: env-vars\ndescription: Conventions for | env vars.\n---\n\nbody\n")
            files = review_pr.upstream_context(repo, ["python/sglang/x.py", "docs/a.md"])
        self.assertEqual(set(files), {
            "upstream/rules/py-style.md", "upstream/rules/tests.md", "upstream/rules/general.md",
            "upstream/skills/env-vars.md", "upstream/INDEX.md",
        })
        index = files["upstream/INDEX.md"]
        self.assertIn("| `upstream/rules/py-style.md` | `**/*.py` | `python/sglang/x.py` |", index)
        self.assertIn("| `upstream/rules/tests.md` | `test/**/*.py` | — |", index)
        self.assertIn("| `upstream/rules/general.md` | all files | `python/sglang/x.py`, `docs/a.md` |", index)
        self.assertIn("| `upstream/skills/env-vars.md` | Conventions for \\| env vars. |", index)

    def test_glob(self):
        g = review_pr._glob_re("**/*.py")
        self.assertTrue(g.match("a.py") and g.match("x/y/a.py"))
        self.assertFalse(g.match("a.pyc"))
        self.assertTrue(review_pr._glob_re("benchmark/kernels/**").match("benchmark/kernels/a/b.py"))
        self.assertFalse(review_pr._glob_re("test/**/*.py").match("python/test/a.py"))


class AgentReviewTest(unittest.TestCase):
    """The whole pipeline with GitHub and the agents faked."""

    def run_review(self, find_outputs, vote_outputs, rounds=()):
        calls = []

        def fake_run_agents(agent_calls, head_sha, pr_number):
            calls.append(agent_calls)
            return find_outputs if len(calls) == 1 else vote_outputs

        pr = {"number": 7, "title": "T", "body": "Fixes y", "user": {"login": "dev"},
              "base": {"ref": "main"}, "head": {"sha": SHA}}
        files = [{"filename": "python/sglang/srt/a.py", "additions": 300, "deletions": 0}]
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(review_pr, "get_pr_info", return_value=pr), \
                mock.patch.object(review_pr, "get_pr_changed_files", return_value=files), \
                mock.patch.object(review_pr, "ensure_sglang_repo", return_value=Path(tmp)), \
                mock.patch.object(review_pr, "_git", return_value=mock.Mock(returncode=0, stdout="since")), \
                mock.patch.object(review_pr, "get_pr_diff", return_value=DIFF), \
                mock.patch.object(review_pr, "previous_rounds", return_value=list(rounds)), \
                mock.patch.object(review_pr, "collect_ci_runtime_env", return_value="env"), \
                mock.patch.object(review_pr, "run_agents", side_effect=fake_run_agents):
            review = review_pr.agent_review("token", 7)
        return review, calls

    def test_find_verify_render(self):
        finds = [findings_text(BUG), findings_text(dict(BUG, line=12, title="Y breaks ROCm")),
                 RuntimeError("timed out"), findings_text(NIT)]
        votes = [votes_text({"id": "F1", "verdict": "confirmed"}, {"id": "F2", "verdict": "refuted"}),
                 votes_text({"id": "F1", "verdict": "confirmed"}, {"id": "F2", "verdict": "uncertain"}),
                 RuntimeError("verifier died")]
        review, calls = self.run_review(finds, votes)

        self.assertEqual(review.tier, "standard")
        self.assertEqual([c.prompt.splitlines()[0] for c in calls[0]], ["Task: PR Code Review"] * 4)
        self.assertIn("Angle: amd", calls[0][1].prompt)
        self.assertIn("upstream/INDEX.md", calls[0][0].context_files)
        self.assertEqual(calls[1][0].prompt.splitlines()[0], "Task: PR Code Review Verification")
        candidates = json.loads(calls[1][0].context_files["candidates.json"])["candidates"]
        self.assertEqual([c["id"] for c in candidates], ["F1", "F2"])  # the two BUG reports merged

        self.assertEqual(review.failed_reviewers, ["contracts"])
        self.assertEqual([f.title for f in review.findings], ["y breaks ROCm"])
        self.assertEqual((review.voters, review.dropped_by_votes), (2, 1))

        body, comments = review_pr.render_review(review)
        self.assertEqual([(c["path"], c["line"], c["side"]) for c in comments],
                         [("python/sglang/srt/a.py", 11, "RIGHT")])
        self.assertIn("**1 issue** · 1 Important · 0 Nit", body)
        self.assertIn("did not finish", body)
        self.assertIn("`contracts`", body)
        self.assertIn("1 candidate finding was not confirmed", body)
        for word in ("Approve", "Request Changes"):
            self.assertNotIn(word, body)
        meta = rf.parse_marker(body)
        self.assertEqual((meta["sha"], meta["round"], meta["findings"][0]["line"]), (SHA, 1, 11))

    def test_clean_pr_skips_the_verifiers(self):
        review, calls = self.run_review([findings_text()] * 4, [])
        self.assertEqual(len(calls), 1)
        body, comments = review_pr.render_review(review)
        self.assertEqual(comments, [])
        self.assertIn("**No issues found**", body)

    def test_no_reviewer_finished_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.run_review([RuntimeError("x")] * 4, [])

    def test_later_round_reviews_new_changes_and_does_not_repeat(self):
        earlier = {"sha": OLD, "round": 1, "findings": [rf.fingerprint(rf.Finding(
            "python/sglang/srt/a.py", 11, "important", "amd", "y breaks ROCm"))]}
        review, calls = self.run_review([findings_text(BUG)] * 4, [], rounds=[earlier] * 3)
        self.assertEqual(review.round, 4)
        self.assertEqual(review.findings, [])
        self.assertEqual(len(calls), 1)  # the repeat was dropped before verification
        ctx = calls[0][0].context_files
        self.assertIn("y breaks ROCm", ctx["previous-review.md"])
        self.assertEqual(ctx["since-last-review.diff"], "since")
        body, _ = review_pr.render_review(review)
        self.assertIn("Changes since the last review", body)
        self.assertIn("stepping back", body)


class PublishTest(unittest.TestCase):
    def test_refused_inline_comments_are_folded_into_the_body(self):
        responses = [mock.Mock(status_code=422, text="line must be part of the diff"),
                     mock.Mock(status_code=200, json=lambda: {"html_url": "u"})]
        responses[1].raise_for_status = lambda: None
        with mock.patch.object(review_pr.requests, "post", side_effect=responses) as post:
            url = review_pr.publish_review(
                "t", 7, SHA, "## amd-bot review\n\n---\n*Generated by amd-bot using Claude Code CLI*\n",
                [{"path": "a.py", "line": 3, "side": "RIGHT", "body": "bug"}],
            )
        self.assertEqual(url, "u")
        first, second = post.call_args_list
        self.assertEqual(first.kwargs["json"]["event"], "COMMENT")
        self.assertNotIn("comments", second.kwargs["json"])
        self.assertIn("#### `a.py:3`\n\nbug", second.kwargs["json"]["body"])


if __name__ == "__main__":
    unittest.main()
