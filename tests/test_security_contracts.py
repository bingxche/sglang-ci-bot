import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import utils  # noqa: E402


class SecurityContractTests(unittest.TestCase):
    def test_agent_child_environment_omits_github_and_ssh_credentials(self):
        secret_names = (
            "SGLANG_PAT",
            "GH_PAT",
            "BOT_PAT",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "BOT_REPO_TOKEN",
            "RUNNER_ADMIN_TOKEN",
            "RUNNER_REGISTRATION_TOKEN",
            "SSH_AUTH_SOCK",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
        )
        parent_env = {name: f"secret-{name}" for name in secret_names}
        completed = Mock(returncode=0, stdout="analysis complete", stderr="")

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, parent_env, clear=False
        ), patch.object(utils.subprocess, "run", return_value=completed) as run:
            result = utils.claude_code_analyze(
                "Task: test",
                Path(tmp),
                max_turns=1,
                timeout_secs=1,
            )

        self.assertEqual(result, "analysis complete")
        child_env = run.call_args.kwargs["env"]
        for name in secret_names:
            self.assertNotIn(name, child_env)
        self.assertEqual(child_env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(child_env["GIT_SSH_COMMAND"], "/bin/false")

    def test_workflows_do_not_persist_checkout_credentials_or_use_eval(self):
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = path.read_text()
            self.assertNotIn("eval ", text, path.name)
            self.assertNotIn("secrets.SGLANG_PAT", text, path.name)
            checkout_count = text.count("uses: actions/checkout@")
            self.assertEqual(
                checkout_count,
                text.count("persist-credentials: false"),
                path.name,
            )

    def test_sglang_checkout_has_explicit_push_block(self):
        text = (ROOT / "scripts" / "utils.py").read_text()
        self.assertIn("disabled://sglang-code-write-blocked", text)
        self.assertNotIn('"git", "push"', text)

    def test_no_upstream_code_mutation_endpoint_or_git_push(self):
        runtime_files = list((ROOT / "scripts").glob("*.py"))
        runtime_files += list((ROOT / ".github" / "workflows").glob("*.yml"))
        runtime_files += [ROOT / "runner" / "entrypoint.sh"]
        source = "\n".join(path.read_text() for path in runtime_files)
        for forbidden in (
            "/git/refs",
            "/git/commits",
            "/git/trees",
            "/git/blobs",
            "/merges",
            '"git", "push"',
            "git push ",
        ):
            self.assertNotIn(forbidden, source)

    def test_runner_does_not_pass_long_lived_pat_to_listener(self):
        entrypoint = (ROOT / "runner" / "entrypoint.sh").read_text()
        setup = (ROOT / "runner" / "setup.sh").read_text()
        self.assertIn(
            "unset GH_PAT BOT_PAT SGLANG_PAT BOT_REPO_TOKEN CONTROL_TOKEN",
            entrypoint,
        )
        self.assertLess(entrypoint.index("unset GH_PAT"), entrypoint.index("exec ./run.sh"))
        self.assertNotIn("https://${GH_PAT}@github.com", entrypoint)
        self.assertIn("RUNNER_REGISTRATION_TOKEN", setup)

    def test_runner_restart_reuses_registration_and_drops_bootstrap_token(self):
        entrypoint = (ROOT / "runner" / "entrypoint.sh").read_text()
        self.assertIn("RUNNER_CONFIG_MARKER", entrypoint)
        self.assertIn("Discarding stale runner configuration", entrypoint)
        self.assertIn('touch "${RUNNER_CONFIG_MARKER}"', entrypoint)
        self.assertIn("if [ -f .runner ]; then", entrypoint)
        self.assertLess(
            entrypoint.index("if [ -f .runner ]; then"),
            entrypoint.index("./config.sh"),
        )
        self.assertNotIn("trap cleanup", entrypoint)
        self.assertLess(
            entrypoint.index("unset RUNNER_REGISTRATION_TOKEN RUNNER_TOKEN"),
            entrypoint.index('if [ "${ENABLE_WATCHER:-}" = "true" ]'),
        )

    def test_control_runner_is_not_eligible_for_production_jobs(self):
        setup = (ROOT / "runner" / "setup.sh").read_text()
        self.assertIn('RUNNER_LABELS="self-hosted,amd-control"', setup)
        self.assertIn('RUNNER_LABELS="self-hosted,amd-internal"', setup)
        self.assertIn('-e LABELS="${RUNNER_LABELS}"', setup)
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = path.read_text()
            self.assertNotIn("amd-control", text, path.name)
            for line in text.splitlines():
                if "runs-on:" in line and "self-hosted" in line:
                    self.assertIn("amd-internal", line, path.name)

    def test_watcher_state_uses_persistent_named_volume(self):
        entrypoint = (ROOT / "runner" / "entrypoint.sh").read_text()
        setup = (ROOT / "runner" / "setup.sh").read_text()
        self.assertIn("/var/lib/sglang-ci-bot", entrypoint)
        self.assertIn(
            'WATCHER_STATE_VOLUME="sglang-ci-bot-watcher-state"', setup,
        )
        self.assertIn(
            '${WATCHER_STATE_VOLUME}:/var/lib/sglang-ci-bot', setup,
        )

    def test_production_workflows_force_api_mode(self):
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            text = path.read_text()
            self.assertNotIn("--use-agent", text, path.name)
            self.assertNotIn("USE_AGENT_INPUT", text, path.name)
            self.assertNotIn("inputs.use_agent", text, path.name)
            self.assertNotIn("vars.USE_AGENT", text, path.name)
            self.assertNotRegex(text, r"USE_AGENT:\s*['\"]?true", path.name)
            if any(
                script in text
                for script in (
                    "analyze_url.py",
                    "check_ci_for_pr.py",
                    "failure_tracker.py",
                    "monitor_ci.py",
                    "review_pr.py",
                )
            ):
                self.assertIn("--no-use-agent", text, path.name)


if __name__ == "__main__":
    unittest.main()
