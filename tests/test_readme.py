"""The README is the map of this repo; these checks keep it from drifting from the code.

They fail when the README links to a file that does not exist, names a function
that is not defined (in the file it links to), or leaves out a script, workflow,
agent task, or environment variable the code uses.
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import monitor_ci  # noqa: E402
import utils  # noqa: E402

README = (ROOT / "README.md").read_text(encoding="utf-8")
SCRIPTS = sorted((ROOT / "scripts").glob("*.py"))

# Read by code but not ours to document.
ENV_NOT_DOCUMENTED = {
    "GITHUB_OUTPUT",  # set by GitHub Actions
    "FOO",            # an example inside a prompt string in utils.py
}

_LINK_RE = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
_LINKED_FUNC_RE = re.compile(r"\[`(?:\w+\.)?(\w+)\(\)`\]\(([^)#\s]+)\)")
_FUNC_RE = re.compile(r"`(?:\w+\.)?(\w+)\(\)`")
_ENV_RE = re.compile(
    r"""(?:environ\.get|environ\[|getenv|_env_int)\(?\s*["']([A-Z][A-Z0-9_]{2,})["']"""
)


def _defined(name: str, files: list[Path]) -> bool:
    pattern = re.compile(rf"^\s*(?:def|class) {re.escape(name)}\b", re.MULTILINE)
    return any(pattern.search(f.read_text(encoding="utf-8")) for f in files)


class ReadmeLinksTest(unittest.TestCase):
    def test_relative_links_point_at_existing_files(self):
        missing = sorted({
            target for target in _LINK_RE.findall(README)
            if not target.startswith(("http://", "https://", "mailto:"))
            and not (ROOT / target).exists()
        })
        self.assertEqual(missing, [], "README links to files that do not exist")

    def test_linked_functions_are_defined_in_the_linked_file(self):
        wrong = sorted({
            f"{name}() -> {target}" for name, target in _LINKED_FUNC_RE.findall(README)
            if not _defined(name, [ROOT / target])
        })
        self.assertEqual(wrong, [], "README links a function to a file that does not define it")

    def test_every_named_function_exists(self):
        missing = sorted({name for name in _FUNC_RE.findall(README) if not _defined(name, SCRIPTS)})
        self.assertEqual(missing, [], "README names functions that are not defined in scripts/")


class ReadmeCoverageTest(unittest.TestCase):
    def test_every_script_workflow_and_task_file_is_mentioned(self):
        names = [p.name for p in SCRIPTS]
        names += [p.name for p in (ROOT / ".github" / "workflows").glob("*.yml")]
        names += [p.name for p in (ROOT / "agent" / "tasks").glob("*.md")]
        self.assertEqual([n for n in names if n not in README], [], "not mentioned in README")

    def test_every_agent_task_is_documented(self):
        self.assertEqual([t for t in utils.AGENT_TASKS if t not in README], [])

    def test_every_environment_variable_is_documented(self):
        used = set()
        for path in SCRIPTS:
            used |= set(_ENV_RE.findall(path.read_text(encoding="utf-8")))
        missing = sorted(v for v in used - ENV_NOT_DOCUMENTED if f"`{v}`" not in README)
        self.assertEqual(missing, [], "environment variables missing from the README")

    def test_monitored_workflows_match_the_code_and_the_workflow(self):
        block = re.search(r"\[`MONITORED_WORKFLOWS`\][^\n]*\n\n```\n(.*?)```", README, re.S)
        self.assertIsNotNone(block, "README lost the monitored-workflows list")
        self.assertEqual(block.group(1).split(), list(monitor_ci.MONITORED_WORKFLOWS))
        yml = (ROOT / ".github" / "workflows" / "ci-monitor.yml").read_text(encoding="utf-8")
        defaults = re.search(r"DEFAULT_WORKFLOWS: \|\n(.*?)\n\njobs:", yml, re.S).group(1).split()
        self.assertEqual(defaults, list(monitor_ci.MONITORED_WORKFLOWS))


if __name__ == "__main__":
    unittest.main()
