"""Agent instructions: shared rules + one file per task, the model per task, the CLI flags."""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import utils  # noqa: E402

# Linux caps a single argv string at 128 KiB; --append-system-prompt carries the instructions.
MAX_INSTRUCTIONS_BYTES = 120_000


class TaskRegistryTest(unittest.TestCase):
    def test_every_task_the_scripts_send_is_registered(self):
        sent = set()
        for path in (ROOT / "scripts").glob("*.py"):
            sent |= set(re.findall(r'"Task: ([^"\\]+?)(?:\\n)?"', path.read_text()))
        self.assertTrue(sent)
        self.assertEqual(sent - set(utils.AGENT_TASKS), set())

    def test_every_task_file_exists_and_fits_in_one_argument(self):
        for task in utils.AGENT_TASKS:
            text = utils.agent_instructions(task)
            self.assertTrue(text.startswith("# sglang CI Bot"), task)
            self.assertIn("## Ground Rules", text, task)
            self.assertLess(len(text.encode()), MAX_INSTRUCTIONS_BYTES, task)

    def test_each_task_gets_only_its_own_methodology(self):
        text = utils.agent_instructions("PR Code Review")
        self.assertIn("## PR Code Review — Find", text)
        self.assertNotIn("## Job Failure Analysis", text)
        self.assertNotIn("## API Mode Prompts", text)

    def test_unknown_task_fails(self):
        with self.assertRaises(RuntimeError):
            utils.agent_instructions("Something New")

    def test_claude_md_is_short(self):
        self.assertLess(len((ROOT / "agent" / "CLAUDE.md").read_bytes()), 20_000)

    def test_api_templates_moved_with_the_loader(self):
        for name in ("focused-job-analysis", "cross-job-summary", "pr-correlation",
                     "cross-run-pattern-summary", "daily-cross-workflow-summary", "pr-review-api"):
            self.assertTrue(utils.load_prompt_template(name), name)
        self.assertNotIn("Approve / Request Changes", utils.load_prompt_template("pr-review-api"))


class TaskModelTest(unittest.TestCase):
    GATEWAY = {"ANTHROPIC_BASE_URL": "https://gateway.example"}

    def test_default_tasks_use_the_runner_model(self):
        with mock.patch.dict(os.environ, self.GATEWAY, clear=True):
            self.assertIsNone(utils.task_model("Job Failure Analysis"))

    def test_sonnet_task_uses_the_alias_when_the_runner_maps_it(self):
        env = {**self.GATEWAY, "ANTHROPIC_DEFAULT_SONNET_MODEL": "Claude-Sonnet-5.5"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(utils.task_model("Failure Tracker Data"), "sonnet")
            self.assertEqual(utils.task_model("PR Code Review Verification"), "sonnet")

    def test_unmapped_alias_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, self.GATEWAY, clear=True):
            self.assertIsNone(utils.task_model("Failure Tracker Data"))

    def test_env_override(self):
        env = {**self.GATEWAY, "AGENT_MODEL_JOB_FAILURE_ANALYSIS": "Claude-Opus-5.5"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(utils.task_model("Job Failure Analysis"), "Claude-Opus-5.5")


class ClaudeCommandTest(unittest.TestCase):
    def run_agent(self, prompt: str, env: dict) -> list[str]:
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(utils.subprocess, "run", side_effect=fake_run):
            work = Path(tmp)
            utils.claude_code_analyze(prompt, work_dir=work, context_files={"upstream/rules/a.md": "x"})
            self.assertFalse((work / ".ci-context").exists())
        return seen["cmd"]

    def test_instructions_settings_and_model(self):
        env = {"ANTHROPIC_BASE_URL": "https://gw", "ANTHROPIC_DEFAULT_SONNET_MODEL": "S"}
        cmd = self.run_agent("Task: Failure Tracker Data\nDate: x", env)
        system = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("## Ground Rules", system)
        self.assertIn("## Failure Tracker Data", system)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "user")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_default_model_passes_no_flag(self):
        cmd = self.run_agent("Task: Job Failure Analysis\nJob: x", {})
        self.assertNotIn("--model", cmd)

    def test_unknown_task_never_starts_the_cli(self):
        with mock.patch.object(utils.subprocess, "run", side_effect=AssertionError("must not run")):
            with self.assertRaises(RuntimeError):
                utils.claude_code_analyze("no task line", work_dir=Path(tempfile.gettempdir()))


class StaleClaudeMdTest(unittest.TestCase):
    def test_an_old_deployed_copy_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "sglang-wt-1"
            wt.mkdir()
            (Path(tmp) / "CLAUDE.md").write_text("old full instructions")
            utils._remove_deployed_claude_md(wt)
            self.assertFalse((Path(tmp) / "CLAUDE.md").exists())
            utils._remove_deployed_claude_md(wt)  # nothing left: no error


if __name__ == "__main__":
    unittest.main()
