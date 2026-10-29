## Cross-Run Pattern Analysis

When asked to analyze patterns across **multiple scheduled runs of the same workflow** within a lookback window (typically 24h, 4-7 runs for `pr-test-amd.yml`), produce a concise pattern report that distinguishes persistent failures, regression candidates, and flakes.

This task is invoked when a single workflow has accumulated several runs in the lookback window (e.g. `pr-test-amd.yml` runs every 6h ⇒ 4 runs/day). The harness pre-computes deterministic buckets (persistent / regression-candidate / flaky); your job is to write the **agent assessment** narrative on top of that.

### Steps

1. The prompt provides:
   - `Workflow:` the workflow file (e.g. `pr-test-amd.yml`)
   - `Runs in window:` count of runs being analyzed
   - Per-run summary (run_id, started_at, n_failed_jobs, head_sha)
   - Pre-computed failure buckets:
     - **Persistent**: jobs that failed in EVERY run in the window
     - **Regression candidates**: jobs that failed ONLY in the latest run
     - **Flaky / intermittent**: jobs that failed in some but not all runs
   - Per-job analyses available in `.ci-context/per-job-analyses.md`
2. Read the per-job analyses, paying attention to each job's `Failure Cluster`, `Status`, and `Hypothesised Causes`.
3. **Apply the completed-runs filter**: if the latest run is still in-progress (not all jobs complete), explicitly label it `[IN-FLIGHT, partial data]` and do NOT base "regression candidate" or "trend dropped" claims on it. Verify run completion via `status` field; if the prompt does not state a run is completed, treat it as in-flight.
4. Cluster the failures by `Failure Cluster` name (same logic as Cross-Job Summary). The same cluster may span persistent + flaky buckets — note which.
5. Identify **trend** for the workflow: failure count per run as a sequence (e.g. `14 → 14 → 17 → 12`). Note direction (improving / stable / worsening) but only if all data points are completed runs.

### Output format

Markdown report under 80 lines (no top-level heading; the harness adds its own):

1. **Headline** (1-3 sentences): are failures dominated by (a) a small set of persistent symptom clusters, (b) flakes, or (c) a fresh regression in the latest run? State the failure-count sequence and explicitly mark any in-flight run.

2. **Persistent clusters** (top 3 by job count): for each cluster, give:
   - Cluster name (one line)
   - Affected jobs (each rendered as ``[`<job label>`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>)`` linking to the upstream sglang job page; never use `[name](#job-<job_id>)` in-issue anchors here)
   - Number of consecutive runs failed and first observed date
   - Top hypothesis with confidence label and disconfirming evidence (one sentence)
   - In-flight fix status

3. **Regression candidates** (latest-run-only failures, EXCLUDING in-flight runs): for each, give:
   - Job + test file + test function (test-level granularity REQUIRED)
   - Why this is likely a fresh regression (vs flake): e.g. "test passed in previous 6 runs, failed in run X with same head_sha as previous job"
   - Hypothesised commit window (`pass_sha..fail_sha`)
   - Suggested triage step (bisect / revert / repro)
   - If the latest run is in-flight, explicitly say `Cannot identify regression candidates yet — latest run [<run_id>] still in progress.`

4. **Flakes / intermittent** (jobs that failed in some but not all completed runs): bullet list with failure ratio (e.g. "3/7 runs"), and suggest whether to retry, quarantine, or investigate.

5. **Suggested next actions** (max 5 bullets): triage steps for the maintainer, ordered by ROI. Each bullet must reference a cluster or row from above. Bot does NOT prescribe fixes — only investigation steps.

Every job/run reference must be a markdown link. Skip empty sections (don't render an empty "Regression candidates" list — say "_(none)_" if you must, but prefer omitting the section).

Do NOT include a `Priority` column or "P0/P1/P2" labels anywhere.
