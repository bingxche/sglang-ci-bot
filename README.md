# sglang-ci-bot

Automated CI monitoring and PR review bot for [sglang](https://github.com/sgl-project/sglang), powered by Claude via AMD LLM Gateway.

Runs entirely from a **separate personal repo** (`bingxche/sglang-ci-bot`) using GitHub Actions + GitHub REST API + Claude API. No admin access to the target repo required — sglang is a public repo, so any authenticated GitHub user can read data and post comments.

All public-facing replies (PR comments, reactions, CI reports) are posted under the dedicated **[amd-bot](https://github.com/amd-bot)** GitHub account.

---

## Table of Contents

- [Features Overview](#features-overview)
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
| CI Failure Monitor | `monitor_ci.py` | Daemon (60s) + Cron (8h fallback) | Monitors CI workflows in real-time, analyzes failures with progressive step-by-step analysis, posts/updates daily issue comments. Detects in-progress run failures immediately. |
| PR Code Review | `review_pr.py` | `@amd-bot review` or manual | Fetches PR diff, sends to Claude for structured code review, posts as PR comment |
| CI Status Check | `check_ci_for_pr.py` | `@amd-bot ci-status` or manual | Checks all CI for a PR, concurrently analyzes failures with Claude, correlates with PR changes |
| Comment Watcher | `watch_comments.py` | Daemon (15s) + Cron (5min fallback) | Polls sglang PRs for `@amd-bot` commands, dispatches with reaction-based idempotency |

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
| Daemon CI monitor (runner-1 container) | `BOT_PAT` env var (amd-bot's PAT) + `LLM_GATEWAY_*` env vars | amd-bot |
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

### 2. Configure Repository Secrets

In `bingxche/sglang-ci-bot`, go to **Settings > Secrets and variables > Actions** and add:

| Secret | Value |
|--------|-------|
| `GH_PAT` | **amd-bot's** GitHub PAT (all workflows use this to post as amd-bot) |
| `LLM_GATEWAY_KEY` | AMD LLM Gateway subscription key |
| `LLM_GATEWAY_URL` | AMD LLM Gateway endpoint (e.g. `https://llm-api.amd.com/Anthropic`) |

> **Note**: The `GH_PAT` secret is amd-bot's PAT, not bingxche's. Bingxche's PAT is only used on runner machines for registration (passed via `--pat` to `setup.sh`).

### 3. Deploy Self-Hosted Runner

All workflows run on self-hosted runners with the `amd-internal` label. The `runner/setup.sh` script builds a Docker image and spawns multiple runner containers (default: 10) so jobs can execute in parallel.

#### New node deployment (one command)

```bash
# Clone the repo
git clone https://github.com/bingxche/sglang-ci-bot.git
cd sglang-ci-bot

# First time: build image and start 10 runners (with CI monitor daemon)
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --llm-gateway-key <KEY> --build

# Or on other machines: pull pre-built image from Docker Hub (no build needed)
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --llm-gateway-key <KEY> --image bingxche/sglang-ci-bot-runner:latest

# Without CI monitor daemon (comment watcher only)
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --build
```

- `--pat`: bingxche's PAT (used for runner registration, requires repo admin)
- `--bot-pat`: amd-bot's PAT (used by the daemon comment watcher to post as amd-bot)
- `--llm-gateway-key`: AMD LLM Gateway key (enables CI monitor daemon on runner-1; omit to disable)
- `--llm-gateway-url`: (optional) LLM Gateway endpoint (defaults to `https://llm-api.amd.com/Anthropic`)

This creates containers `amd-ci-bot-runner-1` through `amd-ci-bot-runner-10`:
- **Runner-1** runs two background daemons:
  - **Comment watcher** (`ENABLE_WATCHER=true`) — polls for `@amd-bot` commands every 15 seconds
  - **CI monitor** (`ENABLE_CI_MONITOR=true`, if `--llm-gateway-key` provided) — polls for CI failures every 60 seconds, analyzes immediately
- **Runners 2-10** are plain job executors
- All runners register with GitHub Actions using bingxche's PAT (admin access)
- `entrypoint.sh` is bind-mounted from the host repo, so changes to it take effect on `docker restart` without rebuilding the image

#### Custom options

```bash
# Custom runner count
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --count 5 --build

# Custom runner name prefix
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT> --name my-runner --build
```

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
4. For each failed job, downloads the **full job log** (no character limit)
5. Parses the log into individual steps using GitHub Actions `##[group]` / `##[endgroup]` markers
6. Runs **progressive step-by-step analysis**:
   - Every step (including passed ones) is sent to Claude for summarization
   - Each step's summary is accumulated and passed as context to the next step
   - This means Claude has full context when analyzing a failed test step — it knows the Docker image version, installed dependency versions, environment config, etc.
   - If a single step's log exceeds 150K characters, a regex pre-filter extracts error-relevant sections (ERROR, FAIL, Traceback, etc.) with surrounding context
7. **Analyzes multiple jobs in parallel** (up to 3 concurrent workers via ThreadPoolExecutor)
8. Produces a final root-cause analysis for each job based on the complete accumulated summary
9. If multiple jobs failed in the same workflow, performs a cross-job analysis to find common patterns
10. **Each workflow gets ONE comment** in the daily issue, updated via PATCH as new failures are discovered. In-progress runs show a "still running" indicator that's removed when the workflow completes

### Monitored Workflows

```
nightly-test-amd.yml
nightly-test-amd-rocm720.yml
release-docker-amd-nightly.yml
release-docker-amd-rocm720-nightly.yml
amd-aiter-scout.yml
pr-test-amd-rocm720.yml
```

### Output Modes

| Mode | Description |
|------|-------------|
| `stdout` | Print the analysis report to the terminal (for local testing) |
| `daily-issue` | Find or create a daily issue `[CI Monitor] Daily Report - YYYY-MM-DD` in the bot repo, append each workflow's report as a comment |

### Running modes

#### Mode 1: Daemon (recommended)

The CI monitor daemon runs as a persistent background process on runner-1. It polls every 60 seconds when tracking in-progress workflows, and every 5 minutes when idle. Automatically started when `--llm-gateway-key` is provided to `setup.sh`.

The daemon detects failures within seconds of a job completing, even if the overall workflow is still running.

#### Mode 2: GitHub Actions cron (fallback)

The workflow runs every 8 hours as a safety net. Both modes share deduplication via GitHub comment metadata (`<!-- processed_job_ids: ... -->`), so running both simultaneously is safe — the same job is never analyzed twice.

### How to use

**Via GitHub Actions (manual trigger):**

```bash
# Default: check last 24h, post to daily issue
gh workflow run ci-monitor.yml

# Custom time window
gh workflow run ci-monitor.yml -f hours_back=48

# Print to stdout only (no issue created)
gh workflow run ci-monitor.yml -f output_mode=stdout

# Monitor specific workflows only
gh workflow run ci-monitor.yml -f workflows="nightly-test-amd.yml,amd-aiter-scout.yml"
```

**Via command line:**

```bash
# Print analysis to stdout
python scripts/monitor_ci.py --output stdout --hours-back 24

# Post to daily issue in bot repo
python scripts/monitor_ci.py --output daily-issue --bot-repo bingxche/sglang-ci-bot --hours-back 24

# Monitor specific workflows only
python scripts/monitor_ci.py --output stdout --workflows nightly-test-amd.yml amd-aiter-scout.yml

# Only analyze a specific job by name
python scripts/monitor_ci.py --output stdout --job-name nightly-8-gpu-grok2

# Run as daemon (equivalent to what runner-1 does)
python scripts/monitor_ci.py --daemon --bot-repo bingxche/sglang-ci-bot

# Daemon with custom poll interval
python scripts/monitor_ci.py --daemon --bot-repo bingxche/sglang-ci-bot --poll-interval 30
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
| `--github-token` | `$GH_PAT` | GitHub token (can also set via env var) |

### State & Deduplication

Deduplication works across processes (daemon + cron) without shared filesystem:

1. **GitHub comment metadata (source of truth)**: Each workflow comment embeds `<!-- processed_job_ids: 111,222,333 -->` as an invisible HTML comment. Before analyzing, both daemon and cron read these IDs from the daily issue's comments to know which jobs have already been processed.
2. **Local state (cache)**: The daemon keeps `job_analyses` in `.state/ci_monitor.json` as a local cache for rebuilding comments on PATCH. The cron uses `actions/cache` for its own local state. Neither process depends on the other's local state.

This means if the daemon is down and the cron takes over, they won't duplicate work. When the daemon comes back, it reads the cron's comment metadata and picks up where it left off.

---

## Feature 2: PR Code Review

**Script**: `scripts/review_pr.py`
**Workflow**: `.github/workflows/pr-review.yml`

### What it does

1. Fetches the PR metadata (title, author, description, branch info)
2. Downloads the full PR diff in unified diff format (truncated at 120K chars if extremely large)
3. Gets the list of changed files with additions/deletions counts
4. Sends everything to Claude with a structured review prompt covering: summary, code quality, bugs, performance, security, testing, and overall assessment
5. Posts the review as a comment on the PR (or prints to stdout)

### How to use

**Via `@amd-bot` comment on a sglang PR:**

```
@amd-bot review
```

With a specific focus area:

```
@amd-bot review-focus AMD ROCm compatibility and memory management
```

**Via GitHub Actions:**

```bash
# Review a specific PR
gh workflow run pr-review.yml -f pr_number=1234

# With focus areas
gh workflow run pr-review.yml -f pr_number=1234 -f focus="AMD ROCm compatibility"

# Print to stdout only (don't post comment)
gh workflow run pr-review.yml -f pr_number=1234 -f no_post=true
```

**Via command line:**

```bash
# Review and post as comment
python scripts/review_pr.py 1234

# Review with focus area
python scripts/review_pr.py 1234 --focus "AMD ROCm compatibility"

# Print to stdout only (don't post)
python scripts/review_pr.py 1234 --no-post

# With additional context
python scripts/review_pr.py 1234 --context "This PR is part of the ROCm 7.2 migration"
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | (required) | PR number to review |
| `--focus` | (none) | Specific areas to focus on |
| `--context` | (none) | Additional context for the review |
| `--no-post` | false | Print review to stdout instead of posting |
| `--github-token` | `$GH_PAT` | GitHub token |

---

## Feature 3: CI Status Check for a PR

**Script**: `scripts/check_ci_for_pr.py`
**Workflow**: `.github/workflows/ci-status-check.yml`

### What it does

1. Fetches the PR's head commit SHA
2. Gets all workflow runs for that commit (passed, failed, pending)
3. For up to 8 failed jobs, downloads the full job logs and analyzes them **concurrently** (multiple jobs in parallel, ~6x faster than serial)
4. Each job is analyzed with progressive step-by-step analysis (same approach as the CI monitor)
5. Fetches the PR diff concurrently with job analysis
6. Runs a PR correlation analysis: Claude assesses whether each failure is likely caused by the PR changes or is pre-existing
7. If multiple jobs failed, performs cross-job analysis to find common patterns
8. Posts the analysis as a comment on the PR (or prints to stdout)

### How to use

**Via `@amd-bot` comment on a sglang PR:**

```
@amd-bot ci-status
```

**Via GitHub Actions:**

```bash
gh workflow run ci-status-check.yml -f pr_number=1234
```

**Via command line:**

```bash
# Check and post as PR comment
python scripts/check_ci_for_pr.py 1234

# Print to stdout only
python scripts/check_ci_for_pr.py 1234 --no-post
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `pr_number` | (required) | PR number to check |
| `--no-post` | false | Print to stdout instead of posting |
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

The comment watcher supports two running modes:

#### Mode 1: Daemon (recommended)

Run as a persistent background process on your self-hosted runner. Polls every 15 seconds for near-instant response to `@amd-bot` commands. Automatically started on runner-1 when deployed via `setup.sh`.

```bash
# Start in foreground (for testing)
export BOT_PAT=ghp_amd_bot_token
python3 scripts/watch_comments.py \
  --daemon \
  --poll-interval 15 \
  --bot-repo bingxche/sglang-ci-bot

# Start as background process
nohup python3 scripts/watch_comments.py \
  --daemon \
  --poll-interval 15 \
  --bot-repo bingxche/sglang-ci-bot \
  > /tmp/comment-watcher.log 2>&1 &

# Monitor logs
tail -f /tmp/comment-watcher.log

# Stop the daemon
pkill -f "watch_comments.py --daemon"
```

Resource usage is minimal: ~25 MB memory, ~0% CPU, 120 GitHub API calls/hour (2.4% of rate limit).

The daemon handles errors gracefully with exponential backoff and responds to SIGTERM/SIGINT for clean shutdown.

#### Mode 2: GitHub Actions cron (fallback)

The workflow runs every 5 minutes as a fallback in case the daemon is not running. Both modes share idempotency via GitHub reactions, so running both simultaneously is safe — the same comment is never processed twice.

```bash
# Trigger manually
gh workflow run comment-watcher.yml

# Check further back
gh workflow run comment-watcher.yml -f since_hours=4
```

#### One-shot via command line

```bash
python scripts/watch_comments.py --bot-repo bingxche/sglang-ci-bot --since-hours 1
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--bot-repo` | (required) | Bot repo for dispatching workflows |
| `--daemon` | false | Run as a long-lived daemon instead of one-shot |
| `--poll-interval` | `30` | Seconds between polls in daemon mode |
| `--since-hours` | `1` | How many hours back to check in one-shot mode |
| `--github-token` | `$BOT_PAT` or `$GH_PAT` | GitHub token (prefers `BOT_PAT` if set) |

### Authorization

Only users listed in `AUTHORIZED_USERS` in `watch_comments.py` can trigger commands. Currently:

```python
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw"]
```

### State Management

Two layers of idempotency prevent duplicate processing:

1. **Reaction-based (distributed)**: Before dispatching, the watcher checks if a `rocket` reaction from the `amd-bot` account exists on the comment. This works across both daemon and cron modes without shared filesystem.
2. **Local state (fast-path)**: Processed comment IDs are stored in `.state/last_check.json` to skip API calls for recently-seen comments.

---

## Applying Changes

Different types of changes require different steps to take effect:

| What changed | Workflows (cron + dispatch) | Daemon (runner-1) |
|---|---|---|
| `scripts/*.py` (e.g. `AUTHORIZED_USERS`, `CLAUDE_MODEL`, `MONITORED_WORKFLOWS`) | `git push` — effective on next run (workflows checkout fresh code) | `docker restart amd-ci-bot-runner-1` (entrypoint runs `git pull` on restart, restarts both comment watcher and CI monitor daemons) |
| `.github/workflows/*.yml` (schedules, secrets usage) | `git push` — effective immediately | N/A |
| `runner/entrypoint.sh` | N/A | `git pull` on host + `docker restart amd-ci-bot-runner-1` (bind-mounted) |
| `runner/Dockerfile` | N/A | `docker build -t sglang-ci-bot-runner:latest runner/` + recreate containers |
| GitHub Actions secrets (`GH_PAT`, `LLM_GATEWAY_*`) | Effective immediately on next workflow run | N/A (daemon uses `BOT_PAT` from container env) |
| Daemon PAT (`BOT_PAT`) | N/A | Must recreate runner-1 container: `bash runner/setup.sh --pat ... --bot-pat <new-PAT> ...` |
| LLM Gateway key (`LLM_GATEWAY_KEY`) | Effective immediately on next workflow run | Must recreate runner-1 container: `bash runner/setup.sh --pat ... --llm-gateway-key <new-KEY> ...` |

**Common scenarios:**

```bash
# Changed AUTHORIZED_USERS or other Python code
git push                                  # workflows pick up changes automatically
docker restart amd-ci-bot-runner-1        # daemon picks up on restart

# Changed entrypoint.sh
git pull                                  # on the runner machine
docker restart amd-ci-bot-runner-1        # bind-mounted, no rebuild needed

# Changed Dockerfile
docker build -t sglang-ci-bot-runner:latest runner/
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <amd-bot-PAT>   # recreate all containers

# Rotated amd-bot's PAT
# 1. Update GH_PAT secret in GitHub Actions (Settings > Secrets)
# 2. Recreate runner-1 with new BOT_PAT:
bash runner/setup.sh --pat <bingxche-PAT> --bot-pat <new-amd-bot-PAT> --image bingxche/sglang-ci-bot-runner:latest
```

---

## Local Development

### Initial setup

```bash
# Clone the repo
git clone https://github.com/bingxche/sglang-ci-bot.git
cd sglang-ci-bot

# Create venv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set up secrets (optional, for local_run.sh)
mkdir -p .secrets
echo 'your_gateway_key' > .secrets/llm_gateway_key
echo 'your_github_pat' > .secrets/gh_pat
```

### Using `local_run.sh`

The `local_run.sh` script handles venv activation, secret loading, and logging automatically:

```bash
bash scripts/local_run.sh monitor stdout 24       # CI monitor, stdout, last 24h
bash scripts/local_run.sh monitor daily-issue 48   # CI monitor, post issue, last 48h
bash scripts/local_run.sh review 1234              # Review PR #1234
bash scripts/local_run.sh review 1234 "AMD ROCm"   # Review with focus area
bash scripts/local_run.sh ci-status 1234            # Check CI for PR #1234
bash scripts/local_run.sh watch                     # Run comment watcher
bash scripts/local_run.sh help                      # Show all commands
```

Logs are saved to the `logs/` directory with timestamps.

### Running scripts directly

```bash
source .venv/bin/activate
export GH_PAT="ghp_..."
export LLM_GATEWAY_KEY="..."
export LLM_GATEWAY_URL="https://llm-api.amd.com/Anthropic"

python scripts/monitor_ci.py --output stdout --hours-back 24
python scripts/review_pr.py 1234 --no-post
python scripts/check_ci_for_pr.py 1234 --no-post
```

---

## Project Structure

```
sglang-ci-bot/
  scripts/
    utils.py               Shared GitHub API helpers, Anthropic client, log parsing
    monitor_ci.py           CI failure monitor (daemon + one-shot, parallel analysis, incremental comments)
    check_ci_for_pr.py      PR CI status checker (concurrent analysis + PR correlation)
    review_pr.py            PR code review
    watch_comments.py       Comment watcher / command dispatcher
    local_run.sh            Local dev runner (venv + secrets + logging)
  .github/workflows/
    ci-monitor.yml          CI monitor cron fallback (every 8h, safety net for daemon)
    ci-status-check.yml     PR CI check (triggered by repository_dispatch)
    pr-review.yml           PR review (triggered by repository_dispatch)
    comment-watcher.yml     Comment poller (cron every 5min, fallback for daemon)
  runner/
    Dockerfile              Self-hosted runner Docker image
    setup.sh                Multi-runner deployment (spawns N containers, default 10, enables daemons on runner-1)
    entrypoint.sh           Runner container entrypoint (register + comment watcher + CI monitor + run)
  .state/                   Persisted state files (gitignored, cached in Actions)
  .secrets/                 Local secret files (gitignored)
  requirements.txt          Python dependencies (anthropic, httpx, requests)
```

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

### Claude Model

Edit `CLAUDE_MODEL` in `scripts/utils.py`:

```python
CLAUDE_MODEL = "claude-opus-4-6"  # default
```

### Bot Identity

Edit `BOT_LOGIN` in `scripts/watch_comments.py`:

```python
BOT_LOGIN = "amd-bot"
BOT_TRIGGER = f"@{BOT_LOGIN}"
```

### Authorized Users

Edit `AUTHORIZED_USERS` in `scripts/watch_comments.py`. After pushing, restart runner-1 for the daemon to pick up the change:

```python
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai", "Jacob0226", "yichiche", "kkHuang-amd", "HaiShaw"]
```

### Schedules

Edit the `cron` expressions in the workflow files:

- `ci-monitor.yml`: `'0 0,8,16 * * *'` (every 8 hours, safety net for daemon)
- `comment-watcher.yml`: `'*/5 * * * *'` (every 5 minutes, fallback for daemon mode)

### CI Monitor Polling Intervals

Edit constants in `scripts/monitor_ci.py`:

```python
IDLE_POLL_INTERVAL = 300   # 5 min — no active runs
ACTIVE_POLL_INTERVAL = 60  # 60s — tracking in-progress runs
```

Or override the active interval at deploy time via `CI_MONITOR_POLL_INTERVAL` env var.

### Pre-filter Threshold

If Claude's context window changes or you want to adjust when regex pre-filtering kicks in, edit `STEP_LOG_PREFILTER_THRESHOLD` in `scripts/utils.py`:

```python
STEP_LOG_PREFILTER_THRESHOLD = 150_000  # characters per step
```
