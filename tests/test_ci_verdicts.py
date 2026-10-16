import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ci_verdicts as cv  # noqa: E402
import utils  # noqa: E402


def _record(result="failed", mode="crash", metrics=(), gpu=True, image="same line", launch=True):
    return {"kind": "main_record", "failure_id": "F1", "test_result": result, "mode": mode,
            "metrics": list(metrics), "comparable": {"gpu": gpu, "image": image, "launch_configs": launch}}


def _pack(mode="crash", attempts=1, pr_metrics=(), records=None, files=("python/x.py",)):
    evidence = {
        "D1": {"kind": "diff", "file": "python/x.py"},
        "F1.failure": {"kind": "failure", "failure_id": "F1"},
        "F1.metrics": {"kind": "metrics", "failure_id": "F1", "values": list(pr_metrics)},
        "F1.env": {"kind": "environment", "failure_id": "F1"},
        "F2.failure": {"kind": "failure", "failure_id": "F2"},
    }
    for i, r in enumerate(records or [_record(mode=mode)], 1):
        evidence[f"F1.M{i}"] = r
    return {
        "pr": {"files": [{"file": f} for f in files]},
        "failures": [
            {"id": "F1", "job": {"id": 11, "name": "job-a", "url": "https://github.com/o/r/actions/runs/1/job/11"},
             "test_file": "test/t.py", "mode": mode, "attempts": attempts, "cascade_of": None},
            {"id": "F2", "job": {"id": 12, "name": "job-b", "url": "https://github.com/o/r/actions/runs/1/job/12"},
             "test_file": None, "mode": "cascade", "attempts": 1, "cascade_of": "job-a"},
        ],
        "evidence": evidence,
    }


def _verdict(**kw):
    v = {"failure_id": "F1", "label": "unknown", "basis": "insufficient_evidence",
         "mechanism": {"change": [], "execution": [], "failure": []},
         "evidence": [], "counter_evidence": [], "next_step": "rerun"}
    v.update(kw)
    return v


def _reasons(pack, verdict):
    checked, _ = cv.validate(pack, {"verdicts": [verdict]})
    return checked[0]["validation"]


def _report(emoji, verdicts):
    return "\n".join([
        "## CI Status for PR #1",
        "",
        "**Verdict:** …",
        "",
        "| Job | Test | Failure | On `main` | Related? | Why |",
        "|-----|------|---------|-----------|----------|-----|",
        f"| [job-a](https://github.com/o/r/actions/runs/1/job/11) | `t.py::T.test` | crash | crashed ×1 | {emoji} | … |",
        "",
        "<!-- ci-status-verdicts",
        json.dumps({"verdicts": verdicts}),
        "-->",
    ])


class TestParse(unittest.TestCase):
    def test_reads_the_last_block_and_rejects_bad_json(self):
        report = _report("🟢", [_verdict(label="unrelated")])
        self.assertEqual(cv.parse_verdict_block(report)["verdicts"][0]["label"], "unrelated")
        self.assertIsNone(cv.parse_verdict_block("## CI Status for PR #1\nno block"))
        self.assertIsNone(cv.parse_verdict_block("<!-- ci-status-verdicts {not json} -->"))


class TestValidate(unittest.TestCase):
    def test_coverage_cascades_duplicates_and_malformed(self):
        checked, problems = cv.validate(_pack(), {"verdicts": []})
        self.assertEqual([(v["failure_id"], v["label"], v["validation"]) for v in checked],
                         [("F1", None, ["no verdict"])])
        _, problems = cv.validate(_pack(), {"verdicts": [_verdict(failure_id="F2"), _verdict(), _verdict(),
                                                         {"failure_id": "F1", "label": "maybe"}]})
        self.assertTrue(any("`F2`" in p for p in problems))
        self.assertTrue(any("duplicate" in p for p in problems))
        self.assertTrue(any("malformed" in p for p in problems))
        _, problems = cv.validate(_pack(), None)
        self.assertTrue(any("no readable" in p for p in problems))

    def test_related_needs_the_full_mechanism_chain(self):
        good = _verdict(label="related", basis="mechanism_chain",
                        mechanism={"change": ["D1"], "execution": ["F1.env"], "failure": ["F1.failure"]})
        self.assertEqual(_reasons(_pack(), good), [])
        no_execution = copy.deepcopy(good)
        no_execution["mechanism"]["execution"] = ["F2.failure"]  # another failure's evidence
        self.assertTrue(any("changed path ran" in r for r in _reasons(_pack(), no_execution)))

    def test_unknown_ids_label_basis_mismatch_and_no_next_step(self):
        self.assertTrue(_reasons(_pack(), _verdict(label="possibly_related", basis="suspicion", evidence=["F9.env"])))
        self.assertTrue(_reasons(_pack(), _verdict(label="related", basis="suspicion", evidence=["F1.env"])))
        self.assertTrue(_reasons(_pack(), _verdict(label="possibly_related", basis="suspicion", evidence=[])))
        self.assertTrue(_reasons(_pack(), _verdict(next_step=" ")))
        self.assertEqual(_reasons(_pack(), _verdict(label="unrelated", basis="comparable_recurrence",
                                                    evidence=["F1.M1"], next_step="")), [])

    def test_recurrence_requires_comparable_same_mode_and_no_worse(self):
        unrelated = _verdict(label="unrelated", basis="comparable_recurrence", evidence=["F1.M1"])
        self.assertEqual(_reasons(_pack(mode="crash"), unrelated), [])
        cases = {
            "different mode": _pack(mode="metric_threshold", records=[_record(mode="crash")]),
            "not comparable": _pack(records=[_record(image="different")]),
            "did not fail": _pack(records=[_record(result="passed")]),
            "worse metrics": _pack(mode="metric_threshold", pr_metrics=[{"accuracy": 0.0, "invalid": 1.0}],
                                   records=[_record(mode="metric_threshold",
                                                    metrics=[{"accuracy": 0.69, "invalid": 0.0}])]),
            "more frequent": _pack(attempts=2, records=[_record(), _record(result="passed"),
                                                        _record(result="passed")]),
        }
        for name, pack in cases.items():
            with self.subTest(name):
                self.assertTrue(_reasons(pack, unrelated))

    def test_independence_must_address_build_and_ci_changes(self):
        verdict = _verdict(label="unrelated", basis="independence", evidence=["D1", "F1.failure"])
        self.assertEqual(_reasons(_pack(), verdict), [])
        pack = _pack(files=("python/x.py", "docker/rocm.Dockerfile"))
        self.assertTrue(any("docker/rocm.Dockerfile" in r for r in _reasons(pack, verdict)))
        pack["evidence"]["D2"] = {"kind": "diff", "file": "docker/rocm.Dockerfile"}
        self.assertEqual(_reasons(pack, {**verdict, "evidence": ["D1", "D2", "F1.failure"]}), [])


class TestAnnotate(unittest.TestCase):
    def _annotate(self, report):
        pack = _pack()
        checked, problems = cv.validate(pack, cv.parse_verdict_block(report))
        return cv.annotate_report(report, pack, checked, problems)

    def test_supported_verdict_leaves_the_report_alone(self):
        report = _report("🟢 unrelated", [_verdict(label="unrelated", basis="comparable_recurrence",
                                                   evidence=["F1.M1"])])
        self.assertEqual(self._annotate(report), report)

    def test_unsupported_verdict_marks_the_row_and_explains(self):
        report = _report("🟢 unrelated", [_verdict(label="unrelated", basis="independence", evidence=[])])
        out = self._annotate(report)
        row = next(line for line in out.split("\n") if line.startswith("|") and "/job/11" in line)
        self.assertIn("⚪ unverified unrelated", row)
        lines = out.split("\n")
        self.assertEqual(lines[0], "## CI Status for PR #1")
        self.assertEqual(lines[2], "> [!WARNING]")
        self.assertIn("cites no diff hunk", out)

    def test_table_and_block_must_agree(self):
        report = _report("🟢 unrelated", [_verdict(label="possibly_related", basis="suspicion", evidence=["F1.env"])])
        out = self._annotate(report)
        self.assertIn("the table says 🟢 but the verdict block says 🟡", out)
        self.assertIn("⚪ unverified", out)

    def test_missing_block_warns(self):
        out = self._annotate("## CI Status for PR #1\n\nAll good.")
        self.assertIn("no readable `ci-status-verdicts` block", out)
        self.assertIn("no verdict", out)


class TestAgentHarness(unittest.TestCase):
    def test_redact_secrets(self):
        env = {"GH_PAT": "SECRETVALUE123", "ANTHROPIC_CUSTOM_HEADERS": "Ocp-Apim-Subscription-Key: KEYVALUE456"}
        with mock.patch.dict(os.environ, env):
            out = utils.redact_secrets("a SECRETVALUE123 b KEYVALUE456 c ghp_" + "x" * 30)
        self.assertEqual(out, "a [redacted] b [redacted] c [redacted]")

    def test_agent_env_flags_and_session_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            work, home, archive = tmp / "work", tmp / "home", tmp / "archive"
            work.mkdir()
            project = home / ".claude" / "projects" / str(work.resolve()).replace("/", "-")
            seen = {}

            def fake_run(cmd, **kwargs):
                seen.update(cmd=cmd, env=kwargs["env"])
                project.mkdir(parents=True)
                (project / "s.jsonl").write_text('{"text": "token SECRETVALUE123"}\n')
                return subprocess.CompletedProcess(cmd, 0, stdout="## CI Status for PR #1\nok", stderr="")

            env = {"GH_PAT": "SECRETVALUE123", "AGENT_GH_TOKEN": "READONLY789", "BOT_PAT": "BOTVALUE000",
                   "AGENT_ARCHIVE_DIR": str(archive)}
            with mock.patch.dict(os.environ, env), mock.patch.object(utils.Path, "home", return_value=home), \
                    mock.patch.object(utils.subprocess, "run", side_effect=fake_run):
                out = utils.claude_code_analyze("Task: PR CI Status Check\nPR: #1", work_dir=work)
            self.assertEqual(out, "## CI Status for PR #1\nok")
            self.assertIn("--disable-slash-commands", seen["cmd"])
            self.assertEqual((seen["env"]["GH_PAT"], seen["env"]["GH_TOKEN"]), ("READONLY789", "READONLY789"))
            self.assertNotIn("BOT_PAT", seen["env"])
            [session_dir] = archive.iterdir()
            self.assertTrue(session_dir.name.endswith("PR_CI_Status_Check"))
            self.assertEqual((session_dir / "s.jsonl").read_text(), '{"text": "token [redacted]"}\n')


if __name__ == "__main__":
    unittest.main()
