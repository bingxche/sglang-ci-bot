## Cross-Job Summary

When asked to summarize failures across multiple jobs in a workflow, read the per-job analyses from `.ci-context/per-job-analyses.md` and produce a concise cross-job summary that **groups jobs by Failure Cluster** rather than listing them one-by-one.

You have access to the sglang source code in the current directory. Use it to verify cluster grouping (e.g. if multiple jobs share the same stack-trace prefix, that supports clustering them). Do NOT use the source code to invent root causes; clusters are symptom-level groupings, not causal claims.

### Steps

1. Read `.ci-context/per-job-analyses.md`. Each per-job section is headed by `### Job: <job_name>` and includes `**Job ID:** <numeric_job_id>` — memorize the `job_id` for each job; you will need it for anchor links.
2. Extract each per-job analysis's `Failure Cluster` line (from the Job Failure Analysis output format). If a per-job is missing this field (legacy), derive a one-line cluster name from the error message.
3. Group jobs by `Failure Cluster`. Two jobs share a cluster only if they have the **same symptom AND the same stack-trace top frames**, not just the same error keyword.
4. For each cluster, aggregate the per-job Hypothesised Causes; surface only those that appear with confidence ≥ MEDIUM in at least one per-job. Lower-confidence hypotheses are deferred to per-job detail.
5. For `amd-aiter-scout.yml` summaries, extract the `Failure Origin` from each per-job analysis; if missing, treat as `unclear`.
6. Produce a summary under 80 lines.

### Row reference rule (MUST follow)

When referring to rows in prose, use `row 1`, `row 2`, ... (or the full job name). **NEVER** write `#1`, `#2`, etc. — GitHub auto-links `#N` to issues/PRs in the repo and produces misleading link text in the rendered comment.

### Summary Table sort order (MUST follow)

Sort the Summary Table rows in this order:
1. **Cluster size** DESC (largest cluster first — biggest blast radius).
2. **Persistence** DESC within a cluster (longest-running failure first).
3. For `amd-aiter-scout.yml` only — then by **Origin**: `aiter-caused` → `pre-existing (sglang)` → `unclear`.
4. Then **Job name** ASC (alphabetical) within otherwise-equal rows.

The `#` column is the 1-based row index in this SORTED order. Bot does NOT assign Priority — engineers infer urgency from cluster size + persistence.

### Job column link (MUST follow)

The `Job` cell MUST be a clickable markdown link to the **actual sglang workflow job page** on github.com — not an in-issue anchor — so that one click takes the maintainer straight to the failing CI log:
```
[`<job label>`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>)
```
where `<run_id>` and `<job_id>` are the numeric IDs from the per-job analysis header, and `<job label>` follows **Job names** in the Ground Rules. Example:
``[`nightly-test-1-gpu-unit (rocm10)`](https://github.com/sgl-project/sglang/actions/runs/24635222311/job/71716987472)``.

Do NOT use `[name](#job-<job_id>)` in the Job column — those in-issue anchors merely scroll the reader to the bot's own analysis block, not to the underlying CI failure. (The `<a id="job-<job_id>">` anchors above each per-job detail block still exist and may be linked from prose like "see analysis below" if useful, but the Summary Table's Job column is reserved for the upstream sglang job URL.)

### Output format

1. **Counts** (MUST be the very first line, before the table):
   - For `amd-aiter-scout.yml`:
     `**Counts**: 27 failures · 5 clusters · Origin: 20 aiter-caused · 4 pre-existing (sglang) · 3 unclear`
   - For other workflows:
     `**Counts**: 27 failures · 5 clusters · 12 carrying over (≥3 days) · 2 new today`

2. **Summary Table** (immediately after Counts): one row per failing test, grouped visually by cluster.
   - For `amd-aiter-scout.yml`, columns MUST be:
     `| # | Cluster | Job | Test File | Test Function | Origin | Status | Hypothesis (confidence) |`
   - For workflows whose job names carry a ROCm version (`rocm10`, `rocm724`, `rocm720`, … — e.g. `pr-test-amd.yml`, `nightly-test-amd.yml`), columns are:
     `| # | Cluster | Job | ROCm | Test File | Test Function | Status | Hypothesis (confidence) |`
   - For all other workflows, columns are:
     `| # | Cluster | Job | Test File | Test Function | Status | Hypothesis (confidence) |`

   Column rules:
   - **Cluster**: short noun phrase (e.g. "GPU mem fault during warmup"). Repeat the same cluster name across all rows that belong to it — this is what makes grouping visible. Each unique cluster also gets a `### Cluster: <name>` subsection below the table.
   - **Job**: ``[`<job label>`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>)`` — link to the actual sglang job page, NOT an in-issue anchor. See "Job column link (MUST follow)" above.
   - **ROCm**: which ROCm versions fail this row and which pass it, in the workflow's version order (default version first): `✗ rocm720 · rocm724; ✓ rocm10`. A version is `✓` only if a per-job analysis shows that same test passing on it in a comparable run (same or later commit); a version with no evidence either way is left out, never guessed. Clusters span ROCm versions — do NOT split a cluster per version; this column is how a reader sees the split.
   - **Test File / Test Function**: from the per-job's `Failed Tests` table. Test-file granularity is REQUIRED — never report at job-only level.
   - **Origin** (scout only): `aiter-caused` | `pre-existing (sglang)` | `unclear`.
   - **Status**: factual one-liner — e.g. "5 days persistent" / "new today" / "1/7 runs (flaky)" / "Never-passed". NOT priority.
   - **Hypothesis (confidence)**: top hypothesis from the per-job analysis with its confidence label, e.g. ``[MEDIUM] [#23001](url) ([`6760c790b`](url)) — narrows out_cache_loc`` (suspected change first, per the Ground Rules). If the per-job has no hypothesis ≥ MEDIUM, write `[no candidate ≥ MEDIUM]`.

3. **Cluster details** (one `### Cluster: <name>` subsection per unique cluster). Each subsection contains:
   - **Affected**: list of `(job, test_file, test_function)` rows from the table (each job rendered as ``[`<job label>`](https://github.com/sgl-project/sglang/actions/runs/<run_id>/job/<job_id>)`` linking to the upstream sglang job page).
   - **Shared facts**: what the per-jobs agree on (stack-trace overlap, common version, common runner). When the ROCm versions split (some fail, some pass), state the split and what differs between those images (ROCm/torch/Python version, image tag) — it is evidence for or against a code change as the cause.
   - **Top hypothesis**: best candidate cause with confidence label and disconfirming evidence.
   - **In-flight fix**: ✅ PR #N or ❌ none — copy from per-job In-flight Fix Check; if multiple per-jobs cite the same PR, list it once.
   - **Suggested triage**: one or two concrete steps (bisect / repro / disable). NOT a prescription.
   - For `amd-aiter-scout.yml`: only discuss aiter-caused failures here. `pre-existing (sglang)` failures go in the next section.

4. **Pre-existing sglang failures (not caused by aiter)** — REQUIRED for `amd-aiter-scout.yml` when at least one row has `Origin = pre-existing (sglang)`. List those rows and note that they already fail in the regular (non-override) runs. Omit this heading for non-scout workflows.

5. **Suggested triage order** — a ranked table of NEXT INVESTIGATION STEPS (NOT fixes; bot does not prescribe fixes). Columns:
   `| Rank | Triage step | Targets | Why this first |`
   - `Rank`: 1, 2, 3, ... in the order steps should be attempted.
   - `Triage step`: one-line concrete investigation action (e.g. "Verify in-flight fix [#23072](url) merges cleanly and re-run cluster A jobs").
   - `Targets`: which clusters/rows this step would resolve — "cluster A (rows 1-6)" or "row 12 only".
   - `Why this first`: rationale based on cluster size, in-flight fix availability, or evidence strength.

   Order rationale: prefer steps that (a) verify an existing in-flight fix, (b) resolve the largest cluster, (c) gather evidence cheaply (single rerun or local repro). Bot does NOT assign Owner — engineering allocation is a human call.

Do NOT repeat per-job analysis. Do NOT write code. Do NOT include a `Priority` column anywhere. Remember the Link hygiene rule: every commit SHA, PR number, and run ID MUST be a clickable markdown link.
