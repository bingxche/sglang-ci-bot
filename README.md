# sglang-ci-bot

Automated CI monitoring and PR review bot for [sglang](https://github.com/sgl-project/sglang), powered by Claude via AMD LLM Gateway.

All AI behavior — both agent mode and API mode — is defined in a single file: [`agent/CLAUDE.md`](agent/CLAUDE.md). Agent mode reads it automatically; API mode loads prompt templates from it at runtime via `load_prompt_template()`. To change any AI behavior, edit only this file.

All public-facing comments are posted under the dedicated **[amd-bot](https://github.com/amd-bot)** GitHub account.

---

## Features

| Feature | Script | Trigger | What it does |
|---------|--------|---------|--------------|
| Cron CI Monitor | `ensure_daily_issue.py` (prepare step) + `monitor_ci.py` (per-workflow matrix) | Runner-1 dispatches `ci-monitor.yml` every 30min | `ci-monitor.yml` first runs a `prepare` job that calls `ensure_daily_issue.py` (idempotently creates today's daily issue with the daily-board placeholder seeded in its body), then fans out a `monitor` matrix job — **max-parallel 7, one entry per workflow file** in `MONITORED_WORKFLOWS`. Each matrix job analyses its workflow's failures with historical comparison and regression detection, then posts/PATCHes a per-workflow comment on the daily issue. Gate/finish jobs are automatically skipped. Reports group failures into **symptom clusters** with **confidence-labeled hypotheses** rather than asserted root causes. After its workflow has any new failure analysed, the matrix job auto-invokes `build_daily_status_board.build_and_publish_board()` to refresh the cross-workflow board pinned in the issue body. |
| Daily Status Board | `build_daily_status_board.py` | Auto-invoked by each `monitor_ci.py` matrix job that produced new failures (gated by `BUILD_DAILY_BOARD` env, default enabled); also CLI | Aggregates per-job analyses from ALL monitored workflows into a single rolling status board **pinned in the daily issue's body** (above all per-workflow comments) between `<!-- ci-monitor-daily-status-board:start -->` / `<!-- ci-monitor-daily-status-board:end -->` placeholder markers. Deduplicates symptom clusters across workflows (same cluster spanning `pr-test-amd` + `nightly-test-amd` is one entry, not two). PATCHes the issue body in place, replacing only the content between the placeholders. Legacy board *comments* from before this move (carrying the older `<!-- ci-monitor-daily-status-board -->` marker without `:start`/`:end`) are auto-deleted by `_cleanup_legacy_board_comments`. |
| On-Demand Analysis | `analyze_url.py` | `workflow_dispatch` (Actions tab) | Paste a GitHub Actions run or job URL, bot creates an issue with analysis results. Supports both run URLs (all failed jobs) and single job URLs. |
| PR Code Review | `review_pr.py` | `@amd-bot review` or manual | Checks out PR branch, reviews with full codebase context, posts structured review |
| CI Status Check | `check_ci_for_pr.py` | `@amd-bot ci-status` or manual | Checks all CI for a PR, analyzes failures, determines if failures are PR-related |
| Comment Watcher | `watch_comments.py` | Daemon (15s poll) + Cron (5min fallback) | Polls sglang PRs for `@amd-bot` commands, dispatches workflows |

### Supported commands

Comment on any sglang PR:

| Command | Action |
|---------|--------|
| `@amd-bot review` | Full code review of the PR |
| `@amd-bot review-focus <areas>` | Focused review on specific areas |
| `@amd-bot ci-status` | Check and analyze CI status |
| `@amd-bot help` | Show available commands |

### Authorized users

Only users listed in `AUTHORIZED_USERS` in `watch_comments.py` can trigger commands:

```python
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw", "1am9trash", "sogalin", "Kangyan-Zhou", "Fridge003", "BowenBao", "ColinZ22", "fxmarty-amd", "hubertlu-tw", "RolaoDenthu", "Duyi-Wang", "amd-danli103"]
```

---

## End-to-end execution flow

There are **three independent loops** running concurrently. Each is triggered differently and handles a different responsibility. They share state only via GitHub (issues, comments, reactions) — never via local disk.

### Loop 1 — CI monitoring (every 30 minutes)

```
runner-1 entrypoint.sh (sleep 1800) ─┐
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
                  │     missing, with daily-board     │
                  │     placeholder seeded in body)   │
                  └─────────────────┬─────────────────┘
                                    │
                                    ▼ matrix fan-out (max-parallel 7)
        ┌──────────┬──────────┬─────┴────┬──────────┬──────────┬──────────┐
        ▼          ▼          ▼          ▼          ▼          ▼          ▼
  nightly-test  nightly-720  release-   release-   amd-aiter  pr-test    pr-test-720
  -amd.yml     .yml         docker-    docker-    -scout.yml -amd.yml   .yml
                            amd-       amd-720-                (sched-   (sched-
                            nightly    nightly                 only)     only)

   each matrix job runs:  python scripts/monitor_ci.py --workflows <one>
                                      AGENT_PARALLEL=3
                                      AGENT_TIMEOUT_SECS=1500
                                      AGENT_MAX_TURNS=150
                                    │
                                    ▼
                  monitor_ci.run_oneshot() per workflow:
                  1. ensure_sglang_repo() — clone or fast-forward /workspace/sglang
                  2. get_workflow_runs(event="schedule") — non-success completed
                                            + in-progress runs in lookback
                                            window (default 24h). Manually-
                                            dispatched / PR-triggered runs are
                                            excluded by design.
                  3. for each run, get_failed_jobs() — drop gates, dedup vs
                     <!-- processed_job_ids: ... --> in existing comment
                  4. for each surviving failed job (up to AGENT_PARALLEL=3
                     in parallel):
                        - create_agent_worktree(job_id, head_sha)
                          → /workspace/sglang-wt-<job_id> at the CI commit
                        - deploy /workspace/CLAUDE.md
                        - run claude -p "Task: CI Monitor ..." (cwd=worktree)
                        - parse output, append to job_analyses
                  5. if >1 failure, run Task: Cross-Job Summary agent
                  6. render_workflow_comment(...) — split into 60KB parts if
                     needed (Part 1/N, Part 2/N, ... markers)
                  7. PATCH or POST the per-workflow comment on today's issue
                  8. if total_reports > 0 AND BUILD_DAILY_BOARD != false:
                        → call build_daily_status_board.build_and_publish_board()
                                    │
                                    ▼
                  build_and_publish_board():
                  1. read ALL per-workflow comments on today's issue
                  2. parse_job_analyses_from_comment() to recover structured data
                  3. fetch yesterday's board (issue body, then legacy comment)
                     for trend / NEW-cluster detection
                  4. write context to .ci-context/per-workflow-analyses.md
                  5. run claude -p "Task: Daily Status Board ..."
                  6. PATCH the daily issue body, replacing only the content
                     between :start --> and :end --> placeholders
                  7. delete any legacy <!-- ci-monitor-daily-status-board -->
                     comments left over from before this body-pinning move
```

**Why matrix instead of one big loop?** Earlier the monitor processed all 7 workflows sequentially in one job, which often timed out and lost in-flight data. Matrix fan-out caps each job at one workflow, runs them in parallel, and uses the GitHub Actions runner pool elastically. The trade-off: every matrix job that produces failures **independently** rebuilds the cross-workflow board, so the board can be rebuilt up to 7 times per dispatch. This is intentional — last writer wins, and since they all read the same per-workflow comments the result is convergent.

**Why a `prepare` step?** Without it, multiple matrix jobs racing `find_or_create_daily_issue()` would create duplicate issues for the same day. `ensure_daily_issue.py` runs once before fan-out so the issue (and its `:start`/`:end` placeholder block) exists by the time the matrix fires.

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
                  → run claude "Task: CI Monitor ..."
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
4. For PR tasks: **checks out the PR branch** in the worktree
5. Copies `agent/CLAUDE.md` to `/workspace/CLAUDE.md`
6. Runs `claude -p "<task prompt>" --dangerously-skip-permissions` with `cwd=<worktree>`

### Task dispatch

Agent prompts are **data-only** — they contain a `Task:` line and metadata, but no instructions. All methodology and output format is defined in `CLAUDE.md`. The agent routes to the correct section based on the task type:

| Prompt `Task:` line | CLAUDE.md section |
|---------------------|-------------------|
| `Task: CI Monitor` | CI Monitor — Per-Job Failure Investigation |
| `Task: Cross-Job Summary` | Cross-Job Summary (one workflow's many jobs, grouped by symptom cluster) |
| `Task: Cross-Run Pattern Analysis` | Cross-Run Pattern Analysis (one workflow across multiple runs) |
| `Task: Daily Status Board` | Daily Cross-Workflow Status Board (rendered into the daily issue **body** between `:start --> / :end -->` placeholders, so it appears above all per-workflow comments) |
| `Task: PR CI Status Check` | PR CI Status Check |
| `Task: PR Code Review` | PR Code Review |
| `Task: PR Correlation` | PR Correlation |

### Agent capabilities

- **CI failures**: Download logs via GitHub API, identify failed tests at the **test file + function** level, compare with recent **completed** runs, detect regressions, propose hypothesised commits with confidence labels, search for in-flight fix PRs to avoid duplication
- **PR reviews**: Read full source files in workspace, find callers of modified functions, verify AMD/ROCm parity, check test coverage
- **Isolation**: Each agent gets its own worktree — parallel agents (`AGENT_PARALLEL=3` per `monitor_ci.py` matrix job; `MAX_PARALLEL_JOBS=2` in `analyze_url.py`) and concurrent tasks cannot interfere with each other

### CI report methodology principles

The CI Monitor / Cross-Job Summary / Cross-Run Pattern Analysis / Daily Status Board tasks all follow the same evidence-based principles defined in CLAUDE.md's Ground Rules:

- **Symptom clustering, not root cause assertion**: failures are grouped into named **Failure Clusters** (e.g. "GPU memory access fault during model warmup"). The bot does NOT claim a cluster has been "caused by commit X" without verified evidence. Causal claims live in a separate **Hypothesised Causes** section with explicit confidence labels.
- **Confidence labels REQUIRED** for every causal claim: `FACT` / `HIGH` / `MEDIUM` / `LOW` / `SPECULATION` (default `LOW`).
- **Disconfirming evidence surfaced**: every hypothesis lists facts that *weaken* it, not just supporting evidence.
- **Bot does NOT assign Priority**: only states factual `Status` (e.g. "5 days persistent across 6 jobs in 3 workflows"). Engineers decide priority. The legacy `Priority: Critical/High/Medium/Low` field has been removed.
- **In-flight fix lookup REQUIRED**: before recommending any fix, the bot searches sglang's open PRs for matching keywords. Existing PRs are linked instead of duplicated.
- **Only completed runs count in trends**: in-progress / queued runs are excluded or labelled `[IN-FLIGHT]`. Drawing "trend dropped" / "regression candidate" conclusions from in-flight runs is forbidden.
- **Recommendations are triage steps, not directives**: bot output uses "Suggested triage: bisect A..B; if commit X is implicated, try reverting on a branch", NOT "Revert commit X".
- **Test-file granularity preserved**: every cluster table lists `(workflow, job, test_file, test_function)` — never collapsed to job-only level.

### Fallback to API mode

If Claude Code CLI is not available, all scripts automatically fall back to API mode (single-shot Anthropic API calls via AMD LLM Gateway). API mode loads prompt templates from the `## API Mode Prompts` section of `CLAUDE.md` via `load_prompt_template()`, ensuring both modes follow the same output format. API mode requires `LLM_GATEWAY_KEY` and `LLM_GATEWAY_URL` environment variables.

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

**Variables:**

| Variable | Value |
|----------|-------|
| `USE_AGENT` | `true` to enable agent mode in workflow-triggered reviews and CI checks |

### Deploy self-hosted runners

`runner/setup.sh` spawns 10 runner containers. Runner-1 runs a comment watcher daemon + a CI monitor dispatch loop (dispatches `ci-monitor.yml` every 30 minutes via `workflow_dispatch`). Runners 2-10 are plain job executors.

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
ANTHROPIC_DEFAULT_OPUS_MODEL=Claude-Opus-4.6
ANTHROPIC_DEFAULT_SONNET_MODEL=Claude-Sonnet-4.6
ANTHROPIC_DEFAULT_HAIKU_MODEL=Claude-Haiku-4.6
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
nightly-test-amd.yml
nightly-test-amd-rocm720.yml
release-docker-amd-nightly.yml
release-docker-amd-rocm720-nightly.yml
amd-aiter-scout.yml
pr-test-amd.yml
pr-test-amd-rocm720.yml
```

**All monitored workflows are filtered to `event=schedule` only.** Manually-dispatched (`workflow_dispatch`) runs and PR-triggered runs are intentionally excluded so the daily report is not polluted by ad-hoc / debug runs. If you need on-demand analysis of a specific manual run, use the `analyze-ci.yml` workflow (Actions tab → "Analyze CI" → paste the run/job URL).

This is enforced in `monitor_ci.run_oneshot()` by passing `event="schedule"` unconditionally to `monitor_workflow()`. The `SCHEDULE_ONLY_WORKFLOWS` constant equals `set(MONITORED_WORKFLOWS)` and exists only as a label for the cross-run-summary code path (which is also skipped automatically when fewer than 2 runs are present in the lookback window).

Each workflow report includes the sglang commit (`head_sha`) in the header, and the agent extracts the aiter commit from `[CI-AITER-CHECK]` log markers.

---

## Concurrency and idempotency

| Workflow | Concurrency group | Behavior |
|----------|-------------------|----------|
| `pr-review.yml` | Per comment ID | Duplicate dispatches cancelled |
| `ci-status-check.yml` | Per comment ID | Duplicate dispatches cancelled |
| `comment-watcher.yml` | Single instance | One watcher at a time |
| `ci-monitor.yml` | Single instance at workflow level (`group: ci-monitor`); inside each dispatch, `prepare` job runs once then fans out a `monitor` matrix (max-parallel 7, one per workflow file) | Only one monitor *dispatch* runs at a time; within it, the prepare step seeds the daily issue once, then up to 7 per-workflow analyses run in parallel. Each matrix job that produces failures independently rebuilds the cross-workflow board (last writer wins; convergent because all readers see the same per-workflow comments). |
| `analyze-ci.yml` | Per run ID | Independent analyses run concurrently |

The comment watcher uses **reaction-based idempotency**: before dispatching, it checks if amd-bot has already added a `rocket` reaction to the comment. Both daemon and cron watcher share this mechanism, so running both simultaneously is safe.

The CI monitor uses **comment metadata deduplication**: each workflow comment embeds `<!-- processed_job_ids: 111,222,333 -->`. Each run reads these IDs before analyzing, preventing duplicate analysis.

When re-rendering an existing per-workflow comment (e.g. on the next 30-minute cron tick after a previous tick added new analyses), the bot needs to recover the previously-analysed jobs to merge them with the new batch. **Recovery is strict and self-contained per `<details>` block**: every per-job block emitted by `_render_per_job_block()` carries its `job_id`, `run_url`, and `started_at` as HTML attributes on the `<details>` tag itself:

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
| `agent/CLAUDE.md` | `git push` — next run picks up changes | `docker restart amd-ci-bot-runner-1` |
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

When `--output daily-issue` is used and at least one workflow had new failures, `monitor_ci.py` automatically invokes `build_daily_status_board.build_and_publish_board()` at the end to refresh the cross-workflow status board pinned in the daily issue body. Set the env var `BUILD_DAILY_BOARD=false` to disable this auto-trigger (e.g. for one-off debug runs).

### Daily Status Board

Aggregates failures from all monitored workflows in today's daily issue into a single rolling status board **pinned in the daily issue body** (above all per-workflow comments).

```bash
# Agent mode (typical use — auto-triggered by monitor_ci.py)
python scripts/build_daily_status_board.py \
    --bot-repo bingxche/sglang-ci-bot --use-agent

# API mode fallback
python scripts/build_daily_status_board.py \
    --bot-repo bingxche/sglang-ci-bot --no-use-agent

# Rebuild the board for a specific date (e.g. yesterday)
python scripts/build_daily_status_board.py \
    --bot-repo bingxche/sglang-ci-bot --date 2026-04-18 --use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `--bot-repo` | required | Bot repo where the daily issue lives |
| `--date` | today (UTC) | Daily issue date `YYYY-MM-DD` to rebuild for |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to force API fallback |
| `--github-token` | `BOT_PAT` / `GH_PAT` / `GITHUB_TOKEN` | GitHub token |

The board lives between two HTML placeholder markers in the daily issue body:

```
<!-- ci-monitor-daily-status-board:start -->
... rendered board ...
<!-- ci-monitor-daily-status-board:end -->
```

The placeholder block is seeded by `ensure_daily_issue.py` (or `monitor_ci.find_or_create_daily_issue`) when the issue is first created — see `_initial_issue_body()` in `monitor_ci.py`. On each invocation the script PATCHes the issue body, replacing **only** the content between the markers (rest of the body is preserved). If the markers are missing (issue created before this change shipped), the script seeds a fresh body via `_initial_issue_body()` and preserves the legacy content as a tail section.

Legacy board *comments* from the pre-body code path (carrying the older `<!-- ci-monitor-daily-status-board -->` marker, no `:start`/`:end`) are auto-deleted by `_cleanup_legacy_board_comments()` so the board never appears twice on the same issue.

If no daily issue exists yet for the date, or if no per-workflow comments have been posted, the script logs and exits cleanly without creating an empty board.

Methodology and output format live in `agent/CLAUDE.md` under `## Daily Cross-Workflow Status Board`. The Python script is a data-only harness.

### PR Review

```bash
# Agent mode is the default:
python scripts/review_pr.py 1234 --no-post

# Force API-mode fallback:
python scripts/review_pr.py 1234 --no-post --no-use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | required | PR number to review |
| `--focus` | none | Specific areas to focus on |
| `--context` | none | Additional context |
| `--no-post` | false | Print to stdout instead of posting |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to force API fallback |

### CI Status Check

```bash
# Agent mode is the default:
python scripts/check_ci_for_pr.py 1234 --no-post

# Force API-mode fallback:
python scripts/check_ci_for_pr.py 1234 --no-post --no-use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | required | PR number |
| `--no-post` | false | Print to stdout |
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to force API fallback |

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
| `--use-agent` / `--no-use-agent` | **enabled** (env: `USE_AGENT=false` to disable) | Use Claude Code agent; pass `--no-use-agent` to force API fallback |

Or via the Actions tab: go to **Analyze CI** workflow, paste the URL, and click Run. The bot creates an issue like `[Analyze] pr-test-amd.yml run #12345 (8fe9bbf)` with results posted as comments.

---

## Project structure

```
sglang-ci-bot/
  agent/
    CLAUDE.md               Single source of truth for ALL AI behavior:
                              - Task dispatch routing
                              - Ground rules (evidence-based, confidence labels, no priority,
                                  in-flight fix lookup, completed-runs-only)
                              - CI Monitor methodology + output format (per-job, with Failure
                                  Cluster + Facts + Hypothesised Causes with confidence)
                              - Cross-Job Summary (one workflow's many jobs, grouped by cluster)
                              - Cross-Run Pattern Analysis (one workflow across runs)
                              - Daily Cross-Workflow Status Board (rendered into the daily
                                  issue body between :start --> / :end --> placeholders)
                              - PR CI Status Check methodology + output format
                              - PR Code Review methodology + output format
                              - AITER analysis instructions (GitHub API)
                              - API Mode prompt templates (loaded at runtime)
  scripts/
    utils.py                Shared utilities:
                              - GitHub API helpers (incl. update_issue_body())
                              - Anthropic client (AMD LLM Gateway)
                              - Log parsing and error extraction
                              - is_gate_job(), get_failed_jobs()
                              - analyze_job_with_agent(), analyze_job_api()
                              - load_prompt_template() — reads CLAUDE.md API templates
                              - Worktree management: create_/remove_/agent_worktree()
                              - Claude Code CLI wrapper: claude_code_analyze()
                              - _deploy_claude_md() — copies agent/CLAUDE.md to /workspace
    ensure_daily_issue.py   Idempotently create today's daily CI-monitor issue
                              (with daily-board placeholder block already seeded
                              into the issue body) BEFORE the matrix monitor jobs
                              fan out, so concurrent find_or_create_daily_issue()
                              calls cannot race and create duplicates. Called from
                              ci-monitor.yml's prepare job.
    monitor_ci.py           CI failure monitor (one-shot, dispatched by GitHub
                              Actions). Called once per matrix entry — exactly one
                              workflow file per process (production deployment).
                              After analysing its workflow's failures, if any
                              report was produced (total_reports > 0) and the
                              BUILD_DAILY_BOARD env var is not "false", the
                              process auto-invokes
                              build_daily_status_board.build_and_publish_board()
                              to refresh the cross-workflow board pinned in the
                              daily issue body.
    build_daily_status_board.py
                            Cross-workflow daily status board generator.
                              Reads per-workflow comments posted by monitor_ci.py
                              on the daily issue, re-parses per-job analyses via
                              parse_job_analyses_from_comment(), spawns the agent
                              with Task: Daily Status Board, and PATCHes the
                              DAILY ISSUE BODY between the placeholder markers
                              <!-- ci-monitor-daily-status-board:start --> /
                              <!-- ci-monitor-daily-status-board:end -->.
                              Also auto-deletes legacy <!-- ci-monitor-daily-
                              status-board --> *comments* left over from before
                              the body-pinning move.
    analyze_url.py          On-demand analysis of a run/job URL
    check_ci_for_pr.py      PR CI status checker
    review_pr.py            PR code review
    watch_comments.py       Comment watcher / command dispatcher
    local_run.sh            Local dev runner
    verify_agent.sh         Claude Code agent verification
  .github/workflows/
    ci-monitor.yml          CI monitor — workflow_dispatch only (no cron in the
                              workflow itself). Runner-1's entrypoint.sh
                              dispatches it every 30 minutes via API.
                              Layout: prepare job (resolve workflow list +
                              ensure_daily_issue.py) → monitor matrix job
                              (max-parallel 7, one entry per workflow file
                              from MONITORED_WORKFLOWS).
    analyze-ci.yml          On-demand URL analysis (workflow_dispatch)
    ci-status-check.yml     PR CI check (repository_dispatch + workflow_dispatch)
    pr-review.yml           PR review (repository_dispatch + workflow_dispatch)
    comment-watcher.yml     Comment poller (cron every 5min, fallback for the
                              runner-1 daemon)
  runner/
    Dockerfile              Runner image: Python 3.12, Node.js 22, Claude Code CLI, GitHub Actions runner
    setup.sh                Multi-runner deployment (default 10 containers)
    entrypoint.sh           Container entrypoint (register + daemons + bot repo
                              clone). Runner-1 only: starts watch_comments.py
                              --daemon AND a 30-minute loop that POSTs to
                              ci-monitor.yml/dispatches.
  .state/                   Persisted state files (gitignored)
  .secrets/                 Local secret files (gitignored): claude.env, llm_gateway_key, gh_pat
  requirements.txt          Python dependencies: anthropic, httpx, requests
```

---

## Environment variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `GH_PAT` / `BOT_PAT` | All scripts | GitHub token for API access and posting comments |
| `LLM_GATEWAY_KEY` | API mode | AMD LLM Gateway subscription key |
| `LLM_GATEWAY_URL` | API mode | AMD LLM Gateway endpoint |
| `USE_AGENT` | All scripts | Set to `false` / `0` / `no` to **disable** agent mode (default: enabled). Same effect as passing `--no-use-agent`. |
| `BUILD_DAILY_BOARD` | `monitor_ci.py` | Set to `false` to skip the auto-trigger of `build_daily_status_board.py` after `monitor_ci` finishes (default: enabled) |
| `AGENT_PARALLEL` | `monitor_ci.py` | Max number of Claude Code agents per matrix job, processing failed jobs in parallel (default: `3`, capped to ≥1). Set in `ci-monitor.yml` env block. |
| `AGENT_TIMEOUT_SECS` | `monitor_ci.py` per-job analysis | Per-job agent timeout (default: `1500` = 25 min in `ci-monitor.yml`; code fallback `1800` when env unset). |
| `AGENT_MAX_TURNS` | Agent mode (per-job + daily board) | Max conversation turns the agent may use (default: `150` in `ci-monitor.yml`; code fallback `1000` for per-job in `utils.py`, `200` in `build_daily_status_board.py` when env unset). |
| `DAILY_BOARD_TIMEOUT_SECS` | `build_daily_status_board.py` | Agent timeout for the cross-workflow synthesis (default: `1200` = 20 min) |
| `AGENT_WORKSPACE` | Agent mode | Base directory for sglang clone (default: `/workspace`) |
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

Edit `CLAUDE_MODEL` in `scripts/utils.py` (currently `claude-opus-4-6`).

### Claude Code model (agent mode)

Set via `ANTHROPIC_MODEL` env var in `.secrets/claude.env`.

### Agent behavior and prompt templates

Edit `agent/CLAUDE.md`. This is the **single source of truth** for all AI behavior:

- **Agent mode sections** (CI Monitor, PR CI Status Check, PR Code Review): methodology, output format, ground rules
- **API Mode Prompts** section: `{placeholder}` templates loaded by `load_prompt_template()` at runtime

Changes take effect on next `git push` + container restart (for daemon) or next workflow run (for workflow-triggered tasks).

### Bot identity

Edit `BOT_LOGIN` in `scripts/watch_comments.py`.

### Polling intervals

Comment watcher daemon: `--poll-interval` in `scripts/watch_comments.py` (default: 30s, deployed as 15s via `setup.sh`).

CI monitor dispatch: `entrypoint.sh` dispatches `ci-monitor.yml` via `workflow_dispatch` every 30 minutes (`sleep 1800` loop).

### Schedules

- `ci-monitor.yml`: triggered by runner-1 every 30 minutes via `workflow_dispatch`
- `comment-watcher.yml`: `'*/5 * * * *'` (every 5 minutes)
