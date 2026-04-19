# sglang-ci-bot

Automated CI monitoring and PR review bot for [sglang](https://github.com/sgl-project/sglang), powered by Claude via AMD LLM Gateway.

All AI behavior — both agent mode and API mode — is defined in a single file: [`agent/CLAUDE.md`](agent/CLAUDE.md). Agent mode reads it automatically; API mode loads prompt templates from it at runtime via `load_prompt_template()`. To change any AI behavior, edit only this file.

All public-facing comments are posted under the dedicated **[amd-bot](https://github.com/amd-bot)** GitHub account.

---

## Features

| Feature | Script | Trigger | What it does |
|---------|--------|---------|--------------|
| Cron CI Monitor | `monitor_ci.py` | Runner-1 dispatch (every 15min) | Monitors CI workflows, analyzes failures with historical comparison and regression detection, posts daily issue reports. Gate/finish jobs are automatically skipped — only actual upstream failures are analyzed. Reports group failures into **symptom clusters** with **confidence-labeled hypotheses** rather than asserted root causes. After processing all workflows, automatically invokes `build_daily_status_board.py` when at least one workflow had failures. |
| Daily Status Board | `build_daily_status_board.py` | Auto from `monitor_ci.py` (when `total_reports > 0`); also CLI | Aggregates per-job analyses from ALL monitored workflows into a single rolling top-of-issue comment. Deduplicates symptom clusters across workflows (same cluster spanning `pr-test-amd` + `nightly-test-amd` is one entry, not two). PATCHes one comment in place, identified by `<!-- ci-monitor-daily-status-board -->` marker. |
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
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw", "1am9trash", "sogalin"]
```

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
| `Task: Daily Status Board` | Daily Cross-Workflow Status Board (top-of-issue rollup across ALL workflows) |
| `Task: PR CI Status Check` | PR CI Status Check |
| `Task: PR Code Review` | PR Code Review |
| `Task: PR Correlation` | PR Correlation |

### Agent capabilities

- **CI failures**: Download logs via GitHub API, identify failed tests at the **test file + function** level, compare with recent **completed** runs, detect regressions, propose hypothesised commits with confidence labels, search for in-flight fix PRs to avoid duplication
- **PR reviews**: Read full source files in workspace, find callers of modified functions, verify AMD/ROCm parity, check test coverage
- **Isolation**: Each agent gets its own worktree — parallel agents (up to 2 in agent mode) and concurrent tasks cannot interfere with each other

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

`runner/setup.sh` spawns 10 runner containers. Runner-1 runs a comment watcher daemon + a CI monitor dispatch loop (dispatches `ci-monitor.yml` every 15 minutes via `workflow_dispatch`). Runners 2-10 are plain job executors.

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
| `--claude-config` | Path to Claude Code config directory |
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

## Monitored workflows

Configured in `MONITORED_WORKFLOWS` in `monitor_ci.py`:

```
nightly-test-amd.yml
nightly-test-amd-rocm720.yml
release-docker-amd-nightly.yml
release-docker-amd-rocm720-nightly.yml
amd-aiter-scout.yml
pr-test-amd.yml              (schedule-only: every 6h)
pr-test-amd-rocm720.yml      (schedule-only: daily)
```

Workflows in `SCHEDULE_ONLY_WORKFLOWS` are filtered to only analyze `schedule`-triggered runs (not PR-triggered runs). Each workflow report includes the sglang commit (`head_sha`) in the header, and the agent extracts the aiter commit from `[CI-AITER-CHECK]` log markers.

---

## Concurrency and idempotency

| Workflow | Concurrency group | Behavior |
|----------|-------------------|----------|
| `pr-review.yml` | Per comment ID | Duplicate dispatches cancelled |
| `ci-status-check.yml` | Per comment ID | Duplicate dispatches cancelled |
| `comment-watcher.yml` | Single instance | One watcher at a time |
| `ci-monitor.yml` | Single instance | One monitor at a time |
| `analyze-ci.yml` | Per run ID | Independent analyses run concurrently |

The comment watcher uses **reaction-based idempotency**: before dispatching, it checks if amd-bot has already added a `rocket` reaction to the comment. Both daemon and cron watcher share this mechanism, so running both simultaneously is safe.

The CI monitor uses **comment metadata deduplication**: each workflow comment embeds `<!-- processed_job_ids: 111,222,333 -->`. Each run reads these IDs before analyzing, preventing duplicate analysis.

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
# Agent mode (stdout)
python scripts/monitor_ci.py --output stdout --hours-back 24 --use-agent

# Agent mode (post to daily issue)
python scripts/monitor_ci.py --output daily-issue --bot-repo bingxche/sglang-ci-bot --use-agent

# API mode fallback
python scripts/monitor_ci.py --output stdout --hours-back 24
```

| Option | Default | Description |
|--------|---------|-------------|
| `--output` | `stdout` | `stdout` or `daily-issue` |
| `--hours-back` | `24` | How far back to search |
| `--workflows` | all monitored | Space-separated workflow files |
| `--job-name` | none | Filter: only jobs whose name contains this string |
| `--branch` | `main` | Only analyze runs on this branch |
| `--use-agent` | false | Use Claude Code agent (also reads `USE_AGENT` env var) |
| `--bot-repo` | none | Bot repo for posting issues (required for `daily-issue`) |
| `--github-token` | `BOT_PAT` / `GH_PAT` / `GITHUB_TOKEN` | GitHub token for API access |

When `--output daily-issue` is used and at least one workflow had new failures, `monitor_ci.py` automatically invokes `build_daily_status_board.build_and_publish_board()` at the end to refresh the cross-workflow status board comment. Set the env var `BUILD_DAILY_BOARD=false` to disable this auto-trigger (e.g. for one-off debug runs).

### Daily Status Board

Aggregates failures from all monitored workflows in today's daily issue into a single rolling top-of-issue comment.

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
| `--use-agent` | true (env `USE_AGENT`) | Use Claude Code agent; falls back to API if missing |
| `--github-token` | `BOT_PAT` / `GH_PAT` / `GITHUB_TOKEN` | GitHub token |

The board comment is identified by the HTML marker `<!-- ci-monitor-daily-status-board -->`. On each invocation the script PATCHes the existing board comment in place rather than appending a new one. If no daily issue exists yet for the date, or if no per-workflow comments have been posted, the script logs and exits cleanly without creating an empty board.

Methodology and output format live in `agent/CLAUDE.md` under `## Daily Cross-Workflow Status Board`. The Python script is a data-only harness.

### PR Review

```bash
# Agent mode
python scripts/review_pr.py 1234 --no-post --use-agent

# API mode
python scripts/review_pr.py 1234 --no-post
```

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | required | PR number to review |
| `--focus` | none | Specific areas to focus on |
| `--context` | none | Additional context |
| `--no-post` | false | Print to stdout instead of posting |
| `--use-agent` | false | Use Claude Code agent (also reads `USE_AGENT` env var) |

### CI Status Check

```bash
python scripts/check_ci_for_pr.py 1234 --no-post --use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | required | PR number |
| `--no-post` | false | Print to stdout |
| `--use-agent` | false | Use Claude Code agent (also reads `USE_AGENT` env var) |

### On-Demand Analysis

Analyze any GitHub Actions run or job URL. When `--bot-repo` is provided, creates a new issue with results; otherwise prints to stdout.

```bash
# Analyze a run (all failed jobs)
python scripts/analyze_url.py --url https://github.com/sgl-project/sglang/actions/runs/24384910439 --use-agent

# Analyze a single job
python scripts/analyze_url.py --url https://github.com/sgl-project/sglang/actions/runs/24384910439/job/71216400611 --use-agent

# Create issue with results
python scripts/analyze_url.py --url <url> --bot-repo bingxche/sglang-ci-bot --use-agent
```

| Option | Default | Description |
|--------|---------|-------------|
| `--url` | required | GitHub Actions run or job URL |
| `--bot-repo` | none | Bot repo to create issue in (omit for stdout) |
| `--use-agent` | false | Use Claude Code agent (also reads `USE_AGENT` env var) |

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
                              - Daily Cross-Workflow Status Board (top-of-issue rollup)
                              - PR CI Status Check methodology + output format
                              - PR Code Review methodology + output format
                              - AITER analysis instructions (GitHub API)
                              - API Mode prompt templates (loaded at runtime)
  scripts/
    utils.py                Shared utilities:
                              - GitHub API helpers
                              - Anthropic client (AMD LLM Gateway)
                              - Log parsing and error extraction
                              - is_gate_job(), get_failed_jobs()
                              - analyze_job_with_agent(), analyze_job_api()
                              - load_prompt_template() — reads CLAUDE.md API templates
                              - Worktree management: create/remove/agent_worktree()
                              - Claude Code CLI wrapper: claude_code_analyze()
    monitor_ci.py           CI failure monitor (one-shot, cron-triggered);
                              after processing all workflows, auto-invokes
                              build_daily_status_board.build_and_publish_board()
                              when total_reports > 0 (gated by BUILD_DAILY_BOARD env)
    build_daily_status_board.py
                            Cross-workflow daily status board generator.
                              Reads per-workflow comments posted by monitor_ci.py,
                              re-parses per-job analyses via
                              parse_job_analyses_from_comment(), spawns the agent
                              with Task: Daily Status Board, PATCHes a single
                              rolling comment marked
                              <!-- ci-monitor-daily-status-board -->.
    analyze_url.py          On-demand analysis of a run/job URL
    check_ci_for_pr.py      PR CI status checker
    review_pr.py            PR code review
    watch_comments.py       Comment watcher / command dispatcher
    local_run.sh            Local dev runner
    verify_agent.sh         Claude Code agent verification
  .github/workflows/
    ci-monitor.yml          CI monitor (workflow_dispatch from runner-1 + manual)
    analyze-ci.yml          On-demand URL analysis (workflow_dispatch)
    ci-status-check.yml     PR CI check (repository_dispatch + workflow_dispatch)
    pr-review.yml           PR review (repository_dispatch + workflow_dispatch)
    comment-watcher.yml     Comment poller (cron every 5min)
  runner/
    Dockerfile              Runner image: Python 3.12, Node.js 22, Claude Code CLI, GitHub Actions runner
    setup.sh                Multi-runner deployment (default 10 containers)
    entrypoint.sh           Container entrypoint (register + daemons + bot repo clone)
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
| `USE_AGENT` | All scripts | Set to `true` to enable agent mode (same as `--use-agent`) |
| `BUILD_DAILY_BOARD` | `monitor_ci.py` | Set to `false` to skip the auto-trigger of `build_daily_status_board.py` after `monitor_ci` finishes (default: enabled) |
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

CI monitor dispatch: `entrypoint.sh` dispatches `ci-monitor.yml` via `workflow_dispatch` every 15 minutes (`sleep 900` loop).

### Schedules

- `ci-monitor.yml`: triggered by runner-1 every 15 minutes via `workflow_dispatch`
- `comment-watcher.yml`: `'*/5 * * * *'` (every 5 minutes)
