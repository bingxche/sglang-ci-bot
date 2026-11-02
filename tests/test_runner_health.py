import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import runner_health as rh  # noqa: E402

NOW = datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)
DISAGG = "nightly-amd-mi355x-disagg.yml"


def _runner(name, status="offline", busy=False, labels=("self-hosted", "Linux", "linux-sglang-mi35x-di")):
    return {"name": name, "status": status, "busy": busy, "labels": [{"name": l} for l in labels]}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _job(labels, status="queued", runner_name=None, created=NOW - timedelta(hours=1)):
    return {"labels": list(labels), "status": status, "runner_name": runner_name, "created_at": _iso(created)}


def _run(run_id, created, status="queued", updated=None):
    return {
        "id": run_id,
        "status": status,
        "html_url": f"https://github.com/sgl-project/sglang/actions/runs/{run_id}",
        "created_at": _iso(created),
        "updated_at": _iso(updated or created),
    }


DI_RUNNERS = [_runner("pit2-sglang-mi355x-di-01"), _runner("pit2-sglang-mi355x-di-02")]


class RunnerStatusTests(unittest.TestCase):
    def test_runners_match_on_every_job_label(self):
        runners = DI_RUNNERS + [_runner("other", "online", labels=("self-hosted", "amd-internal"))]
        status = rh.runner_status(["linux-sglang-mi35x-di"], runners)
        self.assertEqual((status["total"], status["online"]), (2, 0))
        self.assertTrue(rh.all_offline(status))

    def test_an_online_runner_is_not_all_offline(self):
        runners = DI_RUNNERS + [_runner("pit2-sglang-mi355x-di-03", "online", busy=True)]
        status = rh.runner_status(["linux-sglang-mi35x-di"], runners)
        self.assertEqual((status["online"], status["busy"]), (1, 1))
        self.assertFalse(rh.all_offline(status))

    def test_autoscaled_set_matches_by_name_and_is_never_all_offline(self):
        runners = [
            {"name": "linux-mi35x-gpu-1-sj8vf-runner-tpr2j", "status": "offline", "busy": False, "labels": []},
            {"name": "linux-mi35x-gpu-8.fabric-rkfzw-runner-k8mzk", "status": "online", "busy": True, "labels": []},
        ]
        status = rh.runner_status(["linux-mi35x-gpu-1"], runners)
        self.assertEqual(status["total"], 1)
        self.assertTrue(status["autoscaled"])
        self.assertFalse(rh.all_offline(status))

    def test_unknown_runner_list(self):
        self.assertIsNone(rh.runner_status(["linux-sglang-mi35x-di"], None))
        self.assertFalse(rh.all_offline(None))

    def test_hosted_labels(self):
        self.assertTrue(rh.is_hosted(["ubuntu-latest"]))
        self.assertFalse(rh.is_hosted(["linux-sglang-mi35x-di"]))
        self.assertFalse(rh.is_hosted([]))


class StallRuleTests(unittest.TestCase):
    def test_queue_alone_needs_the_long_threshold(self):
        online = {"total": 2, "online": 2, "busy": 2, "offline": [], "autoscaled": False}
        self.assertFalse(rh.is_stalled(timedelta(hours=rh.QUEUE_STALL_HOURS - 1), online))
        self.assertTrue(rh.is_stalled(timedelta(hours=rh.QUEUE_STALL_HOURS), online))
        self.assertTrue(rh.is_stalled(timedelta(hours=rh.QUEUE_STALL_HOURS), None))

    def test_all_offline_needs_only_the_grace_period(self):
        offline = rh.runner_status(["linux-sglang-mi35x-di"], DI_RUNNERS)
        self.assertFalse(rh.is_stalled(timedelta(minutes=rh.OFFLINE_GRACE_MINUTES - 1), offline))
        self.assertTrue(rh.is_stalled(timedelta(minutes=rh.OFFLINE_GRACE_MINUTES), offline))

    def test_no_pickup_needs_nothing_running_and_no_later_pickup(self):
        queued_at = NOW - timedelta(hours=rh.NO_PICKUP_HOURS)
        waited = NOW - queued_at
        self.assertTrue(rh.is_stalled(waited, None, None, queued_at))
        self.assertFalse(rh.is_stalled(waited - timedelta(minutes=1), None, None, queued_at))
        running = {"running": 1, "last_pickup": queued_at - timedelta(hours=5)}
        self.assertFalse(rh.is_stalled(waited, None, running, queued_at))
        picked_later = {"running": 0, "last_pickup": queued_at + timedelta(minutes=10)}
        self.assertFalse(rh.is_stalled(waited, None, picked_later, queued_at))
        picked_before = {"running": 0, "last_pickup": queued_at - timedelta(minutes=10)}
        self.assertTrue(rh.is_stalled(waited, None, picked_before, queued_at))

    def test_visible_busy_runners_are_never_read_as_not_picking_up(self):
        queued_at = NOW - timedelta(hours=rh.NO_PICKUP_HOURS + 1)
        busy = {"total": 2, "online": 2, "busy": 2, "offline": [], "autoscaled": False}
        self.assertFalse(rh.is_stalled(NOW - queued_at, busy, None, queued_at))

    def test_note_activity_counts_running_jobs_and_latest_pickup(self):
        activity = {}
        rh.note_activity(activity, [
            {**_job(["linux-mi35x-gpu-1"], "in_progress", "arc-1"), "started_at": _iso(NOW - timedelta(hours=1))},
            {**_job(["linux-mi35x-gpu-1"], "completed", "arc-2"), "started_at": _iso(NOW - timedelta(hours=3))},
            _job(["linux-mi35x-gpu-1"]),  # queued, no runner
            _job(["ubuntu-latest"], "in_progress", "GitHub Actions 1"),
        ])
        self.assertEqual(activity, {("linux-mi35x-gpu-1",): {"running": 1, "last_pickup": NOW - timedelta(hours=1)}})

    def test_never_started_ignores_a_hosted_setup_job(self):
        setup = _job(["ubuntu-latest"], "completed", "GitHub Actions 1")
        self.assertTrue(rh.never_started([]))
        self.assertTrue(rh.never_started([setup, _job(["linux-sglang-mi35x-di"], "completed")]))
        self.assertFalse(rh.never_started([setup, _job(["linux-sglang-mi35x-di"], "completed", "di-01")]))
        self.assertFalse(rh.never_started([setup]))


class CheckWorkflowTests(unittest.TestCase):
    def _check(self, runs_by_status, jobs_by_run, runners):
        def get_runs(token, wf, status, since, event=None):
            self.assertEqual(event, "schedule")
            return runs_by_status.get(status, [])
        with patch.object(rh, "get_runs", side_effect=get_runs), \
             patch.object(rh, "get_run_jobs", side_effect=lambda t, run_id: jobs_by_run[run_id]):
            return rh.check_workflow("tok", DISAGG, runners, NOW)

    def test_offline_runners_and_superseded_runs_are_reported(self):
        stuck = _run(1, NOW - timedelta(days=4))
        queued_at = NOW - timedelta(days=4) + timedelta(hours=4)
        result = self._check(
            {
                "queued": [stuck],
                "cancelled": [
                    _run(2, NOW - timedelta(days=1), "completed", updated=NOW - timedelta(hours=13)),
                    _run(3, NOW - timedelta(days=2), "completed", updated=NOW - timedelta(hours=37)),
                ],
            },
            {
                1: [
                    _job(["ubuntu-latest"], "completed", "GitHub Actions 1"),
                    _job(["linux-sglang-mi35x-di"], "completed", "di-01"),
                    _job(["linux-sglang-mi35x-di"], created=queued_at),
                    _job(["linux-sglang-mi35x-di"], created=queued_at),
                ],
                2: [],
                3: [],
            },
            DI_RUNNERS,
        )
        self.assertEqual(len(result["stalled"]), 1)
        group = result["stalled"][0]
        self.assertEqual(group["jobs"], 2)
        self.assertEqual(group["queued_since"], queued_at)
        self.assertEqual([r["url"] for r in group["runs"]], [stuck["html_url"]])
        # Run 3 was cancelled outside the 24 h window.
        self.assertEqual([r["url"] for r in result["cancelled"]], [_run(2, NOW)["html_url"]])

    def test_without_the_runner_list_a_pool_that_picks_nothing_up_is_reported(self):
        queued_at = NOW - timedelta(hours=3)
        result = self._check(
            {"queued": [_run(1, queued_at - timedelta(minutes=1))]},
            {1: [
                _job(["ubuntu-latest"], "completed", "GitHub Actions 1"),
                _job(["linux-sglang-mi35x-di"], created=queued_at),
                _job(["linux-sglang-mi35x-di"], created=queued_at),
            ]},
            None,
        )
        self.assertEqual([g["jobs"] for g in result["stalled"]], [2])
        cell = rh._runners_cell(None, result["stalled"][0]["activity"], queued_at)
        self.assertIn("nothing running or picked up", cell)
        self.assertIn("no pickup in view", cell)

    def test_without_the_runner_list_a_pool_running_other_jobs_is_not_reported(self):
        queued_at = NOW - timedelta(hours=3)
        activity = {("linux-mi35x-gpu-1",): {"running": 3, "last_pickup": NOW - timedelta(hours=4)}}
        def get_runs(token, wf, status, since, event=None):
            return [_run(1, queued_at)] if status == "queued" else []
        with patch.object(rh, "get_runs", side_effect=get_runs), \
             patch.object(rh, "get_run_jobs", return_value=[_job(["linux-mi35x-gpu-1"], created=queued_at)]):
            result = rh.check_workflow("tok", "pr-test-amd.yml", None, NOW, activity=activity)
        self.assertEqual(result["stalled"], [])

    def test_a_short_queue_on_busy_runners_is_not_reported(self):
        busy = [_runner("di-01", "online", busy=True)]
        result = self._check(
            {"in_progress": [_run(1, NOW - timedelta(hours=3), "in_progress")]},
            {1: [_job(["linux-sglang-mi35x-di"], created=NOW - timedelta(hours=2))]},
            busy,
        )
        self.assertEqual(result, {"workflow": DISAGG, "stalled": [], "cancelled": []})


class RenderTests(unittest.TestCase):
    def test_healthy_section_is_one_line(self):
        section = rh.render_section([{"workflow": DISAGG, "stalled": [], "cancelled": []}], NOW, True)
        self.assertTrue(section.startswith(rh.RUNNER_HEALTH_START))
        self.assertIn("✅ **Runner health**", section)
        self.assertEqual(section.count("\n"), 4)

    def test_stall_lists_offline_runners_and_runs(self):
        result = {
            "workflow": DISAGG,
            "stalled": [{
                "labels": ["linux-sglang-mi35x-di"],
                "runners": rh.runner_status(["linux-sglang-mi35x-di"], DI_RUNNERS),
                "jobs": 8,
                "queued_since": NOW - timedelta(days=4, hours=1),
                "runs": [{"url": "https://x/runs/1", "created_at": "2026-10-29T07:05:10Z"}],
            }],
            "cancelled": [{"url": "https://x/runs/2", "created_at": "2026-11-01T07:04:00Z"}],
        }
        section = rh.render_section([result], NOW, runners_known=False)
        self.assertIn("## ⚠️ Runner health", section)
        self.assertIn("(4 d 1 h)", section)
        self.assertIn("**0 of 2 online, 0 busy**", section)
        self.assertIn("`pit2-sglang-mi355x-di-01`", section)
        self.assertIn("[2026-10-29](https://x/runs/1)", section)
        self.assertIn("[2026-11-01](https://x/runs/2)", section)
        self.assertIn("`RUNNER_STATUS_TOKEN`", section)

    def test_section_goes_on_top_then_is_replaced_in_place(self):
        body = "<!-- daily-cross-workflow-summary:start -->\nS\n<!-- daily-cross-workflow-summary:end -->\n"
        first = rh.replace_section(body, f"{rh.RUNNER_HEALTH_START}\nA\n{rh.RUNNER_HEALTH_END}\n\n")
        self.assertTrue(first.startswith(rh.RUNNER_HEALTH_START))
        self.assertTrue(first.endswith(body))
        second = rh.replace_section(first, f"{rh.RUNNER_HEALTH_START}\nB\n{rh.RUNNER_HEALTH_END}\n\n")
        self.assertEqual(second, f"{rh.RUNNER_HEALTH_START}\nB\n{rh.RUNNER_HEALTH_END}\n\n" + body)


if __name__ == "__main__":
    unittest.main()
