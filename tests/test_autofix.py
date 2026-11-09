"""ci-autofix: what it picks, how it re-runs and checks a fix, and what it writes."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import autofix  # noqa: E402
import autofix_agents as af_agents  # noqa: E402
import autofix_config as af_config  # noqa: E402
import autofix_github as af_github  # noqa: E402
import autofix_ci as af_ci  # noqa: E402
import autofix_select as af_select  # noqa: E402
import autofix_pr as af_pr  # noqa: E402
import autofix_state as af_state  # noqa: E402
import utils  # noqa: E402

NOW = datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc)

PR_TEST_YAML = """
on:
  workflow_dispatch:
    inputs:
      target_stage:
jobs:
  stage-b-test-1-gpu-small-amd:
    if: contains(format(',{0},', inputs.target_stage || inputs.target_stage_select), ',stage-b-test-1-gpu-small-amd,')
  stage-c-test-large-8-gpu-amd-mi35x:
    if: contains(format(',{0},', inputs.target_stage), ',stage-c-test-large-8-gpu-amd-mi35x,')
"""
EXTRA_YAML = """
on:
  workflow_dispatch:
jobs:
  extra-a-test-1-gpu-large-amd:
    runs-on: x
"""
NIGHTLY_YAML = """
jobs:
  nightly-accuracy-2-gpu-vlm:
    if: contains(format(',{0},', inputs.job_filter || inputs.job_select), ',nightly-accuracy-2-gpu-vlm,')
"""
DISAGG_YAML = "on:\n  workflow_dispatch:\n    inputs:\n      configs:\n"
YAMLS = {"pr-test-amd.yml": PR_TEST_YAML, "pr-test-amd-extra.yml": EXTRA_YAML,
         "nightly-test-amd.yml": NIGHTLY_YAML, "nightly-amd-mi355x-disagg.yml": DISAGG_YAML}

# sgl-project/sglang .github/pull_request_template.md, comments shortened.
TEMPLATE = """<!-- Thank you for your contribution! -->

<!-- PR title: [Type] scope: title -->

## Motivation

<!-- Describe the purpose and goals of this pull request. -->

## Modifications

<!-- Detail the changes made in this pull request. -->

## Accuracy Tests

<!-- If this pull request affects model outputs, provide accuracy test results. -->

## Speed Tests and Profiling

<!-- If this pull request impacts inference speed, provide benchmarking and profiling results. -->

## Checklist

- [ ] Format your code according to the [Format code with pre-commit](https://docs.sglang.io/x).
- [ ] Add unit tests according to the [Run and add unit tests](https://docs.sglang.io/y).
- [ ] Update documentation according to [Write documentations](https://docs.sglang.io/z).

## Review and Merge Process

1. Ping Merge Oncalls to start the process.
2. Get approvals from CODEOWNERS and other reviewers.
"""

SMALL = "stage-b-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang, 3)"
TEST_FILE = "test/registered/amd/test_minimax_hd128_asm_prefill.py"
TEST_FUNC = "TestMiniMaxHD128AsmPrefill.test_first_chunk_reads_fp8_pool"


def ci_log(file_result: str, func_line: str, retried: bool = False) -> str:
    """A run_suite job log, shaped like the real ones (timestamps, Begin/End, summary)."""
    seg = [
        ".", ".", "Begin (8/90):", f"python3 /sglang-checkout/{TEST_FILE}", ".", ".",
        func_line, "", "-" * 70, "Ran 1 test in 5.469s", "", file_result, ".", ".",
        "End (8/90):", f"filename='/sglang-checkout/{TEST_FILE}', elapsed=8", ".", ".",
    ]
    lines = ["Begin (0/90):", "python3 /sglang-checkout/test/registered/a/test_other.py",
             "Ran 3 tests in 1s", "", "OK"]
    lines += seg
    if retried:
        lines += [f"[CI Retry] Attempt 2/2 for /sglang-checkout/{TEST_FILE}"] + seg
    lines += ["Test Summary: 1/2 passed", "✓ PASSED:", "  test/registered/a/test_other.py"]
    return "\n".join(f"2026-11-07T01:33:53.4850848Z {line}" for line in lines)


FAILED_LOG = ci_log("FAILED (errors=1)",
                    "ERROR: test_first_chunk_reads_fp8_pool (__main__.TestMiniMaxHD128AsmPrefill.test_first_chunk_reads_fp8_pool)")
PASSED_LOG = ci_log("OK", "")


class JobNameAndRecipeTest(unittest.TestCase):
    yaml = staticmethod(YAMLS.get)

    def test_job_names(self):
        j = af_ci.parse_job_name("call-pr-test-amd-extra (rocm720) / extra-a-test-1-gpu-large-amd "
                                   "(rocm720, linux-mi300-1gpu-sglang, 0)")
        self.assertEqual((j.caller, j.base, j.rocm, j.runner),
                         ("call-pr-test-amd-extra", "extra-a-test-1-gpu-large-amd", "rocm720", "linux-mi300-1gpu-sglang"))
        j = af_ci.parse_job_name("nightly-test-1-gpu-unit (rocm720, linux-mi300-1gpu-sglang) part 1")
        self.assertEqual((j.base, j.rocm), ("nightly-test-1-gpu-unit", "rocm720"))
        # Partitions are one family; ROCm versions are not.
        self.assertEqual(af_ci.job_family(SMALL), af_ci.job_family(SMALL.replace(", 3)", ", 5)")))
        self.assertNotEqual(af_ci.job_family(SMALL), af_ci.job_family(SMALL.replace("rocm10", "rocm720")))

    def test_pr_test_stage(self):
        r, _ = autofix.recipe_for("pr-test-amd.yml", SMALL, self.yaml)
        self.assertEqual(af_ci.dispatch_inputs([r]), {
            "rocm_version": "rocm10", "runner_arch": "mi300", "continue_on_error": "true",
            "target_stage": "stage-b-test-1-gpu-small-amd"})

    def test_stages_of_one_rocm_share_one_dispatch(self):
        a, _ = autofix.recipe_for("pr-test-amd.yml", SMALL, self.yaml)
        b, _ = autofix.recipe_for("pr-test-amd.yml", "stage-c-test-large-8-gpu-amd-mi35x (rocm10, linux-mi35x-gpu-8, 1)", self.yaml)
        c, _ = autofix.recipe_for("pr-test-amd.yml", SMALL.replace("rocm10", "rocm720"), self.yaml)
        self.assertNotEqual(a.group, c.group)
        # mi35x runners take no runner_arch, so it is a separate dispatch from the mi300 one.
        self.assertNotEqual(a.group, b.group)
        self.assertEqual(af_ci.dispatch_inputs([a, a])["target_stage"], "stage-b-test-1-gpu-small-amd")

    def test_extra_suite_goes_to_its_own_workflow(self):
        r, _ = autofix.recipe_for("pr-test-amd.yml", "call-pr-test-amd-extra (rocm10) / extra-a-test-1-gpu-large-amd "
                                  "(rocm10, linux-mi300-1gpu-sglang, 0)", self.yaml)
        self.assertEqual(r.workflow, "pr-test-amd-extra.yml")
        self.assertEqual(af_ci.dispatch_inputs([r])["rocm_version"], "rocm10")

    def test_nightly_and_disagg(self):
        r, _ = autofix.recipe_for("nightly-test-amd.yml", "nightly-accuracy-2-gpu-vlm (rocm724, linux-mi300-2gpu-sglang)",
                                  self.yaml)
        self.assertEqual(af_ci.dispatch_inputs([r]), {"rocm_version": "rocm724", "continue_on_error": "true",
                                                         "job_filter": "nightly-accuracy-2-gpu-vlm"})
        r, _ = autofix.recipe_for("nightly-amd-mi355x-disagg.yml",
                                  "nightly-mi355x-2n (glm52-fp4-1k1k-1p1d, glm52-fp4-mi355x-sglang, amd/GLM)", self.yaml)
        self.assertEqual(af_ci.dispatch_inputs([r]), {"configs": "glm52-fp4-1k1k-1p1d"})

    def test_no_recipe(self):
        for wf, name in (("nightly-test-amd-miles-rocm10.yml", "test-miles-mi35x (x)"),
                         ("release-docker-amd-rocm10.yml", "publish-gfx1250-rocm101"),
                         ("pr-test-amd.yml", "stage-z-gone (rocm10, linux-mi300-1gpu-sglang)"),
                         ("nightly-test-amd.yml", "nightly-8-gpu-grok2 (rocm10, linux-mi300-8gpu-sglang)")):
            r, why = autofix.recipe_for(wf, name, self.yaml)
            self.assertIsNone(r, name)
            self.assertTrue(why, name)


class InfraTest(unittest.TestCase):
    def test_infrastructure_errors(self):
        for err, kind in (
            ("The self-hosted runner lost communication with the server", "runner lost"),
            ("OSError: [Errno 28] No space left on device", "disk full"),
            ("RuntimeError: hipErrorNoDevice", "GPU unavailable or broken"),
            ("401 Client Error: Unauthorized for url: https://huggingface.co/api/models/x", "Hugging Face download"),
            ("requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='huggingface.co', port=443)", "network"),
            ("docker: Error response from daemon: manifest unknown", "image pull or build"),
        ):
            self.assertEqual(af_ci.classify_infra([err]), kind, err)

    def test_code_errors_are_not_infrastructure(self):
        for err in ("AttributeError: 'types.SimpleNamespace' object has no attribute 'layer_router'",
                    "Memory access fault by GPU node-2 on address 0x7f",
                    "AssertionError: 0.61 not greater than 0.65",
                    # The test's own server died: a bug, not the network.
                    "requests.exceptions.ConnectionError: [Errno 111] Connection refused",
                    "requests.exceptions.ReadTimeout: HTTPConnectionPool(host='127.0.0.1', port=30000)"):
            self.assertIsNone(af_ci.classify_infra([err]), err)

    def test_performance(self):
        self.assertTrue(af_ci.is_perf(["nightly-perf-2-gpu-text (rocm10)"], []))
        self.assertTrue(af_ci.is_perf(["x"], ["AssertionError: output throughput 812 tok/s < 900"]))
        self.assertFalse(af_ci.is_perf([SMALL], ["AttributeError: x"]))
        self.assertFalse(af_ci.is_perf(["jit-kernel-benchmark-test-amd (rocm10)"], ["ImportError: x"]))


class LogParsingTest(unittest.TestCase):
    def test_failed_function(self):
        o = af_ci.test_outcome(FAILED_LOG, TEST_FILE, TEST_FUNC)
        self.assertEqual((o.status, o.retried), ("failed", False))

    def test_passed_file_with_no_named_function(self):
        o = af_ci.test_outcome(PASSED_LOG, TEST_FILE, TEST_FUNC)
        self.assertEqual((o.status, o.retried), ("passed", False))

    def test_retry_is_seen(self):
        log = ci_log("OK", "", retried=True)
        o = af_ci.test_outcome(log, TEST_FILE, TEST_FUNC)
        self.assertEqual((o.status, o.retried), ("passed", True))

    def test_skips_make_a_silent_pass_unknown(self):
        o = af_ci.test_outcome(ci_log("OK (skipped=1)", ""), TEST_FILE, TEST_FUNC)
        self.assertEqual(o.status, "unknown")

    def test_verbose_unittest_and_skips(self):
        line = "test_first_chunk_reads_fp8_pool (__main__.TestMiniMaxHD128AsmPrefill.test_first_chunk_reads_fp8_pool) ... skipped 'no kernel'"
        self.assertEqual(af_ci.test_outcome(ci_log("OK (skipped=1)", line), TEST_FILE, TEST_FUNC).status, "skipped")
        line = "test_first_chunk_reads_fp8_pool (__main__.TestMiniMaxHD128AsmPrefill.test_first_chunk_reads_fp8_pool) ... ok"
        self.assertEqual(af_ci.test_outcome(ci_log("OK", line), TEST_FILE, TEST_FUNC).status, "passed")

    def test_set_up_class(self):
        log = ci_log("FAILED (errors=1)", "ERROR: setUpClass (__main__.TestTorchCompile)")
        self.assertEqual(af_ci.test_outcome(log, TEST_FILE, "TestTorchCompile.setUpClass").status, "failed")
        self.assertEqual(af_ci.test_outcome(PASSED_LOG, TEST_FILE, "setUpClass").status, "passed")

    def test_pytest_status_on_a_later_line(self):
        log = "\n".join([
            "sglang/multimodal_gen/test/server/test_server_2_gpu.py::TestDiffusionServerTwoGpu::test_diffusion_generation[wan2_1_lora] ",
            "[server-test] FAILED during server startup: wan2_1_lora",
            "FAILED",
            "sglang/multimodal_gen/test/server/test_server_2_gpu.py::TestDiffusionServerTwoGpu::test_diffusion_generation[qwen] ",
            "PASSED",
        ])
        f = "python/sglang/multimodal_gen/test/server/test_server_2_gpu.py"
        self.assertEqual(af_ci.test_outcome(log, f, "test_diffusion_generation[wan2_1_lora]").status, "failed")
        self.assertEqual(af_ci.test_outcome(log, f, "TestDiffusionServerTwoGpu::test_diffusion_generation[qwen]").status,
                         "passed")

    def test_file_that_did_not_run(self):
        self.assertEqual(af_ci.test_outcome(FAILED_LOG, "test/registered/x/test_absent.py", "test_a").status, "not-run")

    def test_unrelated_failures(self):
        log = FAILED_LOG + "\n2026-11-07T01:00:00Z ✗ FAILED: /sglang-checkout/test/registered/b/test_b.py returned exit code 1"
        self.assertEqual(af_ci.unrelated_failures(log, [(TEST_FILE, TEST_FUNC)]), ["test/registered/b/test_b.py"])


class HistoryTest(unittest.TestCase):
    def test_fails_for_sure_needs_two_failed_runs_in_a_row(self):
        self.assertTrue(af_ci.fails_for_sure(["failed", "failed", "passed"]))
        self.assertFalse(af_ci.fails_for_sure(["failed", "passed", "failed"]))
        self.assertFalse(af_ci.fails_for_sure(["failed"]))
        self.assertFalse(af_ci.fails_for_sure(["passed", "failed", "failed"]))

    def test_failure_window(self):
        runs = [("failed", {"date": "11-09"}), ("failed", {"date": "11-08"}), ("passed", {"date": "11-07"})]
        ff, lp, n = af_ci.failure_window(runs)
        self.assertEqual((ff["date"], lp["date"], n), ("11-08", "11-07", 2))


def plan(pid: str, jobs: int, since: str) -> dict:
    return {"id": pid, "name": f"problem {pid}", "failed_jobs": jobs, "failing_since": since}


class HourlyCapTest(unittest.TestCase):
    def state(self):
        return autofix.new_day_state({}, "2026-11-09", NOW)

    def test_five_problems_cap_three(self):
        state = self.state()
        eligible = [plan("R1", 1, "2026-11-01"), plan("R2", 5, "2026-11-05"), plan("R3", 3, "2026-11-02"),
                    plan("R4", 3, "2026-11-01"), plan("R5", 2, "2026-11-03")]
        start, waiting = autofix.plan_hour(state, eligible, NOW, 3)
        # Most failed jobs first; on a tie, the one failing longest.
        self.assertEqual([p["id"] for p in start], ["R2", "R4", "R3"])
        self.assertEqual([q["id"] for q in waiting], ["R5", "R1"])
        self.assertEqual([q["id"] for q in state["queue"]], ["R5", "R1"])

        # Next tick, 20 minutes later: the hour is used up; nobody is notified twice.
        later = NOW + timedelta(minutes=20)
        start, waiting = autofix.plan_hour(state, [plan("R5", 2, "2026-11-03"), plan("R1", 1, "2026-11-01")], later, 3)
        self.assertEqual((start, waiting), ([], []))
        self.assertEqual(len(state["notices"]), 1)

        # An hour on, R1 has a fix PR (so it is no longer eligible): it leaves the queue.
        hour_on = NOW + timedelta(minutes=65)
        start, waiting = autofix.plan_hour(state, [plan("R5", 2, "2026-11-03")], hour_on, 3)
        self.assertEqual([p["id"] for p in start], ["R5"])
        self.assertEqual(state["queue"], [])

    def test_notice_mentions_only_the_owner(self):
        state = self.state()
        _, waiting = autofix.plan_hour(state, [plan(f"R{i}", 1, "2026-11-01") for i in range(4)], NOW, 3)
        body = autofix.render_queue_notice(waiting, 3, NOW, "https://github.com/o/r/issues/1")
        self.assertIn("@bingxche ", body)
        self.assertEqual(body.count("@"), 1)
        self.assertIn("AUTOFIX_MAX_PER_HOUR", body)

    def test_state_round_trips_through_the_comment(self):
        state = self.state()
        autofix.plan_hour(state, [plan("R1", 2, "2026-11-01")], NOW, 3)
        state["problems"]["R1"] = {"name": "a --> b <!-- c", "status": "dispatched", "attempts": 1,
                                   "updated": NOW.isoformat()}
        body = autofix.render_log_comment(state, "live", 3, NOW)
        self.assertEqual(af_state.load_state(body), state)

    def test_comment_stays_under_githubs_limit(self):
        state = self.state()
        for i in range(8):
            state["problems"][f"R{i}"] = {"name": "x", "status": "retry", "attempts": 1, "updated": NOW.isoformat(),
                                          "previous": "log line\n" * 20_000,
                                          "attempt": {"diff": "+x\n" * 20_000}}
        for _ in range(150):
            autofix.add_event(state, NOW, "R1", "not fixed", "y" * 300)
        body = autofix.render_log_comment(state, "live", 3, NOW)
        self.assertLess(len(body), 65_536)
        self.assertEqual(set(af_state.load_state(body)["problems"]), {f"R{i}" for i in range(8)})

    def test_skipped_problems_are_forgotten_after_two_days(self):
        old = {"problems": {"R1": {"name": "a", "attempts": 0, "skip": "flaky",
                                   "updated": (NOW - timedelta(days=3)).isoformat()},
                            "R2": {"name": "b", "attempts": 1, "status": "gave-up",
                                   "updated": (NOW - timedelta(days=3)).isoformat()}}}
        self.assertEqual(set(autofix.new_day_state(old, "2026-11-09", NOW)["problems"]), {"R2"})

    def test_model_unavailable_gives_up_after_three_hours(self):
        state = self.state()
        p = {"id": "R1", "name": "x"}
        for _ in range(3):
            autofix.record_attempt(state, "R1", p, 1, autofix.AttemptResult("model-unavailable", "404"), NOW)
        self.assertEqual(state["problems"]["R1"]["status"], "gave-up")
        self.assertEqual(state["problems"]["R1"]["attempts"], 0)

    def test_audit_finds_an_hour_over_the_cap(self):
        import autofix_audit
        entered = [{"id": f"R{i}", "at": (NOW + timedelta(minutes=10 * i)).isoformat()} for i in range(4)]
        self.assertTrue(autofix_audit.hourly_violations(entered, 3))
        self.assertFalse(autofix_audit.hourly_violations(entered[:3], 3))


class PatchGateTest(unittest.TestCase):
    ROOT_CAUSE = {"fix_kind": "root-cause"}

    def diff(self, path: str, removed: list[str], added: list[str]) -> str:
        return "\n".join([f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1 +1 @@"]
                         + [f"-{r}" for r in removed] + [f"+{a}" for a in added]) + "\n"

    def test_root_cause_fix_passes(self):
        d = self.diff("sgl-kernel/csrc/topk.cuh", ["  mask = 0xffffffff;"], ["  mask = warp::kFullMask;"])
        self.assertEqual(af_pr.gate_patch(d, self.ROOT_CAUSE), [])
        self.assertEqual(af_pr.workaround_signals(d), [])

    def test_workarounds_are_recognized(self):
        cases = [
            self.diff("test/registered/a/test_x.py", [], ["@unittest.skipIf(not has_kernel(), 'AITER lacks it')"]),
            self.diff("test/registered/a/test_x.py", [], ["    if is_hip():", "        return"]),
            self.diff("test/registered/a/test_x.py", ["        self.assertGreater(acc, 0.65)"],
                      ["        self.assertGreater(acc, 0.60)"]),
            self.diff("test/registered/a/test_x.py", ['register_amd_ci(est_time=60, suite="stage-b-test-1-gpu-small-amd")'],
                      ['register_amd_ci(est_time=60, suite="nightly-amd")']),
            self.diff("python/sglang/test/x.py", [], ["pytest.mark.xfail(reason='x')"]),
        ]
        for d in cases:
            self.assertTrue(af_pr.workaround_signals(d), d)
            self.assertTrue(af_pr.gate_patch(d, self.ROOT_CAUSE), d)
            self.assertEqual(af_pr.gate_patch(d, {"fix_kind": "workaround"}), [], d)

    def test_forbidden_paths(self):
        for path in ("docker/rocm.Dockerfile", "python/pyproject_rocm.toml", ".github/CODEOWNERS"):
            d = self.diff(path, ["a"], ["b"])
            self.assertTrue(any("may not change" in p for p in af_pr.gate_patch(d, self.ROOT_CAUSE)), path)

    def test_cache_files_are_rejected(self):
        d = self.diff("test/registered/a/__pycache__/test_x.cpython-312.pyc", [], ["x"])
        self.assertTrue(any("cache" in p for p in af_pr.gate_patch(d, self.ROOT_CAUSE)))

    def test_removed_comment_mentioning_nightly_is_not_a_moved_test(self):
        d = self.diff("test/registered/a/test_x.py", ["# flaky on nightly too"], ["# fails on nightly too"])
        self.assertEqual(af_pr.workaround_signals(d), [])

    def test_empty_patch(self):
        self.assertEqual(af_pr.gate_patch("", self.ROOT_CAUSE), ["the agent changed nothing"])


def fix_report(kind: str = "root-cause", **pr) -> dict:
    base = {
        "scope": "attention", "title": "Use the platform warp mask in topk",
        "motivation": "Top-k returns garbage on AMD because a 32-bit mask was hard-coded. "
                      "`[HIGH]` [#48700](https://github.com/sgl-project/sglang/pull/48700) added it. "
                      "This would be wrong if the test also fails with the mask restored.",
        "modifications": "Use `warp::kFullMask`, which is 64 bits on AMD.",
        "workaround": "", "accuracy": "Not affected: no kernel output changes on CUDA.",
        "speed": "Not affected: same instructions.",
    }
    base.update(pr)
    return {"root_cause": "#48700 hard-coded a 32-bit mask", "confidence": "HIGH", "fix_kind": kind,
            "why_minimal": "2 lines at the cause", "tests": [f"{TEST_FILE}::{TEST_FUNC}"],
            "cuda_impact": "none: kFullMask is 0xffffffff on CUDA", "pr": base}


class PrTextTest(unittest.TestCase):
    def test_titles_pass_the_sglang_title_check(self):
        fix = fix_report()
        self.assertEqual(af_pr.build_title(fix, ["sgl-kernel/csrc/topk.cuh"]),
                         "[Fix] [AMD] attention: Use the platform warp mask in topk")
        self.assertEqual(af_pr.build_title(fix, ["test/registered/a/test_x.py"])[:13], "[Test] [AMD] ")
        self.assertEqual(af_pr.build_title(fix, [".github/workflows/pr-test-amd.yml"])[:11], "[CI] [AMD] ")
        for title in ("[Fix] [AMD] attention: Use the platform warp mask in topk",
                      "[Test] [AMD] aiter-backend: Skip the fp8 prefill test when AITER lacks the kernel",
                      "[CI] [AMD] pr-test-amd: Work around the missing MI35x runner label"):
            kind = "root-cause" if title.startswith("[Fix]") else "workaround"
            self.assertEqual(af_pr.check_title(title, kind), [], title)

    def test_bad_titles(self):
        self.assertTrue(af_pr.check_title("[Fix] [AMD] [Workaround] attention: x", "workaround"))
        self.assertTrue(af_pr.check_title("[Fix] [amd] attention: x", "root-cause"))
        self.assertTrue(af_pr.check_title("[Test] [AMD] attention: Change the test", "workaround"))
        self.assertTrue(af_pr.check_title("Fix attention", "root-cause"))

    def test_report_parsing(self):
        text = "Found it.\n\n```json autofix\n" + json.dumps(fix_report()) + "\n```\n"
        self.assertEqual(af_pr.parse_fix_report(text)["fix_kind"], "root-cause")
        with self.assertRaises(ValueError):
            af_pr.parse_fix_report("```json autofix\n" + json.dumps(fix_report("workaround")) + "\n```\n")
        self.assertEqual(af_pr.parse_fix_report('```json autofix\n{"status": "no-fix", "reason": "x"}\n```\n')["status"],
                         "no-fix")

    def test_pr_text_checks(self):
        self.assertEqual(af_pr.check_pr_text(fix_report()), [])
        self.assertTrue(af_pr.check_pr_text(fix_report(motivation="It broke. [HIGH] #1 did it.")))
        self.assertTrue(af_pr.check_pr_text(fix_report(modifications="word " * 300)))
        self.assertTrue(af_pr.check_pr_text(fix_report(modifications="Fixes R386, cc @someone.")))

    def body(self, kind: str = "root-cause") -> str:
        fix = fix_report(kind, title="Skip the fp8 prefill test when AITER lacks the kernel",
                         workaround="AITER #900 adds the kernel; remove after the image has it.") \
            if kind == "workaround" else fix_report()
        p = {"id": "R386", "name": "x", "tests": [[TEST_FILE, TEST_FUNC]], "failing_runs": 3,
             "first_fail": {"date": "2026-11-06", "run_url": "https://github.com/sgl-project/sglang/actions/runs/1"},
             "jobs": [{"job_id": 7, "job_name": SMALL, "run_url": "https://github.com/sgl-project/sglang/actions/runs/1",
                       "tests": [[TEST_FILE, TEST_FUNC]]}],
             "recipe": {"workflow": "pr-test-amd.yml", "families": [af_ci.job_family(SMALL)]}}
        v = {"run_id": 9, "run_url": "https://github.com/sgl-project/sglang/actions/runs/9",
             "results": [{"file": TEST_FILE, "func": TEST_FUNC, "status": "passed",
                          "job_url": "https://github.com/sgl-project/sglang/actions/runs/9/job/10",
                          "before_url": "https://github.com/sgl-project/sglang/actions/runs/1/job/7"}]}
        return autofix.render_pr_body(TEMPLATE, p, fix, v, {"pre_commit": True, "tests_changed": False,
                                                             "review": "PASS"})

    def test_body_keeps_every_template_section_in_order(self):
        body = self.body()
        sections = af_pr.template_sections(body)
        self.assertEqual([s for s in sections if s != "Test plan"], af_pr.template_sections(TEMPLATE))
        self.assertEqual(sections.index("Test plan"), sections.index("Accuracy Tests") - 1)
        self.assertIn("- [x] Format your code", body)
        self.assertIn("- [ ] Add unit tests", body)
        self.assertIn("1. Ping Merge Oncalls", body)
        self.assertIn("<!-- amd-bot-autofix: R386 -->", body)
        self.assertIn("🤖 Generated with [Claude Code](https://claude.com/claude-code)", body)
        self.assertIn("Failing on `main` since [2026-11-06]", body)
        self.assertIn("`stage-b-test-1-gpu-small-amd (rocm10, 3)`", body)
        self.assertNotIn("R386", body.split("<!-- amd-bot-autofix")[0])

    def test_workaround_body_has_its_section(self):
        body = self.body("workaround")
        self.assertIn("### Workaround", body.split("## Test plan")[0])


class FakeGitHub:
    """The parts of autofix.GitHub that build_plan and verify_run read."""

    def __init__(self, history: list[str], fix_prs=()):
        self.history = history  # newest first: "failed" / "passed"
        self.fix_prs = list(fix_prs)
        self.runs_by_id = {}

    def run(self, run_id, fresh=False):
        return {"id": run_id, "event": "schedule", "head_branch": "main", "head_sha": f"sha{run_id}",
                "created_at": "2026-11-09T01:00:00Z", "html_url": f"https://github.com/x/runs/{run_id}"}

    def scheduled_runs(self, workflow):
        return [{"id": 100 + i, "html_url": f"https://github.com/x/runs/{100 + i}", "head_sha": f"s{i}",
                 "created_at": f"2026-11-{9 - i:02d}T01:00:00Z"} for i in range(len(self.history))]

    def jobs(self, run_id, fresh=False):
        i = run_id - 100
        if 0 <= i < len(self.history):
            ok = self.history[i] == "passed"
            return [{"id": 1000 + i, "name": SMALL, "conclusion": "success" if ok else "failure",
                     "html_url": f"https://github.com/x/job/{1000 + i}", "labels": ["linux-mi300-1gpu-sglang"]}]
        return [{"id": 7, "name": SMALL, "labels": ["linux-mi300-1gpu-sglang"]}]

    def job_log(self, job_id):
        return FAILED_LOG

    def search_prs(self, query):
        return [{"number": n, "state": "open", "pull_request": {}} for n in self.fix_prs]

    def open_prs(self, since):
        return []

    def commits_touching(self, path, since):
        return []


def problem(**kw) -> af_select.Problem:
    analysis = ("### Failed Tests\n| Test File | Test Function | Error |\n|---|---|---|\n"
                f"| `{TEST_FILE}` | `{TEST_FUNC}` | AttributeError: no attribute 'layer_router' |\n")
    job = {"job_id": 7, "job_name": SMALL, "run_url": "https://github.com/sgl-project/sglang/actions/runs/42",
           "workflow": "pr-test-amd.yml", "analysis": analysis}
    job.update(kw.pop("job", {}))
    kw.setdefault("tests", af_select.own_tests([job], "", exclusive=True))
    return af_select.Problem("R386", "MiniMax fp8 prefill test crashes", [job], **kw)


class SelectionTest(unittest.TestCase):
    def build(self, p, gh):
        return autofix.build_plan(p, gh, YAMLS.get, NOW)

    def test_a_real_unfixed_failure_is_planned(self):
        plan, why = self.build(problem(), FakeGitHub(["failed", "failed", "passed"]))
        self.assertEqual(why, "")
        self.assertEqual(plan["tests"], [[TEST_FILE, TEST_FUNC]])
        self.assertEqual(plan["recipe"]["inputs"]["target_stage"], "stage-b-test-1-gpu-small-amd")
        self.assertEqual((plan["first_fail"]["date"], plan["last_pass"]["date"], plan["failing_runs"]),
                         ("2026-11-08", "2026-11-07", 2))

    def test_flaky_is_skipped(self):
        plan, why = self.build(problem(), FakeGitHub(["failed", "passed", "failed"]))
        self.assertIsNone(plan)
        self.assertIn("not failing for sure", why)

    def test_fix_pr_in_the_report_or_upstream(self):
        self.assertIn("fix PR", self.build(problem(fix_pr=48978), FakeGitHub(["failed", "failed"]))[1])
        self.assertIn("fix PR #48999", self.build(problem(), FakeGitHub(["failed", "failed"], fix_prs=[48999]))[1])

    def test_not_scheduled_infra_and_no_recipe(self):
        class Manual(FakeGitHub):
            def run(self, run_id, fresh=False):
                return {**super().run(run_id), "event": "workflow_dispatch"}
        self.assertIn("not a scheduled failure", self.build(problem(), Manual(["failed", "failed"]))[1])
        infra = problem(job={"analysis": problem().jobs[0]["analysis"].replace(
            "AttributeError: no attribute 'layer_router'", "OSError: No space left on device")})
        self.assertIn("infrastructure", self.build(infra, FakeGitHub(["failed", "failed"]))[1])
        miles = problem(job={"workflow": "nightly-test-amd-miles-rocm10.yml"})
        self.assertIn("Miles image", self.build(miles, FakeGitHub(["failed", "failed"]))[1])


class ReportParsingTest(unittest.TestCase):
    SUMMARY = "\n".join([
        "## Failures by problem",
        "### <!-- R386 --> 🆕 MiniMax fp8 prefill test crashes — pr-test-amd · ❌ no fix",
        "- **In-flight fix**: ❌ none found",
        "### <!-- R414 --> DeepSeek-V4-Pro FP4 crashes — nightly-test-amd · ✅ fix open",
        "- **In-flight fix**: ✅ [#49028](https://github.com/sgl-project/sglang/pull/49028) (open since Nov 8)",
        "### <!-- R332 · R269 --> Nightly: failures continuing from earlier days — nightly-test-amd · ✅ fix open",
        "| Problem | Workflow | Job |",
        "|---|---|---|",
        "| <!-- R332 --> Qwen3.8-Flash-Next FP8 crashes on MI300 | nightly | x |",
        "| <!-- R269 --> [VLM](https://x) accuracy below the floor | nightly | y |",
    ])

    def test_names_and_fixes(self):
        sections = af_select.problem_sections(self.SUMMARY)
        self.assertEqual(sections["R386"]["name"], "MiniMax fp8 prefill test crashes")
        self.assertEqual((sections["R386"]["fix"], sections["R386"]["fix_pr"]), (False, None))
        self.assertEqual((sections["R414"]["fix"], sections["R414"]["fix_pr"]), (True, 49028))
        # A group heading names neither problem, and its "fix open" is not theirs.
        self.assertEqual(sections["R332"]["name"], "Qwen3.8-Flash-Next FP8 crashes on MI300")
        self.assertEqual(sections["R269"]["name"], "VLM accuracy below the floor")
        self.assertFalse(sections["R332"]["fix"])


class OwnTestsTest(unittest.TestCase):
    """One job can be in several problems; its analysis lists every failure (R392-R394, Nov 6)."""

    ANALYSIS = "\n".join([
        "### Failed Tests",
        "| Test File | Test Function | Error |",
        "|---|---|---|",
        "| `test/registered/kernels/ops/attention/test_dsv4_q_rope_store.py` | `TestQRopeStore.test_exact_output_and_padding` | build failed |",
        "| `test/registered/kernels/ops/attention/test_dsv4_q_rope_store.py` | `test_exact_output_and_padding` | same |",
        "| `test/registered/kernels/ops/gemm/test_hc_mix_flydsl.py` | `test_gated_residual_dispatch`, "
        "`test_gated_residual_dispatch_many_rows[32/96/128]` (4 failed, 1 passed) | ModuleNotFoundError: aiter.ops.flydsl.hc_mix |",
        "| `test/registered/amd/test_minimax_hd128_asm_prefill.py` | `TestMiniMaxHD128AsmPrefill.test_first_chunk_reads_fp8_pool` "
        "| AttributeError: layer_router |",
    ])
    JOB = {"job_id": 127910655397, "job_name": "stage-b-test-1-gpu-small-amd-mi35x (rocm10, linux-mi35x-gpu-1)",
           "run_url": "https://github.com/sgl-project/sglang/actions/runs/42223350044", "workflow": "pr-test-amd.yml",
           "analysis": ANALYSIS}

    def test_functions_are_split_out_of_prose(self):
        self.assertEqual(af_select.split_functions(
            "`test_gated_residual_dispatch`, `test_gated_residual_dispatch_many_rows[32/96/128]` (4 failed, 1 passed)"),
            ["test_gated_residual_dispatch", "test_gated_residual_dispatch_many_rows"])
        self.assertEqual(af_select.split_functions(
            "test_blend_matches_per_slice_loop[100--2-dtype1]` (blend_extent=100 → clamped to 70)"),
            ["test_blend_matches_per_slice_loop[100--2-dtype1]"])
        self.assertEqual(af_select.split_functions("`TestX.setUpClass`"), ["TestX.setUpClass"])
        self.assertEqual(af_select.split_functions("— (server crashed before any test)"), [])

    def test_a_qualified_name_absorbs_the_bare_one(self):
        tests = af_select.problem_tests([self.JOB])
        self.assertIn(("test/registered/kernels/ops/attention/test_dsv4_q_rope_store.py",
                       "TestQRopeStore.test_exact_output_and_padding"), tests)
        self.assertNotIn(("test/registered/kernels/ops/attention/test_dsv4_q_rope_store.py",
                          "test_exact_output_and_padding"), tests)

    def test_each_problem_keeps_only_the_files_its_text_names(self):
        r392 = "| pr-test-amd | `stage-b-test-1-gpu-small-amd-mi35x` | `test/registered/kernels/ops/attention/test_dsv4_q_rope_store.py` |"
        r394 = "| <!-- R394 --> aiter `hc_mix` missing | same | same | `test/registered/kernels/ops/gemm/test_hc_mix_flydsl.py` |"
        self.assertEqual({f for f, _ in af_select.own_tests([self.JOB], r392, exclusive=False)},
                         {"test/registered/kernels/ops/attention/test_dsv4_q_rope_store.py"})
        self.assertEqual([fn for _, fn in af_select.own_tests([self.JOB], r394, exclusive=False)],
                         ["test_gated_residual_dispatch", "test_gated_residual_dispatch_many_rows"])
        # Nothing names its files and another problem shares the job: no tests, so no attempt.
        self.assertEqual(af_select.own_tests([self.JOB], "", exclusive=False), [])
        self.assertEqual(len(af_select.own_tests([self.JOB], "", exclusive=True)), 4)

    def test_report_shorthand_for_file_names(self):
        text = "`test/registered/amd/test_deepseek_v4_pro_fp4_mtp.py` · `_fp4_tbo_mtp.py`"
        self.assertTrue(af_select.file_named("test/registered/amd/test_deepseek_v4_pro_fp4_tbo_mtp.py", text))
        self.assertFalse(af_select.file_named("test/registered/amd/test_kimi.py", text))

    def test_section_text_follows_the_problem(self):
        summary = "\n".join([
            "### <!-- R392 --> RoPE kernels do not compile — pr-test-amd · ✅ fix open",
            "| pr-test-amd | job | `test_dsv4_q_rope_store.py` | `test_x` |",
            "### <!-- R393 · R394 --> Two tests fail — pr-test-amd",
            "| <!-- R393 --> Stale KV-pool mock | pr | job | `test_minimax_hd128_asm_prefill.py` | `t` |",
            "| <!-- R394 --> aiter `hc_mix` missing | same | same | `test_hc_mix_flydsl.py` | `t` |",
            "## Workflow drill-down",
            "| job | `test_other.py` | `t` | <!-- R392 --> RoPE | x |",
        ])
        sections = af_select.problem_sections(summary)
        text = {pid: "\n".join(sections[pid]["text"]) for pid in ("R392", "R393", "R394")}
        self.assertIn("test_dsv4_q_rope_store.py", text["R392"])
        self.assertIn("test_other.py", text["R392"])
        self.assertNotIn("test_hc_mix_flydsl.py", text["R393"])
        self.assertIn("test_hc_mix_flydsl.py", text["R394"])
        self.assertNotIn("test_minimax", text["R394"])


class VerifyRunTest(unittest.TestCase):
    PLAN = {"tests": [[TEST_FILE, TEST_FUNC]],
            "jobs": [{"job_id": 7, "job_name": SMALL, "run_url": "u", "labels": ["linux-mi300-1gpu-sglang"],
                      "tests": [[TEST_FILE, TEST_FUNC]]}],
            "recipe": {"workflow": "pr-test-amd.yml", "families": [af_ci.job_family(SMALL)]}}
    REC = {"head_sha": "abc", "fix": {"fix_kind": "root-cause"}}
    RUN = {"id": 9, "path": ".github/workflows/pr-test-amd.yml", "head_sha": "abc", "status": "completed",
           "html_url": "https://github.com/x/runs/9"}

    def jobs(self, name=SMALL.replace(", 3)", ", 5)"), labels=("linux-mi300-1gpu-sglang",), status="completed"):
        return [{"id": 10, "name": name, "status": status, "labels": list(labels), "html_url": "https://j/10"}]

    def test_passed_in_another_partition(self):
        v = autofix.verify_run(self.PLAN, self.REC, self.RUN, self.jobs(), lambda _: PASSED_LOG)
        self.assertEqual((v["done"], v["ok"]), (True, True), v["reasons"])

    def test_waits_for_the_job(self):
        v = autofix.verify_run(self.PLAN, self.REC, self.RUN, self.jobs(status="in_progress"), lambda _: PASSED_LOG)
        self.assertFalse(v["done"])

    def test_failures(self):
        for jobs, log, why in (
            (self.jobs(), FAILED_LOG, "failed"),
            (self.jobs(), ci_log("OK", "", retried=True), "retry"),
            (self.jobs(labels=("linux-mi325-1gpu-sglang",)), PASSED_LOG, "ran on"),
            (self.jobs(name=SMALL.replace("rocm10", "rocm720")), PASSED_LOG, "has no"),
        ):
            v = autofix.verify_run(self.PLAN, self.REC, self.RUN, jobs, lambda _: log)
            self.assertFalse(v["ok"], why)
            self.assertIn(why, " ".join(v["reasons"]))
        v = autofix.verify_run(self.PLAN, {**self.REC, "head_sha": "other"}, self.RUN, self.jobs(), lambda _: PASSED_LOG)
        self.assertFalse(v["ok"])

    def test_a_partition_that_died_early_does_not_hide_the_pass(self):
        jobs = [{"id": 11, "name": SMALL.replace(", 3)", ", 0)"), "status": "completed",
                 "labels": ["linux-mi300-1gpu-sglang"], "html_url": "https://j/11"}] + self.jobs()
        logs = {11: "2026-11-07T01:00:00Z docker: container exited\n", 10: PASSED_LOG}
        v = autofix.verify_run(self.PLAN, self.REC, self.RUN, jobs, logs.get)
        self.assertTrue(v["ok"], v["reasons"])
        self.assertEqual(v["results"][0]["job_url"], "https://j/10")

    def test_skip_passes_only_for_a_workaround(self):
        line = "test_first_chunk_reads_fp8_pool (__main__.TestMiniMaxHD128AsmPrefill.test_first_chunk_reads_fp8_pool) ... skipped 'x'"
        log = ci_log("OK (skipped=1)", line)
        self.assertFalse(autofix.verify_run(self.PLAN, self.REC, self.RUN, self.jobs(), lambda _: log)["ok"])
        rec = {**self.REC, "fix": {"fix_kind": "workaround"}}
        self.assertTrue(autofix.verify_run(self.PLAN, rec, self.RUN, self.jobs(), lambda _: log)["ok"])


class ModeOffTest(unittest.TestCase):
    def test_off_reads_and_writes_nothing(self):
        boom = mock.Mock(side_effect=AssertionError("no GitHub access with AUTOFIX_MODE=off"))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.multiple(autofix, find_issue=boom, post_comment=boom, update_comment=boom,
                                    load_problems=boom, ensure_sglang_repo=boom, start_attempt=boom,
                                    push_and_dispatch=boom):
            gh = mock.Mock(side_effect=AssertionError)
            self.assertEqual(autofix.fix_phase("t", "o/r", "off", 3, gh, Path(tmp)), 0)
            self.assertEqual(autofix.publish_phase("t", "o/r", "off", 3, gh, Path(tmp)), 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_mode_defaults_to_off(self):
        self.assertEqual(af_config.DEFAULT_MODE, "off")
        self.assertEqual(autofix.MODES, ("off", "live"))

    def test_the_script_runs_from_the_command_line(self):
        # The workflow runs the file; a lost `__main__` guard once made it exit 0 doing nothing.
        for phase in ("fix", "publish"):
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / "autofix.py"), phase, "--bot-repo", "o/r"],
                               capture_output=True, text=True, timeout=60,
                               env={**os.environ, "GH_PAT": "x", "AUTOFIX_MODE": "off", "AUTOFIX_TOKEN": ""})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("AUTOFIX_MODE=off; nothing to do", r.stdout + r.stderr, phase)

    def test_the_switch(self):
        for value, mode in ((None, "off"), ("", "off"), ("LIVE", "live"), (" live ", "live"), ("on", "off"),
                            ("dry-run", "off")):
            env = {} if value is None else {"AUTOFIX_MODE": value}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(af_config.mode_from_env(), mode, value)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(af_config.max_per_hour_from_env(), 3)

    def test_the_fix_job_refuses_the_write_token(self):
        env = {"GH_PAT": "ro", "AUTOFIX_TOKEN": "push", "AUTOFIX_MODE": "live"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "argv", ["autofix.py", "fix", "--bot-repo", "o/r"]), \
                mock.patch.object(autofix, "fix_phase", side_effect=AssertionError("must not start")):
            self.assertEqual(autofix.main(), 1)


class TickTest(unittest.TestCase):
    """Live ticks end to end, every GitHub and agent call faked: fix job, publish job, then the PR."""

    def test_fix_then_publish_then_open_the_pr(self):
        posted, updated = [], []
        p = plan("R386", 2, "2026-11-08")
        p.update(tests=[[TEST_FILE, TEST_FUNC]], jobs=VerifyRunTest.PLAN["jobs"],
                 recipe={**VerifyRunTest.PLAN["recipe"], "inputs": {"target_stage": "x"}},
                 first_fail=None, failing_runs=2)
        record = {"branch": "bingxche/autofix-r386-x", "base_sha": "base", "fix": fix_report(),
                  "title": "[Fix] [AMD] attention: x", "message": "m", "patch": b"diff --git a/x b/x\r\n\xff",
                  "diff": "d", "checks": {"pre_commit": True, "review": "PASS"}}
        gh = mock.Mock()
        common = dict(
            find_issue=mock.Mock(return_value=5), ensure_sglang_repo=mock.Mock(),
            find_log_comment=mock.Mock(return_value=None),
            workflow_yaml_at=mock.Mock(return_value=YAMLS.get), pr_template=mock.Mock(return_value=TEMPLATE),
            post_comment=mock.Mock(side_effect=lambda t, r, n, body: posted.append(body) or {"id": 1}),
            update_comment=mock.Mock(side_effect=lambda t, r, i, body: updated.append(body) or {"id": i}),
        )
        start = mock.Mock(return_value=autofix.AttemptResult("ready", record=record))
        with tempfile.TemporaryDirectory() as tmp:
            handoff = Path(tmp)
            # The fix job: one problem starts, its patch is handed over, nothing is written to GitHub.
            with mock.patch.multiple(autofix, read_state=mock.Mock(return_value=({}, None)),
                                     load_problems=mock.Mock(return_value=[problem()]),
                                     build_plan=mock.Mock(return_value=(p, "")), start_attempt=start, **common):
                autofix.fix_phase("t", "o/r", "live", 3, gh, handoff, NOW)
            self.assertEqual((posted, updated), ([], []))
            # Byte for byte: CRLF and non-UTF-8 text survive the handover.
            self.assertEqual(autofix.patch_path(handoff, "R386").read_bytes(), record["patch"])
            data = json.loads((handoff / autofix.STATE_FILE).read_text())
            self.assertEqual(data["state"]["problems"]["R386"]["status"], "ready")
            self.assertNotIn("patch", data["state"]["problems"]["R386"]["attempt"])

            # The publish job: pushes the patch and dispatches the job.
            pushed = mock.Mock(side_effect=lambda plan, rec, patch, gh: rec.update(
                head_sha="abc", dispatched_at=NOW.isoformat()))
            with mock.patch.multiple(autofix, push_and_dispatch=pushed, **common):
                autofix.publish_phase("t", "o/r", "live", 3, gh, handoff, NOW)
            self.assertEqual(pushed.call_args.args[2], record["patch"])
            state = af_state.load_state(posted[-1])
            self.assertEqual(state["problems"]["R386"]["status"], "dispatched")
            self.assertEqual([e["id"] for e in state["entered"]], ["R386"])

        # An hour later the run passed: the next publish job opens the draft PR.
        gh.pr_for_branch.return_value = None
        gh.create_draft_pr.return_value = {"number": 49001, "html_url": "https://github.com/sgl-project/sglang/pull/49001"}
        verified = {"done": True, "ok": True, "reasons": [], "results": [], "run_url": "r", "run_id": 9}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.multiple(autofix, read_state=mock.Mock(return_value=(state, {"id": 1})),
                                    poll_dispatched=mock.Mock(return_value=("verified", verified)),
                                    find_fix_pr=mock.Mock(return_value=None), **common):
            autofix.publish_phase("t", "o/r", "live", 3, gh, Path(tmp), NOW + timedelta(hours=1))
        state = af_state.load_state(updated[-1])
        self.assertEqual(state["problems"]["R386"]["status"], "opened")
        title, body, branch = gh.create_draft_pr.call_args.args
        self.assertEqual(branch, "bingxche/autofix-r386-x")
        self.assertIn("## Test plan", body)
        self.assertEqual(start.call_count, 1)

    def test_failed_gpu_run_retries_then_gives_up(self):
        gh = mock.Mock()
        state = autofix.new_day_state({}, "2026-11-09", NOW)
        entry = {"name": "x", "attempts": 1, "status": "dispatched", "updated": NOW.isoformat(),
                 "pushed": ["bingxche/autofix-r1-x"],
                 "plan": {"tests": []}, "attempt": {"branch": "bingxche/autofix-r1-x", "head_sha": "a", "diff": ""}}
        state["problems"]["R1"] = entry
        failed = {"done": True, "ok": False, "reasons": ["`t.py::f` failed"], "results": []}
        with mock.patch.object(autofix, "poll_dispatched", return_value=("failed", failed)):
            autofix.check_dispatched("R1", entry, gh, state, NOW, TEMPLATE)
            self.assertEqual(entry["status"], "retry")
            self.assertIn("the GPU re-run failed", entry["previous"])
            gh.delete_branch.assert_not_called()
            entry.update(status="dispatched", attempts=2)
            autofix.check_dispatched("R1", entry, gh, state, NOW, TEMPLATE)
        self.assertEqual(entry["status"], "gave-up")
        gh.delete_branch.assert_called_once_with("bingxche/autofix-r1-x")
        self.assertNotIn("plan", entry)


class AttemptTest(unittest.TestCase):
    """start_attempt() with git, the worktree and both agents faked."""

    DIFF = ("diff --git a/sgl-kernel/csrc/topk.cuh b/sgl-kernel/csrc/topk.cuh\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
            "-  mask = 0xffffffff;\n+  mask = warp::kFullMask;\n")
    PLAN = {"id": "R386", "name": "topk mask", "tests": [[TEST_FILE, TEST_FUNC]], "jobs": [],
            "recipe": {"workflow": "pr-test-amd.yml", "inputs": {}, "families": []}}

    def attempt(self, reviews):
        review = mock.Mock(side_effect=reviews)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.multiple(
                af_agents, git=mock.Mock(return_value="base\n"), create_agent_worktree=mock.Mock(return_value=Path(tmp)),
                remove_agent_worktree=mock.Mock(), collect_ci_runtime_env=mock.Mock(return_value=""),
                run_fix_agent=mock.Mock(return_value=fix_report()), staged_diff=mock.Mock(return_value=self.DIFF),
                staged_patch=mock.Mock(return_value=self.DIFF.encode()),
                run_checks=mock.Mock(return_value=([], {"pre_commit": True})), run_review_agent=review):
            result = af_agents.start_attempt(self.PLAN, 1, mock.Mock(), "base")
        return result, review

    def test_the_second_review_sees_the_first(self):
        first = {"verdict": "FAIL", "checks": {"root_cause": "OK"}, "reasons": ["the PR text overstates the cause"]}
        result, review = self.attempt([first, {"verdict": "PASS", "reasons": []}])
        self.assertEqual(result.status, "ready")
        self.assertIsNone(review.call_args_list[0].args[6])
        self.assertEqual(review.call_args_list[1].args[6], first)
        self.assertEqual(result.record["checks"]["review"], "PASS")
        self.assertTrue(result.record["branch"].startswith("bingxche/autofix-r386-"))

    def test_two_failed_reviews_end_the_attempt_before_any_push(self):
        fail = {"verdict": "FAIL", "reasons": ["root cause not shown"]}
        result, review = self.attempt([fail, fail])
        self.assertEqual((result.status, review.call_count), ("failed", 2))
        self.assertIn("root cause not shown", result.reason)


class ReviewFixesTest(unittest.TestCase):
    """The second code review's findings, one test each."""

    def test_the_culprit_and_the_suspect_are_not_a_fix(self):
        gh = FakeGitHub(["failed", "failed"])
        culprit = {"commit": {"message": "[Fix] dsa: add indexer RoPE (#48547)\n\nbody"}}
        gh.commits_touching = mock.Mock(return_value=[culprit])
        self.assertIsNone(af_select.find_fix_pr(gh, [(TEST_FILE, TEST_FUNC)], "2026-11-06T13:10:00Z", exclude={48547}))
        # Commits are looked up from the failing run's start time, not from midnight.
        self.assertEqual(gh.commits_touching.call_args.args[1], "2026-11-06T13:10:00Z")

    def test_a_later_stage_not_listed_yet_is_waited_for(self):
        plan = {**VerifyRunTest.PLAN, "recipe": {"workflow": "pr-test-amd.yml", "families": [
            af_ci.job_family(SMALL), af_ci.job_family("stage-c-test-4-gpu-amd (rocm10, linux-mi300-4gpu-sglang, 0)")]}}
        run = {**VerifyRunTest.RUN, "status": "in_progress"}
        v = af_ci.verify_run(plan, VerifyRunTest.REC, run, VerifyRunTest().jobs(), lambda _: PASSED_LOG)
        self.assertFalse(v["done"])
        v = af_ci.verify_run(plan, VerifyRunTest.REC, VerifyRunTest.RUN, VerifyRunTest().jobs(), lambda _: PASSED_LOG)
        self.assertTrue(v["done"])
        self.assertIn("has no", " ".join(v["reasons"]))

    def test_the_job_that_names_the_test_decides(self):
        jobs = [{"id": i, "name": SMALL.replace(", 3)", f", {i})"), "status": "completed",
                 "labels": ["linux-mi300-1gpu-sglang"], "html_url": f"https://j/{i}"} for i in (1, 2)]
        logs = {1: "no suite markers here\nRan 3 tests\nOK (skipped=1)",
                2: "test_first_chunk_reads_fp8_pool (__main__.TestMiniMaxHD128AsmPrefill.test_first_chunk_reads_fp8_pool) ... ok"}
        v = af_ci.verify_run(VerifyRunTest.PLAN, VerifyRunTest.REC, VerifyRunTest.RUN, jobs, logs.get)
        self.assertTrue(v["ok"], v["reasons"])
        self.assertEqual(v["results"][0]["job_url"], "https://j/2")

    def test_an_interrupted_attempt_counts(self):
        state = af_state.new_day_state({}, "2026-11-09", NOW)
        p = {"id": "R1", "name": "x"}
        autofix.mark_running(state, p, 1, NOW)
        autofix.mark_running(state, {"id": "R2", "name": "y"}, 2, NOW)
        autofix.finish_interrupted(state, NOW)
        self.assertEqual((state["problems"]["R1"]["status"], state["problems"]["R1"]["attempts"]), ("retry", 1))
        self.assertEqual(state["problems"]["R2"]["status"], "gave-up")

    def test_an_unavailable_model_is_not_an_attempt(self):
        state = af_state.new_day_state({}, "2026-11-09", NOW)
        p = {"id": "R1", "name": "x"}
        autofix.mark_running(state, p, 2, NOW)
        autofix.record_attempt(state, "R1", p, 2, af_agents.AttemptResult("model-unavailable", "404"), NOW)
        self.assertEqual((state["problems"]["R1"]["status"], state["problems"]["R1"]["attempts"]), ("retry", 1))

    def test_a_branch_pushed_before_a_failed_dispatch_is_deleted_on_give_up(self):
        state = af_state.new_day_state({}, "2026-11-09", NOW)
        state["problems"]["R1"] = {"name": "x", "attempts": 2, "status": "ready", "updated": NOW.isoformat(),
                                   "plan": {"id": "R1", "tests": []},
                                   "attempt": {"branch": "bingxche/autofix-r1-x", "base_sha": "b"}}
        gh = mock.Mock()

        def push_then_fail(plan, rec, patch, gh):
            rec["head_sha"] = "abc"
            raise requests.HTTPError("422 dispatch refused")

        with tempfile.TemporaryDirectory() as tmp:
            handoff = Path(tmp)
            autofix.patch_path(handoff, "R1").parent.mkdir(parents=True)
            autofix.patch_path(handoff, "R1").write_bytes(b"diff")
            autofix.save_handoff(handoff, 5, state)
            with mock.patch.multiple(autofix, push_and_dispatch=push_then_fail, ensure_sglang_repo=mock.Mock(),
                                     pr_template=mock.Mock(return_value=TEMPLATE), find_log_comment=mock.Mock(),
                                     write_log=mock.Mock()):
                autofix.publish_phase("t", "o/r", "live", 3, gh, handoff, NOW)
        gh.delete_branch.assert_called_once_with("bingxche/autofix-r1-x")
        self.assertEqual(state["problems"]["R1"]["status"], "ready")  # the saved copy; publish read its own

    def test_publish_git_has_no_token_and_no_user_config(self):
        env = af_github.clean_git_env(Path("/tmp/x"))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertFalse({"GH_PAT", "AUTOFIX_TOKEN", "GITHUB_TOKEN"} & set(env))

    def test_commit_and_push_run_no_hooks(self):
        gh = mock.Mock(write_token="t")
        gh.identity.return_value = ("bingxche", "1+bingxche@users.noreply.github.com")
        calls = []
        with mock.patch.object(af_github, "git", side_effect=lambda wt, *a, **k: calls.append(a) or "sha\n"):
            af_github.push_branch(Path("."), "bingxche/autofix-r1-x", "m", gh)
        commit = next(a for a in calls if "commit" in a)
        push = next(a for a in calls if "push" in a)
        for args in (commit, push):
            self.assertIn("core.hooksPath=/dev/null", args)
            self.assertIn("--no-verify", args)

    def test_an_old_pr_from_the_same_branch_name_is_not_ours(self):
        gh = af_github.GitHub("t")
        old = {"number": 1, "state": "closed", "head": {"sha": "old"}}
        with mock.patch.object(gh, "_get", return_value=[old]):
            self.assertIsNone(gh.pr_for_branch("bingxche/autofix-r1-x", "new"))
            self.assertEqual(gh.pr_for_branch("bingxche/autofix-r1-x", "old"), old)

    def test_checks_run_agent_code_without_tokens(self):
        envs = []

        def fake_run(cmd, **kw):
            envs.append(kw.get("env"))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"GH_PAT": "ghp_x", "AUTOFIX_TOKEN": "ghp_y", "PATH": "/bin"}), \
                mock.patch.object(af_agents.subprocess, "run", side_effect=fake_run):
            (Path(tmp) / "test").mkdir()
            (Path(tmp) / "test" / "test_x.py").write_text("x = 1\n")
            af_agents.run_checks(Path(tmp), ["test/test_x.py"])
        self.assertEqual(len(envs), 2)  # pre-commit and pytest --collect-only
        for env in envs:
            self.assertNotIn("GH_PAT", env)
            self.assertNotIn("AUTOFIX_TOKEN", env)

    def test_a_malformed_problem_does_not_stop_the_others(self):
        bad = af_select.Problem("R9", "bad", [{"job_id": 1, "job_name": SMALL, "run_url": "not-a-run",
                                               "workflow": "pr-test-amd.yml", "analysis": ""}],
                                tests=[(TEST_FILE, TEST_FUNC)])
        good = problem()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.multiple(
                autofix, find_issue=mock.Mock(return_value=5), read_state=mock.Mock(return_value=({}, None)),
                ensure_sglang_repo=mock.Mock(), workflow_yaml_at=mock.Mock(return_value=YAMLS.get),
                load_problems=mock.Mock(return_value=[bad, good]),
                start_attempt=mock.Mock(return_value=af_agents.AttemptResult("failed", "x")),
                deepen_clone=mock.Mock()):
            autofix.fix_phase("t", "o/r", "live", 3, FakeGitHub(["failed", "failed", "passed"]), Path(tmp), NOW)
            state = json.loads((Path(tmp) / autofix.STATE_FILE).read_text())["state"]
        self.assertIn("could not plan it", state["problems"]["R9"]["skip"])
        self.assertEqual(state["problems"]["R386"]["attempts"], 1)


class PushGuardTest(unittest.TestCase):
    def test_push_refuses_a_token_of_another_account(self):
        gh = mock.Mock(write_token="t")
        gh.identity.return_value = ("amd-bot", "1+amd-bot@users.noreply.github.com")
        with mock.patch.object(af_github, "git", side_effect=AssertionError("must not commit or push")):
            with self.assertRaises(RuntimeError):
                af_github.push_branch(Path("."), "bingxche/autofix-r1-x", "m", gh)


class BranchGuardTest(unittest.TestCase):
    def test_only_autofix_branches(self):
        gh = autofix.GitHub("t", "w")
        for call in (lambda: gh.delete_branch("main"), lambda: gh.dispatch("x.yml", "feature/x", {}),
                     lambda: gh.create_draft_pr("t", "b", "michael/fix")):
            with self.assertRaises(RuntimeError):
                call()


class PinnedModelTest(unittest.TestCase):
    """Both autofix tasks: Opus 5.5 1M at effort max, no override, no fallback, no token."""

    TASKS = ("CI Autofix", "CI Autofix Review")
    ENV = {"ANTHROPIC_BASE_URL": "https://gw", "AGENT_MODEL_CI_AUTOFIX": "sonnet",
           "AGENT_MODEL_CI_AUTOFIX_REVIEW": "haiku", "ANTHROPIC_DEFAULT_SONNET_MODEL": "S",
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": "H", "GH_PAT": "ghp_secret", "AUTOFIX_TOKEN": "ghp_push",
           "AGENT_GH_TOKEN": "ghp_ro", "GITHUB_TOKEN": "ghs_x"}

    def session(self, work: Path, model: str) -> None:
        project = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(work.resolve()))
        project.mkdir(parents=True, exist_ok=True)
        (project / "s.jsonl").write_text(json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "model": model, "content": []}}) + "\n")
        self.addCleanup(lambda: (project / "s.jsonl").unlink(missing_ok=True))

    def run_task(self, task: str, stdout="done", rc=0, model="Claude-Opus-5.5"):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append((list(cmd), kwargs["env"]))
            return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, self.ENV, clear=True), \
                mock.patch.object(utils.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(utils.Path, "home", return_value=Path(tmp) / "home"):
            work = Path(tmp) / "wt"
            work.mkdir()
            self.session(work, model)
            try:
                out = utils.claude_code_analyze(f"Task: {task}\nProblem: x", work_dir=work)
            except utils.ModelUnavailable as exc:
                out = exc
        return out, seen

    def test_command_line(self):
        for task in self.TASKS:
            out, seen = self.run_task(task)
            self.assertEqual(out, "done")
            cmd, env = seen[0]
            self.assertEqual(cmd[cmd.index("--model") + 1], "Claude-Opus-5.5[1m]", task)
            self.assertEqual(cmd[cmd.index("--effort") + 1], "max", task)
            self.assertNotIn("--dangerously-skip-permissions", cmd)
            self.assertIn("--restricted", cmd)
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
            allowed = cmd[cmd.index("--allowedTools") + 1:]
            self.assertNotIn("Bash", allowed)
            if task == "CI Autofix Review":
                self.assertFalse({"Edit", "Write"} & set(allowed))
            for name in ("GH_PAT", "GH_TOKEN", "GITHUB_TOKEN", "AGENT_GH_TOKEN", "AUTOFIX_TOKEN"):
                self.assertNotIn(name, env, task)

    def test_unserved_model_fails_without_fallback(self):
        out, seen = self.run_task("CI Autofix", rc=1, stdout='API Error: 400 Deployment of "Claude-Opus-5.5" is not found!')
        self.assertIsInstance(out, utils.ModelUnavailable)
        self.assertEqual(len(seen), 1)

    def test_another_model_in_the_session_fails(self):
        out, _ = self.run_task("CI Autofix Review", model="Claude-Sonnet-5.5")
        self.assertIsInstance(out, utils.ModelUnavailable)

    def test_model_names(self):
        self.assertTrue(utils.model_matches("Claude-Opus-5.5[1m]", "claude-opus-5-5-20261001"))
        self.assertFalse(utils.model_matches("Claude-Opus-5.5[1m]", "claude-opus-5-1"))

    def test_other_tasks_keep_their_behaviour(self):
        with mock.patch.dict(os.environ, {"AGENT_MODEL_JOB_FAILURE_ANALYSIS": "X"}, clear=True):
            self.assertEqual(utils.task_model("Job Failure Analysis"), "X")
        self.assertFalse(utils.AGENT_TASKS["Job Failure Analysis"].pinned)


if __name__ == "__main__":
    unittest.main()
