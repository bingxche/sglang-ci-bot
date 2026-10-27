import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import auto_rerun  # noqa: E402


# Tail of a real failure: release-docker-amd-rocm720-nightly.yml, run
# 40493810177, job 122154058999 (Mooncake's Go module download was cut off).
GO_PROXY_LOG = """\
2026-10-27T13:18:51.0558055Z #23 58.76 go: downloading github.com/pkg/errors v0.8.1
2026-10-27T13:18:53.1218346Z #23 60.82 \tgithub.com/gogo/protobuf/gogoproto: github.com/gogo/protobuf@v1.3.2: read "https://proxy.golang.org/github.com/gogo/protobuf/@v/v1.3.2.zip": stream error: stream ID 19; INTERNAL_ERROR; received from peer
2026-10-27T13:18:53.1220332Z #23 60.82 make[2]: *** [mooncake-common/etcd/CMakeFiles/build_etcd_wrapper.dir/build.make:76: mooncake-common/etcd/libetcd_wrapper.so] Error 1
2026-10-27T13:19:38.7121511Z #23 106.4 make: *** [Makefile:136: all] Error 2
2026-10-27T13:19:38.8530519Z ERROR: failed to build: failed to solve: process "/bin/sh -c ..." did not complete successfully: exit code: 2
2026-10-27T13:19:38.8587254Z ##[error]Process completed with exit code 1.
2026-10-27T13:19:38.8710219Z Post job cleanup.
"""

TEST_FAILURE_LOG = """\
2026-10-27T10:00:00.0000000Z WARNING: Read timed out, retrying download
2026-10-27T10:05:00.0000000Z AssertionError: 0.62 not greater than 0.70
2026-10-27T10:05:00.0000000Z FAILED (failures=1)
2026-10-27T10:05:01.0000000Z ##[error]Process completed with exit code 1.
"""

REAL_ERROR_LOG = """\
2026-10-27T10:00:00.0000000Z error: no matching function for call to 'foo'
2026-10-27T10:00:01.0000000Z ##[error]Process completed with exit code 1.
"""


def _run(**overrides) -> dict:
    run = {
        "id": 40493810177,
        "html_url": "https://github.com/sgl-project/sglang/actions/runs/40493810177",
        "status": "completed",
        "conclusion": "failure",
        "event": "schedule",
        "run_attempt": 1,
        "updated_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    run.update(overrides)
    return run


def _job(job_id: int, name: str = "publish (gfx950-rocm720, all)") -> dict:
    return {"id": job_id, "name": name, "conclusion": "failure"}


class TransientCauseTests(unittest.TestCase):
    def test_go_proxy_stream_reset_is_transient(self):
        cause = auto_rerun.transient_cause(GO_PROXY_LOG)
        self.assertTrue(cause.startswith("HTTP/2 stream reset: "))
        self.assertIn("proxy.golang.org", cause)

    def test_assertion_failure_is_not_transient_even_with_a_timeout(self):
        self.assertIsNone(auto_rerun.transient_cause(TEST_FAILURE_LOG))

    def test_compile_error_is_not_transient(self):
        self.assertIsNone(auto_rerun.transient_cause(REAL_ERROR_LOG))

    def test_only_lines_before_the_error_are_searched(self):
        log = (
            "2026-10-27T10:00:00Z Connection reset by peer\n"
            + "2026-10-27T10:00:01Z building\n" * auto_rerun.TAIL_LINES
            + "2026-10-27T10:00:02Z ##[error]Process completed with exit code 1.\n"
        )
        self.assertIsNone(auto_rerun.transient_cause(log))


class SkipReasonTests(unittest.TestCase):
    def test_eligible_run(self):
        self.assertIsNone(auto_rerun.skip_reason(_run()))

    def test_ineligible_runs(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        for run in (
            _run(status="in_progress", conclusion=None),
            _run(conclusion="cancelled"),
            _run(event="workflow_dispatch"),
            _run(run_attempt=2),
            _run(updated_at=old),
        ):
            self.assertIsNotNone(auto_rerun.skip_reason(run), run)


class ResolveModeTests(unittest.TestCase):
    def test_on_without_token_is_dry_run(self):
        self.assertEqual(auto_rerun.resolve_mode("on", ""), "dry-run")
        self.assertEqual(auto_rerun.resolve_mode(None, "tok"), "on")
        self.assertEqual(auto_rerun.resolve_mode("bogus", "tok"), "on")
        self.assertEqual(auto_rerun.resolve_mode("OFF", "tok"), "off")


@patch.object(auto_rerun.requests, "post")
@patch.object(auto_rerun, "download_job_logs")
class ConsiderRunTests(unittest.TestCase):
    def _consider(self, jobs, records=None, mode="on", run=None):
        records = {} if records is None else records
        record = auto_rerun.consider_run(
            "read-token", run or _run(), jobs, "release.yml", records, mode, "rerun-token",
        )
        return record, records

    def test_all_failed_jobs_transient_reruns_failed_jobs(self, logs, post):
        logs.return_value = GO_PROXY_LOG
        post.return_value = Mock(status_code=201)

        record, records = self._consider([_job(1)])

        self.assertEqual(record["decision"], "rerun-failed-jobs")
        self.assertEqual(record["result"], "rerun")
        self.assertIn("40493810177:1", records)
        url = post.call_args.args[0]
        self.assertTrue(url.endswith("/actions/runs/40493810177/rerun-failed-jobs"))
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"].split()[-1], "rerun-token",
        )

    def test_one_transient_among_real_failures_reruns_that_job(self, logs, post):
        logs.side_effect = lambda _tok, job_id: GO_PROXY_LOG if job_id == 2 else REAL_ERROR_LOG
        post.return_value = Mock(status_code=201)

        record, _ = self._consider([_job(1, "a"), _job(2, "b")])

        self.assertEqual(record["decision"], "rerun-job")
        self.assertTrue(post.call_args.args[0].endswith("/actions/jobs/2/rerun"))

    def test_several_transient_mixed_with_real_failures_is_skipped(self, logs, post):
        logs.side_effect = lambda _tok, job_id: REAL_ERROR_LOG if job_id == 3 else GO_PROXY_LOG

        record, _ = self._consider([_job(1, "a"), _job(2, "b"), _job(3, "c")])

        self.assertEqual(record["decision"], "skip")
        post.assert_not_called()

    def test_no_transient_failure_is_recorded_and_not_reread(self, logs, post):
        logs.return_value = REAL_ERROR_LOG

        record, records = self._consider([_job(1)])
        self.assertEqual(record["decision"], "skip")
        self._consider([_job(1)], records=records)

        logs.assert_called_once()
        post.assert_not_called()

    def test_dry_run_does_not_post(self, logs, post):
        logs.return_value = GO_PROXY_LOG

        record, _ = self._consider([_job(1)], mode="dry-run")

        self.assertEqual(record["result"], "dry-run")
        post.assert_not_called()

    def test_api_error_is_recorded(self, logs, post):
        logs.return_value = GO_PROXY_LOG
        post.return_value = Mock(status_code=403, text="Resource not accessible")

        record, _ = self._consider([_job(1)])

        self.assertEqual(record["result"], "error")
        self.assertIn("HTTP 403", record["reason"])

    def test_ineligible_run_is_not_recorded(self, logs, post):
        record, records = self._consider([_job(1)], run=_run(run_attempt=2))

        self.assertIsNone(record)
        self.assertEqual(records, {})
        logs.assert_not_called()


class RenderTests(unittest.TestCase):
    def test_render_shows_reruns_and_hides_skips(self):
        records = [
            {
                "run_url": "https://github.com/sgl-project/sglang/actions/runs/9",
                "decision": "rerun-failed-jobs",
                "result": "rerun",
                "jobs": [{"id": 5, "name": "publish", "cause": "DNS failure: a | b"}],
            },
            {"decision": "skip", "jobs": []},
        ]
        block = auto_rerun.render_records(records)
        self.assertIn("### Auto-rerun", block)
        self.assertIn("[`publish`](https://github.com/sgl-project/sglang/actions/runs/9/job/5)", block)
        self.assertIn("`DNS failure: a / b`", block)
        self.assertEqual(auto_rerun.render_records(records[1:]), "")


class PruneTests(unittest.TestCase):
    def test_old_records_are_dropped(self):
        now = datetime.now(timezone.utc)
        records = {
            "1:1": {"at": (now - timedelta(days=4)).isoformat()},
            "2:1": {"at": now.isoformat()},
        }
        auto_rerun.prune_records(records, now)
        self.assertEqual(list(records), ["2:1"])


if __name__ == "__main__":
    unittest.main()
