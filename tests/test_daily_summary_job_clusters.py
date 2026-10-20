import json
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_cross_workflow_summary as summary  # noqa: E402


RUN = "https://github.com/sgl-project/sglang/actions/runs/39150288819"


def _job(job_id: int, name: str = "job", cluster: str = "") -> dict:
    analysis = "### Commit Info\nsha\n"
    if cluster:
        analysis += f"\n### Failure Cluster\n{cluster}\n\n### Facts\n- x\n"
    return {
        "run_url": RUN,
        "job_name": name,
        "job_id": job_id,
        "started_at": "2026-10-19T17:40:00Z",
        "failed_steps": ["pytest"],
        "analysis": analysis,
    }


def _link(job_id: int, label: str = "leg") -> str:
    return f"[{label}]({RUN}/job/{job_id})"


# Shapes taken from the Oct 15-19 reports that the dashboard could not join.
REPORT = f"""# Daily Cross-Workflow Summary — 2026-10-19

## TL;DR
🟡 YELLOW · run labels: N18 = {_link(9)}

## Failure clusters (deduplicated across all workflows)

### R268 · 🆕 · Diffusion unit test needs Python 3.11 — 1 leg

| Workflow | Job (shard) | Test File | Test Function | Error | Log |
|---|---|---|---|---|---|
| pr-test-amd | {_link(1)} | `test_a.py` | `test_x` | AttributeError | [log]({RUN}/job/1) |

### R239–R241 · DSV4 HIP unit tests broken — day 2

| Job (shard) | Test File | Test Function | Error |
|---|---|---|---|
| {_link(2)} | `test_dsv4.py` | `test_y` | abort |

### R264 · R265 · 🆕 · miles MI35x nightly

| ID | Job | Test File | Error | Log |
|---|---|---|---|---|
| R264 | {_link(3)} | `test_m.py` | OOM | |
| R265 | {_link(4)} | `test_n.py` | hang | |

<details><summary><b>Known stable clusters (no new action today)</b> · click to expand</summary>

| ID | Cluster · test file::function · legs | Workflows × legs | First seen / status | In-flight fix |
|---|---|---|---|---|
| R25 | Grok TypeError · `test_grok.py` · N18 {_link(5, "720")} {_link(6, "724")} | nightly ×2 | Sep 4 | ❌ |
| R110 | DeepSeek-R1 server never comes up · {_link(7)} | pr-test ×1 | Oct 2 | ❌ |

</details>

## How this report is generated
- Only completed runs counted.
"""


class SplitJobClusterBlockTests(unittest.TestCase):
    def test_block_is_stripped_and_inverted(self):
        text = (
            REPORT
            + '\n```json job-clusters\n{"R25": [5, "6"], "R209": [5], "bogus": [1], "R1": "x"}\n```\n'
        )
        report, by_job = summary.split_job_cluster_block(text)
        self.assertNotIn("job-clusters", report)
        self.assertEqual(by_job, {5: ["R25", "R209"], 6: ["R25"]})

    def test_missing_or_invalid_block(self):
        self.assertEqual(summary.split_job_cluster_block(REPORT), (REPORT, None))
        report, by_job = summary.split_job_cluster_block(
            REPORT + "\n```json job-clusters\n{not json\n```\n"
        )
        self.assertIsNone(by_job)
        self.assertNotIn("job-clusters", report)


class MarkdownFallbackTests(unittest.TestCase):
    def test_reads_rows_and_sections_the_dashboard_could_not(self):
        by_job = summary.job_clusters_from_markdown(REPORT)
        self.assertEqual(by_job[1], ["R268"])
        # A range heading still yields its endpoints.
        self.assertEqual(by_job[2], ["R239", "R241"])
        # A row's own ID beats the multi-ID heading above it.
        self.assertEqual(by_job[3], ["R264"])
        self.assertEqual(by_job[4], ["R265"])
        # Links inside the known-stable table's cluster column.
        self.assertEqual(by_job[5], ["R25"])
        self.assertEqual(by_job[6], ["R25"])
        # `DeepSeek-R1` is a model, not a cluster.
        self.assertEqual(by_job[7], ["R110"])
        # TL;DR links sit outside any cluster section.
        self.assertNotIn(9, by_job)


class AttachJobClusterMapTests(unittest.TestCase):
    def _map(self, body: str) -> dict:
        m = re.search(r"<!-- ci-monitor-job-clusters:v1 (.*?) -->", body)
        self.assertIsNotNone(m)
        return json.loads(m.group(1))

    def test_agent_block_wins_and_gaps_are_listed(self):
        wf_analyses = {
            "pr-test-amd.yml": [_job(1), _job(2), _job(8, "stage-b (rocm720, 1)", "HF Hub 429")],
            "nightly-test-amd.yml": [_job(5), _job(6)],
        }
        text = REPORT + '\n```json job-clusters\n{"R25": [5, 6], "R240": [2], "R999": [123456]}\n```\n'
        body = summary.attach_job_cluster_map(text, wf_analyses)

        payload = self._map(body)
        # 2 comes from the agent (R240), 1 from the table fallback, and the
        # agent's 123456 is neither analysed nor linked, so it is dropped.
        self.assertEqual(payload["clusters"]["R240"], [2])
        self.assertEqual(payload["clusters"]["R268"], [1])
        self.assertEqual(payload["clusters"]["R25"], [5, 6])
        self.assertNotIn("R999", payload["clusters"])
        self.assertEqual(payload["unclustered"], [8])

        # The unassigned job is listed above the methodology footer, with
        # its per-job cluster name and a link to the job.
        section = body.index("⚠️ Not yet in a cluster")
        self.assertLess(section, body.index("## How this report is generated"))
        self.assertIn(f"| pr-test-amd | HF Hub 429 | [stage-b (rocm720, 1)]({RUN}/job/8) |", body)
        self.assertNotIn("```json job-clusters", body)

    def test_unclustered_section_groups_and_is_capped(self):
        unclustered = [
            ("nightly-test-amd.yml", _job(100 + i, f"nightly-{i}", f"GSM8K below threshold {i % 3}"))
            for i in range(300)
        ]
        section = summary.render_unclustered_section(unclustered, max_chars=4_000)
        self.assertLessEqual(len(section), 4_200)
        self.assertIn("300 analysed failure(s)", section)
        self.assertRegex(section, r"…and \d+ more")
        # Same workflow + per-job cluster -> one row.
        uncapped = summary.render_unclustered_section(unclustered[:6])
        self.assertEqual(uncapped.count("| nightly-test-amd |"), 3)
        self.assertNotIn("more;", uncapped)

    def test_section_is_dropped_when_body_is_near_the_limit(self):
        big = REPORT + "\n" + "x" * (summary.ISSUE_BODY_SAFE_CHARS - len(REPORT))
        body = summary.attach_job_cluster_map(big, {"pr-test-amd.yml": [_job(8)]})
        self.assertNotIn("Not yet in a cluster", body)
        self.assertEqual(self._map(body)["unclustered"], [8])

    def test_full_coverage_adds_no_section(self):
        body = summary.attach_job_cluster_map(REPORT, {"pr-test-amd.yml": [_job(1), _job(3)]})
        self.assertNotIn("Not yet in a cluster", body)
        self.assertEqual(self._map(body)["unclustered"], [])

    def test_render_summary_body_keeps_old_behaviour_without_analyses(self):
        body = summary.render_summary_body("2026-10-19 23:16 UTC", REPORT, True)
        self.assertNotIn("ci-monitor-job-clusters", body)
        body = summary.render_summary_body("2026-10-19 23:16 UTC", REPORT, True, {})
        self.assertIn("ci-monitor-job-clusters", body)


if __name__ == "__main__":
    unittest.main()
