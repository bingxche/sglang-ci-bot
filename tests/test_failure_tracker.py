import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import failure_tracker as ft  # noqa: E402
import utils  # noqa: E402

WF = "pr-test-amd.yml"
CFG = ft.TRACKED_WORKFLOWS[WF]
SNAPSHOT = "2026-11-12 07:26 UTC"
TODAY = "2026-11-12"
DAILY = "https://github.com/bingxche/sglang-ci-bot/issues/287"


def _entry(name, first, last, error="AssertionError", **extra):
    e = {
        "test_file": f"test/{name}.py", "test_function": "test_x",
        "cluster": "", "error": error, "job_url": "", "job_name": "", "job_id": None,
        "detail_url": "", "first_seen": first, "last_seen": last, "fail_runs": ["1"],
        "observed": 1, "streak": 1,
    }
    e.update(extra)
    return e


def _state(*entries, archive=()):
    state = ft.upgrade_state({"version": ft.STATE_VERSION, "active": {}, "archive": []})
    for e in entries:
        state["active"][f"{e['test_file']}::{e['test_function']}"] = e
    state["archive"] = [list(a) for a in archive]
    return state


def _row(tf, fn, run="100", **extra):
    url = f"https://github.com/sgl-project/sglang/actions/runs/{run}/job/9{run}"
    return {"workflow": WF, "test_file": tf, "test_function": fn, "error": "boom",
            "job_url": url, "job_id": int(f"9{run}"), **extra}


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _analysis(run, job, table_rows):
    rows = "\n".join(f"| `{tf}` | `{fn}` | err | x |" for tf, fn in table_rows)
    return {
        "job_id": job, "job_name": f"job-{job}",
        "run_url": f"https://github.com/sgl-project/sglang/actions/runs/{run}",
        "analysis": f"### Failed Tests\n| Test File | Test Function | Error | Note |\n|---|---|---|---|\n{rows}\n",
    }


class CanonicalTestTest(unittest.TestCase):
    def test_variants_of_one_test_share_a_key(self):
        # Seen in sgl-project/sglang#45847: one failure, several rows.
        variants = [
            ("`t/a.py`", "TestA::test_b[x]"),
            ("./t/a.py", "TestA.test_b[x]"),
            ("t/a.py", "`TestA.test_b[x]` (most likely; rank 0 did not name it)"),
        ]
        self.assertEqual({ft.canonical_test(*v) for v in variants}, {("t/a.py", "TestA.test_b[x]")})

    def test_placeholders_name_no_function(self):
        for fn in ("N/A", "N/A (module import)", "unknown (likely test_x[17])",
                   "unidentified (likely x)", "—", ""):
            self.assertEqual(ft.canonical_test("t/a.py", fn), ("t/a.py", ""), fn)
        self.assertEqual(ft.canonical_test("t/a.py", "none_left")[1], "none_left")

    def test_step_descriptors_share_one_form(self):
        # Every one of these named the same timeout on #45847 on 2026-11-13.
        for desc in (
            "step: Run diffusion unit tests (multimodal-gen-test-1-gpu-amd)",
            "step timeout: Run diffusion unit tests",
            "step 'Run diffusion unit tests' timeout",
            "step Run diffusion unit tests timed out",
            "(step-level) Run diffusion unit tests, run_suite.py --suite unit --partition-id 3",
            "step: Run diffusion unit tests (not a test)",
        ):
            self.assertEqual(ft.canonical_test(desc, "N/A"), ("step: Run diffusion unit tests", ""), desc)
        self.assertEqual(
            ft.canonical_test("N/A: pre-flight step Ensure VRAM is clear (scripts/ci/amd/x.sh rocm)", ""),
            ("step: Ensure VRAM is clear", ""))

    def test_lost_runner_keeps_its_step_but_not_its_job(self):
        self.assertEqual(
            ft.canonical_test("runner lost during Run diffusion server tests (1-GPU) "
                              "(multimodal-gen-test-1-gpu-amd (rocm724, 2))", ""),
            ("runner lost during Run diffusion server tests (1-GPU)", ""))

    def test_a_real_test_file_is_left_alone(self):
        self.assertEqual(ft.canonical_test("test/registered/amd/test_x.py", "test_y"),
                         ("test/registered/amd/test_x.py", "test_y"))


class SplitTestsTest(unittest.TestCase):
    def test_one_cell_naming_several_tests(self):
        self.assertEqual(
            [fn for _, fn in ft.split_tests(
                "t/v.py", "test_a, test_b[x / y], test_c[1], [2], 10 tests, subtest q='z'")],
            ["test_a", "test_b[x]", "test_b[y]", "test_c[1]", "test_c[2]"])

    def test_notes_after_a_colon_and_star_variants(self):
        self.assertEqual(ft.split_tests("t/a.py", "TestA.test_b: subtests A and B"),
                         [("t/a.py", "TestA.test_b")])
        self.assertEqual(ft.split_tests("t/a.py", "test_b[*]"), [("t/a.py", "test_b")])

    def test_commas_inside_brackets_stay(self):
        self.assertEqual(ft.split_tests("t/a.py", "test_x[a, b]"), [("t/a.py", "test_x[a, b]")])


class UpgradeStateTest(unittest.TestCase):
    def test_version_2_drops_ids_and_days_and_needs_backfill(self):
        v2 = {
            "version": 2, "next_id": 3,
            "active": {
                "t/a.py::TestA::test_b": {"id": "A1", "test_file": "t/a.py", "test_function": "TestA::test_b",
                                          "first_seen": "2026-10-22", "last_seen": "2026-10-23",
                                          "seen_days": 2, "streak_start": "2026-10-22"},
                "t/a.py::TestA.test_b": {"id": "A2", "test_file": "t/a.py", "test_function": "TestA.test_b",
                                         "first_seen": "2026-10-24", "last_seen": "2026-10-25",
                                         "error": "newer", "seen_days": 2},
            },
            "archive": [["A9", "t/z.py::f[*]", "2026-09-01", "2026-09-02", 2, "old"]],
        }
        state = ft.upgrade_state(v2)
        self.assertEqual(list(state["active"]), ["t/a.py::TestA.test_b"])
        merged = state["active"]["t/a.py::TestA.test_b"]
        self.assertEqual((merged["first_seen"], merged["last_seen"], merged["error"]),
                         ("2026-10-22", "2026-10-25", "newer"))
        self.assertFalse({"id", "seen_days", "streak_start"} & set(merged))
        self.assertEqual(state["archive"], [["t/z.py::f", "2026-09-01", "2026-09-02", 0, 0, "old"]])
        self.assertTrue(ft.needs_backfill(state))

    def test_version_1_still_loads(self):
        state = ft.upgrade_state({"t/c.py::N/A": {"test_file": "t/c.py", "test_function": "N/A",
                                                  "first_seen": "2026-10-21", "last_seen": "2026-10-21"}})
        self.assertEqual(list(state["active"]), ["t/c.py::"])

    def test_round_trip_through_the_body(self):
        state = _state(_entry("a", "2026-11-01", TODAY, variants=["rocm10"]),
                       archive=[["t/z.py::", "2026-09-01", "2026-09-02", 2, 5, "e"]])
        state["runs"] = {"1": ["2026-11-01T01:10:00Z", "rocm10"]}
        body = ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)
        self.assertEqual(ft.load_state(body), state)
        self.assertFalse(ft.needs_backfill(ft.load_state(body)))

    def test_comment_closer_in_text_cannot_break_the_blob(self):
        state = _state(_entry("a", TODAY, TODAY, error="x --> y"))
        body = ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)
        self.assertEqual(ft.load_state(body)["active"], state["active"])


class MergeStateTest(unittest.TestCase):
    def test_runs_are_recorded_once_per_run(self):
        state = _state()
        ft.merge_state(state, [_row("t/a.py", "test_a", run="100")], "2026-11-10", "")
        ft.merge_state(state, [_row("t/a.py", "test_a", run="100")], "2026-11-10", "")  # hourly tick
        ft.merge_state(state, [_row("t/a.py", "test_a", run="101")], "2026-11-11", "")
        e = state["active"]["t/a.py::test_a"]
        self.assertEqual((e["fail_runs"], e["first_seen"], e["last_seen"]),
                         (["100", "101"], "2026-11-10", "2026-11-11"))

    def test_one_row_naming_several_tests_becomes_several_entries(self):
        state = _state()
        ft.merge_state(state, [_row("t/v.py", "test_a, test_b[x / y]")], TODAY, "")
        self.assertEqual(sorted(state["active"]), ["t/v.py::test_a", "t/v.py::test_b[x]", "t/v.py::test_b[y]"])

    def test_fix_pr_is_kept_until_the_analysis_says_none(self):
        state = _state()
        ft.merge_state(state, [_row("t/a.py", "f", fix_pr="[#1](u)")], "2026-11-10", "")
        ft.merge_state(state, [_row("t/a.py", "f")], "2026-11-11", "")
        self.assertEqual(state["active"]["t/a.py::f"]["fix_pr"], "[#1](u)")
        ft.merge_state(state, [_row("t/a.py", "f", fix_pr="")], "2026-11-12", "")
        self.assertEqual(state["active"]["t/a.py::f"]["fix_pr"], "")

    def test_archived_failure_comes_back_with_a_fresh_history(self):
        state = _state(archive=[["t/a.py::f", "2026-08-01", "2026-09-01", 5, 9, "old"]])
        ft.merge_state(state, [_row("t/a.py", "f", run="200")], TODAY, "")
        e = state["active"]["t/a.py::f"]
        self.assertEqual((e["first_seen"], e["back_from"], e["fail_runs"]), (TODAY, "2026-09-01", ["200"]))
        self.assertEqual(state["archive"], [])
        self.assertEqual(ft.classify(e, TODAY), "flaky")


class RecordTableRunsTest(unittest.TestCase):
    def test_adds_runs_to_tracked_tests_and_resolves_same_file(self):
        state = _state(_entry("a", TODAY, TODAY, test_file="t/a.py", test_function="test_1", fail_runs=["100"]),
                       _entry("a", TODAY, TODAY, test_file="t/a.py", test_function="test_2", fail_runs=[]))
        added = ft.record_table_runs(state, [
            _analysis("101", 1, [("t/a.py", "test_1"), ("same file", "test_2"), ("t/new.py", "test_n")]),
            _analysis("100", 2, [("t/a.py", "test_1")]),
        ])
        self.assertEqual(added, 2)
        self.assertEqual(state["active"]["t/a.py::test_1"]["fail_runs"], ["100", "101"])
        self.assertEqual(state["active"]["t/a.py::test_2"]["fail_runs"], ["101"])
        self.assertNotIn("t/new.py::test_n", state["active"])  # only tracked tests are touched


class ApplyRunStatsTest(unittest.TestCase):
    RUNS = [  # one ROCm image per scheduled run, as on pr-test-amd.yml
        ("1", "2026-11-10T01:10:00Z", "rocm10"), ("2", "2026-11-10T13:10:00Z", "rocm10"),
        ("3", "2026-11-10T19:00:00Z", "rocm724"), ("4", "2026-11-11T01:10:00Z", "rocm10"),
        ("5", "2026-11-11T13:10:00Z", "rocm10"), ("6", "2026-11-11T19:00:00Z", "rocm724"),
        ("7", "2026-11-12T01:10:00Z", "rocm10"),
    ]

    def _stats(self, fail_runs, first=TODAY):
        state = _state(_entry("a", first, TODAY, fail_runs=fail_runs),
                       _entry("horizon", TODAY, TODAY, fail_runs=["7"]))
        ft.apply_run_stats(state, self.RUNS)
        return state["active"]["test/a.py::test_x"]

    def test_counts_only_runs_of_the_images_it_failed_on(self):
        e = self._stats(["3", "6"])
        self.assertEqual((e["observed"], e["streak"], e["variants"]), (2, 2, ["rocm724"]))
        self.assertEqual(e["first_seen"], "2026-11-10")  # moved back to the first failing run
        self.assertEqual(ft._notes_cell(e, TODAY), "broken · failed 2 of 2 runs on rocm724")

    def test_streak_counts_back_from_the_newest_run(self):
        e = self._stats(["1", "4", "5", "7"])
        self.assertEqual((e["observed"], e["streak"]), (5, 3))
        self.assertEqual(ft._notes_cell(e, TODAY),
                         "broken, last 3 runs in a row · failed 4 of 5 runs on rocm10")
        e = self._stats(["2", "7"])
        self.assertEqual((e["observed"], e["streak"], ft.classify(e, TODAY)), (4, 1, "flaky"))

    def test_runs_newer_than_any_analysis_are_not_counted_yet(self):
        state = _state(_entry("a", TODAY, TODAY, fail_runs=["1", "2"]))
        ft.apply_run_stats(state, self.RUNS)
        e = state["active"]["test/a.py::test_x"]
        self.assertEqual((e["observed"], e["streak"]), (2, 2))

    def test_a_failing_run_still_in_progress_counts_as_newest(self):
        e = self._stats(["4", "5", "7", "8"])
        self.assertEqual((e["observed"], e["streak"]), (4, 4))


class ArchiveQuietTest(unittest.TestCase):
    def test_archives_quiet_past_retention_and_drops_old_archive(self):
        state = _state(
            _entry("failing", "2026-08-01", TODAY),
            _entry("edge", "2026-10-01", "2026-10-13"),  # 30 days quiet: stays
            _entry("old", "2026-09-01", "2026-10-12", fail_runs=["1", "2"], observed=4),  # 31 days
            _entry("bad", "?", "not-a-date"),
            archive=[["t/gone.py::", "2026-01-01", "2026-05-01", 1, 1, "e"]],  # > 180 days
        )
        self.assertEqual(ft.archive_quiet(state, TODAY, 30), (1, 1))
        self.assertEqual(sorted(state["active"]), ["test/bad.py::test_x", "test/edge.py::test_x",
                                                   "test/failing.py::test_x"])
        self.assertEqual(state["archive"],
                         [["test/old.py::test_x", "2026-09-01", "2026-10-12", 2, 4, "AssertionError"]])
        # A failure still red keeps its original first_seen.
        self.assertEqual(state["active"]["test/failing.py::test_x"]["first_seen"], "2026-08-01")


class FixPrsByJobTest(unittest.TestCase):
    def test_reads_the_in_flight_fix_line_in_its_variants(self):
        analyses = [
            {"job_id": 1, "analysis": "- In-flight fix: ✅ [#49711](https://github.com/sgl-project/sglang/pull/49711) (open)"},
            {"job_id": 2, "analysis": "**In-flight fix:** ✅ [radixark/miles#4803](https://github.com/radixark/miles/pull/4803)"},
            {"job_id": 3, "analysis": "In-flight fix: ❌ none found"},
            {"job_id": 4, "analysis": "In-flight fix: ✅ landed upstream"},
        ]
        self.assertEqual(ft.fix_prs_by_job(analyses), {
            "1": "[#49711](https://github.com/sgl-project/sglang/pull/49711)",
            "2": "[radixark/miles#4803](https://github.com/radixark/miles/pull/4803)",
            "3": "",
        })


class TruncateTest(unittest.TestCase):
    def test_cut_never_leaves_a_backslash_before_the_cell_pipe(self):
        text = "a" * 108 + "| tail"
        out = ft._one_line(text)
        self.assertFalse(out[:-1].endswith("\\"), out[-5:])
        entry = _entry("a", TODAY, TODAY, error="b" * 78 + "\\| tail")
        self.assertFalse(ft._archive_row(entry)[5][:-1].endswith("\\"))


class RenderTest(unittest.TestCase):
    def _body(self):
        state = _state(
            _entry("old_broken", "2026-11-01", TODAY, fail_runs=["1", "2", "3"], observed=3, streak=3,
                   variants=["rocm724"], job_name="job-a",
                   job_url="https://github.com/sgl-project/sglang/actions/runs/3/job/30",
                   fix_pr="[#9](https://github.com/sgl-project/sglang/pull/9)"),
            _entry("new", TODAY, TODAY, test_file="test/sub/new.py"),
            _entry("quiet", "2026-11-01", "2026-11-05", fail_runs=["1", "2"], observed=6),
            archive=[["t/z.py::f", "2026-09-01", "2026-09-02", 2, 5, "old error"]],
        )
        return ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)

    def test_layout_follows_17050(self):
        body = self._body()
        self.assertIn("**Last auto-update:** 2026-11-12 07:26 UTC — 1 broken, 0 flaky, 1 new, "
                      "2 no longer failing", body)
        self.assertIn(f"- **[Daily report]({DAILY})**", body)
        self.assertIn(f"- **[CI Failure Monitor]({ft.CI_FAILURE_MONITOR_URL})**", body)
        self.assertIn("| Fix PR (from AI analysis, may be wrong) |", body)
        self.assertNotIn("| ID |", body)

    def test_rows_link_the_job_and_count_runs(self):
        body = self._body()
        self.assertIn("| 2026-11-01 | `old_broken.py::test_x` "
                      "| [job-a](https://github.com/sgl-project/sglang/actions/runs/3/job/30) "
                      "| AssertionError | broken · failed 3 of 3 runs on rocm724 "
                      "| [#9](https://github.com/sgl-project/sglang/pull/9) | — |", body)
        self.assertIn("| 2026-11-05 | `quiet.py::test_x` — AssertionError "
                      "(failed 2 of 6 runs since 2026-11-01) | - |", body)
        self.assertIn("`z.py::f` — old error (failed 2 of 5 runs since 2026-09-01)", body)

    def test_newest_failures_first(self):
        failing = self._body().split("## Currently failing")[1].split("**Notes:**")[0]
        self.assertLess(failing.index("new.py"), failing.index("old_broken.py"))


class RenderBoundedBodyTest(unittest.TestCase):
    def _state(self, n_quiet, n_archive=0):
        quiet = [
            _entry(f"q{i:03d}", "2026-11-01", f"2026-11-{1 + i % 10:02d}", "E" * 100)
            for i in range(n_quiet)
        ]
        archive = [
            [f"t/a{i}.py::", "2026-09-01", f"2026-09-{1 + i % 20:02d}", 1, 1, "E" * 60]
            for i in range(n_archive)
        ]
        return _state(_entry("failing", "2026-11-01", TODAY), *quiet, archive=archive)

    def test_fits_without_dropping(self):
        state = self._state(3, 3)
        body, dropped = ft.render_bounded_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)
        self.assertEqual(dropped, 0)
        self.assertEqual((len(state["active"]), len(state["archive"])), (4, 3))

    def test_sheds_oldest_archive_first(self):
        state = self._state(40, 40)
        full = len(ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY).encode("utf-8"))
        archive_last = sorted(a[2] for a in state["archive"])
        body, dropped = ft.render_bounded_body(
            WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=full - 1500,
        )
        self.assertLessEqual(len(body.encode("utf-8")), full - 1500)
        self.assertGreater(dropped, 0)
        self.assertEqual(len(state["active"]), 41)  # the archive alone covered it
        self.assertLessEqual(archive_last[dropped - 1], min(a[2] for a in state["archive"]))

    def test_then_archives_oldest_quiet(self):
        state = self._state(40)
        full = len(ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY).encode("utf-8"))
        quiet_before = {k: e["last_seen"] for k, e in state["active"].items()}
        body, _ = ft.render_bounded_body(
            WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=full - 1500,
        )
        self.assertLessEqual(len(body.encode("utf-8")), full - 1500)
        self.assertIn("test/failing.py::test_x", state["active"])
        gone = [k for k in quiet_before if k not in state["active"]]
        self.assertTrue(gone)
        kept = [e["last_seen"] for e in state["active"].values() if e["last_seen"] != TODAY]
        self.assertLessEqual(max(quiet_before[k] for k in gone), min(kept))

    def test_raises_when_todays_failures_alone_do_not_fit(self):
        state = _state(_entry("failing", "2026-11-01", TODAY))
        with self.assertRaises(ValueError):
            ft.render_bounded_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=100)


class RunListTest(unittest.TestCase):
    def test_scheduled_runs_keeps_completed_passes_and_failures(self):
        page = {"workflow_runs": [
            {"id": 1, "created_at": "a", "conclusion": "failure"},
            {"id": 2, "created_at": "b", "conclusion": "cancelled"},
            {"id": 3, "created_at": "c", "conclusion": "success"},
        ]}
        with patch.object(ft, "gh_get", return_value=_resp(page)) as get:
            self.assertEqual(ft.scheduled_runs("t", WF, "2026-10-01"), [("1", "a"), ("3", "c")])
        params = get.call_args.kwargs["params"]
        self.assertEqual((params["event"], params["branch"], params["created"]),
                         ("schedule", "main", ">=2026-10-01"))

    def test_variants_are_read_once_and_cached(self):
        state = _state()
        jobs = {"jobs": [{"name": "stage-a (rocm724, linux-mi35x)"}]}
        with patch.object(ft, "gh_get", return_value=_resp(jobs)) as get:
            out = ft.with_variants("t", state, [("5", "c")])
            ft.with_variants("t", state, [("5", "c")])
        self.assertEqual(out, [("5", "c", "rocm724")])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(state["runs"], {"5": ["c", "rocm724"]})


class BackfillRunsTest(unittest.TestCase):
    def test_rebuilds_runs_from_daily_issues_since_first_seen(self):
        state = ft.upgrade_state({"version": 2, "active": {"t/a.py::f": {
            "test_file": "t/a.py", "test_function": "f", "first_seen": "2026-11-10",
            "last_seen": TODAY, "job_url": "https://github.com/sgl-project/sglang/actions/runs/300/job/1"}}})
        issues = [
            {"number": 1, "title": "[CI Monitor] Daily Report - 2026-11-09"},
            {"number": 2, "title": "[CI Monitor] Daily Report - 2026-11-11"},
            {"number": 3, "title": "Something else"},
        ]
        tables = {2: {WF: [_analysis("200", 7, [("t/a.py", "f")])]}}
        with patch.object(ft, "gh_get", return_value=_resp(issues)), \
             patch.object(ft, "collect_tracked_analyses",
                          side_effect=lambda t, r, n: tables.get(n, {WF: []})) as collect:
            ft.backfill_runs("t", "b/r", state, WF)
        self.assertEqual([c.args[2] for c in collect.call_args_list], [2])
        self.assertEqual(state["active"]["t/a.py::f"]["fail_runs"], ["200", "300"])
        self.assertFalse(ft.needs_backfill(state))


class UpdateOneWorkflowTest(unittest.TestCase):
    ROWS = [{"workflow": WF, "test_file": "test/a.py", "test_function": "test_a", "error": "boom"}]

    def setUp(self):
        runs = patch.object(ft, "scheduled_runs", return_value=[])
        runs.start()
        self.addCleanup(runs.stop)

    def test_body_that_was_not_saved_raises(self):
        # GitHub answers 200 to an oversized body and keeps the old one.
        reads = [{"body": ""}, {"body": "stale"}, {"body": "stale"}]
        with patch.object(ft, "find_or_create_tracker_issue", return_value=45847), \
             patch.object(ft, "get_issue", side_effect=reads), \
             patch.object(ft, "update_issue_body"), \
             patch.object(ft.time, "sleep") as sleep:
            with self.assertRaises(RuntimeError):
                ft.update_one_workflow("t", WF, CFG, "bingxche/sglang-ci-bot", 287, self.ROWS, SNAPSHOT)
        sleep.assert_called_with(ft.READ_BACK_DELAY_SECS)

    def test_lagging_first_read_back_is_retried(self):
        sent = {}
        reads = iter([{"body": ""}, {"body": "stale"}])

        def fake_get(token, repo, num):
            return next(reads, None) or {"body": sent["body"]}

        with patch.object(ft, "find_or_create_tracker_issue", return_value=45847), \
             patch.object(ft, "get_issue", side_effect=fake_get), \
             patch.object(ft, "update_issue_body",
                          side_effect=lambda t, r, n, b: sent.setdefault("body", b)), \
             patch.object(ft.time, "sleep"):
            self.assertEqual(
                ft.update_one_workflow("t", WF, CFG, "bingxche/sglang-ci-bot", 287, self.ROWS, SNAPSHOT),
                45847,
            )

    def test_saved_body_returns_issue(self):
        captured = {}

        def fake_get(token, repo, num):
            if "sent" in captured:
                return {"body": captured["sent"]}
            return {"body": ""}

        with patch.object(ft, "find_or_create_tracker_issue", return_value=45847), \
             patch.object(ft, "get_issue", side_effect=fake_get), \
             patch.object(ft, "update_issue_body",
                          side_effect=lambda t, r, n, b: captured.setdefault("sent", b)):
            self.assertEqual(
                ft.update_one_workflow("t", WF, CFG, "bingxche/sglang-ci-bot", 287, self.ROWS, SNAPSHOT),
                45847,
            )
        self.assertIn("test/a.py::test_a", ft.load_state(captured["sent"])["active"])


class FindTrackerIssueTest(unittest.TestCase):
    TITLE = CFG["title"]

    def _issue(self, num, updated, title=None, state="open"):
        return {"number": num, "title": title or self.TITLE, "updated_at": updated, "state": state}

    def test_label_listing_wins_without_search(self):
        listing = [self._issue(45847, "2026-10-25T12:07:09Z")]
        with patch.object(ft, "gh_get", return_value=_resp(listing)) as get:
            self.assertEqual(ft.find_tracker_issue("t", self.TITLE), 45847)
        self.assertEqual(get.call_count, 1)
        self.assertIn("/issues", get.call_args.args[0])

    def test_falls_back_to_search_when_label_is_missing(self):
        responses = [_resp([]), _resp({"items": [self._issue(45847, "2026-10-25T12:07:09Z")]})]
        with patch.object(ft, "gh_get", side_effect=responses) as get:
            self.assertEqual(ft.find_tracker_issue("t", self.TITLE), 45847)
        self.assertIn("search", get.call_args.args[0])

    def test_none_when_nothing_matches(self):
        responses = [_resp([self._issue(1, "x", title="other")]), _resp({"items": []})]
        with patch.object(ft, "gh_get", side_effect=responses):
            self.assertIsNone(ft.find_tracker_issue("t", self.TITLE))

    def test_duplicates_pick_the_oldest_open_issue(self):
        # Stable whatever gets commented on; a closed one is skipped even if
        # the listing still returns it.
        listing = [
            self._issue(45847, "2026-11-12T00:00:00Z"),
            self._issue(45835, "2026-11-12T00:00:01Z"),
            self._issue(37451, "2026-11-12T00:00:02Z", state="closed"),
        ]
        with patch.object(ft, "gh_get", return_value=_resp(listing)):
            self.assertEqual(ft.find_tracker_issue("t", self.TITLE), 45835)


class MainExitCodeTest(unittest.TestCase):
    def _main(self, results):
        argv = ["failure_tracker.py", "--bot-repo", "b/r", "--github-token", "t"]
        with patch.object(sys, "argv", argv), \
             patch.object(ft, "update_trackers", return_value=results):
            return ft.main()

    def test_failed_tracker_fails_the_step(self):
        self.assertEqual(self._main({WF: None}), 1)

    def test_success_and_no_daily_issue_pass(self):
        self.assertEqual(self._main({WF: 45847}), 0)
        self.assertEqual(self._main({}), 0)


class UpdateIssueBodySizeTest(unittest.TestCase):
    def test_oversized_body_is_refused_before_patch(self):
        body = "详" * (utils.GITHUB_ISSUE_BODY_MAX_BYTES // 3 + 1)
        with patch.object(utils.requests, "patch") as p:
            with self.assertRaises(ValueError):
                utils.update_issue_body("t", "o/r", 1, body)
        p.assert_not_called()

    def test_body_at_the_limit_is_sent(self):
        with patch.object(utils.requests, "patch", return_value=_resp({})) as p:
            utils.update_issue_body("t", "o/r", 1, "a" * utils.GITHUB_ISSUE_BODY_MAX_BYTES)
        p.assert_called_once()


if __name__ == "__main__":
    unittest.main()
