import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_ci_status as ev  # noqa: E402


class TestEvalCiStatus(unittest.TestCase):
    def test_metrics(self):
        m = ev.metrics([("related", "unrelated"), ("related", "possibly_related"), ("unrelated", "related"),
                        ("unrelated", "unknown"), ("unrelated", None)])
        self.assertEqual((m["n"], m["truth_related"], m["wrong_unrelated"], m["flagged"]), (5, 2, 1, 1))
        self.assertEqual((m["truth_unrelated"], m["false_related"], m["unknown"], m["missing"]), (3, 1, 1, 1))

    def test_score_matches_verdicts_by_job_and_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "c1"
            (artifact / "pack").mkdir(parents=True)
            (artifact / "pack" / "evidence.json").write_text(json.dumps({"failures": [
                {"id": "F1", "job": {"name": "job-a"}, "test_file": "t.py"}]}))
            (artifact / "verdicts.json").write_text(json.dumps({"verdicts": [
                {"failure_id": "F1", "label": "possibly_related"}]}))
            cases = [{"id": "c1", "labels": [
                {"job": "job-a", "test_file": "t.py", "truth": "related", "status": "confirmed",
                 "legacy_label": "unrelated"},
                {"job": "job-z", "test_file": None, "truth": "unrelated", "status": "proposed"}]}]
            result = ev.score(cases, Path(tmp))
        self.assertEqual([r["bot"] for r in result["rows"]], ["possibly_related", None])
        self.assertEqual(result["confirmed"]["bot"]["flagged"], 1)
        self.assertEqual(result["confirmed"]["legacy"]["wrong_unrelated"], 1)
        self.assertEqual(result["all"]["bot"]["missing"], 1)


def _session(lines: list[dict]) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _tool_result(text: str, ts: str = "2026-11-01T00:00:10Z") -> dict:
    return {"type": "user", "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


class TestRunMissing(unittest.TestCase):
    def test_a_hung_case_is_stopped_with_its_agent_and_the_rest_still_run(self):
        from unittest import mock
        started, killed = [], []

        class FakeProc:
            def __init__(self, cmd, **kwargs):
                self.pid, self.pr = 4242, cmd[3]
                started.append((self.pr, kwargs["start_new_session"]))

            def wait(self, timeout=None):
                if self.pr == "1" and timeout is not None:
                    raise subprocess.TimeoutExpired("x", timeout)
                return 0

            def kill(self):
                killed.append("proc")

        cases = [{"id": f"c{n}", "pr": n, "head_sha": "h", "as_of": "t"} for n in (1, 2)]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ev.subprocess, "Popen", FakeProc), \
                mock.patch.object(ev.os, "killpg", lambda pid, sig: killed.append(pid), create=True):
            stale = Path(tmp) / "c1" / "sessions" / "old"
            stale.mkdir(parents=True)
            ev.run_missing(cases, Path(tmp), marker="selection.json")
            log = (Path(tmp) / "c1.log").read_text()
            self.assertFalse(stale.exists())  # an earlier attempt's sessions are not counted again
        self.assertEqual(started, [("1", True), ("2", True)])
        self.assertEqual(killed, [4242])  # the whole process group, agent included
        self.assertIn("timed out", log)


class TestSelectionScore(unittest.TestCase):
    AS_OF = "2026-11-01T00:00:00Z"

    def artifact(self, root: Path, case_id: str, nv_extra: bool, session: list[dict]):
        artifact = root / case_id
        (artifact / "sessions" / "s1").mkdir(parents=True)
        (artifact / "selection.json").write_text(json.dumps({"platforms": {
            "nv": {"extra_needed": nv_extra, "reason": "r"}, "amd": {"extra_needed": None, "reason": ""}}}))
        (artifact / "sessions" / "s1" / "a.jsonl").write_text(_session(session))

    def case(self, case_id: str, status: str = "confirmed") -> dict:
        return {"id": case_id, "as_of": self.AS_OF, "status": status, "head_sha": "HEAD", "platforms": {
            "nv": {"extra_needed": True}, "amd": {"extra_needed": False}}}

    def test_leaks_find_later_dates_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.artifact(Path(tmp), "c1", True, [
                _tool_result('[{"created_at": "2026-10-30T00:00:00Z", "updated_at": "2026-11-09T00:00:00Z"}]'),
                _tool_result('{"createdAt":"2026-11-03T08:00:00Z","body":"/rerun-test x.py"}'),
                _tool_result("commit abc\nDate:   Mon Nov 2 10:00:00 2026 -0800\n"),
            ])
            # A large tool output kept outside the log.
            big = Path(tmp) / "c1" / "sessions" / "s1" / "a" / "tool-results"
            big.mkdir(parents=True)
            (big / "b1.txt").write_text('{"merged_at": "2026-11-04T00:00:00Z"}')
            (big / "b2.txt").write_text('{"createdAt": "2026-11-05T01:02:03.123Z"}\nabc 2026-11-06 10:00:00 -0800 fix\n')
            found = ev.leaks(Path(tmp) / "c1", self.AS_OF)
        self.assertEqual(sorted(found), ["a.jsonl: createdAt 2026-11-03T08:00:00Z",
                                         "a.jsonl: git date 2026-11-02T18:00:00Z",
                                         "b1.txt: merged_at 2026-11-04T00:00:00Z",
                                         "b2.txt: createdAt 2026-11-05T01:02:03Z",
                                         "b2.txt: git date 2026-11-06T18:00:00Z"])

    def test_a_run_without_a_session_log_is_not_scored_as_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "c1"
            artifact.mkdir()
            (artifact / "selection.json").write_text(json.dumps({"platforms": {
                "nv": {"extra_needed": True}, "amd": {"extra_needed": False}}}))
            self.assertIsNone(ev.leaks(artifact, self.AS_OF))
            result = ev.score_selection([self.case("c1")], Path(tmp))
        self.assertEqual((result["all"]["scored"], result["all"]["unchecked"]), (0, 2))

    def test_score_selection(self):
        clean = [{"type": "assistant", "timestamp": "2026-11-01T00:00:00Z",
                  "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5}}},
                 _tool_result('{"created_at": "2026-10-01T00:00:00Z"}', ts="2026-11-01T00:10:00Z")]
        leaked = [_tool_result('{"mergedAt": "2026-11-05T00:00:00Z"}')]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.artifact(root, "c1", True, clean)
            self.artifact(root, "c2", True, leaked)
            self.artifact(root, "c4", False, clean)
            cases = [self.case("c1"), self.case("c2"), self.case("c3", "proposed"), self.case("c4")]
            result = ev.score_selection(cases, root)
            text = ev.format_selection_score(result)
        m = result["confirmed"]
        # c1 nv right, c1 amd wrong (None), c4 nv wrong, c4 amd wrong; c2 leaked; c3 missing (proposed).
        self.assertEqual((m["scored"], m["of"], m["leaked"]), (4, 6, 2))
        self.assertEqual(m["extra_correct"], 1)
        self.assertEqual(result["all"]["missing"], 2)
        self.assertEqual(result["cost"], {"runs": 3, "p90_seconds": 600.0, "max_seconds": 600.0, "tokens": 30})
        self.assertIn("extra_needed correct: 1/4 (25%)", text)


if __name__ == "__main__":
    unittest.main()
