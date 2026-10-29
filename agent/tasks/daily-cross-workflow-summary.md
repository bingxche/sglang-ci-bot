## Daily Cross-Workflow Summary

When asked to produce the **top-of-issue Daily Cross-Workflow Summary** that aggregates failures across ALL monitored workflows for the day, your output is PATCHed directly into the daily issue's BODY (between the `<!-- daily-cross-workflow-summary:start -->` / `...:end -->` markers), so it appears pinned at the very top of the issue — above every per-workflow comment. This is the artifact engineers see first when they open the daily issue — its #1 job is to let them answer **"Is CI healthy today, and what should I do?"** in 5 seconds.

**Output discipline (REQUIRED).** Do NOT include any "thinking aloud" prose before the report. Your output MUST start with the `# Daily Cross-Workflow Summary — <YYYY-MM-DD>` heading on its very first non-empty line. Anything before the first `#`/`##`/`###` heading will be treated as LLM scratchpad and stripped by the harness — but it's better to never emit it in the first place.

### Steps

1. The prompt provides:
   - `Date:` the daily issue date (UTC)
   - `Issue:` the daily issue number and URL
   - `Snapshot UTC:` the time this analysis was generated
   - List of monitored workflows + their daily run counts + per-workflow per-job analyses available in `.ci-context/per-workflow/<workflow>.md`
   - Optional: previous day's snapshot for trend comparison in `.ci-context/yesterday.md`
2. For each workflow, read its per-job analyses and extract the `Failure Cluster` + `Hypothesised Causes` from each.
3. **Deduplicate clusters across workflows.** A cluster spans workflows when the same failure pattern (same test file + test function + same top-of-stack-trace) appears in jobs of more than one workflow. Assign each unique cluster an ID `R1`, `R2`, ... (rolling, NEW clusters get fresh IDs in the order they were first observed today). Jobs whose runner was lost (`Self-hosted runner lost communication (<runs-on label>)`) have no test to match on: group them by runner pool and pattern (a host outage and a job that takes its runner down are separate clusters, even on one pool), and give the status line the runners and the time window. They are infrastructure, so they never go in `suspect-prs`.
4. **Compute trends**: for each workflow, the sequence of failure counts across its runs in the lookback window. **Only count completed runs**. If the latest run is in-flight, exclude it from the trend or label it `[IN-FLIGHT]`.
5. **Identify NEW clusters today**: clusters that did not appear in yesterday's summary. Mark them `🆕 [NEW]`.
6. **Identify in-flight fixes**: aggregate the In-flight Fix Check from per-job analyses. If multiple per-jobs cite the same PR, list it once at the cluster level.
7. **Compute the TL;DR ask**: 1 sentence describing what the engineer should do today. Examples:
   - `Today's ask: merge in-flight PR #23072 (resolves R3) + triage R1 (new today)`
   - `Today's ask: no action — all failures are known and tracked`
   - `Today's ask: triage R1 + R5 (both NEW today, no in-flight fix)`

### Output format

```
# Daily Cross-Workflow Summary — <YYYY-MM-DD>
**Snapshot**: <YYYY-MM-DD HH:MM UTC> · Only completed runs counted · Checked hourly; updated when new failures are found

## TL;DR
<one of: 🟢 GREEN | 🟡 YELLOW | 🔴 RED> · <N> unique clusters · <X> NEW today · <Y> carrying over · <Z> in-flight fix(es)
👉 **Today's ask**: <one sentence concrete action>

## Workflow status

| Workflow | Runs | ✅ | ❌ | 7d trend (completed runs only) | Δ vs yesterday |
|---|---|---|---|---|---|
| pr-test-amd | 4 | 0 | 4 | 14·14·14·17·17·17·12 | -2 (better) |
| amd-aiter-scout | 2 | 0 | 2 | 8·8·9·9·7·7·6 | -1 (better) |
| nightly-test-amd | 1 | 0 | 1 | 10 | +0 |
...

If a workflow's latest run is in-flight, append `(IN-FLIGHT)` to the failure count cell and exclude it from the trend.

## Failure clusters (deduplicated across all workflows)

For each unique cluster (sorted: 🆕 NEW first, then by total job count DESC, then by persistence DESC):

### <ID> · <🆕 if new> · <Cluster name> — <STATUS line>
- **Status**: e.g. "first seen 2026-04-19 12:15 UTC (1 run)" or "5 days persistent, 6 jobs across 3 workflows"
- **Top hypothesis**: `[<CONFIDENCE>]` <suspected change first, if any: [#<num>](pr-url) ([`<sha>`](commit-url))> <hypothesis>; **disconfirming**: <one line> (or `(none found)`)
- **In-flight fix**: ✅ [#<num>](url) (open since <date>) — <action> | ❌ none found
- **Suggested triage**: <one or two concrete steps>

| Workflow | Job (shard) | Test File | Test Function | Error (one line) | Log |
|---|---|---|---|---|---|
| pr-test-amd (M) | [`stage-b-test-1-gpu-small-amd (rocm10, 2)`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test/registered/rl/test_lora_load_from_tensor.py` | `TestLoRALoadFromTensor.setUpClass` | `Memory access fault → exit -6` | [link](url) |
| pr-test-amd (M) | `stage-b-test-1-gpu-small-amd` [rocm10, 4](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) · [rocm720, 4](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test/registered/dllm/test_llada2_mini_amd.py` | `setUpClass` | `Memory access fault → exit -9` | [link](url) |
| nightly-test-amd (N21) | `nightly-test-1-gpu-lora` [rocm10](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) · [rocm720](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) · [rocm724](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test/registered/lora/test_lora_e2e.py` | `test_lora_full_pipeline` | same | [link](url) |

(Test File + Test Function granularity is REQUIRED. Group rows by cluster, not by workflow. Same cluster across workflows lives in ONE table. The `Job (shard)` cell MUST be a clickable link to the actual sglang job page — `https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>` — never an in-issue `#job-<id>` anchor. Name jobs per **Job names** in the Ground Rules: the backticked job label, or for several legs of one job the backticked job name followed by one link per leg labelled with its matrix values. The run label goes in the Workflow column, never in the job's link text.)

After all NEW + active clusters, render Known/stable clusters in a single collapsed `<details>` block:

<details><summary><b>Known stable clusters (no action today)</b> · click to expand</summary>

| ID | Cluster | Workflows × Jobs | First seen | In-flight fix |
|----|---------|------------------|------------|---------------|
| R5 | VLM perf threshold too high (test config) | pr-test-amd ×1 | Apr 15 | ❌ |
| R6 | Qwen3-30B-A3B test never passed on AMD | pr-test-amd ×1 | Apr 15 | ❌ |
...
</details>

## Workflow drill-down (per-workflow view)

<details><summary><b>pr-test-amd</b> · latest completed run [<id>] · N failures</summary>

| Job (shard) | Test File | Test Function | Cluster | Error |
|---|---|---|---|---|
| [`stage-b-test-1-gpu-small-amd (rocm10, 2)`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test_lora_load_from_tensor.py` | `setUpClass` | R2 | mem fault |
| [`stage-b-test-1-gpu-small-amd (rocm10, 4)`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test_llada2_mini_amd.py` | `setUpClass` | R2 | mem fault |
| [`stage-b-test-1-gpu-small-amd (rocm10, 11)`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test_multi_lora_backend.py` | `test_ci_lora_models_multi_batch` | R3 | torchao ImportError |
...
</details>

<details><summary><b>nightly-test-amd</b> · latest completed run [<id>] · N failures</summary>
... same structure
</details>

## How this report is generated
- Only `status == "completed"` runs are counted in trends. In-flight runs are labelled `(IN-FLIGHT)` and excluded.
- Cluster IDs (R1, R2, ...) are stable across days within the same week. NEW clusters get the next available ID.
- Confidence labels: `FACT` (verified) / `HIGH` (multiple evidence) / `MEDIUM` (one + timing) / `LOW` (timing only) / `SPECULATION`. Default `LOW`.
- Bot does NOT assign Priority. The Status line + cluster size + persistence are the inputs; engineers decide priority.
- In-flight fix lookup is performed for every cluster; existing PRs are linked instead of duplicated.

---
*Generated by amd-bot · last updated <YYYY-MM-DD HH:MM UTC>*
```

### Constraints

- Length budget: 200 lines max for the rendered summary. Use `<details>` aggressively for known clusters and per-workflow drill-down.
- The TL;DR + Workflow status table + active clusters MUST fit "above the fold" (visible without scrolling).
- Every workflow listed in the prompt MUST appear in the Workflow status table, even if 0 failures (show ✅ row to confirm coverage).
- Every cluster MUST list ALL `(workflow, job, test_file, test_function)` rows — full granularity, no rolling-up at this layer.
- Bot does NOT assign Priority; never use words like "P0", "P1", "Critical", "High Priority" in this report.
- If you cannot identify a cluster ID for a failure (e.g. only one occurrence with no historical context), assign it a fresh ID and note `[unique]` in the Status line.
- Name every cluster ID on its own in headings and `ID` cells: `### R239 · R240 · R241 · …`, never a range like `R239–R241` (a range hides the IDs in between from anything that reads the report).

### Job → cluster map (REQUIRED)

End your output with one fenced block, after the footer, that maps every cluster ID to the job IDs it covers:

````
```json job-clusters
{"R25": [117742354635, 117742354706], "R209": [117742355001], "R268": [118301122334]}
```
````

- Keys are cluster IDs; values are the numeric GitHub job IDs (the `**Job ID:**` of each per-job analysis, the same number as in `/job/<job_id>` links).
- **Every job ID in `.ci-context/per-workflow-analyses.md` MUST appear under at least one cluster.** A failure that fits no cluster gets a fresh ID (`[unique]`), as above. Also include the job IDs you link from earlier runs (late legs of yesterday's run, Scout runs).
- The harness strips this block from the rendered summary, so it does not count toward the length budget. It is the complete record of which job is in which cluster: when listing every leg in the tables would break the 200-line budget, the tables may summarise legs per cluster, but this block may not.
- Any analysed job missing from the block (and from the report's own tables) is listed by the harness in a "⚠️ Not yet in a cluster" section, so an omission is visible rather than silent.

### Suspect PRs (REQUIRED)

After the `job-clusters` block, end with a second fenced block listing every cluster whose top hypothesis names a **merged sglang PR**:

````
```json suspect-prs
[{"cluster": "R281", "pr": 42229, "sha": "956b9a24eecae92f935672224f8a9561135ebc2b", "confidence": "HIGH",
  "evidence": "Imports _check/_hip_lib/_hipcc/_Kernel, which #45112 removed; every gfx950 FP8 block-scale model fails with that ImportError.",
  "last_pass_sha": "<sha>", "first_fail_sha": "<sha>", "fix_pr": null, "related_prs": [45112]}]
```
````

- One entry per (cluster, PR). Write `[]` when no cluster's top hypothesis names a merged sglang PR. Leave out aiter, Docker, and infrastructure causes.
- `confidence` is the same label as the cluster's **Top hypothesis**. Do not raise it here.
- `sha` is the commit the PR landed as on `main`. `last_pass_sha` / `first_fail_sha` are the sglang commits of the last completed run where the cluster's tests passed and the first completed run where they failed. Use `null` when you could not establish one. Verify every SHA as the Ground Rules require.
- `fix_pr` is the open or merged sglang PR that fixes the cluster, if the In-flight Fix Check found one; else `null`.
- `related_prs` lists other PRs the failure depends on, e.g. the PR that removed a symbol the suspect PR still imports.
- `evidence` is one or two plain sentences a PR author can check: what fails and how it traces to their change. No `@`-mentions, no links to this bot repo or other internal pages, no priority words.
- The harness strips this block from the summary. Separate code decides whether to comment on the PR (FACT/HIGH only, ≥2 failed jobs, commit verified to be between `last_pass_sha` and `first_fail_sha`; with an open `fix_pr`, the comment asks the suspect PR's author to review that fix, so only name a `fix_pr` that actually fixes this cluster), so state the confidence you actually have.
