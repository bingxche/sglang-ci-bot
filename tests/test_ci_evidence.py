import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_evidence as ce  # noqa: E402
import utils  # noqa: E402


def _log(*lines: str) -> str:
    return "\ufeff" + "\n".join(f"2026-10-14T04:38:{i % 60:02d}.0000000Z {line}" for i, line in enumerate(lines))


HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
PP = "test/registered/amd/disaggregation/test_disaggregation_pp.py"
CASE = "TestDecodePP.test_gsm8k"
ARGS = "server_args={'model_path': 'Qwen/Qwen3-8B', 'tp_size': 2, 'pp_size': 2, 'attention_backend': 'aiter', "

PR_LOG = _log(
    f"[command]/usr/bin/git checkout --progress --force {MERGE}",
    f"HEAD is now at {MERGE[:7]} Merge {HEAD} into {BASE}",
    "Detected GPU architecture from hostname: mi35x",
    "Found available image: rocm/sgl-dev:v0.5.21-rocm10-mi35x-20261013",
    "Digest: sha256:" + "9" * 64,
    f"python3 /sglang-checkout/{PP}",
    ARGS + "'disaggregation_mode': 'prefill'}",
    "[2026-10-14 04:37:57 PP0 TP0] Using AITER QuickAllReduce",
    "[2026-10-14 04:37:57 PP0 TP0] [aiter] import [module_quick_all_reduce] under /sgl-workspace",
    f"[CI Test Method] {CASE}",
    "AssertionError: 0.0 not greater than 0.7",
    "Accuracy: 0.000",
    "Invalid: 1.000",
    "[2026-10-14 04:38:44] retry() failed once (0th try, maximum 1 retries).",
    "IndexError: list index out of range",
    ARGS + "'disaggregation_mode': 'decode'}",
    "[CI Test Method] TestPrefillPP.test_gsm8k",
    "Accuracy: 0.950",
    "Invalid: 0.000",
    f"ERROR: test_gsm8k (__main__.{CASE})",
    "FAILED (errors=1)",
    f"filename='/sglang-checkout/{PP}', elapsed=134",
    f"✗ FAILED: /sglang-checkout/{PP} returned exit code 1",
)


class TestParseJobLog(unittest.TestCase):
    def test_environment_and_failed_case_facts(self):
        p = ce.parse_job_log(PR_LOG)
        self.assertEqual(p["tested_sha"], MERGE)
        self.assertEqual(p["merge"], {"head": HEAD, "base": BASE})
        self.assertEqual(p["image"], "rocm/sgl-dev:v0.5.21-rocm10-mi35x-20261013")
        self.assertEqual(p["image_digest"], "sha256:" + "9" * 64)
        self.assertEqual(p["gpu"], "mi35x")
        self.assertEqual(p["failed_files"], [PP])
        t = p["tests"][PP]
        self.assertEqual(t["failed_cases"], [CASE])
        self.assertEqual(t["attempts"], 2)
        self.assertEqual(t["mode"], "metric_threshold")
        # Only the failed case's metrics and setup, not the next class's.
        self.assertEqual(t["metrics"], [{"accuracy": 0.0, "invalid": 1.0}])
        self.assertEqual([c["disaggregation_mode"] for c in t["launch_configs"]], ["prefill"])
        self.assertIn("Using AITER QuickAllReduce", t["backend_lines"])
        self.assertEqual(t["aiter_modules"], ["module_quick_all_reduce"])
        # Each attempt keeps its own mode: wrong output first, then a crash.
        self.assertEqual(
            [(a["mode"], a["metrics"], a["first_error"]) for a in t["attempt_details"]],
            [("metric_threshold", [{"accuracy": 0.0, "invalid": 1.0}], "AssertionError: 0.0 not greater than 0.7"),
             ("assertion_or_exception", [], "IndexError: list index out of range")],
        )

    def test_later_methods_of_a_class_get_the_class_launch_config(self):
        log = _log(f"python3 /sglang-checkout/{PP}",
                   ARGS + "'disaggregation_mode': 'null'}",
                   "[CI Test Method] TestScore.test_basic", "OK",
                   "[CI Test Method] TestScore.test_batch", "AssertionError: 65.5 not less than or equal to 60",
                   "ERROR: test_batch (__main__.TestScore.test_batch)",
                   f"filename='/sglang-checkout/{PP}', elapsed=1",
                   f"✗ FAILED: /sglang-checkout/{PP} returned exit code 1")
        t = ce.parse_job_log(log)["tests"][PP]
        self.assertEqual(t["failed_cases"], ["TestScore.test_batch"])
        self.assertEqual([c["tp_size"] for c in t["launch_configs"]], ["2"])

    def test_servers_started_inside_the_test_count_when_the_class_starts_none(self):
        log = _log(f"python3 /sglang-checkout/{PP}",
                   "[CI Test Method] TestScore.test_batch",
                   "server_args={'model_path': 'Qwen/Qwen3-Reranker-0.6B', 'tp_size': 1, }",
                   "AssertionError: 65.5 not less than or equal to 60",
                   "ERROR: test_batch (__main__.TestScore.test_batch)",
                   f"filename='/sglang-checkout/{PP}', elapsed=1",
                   f"✗ FAILED: /sglang-checkout/{PP} returned exit code 1")
        t = ce.parse_job_log(log)["tests"][PP]
        self.assertEqual([c["tp_size"] for c in t["launch_configs"]], ["1"])

    def test_reused_workspace_ignores_the_previous_checkout(self):
        old_head, old_base = "d" * 40, "e" * 40
        log = _log(f"HEAD is now at {'f' * 13} Merge {old_head} into {old_base}",
                   "##[group]Checking out the ref",
                   f"[command]/usr/bin/git checkout --progress --force {MERGE}",
                   f"HEAD is now at {MERGE[:13]} Merge {HEAD} into {BASE}")
        p = ce.parse_job_log(log)
        self.assertEqual((p["tested_sha"], p["merge"]), (MERGE, {"head": HEAD, "base": BASE}))

    def test_ref_checkout_yields_abbreviated_sha(self):
        log = _log("[command]/usr/bin/git checkout --progress --force refs/remotes/pull/1/merge",
                   f"HEAD is now at {MERGE[:7]} Merge {HEAD} into {BASE}")
        self.assertEqual(ce.parse_job_log(log)["tested_sha"], MERGE[:7])

    def test_fast_fail_cascade(self):
        log = _log("core.setFailed(`Fail-fast: skipping — root cause job(s): ${names}`);",
                   "##[error]Fail-fast: skipping — root cause job(s): wait-for-base-b")
        self.assertEqual(ce.parse_job_log(log)["cascade_of"], "wait-for-base-b")


class TestHelpers(unittest.TestCase):
    def test_split_hunks(self):
        diff = "\n".join([
            "diff --git a/x.py b/x.py", "--- a/x.py", "+++ b/x.py", "@@ -1,2 +1,2 @@ def f():", "-a", "+b",
            "@@ -10 +10 @@", "+c", "diff --git a/y.py b/y.py", "@@ -1 +1 @@", "+d",
        ])
        self.assertEqual(
            [(h["file"], h["start_line"], h["end_line"]) for h in ce.split_hunks(diff)],
            [("x.py", 4, 6), ("x.py", 7, 8), ("y.py", 10, 11)],
        )

    def test_comparable(self):
        pr = {"gpu": "mi35x", "image": "r:v1-rocm10-mi35x-20261013", "image_digest": "sha256:1",
              "launch_configs": [{"tp_size": "2"}]}
        same_build = {**pr}
        same_line = {**pr, "image": "r:v1-rocm10-mi35x-20261012", "image_digest": "sha256:2"}
        other = {**pr, "gpu": "mi30x", "image": "r:v1-rocm720-mi35x-20261012", "image_digest": None,
                 "launch_configs": [{"tp_size": "1"}]}
        self.assertEqual(ce._comparable(pr, same_build), {"gpu": True, "image": "same build", "launch_configs": True})
        self.assertEqual(ce._comparable(pr, same_line)["image"], "same line")
        self.assertEqual(ce._comparable(pr, other), {"gpu": False, "image": "different", "launch_configs": False})

    def test_test_status(self):
        parsed = ce.parse_job_log(PR_LOG)
        self.assertEqual(ce._test_status(PP, None, None), "not in run")
        self.assertEqual(ce._test_status(PP, {"conclusion": "success"}, None), "passed")
        self.assertEqual(ce._test_status(PP, {"conclusion": "failure"}, parsed), "failed")
        self.assertEqual(ce._test_status("test/other.py", {"conclusion": "failure"}, parsed), "not reached")

    def test_retry_skips_client_errors_and_retries_server_errors(self):
        def err(status):
            return ce.requests.HTTPError(response=mock.Mock(status_code=status))
        fn = mock.Mock(side_effect=[err(502), "[Failed to fetch logs: HTTP 502]", "ok"])
        with mock.patch.object(ce.time, "sleep"):
            self.assertEqual(ce._retry(fn, 1, k=2), "ok")
        fn.assert_called_with(1, k=2)
        fn = mock.Mock(side_effect=err(404))
        with mock.patch.object(ce.time, "sleep"), self.assertRaises(ce.requests.HTTPError):
            ce._retry(fn)
        self.assertEqual(fn.call_count, 1)

    def test_job_log_is_decoded_as_utf8(self):
        resp = mock.Mock(status_code=200, content="✗ FAILED".encode())
        resp.encoding = "ISO-8859-1"
        type(resp).text = mock.PropertyMock(side_effect=lambda: resp.content.decode(resp.encoding))
        with mock.patch.object(utils.requests, "get", return_value=resp):
            self.assertEqual(utils.download_job_logs("t", 1), "✗ FAILED")


if __name__ == "__main__":
    unittest.main()
