import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_cross_workflow_summary as summary  # noqa: E402
import suspect_pr_comments as spc  # noqa: E402


NOW = datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc)
SHA = "956b9a24eecae92f935672224f8a9561135ebc2b"
PASS = "693d33330647c57f82e09ac39a34122fa46b5763"
FAIL = "aa5880e2b8f7807543a6d4745f96bd7a50821b2c"
RUN = "https://github.com/sgl-project/sglang/actions/runs/39645680073"


def _suspect(**overrides) -> dict:
    raw = {
        "cluster": "R281",
        "pr": 42229,
        "sha": SHA,
        "confidence": "HIGH",
        "evidence": "Imports _hipcc, which #45112 removed. cc @someone",
        "last_pass_sha": PASS,
        "first_fail_sha": FAIL,
        "fix_pr": None,
        "related_prs": [45112],
    }
    raw.update(overrides)
    return summary.normalize_suspect(raw)


class FakeGitHub:
    def __init__(self, merged_at="2026-10-21T05:32:30Z", merge_sha=SHA,
                 statuses=None, comments=None):
        self.merged_at = merged_at
        self.merge_sha = merge_sha
        # (base, head) -> status; default puts SHA inside PASS..FAIL.
        self.statuses = statuses or {(PASS, SHA): "ahead", (SHA, FAIL): "ahead"}
        self.comments = comments or []
        self.calls = 0

    def pull(self, pr):
        self.calls += 1
        return {"number": pr, "merged_at": self.merged_at, "merge_commit_sha": self.merge_sha}

    def full_sha(self, sha):
        return SHA if SHA.startswith(sha) else None

    def commit_pulls(self, sha):
        return []

    def compare_status(self, base, head):
        return self.statuses.get((base, head), "behind")

    def pr_comments(self, pr):
        return self.comments


class SuspectBlockTest(unittest.TestCase):
    def test_block_is_stripped_and_kept_hidden(self):
        report = (
            "# Daily Cross-Workflow Summary — 2026-10-21\n\nbody\n\n"
            "```json suspect-prs\n"
            f'[{{"cluster": "R281", "pr": "#42229", "sha": "{SHA}", "confidence": "[HIGH]",'
            ' "evidence": "a\\nb --> c", "fix_pr": null},'
            ' {"cluster": "R1-MXFP4", "pr": 1, "sha": "zz", "confidence": "HIGH"}]\n'
            "```\n"
        )
        body = summary.render_summary_body("2026-10-21 22:00 UTC", report, True)
        self.assertNotIn("```json suspect-prs", body)
        suspects = summary.parse_suspect_prs_comment(body)
        self.assertEqual(len(suspects), 1)
        self.assertEqual(suspects[0]["pr"], 42229)
        self.assertEqual(suspects[0]["confidence"], "HIGH")
        self.assertEqual(suspects[0]["evidence"], "a b -> c")

    def test_missing_block_means_no_suspects(self):
        body = summary.render_summary_body(
            "2026-10-21 22:00 UTC", "# Daily Cross-Workflow Summary — 2026-10-21\n", True,
        )
        self.assertEqual(summary.parse_suspect_prs_comment(body), [])

    def test_job_cluster_comment_round_trips(self):
        comment = summary.render_job_cluster_comment({11: ["R281"], 12: ["R281", "R12"]}, [])
        self.assertEqual(
            summary.parse_job_cluster_comment(f"x\n{comment}\n"),
            {"R12": [12], "R281": [11, 12]},
        )


class CheckSuspectTest(unittest.TestCase):
    def check(self, suspect=None, jobs=(1, 2), gh=None):
        return spc.check_suspect(suspect or _suspect(), list(jobs), gh or FakeGitHub(), NOW)

    def test_passes_all_checks(self):
        d = self.check()
        self.assertEqual((d.result, d.reason), ("post", ""))
        self.assertEqual(d.full_sha, SHA)

    def test_medium_confidence_is_skipped_without_api_calls(self):
        gh = FakeGitHub()
        d = self.check(_suspect(confidence="MEDIUM"), gh=gh)
        self.assertEqual(d.result, "skip")
        self.assertIn("MEDIUM", d.reason)
        self.assertEqual(gh.calls, 0)

    def test_linked_fix_pr_is_skipped(self):
        self.assertIn("fix PR #45656", self.check(_suspect(fix_pr=45656)).reason)

    def test_single_failed_job_is_skipped(self):
        self.assertIn("needs 2", self.check(jobs=[1]).reason)

    def test_missing_window_is_skipped(self):
        self.assertIn("first-failing", self.check(_suspect(first_fail_sha=None)).reason)

    def test_unmerged_pr_is_skipped(self):
        self.assertEqual(self.check(gh=FakeGitHub(merged_at=None)).reason, "PR is not merged")

    def test_old_pr_is_skipped(self):
        self.assertIn("days ago", self.check(gh=FakeGitHub(merged_at="2026-09-01T00:00:00Z")).reason)

    def test_commit_from_another_pr_is_skipped(self):
        self.assertIn("is not from #42229", self.check(gh=FakeGitHub(merge_sha="f" * 40)).reason)

    def test_commit_outside_window_is_skipped(self):
        gh = FakeGitHub(statuses={(PASS, SHA): "behind", (SHA, FAIL): "ahead"})
        self.assertIn("is not between", self.check(gh=gh).reason)

    def test_commit_equal_to_first_failing_passes(self):
        gh = FakeGitHub(statuses={(PASS, SHA): "ahead", (SHA, SHA): "identical"})
        self.assertEqual(self.check(_suspect(first_fail_sha=SHA), gh=gh).result, "post")

    def test_earlier_comment_is_not_repeated(self):
        gh = FakeGitHub(comments=[{"body": spc.pr_marker("R281") + "\nhi", "html_url": "u"}])
        d = self.check(gh=gh)
        self.assertEqual((d.result, d.comment_url), ("already", "u"))


class RenderTest(unittest.TestCase):
    def test_pr_comment(self):
        d = spc.check_suspect(_suspect(), [1, 2, 3], FakeGitHub(), NOW)
        job_info = {1: {"run_url": RUN, "job_name": "stage-b [rocm10]"}, 2: {"run_url": RUN}}
        body = spc.render_pr_comment(d, job_info)
        self.assertTrue(body.startswith(spc.pr_marker("R281")))
        self.assertIn("`@someone`", body)
        self.assertNotIn(" @someone", body)
        self.assertIn(f"[stage-b (rocm10)]({RUN}/job/1)", body)
        self.assertIn("…and 1 more", body)
        self.assertIn("Also involved: #45112.", body)
        self.assertNotIn("sglang-ci-bot", body)

    def test_mentions(self):
        self.assertEqual(
            spc.neutralize_mentions("ping @a-b and @org/team, not a@b.com or `@c`"),
            "ping `@a-b` and `@org/team`, not a@b.com or `@c`",
        )

    def test_dry_run_holds_over_limit_and_logs(self):
        gh = FakeGitHub()
        suspects = [_suspect(cluster=f"R{i}") for i in range(1, spc.MAX_POSTS_PER_RUN + 2)]
        suspects.append(_suspect(cluster="R99", confidence="LOW"))
        clusters = {s["cluster"]: [1, 2] for s in suspects}
        decisions = spc.decide_all(suspects, clusters, {}, gh, NOW)
        spc.apply_decisions("token", decisions, post=False)
        results = [d.result for d in decisions]
        self.assertEqual(results.count("post"), spc.MAX_POSTS_PER_RUN)
        self.assertEqual(results.count("held"), 1)
        self.assertEqual(results[-1], "skip")

        log_body = spc.render_log_comment(decisions, "dry-run", "2026-10-21 22:00 UTC")
        self.assertTrue(log_body.startswith(spc.LOG_COMMENT_MARKER))
        self.assertIn("(dry run)", log_body)
        self.assertIn("would comment", log_body)
        self.assertIn("skipped: confidence LOW", log_body)
        self.assertEqual(log_body.count("<details>"), spc.MAX_POSTS_PER_RUN + 1)

    def test_duplicate_pr_cluster_is_checked_once(self):
        decisions = spc.decide_all(
            [_suspect(), _suspect()], {"R281": [1, 2]}, {}, FakeGitHub(), NOW,
        )
        self.assertEqual(len(decisions), 1)


if __name__ == "__main__":
    unittest.main()
