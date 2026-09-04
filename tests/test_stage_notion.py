import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import stage_notion


def candidate(**overrides):
    value = {
        "canonical_signature": "RuntimeError: RCCL allreduce hang",
        "time": "Since 2026-09-01 UTC",
        "test_file": "test/registered/test_allreduce.py",
        "job_name": "nightly-test-amd / stage-b-test-2",
        "job_url": "https://github.com/sgl-project/sglang/actions/runs/100/job/200",
        "owner": "",
        "fix_pr": "",
        "repro": "Same signature in completed runs 100 and 101; no later comparable pass. Daily Report: https://github.com/bingxche/sglang-ci-bot/issues/123",
        "error_msg": "RuntimeError: RCCL allreduce hang during distributed test",
        "hardware_context": "MI300X / ROCm 7.2",
        "failure_phase": "distributed allreduce",
        "stability_basis": "repeated_runs",
        "evidence_run_ids": ["100", "101"],
        "evidence_runs": [
            {
                "run_id": "100",
                "status": "completed",
                "comparable": True,
                "run_url": "https://github.com/sgl-project/sglang/actions/runs/100",
            },
            {
                "run_id": "101",
                "status": "completed",
                "comparable": True,
                "run_url": "https://github.com/sgl-project/sglang/actions/runs/101",
            },
        ],
        "comparable_pass_after_first": False,
        "status": "Needs review",
    }
    value.update(overrides)
    return value


class CandidateTests(unittest.TestCase):
    def test_extract_marked_json(self):
        payload = {
            "stable": [candidate()],
            "known": [],
            "existing_staging": [],
            "flakes": [],
            "watchlist": [],
        }
        text = (
            "ignored preamble\n"
            f"{stage_notion.CANDIDATES_START}\n```json\n"
            f"{json.dumps(payload)}\n```\n{stage_notion.CANDIDATES_END}"
        )
        parsed = stage_notion.extract_candidate_report(text)
        self.assertEqual(len(parsed["stable"]), 1)

    def test_v1_accepts_two_independent_runs(self):
        accepted, rejected = stage_notion.prepare_candidates({"stable": [candidate()]})
        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])
        self.assertEqual(len(accepted[0]["fingerprint"]), 20)

    def test_v1_rejects_single_run_and_later_pass(self):
        accepted, rejected = stage_notion.prepare_candidates(
            {
                "stable": [
                    candidate(evidence_run_ids=["100"]),
                    candidate(
                        canonical_signature="AssertionError: accuracy threshold",
                        evidence_run_ids=["102", "103"],
                        comparable_pass_after_first=True,
                    ),
                ]
            }
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 2)

    def test_v1_rejects_unverified_or_mismatched_evidence(self):
        item = candidate(
            evidence_runs=[
                {
                    "run_id": "100",
                    "status": "in_progress",
                    "comparable": True,
                    "run_url": "https://github.com/sgl-project/sglang/actions/runs/100",
                },
                {
                    "run_id": "999",
                    "status": "completed",
                    "comparable": False,
                    "run_url": "not-a-run-url",
                },
            ]
        )
        accepted, rejected = stage_notion.prepare_candidates({"stable": [item]})
        self.assertEqual(accepted, [])
        reason = rejected[0]["reason"]
        self.assertIn("not completed", reason)
        self.assertIn("not confirmed comparable", reason)
        self.assertIn("exactly match", reason)

    def test_fingerprint_ignores_evidence_run_ids(self):
        first = candidate(evidence_run_ids=["100", "101"])
        second = candidate(evidence_run_ids=["900", "901"])
        self.assertEqual(
            stage_notion.candidate_fingerprint(first),
            stage_notion.candidate_fingerprint(second),
        )

    def test_duplicate_by_fingerprint(self):
        item = candidate()
        item["fingerprint"] = stage_notion.candidate_fingerprint(item)
        records = [
            {
                "Test File": "unrelated.py",
                "Error msg": "different",
                "Repro": f"[{stage_notion.FINGERPRINT_PREFIX}{item['fingerprint']}]",
            }
        ]
        self.assertTrue(stage_notion.is_duplicate(item, records))

    def test_duplicate_by_test_family_and_error(self):
        item = candidate()
        item["fingerprint"] = stage_notion.candidate_fingerprint(item)
        records = [
            {
                "Test File": "test/registered/test_allreduce.py",
                "Error msg": "RuntimeError: RCCL allreduce hang during distributed test",
                "Repro": "seen before",
            }
        ]
        self.assertTrue(stage_notion.is_duplicate(item, records))


class NotionPayloadTests(unittest.TestCase):
    def setUp(self):
        self.item = candidate()
        self.item["fingerprint"] = stage_notion.candidate_fingerprint(self.item)
        self.schema = {
            "Time": {"type": "title"},
            "Status": {"type": "status"},
            "Test File": {"type": "rich_text"},
            "Job": {"type": "rich_text"},
            "Owner": {"type": "rich_text"},
            "Fix PR": {"type": "rich_text"},
            "Repro": {"type": "rich_text"},
            "Error msg": {"type": "rich_text"},
        }

    def test_build_payload_uses_exact_schema_and_status(self):
        props = stage_notion.build_notion_properties(self.item, self.schema)
        self.assertEqual(set(props), {"Time", "Test File", "Job", "Repro", "Error msg", "Status"})
        self.assertEqual(props["Status"], {"status": {"name": "Needs review"}})
        self.assertEqual(
            props["Job"]["rich_text"][0]["text"]["link"]["url"],
            self.item["job_url"],
        )
        repro = props["Repro"]["rich_text"][0]["text"]["content"]
        self.assertIn(stage_notion.FINGERPRINT_PREFIX, repro)

    def test_rejects_missing_staging_column(self):
        data_source = {"properties": dict(self.schema)}
        del data_source["properties"]["Status"]
        with self.assertRaisesRegex(ValueError, "missing properties"):
            stage_notion.validate_staging_schema(data_source)

    def test_rejects_extra_staging_column(self):
        data_source = {"properties": dict(self.schema)}
        data_source["properties"]["Notes"] = {"type": "rich_text"}
        with self.assertRaisesRegex(ValueError, "unexpected properties"):
            stage_notion.validate_staging_schema(data_source)

    def test_rejects_wrong_staging_column_type(self):
        data_source = {"properties": dict(self.schema)}
        data_source["properties"]["Fix PR"] = {"type": "url"}
        with self.assertRaisesRegex(ValueError, "incorrect property types"):
            stage_notion.validate_staging_schema(data_source)

    def test_dry_run_needs_no_notion_credentials(self):
        report = {"stable": [self.item]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "candidates.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            result = stage_notion.sync(path, dry_run=True)
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(len(result["stable_candidates"]), 1)


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.ok = True
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        if len(self.calls) == 1:
            return _Response({"results": [{"id": "one"}], "has_more": True, "next_cursor": "next"})
        return _Response({"results": [{"id": "two"}], "has_more": False, "next_cursor": None})


class NotionClientTests(unittest.TestCase):
    def test_query_paginates(self):
        session = _Session()
        client = stage_notion.NotionClient("secret", session=session)
        pages = client.query_data_source("data-source")
        self.assertEqual([page["id"] for page in pages], ["one", "two"])
        self.assertEqual(session.calls[1][2]["start_cursor"], "next")


class _InMemoryNotionClient:
    official_pages = []
    staging_pages = []
    writes = []
    schema = {
        "Time": {"type": "title"},
        "Status": {"type": "status"},
        "Test File": {"type": "rich_text"},
        "Job": {"type": "rich_text"},
        "Owner": {"type": "rich_text"},
        "Fix PR": {"type": "rich_text"},
        "Repro": {"type": "rich_text"},
        "Error msg": {"type": "rich_text"},
    }

    def __init__(self, token):
        self.token = token

    def retrieve_data_source(self, data_source_id):
        title = (
            stage_notion.OFFICIAL_DATA_SOURCE_TITLE
            if data_source_id == "official"
            else stage_notion.STAGING_DATA_SOURCE_TITLE
        )
        return {"title": [{"plain_text": title}], "properties": dict(self.schema)}

    def query_data_source(self, data_source_id):
        pages = self.official_pages if data_source_id == "official" else self.staging_pages
        return list(pages)

    @staticmethod
    def _materialize(properties):
        result = {}
        for name, prop in properties.items():
            prop_type, value = next(iter(prop.items()))
            if prop_type in ("title", "rich_text"):
                value = [
                    {**item, "plain_text": item.get("text", {}).get("content", "")}
                    for item in value
                ]
            result[name] = {"type": prop_type, prop_type: value}
        return result

    def create_page(self, data_source_id, properties):
        if self.token != "notion-token" or data_source_id != "staging":
            raise AssertionError("write escaped the isolated staging client")
        page = {"id": "created-1", "url": "https://notion.so/created-1", "properties": self._materialize(properties)}
        self.staging_pages.append(page)
        self.writes.append((data_source_id, properties))
        return page

    def retrieve_page(self, page_id):
        return next(page for page in self.staging_pages if page["id"] == page_id)


class NotionSyncTests(unittest.TestCase):
    def setUp(self):
        _InMemoryNotionClient.official_pages = [
            {"id": "official-1", "properties": {"Error msg": {"type": "rich_text", "rich_text": []}}}
        ]
        _InMemoryNotionClient.staging_pages = []
        _InMemoryNotionClient.writes = []

    def test_write_sync_targets_only_staging_and_verifies_result(self):
        report = {"stable": [candidate()]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "candidates.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with patch.object(stage_notion, "NotionClient", _InMemoryNotionClient):
                result = stage_notion.sync(
                    path,
                    dry_run=False,
                    notion_token="notion-token",
                    known_data_source_id="official",
                    staging_data_source_id="staging",
                )
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(_InMemoryNotionClient.writes[0][0], "staging")
        self.assertEqual([page["id"] for page in _InMemoryNotionClient.official_pages], ["official-1"])

    def test_write_sync_rejects_same_official_and_staging_id(self):
        report = {"stable": [candidate()]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "candidates.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be different"):
                stage_notion.sync(
                    path,
                    dry_run=False,
                    notion_token="notion-token",
                    known_data_source_id="same",
                    staging_data_source_id="same",
                )


if __name__ == "__main__":
    unittest.main()
