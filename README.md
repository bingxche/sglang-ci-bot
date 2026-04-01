# sglang-ci-bot

Automated CI monitoring and PR review bot for [sglang](https://github.com/sgl-project/sglang), powered by Claude via AMD LLM Gateway.

Supports two analysis backends:

- **API mode** (default): Single-shot Claude API calls via AMD LLM Gateway — fast, structured output
- **Agent mode** (`--use-agent`): Claude Code CLI as an autonomous agent — reads source code, checks git history, downloads logs, iteratively investigates root causes

Runs entirely from a **separate personal repo** (`bingxche/sglang-ci-bot`) using GitHub Actions + GitHub REST API. No admin access to the target repo required — sglang is a public repo, so any authenticated GitHub user can read data and post comments.

All public-facing replies (PR comments, reactions, CI reports) are posted under the dedicated **[amd-bot](https://github.com/amd-bot)** GitHub account.

---

## Table of Contents

- [Features Overview](#features-overview)
- [API Mode vs Agent Mode](#api-mode-vs-agent-mode)
- [Architecture](#architecture)
- [Setup](#setup)
  - [Deploy Self-Hosted Runner](#3-deploy-self-hosted-runner)
  - [Concurrency](#concurrency)
- [Feature 1: CI Failure Monitor](#feature-1-ci-failure-monitor)
- [Feature 2: PR Code Review](#feature-2-pr-code-review)
- [Feature 3: CI Status Check for a PR](#feature-3-ci-status-check-for-a-pr)
- [Feature 4: Comment Watcher](#feature-4-comment-watcher)
- [Applying Changes](#applying-changes)
- [Local Development](#local-development)
- [Project Structure](#project-structure)
- [Customization](#customization)

---

## Features Overview

| Feature | Script | Trigger | What it does |
|---------|--------|---------|--------------|
| CI Failure Monitor | `monitor_ci.py` | Daemon (60s) + Cron (8h fallback) | Monitors CI workflows in real-time, analyzes failures, posts/updates daily issue comments |
| PR Code Review | `review_pr.py` | `@amd-bot review` or manual | Reviews PR code, posts structured review as PR comment |
| CI Status Check | `check_ci_for_pr.py` | `@amd-bot ci-status` or manual | Checks all CI for a PR, analyzes failures, correlates with PR changes |
| Comment Watcher | `watch_comments.py` | Daemon (15s) + Cron (5min fallback) | Polls sglang PRs for `@amd-bot` commands, dispatches with reaction-based idempotency |

All three analysis features (monitor, review, ci-status) support both **API mode** and **Agent mode**.

---

## API Mode vs Agent Mode

The bot supports two analysis backends. Each script accepts `--use-agent` (or reads the `USE_AGENT` env var) to switch between them.

### API Mode (default)

Uses single-shot Anthropic API calls via AMD LLM Gateway. Python code downloads logs, extracts errors, pre-filters, and sends everything to Claude in one prompt.

- **Requires**: `LLM_GATEWAY_KEY` + `LLM_GATEWAY_URL` env vars
- **Speed**: ~30 seconds per analysis
- **Quality**: Good for structured, fast output

### Agent Mode (`--use-agent`)

Uses Claude Code CLI (`claude -p`) as an autonomous agent. The Python code is a thin dispatcher — it tells the agent what to investigate, and the agent handles everything: downloading logs via the GitHub API, reading sglang source code, checking git history, iteratively exploring until it finds the root cause.

- **Requires**: `claude` CLI installed + configured (env vars for AMD LLM Gateway)
- **Speed**: ~3-5 minutes per analysis (agent explores iteratively)
- **Quality**: Significantly deeper — can cross-reference source code, find regressions, validate assumptions
- **Fallback**: If `claude` CLI is not found or the agent fails, automatically falls back to API mode

### What Agent Mode does differently

| Step | API Mode | Agent Mode |
|------|----------|------------|
| Download CI logs | Python (`requests`) | Agent (`curl` with `$GH_PAT`) |
| Parse log errors | Python regex (`extract_error_lines`) | Agent reads and understands log |
| Read source code | N/A (only sees the diff) | Agent reads full source files |
| Check git history | N/A | Agent runs `git log`, `git blame` |
| Cross-reference | N/A | Agent finds callers, related tests |
| Analyze | One LLM API call | 15-30 tool-use turns |

### Docker image with Agent support

A pre-built Docker image with Claude Code, Node.js, and a pre-cloned sglang repo is available:

```
bingxche/sglang-ci-bot-runner:claude-agent
```

This image contains:
- Claude Code CLI 2.1.87 + Node.js 22
- sglang repo pre-cloned at `/workspace/sglang` (only needs `git pull` to update)
- Non-sensitive ANTHROPIC env vars pre-configured
- No secrets baked in — the LLM Gateway subscription key is injected at runtime via `--llm-gateway-key`

---

## Architecture

The bot uses **two GitHub accounts** with different roles:

| Account | Role | What it does |
|---------|------|--------------|
| **bingxche** | Repo owner / infra admin | Owns `bingxche/sglang-ci-bot`, registers self-hosted runners (requires admin access) |
| **amd-bot** | Bot identity | Posts all public-facing comments, reactions, and issues on sglang PRs |

This separation exists because self-hosted runner registration requires repo admin access, but `amd-bot` (as a collaborator) cannot have admin on a personal repo.

**Token usage:**

| Context | Token used | Identity |
|---------|-----------|----------|
| GitHub Actions workflows (review, CI check, monitor, cron watcher) | `secrets.GH_PAT` (amd-bot's PAT) | amd-bot |
| Daemon comment watcher (runner-1 container) | `BOT_PAT` env var (amd-bot's PAT) | amd-bot |
| Daemon CI monitor (runner-1 container) | `BOT_PAT` env var (amd-bot's PAT) | amd-bot |
| Agent mode (Claude Code in container) | `GH_PAT` env var (inherited) | amd-bot |
| Runner registration (`entrypoint.sh`) | `GH_PAT` env var (bingxche's PAT) | bingxche |
| Git clone inside container | `GH_PAT` env var (bingxche's PAT) | bingxche |

---

## Setup

### 1. Prerequisites

- **bingxche** GitHub account: owner of this bot repo
- **amd-bot** GitHub account: added as collaborator (write access) to `bingxche/sglang-ci-bot`
- AMD LLM Gateway subscription key and endpoint URL
- Two GitHub PATs:
  - **bingxche's PAT**: `repo` + `workflow` + `admin:repo_hook` scopes (for runner registration)
  - **amd-bot's PAT**: `repo` scope (for posting comments on sglang and dispatching workflows on bot repo)

### 2. Configure Repository Secrets and Variables

In `bingxche/sglang-ci-bot`, go to **Settings > Secrets and variables > Actions** and add:

**Secrets:**

| Secret | Value |
|--------|-------|
| `GH_PAT` | **amd-bot's** GitHub PAT (all workflows use this to post as amd-bot) |
| `LLM_GATEWAY_KEY` | AMD LLM Gateway subscription key |
| `LLM_GATEWAY_URL` | AMD LLM Gateway endpoint (e.g. `https://llm-api.amd.com/Anthropic`) |

**Variables (for agent mode):**

| Variable | Value |
|----------|-------|
| `USE_AGENT` | `true` to enable agent mode in workflow-triggered reviews and CI checks |

> **Note**: The `GH_PAT` secret is amd-bot's PAT, not bingxche's. Bingxche's PAT is only used on runner machines for registration (passed via `--pat` to `setup.sh`).

### 3. Deploy Self-Hosted Runner

All workflows run on self-hosted runners with the `amd-internal` label. The `runner/setup.sh` script spawns multiple runner containers (default: 10) so jobs can execute in parallel.

#### With Agent mode (recommended)

```bash
# Pull pre-built image with Claude Code + sglang repo baked in
bash runner/setup.sh \
  --pat <bingxche-PAT> \
  --bot-pat <amd-bot-PAT> \
  --llm-gateway-key <KEY> \
  --image bingxche/sglang-ci-bot-runner:claude-agent \
  --use-agent
```

#### Without Agent mode (API only)

```bash
# Build image locally
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --llm-gateway-key <KEY> --build

# Or pull pre-built image
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --llm-gateway-key <KEY> --image bingxche/sglang-ci-bot-runner:latest
```

#### Without CI monitor daemon (comment watcher only)

```bash
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --build
```

**setup.sh options:**

| Option | Description |
|--------|-------------|
| `--pat` | bingxche's PAT (runner registration, requires repo admin) |
| `--bot-pat` | amd-bot's PAT (daemon comment watcher) |
| `--llm-gateway-key` | AMD LLM Gateway key (enables CI monitor daemon + agent auth) |
| `--llm-gateway-url` | LLM Gateway endpoint (defaults to `https://llm-api.amd.com/Anthropic`) |
| `--image` | Pull image from registry instead of building |
| `--build` | Force local build from Dockerfile |
| `--use-agent` | Enable Claude Code agent mode on runner-1 |
| `--count` | Number of runner containers (default: 10) |
| `--name` | Runner name prefix (default: `amd-ci-bot-runner`) |

This creates containers `amd-ci-bot-runner-1` through `amd-ci-bot-runner-10`:
- **Runner-1** runs two background daemons:
  - **Comment watcher** (`ENABLE_WATCHER=true`) — polls for `@amd-bot` commands every 15 seconds
  - **CI monitor** (`ENABLE_CI_MONITOR=true`, if `--llm-gateway-key` provided) — polls for CI failures every 60 seconds
  - If `--use-agent`: CI monitor uses Claude Code agent; sglang repo at `/workspace/sglang` is updated on startup
- **Runners 2-10** are plain job executors
- `entrypoint.sh` is bind-mounted from the host repo, so changes take effect on `docker restart`

#### Day-to-day operations

```bash
# View logs (runner-1 shows daemon output)
docker logs -f amd-ci-bot-runner-1

# Restart runner-1 (pulls latest code for daemon)
git pull
docker restart amd-ci-bot-runner-1

# Restart all runners
for i in $(seq 1 10); do docker restart amd-ci-bot-runner-$i; done

# Stop all runners
for i in $(seq 1 10); do docker rm -f amd-ci-bot-runner-$i; done
```

### 4. Enable Workflows

Push the code to your repo. GitHub Actions will automatically pick up the workflow files and start running on their cron schedules.

### Concurrency & Idempotency

Each workflow has a `concurrency` group to prevent redundant or conflicting runs:

| Workflow | Concurrency group | Behavior |
|----------|-------------------|----------|
| `pr-review.yml` | Per comment ID | Duplicate dispatches from the same `@amd-bot` comment share a group; second run is cancelled |
| `ci-status-check.yml` | Per comment ID | Same as above |
| `comment-watcher.yml` | Single instance | Only one watcher runs at a time (stateful, prevents duplicate dispatches) |
| `ci-monitor.yml` | Single instance | Only one monitor runs at a time (stateful, prevents duplicate issues) |

Different PRs are processed in parallel across the available runners.

The comment watcher (both daemon and cron modes) uses **reaction-based idempotency**: before dispatching, it checks if the `amd-bot` account has already added a `rocket` reaction to the comment. This works as a distributed lock — even if both the daemon and cron watcher see the same comment, only the first one to react will dispatch. The `eyes` reaction is added as a user-visible acknowledgment.

---

## Feature 1: CI Failure Monitor

**Script**: `scripts/monitor_ci.py`
**Workflow**: `.github/workflows/ci-monitor.yml` (cron fallback)
**Daemon**: runner-1 container (primary, polls every 60s)

### What it does

1. Queries the GitHub API for failed workflow runs in the monitored workflows (main branch only, last 24 hours by default)
2. **Monitors in-progress runs**: doesn't wait for the entire workflow to finish — if a job fails 10 minutes into a 4-hour workflow, it's analyzed immediately
3. Skips jobs that have already been analyzed (deduplication via HTML metadata embedded in GitHub comments — shared between daemon and cron)
4. Analyzes each failed job (API mode or agent mode)
5. **Each workflow gets ONE comment** in the daily issue, updated via PATCH as new failures are discovered. In-progress runs show a "still running" indicator that's removed when the workflow completes

**In API mode**: Downloads full job log, pre-filters large logs, extracts errors with regex, runs focused single-shot analysis with Claude, then cross-job analysis if multiple jobs failed.

**In agent mode**: Gives the agent the job ID and URL. The agent downloads the log via GitHub API, reads sglang source code, checks git history, and produces a root-cause analysis autonomously. Parallel workers reduced from 3 to 2 (agent processes are heavier).

### Monitored Workflows

```
nightly-test-amd.yml
nightly-test-amd-rocm720.yml
release-docker-amd-nightly.yml
release-docker-amd-rocm720-nightly.yml
amd-aiter-scout.yml
pr-test-amd-rocm720.yml
```

### How to use

**Via GitHub Actions (manual trigger):**

```bash
gh workflow run ci-monitor.yml
gh workflow run ci-monitor.yml -f hours_back=48
gh workflow run ci-monitor.yml -f output_mode=stdout
gh workflow run ci-monitor.yml -f use_agent=true
```

**Via command line:**

```bash
# API mode (default)
python scripts/monitor_ci.py --output stdout --hours-back 24

# Agent mode
python scripts/monitor_ci.py --output stdout --hours-back 24 --use-agent

# Daemon with agent mode
python scripts/monitor_ci.py --daemon --bot-repo bingxche/sglang-ci-bot --use-agent
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--output` | `stdout` | Output mode: `stdout` or `daily-issue` (one-shot only) |
| `--hours-back` | `24` | How many hours back to search for failures |
| `--workflows` | (all monitored) | Space-separated list of workflow files to check |
| `--job-name` | (none) | Only analyze jobs whose name contains this string |
| `--branch` | `main` | Only analyze runs triggered on this branch |
| `--bot-repo` | (none) | Bot repo for posting issues (required for `daily-issue` and `--daemon`) |
| `--daemon` | false | Run as a long-lived daemon instead of one-shot |
| `--poll-interval` | `60` | Override active poll interval in daemon mode (seconds) |
| `--use-agent` | false | Use Claude Code agent (also reads `USE_AGENT` env var) |
| `--github-token` | `$GH_PAT` | GitHub token (can also set via env var) |

### State & Deduplication

Deduplication works across processes (daemon + cron) without shared filesystem:

1. **GitHub comment metadata (source of truth)**: Each workflow comment embeds `<!-- processed_job_ids: 111,222,333 -->` as an invisible HTML comment. Before analyzing, both daemon and cron read these IDs from the daily issue's comments to know which jobs have already been processed.
2. **Local state (cache)**: The daemon keeps `job_analyses` in `.state/ci_monitor.json` as a local cache for rebuilding comments on PATCH. The cron uses `actions/cache` for its own local state. Neither process depends on the other's local state.

---

## Feature 2: PR Code Review

**Script**: `scripts/review_pr.py`
**Workflow**: `.github/workflows/pr-review.yml`

### What it does

**In API mode**: Fetches PR diff and file list, sends to Claude in a single prompt for structured code review.

**In agent mode**: Checks out the PR branch in the sglang repo, tells the agent the PR number. The agent fetches the diff via GitHub API, reads the full source files (not just diff hunks), finds callers of modified functions, checks test coverage, and produces a thorough review.

### How to use

**Via `@amd-bot` comment on a sglang PR:**

```
@amd-bot review
@amd-bot review-focus AMD ROCm compatibility and memory management
```

**Via command line:**

```bash
# API mode
python scripts/review_pr.py 1234 --no-post

# Agent mode — deeper review, reads full source
python scripts/review_pr.py 1234 --no-post --use-agent
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | (required) | PR number to review |
| `--focus` | (none) | Specific areas to focus on |
| `--context` | (none) | Additional context for the review |
| `--no-post` | false | Print review to stdout instead of posting |
| `--use-agent` | false | Use Claude Code agent (also reads `USE_AGENT` env var) |
| `--github-token` | `$GH_PAT` | GitHub token |

---

## Feature 3: CI Status Check for a PR

**Script**: `scripts/check_ci_for_pr.py`
**Workflow**: `.github/workflows/ci-status-check.yml`

### What it does

**In API mode**: Fetches PR's head SHA, collects workflow status, downloads logs from failed jobs in parallel, extracts errors structurally, runs a single LLM call for PR correlation analysis, outputs a merged table.

**In agent mode**: Gives the agent the PR number. The agent queries the GitHub API for all CI status, downloads logs from failed jobs, reads source code to understand if failures relate to the PR changes, and produces a complete report with per-job verdicts.

### How to use

**Via `@amd-bot` comment on a sglang PR:**

```
@amd-bot ci-status
```

**Via command line:**

```bash
# API mode
python scripts/check_ci_for_pr.py 1234 --no-post

# Agent mode
python scripts/check_ci_for_pr.py 1234 --no-post --use-agent
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | (required) | PR number to check |
| `--no-post` | false | Print to stdout instead of posting |
| `--use-agent` | false | Use Claude Code agent (also reads `USE_AGENT` env var) |
| `--github-token` | `$GH_PAT` | GitHub token |

---

## Feature 4: Comment Watcher

**Script**: `scripts/watch_comments.py`
**Workflow**: `.github/workflows/comment-watcher.yml`

### What it does

1. Polls the sglang repo for recent issue/PR comments
2. Filters for comments by authorized users containing the `@amd-bot` trigger
3. Parses the command (`review`, `review-focus`, `ci-status`, `help`)
4. Verifies the comment is on a PR (not a plain issue)
5. Checks if the comment has already been claimed (via `rocket` reaction from `amd-bot`) — if so, skips it (idempotency)
6. Adds a `rocket` reaction to claim the comment, then `eyes` to acknowledge
7. Dispatches the appropriate workflow via `repository_dispatch` event with `comment_id` for downstream deduplication

### Supported commands

| Command | Action |
|---------|--------|
| `@amd-bot review` | Full code review of the PR |
| `@amd-bot review-focus <areas>` | Focused review on specific areas |
| `@amd-bot ci-status` | Check and analyze CI status |
| `@amd-bot help` | Post a help message with available commands |

### Running modes

#### Mode 1: Daemon (recommended)

Run as a persistent background process on your self-hosted runner. Polls every 15 seconds for near-instant response to `@amd-bot` commands. Automatically started on runner-1 when deployed via `setup.sh`.

Resource usage is minimal: ~25 MB memory, ~0% CPU, 120 GitHub API calls/hour (2.4% of rate limit).

#### Mode 2: GitHub Actions cron (fallback)

The workflow runs every 5 minutes as a fallback in case the daemon is not running. Both modes share idempotency via GitHub reactions, so running both simultaneously is safe.

### Authorization

Only users listed in `AUTHORIZED_USERS` in `watch_comments.py` can trigger commands. Currently:

```python
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw"]
```

---

## Applying Changes

| What changed | Workflows (cron + dispatch) | Daemon (runner-1) |
|---|---|---|
| `scripts/*.py` | `git push` — effective on next run | `docker restart amd-ci-bot-runner-1` |
| `.github/workflows/*.yml` | `git push` — effective immediately | N/A |
| `runner/entrypoint.sh` | N/A | `git pull` on host + `docker restart amd-ci-bot-runner-1` (bind-mounted) |
| `runner/Dockerfile` | N/A | Rebuild image + recreate containers |
| GitHub Actions secrets | Effective immediately | N/A (daemon uses container env) |
| `BOT_PAT` or `LLM_GATEWAY_KEY` | N/A | Recreate containers via `setup.sh` |
| Enable/disable agent mode | Set `vars.USE_AGENT` in repo settings | Re-run `setup.sh` with/without `--use-agent` |

---

## Local Development

### Initial setup

```bash
git clone https://github.com/bingxche/sglang-ci-bot.git
cd sglang-ci-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set up secrets
mkdir -p .secrets
echo 'your_gateway_key' > .secrets/llm_gateway_key
echo 'your_github_pat' > .secrets/gh_pat
```

### Using agent mode locally

```bash
# Ensure Claude Code is installed and configured
claude --version

# Set env vars
export GH_PAT="ghp_..."
export AGENT_WORKSPACE="/tmp/agent-workspace"

# Agent-powered review (no post)
python scripts/review_pr.py 1234 --use-agent --no-post

# Agent-powered CI check
python scripts/check_ci_for_pr.py 1234 --use-agent --no-post

# Agent-powered CI monitor
python scripts/monitor_ci.py --output stdout --hours-back 24 --use-agent
```

### Using API mode locally

```bash
export GH_PAT="ghp_..."
export LLM_GATEWAY_KEY="..."
export LLM_GATEWAY_URL="https://llm-api.amd.com/Anthropic"

python scripts/review_pr.py 1234 --no-post
python scripts/check_ci_for_pr.py 1234 --no-post
python scripts/monitor_ci.py --output stdout --hours-back 24
```

### Using `local_run.sh`

```bash
bash scripts/local_run.sh monitor stdout 24
bash scripts/local_run.sh review 1234
bash scripts/local_run.sh ci-status 1234
bash scripts/local_run.sh watch
bash scripts/local_run.sh help
```

---

## Project Structure

```
sglang-ci-bot/
  scripts/
    utils.py               Shared: GitHub API, Anthropic client, log parsing, Claude Code agent wrapper
    monitor_ci.py           CI failure monitor (daemon + one-shot, API + agent mode)
    check_ci_for_pr.py      PR CI status checker (API + agent mode)
    review_pr.py            PR code review (API + agent mode)
    watch_comments.py       Comment watcher / command dispatcher
    local_run.sh            Local dev runner (venv + secrets + logging)
  .github/workflows/
    ci-monitor.yml          CI monitor (cron every 8h + manual, supports use_agent input)
    ci-status-check.yml     PR CI check (repository_dispatch, reads vars.USE_AGENT)
    pr-review.yml           PR review (repository_dispatch, reads vars.USE_AGENT)
    comment-watcher.yml     Comment poller (cron every 5min)
  runner/
    Dockerfile              Runner image: Python 3.12 + Node.js 22 + Claude Code + GitHub Actions runner
    setup.sh                Multi-runner deployment (--use-agent flag for agent mode)
    entrypoint.sh           Container entrypoint (register + daemons + agent repo setup)
  .state/                   Persisted state files (gitignored, cached in Actions)
  .secrets/                 Local secret files (gitignored)
  requirements.txt          Python dependencies (anthropic, httpx, requests)
```

---

## Environment Variables

| Variable | Required by | Description |
|----------|-------------|-------------|
| `GH_PAT` / `BOT_PAT` | All scripts | GitHub token for API access and posting comments |
| `LLM_GATEWAY_KEY` | API mode only | AMD LLM Gateway subscription key |
| `LLM_GATEWAY_URL` | API mode only | AMD LLM Gateway endpoint |
| `USE_AGENT` | Optional | Set to `true` to enable agent mode (same as `--use-agent`) |
| `AGENT_WORKSPACE` | Agent mode | Base directory for sglang clone (default: `/workspace`) |
| `ANTHROPIC_API_KEY` | Agent mode | Set to `dummy` (Claude Code uses gateway, not direct API) |
| `ANTHROPIC_BASE_URL` | Agent mode | LLM Gateway endpoint for Claude Code |
| `ANTHROPIC_CUSTOM_HEADERS` | Agent mode | Gateway auth header (injected at runtime, never in image) |
| `ANTHROPIC_MODEL` | Agent mode | Claude Code model selector |
| `COMMENT_AUTHOR` | Workflows | Set by watcher, displayed in comment header |

---

## Customization

### Monitored Workflows

Edit `MONITORED_WORKFLOWS` in `scripts/monitor_ci.py`:

```python
MONITORED_WORKFLOWS = [
    "nightly-test-amd.yml",
    "nightly-test-amd-rocm720.yml",
    # add or remove workflows here
]
```

### Claude Model (API mode)

Edit `CLAUDE_MODEL` in `scripts/utils.py`:

```python
CLAUDE_MODEL = "claude-opus-4-6"
```

### Claude Code Model (Agent mode)

Set via `ANTHROPIC_MODEL` env var or in the Docker image. Default: `opus[1m]`.

### Bot Identity

Edit `BOT_LOGIN` in `scripts/watch_comments.py`:

```python
BOT_LOGIN = "amd-bot"
```

### Authorized Users

Edit `AUTHORIZED_USERS` in `scripts/watch_comments.py`:

```python
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw"]
```

### Agent Behavior

- **Max turns**: `max_turns` in `claude_code_analyze()` (default: 15 for monitor/ci-check, 20 for review)
- **Timeout**: `timeout_secs` (default: 600 seconds / 10 minutes)
- **Workspace**: `AGENT_WORKSPACE` env var (default: `/workspace`)
- **Parallelism**: Agent mode uses 2 parallel workers (vs 3 for API mode)

### Schedules

- `ci-monitor.yml`: `'0 0,8,16 * * *'` (every 8 hours)
- `comment-watcher.yml`: `'*/5 * * * *'` (every 5 minutes)

### CI Monitor Polling Intervals

```python
IDLE_POLL_INTERVAL = 300   # 5 min — no active runs
ACTIVE_POLL_INTERVAL = 60  # 60s — tracking in-progress runs
```
