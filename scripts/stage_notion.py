#!/usr/bin/env python3
"""Find stable AMD CI failures with Claude Code and stage them in Notion.

The command is deliberately split into two phases:

``analyze``
    Reads GitHub Daily Reports and invokes the existing Claude Code agent.  No
    Notion credentials are present in this phase.

``sync``
    Validates the machine-readable candidate file, reads the official and
    staging data sources, appends only new rows to the staging data source,
    and verifies every write.  This phase never invokes an agent.

Keeping the phases separate prevents Notion credentials from being inherited
by the Claude Code subprocess and makes ``--dry-run`` safe for first tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("notion-staging")

NOTION_VERSION = "2026-03-11"
CANDIDATES_START = "<!-- notion-staging-candidates:start -->"
CANDIDATES_END = "<!-- notion-staging-candidates:end -->"
EXPECTED_PROPERTIES = (
    "Time",
    "Status",
    "Test File",
    "Job",
    "Owner",
    "Fix PR",
    "Repro",
    "Error msg",
)
EXPECTED_PROPERTY_TYPES = {
    "Time": "title",
    "Status": "status",
    "Test File": "rich_text",
    "Job": "rich_text",
    "Owner": "rich_text",
    "Fix PR": "rich_text",
    "Repro": "rich_text",
    "Error msg": "rich_text",
}
FINGERPRINT_PREFIX = "staging-fingerprint:"
OFFICIAL_DATA_SOURCE_TITLE = "SGLang AMD CI Known Errors"
STAGING_DATA_SOURCE_TITLE = "SGLang AMD CI Staging Errors"


def _strip_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def extract_candidate_report(agent_text: str) -> dict[str, Any]:
    """Parse the agent's marked JSON object and fail closed on malformed output."""
    if not agent_text:
        raise ValueError("Claude Code returned no staging-candidate output")
    start = agent_text.rfind(CANDIDATES_START)
    end = agent_text.find(CANDIDATES_END, start + len(CANDIDATES_START))
    payloads: list[str] = []
    if start != -1 and end != -1:
        payloads.append(_strip_fence(agent_text[start + len(CANDIDATES_START) : end]))
    else:
        # Claude Code occasionally drops HTML comments while preserving the
        # requested fenced JSON. Accept only a uniquely identifiable report;
        # deterministic candidate validation still runs immediately after.
        payloads.extend(
            match.group(1).strip()
            for match in re.finditer(r"```json\s*(\{.*?\})\s*```", agent_text, re.DOTALL | re.IGNORECASE)
        )

    reports: list[dict[str, Any]] = []
    for payload in payloads:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "stable" in parsed:
            reports.append(parsed)
    if len(reports) != 1:
        raise ValueError(
            "expected exactly one marked or fenced staging-candidate JSON object; "
            f"found {len(reports)}"
        )
    report = reports[0]
    for key in ("stable", "known", "existing_staging", "flakes", "watchlist"):
        value = report.setdefault(key, [])
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a JSON array")
    return report


def normalize_text(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"0x[0-9a-f]+", " <addr> ", value)
    value = re.sub(r"\b(?:run|job|pid|port|request)[-_ ]?\d+\b", " ", value)
    value = re.sub(r"\b\d{6,}\b", " <id> ", value)
    value = re.sub(r"[^a-z0-9_./:+-]+", " ", value)
    return " ".join(value.split())


def candidate_fingerprint(candidate: dict[str, Any]) -> str:
    pieces = (
        candidate.get("canonical_signature", ""),
        candidate.get("test_file", ""),
        candidate.get("hardware_context", ""),
        candidate.get("failure_phase", ""),
    )
    canonical = "\n".join(normalize_text(str(piece)) for piece in pieces)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


def validate_candidate(candidate: dict[str, Any]) -> list[str]:
    """Return rejection reasons for a candidate.

    The first rollout intentionally implements only the strongest stability
    rule: the same canonical signature in at least two independent completed
    runs, with no later comparable pass.  Other policy-valid cases remain on
    the watchlist until the initial integration is proven.
    """
    errors: list[str] = []
    required = (
        "canonical_signature",
        "time",
        "test_file",
        "job_name",
        "job_url",
        "repro",
        "error_msg",
        "hardware_context",
        "failure_phase",
    )
    for field in required:
        if not str(candidate.get(field, "")).strip():
            errors.append(f"missing {field}")

    job_url = str(candidate.get("job_url", "")).strip()
    if job_url and not re.match(
        r"^https://github\.com/[^/]+/[^/]+/actions/runs/\d+/job/\d+(?:[/?#].*)?$",
        job_url,
    ):
        errors.append("job_url must be a direct GitHub Actions job URL")
    repro = str(candidate.get("repro", ""))
    if repro and not re.search(
        r"https://github\.com/[^/]+/[^/]+/issues/\d+(?:[/?#]\S*)?",
        repro,
    ):
        errors.append("repro must include a Daily Report issue URL")
    if candidate.get("time"):
        try:
            _date_value(str(candidate["time"]))
        except ValueError as exc:
            errors.append(str(exc))

    if candidate.get("stability_basis") != "repeated_runs":
        errors.append("v1 accepts only stability_basis=repeated_runs")

    run_ids = {
        str(run_id).strip()
        for run_id in candidate.get("evidence_run_ids", [])
        if str(run_id).strip()
    }
    if len(run_ids) < 2:
        errors.append("fewer than two independent completed evidence runs")

    evidence_runs = candidate.get("evidence_runs")
    verified_run_ids: set[str] = set()
    if not isinstance(evidence_runs, list) or len(evidence_runs) < 2:
        errors.append("evidence_runs must contain at least two run records")
    else:
        for evidence in evidence_runs:
            if not isinstance(evidence, dict):
                errors.append("every evidence run must be an object")
                continue
            run_id = str(evidence.get("run_id", "")).strip()
            if not run_id:
                errors.append("evidence run is missing run_id")
            else:
                verified_run_ids.add(run_id)
            if evidence.get("status") != "completed":
                errors.append(f"evidence run {run_id or '<unknown>'} is not completed")
            if evidence.get("comparable") is not True:
                errors.append(f"evidence run {run_id or '<unknown>'} is not confirmed comparable")
            run_url = str(evidence.get("run_url", "")).strip()
            if not re.match(
                r"^https://github\.com/[^/]+/[^/]+/actions/runs/\d+(?:[/?#].*)?$",
                run_url,
            ):
                errors.append(f"evidence run {run_id or '<unknown>'} has an invalid run URL")
        if len(verified_run_ids) < 2:
            errors.append("evidence_runs do not identify two distinct runs")
        if run_ids != verified_run_ids:
            errors.append("evidence_run_ids must exactly match evidence_runs")

    if candidate.get("comparable_pass_after_first") is not False:
        errors.append("a later comparable pass exists or was not disproved")

    if candidate.get("status", "Needs review") != "Needs review":
        errors.append("Status must be Needs review")

    return errors


def prepare_candidates(report: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in report.get("stable", []):
        if not isinstance(raw, dict):
            rejected.append({"reason": "candidate is not an object", "candidate": raw})
            continue
        candidate = dict(raw)
        candidate["status"] = "Needs review"
        reasons = validate_candidate(candidate)
        if reasons:
            rejected.append({"reason": "; ".join(reasons), "candidate": candidate})
            continue
        fingerprint = candidate_fingerprint(candidate)
        candidate["fingerprint"] = fingerprint
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        accepted.append(candidate)
    return accepted, rejected


def collect_daily_report_context(
    token: str,
    bot_repo: str,
    lookback_days: int,
    end_date: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    # Lazy imports keep deterministic validation/dry-run usable without the
    # agent runtime and its Anthropic SDK dependency.
    from daily_cross_workflow_summary import (
        build_workflows_block,
        collect_workflow_analyses,
    )
    from monitor_ci import find_daily_issue

    end = (
        datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if end_date
        else datetime.now(timezone.utc)
    )
    reports: list[dict[str, Any]] = []
    blocks: list[str] = []
    for offset in reversed(range(max(lookback_days, 1))):
        date_str = (end - timedelta(days=offset)).strftime("%Y-%m-%d")
        issue_num = find_daily_issue(token, bot_repo, date_str)
        if not issue_num:
            continue
        url = f"https://github.com/{bot_repo}/issues/{issue_num}"
        analyses = collect_workflow_analyses(token, bot_repo, issue_num)
        reports.append({"date": date_str, "issue_number": issue_num, "url": url})
        blocks.extend(
            (
                f"# Daily Report {date_str}",
                f"Issue: {url}",
                build_workflows_block(analyses),
                "",
            )
        )
    return "\n".join(blocks), reports


def analyze(
    token: str,
    bot_repo: str,
    lookback_days: int,
    output: Path,
    end_date: str | None = None,
) -> dict[str, Any]:
    context, reports = collect_daily_report_context(
        token, bot_repo, lookback_days, end_date=end_date,
    )
    if not reports:
        report: dict[str, Any] = {
            "stable": [],
            "known": [],
            "existing_staging": [],
            "flakes": [],
            "watchlist": [],
        }
    else:
        from utils import claude_code_analyze, ensure_sglang_repo

        repo_path = ensure_sglang_repo()
        allowed_report_urls = ", ".join(report["url"] for report in reports)
        prompt = (
            "Task: SGLang AMD CI Staging Candidates\n"
            f"Bot repository: {bot_repo}\n"
            f"Daily Reports: .ci-context/notion-staging-history.md\n"
            f"Allowed concrete Daily Report issue URLs: {allowed_report_urls}\n"
            f"Lookback days: {lookback_days}\n"
            "Source: current directory\n"
            "GitHub API token: $GH_PAT"
        )
        agent_text = claude_code_analyze(
            prompt=prompt,
            work_dir=repo_path,
            context_files={"notion-staging-history.md": context},
            max_turns=int(os.environ.get("STAGING_AGENT_MAX_TURNS", "120")),
            timeout_secs=int(os.environ.get("STAGING_AGENT_TIMEOUT_SECS", "1200")),
            output_must_contain=CANDIDATES_START,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_name(f"{output.name}.raw.txt").write_text(agent_text, encoding="utf-8")
        report = extract_candidate_report(agent_text)

    accepted, rejected = prepare_candidates(report)
    report["stable"] = accepted
    report.setdefault("watchlist", []).extend(rejected)
    report["daily_reports"] = reports
    report["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(
        "Candidate analysis complete: %d stable, %d flakes, %d watchlist (%s)",
        len(report["stable"]), len(report["flakes"]), len(report["watchlist"]), output,
    )
    return report


class NotionClient:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.request(
            method,
            f"https://api.notion.com/v1{path}",
            headers=self.headers,
            json=payload,
            timeout=60,
        )
        if not response.ok:
            detail = response.text[:500]
            raise RuntimeError(f"Notion {method} {path} failed: HTTP {response.status_code}: {detail}")
        return response.json()

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self.request("GET", f"/data_sources/{data_source_id}")

    def query_data_source(self, data_source_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            page = self.request("POST", f"/data_sources/{data_source_id}/query", payload)
            results.extend(page.get("results", []))
            if not page.get("has_more"):
                return results
            cursor = page.get("next_cursor")
            if not cursor:
                raise RuntimeError("Notion pagination reported has_more without next_cursor")

    def create_page(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": data_source_id},
                "properties": properties,
            },
        )

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self.request("GET", f"/pages/{page_id}")


def _rich_text(content: str, href: str | None = None) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    content = content or ""
    for start in range(0, len(content), 1900):
        text: dict[str, Any] = {"content": content[start : start + 1900]}
        if href:
            text["link"] = {"url": href}
        chunks.append({"type": "text", "text": text})
    return chunks


def _date_value(value: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?Z)?", value or "")
    if not match:
        raise ValueError(f"cannot convert Time value to a Notion date: {value!r}")
    return match.group(0)


def property_input(prop_type: str, value: str, href: str | None = None) -> dict[str, Any]:
    if prop_type in ("title", "rich_text"):
        return {prop_type: _rich_text(value, href=href)}
    if prop_type == "url":
        return {"url": href or value or None}
    if prop_type == "select":
        return {"select": {"name": value}}
    if prop_type == "status":
        return {"status": {"name": value}}
    if prop_type == "date":
        return {"date": {"start": _date_value(value)}}
    raise ValueError(f"unsupported Notion property type: {prop_type}")


def validate_staging_schema(data_source: dict[str, Any]) -> dict[str, Any]:
    schema = data_source.get("properties") or {}
    missing = [name for name in EXPECTED_PROPERTIES if name not in schema]
    if missing:
        raise ValueError(f"staging data source is missing properties: {', '.join(missing)}")
    unexpected = [name for name in schema if name not in EXPECTED_PROPERTIES]
    if unexpected:
        raise ValueError(f"staging data source has unexpected properties: {', '.join(unexpected)}")
    wrong_types = [
        f"{name} ({schema[name].get('type')}, expected {EXPECTED_PROPERTY_TYPES[name]})"
        for name in EXPECTED_PROPERTIES
        if schema[name].get("type") != EXPECTED_PROPERTY_TYPES[name]
    ]
    if wrong_types:
        raise ValueError(f"staging data source has incorrect property types: {', '.join(wrong_types)}")
    return schema


def data_source_title(data_source: dict[str, Any]) -> str:
    return "".join(item.get("plain_text", "") for item in data_source.get("title", []))


def candidate_values(candidate: dict[str, Any]) -> dict[str, str]:
    fingerprint = candidate["fingerprint"]
    repro = str(candidate["repro"]).strip()
    if FINGERPRINT_PREFIX not in repro:
        repro = f"{repro} [{FINGERPRINT_PREFIX}{fingerprint}]"
    return {
        "Time": str(candidate["time"]).strip(),
        "Status": "Needs review",
        "Test File": str(candidate["test_file"]).strip(),
        "Job": f"{str(candidate['job_name']).strip()} — {str(candidate['job_url']).strip()}",
        "Owner": str(candidate.get("owner", "")).strip(),
        "Fix PR": str(candidate.get("fix_pr", "")).strip(),
        "Repro": repro,
        "Error msg": str(candidate["error_msg"]).strip(),
    }


def build_notion_properties(candidate: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    values = candidate_values(candidate)
    properties: dict[str, Any] = {}
    for name in EXPECTED_PROPERTIES:
        value = values[name]
        prop_type = schema[name]["type"]
        if not value:
            continue
        if prop_type == "people":
            raise ValueError(f"{name} is a people property but the candidate supplies a name, not a user id")
        href = None
        if name == "Job":
            href = str(candidate.get("job_url", "")).strip() or None
        elif name == "Fix PR" and value.startswith("http"):
            href = value
        properties[name] = property_input(prop_type, value, href=href)
    return properties


def property_plain_text(prop: dict[str, Any]) -> str:
    prop_type = prop.get("type")
    value = prop.get(prop_type) if prop_type else None
    if prop_type in ("title", "rich_text"):
        return "".join(item.get("plain_text", "") for item in value or [])
    if prop_type in ("select", "status"):
        return (value or {}).get("name", "")
    if prop_type == "url":
        return value or ""
    if prop_type == "date":
        return (value or {}).get("start", "")
    if prop_type == "people":
        return ", ".join(person.get("name", person.get("id", "")) for person in value or [])
    if value is None:
        return ""
    return str(value)


def row_record(page: dict[str, Any]) -> dict[str, str]:
    return {
        name: property_plain_text(prop)
        for name, prop in (page.get("properties") or {}).items()
    }


def is_duplicate(candidate: dict[str, Any], records: list[dict[str, str]]) -> bool:
    fingerprint = candidate["fingerprint"]
    signature = normalize_text(str(candidate.get("canonical_signature", "")))
    test_file = normalize_text(str(candidate.get("test_file", "")))
    error = normalize_text(str(candidate.get("error_msg", "")))
    for record in records:
        combined = normalize_text(" ".join(record.values()))
        if f"{FINGERPRINT_PREFIX}{fingerprint}" in combined:
            return True
        existing_test = normalize_text(record.get("Test File", ""))
        existing_error = normalize_text(record.get("Error msg", ""))
        existing_repro = normalize_text(record.get("Repro", ""))
        same_test_family = bool(
            test_file
            and existing_test
            and (test_file in existing_test or existing_test in test_file)
        )
        if signature and (signature in existing_error or signature in existing_repro):
            return True
        if same_test_family and error and existing_error:
            if error in existing_error or existing_error in error:
                return True
            if SequenceMatcher(None, error, existing_error).ratio() >= 0.82:
                return True
    return False


def load_candidates(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("candidate file must contain a JSON object")
    accepted, rejected = prepare_candidates(report)
    if rejected:
        reasons = "; ".join(item["reason"] for item in rejected[:5])
        raise ValueError(f"candidate file failed deterministic validation: {reasons}")
    return report, accepted


def sync(
    candidate_path: Path,
    dry_run: bool,
    notion_token: str | None = None,
    known_data_source_id: str | None = None,
    staging_data_source_id: str | None = None,
) -> dict[str, Any]:
    report, candidates = load_candidates(candidate_path)
    if dry_run:
        result = {
            "mode": "dry-run",
            "daily_reports": report.get("daily_reports", []),
            "candidate_count": len(candidates),
            "stable_candidates": candidates,
            "notion_rows": [candidate_values(c) for c in candidates],
            "known": report.get("known", []),
            "existing_staging": report.get("existing_staging", []),
            "flakes": report.get("flakes", []),
            "watchlist": report.get("watchlist", []),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    missing = [
        name
        for name, value in (
            ("NOTION_TOKEN", notion_token),
            ("NOTION_KNOWN_DATA_SOURCE_ID", known_data_source_id),
            ("NOTION_STAGING_DATA_SOURCE_ID", staging_data_source_id),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"missing write-mode configuration: {', '.join(missing)}")
    if known_data_source_id == staging_data_source_id:
        raise ValueError("official and staging data source ids must be different")

    notion = NotionClient(notion_token or "")
    known_schema = notion.retrieve_data_source(known_data_source_id or "")
    staging_schema_obj = notion.retrieve_data_source(staging_data_source_id or "")
    known_title = data_source_title(known_schema)
    if known_title and known_title.strip().lower() != OFFICIAL_DATA_SOURCE_TITLE.lower():
        raise ValueError(f"refusing to treat unexpected data source {known_title!r} as official")
    staging_title = data_source_title(staging_schema_obj)
    if staging_title and staging_title.strip().lower() != STAGING_DATA_SOURCE_TITLE.lower():
        raise ValueError(f"refusing to write unexpected staging data source {staging_title!r}")
    schema = validate_staging_schema(staging_schema_obj)

    known_before = notion.query_data_source(known_data_source_id or "")
    staging_before = notion.query_data_source(staging_data_source_id or "")
    records = [row_record(page) for page in known_before + staging_before]
    new_candidates = [candidate for candidate in candidates if not is_duplicate(candidate, records)]

    created: list[dict[str, Any]] = []
    for candidate in new_candidates:
        page = notion.create_page(
            staging_data_source_id or "",
            build_notion_properties(candidate, schema),
        )
        page_id = page.get("id")
        if not page_id:
            raise RuntimeError("Notion create-page response did not include an id")
        verified = notion.retrieve_page(page_id)
        record = row_record(verified)
        if record.get("Status") != "Needs review":
            raise RuntimeError(f"created page {page_id} has unexpected Status")
        if f"{FINGERPRINT_PREFIX}{candidate['fingerprint']}" not in record.get("Repro", ""):
            raise RuntimeError(f"created page {page_id} is missing its fingerprint")
        created.append({"id": page_id, "url": page.get("url"), "fingerprint": candidate["fingerprint"]})

    known_after = notion.query_data_source(known_data_source_id or "")
    staging_after = notion.query_data_source(staging_data_source_id or "")
    known_snapshot_before = {
        page.get("id"): json.dumps(page.get("properties") or {}, sort_keys=True)
        for page in known_before
    }
    known_snapshot_after = {
        page.get("id"): json.dumps(page.get("properties") or {}, sort_keys=True)
        for page in known_after
    }
    if known_snapshot_before != known_snapshot_after:
        raise RuntimeError("official known-error data source changed during staging sync")
    created_ids = {item["id"] for item in created}
    staging_ids_before = {page.get("id") for page in staging_before}
    staging_ids_after = {page.get("id") for page in staging_after}
    if not staging_ids_before.issubset(staging_ids_after):
        raise RuntimeError("one or more pre-existing staging pages disappeared during verification")
    if not created_ids.issubset(staging_ids_after):
        raise RuntimeError("one or more created staging pages were not found during verification")
    if len(staging_after) < len(staging_before) + len(created):
        raise RuntimeError("verified staging row count is lower than expected")

    result = {
        "mode": "write",
        "candidate_count": len(candidates),
        "deduplicated_count": len(candidates) - len(new_candidates),
        "created_count": len(created),
        "created": created,
        "official_count": len(known_after),
        "staging_count": len(staging_after),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _token(args: argparse.Namespace) -> str:
    token = args.github_token or os.environ.get("GH_PAT") or os.environ.get("BOT_PAT") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("GitHub token required via --github-token, GH_PAT, BOT_PAT, or GITHUB_TOKEN")
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage stable SGLang AMD CI errors in Notion")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze_parser = sub.add_parser("analyze", help="Generate validated candidate JSON with Claude Code")
    analyze_parser.add_argument("--bot-repo", required=True)
    analyze_parser.add_argument("--lookback-days", type=int, default=7)
    analyze_parser.add_argument("--end-date", help="UTC end date YYYY-MM-DD (test/replay only)")
    analyze_parser.add_argument("--output", type=Path, required=True)
    analyze_parser.add_argument("--github-token")

    sync_parser = sub.add_parser("sync", help="Preview or sync a candidate JSON file to Notion")
    sync_parser.add_argument("--input", type=Path, required=True)
    sync_parser.add_argument("--dry-run", action="store_true")
    sync_parser.add_argument("--known-data-source-id", default=os.environ.get("NOTION_KNOWN_DATA_SOURCE_ID"))
    sync_parser.add_argument("--staging-data-source-id", default=os.environ.get("NOTION_STAGING_DATA_SOURCE_ID"))
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    try:
        if args.command == "analyze":
            analyze(
                _token(args), args.bot_repo, args.lookback_days,
                args.output, end_date=args.end_date,
            )
        else:
            sync(
                args.input,
                args.dry_run,
                notion_token=os.environ.get("NOTION_TOKEN"),
                known_data_source_id=args.known_data_source_id,
                staging_data_source_id=args.staging_data_source_id,
            )
        return 0
    except Exception as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
