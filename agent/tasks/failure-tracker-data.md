## Failure Tracker Data

This task produces the machine-readable input for the **long-lived per-workflow Failure Trackers** on `sgl-project/sglang` (one issue per tracked workflow, titled `[Failure Tracker] <workflow>`). Those trackers are a **permanent fact record**: a test that has been red for months keeps an accurate "Broken since" date. A deterministic harness owns all persistence (`first_seen`, duration, dedup, rendering); **your only job is to find what failed today** and report it as JSON. You do NOT compute dates, durations, or "broken for N days".

Your output is consumed by a parser, NOT shown to humans, so it must be **only** the JSON block below — no prose, no narrative, no tables.

### Steps

1. The prompt provides:
   - `Workflows:` the list of tracked workflow files (e.g. `pr-test-amd.yml`).
   - `.ci-context/failure-tracker-analyses.md` — the per-job failure analyses for those workflows, recovered from today's daily report. Each job block carries `Workflow:`, `Job ID:`, `Run URL:`, `Started:` and the full per-job analysis (which already contains a `### Failed Tests` table, a `### Failure Cluster`, and a `### Regression Status`).
2. For each tracked workflow, read every job's analysis and extract each failing **(test file + test function)** from its `### Failed Tests` table.
3. **Deduplicate by (workflow, test_file, test_function).** If the same test failed in several jobs/shards, emit it ONCE — pick the most recent run for `job_url` / `job_id` / `run_started_at`.
4. Build `job_url` as `https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>`, where `<run_id>` is the LAST path segment of that job's `Run URL` and `<job_id>` is its `Job ID`.

### Rules

- Reuse the facts already in the analyses — do NOT re-investigate or invent.
- `error` is exactly ONE line (error type + short message), no newlines.
- `status` is the regression status you see, e.g. `new today` / `recurring` / `flaky 2/4` / `never-passed`.
- A non-test failure (build error, server crash, runner hang) is still a row: put a short descriptor in `test_file` and `N/A` in `test_function`. A lost runner is `runner lost during <step>`, one row per job, since there is no test to deduplicate on.
- Do NOT include `first_seen`, `duration`, or any "days" field — the harness owns those.
- Include a row for EVERY failing test across ALL tracked workflows. If a tracked workflow had no failures, simply emit no rows for it (an overall empty array `[]` is valid if nothing failed).

### Output format

Output ONLY this block (verbatim markers, JSON inside the fence, nothing else):

<!-- ci-failure-tracker-data:start -->
```json
[
  {
    "workflow": "pr-test-amd.yml",
    "test_file": "test/registered/rl/test_lora_load_from_tensor.py",
    "test_function": "TestLoRALoadFromTensor.setUpClass",
    "cluster": "GPU memory access fault during warmup",
    "error": "Memory access fault → exit -6",
    "status": "recurring",
    "job_url": "https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>",
    "job_id": 71716987472,
    "run_started_at": "2026-06-11T06:41:00Z"
  }
]
```
<!-- ci-failure-tracker-data:end -->
