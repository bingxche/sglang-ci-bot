import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import utils  # noqa: E402


MILES_SHA = "af4413b74f33f072883b6fbc760cccba3d56160d"
JOB_ID = 117741957893


def _payload() -> dict:
    """The shape of the dashboard's api/miles/cause-fix.json, trimmed."""
    return {
        "available": True,
        "rows": [
            {
                "id": "test:tests/e2e/ft/test_kill_train.py#cde26034",
                "state": "failing",
                "kind": "test",
                "error": "AssertionError: main_param is None",
                "tests": [{
                    "file": "tests/e2e/ft/test_kill_train.py",
                    "status": "failed",
                    "error": "AssertionError: main_param is None",
                    "exit_code": 1,
                }],
                "jobs": [{
                    "id": JOB_ID,
                    "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261015",
                    "commits": {"radixark/miles": MILES_SHA, "sgl-project/sglang": "7b6993a310"},
                    "refs": {"radixark/miles": "main", "sgl-project/sglang": "sglang-miles"},
                }],
                "frames": [{
                    "repo": "radixark/miles",
                    "path": "miles/backends/megatron_utils/model.py",
                    "line": 120,
                    "func": "train_one_step",
                    "code": "loss = step(batch)",
                }],
                "since": [{
                    "label": "MI35X",
                    "first_failed": {"date": "2026-10-15", "job_url": "https://example.test/job/1",
                                     "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261015"},
                    "last_passed": {"date": "2026-10-14", "job_url": "https://example.test/job/0",
                                    "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261014"},
                    "failed_runs": 4,
                    "lower_bound": False,
                    "windows": [{
                        "repo": "radixark/miles",
                        "base": "a87843494f4083b8be6d575edabf7e0f7ad6f5bc",
                        "head": MILES_SHA,
                        "commits": 81,
                        "url": "https://github.com/radixark/miles/compare/a87843494f40...af4413b74f33",
                        "ref": "main",
                    }],
                }],
                "cause_prs": [{
                    "repo": "radixark/miles", "number": 3423,
                    "sha": "dd1545c31fbb29fc2e70edd86c73ff2e03ddbe12",
                    "url": "https://github.com/radixark/miles/pull/3423",
                    "commit_url": "https://github.com/radixark/miles/commit/dd1545c31fbb",
                    "title": "Keep the weight version with the Megatron model state",
                    "reason": "changed train_one_step() in model.py, on the traceback",
                    "strong": True, "pr_state": "merged",
                }],
                "fix_prs": [],
            },
            {
                "id": "test:tests/other.py#00000000",
                "state": "recovered",
                "kind": "test",
                "error": "x",
                "tests": [],
                "jobs": [{"id": 1}],
                "since": [],
            },
        ],
    }


class MilesWorkflowTests(unittest.TestCase):
    def test_miles_workflows_are_recognised_by_file_name(self):
        self.assertTrue(utils.is_miles_workflow("nightly-test-amd-miles-rocm10.yml"))
        self.assertTrue(utils.is_miles_workflow(
            ".github/workflows/release-docker-amd-miles-rocm724-nightly.yml"
        ))
        self.assertFalse(utils.is_miles_workflow("nightly-test-amd.yml"))

    def test_rows_and_image_of_a_job(self):
        rows = utils.miles_rows_for_job(_payload(), JOB_ID)
        self.assertEqual([r["state"] for r in rows], ["failing"])
        image = utils.miles_job_image(rows, JOB_ID)
        self.assertEqual(image["image"], "rocm/sgl-dev:miles-rocm10-mi35x-20261015")
        self.assertEqual(image["commits"]["radixark/miles"], MILES_SHA)
        self.assertEqual(image["refs"]["sgl-project/sglang"], "sglang-miles")
        self.assertEqual(utils.miles_rows_for_job(None, JOB_ID), [])
        self.assertEqual(utils.miles_job_image([], JOB_ID)["commits"], {})


class EvidenceTests(unittest.TestCase):
    JOB = {"id": JOB_ID, "name": "test-miles-mi35x (nightly-stage-c-8-gpu-mi350)",
           "html_url": "https://example.test/job"}

    def test_evidence_names_the_code_window_and_candidates(self):
        payload = _payload()
        rows = utils.miles_rows_for_job(payload, JOB_ID)
        text = utils.render_miles_evidence(
            self.JOB, rows, utils.miles_job_image(rows, JOB_ID),
            {"miles_src": ".ci-context/miles/src", "sglang_src": "."}, payload,
        )
        self.assertIn("rocm/sgl-dev:miles-rocm10-mi35x-20261015", text)
        self.assertIn(f"https://github.com/radixark/miles/commit/{MILES_SHA}", text)
        self.assertIn("checked out at `.ci-context/miles/src`", text)
        self.assertIn("(branch `sglang-miles`) — checked out at `.`", text)
        self.assertIn("`radixark/miles:miles/backends/megatron_utils/model.py:120` in `train_one_step`", text)
        self.assertIn("failing since [2026-10-15](https://example.test/job/1)", text)
        self.assertIn("81 commit(s)", text)
        self.assertIn("[radixark/miles#3423](https://github.com/radixark/miles/pull/3423)", text)
        self.assertIn("(strong, merged): changed train_one_step()", text)
        self.assertNotIn("other.py", text)  # another job's row

    def test_evidence_says_what_is_missing(self):
        text = utils.render_miles_evidence(
            self.JOB, [], {"image": "", "commits": {}, "refs": {}}, {}, None,
        )
        self.assertIn("Image: unknown", text)
        self.assertIn("radixark/miles: unknown", text)
        self.assertIn("payload could not be fetched", text)
        text = utils.render_miles_evidence(
            self.JOB, [], {"image": "", "commits": {}, "refs": {}}, {}, {"rows": []},
        )
        self.assertIn("no row for this job yet", text)


class FetchPayloadTests(unittest.TestCase):
    def setUp(self):
        utils._miles_payload.clear()

    def test_payload_is_fetched_once_and_unavailable_is_none(self):
        ok = Mock()
        ok.json.return_value = {"available": True, "rows": []}
        with patch("utils.requests.get", return_value=ok) as get:
            self.assertEqual(utils.fetch_miles_cause_fix("u1"), {"available": True, "rows": []})
            utils.fetch_miles_cause_fix("u1")
        self.assertEqual(get.call_count, 1)

        off = Mock()
        off.json.return_value = {"available": False}
        with patch("utils.requests.get", return_value=off):
            self.assertIsNone(utils.fetch_miles_cause_fix("u2"))
        with patch("utils.requests.get", side_effect=utils.requests.ConnectionError("down")):
            self.assertIsNone(utils.fetch_miles_cause_fix("u3"))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


class CheckoutTests(unittest.TestCase):
    """The checkouts against throwaway local repositories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _repo(self, name: str, commits: int) -> tuple[Path, list[str]]:
        repo = self.root / name
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        shas = []
        for i in range(commits):
            (repo / "f.txt").write_text(str(i))
            _git(repo, "add", "f.txt")
            _git(repo, "commit", "-q", "-m", f"c{i}")
            shas.append(_git(repo, "rev-parse", "HEAD"))
        return repo, shas

    def test_miles_worktree_at_the_image_commit(self):
        repo, shas = self._repo("miles", 3)
        dest = self.root / "wt" / ".ci-context" / "miles" / "src"
        with patch("utils.ensure_miles_repo", return_value=repo):
            self.assertTrue(utils.add_miles_worktree(dest, shas[1]))
        self.assertEqual((dest / "f.txt").read_text(), "1")

    def test_sglang_moves_to_an_abbreviated_commit_on_its_branch(self):
        origin, _ = self._repo("origin", 1)
        _git(origin, "checkout", "-q", "-b", "sglang-miles")
        (origin / "f.txt").write_text("miles branch")
        _git(origin, "commit", "-q", "-am", "on the branch")
        branch_sha = _git(origin, "rev-parse", "HEAD")
        _git(origin, "checkout", "-q", "main")

        clone = self.root / "clone"
        _git(self.root, "clone", "-q", "--single-branch", "--branch", "main",
             origin.as_uri(), str(clone))
        self.assertTrue(utils.checkout_sglang_for_miles(clone, branch_sha[:10], "sglang-miles"))
        self.assertEqual((clone / "f.txt").read_text(), "miles branch")
        self.assertFalse(utils.checkout_sglang_for_miles(clone, "0" * 10, ""))

    def test_sglang_commit_dropped_by_a_force_push_is_fetched_by_sha(self):
        origin, _ = self._repo("origin", 1)
        _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        _git(origin, "checkout", "-q", "-b", "sglang-miles")
        (origin / "f.txt").write_text("before the force push")
        _git(origin, "commit", "-q", "-am", "dropped later")
        dropped = _git(origin, "rev-parse", "HEAD")
        _git(origin, "reset", "-q", "--hard", "main")  # the force push

        clone = self.root / "clone"
        _git(self.root, "clone", "-q", "--single-branch", "--branch", "main",
             origin.as_uri(), str(clone))
        found = Mock(status_code=200)
        found.json.return_value = {"sha": dropped}
        with patch("utils.requests.get", return_value=found) as lookup:
            self.assertTrue(utils.checkout_sglang_for_miles(
                clone, dropped[:10], "sglang-miles", token="t",
            ))
        self.assertIn(f"/commits/{dropped[:10]}", lookup.call_args.args[0])
        self.assertEqual((clone / "f.txt").read_text(), "before the force push")


class PromptTests(unittest.TestCase):
    def test_miles_job_prompt_points_at_the_evidence_and_sources(self):
        job = {"id": JOB_ID, "name": "test-miles-mi35x", "steps": []}
        context = {
            "evidence": ".ci-context/miles/EVIDENCE.md",
            "image": "rocm/sgl-dev:miles-rocm10-mi35x-20261015",
            "miles_src": ".ci-context/miles/src", "miles_commit": MILES_SHA,
            "sglang_src": ".", "sglang_commit": "7b6993a310",
        }
        with (
            patch("utils.prepare_miles_context", return_value=context),
            patch("utils.claude_code_analyze", return_value="### Commit Info") as analyze,
        ):
            utils.analyze_job_with_agent(
                job, "https://example.test/run", Path("."),
                "nightly-test-amd-miles-rocm10.yml", head_sha="abc",
            )
        prompt = analyze.call_args.kwargs["prompt"]
        self.assertIn("Miles evidence: .ci-context/miles/EVIDENCE.md", prompt)
        self.assertIn(f"Miles source: .ci-context/miles/src (radixark/miles @ {MILES_SHA})", prompt)
        self.assertIn("sglang source: current directory (sgl-project/sglang @ 7b6993a310", prompt)

    def test_a_failed_context_still_runs_the_agent(self):
        job = {"id": JOB_ID, "name": "test-miles-mi35x", "steps": []}
        with (
            patch("utils.prepare_miles_context", side_effect=RuntimeError("boom")),
            patch("utils.claude_code_analyze", return_value="x") as analyze,
        ):
            utils.analyze_job_with_agent(
                job, "https://example.test/run", Path("."),
                "nightly-test-amd-miles-rocm10.yml",
            )
        prompt = analyze.call_args.kwargs["prompt"]
        self.assertIn("Miles evidence: unavailable", prompt)
        self.assertIn("NOT the image's sglang", prompt)

    def test_other_workflows_get_no_miles_lines(self):
        job = {"id": 1, "name": "j", "steps": []}
        with (
            patch("utils.prepare_miles_context") as prepare,
            patch("utils.claude_code_analyze", return_value="x") as analyze,
        ):
            utils.analyze_job_with_agent(job, "https://example.test/run", Path("."), "pr-test-amd.yml")
        prepare.assert_not_called()
        self.assertNotIn("Miles", analyze.call_args.kwargs["prompt"])


if __name__ == "__main__":
    unittest.main()
