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


def _entry(name, first, last, error="AssertionError"):
    return {
        "test_file": f"test/{name}.py", "test_function": "test_x",
        "cluster": "", "error": error, "status": "", "job_url": "",
        "job_id": None, "detail_url": "", "run_started_at": "",
        "first_seen": first, "last_seen": last,
    }


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


class PruneStateTest(unittest.TestCase):
    def test_drops_only_entries_quiet_past_retention(self):
        state = {
            "failing": _entry("failing", "2026-08-01", TODAY),
            "edge": _entry("edge", "2026-10-01", "2026-10-13"),  # 30 days quiet
            "old": _entry("old", "2026-09-01", "2026-10-12"),    # 31 days quiet
            "bad": _entry("bad", "?", "not-a-date"),
        }
        self.assertEqual(ft.prune_state(state, TODAY, 30), 1)
        self.assertEqual(sorted(state), ["bad", "edge", "failing"])
        # A failure still red keeps its original first_seen.
        self.assertEqual(state["failing"]["first_seen"], "2026-08-01")


class RenderBoundedBodyTest(unittest.TestCase):
    def _state(self, n_quiet):
        state = {"failing": _entry("failing", "2026-11-01", TODAY)}
        for i in range(n_quiet):
            state[f"q{i}"] = _entry(f"q{i:03d}", "2026-11-01", f"2026-11-{1 + i % 10:02d}", "E" * 100)
        return state

    def test_fits_without_dropping(self):
        state = self._state(3)
        body, dropped = ft.render_bounded_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(state), 4)
        self.assertEqual(ft.load_state(body).keys(), state.keys())

    def test_drops_oldest_quiet_entries_to_fit(self):
        state = self._state(40)
        original = dict(state)
        full = ft.render_body(WF, "PR Test (AMD)", original, SNAPSHOT, DAILY)
        budget = len(full.encode("utf-8")) - 3000
        body, dropped = ft.render_bounded_body(
            WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=budget,
        )
        self.assertGreater(dropped, 0)
        self.assertEqual(len(original) - len(state), dropped)
        self.assertLessEqual(len(body.encode("utf-8")), budget)
        self.assertIn("failing", state)
        # Everything left is at least as recent as everything dropped.
        gone = [original[k]["last_seen"] for k in original if k not in state]
        kept = [e["last_seen"] for k, e in state.items() if k != "failing"]
        self.assertLessEqual(max(gone), min(kept))
        self.assertEqual(ft.load_state(body).keys(), state.keys())

    def test_raises_when_todays_failures_alone_do_not_fit(self):
        state = {"failing": _entry("failing", "2026-11-01", TODAY)}
        with self.assertRaises(ValueError):
            ft.render_bounded_body(WF, "PR Test (AMD)", state, SNAPSHOT, DAILY, max_bytes=100)


class UpdateOneWorkflowTest(unittest.TestCase):
    ROWS = [{"workflow": WF, "test_file": "test/a.py", "test_function": "test_a", "error": "boom"}]

    def _run(self, saved_body):
        old = {"body": "", "number": 45847}
        with patch.object(ft, "find_or_create_tracker_issue", return_value=45847), \
             patch.object(ft, "get_issue", side_effect=[old, {"body": saved_body}]), \
             patch.object(ft, "update_issue_body") as upd:
            result = ft.update_one_workflow("t", WF, CFG, "bingxche/sglang-ci-bot", 287, self.ROWS, SNAPSHOT)
        return result, upd

    def test_body_that_was_not_saved_raises(self):
        # GitHub answers 200 to an oversized body and keeps the old one.
        with self.assertRaises(RuntimeError):
            self._run(saved_body="stale")

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
        self.assertIn("test/a.py::test_a", ft.load_state(captured["sent"]))


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

    def test_duplicates_pick_most_recently_updated_open_issue(self):
        listing = [
            self._issue(37451, "2026-10-22T04:02:48Z"),
            self._issue(45847, "2026-10-25T12:07:09Z"),
            self._issue(45835, "2026-11-12T00:00:00Z", state="closed"),
        ]
        with patch.object(ft, "gh_get", return_value=_resp(listing)):
            self.assertEqual(ft.find_tracker_issue("t", self.TITLE), 45847)


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
