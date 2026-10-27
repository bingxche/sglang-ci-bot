import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import monitor_ci  # noqa: E402


def _jobs_response(jobs: list[dict], total_count: int = 130) -> Mock:
    response = Mock()
    response.json.return_value = {"total_count": total_count, "jobs": jobs}
    return response


def _analysis(job_id: int, text: str, job_name: str = "test-job") -> dict:
    if text.strip():
        text = f"### Commit Info\n{text}\n\n" + "Evidence-backed detail. " * 12
    return {
        "run_url": "https://example.test/old-run",
        "job_name": job_name,
        "job_id": job_id,
        "head_sha": "old-sha",
        "started_at": "2026-09-15T20:00:00Z",
        "failed_steps": ["old-step"],
        "analysis": text,
    }


class PriorAnalysisIndexTests(unittest.TestCase):
    def test_prior_index_is_workflow_scoped_and_latest_valid_entry_wins(self):
        failed = _analysis(106, "placeholder")
        failed["analysis"] = "agent timed out " + "x" * 300
        recovered_failure = monitor_ci.parse_job_analyses_from_comment(
            monitor_ci._render_per_job_block(failed)
        )
        self.assertEqual(len(recovered_failure), 1)
        self.assertTrue(
            recovered_failure[0]["analysis"].lower().startswith(
                "**analysis did not complete.**"
            )
        )

        state = {
            "daily_comments": {
                "2026-09-13": {
                    "workflows": {
                        "target.yml": {
                            "job_analyses": [
                                _analysis(101, "older"),
                                _analysis(102, "   "),
                                *recovered_failure,
                            ],
                        },
                    },
                },
                "2026-09-14": {
                    "workflows": {
                        "target.yml": {
                            "job_analyses": [
                                _analysis(101, "newer"),
                                _analysis(103, "reusable"),
                            ],
                        },
                        "other.yml": {
                            "job_analyses": [_analysis(104, "wrong workflow")],
                        },
                    },
                },
                "2026-09-15": {
                    "workflows": {
                        "target.yml": {
                            "job_analyses": [_analysis(105, "current day")],
                        },
                    },
                },
            },
        }

        reusable = monitor_ci.get_prior_analyses_by_job_id(
            state, "target.yml", "2026-09-15",
        )

        self.assertEqual(set(reusable), {101, 103})
        self.assertIn("newer", reusable[101]["analysis"])


class MonitorWorkflowReuseTests(unittest.TestCase):
    def setUp(self):
        self.run = {
            "id": 9001,
            "html_url": "https://example.test/current-run",
            "head_sha": "current-sha",
            "status": "completed",
            "conclusion": "failure",
        }
        self.reused_job = {
            "id": 101,
            "name": "same-name",
            "started_at": "2026-09-16T01:00:00Z",
            "steps": [{"name": "current-step", "conclusion": "failure"}],
        }
        self.fresh_job = {
            "id": 202,
            "name": "same-name",
            "started_at": "2026-09-16T02:00:00Z",
            "steps": [{"name": "fresh-step", "conclusion": "failure"}],
        }

    @patch.object(monitor_ci, "remove_agent_worktree")
    @patch.object(monitor_ci, "create_agent_worktree")
    @patch.object(monitor_ci, "analyze_job_with_agent")
    @patch.object(monitor_ci, "get_failed_jobs")
    @patch.object(monitor_ci, "get_workflow_runs")
    def test_reuses_exact_job_id_and_analyzes_only_cache_miss(
        self,
        get_runs,
        get_jobs,
        analyze,
        create_worktree,
        remove_worktree,
    ):
        get_runs.return_value = [self.run]
        get_jobs.return_value = [self.reused_job, self.fresh_job]
        create_worktree.return_value = Path("/tmp/fresh-worktree")
        analyze.return_value = {
            **_analysis(202, "fresh analysis"),
            "run_url": self.run["html_url"],
            "head_sha": self.run["head_sha"],
        }
        reusable = {
            101: _analysis(101, "cached analysis"),
            303: _analysis(303, "same name, different ID", job_name="same-name"),
            999: _analysis(999, "not in current lookup"),
        }

        analyses, job_ids, pending = monitor_ci.monitor_workflow(
            "token",
            "target.yml",
            reusable_job_analyses=reusable,
            use_agent=True,
            agent_repo_path=Path("/workspace/sglang"),
        )

        self.assertEqual({item["job_id"] for item in analyses}, {101, 202})
        self.assertEqual(set(job_ids), {101, 202})
        self.assertEqual(pending, [])
        cached = next(item for item in analyses if item["job_id"] == 101)
        self.assertIn("cached analysis", cached["analysis"])
        self.assertEqual(cached["run_url"], self.run["html_url"])
        self.assertEqual(cached["head_sha"], "current-sha")
        self.assertEqual(cached["failed_steps"], ["current-step"])
        self.assertEqual(reusable[101]["run_url"], "https://example.test/old-run")
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.args[0]["id"], 202)
        create_worktree.assert_called_once_with(202, head_sha="current-sha")
        remove_worktree.assert_called_once_with(Path("/tmp/fresh-worktree"))

    @patch.object(monitor_ci, "analyze_job_with_agent")
    @patch.object(monitor_ci, "get_failed_jobs")
    @patch.object(monitor_ci, "get_workflow_runs")
    def test_today_processed_id_takes_precedence_over_prior_cache(
        self, get_runs, get_jobs, analyze,
    ):
        get_runs.return_value = [self.run]
        get_jobs.return_value = [self.reused_job]

        analyses, job_ids, pending = monitor_ci.monitor_workflow(
            "token",
            "target.yml",
            processed_job_ids={101},
            reusable_job_analyses={101: _analysis(101, "cached analysis")},
            use_agent=True,
            agent_repo_path=Path("/workspace/sglang"),
        )

        self.assertEqual(analyses, [])
        self.assertEqual(job_ids, [])
        self.assertEqual(pending, [])
        analyze.assert_not_called()


class RunJobsPaginationTests(unittest.TestCase):
    def test_get_failed_jobs_includes_failure_from_second_page(self):
        first_page = [
            {"id": job_id, "status": "completed", "conclusion": "success"}
            for job_id in range(1, 101)
        ]
        second_page = [
            {"id": job_id, "status": "completed", "conclusion": "success"}
            for job_id in range(101, 130)
        ] + [{"id": 130, "status": "completed", "conclusion": "failure"}]
        responses = [
            _jobs_response(first_page),
            _jobs_response(second_page),
        ]

        with patch("utils.requests.get", side_effect=responses) as get_jobs_request:
            failed = monitor_ci.get_failed_jobs("token", 9003)

        self.assertEqual([job["id"] for job in failed], [130])
        self.assertEqual(get_jobs_request.call_count, 2)
        self.assertEqual(
            [call.kwargs["params"] for call in get_jobs_request.call_args_list],
            [
                {"filter": "latest", "per_page": 100, "page": 1},
                {"filter": "latest", "per_page": 100, "page": 2},
            ],
        )
        for response in responses:
            response.raise_for_status.assert_called_once_with()

    def test_get_run_jobs_deduplicates_ids_if_pages_shift(self):
        first_page = [
            {"id": job_id, "status": "completed", "conclusion": "success"}
            for job_id in range(1, 101)
        ]
        second_page = [first_page[-1]] + [
            {"id": job_id, "status": "completed", "conclusion": "success"}
            for job_id in range(101, 130)
        ]

        with patch(
            "utils.requests.get",
            side_effect=[
                _jobs_response(first_page, total_count=129),
                _jobs_response(second_page, total_count=129),
            ],
        ):
            jobs = monitor_ci.get_run_jobs("token", 9005)

        self.assertEqual(len(jobs), 129)
        self.assertEqual(len({job["id"] for job in jobs}), 129)

    def test_get_pending_job_info_counts_running_job_from_second_page(self):
        first_page = [
            {"id": job_id, "status": "completed", "conclusion": "success"}
            for job_id in range(1, 101)
        ]
        second_page = [
            {"id": job_id, "status": "completed", "conclusion": "success"}
            for job_id in range(101, 130)
        ] + [{"id": 130, "status": "in_progress", "conclusion": None}]
        responses = [
            _jobs_response(first_page),
            _jobs_response(second_page),
        ]

        with patch("utils.requests.get", side_effect=responses) as get_jobs_request:
            pending = monitor_ci.get_pending_job_info("token", 9004)

        self.assertEqual(pending, {"count": 1, "run_id": 9004})
        self.assertEqual(get_jobs_request.call_count, 2)
        self.assertEqual(
            [call.kwargs["params"]["page"] for call in get_jobs_request.call_args_list],
            [1, 2],
        )
        for response in responses:
            response.raise_for_status.assert_called_once_with()


class InProgressRunTests(unittest.TestCase):
    def test_completed_failed_job_is_analyzed_before_parent_run_finishes(self):
        run = {
            "id": 9002,
            "html_url": "https://example.test/in-progress-run",
            "head_sha": "current-sha",
            "status": "in_progress",
            "conclusion": None,
        }
        completed_failure = {
            "id": 301,
            "name": "completed-failure",
            "status": "completed",
            "conclusion": "failure",
            "started_at": "2026-09-16T03:00:00Z",
            "steps": [{"name": "test", "conclusion": "failure"}],
        }
        running_job = {
            "id": 302,
            "name": "still-running",
            "status": "in_progress",
            "conclusion": None,
            "started_at": "2026-09-16T03:05:00Z",
            "steps": [],
        }
        successful_job = {
            "id": 303,
            "name": "completed-success",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-09-16T03:00:00Z",
            "steps": [{"name": "test", "conclusion": "success"}],
        }
        jobs_response = _jobs_response(
            [completed_failure, running_job, successful_job],
            total_count=3,
        )
        analyze_result = {
            **_analysis(301, "fresh analysis", job_name="completed-failure"),
            "run_url": run["html_url"],
            "head_sha": run["head_sha"],
        }

        with (
            patch.object(monitor_ci, "get_workflow_runs", return_value=[run]),
            patch("utils.requests.get", return_value=jobs_response) as get_jobs_request,
            patch.object(
                monitor_ci,
                "get_pending_job_info",
                return_value={"count": 1, "run_id": run["id"]},
            ) as get_pending,
            patch.object(
                monitor_ci,
                "create_agent_worktree",
                return_value=Path("/tmp/in-progress-worktree"),
            ),
            patch.object(
                monitor_ci, "analyze_job_with_agent", return_value=analyze_result,
            ) as analyze,
            patch.object(monitor_ci, "remove_agent_worktree"),
        ):
            analyses, job_ids, pending = monitor_ci.monitor_workflow(
                "token",
                "target.yml",
                use_agent=True,
                agent_repo_path=Path("/workspace/sglang"),
            )

        jobs_response.raise_for_status.assert_called_once_with()
        self.assertEqual(
            get_jobs_request.call_args.args[0],
            "https://api.github.com/repos/sgl-project/sglang/actions/runs/9002/jobs",
        )
        self.assertEqual([item["job_id"] for item in analyses], [301])
        self.assertEqual(job_ids, [301])
        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.args[0]["id"], 301)
        get_pending.assert_called_once_with("token", 9002)
        self.assertEqual(pending, [{"count": 1, "run_id": 9002}])


class WorkflowConfigurationTests(unittest.TestCase):
    def test_production_matrix_defaults_match_monitor_workflows(self):
        workflow_lines = (
            (ROOT / ".github/workflows/ci-monitor.yml")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        start = next(
            i for i, line in enumerate(workflow_lines)
            if line.strip() == "DEFAULT_WORKFLOWS: |"
        )
        matrix_defaults = []
        for line in workflow_lines[start + 1:]:
            if not line.startswith("    "):
                break
            matrix_defaults.append(line.strip())

        expected = [
            "release-docker-amd-rocm10.yml",
            "release-docker-amd-rocm720-nightly.yml",
            "release-docker-amd-gfx1151-nightly.yml",
            "release-docker-amd-miles-rocm10-nightly.yml",
            "release-docker-amd-miles-rocm724-nightly.yml",
            "pr-test-amd.yml",
            "nightly-test-amd.yml",
            "nightly-amd-mi355x-disagg.yml",
            "nightly-test-amd-miles-rocm10.yml",
            "nightly-test-amd-miles-rocm10-mi30x.yml",
            "amd-aiter-scout.yml",
        ]
        self.assertEqual(monitor_ci.MONITORED_WORKFLOWS, expected)
        self.assertEqual(matrix_defaults, expected)


class JobReportOrderTests(unittest.TestCase):
    def test_pr_test_jobs_follow_stage_order(self):
        names = [
            "multimodal-gen-test-1-gpu-amd (rocm10, linux-mi300-1gpu-sglang, 0)",
            "stage-c-test-large-8-gpu-amd (rocm10, linux-mi300-8gpu-sglang, 2)",
            "unknown-job (rocm10)",
            "jit-kernel-unit-test-amd (rocm10, linux-mi300-1gpu-sglang)",
            "stage-b-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang, 10)",
            "stage-b-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang, 2)",
            "sgl-kernel-unit-test-amd (rocm10, linux-mi300-1gpu-sglang)",
            "stage-a-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang)",
        ]
        jobs = [_analysis(i, "x", job_name=n) for i, n in enumerate(names)]

        ordered = monitor_ci.sort_job_analyses("pr-test-amd.yml", jobs)

        self.assertEqual([ja["job_name"] for ja in ordered], [
            "stage-a-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang)",
            "stage-b-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang, 2)",
            "stage-b-test-1-gpu-small-amd (rocm10, linux-mi300-1gpu-sglang, 10)",
            "stage-c-test-large-8-gpu-amd (rocm10, linux-mi300-8gpu-sglang, 2)",
            "jit-kernel-unit-test-amd (rocm10, linux-mi300-1gpu-sglang)",
            "sgl-kernel-unit-test-amd (rocm10, linux-mi300-1gpu-sglang)",
            "multimodal-gen-test-1-gpu-amd (rocm10, linux-mi300-1gpu-sglang, 0)",
            "unknown-job (rocm10)",
        ])

    def test_pr_test_jobs_group_by_rocm_with_default_first(self):
        names = [
            "stage-a-test-1-gpu-small-amd (rocm720, linux-mi300-1gpu-sglang)",
            "multimodal-gen-test-1-gpu-amd (rocm724, linux-mi300-1gpu-sglang, 0)",
            "stage-a-test-1-gpu-small-amd (rocm724, linux-mi300-1gpu-sglang)",
            "multimodal-gen-test-1-gpu-amd (rocm10, linux-mi300-1gpu-sglang, 0)",
            "stage-c-test-large-8-gpu-amd-mi35x (rocm10, linux-mi35x-gpu-8, 2)",
            "stage-b-test-1-gpu-small-amd (rocm720, linux-mi300-1gpu-sglang, 1)",
        ]
        jobs = [_analysis(i, "x", job_name=n) for i, n in enumerate(names)]

        ordered = monitor_ci.sort_job_analyses("pr-test-amd.yml", jobs)

        self.assertEqual([ja["job_name"] for ja in ordered], [
            "stage-c-test-large-8-gpu-amd-mi35x (rocm10, linux-mi35x-gpu-8, 2)",
            "multimodal-gen-test-1-gpu-amd (rocm10, linux-mi300-1gpu-sglang, 0)",
            "stage-a-test-1-gpu-small-amd (rocm724, linux-mi300-1gpu-sglang)",
            "multimodal-gen-test-1-gpu-amd (rocm724, linux-mi300-1gpu-sglang, 0)",
            "stage-a-test-1-gpu-small-amd (rocm720, linux-mi300-1gpu-sglang)",
            "stage-b-test-1-gpu-small-amd (rocm720, linux-mi300-1gpu-sglang, 1)",
        ])

    def test_other_workflows_keep_their_order(self):
        jobs = [_analysis(1, "x", job_name="b"), _analysis(2, "x", job_name="a")]
        ordered = monitor_ci.sort_job_analyses("nightly-test-amd.yml", jobs)
        self.assertEqual([ja["job_id"] for ja in ordered], [1, 2])

    def test_rendered_pr_test_report_lists_stage_a_first(self):
        jobs = [
            _analysis(1, "x", job_name="multimodal-gen-test-1-gpu-amd (rocm10)"),
            _analysis(2, "x", job_name="stage-a-test-1-gpu-small-amd (rocm10)"),
        ]
        body = monitor_ci.render_workflow_comment("pr-test-amd.yml", jobs)
        self.assertLess(
            body.index("stage-a-test-1-gpu-small-amd"),
            body.index("multimodal-gen-test-1-gpu-amd"),
        )


def _report_comment(cid: int, wf: str, part: int = 1, total: int = 1) -> dict:
    return {
        "id": cid,
        "body": f"<!-- processed_job_ids: {cid} -->\n"
                f"<!-- ci-monitor-part: {part}/{total} -->\n"
                f"## `{wf}` — part {part}\n",
    }


def _cross_run_comment(cid: int, wf: str) -> dict:
    return {
        "id": cid,
        "body": monitor_ci.CROSS_RUN_MARKER_TMPL.format(wf=wf)
        + f"\n## `{wf}` — Cross-Run Pattern Summary\n",
    }


class CommentOrderTests(unittest.TestCase):
    def test_reports_are_rewritten_into_priority_order(self):
        miles = _report_comment(10, "nightly-test-amd-miles-rocm10.yml")
        nightly = _report_comment(11, "nightly-test-amd.yml")
        human = {"id": 12, "body": "looking at R281"}
        pr1 = _report_comment(13, "pr-test-amd.yml", 1, 2)
        pr_cross = _cross_run_comment(14, "pr-test-amd.yml")
        pr2 = _report_comment(15, "pr-test-amd.yml", 2, 2)
        docker = _report_comment(16, "release-docker-amd-rocm720-nightly.yml")

        edits = monitor_ci.plan_comment_order(
            [miles, nightly, human, pr1, pr_cross, pr2, docker]
        )

        layout = {c["id"]: c["body"] for c in (miles, nightly, human, pr1, pr_cross, pr2, docker)}
        layout.update(dict(edits))
        self.assertEqual([layout[i] for i in sorted(layout)], [
            docker["body"], pr1["body"], human["body"], pr2["body"],
            pr_cross["body"], nightly["body"], miles["body"],
        ])
        self.assertNotIn(12, dict(edits))

    def test_ordered_issue_needs_no_edits(self):
        comments = [
            _report_comment(1, "pr-test-amd.yml"),
            _cross_run_comment(2, "pr-test-amd.yml"),
            _report_comment(3, "nightly-test-amd.yml"),
            {"id": 4, "body": "human reply"},
            _report_comment(5, "amd-aiter-scout.yml"),
        ]
        self.assertEqual(monitor_ci.plan_comment_order(comments), [])

    @patch.object(monitor_ci.time, "sleep")
    @patch.object(monitor_ci, "update_comment")
    @patch.object(monitor_ci, "get_issue_comments")
    def test_order_workflow_comments_applies_the_plan(self, get_comments, update, _sleep):
        get_comments.return_value = [
            _report_comment(1, "nightly-test-amd.yml"),
            _report_comment(2, "pr-test-amd.yml"),
        ]
        edited = monitor_ci.order_workflow_comments("token", "owner/bot", 7)
        self.assertEqual(edited, 2)
        self.assertEqual(
            [c.args[2] for c in update.call_args_list], [1, 2],
        )
        self.assertIn("pr-test-amd.yml", update.call_args_list[0].args[3])


class PublishAfterReorderTests(unittest.TestCase):
    @patch.object(monitor_ci, "maybe_publish_cross_run_summary")
    @patch.object(monitor_ci, "post_comment", return_value={"id": 99})
    @patch.object(monitor_ci, "update_comment")
    def test_cached_id_is_not_reused_when_comment_holds_other_report(
        self, update, post, _cross,
    ):
        today = "2026-10-21"
        state = {"daily_comments": {today: {"issue_number": 7, "workflows": {
            "pr-test-amd.yml": {"comment_id": 1, "owned": True, "overflow_ids": [],
                                "job_analyses": []},
        }}}}
        # Comment 1 was rewritten by order_workflow_comments to another report.
        comments = [_report_comment(1, "release-docker-amd-rocm720-nightly.yml")]

        monitor_ci.publish_workflow_report(
            "token", "owner/bot", "pr-test-amd.yml",
            [_analysis(5, "fresh", job_name="stage-a-test")], [], state,
            gh_comments=comments, use_agent=False, date_str=today,
        )

        update.assert_not_called()
        post.assert_called_once()
        wf_state = state["daily_comments"][today]["workflows"]["pr-test-amd.yml"]
        self.assertEqual(wf_state["comment_id"], 99)
        self.assertNotIn("unpublished", wf_state)

    @patch.object(monitor_ci, "post_comment")
    @patch.object(monitor_ci, "get_issue_comments", side_effect=monitor_ci.requests.HTTPError("502"))
    def test_failed_comment_fetch_keeps_analyses_for_next_run(self, _get, post):
        today = "2026-10-21"
        state = {"daily_comments": {today: {"issue_number": 7, "workflows": {}}}}

        with self.assertRaises(monitor_ci.requests.HTTPError):
            monitor_ci.publish_workflow_report(
                "token", "owner/bot", "pr-test-amd.yml",
                [_analysis(5, "fresh")], [], state,
                gh_comments=None, use_agent=False, date_str=today,
            )

        post.assert_not_called()
        wf_state = state["daily_comments"][today]["workflows"]["pr-test-amd.yml"]
        self.assertTrue(wf_state["unpublished"])
        self.assertEqual([ja["job_id"] for ja in wf_state["job_analyses"]], [5])


class CrossJobFailureTests(unittest.TestCase):
    WF = "pr-test-amd.yml"
    TODAY = "2026-10-26"

    def _published(self, cross: str, *analyses: dict) -> dict:
        body = monitor_ci.render_workflow_comment_parts(self.WF, list(analyses), [], cross)[0]
        return {"id": 1, "body": body}

    def _publish(self, comments: list[dict], new: list[dict]) -> str:
        state = {"daily_comments": {self.TODAY: {"issue_number": 7, "workflows": {}}}}
        with patch.object(monitor_ci, "update_comment") as update, \
                patch.object(monitor_ci, "post_comment", return_value={"id": 99}), \
                patch.object(monitor_ci, "maybe_publish_cross_run_summary"):
            monitor_ci.publish_workflow_report(
                "token", "owner/bot", self.WF, new, [], state,
                gh_comments=comments, use_agent=True, date_str=self.TODAY,
            )
        wf_state = state["daily_comments"][self.TODAY]["workflows"][self.WF]
        self.assertNotIn("unpublished", wf_state)
        return update.call_args_list[0].args[3]

    @patch.object(monitor_ci, "cross_job_analysis", side_effect=RuntimeError("Claude Code timed out after 1200s"))
    def test_failed_summary_publishes_jobs_under_the_previous_summary(self, _cross):
        old = self._published("**Counts**: 2 failures · 1 clusters\n\nOLD TABLE", _analysis(1, "a"), _analysis(2, "b"))

        body = self._publish([old], [_analysis(3, "fresh")])

        self.assertIn("OLD TABLE", body)
        self.assertIn(monitor_ci.CROSS_STALE_MARKER, body)
        self.assertIn("timed out after 1200s", body)
        self.assertIn('data-job-id="3"', body)
        self.assertTrue(monitor_ci.cross_summary_is_stale([{"id": 1, "body": body}], self.WF))

        # A second failure keeps one note, not two.
        again = self._publish([{"id": 1, "body": body}], [])
        self.assertEqual(again.count(monitor_ci.CROSS_STALE_MARKER), 1)
        self.assertIn("OLD TABLE", again)

    def test_refreshed_summary_drops_the_stale_note(self):
        with patch.object(monitor_ci, "cross_job_analysis", side_effect=RuntimeError("timeout")):
            stale = self._publish(
                [self._published("**Counts**: old", _analysis(1, "a"), _analysis(2, "b"))], [],
            )
        with patch.object(monitor_ci, "cross_job_analysis", return_value="**Counts**: 2 failures · NEW"):
            body = self._publish([{"id": 1, "body": stale}], [])

        self.assertNotIn(monitor_ci.CROSS_STALE_MARKER, body)
        self.assertIn("NEW", body)
        self.assertFalse(monitor_ci.cross_summary_is_stale([{"id": 1, "body": body}], self.WF))

    def test_published_cross_summary_without_a_summary(self):
        self.assertEqual(monitor_ci.published_cross_summary(None), "")
        no_summary = self._published("", _analysis(1, "a"))
        self.assertEqual(monitor_ci.published_cross_summary(no_summary), "")


class RunOneshotFailureTests(unittest.TestCase):
    @patch.dict(os.environ, {"BUILD_DAILY_SUMMARY": "false"}, clear=False)
    @patch.object(monitor_ci, "save_state")
    @patch.object(monitor_ci, "publish_workflow_report", side_effect=RuntimeError("boom"))
    @patch.object(monitor_ci, "monitor_workflow", return_value=([_analysis(1, "a")], [1], []))
    @patch.object(monitor_ci, "get_issue_comments", return_value=[])
    @patch.object(monitor_ci, "find_daily_issue", return_value=77)
    @patch.object(monitor_ci, "load_state", return_value={})
    def test_errored_workflow_is_returned(self, *_mocks):
        failed = monitor_ci.run_oneshot(
            "token", "owner/bot", "daily-issue", ["a.yml"], 24, "main", use_agent=False,
        )
        self.assertEqual(failed, ["a.yml"])

    @patch.dict(os.environ, {"BUILD_DAILY_SUMMARY": "false"}, clear=False)
    @patch.object(monitor_ci, "save_state")
    @patch.object(monitor_ci, "publish_workflow_report")
    @patch.object(monitor_ci, "monitor_workflow", return_value=([], [], []))
    @patch.object(monitor_ci, "find_daily_issue", return_value=77)
    @patch.object(monitor_ci, "load_state", return_value={})
    def test_stale_summary_is_retried_without_new_failures(self, _load, _find, _monitor, publish, _save):
        stale = {"id": 5, "body": "<!-- ci-monitor-part: 1/1 -->\n## `a.yml` — 2 failure(s)\n"
                                  + monitor_ci.CROSS_STALE_MARKER}
        with patch.object(monitor_ci, "get_issue_comments", return_value=[stale]):
            failed = monitor_ci.run_oneshot(
                "token", "owner/bot", "daily-issue", ["a.yml"], 24, "main", use_agent=False,
            )
        self.assertEqual(failed, [])
        publish.assert_called_once()


class IssueCommentPaginationTests(unittest.TestCase):
    @patch.object(monitor_ci.requests, "get")
    def test_reads_every_page(self, get):
        pages = [
            [{"id": i} for i in range(100)],
            [{"id": i} for i in range(100, 130)],
        ]
        responses = []
        for page in pages:
            resp = Mock()
            resp.json.return_value = page
            responses.append(resp)
        get.side_effect = responses

        comments = monitor_ci.get_issue_comments("token", "owner/bot", 7)

        self.assertEqual(len(comments), 130)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["page"], 2)


class RunOneshotReuseTests(unittest.TestCase):
    @patch.dict(os.environ, {"BUILD_DAILY_SUMMARY": "true"}, clear=False)
    @patch("daily_cross_workflow_summary.build_and_publish_summary")
    @patch.object(monitor_ci, "save_state")
    @patch.object(monitor_ci, "publish_workflow_report")
    @patch.object(monitor_ci, "get_issue_comments", return_value=[])
    @patch.object(monitor_ci, "find_daily_issue", return_value=77)
    @patch.object(monitor_ci, "remove_agent_worktree")
    @patch.object(monitor_ci, "create_agent_worktree")
    @patch.object(monitor_ci, "analyze_job_with_agent")
    @patch.object(monitor_ci, "get_failed_jobs")
    @patch.object(monitor_ci, "get_workflow_runs")
    @patch.object(monitor_ci, "ensure_sglang_repo", return_value=Path("/workspace/sglang"))
    @patch.object(monitor_ci, "claude_code_available", return_value=True)
    @patch.object(monitor_ci, "load_state")
    def test_reused_analysis_is_published_into_new_daily_issue(
        self,
        load_state,
        _claude_available,
        _ensure_repo,
        get_runs,
        get_jobs,
        analyze,
        create_worktree,
        remove_worktree,
        _find_issue,
        _get_comments,
        publish,
        _save_state,
        build_summary,
    ):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        cached = _analysis(101, "cached analysis")
        load_state.return_value = {
            "daily_comments": {
                yesterday: {
                    "issue_number": 76,
                    "workflows": {
                        "target.yml": {"job_analyses": [cached]},
                    },
                },
            },
        }
        get_runs.return_value = [{
            "id": 9001,
            "html_url": "https://example.test/current-run",
            "head_sha": "current-sha",
            "status": "completed",
            "conclusion": "failure",
        }]
        get_jobs.return_value = [{
            "id": 101,
            "name": "test-job",
            "started_at": f"{today}T01:00:00Z",
            "steps": [{"name": "test", "conclusion": "failure"}],
        }]

        monitor_ci.run_oneshot(
            "token",
            "owner/bot",
            "daily-issue",
            ["target.yml"],
            24,
            "main",
            use_agent=True,
        )

        analyze.assert_not_called()
        create_worktree.assert_not_called()
        remove_worktree.assert_not_called()
        publish.assert_called_once()
        published_analyses = publish.call_args.args[3]
        self.assertEqual(len(published_analyses), 1)
        self.assertEqual(published_analyses[0]["job_id"], 101)
        self.assertEqual(published_analyses[0]["analysis"], cached["analysis"])
        self.assertEqual(publish.call_args.kwargs["date_str"], today)
        build_summary.assert_called_once_with(
            "token", "owner/bot", use_agent=True, date_str=today,
        )


if __name__ == "__main__":
    unittest.main()
