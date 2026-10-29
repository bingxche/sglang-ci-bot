# sglang-ci-bot

Automated CI monitoring and PR review bot for [sglang](https://github.com/sgl-project/sglang), powered by Claude via AMD LLM Gateway.

All AI behavior — both agent mode and API mode — is defined under [`agent/`](agent/): the Ground Rules every task follows in [`agent/CLAUDE.md`](agent/CLAUDE.md), one methodology file per task in [`agent/tasks/`](agent/tasks/), and the API-mode templates in [`agent/api-prompts.md`](agent/api-prompts.md). An agent gets `CLAUDE.md` plus its own task's file, appended to its system prompt; API mode loads templates at runtime via `load_prompt_template()`. To change any AI behavior, edit only these files.

All public-facing comments are posted under the dedicated **[amd-bot](https://github.com/amd-bot)** GitHub account.

---

## Features

| Feature | Script | Trigger | What it does |
|---------|--------|---------|--------------|
| Cron CI Monitor | `ensure_daily_issue.py` (prepare step) + `monitor_ci.py` (per-workflow matrix) | Runner-1 dispatches `ci-monitor.yml` hourly | `ci-monitor.yml` first runs a `prepare` job that calls `ensure_daily_issue.py` (idempotently creates today's daily issue with the Daily Cross-Workflow Summary placeholder seeded in its body), then fans out a `monitor` matrix job — **max-parallel 8, one entry per workflow file** in `MONITORED_WORKFLOWS`. Each matrix job analyses its workflow's failures with historical comparison and regression detection, then posts/PATCHes a per-workflow comment on the daily issue. Gate/finish jobs are automatically skipped. For `nightly-amd-mi355x-disagg.yml`, per-job analysis pre-downloads the run's `mi355x-*` artifacts and unpacks bundled prefill/decode/router logs into `.ci-context/mi355x-artifacts` before invoking the agent. Reports group failures into **symptom clusters** with **confidence-labeled hypotheses** rather than asserted root causes. After its workflow has any new failure analysed, the matrix job auto-invokes `daily_cross_workflow_summary.build_and_publish_summary()` to refresh the Daily Cross-Workflow Summary pinned in the issue body. |
| Daily Cross-Workflow Summary | `daily_cross_workflow_summary.py` | Auto-invoked by each `monitor_ci.py` matrix job that produced new failures (gated by `BUILD_DAILY_SUMMARY` env, default enabled); also CLI | Aggregates per-job analyses from ALL monitored workflows into a single rolling **Daily Cross-Workflow Summary** **pinned in the daily issue's body** (above all per-workflow comments) between `<!-- daily-cross-workflow-summary:start -->` / `<!-- daily-cross-workflow-summary:end -->` placeholder markers. Deduplicates symptom clusters across workflows (same cluster spanning `pr-test-amd` + `nightly-test-amd` is one entry, not two). PATCHes the issue body in place, replacing only the content between the placeholders (matching the legacy `ci-monitor-daily-status-board` markers too, so older issues migrate in place). Legacy summary *comments* from before this move are auto-deleted by `_cleanup_legacy_summary_comments`. |
| Failure Trackers (per workflow) | `failure_tracker.py` | A `finalize` job in `ci-monitor.yml` (`needs: monitor`), once per hourly tick after the whole matrix completes | Maintains **one long-lived issue per tracked workflow on the upstream `sgl-project/sglang` repo** (`[Failure Tracker] <workflow>`), each a persistent fact record of every test failure that workflow has shown — one unified, transparent place to see every AMD CI failure and how long it has been red. **Content** (which tests failed, error, cluster, regression status) is found by a small, dedicated agent task (`Task: Failure Tracker Data`) reading the SAME per-job analyses the daily report is built from — kept consistent with the daily report, and decoupled from the giant Daily Cross-Workflow Summary prose (the earlier "append JSON to the summary output" approach was observed to silently drop the block). A deterministic fallback parses the per-job `### Failed Tests` tables if the agent is unavailable. **State** (each failure's `first_seen` date, duration, dedup-by-test) is owned by deterministic Python — a hidden JSON blob in the issue body, NEVER recomputed — so a test red for months keeps an accurate "Broken since" date independent of the daily report's short lookback or GitHub's log retention. Config-driven via `TRACKED_WORKFLOWS`: add a workflow → it gets its own ledger issue. |
| Suspect PR comments | `suspect_pr_comments.py` | The `finalize` job in `ci-monitor.yml`, once per hourly tick | Reads the suspect PRs the Daily Cross-Workflow Summary names for each cluster, checks them in code (FACT/HIGH confidence, ≥2 failed jobs, PR merged in the last 14 days, commit verified between the last passing and first failing commit), and comments on the merged sglang PR; when a fix PR is open, the comment asks the PR's author to review it. **Dry run by default**: records what it would post in one comment on the daily issue until the `SUSPECT_PR_COMMENTS` repository variable is set to `post`. See [Suspect PR comments](#suspect-pr-comments). |
| Notion Staging (daily) | `stage_notion.py` | Independent `notion-staging.yml` schedule at **20:00 Asia/Shanghai daily**; manual `dry-run` / `write` | Uses the existing Claude Code agent to canonicalize failures from recent Daily Reports, then applies a deterministic two-completed-run/no-later-pass gate. Analysis and writing are separate steps: the agent never receives Notion credentials. The sync phase reads the official and isolated staging data sources, skips known/staged matches, appends only new rows to `SGLang AMD CI Staging Errors` with a blank `Status`, and verifies both the new pages and that the official data source is unchanged. |
| On-Demand Analysis | `analyze_url.py` | `workflow_dispatch` (Actions tab) | Paste a GitHub Actions run or job URL, bot creates an issue with analysis results. Supports both run URLs (all failed jobs) and single job URLs. |
| PR Code Review | `review_pr.py` + `review_findings.py` | `@amd-bot review` or manual | Reviewer agents read the PR in parallel, one per angle (correctness, AMD/ROCm, contracts, tests and conventions; one reviewer for small PRs), against the sglang maintainers' own `.claude/rules` and `.agents/skills`. Code checks each finding's location and drops repeats from earlier rounds; independent verifier agents vote, and only findings 2 of 3 confirm are posted, as one PR review with inline comments tagged Important / Nit / Pre-existing. It never approves. See [PR Review](#pr-review). |
| CI Status Check | `check_ci_for_pr.py` | `@amd-bot ci-status` or manual | Checks all CI for a PR, separates CI completeness from executed failure attribution, determines if executed failures are PR-related, and warns when required downstream jobs were fast-fail skipped or when the AMD test covering a changed code path did not run |
| Comment Watcher | `watch_comments.py` | Daemon (15s poll) + Cron (5min fallback) | Polls sglang PRs for `@amd-bot` commands, dispatches workflows |
| Coverage Exclusion Audit (weekly) | `upstream_ci/coverage_audit/audit.py` in [ROCm/sglang-ci](https://github.com/ROCm/sglang-ci) | `coverage-audit.yml` schedule, **Mondays 16:07 UTC**; manual dry run | Keeps the AMD-vs-NVIDIA coverage number on ROCm/sglang-ci's `/coverage` page honest: finds NVIDIA tests that newly joined its "NVIDIA tests not run on AMD" gap list, has Claude Code decide which can never run on ROCm, and opens and merges a ROCm/sglang-ci PR adding those to `AMD_EXCLUSION_REASONS`. It runs here only because these runners can reach the AMD LLM Gateway; the code, prompt and ledger live in ROCm/sglang-ci. The agent gets no token and no shell (`--tools Read,Grep,Glob --restricted`, under `env -i`, in the job's temp directory), and its answer is validated before any step writes. See [Coverage exclusion audit](#coverage-exclusion-audit). |

### Supported commands

Comment on any sglang PR:

| Command | Action |
|---------|--------|
| `@amd-bot review` | Full code review of the PR |
| `@amd-bot review-focus <areas>` | Focused review on specific areas |
| `@amd-bot ci-status` | Check and analyze CI status |
| `@amd-bot help` | Show available commands |

`@amd-bot` must start its own line, and quote-reply lines (starting with `>`) are ignored. Mentioning the bot inside a sentence — or quoting someone else's command when you reply to them — therefore does not run anything, which is what previously caused a quoted `@amd-bot ci-status` to be executed a second time.

Hyphenated commands also accept a space instead of the hyphen, so `@amd-bot ci status` and `@amd-bot review focus <areas>` work too. A mention with an unrecognized command (for example `@amd-bot ci`) gets the help comment as a reply instead of being silently dropped.

### Authorized users

Only users listed in `AUTHORIZED_USERS` in `watch_comments.py` can trigger commands:

```python
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw", "1am9trash", "sogalin", "Kangyan-Zhou", "Fridge003", "BowenBao", "ColinZ22", "fxmarty-amd", "hubertlu-tw", "RolaoDenthu", "Duyi-Wang", "amd-danli103", "akao-amd", "jonahbernard", "At1a8", "chuyeh", "mqhc2020", "chien-an-chen", "yuychang", "jiaryang", "Emmanuel0612", "aditi-amd"]
```

---

## End-to-end execution flow

There are **three independent loops** running concurrently. Each is triggered differently and handles a different responsibility. They share state only via GitHub (issues, comments, reactions) — never via local disk.

### Loop 1 — CI monitoring (hourly)

```
runner-1 entrypoint.sh (sleep 3600) ─┐
                                    ▼
              POST .../actions/workflows/ci-monitor.yml/dispatches
                                    │
                                    ▼
                        ci-monitor.yml dispatched
                                    │
                  ┌─────────────────┴─────────────────┐
                  │  prepare job (1 step)             │
                  │  - resolve workflow list          │
                  │  - ensure_daily_issue.py          │
                  │    (creates today's issue if      │
                  │     missing, with summary         │
                  │     placeholder seeded in body)   │
                  └─────────────────┬─────────────────┘
                                    │
                                    ▼ matrix fan-out (max-parallel 8)
        ┌──────────┬──────────┬─────┴────┬──────────┬──────────┬──────────┬──────────┐
        ▼          ▼          ▼          ▼          ▼          ▼          ▼          ▼
  nightly-test  nightly-720  release-   release-   mi355x     amd-aiter  pr-test    pr-test-720
  -amd.yml     .yml         docker-    docker-    disagg.yml  -scout.yml -amd.yml   .yml
                            amd-       amd-720-                           (sched-   (sched-
                            nightly    nightly                            only)     only)

   each matrix job runs:  python scripts/monitor_ci.py --workflows <one>
                                      AGENT_PARALLEL=3
                                      AGENT_TIMEOUT_SECS=1500
                                      AGENT_MAX_TURNS=150
                                    │
                                    ▼
                  monitor_ci.run_oneshot() per workflow:
                  1. ensure_sglang_repo() — clone or fast-forward /workspace/sglang
                  2. get_workflow_runs(event=workflow-specific filter) —
                                            non-success completed + in-progress
                                            runs in the lookback window (default
                                            24h). Most workflows use `schedule`;
                                            AMD AITER Scout also includes manual
                                            `workflow_dispatch` runs.
                  3. for each run, get_failed_jobs() — drop gates, dedup vs
                     <!-- processed_job_ids: ... --> in existing comment
                  4. for each surviving failed job (up to AGENT_PARALLEL=3
                     in parallel):
                        - create_agent_worktree(job_id, head_sha)
                          → /workspace/sglang-wt-<job_id> at the CI commit
                        - for nightly-amd-mi355x-disagg.yml only, download
                          the run's mi355x-* artifacts and unpack bundled
                          prefill/decode/router logs under
                          .ci-context/mi355x-artifacts
                        - for the Miles workflows only, move the worktree to
                          the image's sglang commit, check radixark/miles out
                          at the image's commit under .ci-context/miles/src,
                          and write .ci-context/miles/EVIDENCE.md from the
                          sglang-ci dashboard's Miles cause-fix payload
                        - run claude -p "Task: Job Failure Analysis ..." (cwd=worktree;
                          agent/CLAUDE.md + agent/tasks/job-failure-analysis.md
                          appended to the system prompt)
                        - parse output, append to job_analyses
                  5. if >1 failure, run Task: Cross-Job Summary agent
                     (no API fallback; if the agent fails, the last good
                     summary is kept with a "not refreshed" note and the
                     next tick retries it, even with no new failures)
                  6. render_workflow_comment(...) — split into 60KB parts if
                     needed (Part 1/N, Part 2/N, ... markers)
                  7. PATCH or POST the per-workflow comment on today's issue
                     (a workflow that errors here fails its matrix job)
                  8. if total_reports > 0 AND BUILD_DAILY_SUMMARY != false:
                        → call daily_cross_workflow_summary.build_and_publish_summary()
                                    │
                                    ▼
                  build_and_publish_summary():
                  1. read ALL per-workflow comments on today's issue
                  2. parse_job_analyses_from_comment() to recover structured data
                  3. fetch yesterday's summary (issue body, then legacy comment)
                     for trend / NEW-cluster detection
                  4. write context to .ci-context/per-workflow-analyses.md
                  5. run claude -p "Task: Daily Cross-Workflow Summary ..."
                  6. PATCH the daily issue body, replacing only the content
                     between :start --> and :end --> placeholders
                  7. delete any legacy daily-summary comments left over from
                     before this body-pinning move
```

**Why matrix instead of one big loop?** Earlier the monitor processed all workflows sequentially in one job, which often timed out and lost in-flight data. Matrix fan-out caps each job at one workflow, runs them in parallel, and uses the GitHub Actions runner pool elastically. The trade-off: every matrix job that produces failures **independently** rebuilds the Daily Cross-Workflow Summary, so the summary can be rebuilt up to 8 times per dispatch. This is intentional — last writer wins, and since they all read the same per-workflow comments the result is convergent.

**Why a `prepare` step?** Without it, multiple matrix jobs racing `find_or_create_daily_issue()` would create duplicate issues for the same day. `ensure_daily_issue.py` runs once before fan-out so the issue (and its `:start`/`:end` placeholder block) exists by the time the matrix fires.

After the matrix completes, `finalize` puts the daily issue's report comments in priority order (`order_daily_comments.py`, see [Report order](#report-order)), checks the summary's suspect PRs (`suspect_pr_comments.py`, see [Suspect PR comments](#suspect-pr-comments)), and always refreshes the Failure Trackers. Notion staging runs in its own `notion-staging.yml` workflow, so the hourly monitor has no staging steps and keeps its original execution sequence.

### Loop 2 — PR command dispatch (continuous, two redundant paths)

```
sglang PR comment: "@amd-bot review"
                          │
       ┌──────────────────┴──────────────────┐
       │ daemon path                          │ cron path
       │ runner-1 entrypoint.sh runs:         │ comment-watcher.yml schedule:
       │  watch_comments.py --daemon          │  '*/5 * * * *' →
       │   --poll-interval 15                 │  watch_comments.py
       │   --bot-repo bingxche/sglang-ci-bot  │   --since-hours 1
       └──────────────────┬───────────────────┘
                          ▼
          GET sglang/issues/comments?since=<now-3*poll>
                          │
                          ▼
          for each new comment:
            1. skip if author NOT in AUTHORIZED_USERS
            2. parse_command()  → command + args
            3. skip if not actually a PR (is_pull_request)
            4. has_bot_claimed(comment_id, "rocket") ?
                 - YES → another watcher already grabbed it, skip
                 - NO  → claim it now:
                          add_reaction(comment_id, "rocket")  ← idempotency key
                          add_reaction(comment_id, "eyes")
            5. POST bingxche/sglang-ci-bot/dispatches
                 event_type: pr-review     (review / review-focus)
                 event_type: ci-status     (ci-status)
                 (help → no dispatch; daemon posts help comment directly)
                          │
                          ▼
          repository_dispatch fires the corresponding workflow:
            pr-review.yml         → scripts/review_pr.py <PR>
            ci-status-check.yml   → scripts/check_ci_for_pr.py <PR>
                          │
                          ▼
          on a self-hosted runner:
            - USE_AGENT comes from vars.USE_AGENT (repo Variable)
            - if true: clone sglang, agent_worktree(tag, pr_number=N)
                       → checkout pull/N/head → run claude
            - else:    single-shot Anthropic API call via AMD LLM Gateway
            - post the result as a comment on the sglang PR
              (under amd-bot identity using secrets.GH_PAT)
```

**Why two redundant paths?** The daemon (15s poll) is the fast path; the cron (5min) is the safety net for when runner-1 is restarting or rebuilding. Both use the **same `rocket` reaction** as the cross-process idempotency lock, so they cannot double-dispatch even when they observe the same comment in the same window.

### Loop 3 — On-demand analysis (manual)

```
maintainer pastes URL into Actions tab → analyze-ci.yml workflow_dispatch
                          │
                          ▼
          scripts/analyze_url.py --url <url> --bot-repo ... --use-agent
            1. parse URL → (run_id, optional job_id)
            2. if job URL: analyze just that job
               if run URL: get_failed_jobs() (gates filtered out)
            3. create_github_issue(
                  title="[Analyze] <workflow>.yml run #<id> (<short_sha>)"
               ) on bingxche/sglang-ci-bot
            4. parallel agents (max 2):
                  agent_worktree(job_id, head_sha=run.head_sha)
                  → run claude "Task: Job Failure Analysis ..."
                  → post per-job comment
            5. if >1 jobs: run "Task: Cross-Job Summary"
            6. PATCH the issue body with the rolled-up summary report
```

Concurrency is keyed on `${{ github.run_id }}` so independent dispatches run side-by-side without cancellation.

---

## How Agent Mode Works

When a task is triggered (CI failure analysis, PR review, or CI status check), the Python script:

1. Clones/updates the sglang repo to `/workspace/sglang` (shared git object store)
2. Creates an **isolated git worktree** per agent (e.g. `/workspace/sglang-wt-{job_id}`)
3. For CI analysis: **checks out the exact commit** that was tested in CI (`head_sha`), so the agent reads the correct source code
4. For `nightly-amd-mi355x-disagg.yml` CI analysis: downloads the run's `mi355x-*` artifacts, extracts bundled `*_logs.tar.gz` server logs, and writes a manifest under `.ci-context/mi355x-artifacts/`
   - For the Miles workflows (`MILES_WORKFLOWS` in `utils.py`): the code under test is not the run's commit but the image's, so `prepare_miles_context()` re-checks out the worktree at the image's sglang commit, adds radixark/miles at the image's commit as `.ci-context/miles/src`, and writes `.ci-context/miles/EVIDENCE.md` — the failing tests, traceback, first failure / last pass, commit ranges and candidate commits that the [sglang-ci dashboard](https://rocm.github.io/sglang-ci/miles/) parses from the job and build logs (`MILES_CAUSE_FIX_URL`). It also asks Docker Hub (`check_miles_image()`, no auth) whether the image the job ran on is still the one under its tag, whether the run's day had an image at all, and which days of the track have none. The agent verifies the candidates rather than trusting them; every piece degrades to a note in the evidence file when it is unavailable
5. For PR tasks: **checks out the PR branch** in the worktree
   - For `PR CI Status Check`: builds the evidence pack (`ci_evidence.py`, see [CI Status Check](#ci-status-check)) and re-checks out the worktree at the **commit CI actually tested** (usually a merge commit, not the PR head)
   - For `PR CI Status Check`: writes `.ci-context/ci-runtime-env.md`, the env vars AMD PR CI actually sets (`collect_ci_runtime_env()` reads `docker/rocm.Dockerfile` `ENV` lines, `scripts/ci/amd/amd_ci_start_container.sh`, `scripts/ci/amd/amd_ci_exec.sh`, and `-e` flags in `.github/workflows/pr-test-amd*.yml`), so the agent judges env-gated code paths against CI's real environment instead of the code default
6. Runs `claude -p "<task prompt>" --append-system-prompt "<instructions>" --setting-sources user --dangerously-skip-permissions --disable-slash-commands [--model <m>]` with `cwd=<worktree>`:
   - The instructions are `agent/CLAUDE.md` (Ground Rules) followed by the task's file in `agent/tasks/`, picked by the prompt's `Task:` line (`AGENT_TASKS` in `utils.py`). An unknown task fails before the CLI starts. Earlier versions copied the whole `CLAUDE.md` to `/workspace/CLAUDE.md`; a stale copy there is deleted.
   - `--setting-sources user` loads no settings from the checkout. The worktree is an sglang commit, often a PR's, and a `.claude/settings.json` in it could otherwise register hooks: a `SessionStart` hook runs a shell command as the session starts, before any model call (verified with Claude Code 2.1.156). It also keeps the checkout's own `CLAUDE.md` and `.claude/rules` out of the context; the PR review hands the maintainers' rules to the agents explicitly, from `main`.
   - `--disable-slash-commands`: the sglang checkout ships developer skills the bot must not run.
   - `--model` comes from the task (see [Models per task](#models-per-task)).
   - The agent's env drops `BOT_PAT` and `LLM_GATEWAY_KEY`; if `AGENT_GH_TOKEN` is set, it replaces `GH_PAT` / `GH_TOKEN`, so the agent, which reads PR content anyone can write, only holds a read-only token
7. If `AGENT_ARCHIVE_DIR` is set, copies the prompt and the agent's session log (secrets redacted) there, so a bad report can be traced to what the agent read and ran

### Task dispatch

Agent prompts are **data-only** — they contain a `Task:` line and metadata, but no instructions. All methodology and output format is defined under `agent/`. The `Task:` line picks the file appended after the Ground Rules, and the model:

| Prompt `Task:` line | Instructions (`agent/tasks/`) | Model |
|---------------------|-------------------------------|-------|
| `Task: Job Failure Analysis` | `job-failure-analysis.md` | default |
| `Task: Cross-Job Summary` | `cross-job-summary.md` (one workflow's many jobs, grouped by symptom cluster) | default |
| `Task: Cross-Run Pattern Analysis` | `cross-run-pattern-analysis.md` (one workflow across multiple runs) | default |
| `Task: Daily Cross-Workflow Summary` | `daily-cross-workflow-summary.md` (rendered into the daily issue **body** between `:start --> / :end -->` placeholders, so it appears above all per-workflow comments) | default |
| `Task: Failure Tracker Data` | `failure-tracker-data.md` (compact JSON-only output of today's failures for the tracked workflows; feeds the long-lived upstream per-workflow Failure Trackers) | Sonnet |
| `Task: SGLang AMD CI Staging Candidates` | `staging-candidates.md` | default |
| `Task: PR CI Status Check` | `pr-ci-status-check.md` | default |
| `Task: PR Code Review` | `pr-code-review.md` (one reviewer, one angle) | default |
| `Task: PR Code Review Verification` | `pr-code-review-verify.md` (one verifier's votes) | Sonnet |
| `Task: Suspect PR Verification` | `suspect-pr-verify.md` (one verifier's vote on a suspect PR) | default |

### Models per task

The model follows what a wrong answer costs. Tasks whose output is published, or decides what gets published (analyses, summaries, the CI status verdicts, PR review findings, suspect-PR verification), use the runner's default, `ANTHROPIC_MODEL` (`opus[1m]`). Extraction from existing analyses (Failure Tracker Data) and the PR review's second-opinion votes, three per finding, use Sonnet.

An alias goes through the gateway only when the runner maps it: `ANTHROPIC_DEFAULT_SONNET_MODEL` must be set in `claude.env`, or the task logs a warning and keeps the default model. Override one task with `AGENT_MODEL_<TASK>` (the task name upper-cased, non-alphanumerics as `_`), e.g. `AGENT_MODEL_FAILURE_TRACKER_DATA=opus` or `AGENT_MODEL_PR_CODE_REVIEW_VERIFICATION=Claude-Opus-5.5`.

### Agent capabilities

- **CI failures**: Download logs via GitHub API, identify failed tests at the **test file + function** level, compare with recent **completed** runs, detect regressions, propose hypothesised commits with confidence labels, search for in-flight fix PRs to avoid duplication. For `nightly-amd-mi355x-disagg.yml`, the bot also downloads `mi355x-*` run artifacts and analyzes bundled `bench.log`, `prefill_*.log`, `decode_*.log`, `server_exit_*`, and `bench_exit` files because the job page log alone does not contain the full multi-node failure evidence.
- **Lost runners**: a job whose self-hosted runner went away mid-job ("The self-hosted runner lost communication with the server") fails with no failed step and usually no log. `is_runner_lost()` in `utils.py` spots it from the step list (checked against GitHub's annotation on 1,384 failed AMD jobs: all 22 annotated jobs, plus one GitHub never annotated), and `runner_lost_evidence()` hands the analysis the facts instead: the runner, the step it was in, the annotation, and the other jobs in the run that lost theirs. In agent mode this is a `Runner lost: yes` block in the prompt; in API mode a `## Runner lost` section ahead of the log; in the PR CI-status evidence pack, mode `runner_lost`. The **Lost runner** section of `agent/tasks/job-failure-analysis.md` has the agent report it as infrastructure under one cluster name per runner pool, and tell a host outage (many jobs, one window) from a job that takes its own runner down (the same shard, every run). The same rule drives the "Runner lost" column on the ROCm/sglang-ci Runner Health page.
- **PR reviews**: several reviewers, each from one angle, read full source files, find callers of modified functions, check AMD/ROCm parity, test coverage and the maintainers' conventions; verifiers then try to refute each finding (see [PR Review](#pr-review))
- **Isolation**: Each agent gets its own worktree — parallel agents (`AGENT_PARALLEL=3` per `monitor_ci.py` matrix job; `MAX_PARALLEL_JOBS=2` in `analyze_url.py`) and concurrent tasks cannot interfere with each other

### CI report methodology principles

The Job Failure Analysis / Cross-Job Summary / Cross-Run Pattern Analysis / Daily Cross-Workflow Summary tasks all follow the same evidence-based principles defined in CLAUDE.md's Ground Rules:

- **Symptom clustering, not root cause assertion**: failures are grouped into named **Failure Clusters** (e.g. "GPU memory access fault during model warmup"). The bot does NOT claim a cluster has been "caused by commit X" without verified evidence. Causal claims live in a separate **Hypothesised Causes** section with explicit confidence labels.
- **Confidence labels REQUIRED** for every causal claim: `FACT` / `HIGH` / `MEDIUM` / `LOW` / `SPECULATION` (default `LOW`).
- **Disconfirming evidence surfaced**: every hypothesis lists facts that *weaken* it, not just supporting evidence.
- **Bot does NOT assign Priority**: only states factual `Status` (e.g. "5 days persistent across 6 jobs in 3 workflows"). Engineers decide priority. The legacy `Priority: Critical/High/Medium/Low` field has been removed.
- **In-flight fix lookup REQUIRED**: before recommending any fix, the bot searches sglang's open PRs for matching keywords. Existing PRs are linked instead of duplicated.
- **Only completed runs count in trends**: in-progress / queued runs are excluded or labelled `[IN-FLIGHT]`. Drawing "trend dropped" / "regression candidate" conclusions from in-flight runs is forbidden.
- **Recommendations are triage steps, not directives**: bot output uses "Suggested triage: bisect A..B; if commit X is implicated, try reverting on a branch", NOT "Revert commit X".
- **Test-file granularity preserved**: every cluster table lists `(workflow, job, test_file, test_function)` — never collapsed to job-only level.
- **One way to write a job**: a job is always its GitHub name without the runner label or caller prefix, in backticks (`` `nightly-test-1-gpu-kernel (rocm720)` ``), linked to the job page. Several legs of one job are the backticked name followed by one link per leg labelled with its matrix values. No nicknames (`v4-pro`), and no links labelled only `720` or `(1)`, so searching the page for a job finds every mention of it.
- **Suspected change first**: a hypothesis that blames a change opens with the PR and its commit side by side (`` [#45112](…) ([`693d3333`](…)) moved … ``), and a change being ruled out goes under Disconfirming. Downstream dashboards read that first pair as the suspected cause.

### No API fallback in agent mode

Agent mode (the default) never falls back to a single-shot API call: those answers are too shallow to publish. When the Claude Code CLI is missing or the agent fails, the bot publishes a "The Claude Code agent did not work" notice (`agent_failure_notice()` in `utils.py`, with the error) where the output would have gone, and the job fails:

| Script | Where the notice goes |
|---|---|
| `review_pr.py`, `check_ci_for_pr.py` | The PR comment, in place of the review / CI status |
| `monitor_ci.py` per-workflow report | Cross-job summary failed: the last good summary is kept under a "not refreshed" note and retried next tick. CLI missing: nothing is analysed; the daily summary gets the notice |
| `daily_cross_workflow_summary.py` | Top of the issue-body summary, above the last good summary (`<!-- daily-summary-agent-failed -->`; replaced, not stacked; removed by the next successful build) |
| `analyze_url.py` | The `[Analyze]` issue body, and a comment per job whose agent failed |

The Failure Trackers are unchanged: when their extraction agent fails they parse the per-job `Failed Tests` tables deterministically, with no LLM.

API mode (single-shot Anthropic API calls via AMD LLM Gateway) runs only when chosen explicitly with `USE_AGENT=false` / `--no-use-agent`. It loads prompt templates from the `## API Mode Prompts` section of `CLAUDE.md` via `load_prompt_template()` and requires `LLM_GATEWAY_KEY` and `LLM_GATEWAY_URL`.

### Comment footers

All bot-generated comments include a footer indicating the method used:

| Method | Footer |
|--------|--------|
| Claude Code CLI (agent mode) | *Generated by amd-bot using Claude Code CLI* |
| Claude API (API mode) | *Generated by amd-bot using Claude API* |
| No LLM (e.g. help command) | *Generated by amd-bot* |

---

## Architecture

The bot uses **two GitHub accounts**:

| Account | Role |
|---------|------|
| **bingxche** | Repo owner. Owns `bingxche/sglang-ci-bot`, registers self-hosted runners (requires admin access) |
| **amd-bot** | Bot identity. Posts all public-facing comments and reactions on sglang PRs |

**Token usage:**

| Context | Token | Identity |
|---------|-------|----------|
| GitHub Actions workflows | `secrets.GH_PAT` (amd-bot's PAT) | amd-bot |
| Daemon comment watcher + CI monitor (runner-1) | `BOT_PAT` env var (amd-bot's PAT) | amd-bot |
| Claude Code agent (all containers) | `GH_PAT` env var | amd-bot |
| Runner registration (`entrypoint.sh`) | `GH_PAT` env var (bingxche's PAT) | bingxche |
| Coverage Exclusion Audit (`coverage-audit.yml`) | `secrets.GH_PAT` (amd-bot's PAT), or `secrets.SGLANG_CI_TOKEN` if set | amd-bot (admin collaborator on ROCm/sglang-ci) |

---

## Setup

### Prerequisites

- **bingxche** GitHub account: owner of this bot repo
- **amd-bot** GitHub account: collaborator (write access) on `bingxche/sglang-ci-bot`
- AMD LLM Gateway subscription key and endpoint URL
- Two GitHub PATs:
  - **bingxche's PAT**: `repo` + `workflow` + `admin:repo_hook` scopes (runner registration)
  - **amd-bot's PAT**: `repo` scope (posting comments, dispatching workflows)

### Repository secrets and variables

In `bingxche/sglang-ci-bot` > Settings > Secrets and variables > Actions:

**Secrets:**

| Secret | Value |
|--------|-------|
| `GH_PAT` | amd-bot's GitHub PAT |
| `LLM_GATEWAY_KEY` | AMD LLM Gateway subscription key |
| `LLM_GATEWAY_URL` | AMD LLM Gateway endpoint (e.g. `https://llm-api.amd.com/Anthropic`) |
| `SGLANG_CI_TOKEN` | Optional. Overrides `GH_PAT` for `coverage-audit.yml` with another ROCm/sglang-ci admin's PAT |
| `AGENT_GH_TOKEN` | Recommended. A read-only GitHub token handed to the agents in place of `GH_PAT` (PR review, CI status check, suspect-PR verification), since they read content anyone can write |

**Variables:**

| Variable | Value |
|----------|-------|
| `USE_AGENT` | `true` to enable agent mode in workflow-triggered reviews and CI checks |
| `SUSPECT_PR_COMMENTS` | `dry-run` (default when unset), `post`, or `off`. See [Suspect PR comments](#suspect-pr-comments). |
| `SUSPECT_PR_VERIFY` | `on` (default when unset) or `off`: whether verifier agents must confirm a suspect before it is commented on |
| `COVERAGE_AUDIT_MODEL` | Optional `--model` for `coverage-audit.yml` (default `opus`, resolved through the runner's `ANTHROPIC_DEFAULT_OPUS_MODEL`) |

### Deploy self-hosted runners

`runner/setup.sh` spawns 10 runner containers. Runner-1 runs a comment watcher daemon + a CI monitor dispatch loop (dispatches `ci-monitor.yml` hourly via `workflow_dispatch`). Runners 2-10 are plain job executors.

```bash
bash runner/setup.sh \
  --pat <bingxche-PAT> \
  --bot-pat <amd-bot-PAT> \
  --llm-gateway-key <KEY> \
  --claude-env .secrets/claude.env \
  --use-agent \
  --build
```

Or pull a pre-built image instead of building locally:

```bash
bash runner/setup.sh \
  --pat <bingxche-PAT> \
  --bot-pat <amd-bot-PAT> \
  --llm-gateway-key <KEY> \
  --claude-env .secrets/claude.env \
  --use-agent \
  --image bingxche/sglang-ci-bot-runner:latest
```

**setup.sh options:**

| Option | Description |
|--------|-------------|
| `--pat` | bingxche's PAT (runner registration, requires repo admin) |
| `--bot-pat` | amd-bot's PAT (daemon comment watcher + CI monitor) |
| `--llm-gateway-key` | AMD LLM Gateway key (enables CI monitor dispatch + API mode fallback) |
| `--llm-gateway-url` | AMD LLM Gateway endpoint URL |
| `--claude-env` | Path to env file with all Claude Code variables (e.g. `.secrets/claude.env`) |
| `--use-agent` | Enable Claude Code agent mode |
| `--image` | Pull image from registry instead of building |
| `--build` | Force local build from Dockerfile |
| `--repo` | GitHub repo (default: `bingxche/sglang-ci-bot`) |
| `--count` | Number of runner containers (default: 10) |
| `--name` | Runner name prefix (default: `amd-ci-bot-runner`) |

### Claude Code environment variables

Claude Code requires several environment variables to authenticate with AMD LLM Gateway. Store them in `.secrets/claude.env` (gitignored):

```
ANTHROPIC_API_KEY=dummy
ANTHROPIC_BASE_URL=https://llm-api.amd.com/Anthropic
ANTHROPIC_CUSTOM_HEADERS=Ocp-Apim-Subscription-Key: <your-key>
ANTHROPIC_MODEL=opus[1m]
ANTHROPIC_DEFAULT_OPUS_MODEL=Claude-Opus-5.5
ANTHROPIC_DEFAULT_SONNET_MODEL=Claude-Sonnet-5.5
ANTHROPIC_DEFAULT_HAIKU_MODEL=Claude-Haiku-4.8
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
```

When `--claude-env .secrets/claude.env` is passed to `setup.sh`, all variables are injected into every container via Docker's `--env-file`. If `--claude-env` is not provided but `--llm-gateway-key` is, only `ANTHROPIC_CUSTOM_HEADERS` is injected (other variables must be baked into the Docker image).

### Container isolation

Containers are fully isolated from the host. The only host files mounted are:

- `runner/entrypoint.sh` — read-only bind mount
- `sglang-runner-toolcache-{i}` — Docker named volume for GitHub Actions tool cache

The bot code is cloned by `entrypoint.sh` to `/tmp/bot`. The sglang repo (`/workspace/sglang`) is cloned on-demand by `ensure_sglang_repo()` in `utils.py` when agent mode is invoked. Each agent runs in an isolated git worktree (`/workspace/sglang-wt-{tag}`) created by `create_agent_worktree(tag, head_sha)`, checked out to the exact CI commit being analyzed. Concurrent agents (up to 2 in agent mode) cannot interfere with each other. All analysis happens entirely inside the container.

---

## Disaster recovery / migration to a new host

The bot is fully containerized and stateless — all persistent state lives on GitHub (issues, comments, reactions). Migrating to a new host requires only Docker and the credentials listed below.

### What to bring

| Item | Where to find it |
|------|-----------------|
| bingxche's GitHub PAT | GitHub > Settings > Developer settings > Personal access tokens |
| amd-bot's GitHub PAT | Same, under the amd-bot account |
| `.secrets/claude.env` | Copy from the old host (`sglang-ci-bot/.secrets/claude.env`) |
| LLM Gateway key | Inside `claude.env` (`Ocp-Apim-Subscription-Key` value) |

GitHub Actions repo secrets (`GH_PAT`, `LLM_GATEWAY_KEY`, `LLM_GATEWAY_URL`) and variables (`USE_AGENT`) are stored on GitHub — no migration needed.

### Step-by-step

```bash
# 1. Install Docker (skip if already installed)
curl -fsSL https://get.docker.com | sh

# 2. Clone the bot repo
git clone https://github.com/bingxche/sglang-ci-bot.git
cd sglang-ci-bot

# 3. Copy secrets from old host (or recreate manually)
mkdir -p .secrets
scp old-host:/path/to/sglang-ci-bot/.secrets/claude.env .secrets/claude.env

# 4. Deploy (pulls pre-built image, no build needed)
bash runner/setup.sh \
  --pat <BINGXCHE_PAT> \
  --bot-pat <AMD_BOT_PAT> \
  --llm-gateway-key <LLM_GATEWAY_KEY> \
  --claude-env .secrets/claude.env \
  --use-agent \
  --image bingxche/sglang-ci-bot-runner:latest

# 5. Verify
docker ps | grep amd-ci-bot-runner    # 10 containers running
docker logs -f amd-ci-bot-runner-1    # watcher + CI monitor active
```

### Failover verification

1. **Check containers are running**:
   ```bash
   docker ps | grep amd-ci-bot-runner
   ```
   All 10 containers should show `Up` status.

2. **Check runner logs**:
   ```bash
   # Runner-1: should show watcher daemon + CI monitor started
   docker logs --tail 50 amd-ci-bot-runner-1

   # Any runner: should show "Listening for Jobs"
   docker logs --tail 20 amd-ci-bot-runner-2
   ```

3. **Verify runners accept GitHub Actions jobs**: go to the repo's [Actions tab](https://github.com/bingxche/sglang-ci-bot/actions), manually trigger any workflow (e.g. `Analyze CI`), and confirm a runner picks it up.

---

## Monitored workflows

Configured in `MONITORED_WORKFLOWS` in `monitor_ci.py`:

```
release-docker-amd-rocm10.yml
release-docker-amd-rocm720-nightly.yml
release-docker-amd-gfx1151-nightly.yml
release-docker-amd-miles-rocm10-nightly.yml
release-docker-amd-miles-rocm724-nightly.yml
pr-test-amd.yml
nightly-test-amd.yml
nightly-amd-mi355x-disagg.yml
nightly-test-amd-miles-rocm10.yml
nightly-test-amd-miles-rocm10-mi30x.yml
amd-aiter-scout.yml
```

### Report order

The list is in priority order, and the daily issue shows the reports in the
same order: Docker image builds, PR test, nightly, disagg, Miles, then AITER
Scout. The matrix jobs post each workflow's first comment whenever its
analysis finishes, and GitHub cannot reorder comments, so the `finalize` job
runs `order_daily_comments.py`: it treats the report comments (each
workflow's part 1, overflow parts, then cross-run summary) as slots, oldest
first, and rewrites the bodies into them in this order. People's replies and
stray duplicates stay where they are. Because a body can move to another
comment, `publish_workflow_report()` finds a workflow's comments by their
markers on every run and never reuses a comment ID cached from an earlier run.

Within the `pr-test-amd.yml` report, jobs are grouped by ROCm version
(`ROCM_REPORT_ORDER`): the workflow's default `rocm_version` first, since
that is what PRs run (currently `rocm10`), then `rocm724`, `rocm720`. Update
the list when upstream changes the default. Inside each version, jobs are
listed by stage (`JOB_REPORT_ORDER`): `stage-a`, `stage-b`, `stage-c`, the
kernel jobs (`sgl-kernel-*`, `jit-kernel-*`), `multimodal-*`, then anything
else. Other workflows keep the order their analyses finished in.

The four Miles workflows test [radixark/miles](https://github.com/radixark/miles) in
images built from radixark/miles and an sglang branch (`sglang-miles`). They are
analysed against those images' commits (see [How Agent Mode Works](#how-agent-mode-works)),
appear in the daily report and the Daily Cross-Workflow Summary like any other
workflow, and are left out of Notion staging and the upstream Failure Trackers:
their failures are radixark/miles' to triage.

Upstream now expands ROCm 10, ROCm 7.2.4, and ROCm 7.2 as job matrices inside
the unified `nightly-test-amd.yml` and `pr-test-amd.yml` runs. The workflow
files `nightly-test-amd-rocm720.yml`, `pr-test-amd-rocm720.yml`, and
`release-docker-amd-nightly.yml` are retired from the upstream default branch
and therefore are not dispatched by the monitor.
`release-docker-amd-rocm10.yml` (the ROCm 10 nightly image build, daily at
12:00 UTC), `release-docker-amd-rocm720-nightly.yml` and
`release-docker-amd-gfx1151-nightly.yml` (daily at 14:00 UTC) are active and
monitored. `amd-ci-job-monitor.yml` is also scheduled on AMD runners but is
CI housekeeping, not a build or test, so it is not monitored. The
unified `release-docker-amd.yml` is tag/manual-only and has no scheduled runs,
so it is not part of this schedule-only monitor. Run job listings are fetched
across every GitHub API page, so unified runs with more than 100 jobs are fully
considered for failure analysis and pending counts.

Most monitored workflows are filtered to `event=schedule` only. Manually-dispatched (`workflow_dispatch`) runs and PR-triggered runs are excluded so the daily report is not polluted by ad-hoc / debug runs. If you need on-demand analysis of a specific manual run, use the `analyze-ci.yml` workflow (Actions tab → "Analyze CI" → paste the run/job URL).

**AMD AITER Scout is the intentional exception.** `amd-aiter-scout.yml` is queried without an event filter, so both its scheduled and manually-dispatched scout runs enter the daily CI monitor. `workflow_event_filter()` owns this per-workflow policy; `CROSS_RUN_SUMMARY_WORKFLOWS` separately controls cross-run summaries and includes every monitored workflow. Cross-run synthesis is skipped automatically when fewer than 2 runs are present in the lookback window.

Each workflow report includes the sglang commit (`head_sha`) in the header, and the agent extracts the aiter commit from `[CI-AITER-CHECK]` log markers.

---

## Coverage exclusion audit

`coverage-audit.yml` is a guest here: it maintains the exclusion list behind
ROCm/sglang-ci's `/coverage` page and needs these runners only because the AMD
LLM Gateway does not resolve on GitHub-hosted ones. Everything it runs comes
from a fresh ROCm/sglang-ci clone: `upstream_ci/coverage_audit/audit.py`, the
agent prompt next to it, and the ledger of already-audited files
(`upstream_ci/coverage_audit/reviewed.json` on that repo's `log` branch).
Its documentation lives with that code, in ROCm/sglang-ci's
`sglang-ci-dashboard` skill.

It acts on ROCm/sglang-ci as the owner of the token it uses: `SGLANG_CI_TOKEN`
if set, otherwise `GH_PAT`. That account has to be an **admin** collaborator
there, because that repo's `main` ruleset requires a review only admins can
bypass. The token also has to be authorized for the ROCm org's SSO. In the
ROCm org only org owners can invite an outside collaborator; once one has,
the job's first step accepts the pending invitation with the same token.
When access is missing, that step stops and quotes GitHub's reason. Commits
are authored by the token's owner.

Each run: builds the gap list with `main`'s rules; picks the files the ledger
has not seen, plus kept files upstream changed since; splits them into shards
of 35 and runs one Claude Code agent per shard in parallel; refuses the
answer if it contains a credential; applies the verdicts to
`compare_suites.py`; runs that repo's `upstream_ci/test_*.py`; checks the
before/after gap lists differ by exactly the excluded files and that
`/coverage` still renders; then opens the PR, merges it with `--admin`, and
writes the ledger back.

Isolation, because the agent reads upstream sglang test files anyone can land
text in:

- The job works in `$RUNNER_TEMP`, never the shared workspace that holds this
  repo's checkout and `agent/CLAUDE.md`, and the runner deletes it afterwards.
- Clones are made with a one-off auth header, so no `.git/config` holds a token.
- The agent starts under `env -i` with only `PATH`, a throwaway `HOME` /
  `CLAUDE_CONFIG_DIR`, and the gateway variables, so the container's `GH_PAT`
  and `BOT_PAT` are not in its environment; `--tools Read,Grep,Glob
  --restricted --strict-mcp-config` leaves it those three tools inside the
  temp directory, with no settings files or MCP servers.
- Only validated outputs are uploaded as artifacts (this repo is public), never
  the agent transcripts.

Manual dry run (no PR, ledger untouched): Actions > Coverage Exclusion Audit >
Run workflow with `dry_run` checked, optionally `max_files` to cap cost.

## Concurrency and idempotency

| Workflow | Concurrency group | Behavior |
|----------|-------------------|----------|
| `pr-review.yml` | Per comment ID | Duplicate dispatches cancelled |
| `ci-status-check.yml` | Per comment ID | Duplicate dispatches cancelled |
| `comment-watcher.yml` | Single instance | One watcher at a time |
| `ci-monitor.yml` | Single instance at workflow level (`group: ci-monitor`); inside each dispatch, `prepare` job runs once → `monitor` matrix (max-parallel 8, one per workflow file) → `finalize` job runs once (`needs: monitor`, `if: always()`) | Only one monitor *dispatch* runs at a time; within it, the prepare step seeds the daily issue once, then up to 8 per-workflow analyses run in parallel (each producing failures independently rebuilds the Daily Cross-Workflow Summary — last writer wins, convergent). After all matrix jobs finish, the single `finalize` job checks the suspect PRs (`suspect_pr_comments.py`) and updates the upstream per-workflow Failure Trackers (`failure_tracker.py`) once, seeing the fully-populated daily issue. |
| `analyze-ci.yml` | Per run ID | Independent analyses run concurrently |
| `coverage-audit.yml` | Single instance (`group: coverage-audit`) | One audit at a time; a PR an earlier run could not merge is merged at the start of the next |

The comment watcher uses **reaction-based idempotency**: before dispatching, it checks if amd-bot has already added a `rocket` reaction to the comment. Both daemon and cron watcher share this mechanism, so running both simultaneously is safe.

The CI monitor uses **comment metadata deduplication**: each workflow comment embeds `<!-- processed_job_ids: 111,222,333 -->`. Each run reads these IDs before analyzing, preventing duplicate analysis within the same daily issue. Across UTC day boundaries, the monitor also indexes retained earlier-day state by exact GitHub job ID. If a failed job is still returned by the current lookback window, its saved analysis is copied into today's report instead of invoking Claude again. Prior-only jobs outside the current lookup are not copied, same-named jobs with different IDs are analyzed normally, and a missing or invalid cached analysis falls back to a fresh agent run.

Each monitor invocation pins its UTC report date when it starts and passes that
same date through per-workflow publication and Daily Summary refresh. A long
agent run that crosses UTC midnight therefore cannot attach new-day state to a
comment fetched from the previous day's issue.

When re-rendering an existing per-workflow comment (e.g. on the next hourly cron tick after a previous tick added new analyses), the bot needs to recover the previously-analysed jobs to merge them with the new batch. **Recovery is strict and self-contained per `<details>` block**: every per-job block emitted by `_render_per_job_block()` carries its `job_id`, `run_url`, and `started_at` as HTML attributes on the `<details>` tag itself:

```html
<details data-job-id="71234567"
         data-run-url="https://github.com/sgl-project/sglang/actions/runs/24500001234"
         data-started-at="2026-04-21T03:15:42Z">
<summary><b>job-name</b> — failed step(s): pytest</summary>

(per-job analysis text, may contain arbitrary nested markdown tables)

</details>
```

`parse_job_analyses_from_comment()` only matches blocks that have all three `data-*` attributes — it never scans loose markdown table rows or any other heuristic. This is a deliberate hard boundary: the older parser used a generic 4-column table-row regex that over-matched cluster summary tables, hypothesis tables, and failed-test tables embedded inside the agent-generated analysis text, producing fake job entries with `job_id=0` that snowballed across cron cycles into 800+ comment parts and >1500 spam comments per day (see issue #41 / #42 postmortem). The strict attribute-based parser eliminates this class of bug entirely. Legacy comments without the `data-job-id` attribute are intentionally ignored by the recovery path — the next cron run simply re-renders today's analyses in the new format; the `processed_job_ids` marker continues to dedup so no job is re-analysed.

Gate/finish jobs (e.g. `pr-test-amd-finish`, `wait-for-stage-b`) are automatically detected and skipped by the CI monitor. Only actual upstream failed jobs are analyzed, preventing redundant monolithic analyses under gate job names.

---

## Day-to-day operations

### View logs

```bash
docker logs -f amd-ci-bot-runner-1
```

### Apply code changes

| What changed | Workflows (cron + dispatch) | Daemon (runner-1) |
|---|---|---|
| `scripts/*.py` | `git push` — next run picks up changes | `docker restart amd-ci-bot-runner-1` |
| `agent/` (CLAUDE.md, tasks/, api-prompts.md) | `git push` — next run picks up changes | `docker restart amd-ci-bot-runner-1` |
| `.github/workflows/*.yml` | `git push` — effective immediately | N/A |
| `runner/entrypoint.sh` | N/A | `git pull` on host + `docker restart amd-ci-bot-runner-1` (bind-mounted) |
| `runner/Dockerfile` | N/A | Rebuild image + recreate containers |
| GitHub Actions secrets/variables | Effective immediately | N/A (daemon uses container env) |
| `.secrets/claude.env` | N/A | Re-run `setup.sh` to recreate containers |

### Restart runners

```bash
# Restart runner-1 (daemon picks up latest code from GitHub)
docker restart amd-ci-bot-runner-1

# Restart all
for i in $(seq 1 10); do docker restart amd-ci-bot-runner-$i; done

# Stop all
for i in $(seq 1 10); do docker rm -f amd-ci-bot-runner-$i; done
```

---

## CLI usage

### CI Monitor

```bash
# Agent mode is the default — these all use Claude Code CLI:
python scripts/monitor_ci.py --output stdout --hours-back 24
python scripts/monitor_ci.py --output daily-issue --bot-repo bingxche/sglang-ci-bot

# Force API-mode fallback (single-shot Anthropic via AMD LLM Gateway):
python scripts/monitor_ci.py --output stdout --hours-back 24 --no-use-agent
USE_AGENT=false python scripts/monitor_ci.py --output stdout --hours-back 24
```

| Option | Default | Description |
|--------|---------|-------------|
| `--output` | `stdout` | `stdout` or `daily-issue` |
| `--hours-back` | `24` | How far back to search |
| `--workflows` | all `MONITORED_WORKFLOWS` | Space-separated workflow files. In production, `ci-monitor.yml`'s matrix passes exactly **one** workflow per invocation. |
| `--job-name` | none | Filter: only jobs whose name contains this string |
| `--branch` | `main` | Only analyze runs on this branch |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` / `0` / `no` to disable) | Use Claude Code agent. Default ON; pass `--no-use-agent` (or set `USE_AGENT=false`) to fall back to direct Anthropic API calls via AMD LLM Gateway. |
| `--bot-repo` | none | Bot repo for posting issues (required for `daily-issue`) |
| `--github-token` | `BOT_PAT` / `GH_PAT` / `GITHUB_TOKEN` | GitHub token for API access |

When `--output daily-issue` is used and at least one workflow had new failures, `monitor_ci.py` automatically invokes `daily_cross_workflow_summary.build_and_publish_summary()` at the end to refresh the Daily Cross-Workflow Summary pinned in the daily issue body. Set the env var `BUILD_DAILY_SUMMARY=false` to disable this auto-trigger (e.g. for one-off debug runs).

### Daily Cross-Workflow Summary

Aggregates failures from all monitored workflows in today's daily issue into a single rolling **Daily Cross-Workflow Summary** **pinned in the daily issue body** (above all per-workflow comments). This is the once-a-day, all-workflows overview that answers "Is CI healthy today, and what should I do?" — distinct from the per-workflow `Cross-Job Summary` and `Cross-Run Pattern Analysis`.

```bash
# Agent mode (typical use — auto-triggered by monitor_ci.py)
python scripts/daily_cross_workflow_summary.py \
    --bot-repo bingxche/sglang-ci-bot --use-agent

# API mode fallback
python scripts/daily_cross_workflow_summary.py \
    --bot-repo bingxche/sglang-ci-bot --no-use-agent

# Rebuild the summary for a specific date (e.g. yesterday)
python scripts/daily_cross_workflow_summary.py \
    --bot-repo bingxche/sglang-ci-bot --date 2026-04-18 --use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `--bot-repo` | required | Bot repo where the daily issue lives |
| `--date` | today (UTC) | Daily issue date `YYYY-MM-DD` to rebuild for |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to use API mode instead |
| `--github-token` | `BOT_PAT` / `GH_PAT` / `GITHUB_TOKEN` | GitHub token |

The summary lives between two HTML placeholder markers in the daily issue body:

```
<!-- daily-cross-workflow-summary:start -->
... rendered summary ...
<!-- daily-cross-workflow-summary:end -->
```

The placeholder block is seeded by `ensure_daily_issue.py` (or `monitor_ci.find_or_create_daily_issue`) when the issue is first created — see `_initial_issue_body()` in `monitor_ci.py`. On each invocation the script PATCHes the issue body, replacing **only** the content between the markers (rest of the body is preserved). It also matches the **legacy** `ci-monitor-daily-status-board` markers, so daily issues created before the rename migrate in place. If the markers are missing entirely, the script seeds a fresh body via `_initial_issue_body()` and preserves the legacy content as a tail section.

Legacy summary *comments* from the pre-body code path (carrying the older `<!-- ci-monitor-daily-status-board -->` marker) are auto-deleted by `_cleanup_legacy_summary_comments()` so the summary never appears twice on the same issue.

If no daily issue exists yet for the date, or if no per-workflow comments have been posted, the script logs and exits cleanly without creating an empty summary.

Methodology and output format live in `agent/tasks/daily-cross-workflow-summary.md`. The Python script is a data-only harness.

The agent ends the report with two fenced JSON blocks that the script strips before publishing: `job-clusters` (cluster → job IDs, kept as the hidden `<!-- ci-monitor-job-clusters:v1 … -->` comment) and `suspect-prs` (per cluster, the merged sglang PR its top hypothesis names, with confidence and the last-passing / first-failing commits). Valid `suspect-prs` entries are kept as the hidden `<!-- ci-monitor-suspect-prs:v1 … -->` comment for `suspect_pr_comments.py`. If the agent leaves the block out, no PR gets a comment.

### Suspect PR comments

When the daily summary attributes a cluster to a merged sglang PR, `suspect_pr_comments.py` (run once per tick by the `finalize` job) can tell the PR's author on the PR. The agent only names the suspect; whether to comment is decided in code. A suspect is commented on only if all of these hold, checked in this order:

1. Confidence is `FACT` or `HIGH`.
2. The cluster has at least 2 failed jobs in today's job → cluster map.
3. The agent gave both the last passing and the first failing commit.
4. The PR is merged, within the last 14 days, and the cited commit is the PR's (its merge commit, or GitHub lists the PR for that commit).
5. The compare API puts the commit after the last passing commit and at or before the first failing commit.
6. If the cluster has a fix PR, it is still open and by someone other than the PR's author. A merged fix skips the suspect (nothing left to ask); a fix closed without merging is treated as no fix.
7. Without an open fix PR, the PR has no earlier suspect-PR comment (a `<!-- amd-bot-suspect-pr:<cluster> -->` marker) for any cluster. Cluster IDs are reassigned from day to day (#45791 was R327, then R332, an hour apart), so checking only the same cluster would comment on the same break again. A PR gets one comment, however long it stays red; when two clusters name the same PR in one tick, only the strongest is posted.
8. **Independent verifiers confirm it.** The checks above prove the commit is in the window, not that it caused the failure: that claim is one agent's reading of the per-job analyses. So 3 agents (`Task: Suspect PR Verification`, `agent/tasks/suspect-pr-verify.md`), each in a fresh worktree at the first failing commit, try to refute the attribution: they read the PR's diff and the failing logs, list the other commits in the window, and look for an image, dependency or infrastructure change, or a later pass. The comment is posted only if at least 2 confirm, and its footer says how many did. A verifier that does not finish counts for nothing; if fewer than 2 finish, the suspect is skipped (fail closed). Votes are stored, keyed by PR, commit and first failing commit, in a hidden `<!-- suspect-pr-votes … -->` block of the daily-issue log comment, so each suspect is verified once a day rather than every hourly tick, at most 5 new suspects per tick. `SUSPECT_PR_VERIFY=off` (repository variable) skips this check.

At most 3 comments are posted per tick, strongest confidence and most failed jobs first. The comment says what fails and why the PR is suspected, gives the commit window and up to 8 failing job links, and asks the author to say so if the PR is not the cause. It ends with a footer saying it was posted automatically by amd-bot, the AMD CI monitor, whichever account's token posts it. It has no `@`-mentions (any in the agent's evidence are put in code spans) and no links to this repo.

When an open fix PR is linked, the comment instead starts with `@<PR author>`, says the fix PR proposes a fix and asks them to review it, followed by the same evidence, window and job links. This is the only mention the bot makes. It carries a `<!-- amd-bot-suspect-fix:<fix PR> -->` marker and is sent once per fix PR, including on a PR that already had a suspect comment, so an author first told about the break is told again when a fix shows up.

Every tick also writes one comment on the daily issue (marker `<!-- ci-monitor-suspect-pr-log -->`, edited in place) with a row per suspect: would comment / commented / already commented / skipped and why, the verifier count, and each verifier's reason. In dry-run mode it includes the full text of each comment that would be posted. Verification runs in dry-run mode too, so the log shows what the verifiers would let through before `post` is switched on.

| `SUSPECT_PR_COMMENTS` | Behavior |
|---|---|
| `dry-run` (default) | Nothing is posted upstream; only the daily-issue log comment is written |
| `post` | Comments are posted on sglang PRs as amd-bot |
| `off` | The step does nothing |

Set it as a repository variable (Settings > Secrets and variables > Actions > Variables); the workflow defaults to `dry-run` when it is unset. The step is `continue-on-error`, so it cannot hold back the Failure Trackers.

```bash
python scripts/suspect_pr_comments.py --bot-repo bingxche/sglang-ci-bot               # SUSPECT_PR_COMMENTS or dry-run
python scripts/suspect_pr_comments.py --bot-repo bingxche/sglang-ci-bot --mode dry-run --date 2026-10-21
```

| Option | Default | Description |
|--------|---------|-------------|
| `--bot-repo` | required | Bot repo hosting the daily issue |
| `--date` | today (UTC) | Daily issue date `YYYY-MM-DD` to read suspects from |
| `--mode` | `SUSPECT_PR_COMMENTS` or `dry-run` | `off` / `dry-run` / `post` |
| `--verify` / `--no-verify` | on (env `SUSPECT_PR_VERIFY=off` to disable) | Have verifier agents confirm each suspect first |
| `--github-token` | `BOT_PAT` / `GH_PAT` / `GITHUB_TOKEN` | GitHub token (posts as amd-bot in production) |

### Failure Trackers (per workflow)

Long-lived issues on the upstream `sgl-project/sglang` repo — **one per tracked workflow**, titled `[Failure Tracker] <display>` — that record every failure as a persistent fact tracker, complementing the bot's rolling daily report. The goal is **transparency**: one unified place where anyone can see every AMD CI failure and exactly how long it has been red. Driven by `failure_tracker.py`, run **once per tick** by the `finalize` job in `ci-monitor.yml` (`needs: monitor`, `if: always()`) after the whole matrix completes — so it sees the fully-populated daily issue and updates each tracker exactly once (no per-matrix-job races).

**Why a dedicated `finalize` job instead of generating alongside the Daily Cross-Workflow Summary?** Two reasons learned the hard way:

1. **Reliability.** The Daily Cross-Workflow Summary agent emits ~30 KB of human prose; a trailing machine-readable JSON block appended to it was observed to be silently dropped by the model under output pressure (the tracker then never updated). A small, dedicated agent task whose *only* output is the JSON does not get dropped.
2. **No races.** The summary is rebuilt by *each* matrix job that had new failures (up to 8 concurrently per tick), each seeing only partial data. The `finalize` job runs once after all 8 finish, so the tracker update is single and complete.

**Content vs state split** (the core design):

- **Content (what failed)** is found by the **agent** via the dedicated `Task: Failure Tracker Data`, reading the SAME per-job analyses the daily report is built from — so tracker rows are consistent with the daily report. If Claude Code is unavailable, a **deterministic fallback** parses the per-job `### Failed Tests` tables (scoped to each already-isolated per-job block, not a loose whole-comment scan).
- **State (`first_seen` / duration / dedup)** is owned by **deterministic Python**. Each failure, keyed by `(test_file, test_function)` within its workflow's issue, lives in a hidden `<!-- ci-failure-tracker-state: {...} -->` JSON blob. On every scan an existing key keeps its original `first_seen` and only bumps `last_seen`; a new key is dated today; a key absent this scan is left untouched (never deleted, never re-dated). `Duration = last_seen − first_seen` and a `State` column (🔴 failing in latest scan / ⚪ quiet since `last_seen`) make "broken for N days" exact and persistent for months — the LLM is never asked to remember dates. (Issues created by the original single-workflow prototype using the legacy `<!-- pr-test-amd-tracker-state: -->` marker are read for backward compatibility and migrate to the generic marker on next update.)

The `Detail` column on each row deep-links back to that day's per-job analysis in the bot's daily issue (`https://github.com/<bot-repo>/issues/<n>#job-<job_id>`).

**Extensibility.** Tracked workflows are configured in `TRACKED_WORKFLOWS` in `failure_tracker.py` (currently just `pr-test-amd.yml`). Add an entry (`workflow → {title, display, legacy_titles}`) and that workflow gets its own Failure Tracker issue — the engine is fully generic; nothing else changes.

Standalone CLI (for testing / manual runs):

```bash
python scripts/failure_tracker.py --bot-repo bingxche/sglang-ci-bot          # today
python scripts/failure_tracker.py --bot-repo bingxche/sglang-ci-bot --date 2026-06-11
python scripts/failure_tracker.py --bot-repo bingxche/sglang-ci-bot --no-use-agent  # deterministic only
```

| Option | Default | Description |
|--------|---------|-------------|
| `--bot-repo` | required | Bot repo hosting the daily issue (used to build `Detail` deep-links) |
| `--date` | today (UTC) | Daily issue date `YYYY-MM-DD` to read failures from |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Agent extraction; `--no-use-agent` forces the deterministic table parser |
| `--github-token` | `BOT_PAT` / `GH_PAT` / `GITHUB_TOKEN` | GitHub token (needs issues:write on `sgl-project/sglang`) |

Each tracker issue is found-or-created idempotently by exact-title search on the upstream repo (an issue still under the old `[CI Tracker] … — Persistent Failure Ledger` title is auto-renamed to the new `[Failure Tracker] …` scheme rather than duplicated). Per-workflow failures are caught and logged so one workflow's tracker error can never break another's.

### Notion staging

The initial rollout is deliberately fail-closed. It recognizes a
stable candidate only when Claude Code verifies the same canonical signature in
at least two independent **completed** comparable runs, emits a direct URL and
completion/comparability record for every supporting run, and finds no later
comparable pass. One-run regressions, partial runs, incomplete history, and the
two more subjective policy paths remain on the watchlist for this first version.

The flow is split so the agent cannot inherit Notion secrets:

```bash
# Phase 1: GitHub evidence + Claude Code only
python scripts/stage_notion.py analyze \
    --bot-repo bingxche/sglang-ci-bot \
    --lookback-days 7 \
    --output .state/notion-staging-candidates.json

# Safe preview: validates and prints exactly what would be staged
python scripts/stage_notion.py sync \
    --input .state/notion-staging-candidates.json \
    --dry-run

# Phase 2: deterministic Notion read/dedup/write/verify; never runs an agent
NOTION_TOKEN=... \
NOTION_KNOWN_DATA_SOURCE_ID=... NOTION_STAGING_DATA_SOURCE_ID=... \
python scripts/stage_notion.py sync \
    --input .state/notion-staging-candidates.json
```

The analyzer prefers the marked JSON contract. If Claude Code drops only the
HTML markers, it accepts exactly one structurally identifiable fenced JSON
report and still applies the full deterministic gate; zero or multiple reports
fail closed. The raw agent response is retained beside the candidate file for
diagnostics.

`SGLang AMD CI Daily Staging` is independent from `Cron CI Monitor`. GitHub
Actions schedules it at `12:00 UTC`, which is `20:00 Asia/Shanghai` every day.
Scheduled runs always write after validation and deduplication. In the Actions
UI, manual runs default to `dry-run`; select `write` for an explicit sync test.

Use one minimum-permission Notion connection and keep the staging table in a
**separate database container on its own page** (not merely as a second data
source inside the official database, because Notion shares permissions at the
database-container level):

- `NOTION_TOKEN`: enable Read content and Insert content, disable Update
  content, and share only the official known-error database and isolated
  `SGLang AMD CI Staging Errors` database with this connection. The deterministic sync code
  uses the official ID only for reads and the staging ID as its sole create-page
  destination; the agent process never receives this token.

The script refuses identical official/staging IDs, verifies that the staging
schema contains exactly `Time`, `Status`, `Test File`, `Job`, `Owner`, `Fix PR`,
`Repro`, and `Error msg` with no extra columns, requires the target title to be
`SGLang AMD CI Staging Errors`, adds a
stable fingerprint to `Repro`, leaves `Status` unset, and checks after every run
that all created rows exist while the official data source's page IDs are
unchanged. `Time` is the title property, `Status` is a status property, and the
remaining six columns are rich text exactly as shown in the staging-table
layout. Blank `Owner` and `Fix PR` values are omitted.

`analyze` options:

| Option | Default | Description |
|--------|---------|-------------|
| `--bot-repo` | required | Repository containing the Daily Report issues |
| `--lookback-days` | `7` | Number of UTC Daily Reports supplied to Claude Code |
| `--end-date` | today (UTC) | Replay boundary in `YYYY-MM-DD` form |
| `--output` | required | Machine-readable candidate JSON path |
| `--github-token` | `GH_PAT` / `BOT_PAT` / `GITHUB_TOKEN` | GitHub API token |

`sync` options:

| Option | Default | Description |
|--------|---------|-------------|
| `--input` | required | Candidate JSON generated by `analyze` |
| `--dry-run` | false | Validate and print candidates without contacting Notion |
| `--known-data-source-id` | `NOTION_KNOWN_DATA_SOURCE_ID` | Official read-only data source |
| `--staging-data-source-id` | `NOTION_STAGING_DATA_SOURCE_ID` | Isolated writable staging data source |

### PR Review

```bash
# Agent mode is the default:
python scripts/review_pr.py 1234 --no-post

# Force a tier:
python scripts/review_pr.py 1234 --no-post --tier standard

# Force API-mode fallback:
python scripts/review_pr.py 1234 --no-post --no-use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | required | PR number to review |
| `--focus` | none | Specific areas to focus on (passed to every reviewer) |
| `--context` | none | Additional context |
| `--tier` | `auto` (env `REVIEW_TIER`) | `quick`, `standard`, or `auto` (from the PR's size) |
| `--no-post` | false | Print to stdout instead of posting |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to use API mode instead |

How an agent-mode review works (`review_pr.agent_review()`; the deterministic half is `review_findings.py`):

1. **Context.** Each agent's `.ci-context/` holds `pr.md` (description, changed files), the full `pr.diff`, `ci-runtime-env.md`, and `upstream/`: the sglang maintainers' `.claude/rules/*.md` and `.agents/skills/*/SKILL.md` from `main`, with an `INDEX.md` listing which changed files each rule's `paths` cover. These are the review standards; the checkout's own `.claude/` is not loaded (see [How Agent Mode Works](#how-agent-mode-works)).
2. **Tier.** A PR of at most 200 changed lines in at most 6 files is `quick`: one reviewer with every angle. Otherwise `standard`: four reviewers in parallel (`REVIEW_PARALLEL`), one per angle: `correctness`, `amd` (ROCm parity, aiter dispatch, env gates against the CI env), `contracts` (callers, signatures, config and IPC fields), `tests-conventions` (tests for the changed paths, `register_amd_ci`, the maintainers' rules). Each works in its own worktree at the PR head (`Task: PR Code Review`, `agent/tasks/pr-code-review.md`) and ends with a `<!-- review-findings {...} -->` JSON block. A finding needs a mechanism (the line and the concrete failure); style, naming, logging and "consider" suggestions are not reported, nor are conventions without a rule behind them.
3. **Checks in code.** A finding must name a file the PR changed, unless marked pre-existing; it is commented inline only if its line is in a hunk of the diff, otherwise it is listed in the summary. A `convention` finding must cite an `upstream/` rule file. Duplicates from several angles are merged; findings an earlier round posted are dropped; at most 12 go on (`REVIEW_MAX_FINDINGS`), with at most 3 nits.
4. **Verification.** Three verifier agents (`Task: PR Code Review Verification`, `REVIEW_VOTERS`) each read every candidate and vote `confirmed` / `refuted` / `uncertain`, trying to refute it: reachability, guards in callers, intent, whether the PR introduced it. A finding is kept only with 2 confirmations (`REVIEW_VOTES_REQUIRED`); a missing vote counts as no. Fewer than 2 verifiers finishing fails the review. A clean PR skips this step.
5. **Posting.** One PR review with event `COMMENT` (never Approve or Request changes): inline comments tagged 🔴 Important / 🟡 Nit / ⚪ Pre-existing, each with the verifier count, the bug's other locations, and the maintainers' rule when one applies. The summary starts with a count, or says **No issues found**, and notes reviewers that did not finish. If GitHub refuses the inline comments, they are folded into the review body. A hidden `<!-- amd-bot-review {...} -->` marker records the reviewed commit, the round and the findings.
6. **Later rounds.** On the next `@amd-bot review`, the reviewers get `previous-review.md` (what was posted) and `since-last-review.diff` (changes since the reviewed commit). They focus on the new changes, and earlier findings are not repeated. From round 4, the summary suggests stepping back to reconsider the approach.

If no reviewer finishes, the PR gets the "agent did not work" notice and the job fails. `pr-review.yml` passes `AGENT_GH_TOKEN` and uploads the agent sessions (`AGENT_ARCHIVE_DIR`) as an artifact. Timeouts: `REVIEW_FIND_TIMEOUT_SECS` (1500), `REVIEW_VERIFY_TIMEOUT_SECS` (900); turns: `REVIEW_FIND_MAX_TURNS` (200), `REVIEW_VERIFY_MAX_TURNS` (80).

### CI Status Check

```bash
# Agent mode is the default:
python scripts/check_ci_for_pr.py 1234 --no-post

# Force API-mode fallback:
python scripts/check_ci_for_pr.py 1234 --no-post --no-use-agent

# Replay a past check (CI state as of a time) and keep the evidence:
python scripts/check_ci_for_pr.py 40771 --no-post --head-sha 0a514355126300880c26c5d3e1b2c801d8f7ece6 \
  --as-of 2026-10-14T17:54:00Z --out-dir runs/40771-0a514355

# Score replays against hand-labeled cases (evals/ci_status/cases/*.json):
python scripts/eval_ci_status.py --runs runs [--run-missing]
```

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | required | PR number |
| `--no-post` | false | Print to stdout |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to use API mode instead |
| `--head-sha` | current PR head | Check this PR head (agent mode) |
| `--as-of` | now | Use each workflow run's attempt that was current at this UTC time (agent mode) |
| `--out-dir` | env `CI_STATUS_OUT_DIR` | Save `pack/` (evidence pack), `agent-report.md`, `report.md` (as posted), and `verdicts.json` (agent mode) |

An agent-mode failure posts the "agent did not work" notice on the PR and exits with an error (see [No API fallback in agent mode](#no-api-fallback-in-agent-mode)). When `--head-sha` or `--as-of` is set (replays), it exits with the error without posting.

**Untested-change check (AMD coverage gap).** Beyond classifying failures, the checker flags when an AMD test that covers this PR's changes never actually ran — the trap where AMD looks green only because the relevant test never executed. A changed `test/**` file declares its suite via `register_amd_ci(suite=...)` (nightly-only registrations are ignored); that suite maps to an AMD CI job (its `stage-a/b/c` is the stage). A banner is rendered at the **top** of the report whenever:

- **AMD CI was not triggered at all** for the commit → a `> [!CAUTION]` block (most dangerous: the change is completely untested on AMD; trigger / re-run AMD CI), or
- **a relevant suite did not run** (cancelled, skipped, or blocked by an earlier stage failure) or is still pending → a `> [!WARNING]` block naming the test, its AMD job, and stage.

The banner states that a passing / "Unlikely related" status does **not** mean the change is verified and tells the author to **re-run AMD CI** (no `/rerun-*` slash command is suggested — AMD tests can't be dispatched that way). This deterministic banner is computed in Python and injected at the top in **both** agent and API modes, so it appears regardless of agent behavior; in agent mode the agent additionally traces changed **source** files to their covering registered tests. (`ci-status-check.yml` defaults to agent mode — `USE_AGENT` defaults to `true`.)

**Evidence pack (agent mode).** Before the agent runs, `ci_evidence.py` collects into `.ci-context/`: every workflow run for the PR head (at the attempt current at `--as-of`); for each failed job the failing test file and cases, a log excerpt, raw metrics and how each retry attempt failed, the commit the job actually tested, the CI image tag and digest, the image's real `ENV` (from the registry by digest), the GPU, launch configs, backend-selection lines, and AITER modules loaded; the same job and test case in recent `main` runs, each marked comparable or not (same GPU, same image build or line, same launch config); the PR diff; full logs; and the gaps it could not fill. A job whose runner was lost mid-job gets mode `runner_lost` and the runner facts in place of the log it does not have. Every fact gets an ID (`D3`, `F1.env`, `F1.M2`, …). The worktree is checked out at the commit CI tested. `.ci-context/ci-runtime-env.md` adds the env vars the workflows and container scripts set. The pack is the agent's starting point, not a limit: it still reads source, history, and more logs as needed.

**Verdict check (agent mode).** The agent ends its report with a hidden `<!-- ci-status-verdicts {...} -->` block: per failure, a label (`related` / `possibly_related` / `unrelated` / `unknown`), the basis, and the evidence IDs it rests on. `ci_verdicts.py` checks it against the pack: cited IDs exist; `related` cites the full chain (diff hunk, evidence the changed path ran in that job, the failure); `unrelated` rests on comparable `main` records that failed the same test the same way with the PR no worse in metrics or frequency, or on independence that cites the diff, the failure, and any CI/build/dependency changes; the table and the block agree. A row that fails is shown as **⚪ unverified**, and a `> [!WARNING]` under the title says why. The check proves a verdict is grounded in collected facts, not that its reasoning is right. `CI_STATUS_AGENT_TIMEOUT` (default 40 min) gives the agent time to investigate. These exist because on sgl-project/sglang#40771 the bot read the PR head instead of the tested merge commit, judged QuickReduce off from its code default while the CI image sets `ROCM_QUICK_REDUCE_QUANTIZATION=INT8`, and called a PR-caused accuracy drop (0.95 → 0.00) unrelated because `main` had a milder failure in the same test.

**CI completeness check (agent mode).** The `PR CI Status Check` agent also evaluates whether required vendor PR CI pipelines actually completed. If an upstream stage or wait/gate job fast-fails and downstream jobs are skipped / not reached, the merge verdict says **PR CI is incomplete** and the caution block calls out that fast-failed/skipped downstream jobs are **not tested**. Failure tables are scoped to **executed** CI failures only. For high-priority PRs that need full signal despite unrelated early failures, the bot suggests adding the `bypass-fastfail` label and rerunning/updating the branch, with a warning that this consumes more CI resources and should be used sparingly.

### On-Demand Analysis

Analyze any GitHub Actions run or job URL. When `--bot-repo` is provided, creates a new issue with results; otherwise prints to stdout.

```bash
# Analyze a run (all failed jobs) — agent mode is default
python scripts/analyze_url.py --url https://github.com/sgl-project/sglang/actions/runs/24384910439

# Analyze a single job
python scripts/analyze_url.py --url https://github.com/sgl-project/sglang/actions/runs/24384910439/job/71216400611

# Create issue with results
python scripts/analyze_url.py --url <url> --bot-repo bingxche/sglang-ci-bot

# Force API-mode fallback
python scripts/analyze_url.py --url <url> --no-use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | required | GitHub Actions run or job URL |
| `--bot-repo` | none | Bot repo to create issue in (omit for stdout) |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to use API mode instead |

Or via the Actions tab: go to **Analyze CI** workflow, paste the URL, and click Run. The bot creates an issue like `[Analyze] pr-test-amd.yml run #12345 (8fe9bbf)` with results posted as comments.

---

## Project structure

```
sglang-ci-bot/
  agent/                    Single source of truth for ALL AI behavior
    CLAUDE.md               Ground rules every task follows (evidence-based,
                              confidence labels, no priority, in-flight fix
                              lookup, completed-runs-only, link hygiene)
    tasks/                  One methodology + output format per Task: line,
                              appended after CLAUDE.md (AGENT_TASKS in utils.py):
      job-failure-analysis.md       per-job, with Failure Cluster + Facts +
                                      Hypothesised Causes; AITER, Miles,
                                      MI355X disagg, lost runners
      cross-job-summary.md          one workflow's many jobs, by cluster
      cross-run-pattern-analysis.md one workflow across runs
      daily-cross-workflow-summary.md  daily issue body; ends with
                                      job-clusters and suspect-prs JSON blocks
      failure-tracker-data.md       JSON for the upstream Failure Trackers
      staging-candidates.md         strict JSON for the Notion staging gate
      pr-ci-status-check.md         judgment bar, hidden verdict block
      pr-code-review.md             one reviewer's angle and findings block
      pr-code-review-verify.md      one verifier's votes on the findings
      suspect-pr-verify.md          one verifier's vote on a suspect PR
    api-prompts.md          API Mode prompt templates (loaded at runtime)
  scripts/
    utils.py                Shared utilities:
                              - GitHub API helpers (incl. update_issue_body())
                              - Anthropic client (AMD LLM Gateway)
                              - Log parsing and error extraction
                              - is_gate_job(), get_failed_jobs()
                              - is_runner_lost(), runner_lost_evidence() — a job
                                  whose runner went away mid-job, and its facts
                              - analyze_job_with_agent(), analyze_job_api()
                              - load_prompt_template() — reads agent/api-prompts.md
                              - AGENT_TASKS, agent_instructions(), task_model() —
                                  each Task: line's instructions and model
                              - Worktree management: create_/remove_/agent_worktree()
                              - collect_ci_runtime_env() — AMD PR CI env vars for
                                  .ci-context/ci-runtime-env.md
                              - checkout_commit() — check out an exact SHA, verified
                              - Claude Code CLI wrapper: claude_code_analyze()
                                  (instructions, user-only settings, model, agent
                                  env, session archive, redact_secrets())
                              - _remove_deployed_claude_md() — deletes the copy
                                  older versions left in /workspace
    ensure_daily_issue.py   Idempotently create today's daily CI-monitor issue
                              (with Daily Cross-Workflow Summary placeholder
                              block already seeded
                              into the issue body) BEFORE the matrix monitor jobs
                              fan out, so concurrent find_or_create_daily_issue()
                              calls cannot race and create duplicates. Called from
                              ci-monitor.yml's prepare job.
    monitor_ci.py           CI failure monitor (one-shot, dispatched by GitHub
                              Actions). Called once per matrix entry — exactly one
                              workflow file per process (production deployment).
                              After analysing its workflow's failures, if any
                              report was produced (total_reports > 0) and the
                              BUILD_DAILY_SUMMARY env var is not "false", the
                              process auto-invokes
                              daily_cross_workflow_summary.build_and_publish_summary()
                              to refresh the Daily Cross-Workflow Summary pinned
                              in the daily issue body.
    daily_cross_workflow_summary.py
                            Daily Cross-Workflow Summary generator (the once-a-day,
                              all-workflows rollup). Reads per-workflow comments
                              posted by monitor_ci.py on the daily issue, re-parses
                              per-job analyses via parse_job_analyses_from_comment(),
                              spawns the agent with Task: Daily Cross-Workflow
                              Summary, and PATCHes the DAILY ISSUE BODY between the
                              placeholder markers
                              <!-- daily-cross-workflow-summary:start --> /
                              <!-- daily-cross-workflow-summary:end --> (also
                              matching the legacy ci-monitor-daily-status-board
                              markers for in-place migration). Also auto-deletes
                              legacy summary *comments* left over from before the
                              body-pinning move.
    failure_tracker.py      Persistent per-workflow Failure Trackers on the
                              UPSTREAM sgl-project/sglang repo (one issue per
                              workflow in TRACKED_WORKFLOWS). Run ONCE per tick
                              by ci-monitor.yml's `finalize` job (needs:
                              monitor) after the matrix completes. Recovers the
                              tracked workflows' per-job analyses from today's
                              daily issue, runs the dedicated `Task: Failure
                              Tracker Data` agent (small JSON-only output, with
                              a deterministic `### Failed Tests`-table parser as
                              fallback), then merges into each issue's hidden
                              state blob that preserves every failure's
                              first_seen date forever (state = deterministic,
                              never recomputed by the LLM). Renders a table
                              (time columns first; Detail column deep-links back
                              to the per-job analysis in the bot's daily issue
                              via #job-<id> anchors) and PATCHes the issue body
                              idempotently. Generic over workflow — add a line
                              to TRACKED_WORKFLOWS to extend.
    stage_notion.py         Two-phase, opt-in Notion staging pipeline. `analyze`
                              collects recent Daily Reports and invokes Claude
                              Code with Task: SGLang AMD CI Staging Candidates;
                              deterministic validation accepts only repeated
                              signatures from 2+ completed comparable runs with
                              no later pass. `sync --dry-run` previews without
                              Notion access. Write-mode reads both data sources,
                              deduplicates, writes only to the isolated staging
                              source, and verifies the official source is intact.
    order_daily_comments.py Rewrites the daily issue's report comments into
                              MONITORED_WORKFLOWS (priority) order. Run once
                              per tick by ci-monitor.yml's `finalize` job.
    suspect_pr_comments.py  Checks the summary's suspect PRs in code and
                              comments on the merged sglang PR (dry run by
                              default; SUSPECT_PR_COMMENTS=post to post). Logs
                              every decision in one daily-issue comment. Run
                              once per tick by the `finalize` job.
    analyze_url.py          On-demand analysis of a run/job URL
    check_ci_for_pr.py      PR CI status checker
    ci_evidence.py          Evidence pack for PR CI status (runs, failed-job facts,
                              main baseline, diff, logs; tested-commit checkout)
    ci_verdicts.py          Checks the agent's verdict block against the evidence
                              pack and marks unsupported rows in the report
    eval_ci_status.py       Scores ci-status replays against evals/ci_status/cases
    review_pr.py            PR code review: reviewer agents per angle, verifier
                              agents, one PR review with inline comments
    review_findings.py      The review's deterministic half: parse findings
                              and votes, check locations, merge, drop repeats,
                              tally, render
    watch_comments.py       Comment watcher / command dispatcher
    local_run.sh            Local dev runner
    verify_agent.sh         Claude Code agent verification
  .github/workflows/
    ci-monitor.yml          CI monitor — workflow_dispatch only (no cron in the
                              workflow itself). Runner-1's entrypoint.sh
                              dispatches it hourly via API.
                              Layout: prepare job (resolve workflow list +
                              ensure_daily_issue.py) → monitor matrix job
                              (max-parallel 8, one entry per workflow file
                              from MONITORED_WORKFLOWS) → finalize job
                              (order_daily_comments.py, suspect_pr_comments.py,
                              failure_tracker.py).
    notion-staging.yml      Independent daily Notion staging workflow. GitHub
                              schedules it for 12:00 UTC / 20:00 Asia/Shanghai;
                              scheduled runs write, while manual runs offer
                              dry-run and write modes.
    analyze-ci.yml          On-demand URL analysis (workflow_dispatch)
    ci-status-check.yml     PR CI check (repository_dispatch + workflow_dispatch)
    pr-review.yml           PR review (repository_dispatch + workflow_dispatch)
    comment-watcher.yml     Comment poller (cron every 5min, fallback for the
                              runner-1 daemon)
    coverage-audit.yml      Weekly coverage exclusion audit for ROCm/sglang-ci
                              (cron Mondays 16:07 UTC + workflow_dispatch dry run).
                              Runs ROCm/sglang-ci's upstream_ci/coverage_audit/;
                              works entirely in $RUNNER_TEMP.
  runner/
    Dockerfile              Runner image: Python 3.12, Node.js 22, Claude Code CLI, GitHub Actions runner
    setup.sh                Multi-runner deployment (default 10 containers)
    entrypoint.sh           Container entrypoint (register + daemons + bot repo
                              clone). Runner-1 only: starts watch_comments.py
                              --daemon AND an hourly loop that POSTs to
                              ci-monitor.yml/dispatches.
  tests/
    test_monitor_ci.py      Standard-library regression tests for CI-monitor
                              pagination, workflow configuration, cross-day
                              analysis reuse, and publication.
    test_ci_runtime_env.py  Tests for the AMD PR CI runtime-env extraction
                              (Dockerfile ENV, ENV_MAP, -e flags).
    test_ci_evidence.py     Tests for evidence-pack log parsing and comparability.
    test_ci_verdicts.py     Tests for the verdict check, report annotation, and
                              agent env / session archive.
    test_eval_ci_status.py  Tests for ci-status eval scoring.
    test_suspect_pr_comments.py
                            Tests for the suspect-prs block round trip, each
                              comment check, rendering, the per-tick cap, and
                              the verifier vote and its cache.
    test_agent_tasks.py     Tests that every Task: line has instructions that fit
                              in one argument, the model per task, and the CLI
                              flags (system prompt, user-only settings).
    test_review_findings.py Tests for findings/votes parsing, location checks,
                              merging, repeats, caps, tally and rendering.
    test_review_pr.py       Tests for tiers, the upstream rules index, the review
                              pipeline end to end with agents faked, and posting.
  evals/ci_status/cases/    Hand-labeled ci-status cases (PR head, as-of time,
                              truth + basis per failure; "proposed" until a
                              reviewer confirms)
  .state/                   Persisted state files (gitignored)
  .secrets/                 Local secret files (gitignored): claude.env, llm_gateway_key, gh_pat
  requirements.txt          Python dependencies: anthropic, httpx, requests
```

The regression tests cover exact job-ID reuse/publication, full multi-page job
enumeration, production workflow defaults, and incremental analysis of
completed failures while their parent workflow run is still in progress. Run
them with:

```bash
PYTHONPATH=scripts python3 -m unittest discover -s tests -p 'test_*.py'
```

---

## Environment variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `GH_PAT` / `BOT_PAT` | All scripts | GitHub token for API access and posting comments |
| `LLM_GATEWAY_KEY` | API mode | AMD LLM Gateway subscription key |
| `LLM_GATEWAY_URL` | API mode | AMD LLM Gateway endpoint |
| `USE_AGENT` | All scripts | Set to `false` / `0` / `no` to **disable** agent mode (default: enabled). Same effect as passing `--no-use-agent`. |
| `BUILD_DAILY_SUMMARY` | `monitor_ci.py` | Set to `false` to skip the auto-trigger of `daily_cross_workflow_summary.py` after `monitor_ci` finishes (default: enabled; legacy alias `BUILD_DAILY_BOARD` still honored) |
| `AGENT_PARALLEL` | `monitor_ci.py` | Max number of Claude Code agents per matrix job, processing failed jobs in parallel (default: `3`, capped to ≥1). Set in `ci-monitor.yml` env block. |
| `AGENT_TIMEOUT_SECS` | `monitor_ci.py` per-job analysis | Per-job agent timeout (default: `1500` = 25 min in `ci-monitor.yml`; code fallback `1800` when env unset). |
| `AGENT_MAX_TURNS` | Agent mode (per-job + daily summary) | Max conversation turns the agent may use (default: `150` in `ci-monitor.yml`; code fallback `1000` for per-job in `utils.py`, `200` in `daily_cross_workflow_summary.py` when env unset). |
| `CROSS_JOB_TIMEOUT_SECS` | `utils.cross_job_analysis` | Agent timeout for one workflow's Cross-Job Summary (default: `1200` = 20 min; 300 s timed out on every tick once pr-test-amd had 40+ analyses) |
| `CROSS_JOB_MAX_TURNS` | `utils.cross_job_analysis` | Max turns for the Cross-Job Summary agent (default: `100`) |
| `DAILY_SUMMARY_TIMEOUT_SECS` | `daily_cross_workflow_summary.py` | Agent timeout for the cross-workflow synthesis (default: `1200` = 20 min) |
| `TRACKER_AGENT_MAX_TURNS` | `failure_tracker.py` | Max turns for the `Task: Failure Tracker Data` extraction agent (default: `80`) |
| `TRACKER_AGENT_TIMEOUT_SECS` | `failure_tracker.py` | Agent timeout for the Failure Tracker extraction (default: `900` = 15 min) |
| `SUSPECT_PR_COMMENTS` | `suspect_pr_comments.py` | `dry-run` (default), `post`, or `off`; same as `--mode`. Set from the repository variable of the same name in `ci-monitor.yml`. |
| `SUSPECT_PR_VERIFY` | `suspect_pr_comments.py` | `on` (default) or `off`; same as `--verify` / `--no-verify` |
| `SUSPECT_VERIFY_TIMEOUT_SECS` / `SUSPECT_VERIFY_MAX_TURNS` | `suspect_pr_comments.py` | Per-verifier timeout (default `1200`) and turns (default `120`) |
| `REVIEW_TIER` | `review_pr.py` | `auto` (default), `quick` or `standard`; same as `--tier` |
| `REVIEW_PARALLEL` / `REVIEW_VOTERS` / `REVIEW_VOTES_REQUIRED` / `REVIEW_MAX_FINDINGS` | `review_pr.py` | Agents run at once (4), verifiers (3), confirmations a finding needs (2), findings sent to verification (12) |
| `REVIEW_FIND_TIMEOUT_SECS` / `REVIEW_FIND_MAX_TURNS` / `REVIEW_VERIFY_TIMEOUT_SECS` / `REVIEW_VERIFY_MAX_TURNS` | `review_pr.py` | Reviewer (1500 s, 200 turns) and verifier (900 s, 80 turns) budgets |
| `AGENT_MODEL_<TASK>` | Agent mode | Model for one task, overriding the default in `AGENT_TASKS` (see [Models per task](#models-per-task)) |
| `STAGING_AGENT_MAX_TURNS` | `stage_notion.py analyze` | Max turns for the machine-readable staging-candidate agent (default: `120`) |
| `STAGING_AGENT_TIMEOUT_SECS` | `stage_notion.py analyze` | Timeout for the staging-candidate agent (default: `1200` = 20 min) |
| `NOTION_TOKEN` | `stage_notion.py sync` | Single Notion token with Read + Insert but no Update capability, shared only with the official and isolated staging databases; never present in the agent step |
| `NOTION_KNOWN_DATA_SOURCE_ID` | `stage_notion.py sync` | Read-only `SGLang AMD CI Known Errors` data source ID; required to differ from the staging ID |
| `NOTION_STAGING_DATA_SOURCE_ID` | `stage_notion.py sync` | Isolated data source titled `SGLang AMD CI Staging Errors`; the only write destination |
| `AGENT_WORKSPACE` | Agent mode | Base directory for sglang clone (default: `/workspace`) |
| `AGENT_GH_TOKEN` | Agent mode | Optional read-only GitHub token; replaces `GH_PAT` / `GH_TOKEN` in the agent's env (the scripts keep the posting token) |
| `SGLANG_CI_TOKEN` | `coverage-audit.yml` | Optional ROCm/sglang-ci token for checkout, ledger, PR and merge, used instead of `GH_PAT`; neither is present in the agent step |
| `AGENT_ARCHIVE_DIR` | Agent mode | If set, the prompt and agent session log (secrets redacted) of every agent run are copied here; `ci-status-check.yml` uploads it as an artifact (30 days; the repo is public, so artifacts are too) |
| `CI_STATUS_OUT_DIR` | `check_ci_for_pr.py` | Same as `--out-dir` |
| `CI_STATUS_AGENT_TIMEOUT` | `check_ci_for_pr.py` | PR CI Status Check agent timeout in seconds (default: `2400` = 40 min) |
| `ANTHROPIC_API_KEY` | Agent mode | Set to `dummy` (Claude Code uses gateway, not direct API) |
| `ANTHROPIC_BASE_URL` | Agent mode | LLM Gateway endpoint for Claude Code |
| `ANTHROPIC_CUSTOM_HEADERS` | Agent mode | Gateway auth header (`Ocp-Apim-Subscription-Key: <key>`) |
| `ANTHROPIC_MODEL` | Agent mode | Claude Code model selector (e.g. `opus[1m]`) |
| `COMMENT_AUTHOR` | Workflows | Set by watcher, displayed in comment header |

---

## Customization

### Monitored workflows

Edit `MONITORED_WORKFLOWS` in `scripts/monitor_ci.py`.

### Claude model (API mode)

Edit `CLAUDE_MODEL` in `scripts/utils.py` (currently `claude-opus-5-5`).

### Claude Code model (agent mode)

Set via `ANTHROPIC_MODEL` in `.secrets/claude.env`. The current runner setup uses
`opus[1m]` and maps that selector to `Claude-Opus-5.5` with
`ANTHROPIC_DEFAULT_OPUS_MODEL`. Opus 5.5 requires Claude Code 2.1.280 or newer;
the CLI version is pinned in `runner/Dockerfile`, so bump it there and rebuild
the image when a new model needs a newer CLI.

### Agent behavior and prompt templates

Edit `agent/`. This is the **single source of truth** for all AI behavior:

- `agent/CLAUDE.md`: Ground Rules for every task. Keep it short; every agent run carries it.
- `agent/tasks/<task>.md`: one task's methodology and output format. A new task needs a file here and an entry in `AGENT_TASKS` in `scripts/utils.py` (`tests/test_agent_tasks.py` fails otherwise).
- `agent/api-prompts.md`: `{placeholder}` templates loaded by `load_prompt_template()` at runtime

Changes take effect on next `git push` + container restart (for daemon) or next workflow run (for workflow-triggered tasks).

### Bot identity

Edit `BOT_LOGIN` in `scripts/watch_comments.py`.

### Polling intervals

Comment watcher daemon: `--poll-interval` in `scripts/watch_comments.py` (default: 30s, deployed as 15s via `setup.sh`).

CI monitor dispatch: `entrypoint.sh` dispatches `ci-monitor.yml` via `workflow_dispatch` hourly (`sleep 3600` loop).

### Schedules

- `ci-monitor.yml`: triggered hourly by runner-1 via `workflow_dispatch`
- `comment-watcher.yml`: `'*/5 * * * *'` (every 5 minutes)
- `coverage-audit.yml`: `'7 16 * * 1'` (Mondays 16:07 UTC)
