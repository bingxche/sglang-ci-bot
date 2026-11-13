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
        "cluster": "", "error": error, "job_url": "", "job_id": None,
        "detail_url": "", "first_seen": first, "last_seen": last,
        "seen_days": 1, "streak_start": last,
    }
    e.update(extra)
    return e


def _state(*entries, archive=()):
    state = ft.upgrade_state({"version": ft.STATE_VERSION, "next_id": 1, "active": {}, "archive": []})
    for e in entries:
        e.setdefault("id", ft._next_id(state))
        state["active"][f"{e['test_file']}::{e['test_function']}"] = e
    state["archive"] = [list(a) for a in archive]
    return state


def _row(tf, fn, **extra):
    return {"workflow": WF, "test_file": tf, "test_function": fn, "error": "boom", **extra}


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


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


class UpgradeStateTest(unittest.TestCase):
    def test_version_1_is_rekeyed_merged_and_numbered(self):
        v1 = {
            "t/a.py::TestA::test_b": _entry("x", "2026-10-22", "2026-10-23",
                                            test_file="t/a.py", test_function="TestA::test_b"),
            "t/a.py::TestA.test_b": _entry("x", "2026-10-24", "2026-10-25", error="newer",
                                           test_file="t/a.py", test_function="TestA.test_b"),
            "t/c.py::N/A": _entry("x", "2026-10-21", "2026-10-21",
                                  test_file="t/c.py", test_function="N/A"),
        }
        for e in v1.values():
            del e["seen_days"], e["streak_start"]
        state = ft.upgrade_state(v1)
        self.assertEqual(sorted(state["active"]), ["t/a.py::TestA.test_b", "t/c.py::"])
        merged = state["active"]["t/a.py::TestA.test_b"]
        self.assertEqual((merged["first_seen"], merged["last_seen"], merged["error"]),
                         ("2026-10-22", "2026-10-25", "newer"))
        # IDs follow first_seen and are never handed out twice.
        self.assertEqual(state["active"]["t/c.py::"]["id"], "A1")
        self.assertEqual(merged["id"], "A2")
        self.assertEqual(state["next_id"], 3)

    def test_round_trip_through_the_body(self):
        state = _state(_entry("a", "2026-11-01", TODAY),
                       archive=[["A9", "t/z.py::", "2026-09-01", "2026-09-02", 2, "e"]])
        body = ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)
        self.assertEqual(ft.load_state(body), state)

    def test_comment_closer_in_text_cannot_break_the_blob(self):
        state = _state(_entry("a", TODAY, TODAY, error="x --> y"))
        body = ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)
        self.assertEqual(ft.load_state(body)["active"], state["active"])


class MergeStateTest(unittest.TestCase):
    def test_days_streak_and_classification(self):
        state = _state()
        rows = [_row("t/a.py", "test_a")]
        ft.merge_state(state, rows, "2026-11-10", "")
        e = state["active"]["t/a.py::test_a"]
        self.assertEqual((e["id"], e["seen_days"]), ("A1", 1))
        self.assertEqual(ft.classify(e, "2026-11-10"), "new")
        ft.merge_state(state, rows, "2026-11-10", "")  # hourly tick, same day
        ft.merge_state(state, rows, "2026-11-11", "")
        self.assertEqual(e["seen_days"], 2)
        self.assertEqual(ft.classify(e, "2026-11-11"), "broken")
        ft.merge_state(state, rows, "2026-11-13", "")  # skipped a day
        self.assertEqual((e["seen_days"], e["streak_start"], e["first_seen"]),
                         (3, "2026-11-13", "2026-11-10"))
        self.assertEqual(ft.classify(e, "2026-11-13"), "intermittent")
        self.assertEqual(ft.classify(e, "2026-11-14"), "quiet")

    def test_fix_pr_is_kept_until_the_analysis_says_none(self):
        state = _state()
        ft.merge_state(state, [_row("t/a.py", "f", fix_pr="[#1](u)")], "2026-11-10", "")
        ft.merge_state(state, [_row("t/a.py", "f")], "2026-11-11", "")
        self.assertEqual(state["active"]["t/a.py::f"]["fix_pr"], "[#1](u)")
        ft.merge_state(state, [_row("t/a.py", "f", fix_pr="")], "2026-11-12", "")
        self.assertEqual(state["active"]["t/a.py::f"]["fix_pr"], "")

    def test_archived_failure_comes_back_with_its_id(self):
        state = _state(archive=[["A4", "t/a.py::f", "2026-08-01", "2026-09-01", 5, "old"]])
        state["next_id"] = 9
        ft.merge_state(state, [_row("t/a.py", "f")], TODAY, "")
        e = state["active"]["t/a.py::f"]
        self.assertEqual((e["id"], e["first_seen"], e["back_from"], e["seen_days"]),
                         ("A4", TODAY, "2026-09-01", 1))
        self.assertEqual(state["archive"], [])
        self.assertEqual(state["next_id"], 9)
        self.assertEqual(ft.classify(e, TODAY), "intermittent")


class ArchiveQuietTest(unittest.TestCase):
    def test_archives_quiet_past_retention_and_drops_old_archive(self):
        state = _state(
            _entry("failing", "2026-08-01", TODAY),
            _entry("edge", "2026-10-01", "2026-10-13"),  # 30 days quiet: stays
            _entry("old", "2026-09-01", "2026-10-12"),   # 31 days quiet: archived
            _entry("bad", "?", "not-a-date"),
            archive=[["A50", "t/gone.py::", "2026-01-01", "2026-05-01", 1, "e"]],  # > 180 days
        )
        self.assertEqual(ft.archive_quiet(state, TODAY, 30), (1, 1))
        self.assertEqual(sorted(state["active"]), ["test/bad.py::test_x", "test/edge.py::test_x",
                                                   "test/failing.py::test_x"])
        self.assertEqual([a[1] for a in state["archive"]], ["test/old.py::test_x"])
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
    def test_summary_line_and_archive_section(self):
        state = _state(
            _entry("broken", "2026-11-01", TODAY, streak_start="2026-11-10", seen_days=5),
            _entry("new", TODAY, TODAY),
            _entry("quiet", "2026-11-01", "2026-11-05"),
            archive=[["A9", "t/z.py::f", "2026-09-01", "2026-09-02", 2, "old error"]],
        )
        body = ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)
        self.assertIn("**1 broken · 0 intermittent · 1 new · 1 quiet · 1 archived**", body)
        self.assertIn("🔴 broken 3d", body)
        self.assertIn("| A9 | `t/z.py::f` | 2026-09-01 | 2026-09-02 | 2/2d | old error |", body)
        failing = body.split("## Currently failing")[1].split("<details>")[0]
        self.assertLess(failing.index("broken.py"), failing.index("new.py"))


class RenderBoundedBodyTest(unittest.TestCase):
    def _state(self, n_quiet, n_archive=0):
        quiet = [
            _entry(f"q{i:03d}", "2026-11-01", f"2026-11-{1 + i % 10:02d}", "E" * 100)
            for i in range(n_quiet)
        ]
        archive = [
            [f"A{900 + i}", f"t/a{i}.py::", "2026-09-01", f"2026-09-{1 + i % 20:02d}", 1, "E" * 60]
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
        archive_last = sorted(a[3] for a in state["archive"])
        body, dropped = ft.render_bounded_body(
            WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=full - 2000,
        )
        self.assertLessEqual(len(body.encode("utf-8")), full - 2000)
        self.assertGreater(dropped, 0)
        self.assertEqual(len(state["active"]), 41)  # the archive alone covered it
        self.assertLessEqual(archive_last[dropped - 1], min(a[3] for a in state["archive"]))

    def test_then_archives_oldest_quiet(self):
        state = self._state(40)
        full = len(ft.render_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY).encode("utf-8"))
        quiet_before = {k: e["last_seen"] for k, e in state["active"].items()}
        body, _ = ft.render_bounded_body(
            WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=full - 6000,
        )
        self.assertLessEqual(len(body.encode("utf-8")), full - 6000)
        self.assertIn("test/failing.py::test_x", state["active"])
        gone = [k for k in quiet_before if k not in state["active"]]
        self.assertTrue(gone)
        kept = [e["last_seen"] for e in state["active"].values() if e["last_seen"] != TODAY]
        self.assertLessEqual(max(quiet_before[k] for k in gone), min(kept))

    def test_raises_when_todays_failures_alone_do_not_fit(self):
        state = _state(_entry("failing", "2026-11-01", TODAY))
        with self.assertRaises(ValueError):
            ft.render_bounded_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=100)


class UpdateOneWorkflowTest(unittest.TestCase):
    ROWS = [{"workflow": WF, "test_file": "test/a.py", "test_function": "test_a", "error": "boom"}]

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
