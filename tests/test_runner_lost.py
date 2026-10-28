"""Tests for jobs whose self-hosted runner was lost mid-job.

GitHub fails such a job with "The self-hosted runner lost communication with
the server" and usually keeps no log. On sgl-project/sglang run 40465896895 both
MI355X disagg runners dropped twice and 13 of the 14 failed jobs were this; the
analyses only had a 404 log to go on. The job shapes below are the real ones
from that run.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_evidence as ce  # noqa: E402
import utils  # noqa: E402

DI = "linux-sglang-mi35x-di"
ANNOTATION = (
    "The self-hosted runner lost communication with the server. Verify the machine "
    "is running and has a healthy network connection."
)


def step(n, name, status="completed", conclusion="success"):
    return {"number": n, "name": name, "status": status, "conclusion": conclusion}


SETUP = [step(1, "Set up job"), step(2, "Checkout code"), step(3, "Clean up prior Slurm jobs")]
# Job 122061201003: the running step is left in progress.
LOST_MID_STEP = SETUP + [
    step(4, "Launch MI355X benchmark", "in_progress", None),
    step(5, "Pack logs", "pending", None),
]
# Job 122061199704: same outage, the step list just stops.
LOST_TRUNCATED = list(SETUP)
# Job 122061197952: a real failure -- a step failed.
REAL_FAILURE = SETUP + [step(4, "Launch MI355X benchmark", conclusion="failure"), step(5, "Pack logs")]


def job(job_id, steps, conclusion="failure", runner="pit2-sglang-mi355x-di-01",
        completed_at="2026-10-27T13:07:27Z", name=None):
    return {
        "id": job_id,
        "name": name or f"nightly-mi355x-2n (config-{job_id})",
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-10-27T12:49:26Z",
        "completed_at": completed_at,
        "html_url": f"https://github.com/sgl-project/sglang/actions/runs/1/job/{job_id}",
        "labels": [DI],
        "runner_name": runner,
        "steps": steps,
    }


class IsRunnerLostTest(unittest.TestCase):
    def test_shapes_of_a_lost_runner(self):
        self.assertTrue(utils.is_runner_lost(job(1, LOST_MID_STEP)))
        self.assertTrue(utils.is_runner_lost(job(2, LOST_TRUNCATED)))
        # Job 122206637186: gone before any step was reported -- the most common shape.
        self.assertTrue(utils.is_runner_lost(job(3, [])))

    def test_not_a_lost_runner(self):
        self.assertFalse(utils.is_runner_lost(job(4, REAL_FAILURE)))
        # timeout-minutes cancels the running step.
        self.assertFalse(utils.is_runner_lost(job(5, SETUP + [step(4, "Run test", conclusion="cancelled")])))
        for conclusion in ("success", "cancelled", "skipped"):
            self.assertFalse(utils.is_runner_lost(job(6, LOST_MID_STEP, conclusion=conclusion)))
        # A startup failure never had a runner to lose.
        self.assertFalse(utils.is_runner_lost(job(7, [], runner=None)))


class EvidenceTest(unittest.TestCase):
    def evidence(self, target, run_jobs=None, annotations=None):
        with mock.patch.object(utils, "get_job_annotations", return_value=annotations):
            return utils.runner_lost_evidence("tok", target, run_jobs)

    def test_facts(self):
        text = self.evidence(job(1, LOST_MID_STEP), annotations=[{"message": ANNOTATION}])
        self.assertIn("`pit2-sglang-mi355x-di-01`", text)
        self.assertIn("`Launch MI355X benchmark` (step 4, left in progress)", text)
        self.assertIn("lost communication with the server", text)
        self.assertIn("lost their runner: none", text)

    def test_where_it_stopped(self):
        self.assertIn("before any step was reported", self.evidence(job(1, [])))
        self.assertIn("after `Clean up prior Slurm jobs` (step 3)", self.evidence(job(1, LOST_TRUNCATED)))

    def test_unannotated_and_unreadable(self):
        self.assertIn("GitHub annotation: none", self.evidence(job(1, LOST_TRUNCATED), annotations=[]))
        self.assertNotIn("GitHub annotation", self.evidence(job(1, LOST_TRUNCATED), annotations=None))

    def test_other_lost_jobs_in_the_run(self):
        target = job(1, LOST_MID_STEP)
        run_jobs = [
            target,
            job(3, [], runner="pit2-sglang-mi355x-di-02", completed_at="2026-10-27T13:18:29Z"),
            job(2, LOST_TRUNCATED, runner="pit2-sglang-mi355x-di-02", completed_at="2026-10-27T10:18:19Z"),
            job(4, REAL_FAILURE),
        ]
        text = self.evidence(target, run_jobs)
        self.assertIn("lost their runner: 2", text)
        # Oldest first, the job itself and the real failure left out.
        self.assertLess(text.index("10:18:19"), text.index("13:18:29"))
        self.assertNotIn("/job/1)", text)
        self.assertNotIn("/job/4)", text)


class AnalysisInputTest(unittest.TestCase):
    def test_agent_prompt_carries_the_facts(self):
        prompts = []
        with mock.patch.object(utils, "claude_code_analyze", side_effect=lambda **kw: prompts.append(kw["prompt"]) or "x"), \
                mock.patch.object(utils, "get_job_annotations", return_value=[{"message": ANNOTATION}]):
            utils.analyze_job_with_agent(job(1, LOST_MID_STEP), "https://run/1", Path("."),
                                         github_token="tok", run_jobs=[job(2, [])])
            utils.analyze_job_with_agent(job(3, REAL_FAILURE), "https://run/1", Path("."), github_token="tok")
        self.assertIn("Runner lost: yes\n- Runner: `pit2-sglang-mi355x-di-01`", prompts[0])
        self.assertIn("lost their runner: 1", prompts[0])
        self.assertNotIn("Runner lost", prompts[1])

    def test_api_mode_puts_the_facts_ahead_of_the_log(self):
        logs = []
        with mock.patch.object(utils, "download_job_logs", return_value="[Failed to fetch logs: HTTP 404]"), \
                mock.patch.object(utils, "get_job_annotations", return_value=[]), \
                mock.patch.object(utils, "focused_job_analysis",
                                  side_effect=lambda c, n, u, e, log: logs.append(log) or "x"):
            utils.analyze_job_api(None, "tok", job(1, []), "https://github.com/x/actions/runs/1")
        self.assertTrue(logs[0].startswith("## Runner lost\n"))
        self.assertTrue(logs[0].endswith("[Failed to fetch logs: HTTP 404]"))


class EvidencePackTest(unittest.TestCase):
    def test_lost_job_gets_its_own_mode_and_facts(self):
        lost = job(1, [], name="stage-b-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang, 0)")
        lost["labels"] = ["linux-mi300-1gpu-sglang"]
        run = {"id": 10, "path": ".github/workflows/pr-test-amd.yml", "created_at": "2026-10-27T00:00:00Z",
               "run_attempt": 1, "html_url": "https://run/10", "event": "pull_request",
               "status": "completed", "conclusion": "failure"}

        def paged(token, path, key, **params):
            return [run] if path == "actions/runs" else [lost]

        pr = {"head": {"sha": "a" * 40}, "base": {"ref": "main"}, "title": "t", "user": {"login": "u"},
              "html_url": "https://pr"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ce, "_get", return_value=pr), \
                mock.patch.object(ce, "_pr_diff", return_value=("", [])), \
                mock.patch.object(ce, "_paged", side_effect=paged), \
                mock.patch.object(ce, "_attempt_as_of", side_effect=lambda t, r, a: r), \
                mock.patch.object(ce, "download_job_logs", return_value="[Failed to fetch logs: HTTP 404]"), \
                mock.patch.object(ce, "checkout_commit", return_value=True), \
                mock.patch.object(ce, "_exec_env", return_value={}), \
                mock.patch.object(ce, "_main_runs", return_value=[]), \
                mock.patch.object(utils, "get_job_annotations", return_value=[{"message": ANNOTATION}]):
            pack = ce.build_evidence_pack("tok", 1, Path(tmp))
            md = (Path(tmp) / ".ci-context" / "evidence.md").read_text()
        (failure,) = pack["failures"]
        self.assertEqual(failure["mode"], "runner_lost")
        text = pack["evidence"]["F1.failure"]["text"]
        self.assertTrue(text.startswith("[Failed to fetch logs: HTTP 404]"))
        self.assertIn("The runner was lost mid-job", text)
        self.assertIn("mode: `runner_lost`", md)
        self.assertIn("lost communication with the server", md)


if __name__ == "__main__":
    unittest.main()
