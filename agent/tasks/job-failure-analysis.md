## Job Failure Analysis — Per-Job Failure Investigation

When the prompt asks you to analyze a single CI job failure, answer three questions:

1. **What failed?** — Identify the exact test file(s) and test function(s) that failed. Include the error message and a link to the specific log line. Be precise: not just "the decode test job failed", but "test_mla_correctness in test/srt/test_mla_correctness.py failed with AssertionError on line 42".
2. **When did it start?** — Check the most recent 15 comparable completed scheduled runs of the same workflow/job to determine if this is a new regression, a recurring failure, or a flaky test. When querying historical runs via the GitHub API, you MUST filter to get comparable runs:
   - **`branch`**: filter by the same branch (e.g. `branch=main`). Runs on different branches have different job matrices and are not comparable.
   - **`event`**: filter by the same trigger event. The prompt includes an `Event filter:` line — use it. For this scheduled monitor, use `event=schedule`; a `schedule`-triggered run should only be compared with other `schedule`-triggered runs, not `pull_request` or `workflow_dispatch` runs of the same workflow.
   - **`status`**: only count completed runs. Exclude queued / in-progress / waiting runs from the 15-run history, or explicitly label them `[IN-FLIGHT]` if mentioned for context.
   - **Only count runs where the job actually ran** — skip runs where the job was skipped, cancelled, or not part of the matrix.
   - **Check at the test file level, NOT the job level.** A job may fail for many reasons — a different test file may have failed in a previous run. You MUST download the log for each historical run and check whether the **same test file** that failed in the current run also failed in that historical run. Do NOT rely on the job's overall pass/fail status. For example, if `test_mla.py` failed in today's run, and yesterday's run also shows "failed" but the failure was in `test_decode.py` (while `test_mla.py` passed), then yesterday is a **passing** run for `test_mla.py`.
3. **Why did it fail?** — For regressions, narrow down the suspicious commit(s) using the commit range between the last passing run and the first failing run:
   - Get the head SHA of the last passing run (`pass_sha`) and the first failing run (`fail_sha`) from step 2.
   - Run `git log --oneline pass_sha..fail_sha` to enumerate all commits merged in that window.
   - Don't just look at the test file's own git history — the root cause is often in production code that the test imports or exercises.
   - Read the relevant source code and use git blame/log/show as needed to identify the culprit.

Include all evidence with hyperlinks.

### Test-file level analysis (REQUIRED)

CI jobs often run multiple test files. You MUST identify failures at the **test file + test function** level, not just the job level. From the log, extract:

- The test file path (e.g. `test/srt/test_mla_correctness.py`)
- The specific test function(s) that failed (e.g. `test_mla_correctness`)
- The exact error type and message (e.g. `AssertionError: Tensor mismatch at rtol=0.01`)
- Pass/fail counts if available (e.g. `3 passed, 1 failed`)

If a job runs a test suite (pytest, unittest), always report which specific tests failed, not just "the job failed".

### Link format

- Job page: `https://github.com/sgl-project/sglang/actions/runs/{run_id}/job/{job_id}`
- Specific log line: `https://github.com/sgl-project/sglang/actions/runs/{run_id}/job/{job_id}#step:{step_number}:{line_number}`

### Workflow-specific artifact evidence (REQUIRED for `nightly-amd-mi355x-disagg.yml`)

The MI355X disagg nightly is a multi-node prefill/decode/router benchmark. Its
GitHub job page log is mostly launcher/control-plane output; the authoritative
prefill server, decode server, router/bench, and exit-marker logs are uploaded
as run artifacts named `mi355x-*`. For this workflow, do NOT diagnose from the
job page log alone.

If the prompt includes `Artifact context: .ci-context/mi355x-artifacts/MANIFEST.md`,
read that manifest first, then inspect the extracted files it lists:

- `bench.log` — router startup, health waits, smoke request, GSM8K gate, and benchmark harness output.
- `prefill_*.log` — prefill server process logs.
- `decode_*.log` — decode server process logs.
- `server_exit_*` / `bench_exit` — which role exited first and with what code.

Use the job log only as the timeline and artifact upload confirmation. The
failure classification, Failed Tests / non-test failure row, Facts, and
Hypothesised Causes must cite evidence from the artifact logs when those logs
exist. Since artifact files do not have GitHub line anchors, cite them as
`artifact:<relative_path>` plus the matched timestamp / log fragment; keep the
job-page link for the outer job reference.

For regression history on `nightly-amd-mi355x-disagg.yml`, apply the same rule
to comparable historical runs: list that run's `mi355x-*` artifacts, download
the matching archive(s), extract any `*_logs.tar.gz`, and compare the same
prefill/decode/router failure signal at the artifact-log level. Do not mark a
historical run "passed for this failure" merely because its GitHub job page log
does not contain the server-side error.

### Miles workflows (REQUIRED for `nightly-test-amd-miles-*.yml` and `release-docker-amd-miles-*.yml`)

These workflows test [radixark/miles](https://github.com/radixark/miles), an RL
training framework built on sglang. Their runs sit on sglang `main`, but the
code they execute is **not** the run's commit:

- A **nightly** (`nightly-test-amd-miles-rocm10*.yml`) runs miles' own test
  scripts (`tests/...`, `/root/miles/...` in tracebacks) inside a dated image,
  `rocm/sgl-dev:miles-<track>-<gpu>-<YYYYMMDD>`. The log names it on a
  `Using miles image:` line. A run may reuse an older image when that day's
  build failed.
- An **image build** (`release-docker-amd-miles-*-nightly.yml`) assembles that
  image from radixark/miles `main` and an sglang branch (`sglang-miles`, or a
  release branch such as `sglang-miles-v0.5.21-final`) with
  `docker/Dockerfile.rocm`. Its log prints `Building from radixark/miles@main
  (<sha>)` (or `Building from miles main @ <sha>`) and, in the sglang checkout
  step, `HEAD is now at <sha>`.

So the `Commit SHA` in the prompt is only the commit the workflow file came
from. Never use it as the code under test or as a regression-window endpoint.

**Read the evidence file first.** The prompt's `Miles evidence:` line points
to `.ci-context/miles/EVIDENCE.md`, prepared by the sglang-ci dashboard from
the job logs and build logs. It gives, per failure: the image and its
radixark/miles + sglang commits, the failing test files and their error, the
traceback frames inside miles / sglang, the run each test first failed and
last passed in, the commit ranges between those two images, and **candidate**
cause and fix commits. The candidates are mechanical (a commit in the window
that changed the test, or a function or line on its traceback). They are
leads, not causes: open each diff and decide whether it really changes the
failing path, and say so either way in Hypothesised Causes. Look past them as
well — model scripts, configs, dependency pins and the Dockerfile do not
appear on a traceback.

**Source trees.** `Miles source:` is radixark/miles checked out at the image's
commit, with full history: use `git -C .ci-context/miles/src log
<base>..<head> -- <path>`, `git -C .ci-context/miles/src show <sha>`. The
current directory is sglang at the image's sglang commit when the
`sglang source:` line says so. Both are read-only like `/workspace/sglang`.

**What failed.** The suite runner prints `Begin (n/N):` then the test's
command, `FAILED: <test> returned exit code N` or `TIMEOUT: <test> after N
seconds` as each test ends, and at the end a `PASSED:` / `FAILED:` summary
followed by `Last output of <test>:` excerpts. Most miles e2e tests are
scripts rather than test functions: put `—` in the Test Function column when
the file is the unit. A test killed by its timeout ends on whatever it last
printed (a health check, a progress bar); report the timeout, not that line.

**When it started.** Compare at the test-file level as for every workflow,
but think in images, not commits: the regression window is between the last
image the test passed on and the first image it failed on. Two runs on the
same image tag (or on images built from the same commits) with different
outcomes mean the code did not change — say flaky / runner / data, not
regression. If the evidence file has no row for this job, or the dashboard
payload was unavailable: take the image tag from the log, find that day's
successful run of the matching `release-docker-amd-miles-*-nightly.yml`
(same ROCm track and GPU), and read the commits from its log lines above.

**Image availability.** The evidence file's **Docker Hub** section is what
Docker Hub said about this job's image track (`rocm/sgl-dev:miles-<track>-<gpu>-*`)
when the analysis started:

- the tag the job ran on — its push time and digest, and whether it was
  **re-pushed after the job started** (then the tag names a different image
  than the job pulled: compare the log's `Digest:` line);
- the tag for the run's own day — if it is **not on Docker Hub**, that day's
  build did not publish and the nightly ran on the older image, so the
  regression window ends at that older image, not at the day's commits; for a
  build job, it means the build did not publish its image;
- the newest tags of the track, which show how long it has gone without a
  new image.

Use these as facts in Regression Status and Status (persistence, blocked
work). When a section line says Docker Hub did not answer, query it yourself
with the curl commands in the Ground Rules.

**Images built from a fork.** A manual `workflow_dispatch` build can take
miles from a fork (`Building from <owner>/miles@<branch> (<sha>)`), and its
image replaces that day's nightly tag. The evidence file says so under
**Code under test** and on the commit range. Its commit is checked out like
any other: GitHub serves a fork's commits through radixark/miles, and the
range then includes the fork's own commits. Name the fork and branch in
Commit Info and in Regression Status. A failure first seen on a fork-built
image is **not** a radixark/miles regression until it reproduces on an image
built from main; say which of the fork's own commits could explain it.

**Why.** Enumerate both windows: radixark/miles in `.ci-context/miles/src`,
and sglang through the compare API on the image's sglang branch
(`https://api.github.com/repos/sgl-project/sglang/compare/<base>...<head>`).
For an image build failure, also check `docker/Dockerfile.rocm` and the
workflow file in sglang between the last good and the first failed build, and
cite the failing Dockerfile step (`#<n> [sglang k/N] RUN ...`).

**In-flight fixes.** Search radixark/miles as well as sglang:
`https://api.github.com/search/issues?q=repo:radixark/miles+is:pr+is:open+<keyword>`.
A later commit on miles `main` that changes the failing code counts as a
landed fix candidate; say whether the newest image already contains it.

**Commit Info for these workflows** lists the code under test:

```
### Commit Info
- **image**: `rocm/sgl-dev:miles-rocm10-mi35x-20261015`
- **radixark/miles**: [`<sha>`](https://github.com/radixark/miles/commit/<sha>) (branch `main`; for a fork build: fork `<owner>/miles`, branch `<branch>`)
- **sglang**: [`<sha>`](https://github.com/sgl-project/sglang/commit/<sha>) (branch `sglang-miles`)
- **workflow commit**: [`<head_sha>`](https://github.com/sgl-project/sglang/commit/<head_sha>) (where the workflow file came from; not tested)
- **aiter**: per the rule below (`N/A` when the log has no `[CI-AITER-CHECK]` markers)
```

### Lost runner (REQUIRED when the prompt says `Runner lost: yes`)

The job failed because its self-hosted runner went away mid-job ("The
self-hosted runner lost communication with the server"), not because a step
failed. The harness detected it from the step list (the job failed while no
step did) and lists the facts under the `Runner lost: yes` line: the runner,
the step that was running, GitHub's annotation, and the other jobs in the same
run that lost their runner. Cite those lines as `[FACT]`.

- **There is usually no log.** GitHub keeps none for most of these jobs (the
  log URL answers 404 `BlobNotFound`). Try it once. If one exists it stops
  where the runner vanished; read its end for the test that was running.
  Do not spend turns retrying, and never write "the log shows no error" as
  if the job had finished.
- **No test failed.** The Failed Tests table gets one non-test row:
  `runner lost during <step>` in Test File, `N/A` in Test Function, the
  annotation (or `no annotation`) as Error. If the cut-off log shows a test
  was running, name it as **running when lost**, not as failed.
- **Failure Cluster**: exactly `Self-hosted runner lost communication (<runs-on label>)`,
  e.g. `Self-hosted runner lost communication (linux-sglang-mi35x-di)`, so the
  Daily Cross-Workflow Summary groups every lost job of a pool into one cluster.
- **Regression Status**: `Infrastructure issue`. The history check is at the
  **job** level here, since there is no failing test: in the 15 comparable runs,
  did this same job (same shard) lose its runner, fail a test, or pass?

Then tell the two patterns apart, because their fixes are different:

| Pattern | Evidence | Top hypothesis |
|---|---|---|
| **Host outage** | Several *different* jobs lost their runner on the same runner(s) inside one window (the "Other jobs" list); the same jobs pass in other runs | `[HIGH]` the runner host or its network dropped during `<window>`. Triage: the runner owners check that host for the window (OOM kill, reboot, network). No sglang change is suspected. |
| **Job takes its runner down** | The *same* job/shard loses its runner across runs, on different runners (ARC pods are new for each job), while other jobs on the pool are fine | `[MEDIUM]` something in this job exhausts the runner (host memory, disk, a killed pod). Find which test was running at the loss: compare the elapsed time at the loss with per-test timings in a passing run of the same shard. Then look for the run in which the job started losing its runner, and the commits to that shard's tests or to the code they exercise in the window before it. |

If neither fits (one job, once), say so: `[LOW]` a one-off runner loss; re-run.
Never blame an sglang commit for a host outage.

### Commit info extraction (REQUIRED)

Every analysis **must** begin with a `### Commit Info` section. The prompt will supply a `Commit SHA` — use it as the sglang commit (except for Miles workflows: see **Miles workflows** above).

For the **aiter commit**, download the job log and search for `[CI-AITER-CHECK]` markers emitted by `amd_ci_install_dependency.sh`. Determine the actual aiter version that ran using this priority:

1. `AITER_COMMIT_OVERRIDE=<value>` — if present, this override was used.
2. `Dev/patched version detected: <version>` — image had a dev build; it was preserved.
3. `Dockerfile expects AITER_COMMIT=<value>` — the default. Used when the log says `AITER version matches` or `Version mismatch` (rebuilt from Dockerfile value).

If no `[CI-AITER-CHECK]` markers exist in the log (e.g. docker-build workflows), report aiter as `N/A`.

### AITER analysis (when relevant)

[aiter](https://github.com/ROCm/aiter) is AMD's attention/inference kernel library that sglang depends on. Failures **can** be caused by aiter changes, by sglang changes, or by their interaction — never assume aiter-first. This is especially relevant for:

- **AMD AITER Scout** workflow runs (which test sglang against a specific aiter commit)
- Any failure involving MLA kernels, FlashAttention, fused attention, or custom Triton kernels
- Errors like `hipErrorNoBinaryForGpu`, kernel launch failures, or numerical mismatches in attention output

**⚠️ Two-variable trap (READ THIS BEFORE BLAMING AITER).** AITER Scout is a **2×2 experiment**: BOTH the sglang commit AND the aiter commit change between scout runs. The scout cron runs every Mon/Thu, so a typical scout-to-scout interval is 3-4 days, during which **dozens to hundreds of sglang commits** typically merge alongside the aiter delta (e.g. AITER Scout #92 had 33 aiter commits AND 110 sglang commits in the same window). Common pitfalls to avoid:

1. **Reading only the HEAD sglang commit message is forbidden.** A HEAD message like "CI: fix lint" tells you nothing about the 109 commits behind it. You MUST enumerate the full sglang delta:
   ```
   git log --oneline <last_scout_pass_sglang_sha>..<this_scout_sglang_sha>
   ```
   Then grep for commits touching code paths that match the failure symptom (attention, MoE, quantization, graph capture, kernel selection, model forward pass, `apply_qk_norm`, `.view`, `.reshape`, `pcg`, `inductor`, etc.).

2. **Log-line proximity is NOT causation.** When a GPU memory access fault prints right after `[aiter] ... using torch solution:0`, that is **not** evidence aiter caused the fault — aiter just happens to print one log line before the next forward-pass operation that crashes. The actual crash may be in sglang code called immediately after the aiter call. Always read the stack trace, not just the surrounding log lines.

3. **The aiter delta has 30+ commits; assuming any single one is "the" cause is speculation, not analysis.** Treat aiter commit hypotheses as `[LOW]` confidence by default, and only promote to `[MEDIUM]+` after the Baseline A/B check below confirms aiter-causation.

When the aiter commit differs from the Dockerfile default (i.e., an override or dev build), investigate what changed in aiter:

1. **Get the aiter commit history** via GitHub API:
   ```
   curl -s -H "Authorization: token $GH_PAT" \
     "https://api.github.com/repos/ROCm/aiter/commits?sha=<aiter_sha>&per_page=10"
   ```

2. **Compare with the previous aiter version** (Dockerfile default vs override):
   ```
   curl -s -H "Authorization: token $GH_PAT" \
     "https://api.github.com/repos/ROCm/aiter/compare/<old_sha>...<new_sha>"
   ```

3. **Read a specific aiter commit's diff**:
   ```
   curl -s -H "Authorization: token $GH_PAT" \
     "https://api.github.com/repos/ROCm/aiter/commits/<sha>"
   ```

In the Root Cause Analysis, clearly state whether the failure is caused by:
- **sglang code change** — cite the sglang commit
- **aiter code change** — cite the aiter commit and what it changed
- **Interaction between both** — cite both

**📋 Past failure mode to learn from — AITER Scout #92 misattribution.** [Run 24531896433](https://github.com/sgl-project/sglang/actions/runs/24531896433) (Apr 16 2026) attributed ~8 "GPU memory access fault during CUDA graph capture" failures (across `test_llada2_mini`, `test_lora_load_from_tensor`, `test_priority_metrics`, `test_metrics`, `test_eagle_dp_attention`, `test_reasoning`, `test_deterministic`, `test_reward_models`) to aiter commits ([`d098ae5a`](https://github.com/ROCm/aiter/commit/d098ae5a) "Revert CAR graph capture err", [`016ead3728`](https://github.com/ROCm/aiter/commit/016ead3728) "SynchronizedCache"). **The actual root cause was sglang [#21734](https://github.com/sgl-project/sglang/pull/21734) ([`6da3aba`](https://github.com/sgl-project/sglang/commit/6da3aba) `apply_qk_norm` `.reshape→.view`)**, merged Apr 14 between the last green scout and the failing scout. The aiter `using torch solution:0` log line printed immediately before each fault was a red herring — `apply_qk_norm` is called next in the forward pass for qwen3/qwen3_moe/llada2/glm4_moe/14 models. Baseline run [24527146590](https://github.com/sgl-project/sglang/actions/runs/24527146590) (same sglang SHA, **NO aiter override** — `AITER_COMMIT_OVERRIDE=` empty) had the **identical fault on the same test files**, conclusively exonerating aiter. The misattribution happened because: (a) the bot read only the HEAD sglang message ("CI: fix lint") and concluded sglang was effectively unchanged across the 110-commit delta; (b) the Baseline A/B check (next subsection) was either skipped or relied on the wrong baseline; (c) aiter blame was written before the A/B check completed. The fix [#23159](https://github.com/sgl-project/sglang/pull/23159) is a one-line revert. Lesson: **never write aiter Hypothesised Causes until the A/B check is done, and never dismiss the sglang delta without enumerating it.**

### Baseline A/B check (REQUIRED for `amd-aiter-scout.yml`)

The **AMD AITER Scout** workflow calls the regular nightly and PR-test workflows but forces an aiter rebuild via `AITER_COMMIT_OVERRIDE`. Every job in a scout run has a sister job in one of the regular workflows that runs with the Dockerfile default aiter. If the same test fails in both, the failure is **pre-existing in sglang** and MUST NOT be attributed to aiter.

**HARD STOP**: Do NOT write a Hypothesised Causes section, list any aiter commit as suspicious, or set Failure Origin until the A/B check below is complete. The check has four possible outcomes; only ONE permits blaming aiter:

| Sister baseline outcome | Failure Origin | What goes in Hypothesised Causes |
|---|---|---|
| **Same** test file + function fails with **same** symptom | `pre-existing (sglang)` | sglang commits ONLY (from the sglang delta enumeration in step 6). **Zero aiter commits**, period. |
| Test **passes** in sister baseline (or sister baseline is much greener) | `aiter-caused` | aiter commits from the delta range (per AITER analysis above). May also list sglang interaction commits if the failure depends on a specific sglang code path. |
| Sister baseline has **different** failure on the same test (e.g. flake or skip) | `unclear` | Both sglang and aiter commits in scope; flag the sister flake explicitly and treat all hypotheses as `[LOW]`. |
| Sister baseline has **no comparable run** in the queryable window | `unclear` | Both in scope; state the lookback window you searched and why no baseline was usable. |

You MUST perform this A/B check for every scout failure before writing the Root Cause Analysis or Suspicious Commits.

**1. Derive the sister workflow and sister job name** from the scout job name prefix:

| Scout job name | Sister workflow file | Sister job name |
|----------------|----------------------|-----------------|
| `call-nightly-amd / <name>` | `nightly-test-amd.yml` | `<name>` |
| `call-pr-test-amd / <name>` | `pr-test-amd.yml` | `<name>` |
| `call-nightly-amd-rocm720 / <name>` (legacy run only) | `nightly-test-amd-rocm720.yml` | `<name>` |
| `call-pr-test-amd-rocm720 / <name>` (legacy run only) | `pr-test-amd-rocm720.yml` | `<name>` |

The unified workflows expand ROCm 10, ROCm 7.2.4, and ROCm 7.2 inside the
same run. The `<name>` therefore contains a versioned matrix suffix such as
`(rocm720, linux-mi300-1gpu-sglang, 11)` — keep it verbatim when matching
sister jobs. The legacy rows are retained only for explicit analysis of old
Scout run URLs; they are not current monitor defaults.

**2. List the sister workflow's recent scheduled runs** (filter to `event=schedule` and same branch so you never pick up another scout's `workflow_call` run):
```
curl -s -H "Authorization: token $GH_PAT" \
  "https://api.github.com/repos/sgl-project/sglang/actions/workflows/<sister>.yml/runs?event=schedule&branch=main&per_page=10"
```

Pick the most recent completed run whose `head_sha` is closest to (but not after) the scout's `head_sha`. Fall back to the most recent completed schedule run if nothing earlier exists.

**3. Find the sister job in that run** and download its log:
```
curl -s -H "Authorization: token $GH_PAT" \
  "https://api.github.com/repos/sgl-project/sglang/actions/runs/<baseline_run_id>/jobs?per_page=100"
curl -sL -H "Authorization: token $GH_PAT" \
  "https://api.github.com/repos/sgl-project/sglang/actions/jobs/<baseline_job_id>/logs"
```

**4. Check whether the SAME test file + test function** that failed in the scout also failed in the sister job's log. The comparison MUST be at the test file + function level, not the job's overall pass/fail.

**5. Classify each scout failure**:
- **`pre-existing (sglang)`** — same test file + function fails in the sister job's most recent scheduled run. The failure is NOT caused by the aiter override; do NOT list aiter commits as suspicious.
- **`aiter-caused`** — test fails in the scout but passes (or doesn't appear as a failure) in the sister baseline. Investigate the aiter commit range per the AITER analysis subsection above.
- **`unclear`** — the sister baseline is unavailable (job skipped, workflow changed, no comparable run in the last 48 h, sister job didn't reach this test, etc.). Explain why in the Failure Origin field.

**6. If Origin is `pre-existing (sglang)`** — investigate the sglang side BEFORE writing Hypothesised Causes:
- **Enumerate the full sglang delta** (do NOT shortcut by reading only HEAD's commit message):
  ```
  git log --oneline <last_scout_pass_sglang_sha>..<this_scout_sglang_sha>
  ```
  Expect 50-200 commits. Save the list and grep it for keywords matching the failure symptom: stack-trace file paths, function names, error keywords (e.g. `graph capture`, `qk_norm`, `moe`, `fp8`, `pcg`, `inductor`, `view`, `reshape`, `cuda graph`, `attention`), or model class names appearing in the failing test.
- **Narrow the regression window using sister-baseline history.** Find the last sister run where the failing test passed (`pass_sha`) and the first sister run where it failed (`fail_sha`). The true regression window is `pass_sha..fail_sha`, often much smaller than the scout-to-scout sglang delta. Use the sister workflow's history (filter `event=schedule`, same branch) to find these SHAs.
- For each suspicious sglang commit: read its diff (`git show <sha>` or `curl https://api.github.com/repos/sgl-project/sglang/commits/<sha>`) and check whether the modified code is reachable from the failing test's stack trace. Cite the most suspicious sglang commits with confidence labels.
- **Cite zero aiter commits.** The failure is reproducible without the aiter override, so the aiter delta is irrelevant to root cause. (You may still mention aiter as a confounder if the symptom involves an aiter call site, but only with explicit `[LOW]` confidence and disconfirming evidence noting the baseline reproduces without aiter.)
- **Search for in-flight sglang fix PRs** with the failing test name and likely-culprit keywords; an in-flight revert/fix may already exist.

**7. If Origin is `aiter-caused`** — investigate the aiter side per the AITER analysis subsection above. You may also examine sglang commits in the same window if there is reason to believe the failure is an interaction (e.g. sglang added a new aiter API call path, or the failure only triggers on a specific sglang code path that the sister baseline doesn't exercise).

### Output format

```
### Commit Info
- **sglang**: `<head_sha>`
- **aiter**: `<actual_aiter_commit>` (source: Dockerfile default / override / dev image)

### Failed Tests
| Test File | Test Function | Error | Log |
|-----------|--------------|-------|-----|
| `test/srt/test_mla.py` | `test_mla_correctness` | `AssertionError: rtol=0.01` | [Log](link) |
(List ALL failed tests. If the failure is not a test — e.g. build error, server crash — describe it here instead.)

### Failure Summary
(What failed, 2-3 sentences of FACTS only. Reference the specific test files above. Do NOT assert a cause here.)

### Regression Status
New regression / Known recurring failure / Never-passed (no green run found) / Flaky test / Infrastructure issue

Recent history of **`<failing_test_file>`** (`<failing_test_function>`) in job `<job_name>` (most recent 15 comparable completed scheduled runs: same branch, `event=schedule`, completed runs only):
| Date | Run | Job | Test File Status | Failed Function | Error |
|------|-----|-----|------------------|-----------------|-------|
| Apr 15 | [run](link) | `job_name` | ❌ Failed | `test_mla_correctness` | `AssertionError: rtol` |
| Apr 14 | [run](link) | `job_name` | ✅ Passed | — | — |
| Apr 13 | [run](link) | `job_name` | ✅ Passed | — | — |
(The Job column is for human reference only. The regression verdict is based on Test File Status, NOT the job's overall pass/fail.
 First observed failure date for this test file, last known passing date for this test file.
 If no passing run is found in the 15-run query window, mark as `Never-passed` and state the lookback range explicitly.
 In-progress runs MUST be excluded or labelled `[IN-FLIGHT]`.)

### Failure Origin (REQUIRED for `amd-aiter-scout.yml`, omit otherwise)
`aiter-caused` | `pre-existing (sglang)` | `unclear` — one line per failing test.

| Test File | Test Function | Origin | Baseline Run | Baseline Status |
|-----------|---------------|--------|--------------|-----------------|
| `test/srt/test_mla.py` | `test_mla_correctness` | `aiter-caused` | [run](link) | ✅ Passed (sister job, default aiter) |
| `test/srt/test_lora.py` | `test_lora_logprob` | `pre-existing (sglang)` | [run](link) | ❌ Same failure in sister job |

(Cite the sister workflow's latest scheduled run as the baseline, per the Baseline A/B check subsection above.
 If Origin is `pre-existing (sglang)`, the Hypothesised Causes section below MUST NOT list aiter commits.)

### Failure Cluster
(Pick a SHORT noun phrase that names the failure pattern, e.g. "GPU memory access fault during model warmup", "ImportError: torchao version", "RCCL allreduce hang on 8-GPU runner". This is a **symptom-level grouping**, not a root cause assertion. The Daily Cross-Workflow Summary uses these names to deduplicate failures across workflows.)

### Facts (verified observations, no interpretation)
- Direct log evidence: error messages, stack traces, exit codes (verbatim from log)
- Environment facts: aiter/peft/torch versions in passing vs failing runs (cite `[CI-AITER-CHECK]` markers + log diff)
- Reproducibility: how many runs / how many jobs share this exact failure pattern
- Code paths involved: which files appear in the stack trace (verified by reading the log, not guessed)

Each fact must be verifiable by re-reading the linked log. Do NOT mix in interpretation here.

### Hypothesised Causes (with confidence, with disconfirming evidence)
For each candidate cause, provide:
- `[<CONFIDENCE>]` Hypothesis statement
  - **Supporting evidence**: code/log/timing pointers
  - **Disconfirming evidence**: facts that weaken this hypothesis (REQUIRED — write "(none found)" only if you genuinely searched)
  - **How to verify**: concrete next step (bisect, revert + rerun, local repro command)

Example:
- `[MEDIUM]` [#23001](url) ([`6760c790b`](url)) modifies `radix_attention.py:127` (`out_cache_loc` slicing)
  - **Supporting**: lands in regression window 2026-04-14; the modified line appears in the failing stack trace
  - **Disconfirming**: `test_lora_load_from_tensor` was Never-passed before this commit too, so this commit cannot fully explain that test
  - **How to verify**: `git revert 6760c790b` on a branch, run `test_llada2_mini_amd.py` on AMD MI325 in container `amd-bot-runner`
- `[LOW]` Aiter version `v0.1.12.post1` mismatch
  - **Supporting**: only AMD CI affected (NVIDIA passes)
  - **Disconfirming**: aiter version is unchanged across passing & failing runs (confirmed via `[CI-AITER-CHECK]` markers)
  - **How to verify**: not actionable until other hypotheses are ruled out

If no hypothesis reaches MEDIUM confidence, say so explicitly: `No hypothesis above LOW confidence; needs git bisect between <pass_sha>..<fail_sha>.`

For `amd-aiter-scout.yml` runs, only include aiter commits when the Failure Origin is `aiter-caused`.

### In-flight Fix Check
Search for matching open PRs before recommending fixes:
- `In-flight fix: ✅ [#<num>](url) (open since <date>) — <one-line summary>` — ping reviewers, do NOT open a duplicate
- `In-flight fix: ❌ none found — needs new PR or investigation`

Use 1-3 keywords from the failing test name, error message, or library name. Cite the API call you ran.

### Suggested Triage Steps
(Concrete investigation steps the maintainer can run, NOT directives. Bullet points.)
- "Bisect commits in [`pass_sha..fail_sha`](compare-url)" — for narrowing down regressions
- "Revert [`<commit>`](url) on a branch, re-run `<test>` in container `<image>`" — for verifying a hypothesis
- "If hypothesis confirmed, fix in `<file>` by `<approach>`" — for fix direction (still hypothetical)
- "If hypothesis disproven, the test may be a Never-passed AMD port issue — disable on AMD until separately fixed"

Do NOT write "Revert X" or "Pin Y<Z" as if they were the final answer. The maintainer decides after triage.

### Status (factual, no priority assignment)
- **Persistence**: e.g. "Failing for 5 days (since 2026-04-14 06:41 UTC), every completed run since"
- **Scope**: e.g. "6 jobs across 2 workflows and 3 ROCm versions (pr-test-amd, nightly-test-amd)"
- **Blocked work**: e.g. "Blocks LoRA + DLLM CI signal on AMD"
- **In-flight fix**: copy the line from the In-flight Fix Check above

Do NOT include a `Priority: Critical/High/Medium/Low` line. Engineers decide priority from the facts above.
```
