import json
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


if __name__ == "__main__":
    unittest.main()
