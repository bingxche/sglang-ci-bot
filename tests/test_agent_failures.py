"""Agent mode never falls back to the API: it publishes an "agent did not work" notice."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_url  # noqa: E402
import check_ci_for_pr  # noqa: E402
import daily_cross_workflow_summary as summary  # noqa: E402
import monitor_ci  # noqa: E402
import review_pr  # noqa: E402
import utils  # noqa: E402

NOTICE = "The Claude Code agent did not work"


class NoticeTests(unittest.TestCase):
    def test_notice_names_the_output_and_the_error(self):
        notice = utils.agent_failure_notice("the review", RuntimeError("bad `tick`\nline two"))
        self.assertIn(NOTICE, notice)
        self.assertIn("the review", notice)
        self.assertIn("`bad 'tick' line two`", notice)
        self.assertIn("No API fallback", notice)
        self.assertTrue(all(line.startswith(">") for line in notice.splitlines()))


class PrCommandTests(unittest.TestCase):
    def _assert_notice_posted(self, post, text):
        post.assert_called_once()
        body = post.call_args.args[3]
        self.assertIn(NOTICE, body)
        self.assertIn(text, body)
        self.assertNotIn("using Claude API", body)

    @patch.object(review_pr, "get_pr_info", side_effect=AssertionError("no API-mode path"))
    @patch.object(review_pr, "post_comment", return_value={"html_url": "u"})
    @patch.object(review_pr, "claude_code_available", return_value=False)
    def test_review_without_cli_posts_notice_and_fails(self, _cli, post, _info):
        with self.assertRaises(RuntimeError):
            review_pr.review_pr("token", 7)
        self._assert_notice_posted(post, "Claude Code CLI not found")

    @patch.object(review_pr, "get_pr_info", side_effect=AssertionError("no API-mode path"))
    @patch.object(review_pr, "post_comment", return_value={"html_url": "u"})
    @patch.object(review_pr, "review_pr_with_agent", side_effect=TimeoutError("timed out after 600s"))
    @patch.object(review_pr, "agent_worktree")
    @patch.object(review_pr, "claude_code_available", return_value=True)
    def test_review_agent_error_posts_notice_and_fails(self, _cli, _wt, _agent, post, _info):
        with self.assertRaises(TimeoutError):
            review_pr.review_pr("token", 7)
        self._assert_notice_posted(post, "timed out after 600s")

    @patch.object(check_ci_for_pr, "get_pr_head_sha", side_effect=AssertionError("no API-mode path"))
    @patch.object(check_ci_for_pr, "post_comment", return_value={"html_url": "u"})
    @patch.object(check_ci_for_pr, "_check_ci_with_agent", side_effect=TimeoutError("timed out after 2400s"))
    @patch.object(check_ci_for_pr, "agent_worktree")
    @patch.object(check_ci_for_pr, "claude_code_available", return_value=True)
    def test_ci_status_agent_error_posts_notice_and_fails(self, _cli, _wt, _agent, post, _sha):
        with self.assertRaises(TimeoutError):
            check_ci_for_pr.check_ci_for_pr("token", 7)
        self._assert_notice_posted(post, "timed out after 2400s")
        self.assertIn("## CI Status for PR #7", post.call_args.args[3])


class AnalyzeUrlTests(unittest.TestCase):
    @patch.object(analyze_url, "create_anthropic_client", side_effect=AssertionError("API must not be used"))
    @patch.object(analyze_url, "update_issue_body")
    @patch.object(analyze_url, "create_github_issue", return_value={"number": 5})
    @patch.object(analyze_url, "get_job", return_value={"id": 2, "name": "stage-a-test"})
    @patch.object(analyze_url, "get_run", return_value={
        "head_sha": "abc1234", "html_url": "https://example.test/run", "path": ".github/workflows/x.yml",
    })
    @patch.object(analyze_url, "claude_code_available", return_value=False)
    def test_missing_cli_writes_notice_to_issue(self, _cli, _run, _job, _create, update, _api):
        with self.assertRaises(RuntimeError):
            analyze_url.run_analysis(
                "token", "https://github.com/sgl-project/sglang/actions/runs/1/job/2",
                bot_repo="owner/bot", use_agent=True,
            )
        body = update.call_args.args[3]
        self.assertIn(NOTICE, body)
        self.assertIn("stage-a-test", body)


class DailySummaryTests(unittest.TestCase):
    PREVIOUS = "# Daily Cross-Workflow Summary — 2026-10-27\n\nOLD CLUSTERS"

    def setUp(self):
        self.body = (
            f"{monitor_ci.DAILY_SUMMARY_PLACEHOLDER_START}\n{self.PREVIOUS}\n"
            f"{monitor_ci.DAILY_SUMMARY_PLACEHOLDER_END}\n\n---\n\n## CI Monitor"
        )
        patches = [
            patch.object(summary, "find_daily_issue", return_value=9),
            patch.object(summary, "collect_workflow_analyses", return_value={"a.yml": [{"job_id": 1}]}),
            patch.object(summary, "build_workflows_block", return_value="block"),
            patch.object(summary, "fetch_yesterday_summary", return_value=None),
            patch.object(summary, "run_api", side_effect=AssertionError("API must not be used")),
            patch.object(summary, "get_issue", side_effect=lambda *a: {"body": self.body}),
            patch.object(summary, "update_issue_body", side_effect=self._update),
            patch.object(summary, "_cleanup_legacy_summary_comments"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _update(self, _token, _repo, _issue, body):
        self.body = body

    def _fail_once(self, error: Exception):
        with patch.object(summary, "run_agent", side_effect=error):
            with self.assertRaises(type(error)):
                summary.build_and_publish_summary("token", "owner/bot", date_str="2026-10-27")

    def test_agent_failure_keeps_the_previous_summary_under_a_notice(self):
        self._fail_once(TimeoutError("timed out after 1200s"))
        self.assertIn(NOTICE, self.body)
        self.assertIn("timed out after 1200s", self.body)
        self.assertIn("OLD CLUSTERS", self.body)
        self.assertIn("## CI Monitor", self.body)

        self._fail_once(RuntimeError("Claude Code CLI not found on the runner"))
        self.assertEqual(self.body.count(summary.AGENT_FAILED_MARKER), 1)
        self.assertNotIn("timed out after 1200s", self.body)
        self.assertIn("OLD CLUSTERS", self.body)

    def test_success_drops_the_notice(self):
        self._fail_once(TimeoutError("timed out"))
        fresh = "# Daily Cross-Workflow Summary — 2026-10-27\n\nNEW CLUSTERS"
        with patch.object(summary, "run_agent", return_value=fresh):
            summary.build_and_publish_summary("token", "owner/bot", date_str="2026-10-27")
        self.assertNotIn(summary.AGENT_FAILED_MARKER, self.body)
        self.assertIn("NEW CLUSTERS", self.body)


class MonitorWithoutAgentTests(unittest.TestCase):
    @patch.object(monitor_ci, "monitor_workflow", side_effect=AssertionError("must not analyse"))
    @patch.object(monitor_ci, "find_daily_issue", return_value=9)
    @patch.object(monitor_ci, "claude_code_available", return_value=False)
    def test_missing_cli_fails_every_workflow_and_marks_the_summary(self, _cli, _find, _monitor):
        with patch.object(summary, "mark_summary_stale") as mark:
            failed = monitor_ci.run_oneshot(
                "token", "owner/bot", "daily-issue", ["a.yml", "b.yml"], 24, "main", use_agent=True,
            )
        self.assertEqual(failed, ["a.yml", "b.yml"])
        mark.assert_called_once()
        self.assertIn("Claude Code CLI not found", str(mark.call_args.args[4]))


if __name__ == "__main__":
    unittest.main()
