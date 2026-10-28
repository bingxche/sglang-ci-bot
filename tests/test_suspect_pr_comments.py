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
                 statuses=None, comments=None, fixes=None):
        self.merged_at = merged_at
        # fix PR number -> its pull payload
        self.fixes = fixes or {}
        self.merge_sha = merge_sha
        # (base, head) -> status; default puts SHA inside PASS..FAIL.
        self.statuses = statuses or {(PASS, SHA): "ahead", (SHA, FAIL): "ahead"}
        self.comments = comments or []
        self.calls = 0

    def pull(self, pr):
        self.calls += 1
        if pr in self.fixes:
            return self.fixes[pr]
        return {"number": pr, "merged_at": self.merged_at, "merge_commit_sha": self.merge_sha,
                "user": {"login": "pr-author"}}

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

    def _fix(self, state="open", merged_at=None, author="fixer"):
        return {45656: {"number": 45656, "state": state, "merged_at": merged_at,
                        "title": "Fix it", "user": {"login": author}}}

    def test_open_fix_pr_asks_the_author_to_review(self):
        d = self.check(_suspect(fix_pr=45656), gh=FakeGitHub(fixes=self._fix()))
        self.assertEqual((d.result, d.fix["number"], d.pr_author), ("post", 45656, "pr-author"))
        body = spc.render_pr_comment(d, {})
        self.assertTrue(body.startswith(spc.fix_marker(45656)))
        self.assertIn("@pr-author AMD CI jobs started failing", body)
        self.assertIn("#45656 proposes a fix. Could you review it?", body)
        # The author is the only mention; the evidence's are still neutralized.
        self.assertIn("`@someone`", body)
        log_body = spc.render_log_comment([d], "post", "2026-10-28 18:00 UTC")
        self.assertIn("would comment (review request for #45656)", log_body)

    def test_review_request_goes_out_after_an_earlier_suspect_comment(self):
        gh = FakeGitHub(fixes=self._fix(), comments=[{"body": spc.pr_marker("R281"), "html_url": "u"}])
        self.assertEqual(self.check(_suspect(fix_pr=45656), gh=gh).result, "post")

    def test_review_request_is_sent_once_per_fix(self):
        gh = FakeGitHub(fixes=self._fix(), comments=[{"body": spc.fix_marker(45656), "html_url": "u"}])
        d = self.check(_suspect(fix_pr=45656), gh=gh)
        self.assertEqual((d.result, d.reason), ("already", "asked to review #45656"))

    def test_merged_fix_is_skipped(self):
        d = self.check(_suspect(fix_pr=45656), gh=FakeGitHub(fixes=self._fix("closed", "2026-10-22T00:00:00Z")))
        self.assertEqual(d.reason, "fix PR #45656 already merged")

    def test_fix_by_the_pr_author_is_skipped(self):
        d = self.check(_suspect(fix_pr=45656), gh=FakeGitHub(fixes=self._fix(author="PR-Author")))
        self.assertEqual(d.reason, "fix PR #45656 is by the PR's author")

    def test_abandoned_fix_falls_back_to_a_plain_comment(self):
        d = self.check(_suspect(fix_pr=45656), gh=FakeGitHub(fixes=self._fix("closed")))
        self.assertEqual((d.result, d.fix), ("post", None))
        self.assertTrue(spc.render_pr_comment(d, {}).startswith(spc.pr_marker("R281")))

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

    def test_comment_under_an_earlier_cluster_id_is_not_repeated(self):
        # #45791 was R327 at 22:05 and R332 at 23:05 on Oct 27.
        gh = FakeGitHub(comments=[{"body": spc.pr_marker("R327") + "\nhi", "html_url": "u"}])
        d = self.check(_suspect(cluster="R332"), gh=gh)
        self.assertEqual((d.result, d.reason), ("already", "commented earlier as R327"))
        log_body = spc.render_log_comment([d], "post", "2026-10-28 18:00 UTC")
        self.assertIn("[already commented (commented earlier as R327)](u)", log_body)


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
        self.assertIn("Posted automatically by [amd-bot](https://github.com/amd-bot)", body)
        self.assertNotIn("sglang-ci-bot", body)

    def test_mentions(self):
        self.assertEqual(
            spc.neutralize_mentions("ping @a-b and @org/team, not a@b.com or `@c`"),
            "ping `@a-b` and `@org/team`, not a@b.com or `@c`",
        )

    def test_dry_run_holds_over_limit_and_logs(self):
        gh = FakeGitHub()
        suspects = [_suspect(cluster=f"R{i}", pr=42229 + i) for i in range(1, spc.MAX_POSTS_PER_RUN + 2)]
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

    def test_two_clusters_on_one_pr_post_once(self):
        decisions = spc.decide_all(
            [_suspect(cluster="R1"), _suspect(cluster="R2")],
            {"R1": [1, 2], "R2": [1, 2, 3]}, {}, FakeGitHub(), NOW,
        )
        self.assertEqual(
            [(d.suspect["cluster"], d.result) for d in decisions],
            [("R2", "post"), ("R1", "skip")],
        )
        self.assertEqual(decisions[1].reason, "#42229 gets one comment, for R2")
        self.assertEqual(decisions[1].body, "")


if __name__ == "__main__":
    unittest.main()
