## Daily Cross-Workflow Summary

When asked to produce the **top-of-issue Daily Cross-Workflow Summary** that aggregates failures across ALL monitored workflows for the day, your output is PATCHed directly into the daily issue's BODY (between the `<!-- daily-cross-workflow-summary:start -->` / `...:end -->` markers), so it appears pinned at the very top of the issue — above every per-workflow comment. This is the artifact engineers see first when they open the daily issue — its #1 job is to let them answer **"Is CI healthy today, and what should I do?"** in 5 seconds.

**Output discipline (REQUIRED).** Do NOT include any "thinking aloud" prose before the report. Your output MUST start with the `# Daily Cross-Workflow Summary — <YYYY-MM-DD>` heading on its very first non-empty line. Anything before the first `#`/`##`/`###` heading will be treated as LLM scratchpad and stripped by the harness — but it's better to never emit it in the first place.

### Steps

1. The prompt provides:
   - `Date:` the daily issue date (UTC)
   - `Issue:` the daily issue number and URL
   - `Snapshot UTC:` the time this analysis was generated
   - List of monitored workflows + their daily run counts + per-workflow per-job analyses available in `.ci-context/per-workflow/<workflow>.md`
   - Optional: previous day's summary for trend comparison in `.ci-context/yesterday-summary.md`
2. For each workflow, read its per-job analyses and extract the `Failure Cluster` + `Hypothesised Causes` from each.
3. **Group the failures by cause, across workflows.** Failures belong together when the same failure pattern (same test file + test function + same top-of-stack-trace) appears in one or more jobs, in any workflow. Each group is one **problem**, with an internal ID `R1`, `R2`, …: reuse the ID yesterday's summary gave the same problem (it is in the hidden `<!-- R… -->` markers and the hidden `ci-monitor-job-clusters` map of `.ci-context/yesterday-summary.md`). A problem that is back after missing days keeps the ID it had when it was last seen: look it up in that day's daily issue (`[CI Monitor] Daily Report - <date>` in `bingxche/sglang-ci-bot`, label `ci-monitor`; older reports show IDs as visible `R…` text, newer ones in hidden comments). Give a problem first seen today the next free ID: yesterday's `<!-- next-free-id: R… -->`, or, when yesterday has none, one more than the highest ID anywhere in yesterday's summary. Never give an old ID to a different problem. IDs are for code and for tomorrow's report, never for the reader: see **Plain language** below. Jobs whose runner was lost (`Self-hosted runner lost communication (<runs-on label>)`) have no test to match on: group them by runner pool and pattern (a host outage and a job that takes its runner down are separate problems, even on one pool), and give the status line the runners and the time window. They are infrastructure, so they never go in `suspect-prs`.
4. **Compute trends**: for each workflow, the sequence of failure counts across its runs in the lookback window. **Only count completed runs**. A run that is still going is left out of the trend and called "still running".
5. **Identify problems new today**: problems that did not appear in yesterday's summary. Mark them `🆕`. A problem that is back after missing days is marked `↩️` with the date it was last seen.
6. **Identify in-flight fixes**: aggregate the In-flight Fix Check from per-job analyses. If multiple per-jobs cite the same PR, list it once at the problem level.
7. **Compute the TL;DR ask**: the concrete actions an engineer should take today, each naming the problem in words. Examples:
   - `👉 **Today's ask**: get [#23072](url) reviewed; it fixes the MiniCPM-V 2.6 startup crash`
   - `👉 **Today's ask**: no action — all failures are known and tracked`

### Plain language (REQUIRED)

The reader is an engineer who opens the issue cold. Every line must make sense without knowing the bot's bookkeeping.

- **No problem IDs in visible text.** Never write `R386` in a heading, table cell (the quiet-problems table and its fix column included), TL;DR, ask, Status line or prose; refer to another problem, today's or an older one, by its name. Name the problem by what fails, in a few words: "GPU 0 on `mia1-p02-g28` is faulty", "MiniCPM-V 2.6 crashes at startup". The ID goes only in a hidden HTML comment at the start of the problem's `###` heading or table row (`<!-- R386 -->`; several problems: `<!-- R332 · R269 -->`), and in the `job-clusters` and `suspect-prs` blocks. Code reads the IDs from those places.
- **No run labels.** Never invent short names for runs (`BC`, `BI`, `N35a`, `ML10o`, `D15`). Name a run by its workflow, start time (UTC) and ROCm version, linked to the run: `[PR Test, Nov 5 01:11, rocm10](https://github.com/sgl-project/sglang/actions/runs/<run_id>)`. In tables, the Workflow column holds the same: `pr-test-amd ([Nov 5 01:11, rocm10](run-url))`. Trends are listed oldest to newest, with the dates they cover.
- **Plain words over shorthand.** Write "still running" (not `IN-FLIGHT`), "continuing from earlier days" (not "carry-over"), "config" or "test" (not "leg"), "ROCm version" (not "lane"), "take the node out of service" (not "cordon"), "run the workflow by hand" (not "dispatch"), "problem" (not "cluster"). Keep the confidence labels (`[FACT]` … `[SPECULATION]`); the report's footer explains them.

### Output format

```
# Daily Cross-Workflow Summary — <YYYY-MM-DD>
**Snapshot**: <YYYY-MM-DD HH:MM UTC> · Only completed runs counted · Checked hourly; updated when new failures are found

## TL;DR
<one of: 🟢 GREEN | 🟡 YELLOW | 🔴 RED> · **<N> problems** (**<X> new today** · <W> back again · <Y> continuing) · <Z> with a fix PR
- **Since the last update (<HH:MM>)**: <what changed, in one or two sentences>
- **New**: <each new problem in a few words, and where>
- **Better**: <problems that passed or did not come back>
- <at most two more bullets: a fix waiting for an image, runners not picking up jobs, …>

👉 **Today's ask**:
1. <concrete action, naming the problem in words>
2. …

## Workflow status

| Workflow | Runs | ✅ | ❌ | Trend, failed jobs per completed run (oldest → newest) | Δ vs yesterday |
|---|---|---|---|---|---|
| pr-test-amd | [Nov 5 01:11, rocm10](run-url) (still running) | 32 | 2 (4 waiting) | rocm10 Oct 31–Nov 4: 3·3·4·2·**1** · rocm724 Oct 30–Nov 3: 9·5·10 | -1 (better) |
| nightly-test-amd | [Nov 4 16:07, rocm10](run-url) | 42 | 2 | 3·4·3·8·6·12 (Oct 30–Nov 4) | +0 |
...

## Failures by problem

For each problem (sorted: 🆕 new first, then ↩️ back again, then by total job count DESC, then by persistence DESC):

### <!-- <ID> --> <🆕 or ↩️ if it applies> <Problem in plain words> — <workflows> · <✅ fix open | ✅ fix merged | ⚠️ partial fix | ❌ no fix>
- **Status**: e.g. "first seen 2026-04-19 12:15 UTC (1 run)" or "failing for 5 days, 6 jobs in 3 workflows"
- **Top hypothesis**: `[<CONFIDENCE>]` <suspected change first, if any: [#<num>](pr-url) ([`<sha>`](commit-url))> <hypothesis>. **Disconfirming**: <one line> (or `none found`)
- **In-flight fix**: ✅ [#<num>](url) (open since <date>) — <action> | ❌ none found
- **Suggested triage**: <one or two concrete steps>

| Workflow | Job | Test File | Test Function | Error (one line) | Log |
|---|---|---|---|---|---|
| pr-test-amd ([Nov 5 01:11, rocm10](run-url)) | [`stage-b-test-1-gpu-small-amd (rocm10, 2)`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test/registered/rl/test_lora_load_from_tensor.py` | `TestLoRALoadFromTensor.setUpClass` | `Memory access fault → exit -6` | [link](url) |
| nightly-test-amd ([Nov 4 16:07](run-url)) | `nightly-test-1-gpu-lora` [rocm10](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) · [rocm724](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test/registered/lora/test_lora_e2e.py` | `test_lora_full_pipeline` | same | [link](url) |

Several small problems of one workflow may share a section. Then the heading's hidden comment lists every ID, the heading names the group in words, and each table row starts with its own problem's ID and name:

### <!-- R332 · R269 --> Nightly: failures continuing from earlier days — nightly-test-amd · ✅ fix open
| Problem | Workflow | Job | Test File | Test Function | Log |
|---|---|---|---|---|---|
| <!-- R332 --> Qwen3.8-Flash-Next FP8 crashes on MI300 | … | … | … | … | … |

(Test File + Test Function granularity is REQUIRED. Same problem across workflows lives in ONE table. The `Job` cell MUST be a clickable link to the actual sglang job page — `https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>` — never an in-issue `#job-<id>` anchor. Name jobs per **Job names** in the Ground Rules: the backticked job label, or for several legs of one job the backticked job name followed by one link per leg labelled with its matrix values.)

After all new and active problems, render the problems with no failure today in a single collapsed `<details>` block:

<details><summary><b>Quiet problems (no action today)</b> · click to expand</summary>

| Problem | Workflows × Jobs | First seen | In-flight fix |
|---------|------------------|------------|---------------|
| <!-- R5 --> VLM perf threshold too high (test config) | pr-test-amd ×1 | Apr 15 | ❌ |
| <!-- R6 --> Qwen3-30B-A3B test never passed on AMD | pr-test-amd ×1 | Apr 15 | ❌ |
...
</details>

## Workflow drill-down (per-workflow view)

<details><summary><b>pr-test-amd</b> · latest completed run [Nov 4 01:10, rocm10](run-url) · N failures</summary>

| Job | Test File | Test Function | Problem | Error |
|---|---|---|---|---|
| [`stage-b-test-1-gpu-small-amd (rocm10, 2)`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test_lora_load_from_tensor.py` | `setUpClass` | <!-- R2 --> LoRA memory fault | mem fault |
| [`stage-b-test-1-gpu-small-amd (rocm10, 11)`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>) | `test_multi_lora_backend.py` | `test_ci_lora_models_multi_batch` | <!-- R3 --> torchao missing | torchao ImportError |
...
</details>

<details><summary><b>nightly-test-amd</b> · latest completed run [<date time, rocm>](run-url) · N failures</summary>
... same structure
</details>

## How this report is generated
- Only `status == "completed"` runs are counted in trends. Runs that are still running are listed but left out of the trends.
- Confidence labels: `FACT` (verified) / `HIGH` (multiple evidence) / `MEDIUM` (one + timing) / `LOW` (timing only) / `SPECULATION`. Default `LOW`.
- Bot does NOT assign Priority. The Status line + problem size + persistence are the inputs; engineers decide priority.
- In-flight fix lookup is performed for every problem; existing PRs are linked instead of duplicated.
<!-- next-free-id: R<n> -->

---
*Generated by amd-bot · last updated <YYYY-MM-DD HH:MM UTC>*
```

### Constraints

- Length budget: 200 lines max for the rendered summary. Use `<details>` aggressively for quiet problems and per-workflow drill-down.
- The TL;DR + Workflow status table + active problems MUST fit "above the fold" (visible without scrolling).
- Every workflow listed in the prompt MUST appear in the Workflow status table, even if 0 failures (show ✅ row to confirm coverage).
- Every problem MUST list ALL `(workflow, job, test_file, test_function)` rows — full granularity, no rolling-up at this layer.
- Bot does NOT assign Priority; never use words like "P0", "P1", "Critical", "High Priority" in this report.
- If a failure fits no known problem (e.g. only one occurrence with no historical context), give it a fresh ID and say "seen once" in the Status line.
- Write every ID on its own in the hidden comments, `<!-- R239 · R240 · R241 -->`, never as a range like `R239–R241` (a range hides the IDs in between from the code that reads them).

### Job → cluster map (REQUIRED)

End your output with one fenced block, after the footer, that maps every cluster ID to the job IDs it covers:

````
```json job-clusters
{"R25": [117742354635, 117742354706], "R209": [117742355001], "R268": [118301122334]}
```
````

- Keys are cluster IDs; values are the numeric GitHub job IDs (the `**Job ID:**` of each per-job analysis, the same number as in `/job/<job_id>` links).
- **Every job ID in `.ci-context/per-workflow-analyses.md` MUST appear under at least one cluster.** A failure that fits no known problem gets a fresh ID, as above. Also include the job IDs you link from earlier runs (late legs of yesterday's run, Scout runs).
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
