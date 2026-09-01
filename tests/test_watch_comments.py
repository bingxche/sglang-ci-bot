import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import watch_comments  # noqa: E402


class WatchCommentsTests(unittest.TestCase):
    def test_trigger_stays_amd_bot(self):
        self.assertEqual(watch_comments.BOT_TRIGGER, "@amd-bot")
        self.assertEqual(
            watch_comments.parse_command("@amd-bot ci-status"),
            {"command": "ci-status", "args": ""},
        )
        self.assertIsNone(watch_comments.parse_command("@bingxche ci-status"))

    def test_claim_accepts_old_and_fallback_publishers(self):
        for login in ("amd-bot", "bingxche"):
            response = Mock(status_code=200)
            response.json.return_value = [
                {"content": "rocket", "user": {"login": login}}
            ]
            with patch.object(watch_comments.requests, "get", return_value=response):
                self.assertTrue(
                    watch_comments.has_bot_claimed(
                        "upstream", 123, {"amd-bot", "bingxche"}
                    )
                )

    def test_ci_status_uses_workflow_dispatch_and_preserves_comment(self):
        response = Mock()
        response.raise_for_status.return_value = None
        with patch.object(watch_comments.requests, "post", return_value=response) as post:
            watch_comments.dispatch_ci_status(
                "dispatch-token",
                "bingxche/sglang-ci-bot",
                34502,
                comment_author="requester",
                comment_id=5449078191,
            )

        url = post.call_args.args[0]
        payload = post.call_args.kwargs["json"]
        self.assertEqual(
            url,
            "https://api.github.com/repos/bingxche/sglang-ci-bot/actions/"
            "workflows/ci-status-check.yml/dispatches",
        )
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"]["pr_number"], "34502")
        self.assertEqual(payload["inputs"]["comment_id"], "5449078191")
        self.assertEqual(payload["inputs"]["comment_author"], "requester")

    def test_definitive_dispatch_failure_releases_new_rocket(self):
        comment = {
            "id": 99,
            "body": "@amd-bot ci-status",
            "user": {"login": "bingxche"},
            "issue_url": "https://api.github.com/repos/sgl-project/sglang/issues/7",
            "created_at": "2026-09-01T00:00:00Z",
        }
        with (
            patch.object(watch_comments, "get_recent_comments", return_value=[comment]),
            patch.object(watch_comments, "load_state", return_value={}),
            patch.object(watch_comments, "save_state"),
            patch.object(watch_comments, "is_pull_request", return_value=True),
            patch.object(watch_comments, "has_bot_claimed", return_value=False),
            patch.object(watch_comments, "add_reaction", return_value=(True, 1234)),
            patch.object(
                watch_comments,
                "dispatch_ci_status",
                side_effect=requests.HTTPError("403"),
            ),
            patch.object(watch_comments, "delete_reaction") as delete_reaction,
        ):
            with self.assertRaises(requests.HTTPError):
                watch_comments.process_comments(
                    "upstream",
                    "dispatch",
                    "bingxche/sglang-ci-bot",
                    {"amd-bot", "bingxche"},
                )

        self.assertEqual(delete_reaction.call_args, call("upstream", 99, 1234))


if __name__ == "__main__":
    unittest.main()
