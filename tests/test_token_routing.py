import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_url  # noqa: E402
import failure_tracker  # noqa: E402
import monitor_ci  # noqa: E402


class TokenRoutingTests(unittest.TestCase):
    def test_analyze_reads_upstream_and_writes_bot_repo(self):
        run = {
            "html_url": "https://github.com/sgl-project/sglang/actions/runs/1",
            "head_sha": "abcdef0123456789",
            "path": ".github/workflows/test.yml",
            "run_number": 4,
        }
        with (
            patch.object(analyze_url, "get_run", return_value=run) as get_run,
            patch.object(analyze_url, "get_failed_jobs", return_value=[]),
            patch.object(analyze_url, "create_github_issue", return_value={"number": 9}) as create_issue,
        ):
            analyze_url.run_analysis(
                "upstream-token",
                "bot-token",
                run["html_url"],
                bot_repo="bingxche/sglang-ci-bot",
                use_agent=False,
            )

        get_run.assert_called_once_with("upstream-token", 1)
        self.assertEqual(create_issue.call_args.args[0], "bot-token")

    def test_monitor_routes_each_repository_to_its_token(self):
        with (
            patch.object(monitor_ci, "load_state", return_value={}),
            patch.object(monitor_ci, "save_state"),
            patch.object(monitor_ci, "find_daily_issue", return_value=None) as find_daily,
            patch.object(monitor_ci, "monitor_workflow", return_value=([], set(), False)) as monitor,
        ):
            monitor_ci.run_oneshot(
                "upstream-token",
                "bot-token",
                "bingxche/sglang-ci-bot",
                "daily-issue",
                ["nightly-test-amd.yml"],
                24,
                "main",
                use_agent=False,
            )

        self.assertEqual(find_daily.call_args.args[0], "bot-token")
        self.assertEqual(monitor.call_args.args[:2], ("upstream-token", "nightly-test-amd.yml"))

    def test_failure_tracker_reads_daily_issue_with_bot_token(self):
        with patch.object(
            failure_tracker, "find_daily_issue", return_value=None
        ) as find_daily:
            result = failure_tracker.update_trackers(
                "upstream-token",
                "bot-token",
                "bingxche/sglang-ci-bot",
                date_str="2026-09-01",
                use_agent=False,
            )

        self.assertEqual(result, {})
        find_daily.assert_called_once_with(
            "bot-token", "bingxche/sglang-ci-bot", "2026-09-01"
        )


if __name__ == "__main__":
    unittest.main()
