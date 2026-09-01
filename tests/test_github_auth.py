import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import github_auth  # noqa: E402


class GitHubAuthTests(unittest.TestCase):
    def test_legacy_env_is_accepted_for_single_token_rollout(self):
        with patch.dict(os.environ, {"SGLANG_PAT": "", "GH_PAT": "legacy"}, clear=False):
            self.assertEqual(github_auth.sglang_token_from_env(), "legacy")
            self.assertEqual(github_auth.bot_repo_token_from_env(), "legacy")

    def test_github_token_is_preferred_for_bot_repo(self):
        env = {
            "SGLANG_PAT": "upstream",
            "GH_PAT": "legacy",
            "GITHUB_TOKEN": "actions-token",
            "BOT_REPO_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(github_auth.bot_repo_token_from_env(), "actions-token")

    def test_classic_bingxche_token_passes_identity_check(self):
        response = Mock()
        response.json.return_value = {"login": "bingxche"}
        response.headers = {"X-OAuth-Scopes": "repo, workflow"}
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response

        self.assertEqual(
            github_auth.validate_sglang_token(
                "ghp_emergency", expected_login="bingxche", session=session
            ),
            "bingxche",
        )

    def test_wrong_actor_is_rejected(self):
        response = Mock()
        response.json.return_value = {"login": "someone-else"}
        response.headers = {}
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response

        with self.assertRaisesRegex(ValueError, "expected 'bingxche'"):
            github_auth.validate_sglang_token(
                "token", expected_login="bingxche", session=session
            )

    def test_single_token_mode_is_allowed(self):
        github_auth.require_distinct_tokens("same", "same")


if __name__ == "__main__":
    unittest.main()
