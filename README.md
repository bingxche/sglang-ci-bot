# sglang-ci-bot

Automated CI monitoring and PR review bot for [sglang](https://github.com/sgl-project/sglang), powered by Claude via AMD LLM Gateway.

Runs entirely from a **separate personal repo** using GitHub Actions + GitHub REST API + Claude API. No admin access to the target repo required — only collaborator-level read access.

---

## Table of Contents

- [Features Overview](#features-overview)
- [Setup](#setup)
- [Feature 1: CI Failure Monitor](#feature-1-ci-failure-monitor)
- [Feature 2: PR Code Review](#feature-2-pr-code-review)
- [Feature 3: CI Status Check for a PR](#feature-3-ci-status-check-for-a-pr)
- [Feature 4: Comment Watcher](#feature-4-comment-watcher)
- [Local Development](#local-development)
- [Project Structure](#project-structure)
- [Customization](#customization)

---

## Features Overview

| Feature | Script | Trigger | What it does |
|---------|--------|---------|--------------|
| CI Failure Monitor | `monitor_ci.py` | Cron (every 8h) or manual | Monitors nightly CI workflows, analyzes failures with progressive step-by-step analysis, posts daily issue reports |
| PR Code Review | `review_pr.py` | `@amd-bot review` or manual | Fetches PR diff, sends to Claude for structured code review, posts as PR comment |
| CI Status Check | `check_ci_for_pr.py` | `@amd-bot ci-status` or manual | Checks all CI checks for a PR, downloads failure logs, analyzes with Claude |
| Comment Watcher | `watch_comments.py` | Cron (every 5min) | Polls sglang PRs for `@amd-bot` commands, dispatches the appropriate action |

---

## Setup

### 1. Prerequisites

- GitHub account with collaborator access to `sgl-project/sglang`
- AMD LLM Gateway subscription key and endpoint URL
- GitHub Personal Access Token (PAT) with these permissions on `sgl-project/sglang` + your bot repo:
  - `Actions`: Read (fetch workflow runs and logs)
  - `Issues`: Read & Write (post comments, create issues)
  - `Pull requests`: Read & Write (read PR data, post reviews)
  - `Contents`: Read (fetch file contents)

### 2. Configure Repository Secrets

In your bot repo, go to **Settings > Secrets and variables > Actions** and add:

| Secret | Value |
|--------|-------|
| `GH_PAT` | Your GitHub Personal Access Token |
| `LLM_GATEWAY_KEY` | AMD LLM Gateway subscription key |
| `LLM_GATEWAY_URL` | AMD LLM Gateway endpoint (e.g. `https://llm-api.amd.com/Anthropic`) |

### 3. Enable Workflows

Push the code to your repo. GitHub Actions will automatically pick up the workflow files and start running on their cron schedules.

---

## Feature 1: CI Failure Monitor

**Script**: `scripts/monitor_ci.py`
**Workflow**: `.github/workflows/ci-monitor.yml`
**Schedule**: Every 8 hours (0:00, 8:00, 16:00 UTC)

### What it does

1. Queries the GitHub API for failed workflow runs in the monitored workflows (last 24 hours by default)
2. Skips runs that have already been analyzed (tracked via `.state/ci_monitor.json`)
3. For each failed job, downloads the **full job log** (no character limit)
4. Parses the log into individual steps using GitHub Actions `##[group]` / `##[endgroup]` markers
5. Runs **progressive step-by-step analysis**:
   - Every step (including passed ones) is sent to Claude for summarization
   - Each step's summary is accumulated and passed as context to the next step
   - This means Claude has full context when analyzing a failed test step — it knows the Docker image version, installed dependency versions, environment config, etc.
   - If a single step's log exceeds 150K characters, a regex pre-filter extracts error-relevant sections (ERROR, FAIL, Traceback, etc.) with surrounding context
6. Produces a final root-cause analysis for each job based on the complete accumulated summary
7. If multiple jobs failed in the same workflow, performs a cross-job analysis to find common patterns
8. Outputs the report

### Monitored Workflows

```
nightly-test-amd.yml
nightly-test-amd-rocm720.yml
release-docker-amd-nightly.yml
release-docker-amd-rocm720-nightly.yml
amd-aiter-scout.yml
```

### Output Modes

| Mode | Description |
|------|-------------|
| `stdout` | Print the analysis report to the terminal (for local testing) |
| `daily-issue` | Find or create a daily issue `[CI Monitor] Daily Report - YYYY-MM-DD` in the bot repo, append each workflow's report as a comment |

### How to use

**Via GitHub Actions (automatic):**

The workflow runs automatically every 8 hours. To trigger manually:

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
python scripts/monitor_ci.py --output daily-issue --bot-repo user/sglang-ci-bot --hours-back 24

# Monitor specific workflows only
python scripts/monitor_ci.py --output stdout --workflows nightly-test-amd.yml amd-aiter-scout.yml

# Only analyze a specific job by name
python scripts/monitor_ci.py --output stdout --job-name nightly-8-gpu-grok2

# Save to a local file for review
python scripts/monitor_ci.py --output stdout --hours-back 48 2>&1 | tee report.md
```

**CLI options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--output` | `stdout` | Output mode: `stdout` or `daily-issue` |
| `--hours-back` | `24` | How many hours back to search for failures |
| `--workflows` | (all monitored) | Space-separated list of workflow files to check |
| `--job-name` | (none) | Only analyze jobs whose name contains this string |
| `--bot-repo` | (none) | Bot repo for posting issues (required for `daily-issue` mode) |
| `--github-token` | `$GH_PAT` | GitHub token (can also set via env var) |

### State Management

Processed run IDs are stored in `.state/ci_monitor.json` to avoid re-analyzing the same failures. In GitHub Actions, this state is persisted across runs using `actions/cache`.

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
2. Gets all check runs for that commit (passed, failed, pending)
3. For up to 5 failed checks, downloads the full job logs
4. If there are failures, sends the check summary and failure logs to Claude for analysis
5. Claude determines: what failed, root causes, suggested fixes, and whether failures are PR-related or pre-existing
6. Posts the analysis as a comment on the PR (or prints to stdout)

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
**Schedule**: Every 5 minutes

### What it does

1. Polls the sglang repo for recent issue/PR comments (last 1 hour by default)
2. Filters for comments by authorized users containing the `@amd-bot` trigger
3. Parses the command (`review`, `review-focus`, `ci-status`, `help`)
4. Verifies the comment is on a PR (not a plain issue)
5. Adds an "eyes" reaction to acknowledge the command
6. Dispatches the appropriate workflow via `repository_dispatch` event

### Supported commands

| Command | Action |
|---------|--------|
| `@amd-bot review` | Full code review of the PR |
| `@amd-bot review-focus <areas>` | Focused review on specific areas |
| `@amd-bot ci-status` | Check and analyze CI status |
| `@amd-bot help` | Post a help message with available commands |

### How to use

The comment watcher runs automatically every 5 minutes. To trigger manually:

```bash
# Check comments from the last 1 hour
gh workflow run comment-watcher.yml

# Check further back
gh workflow run comment-watcher.yml -f since_hours=4
```

**Via command line:**

```bash
python scripts/watch_comments.py --bot-repo user/sglang-ci-bot --since-hours 1
```

### Authorization

Only users listed in `AUTHORIZED_USERS` in `watch_comments.py` can trigger commands. Currently: `["bingxche", "yctseng0211", "michaelzhang-ai"]`.

### State Management

Processed comment IDs are stored in `.state/last_check.json` to avoid re-processing the same commands.

---

## Local Development

### Initial setup

```bash
# Clone the repo
git clone https://github.com/user/sglang-ci-bot.git
cd sglang-ci-bot

# Create venv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set up secrets
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
    monitor_ci.py          CI failure monitor (progressive step-by-step analysis)
    check_ci_for_pr.py     PR CI status checker
    review_pr.py           PR code review
    watch_comments.py      Comment watcher / command dispatcher
    local_run.sh           Local dev runner (venv + secrets + logging)
  .github/workflows/
    ci-monitor.yml         Scheduled CI monitor (cron every 8h)
    ci-status-check.yml    PR CI check (triggered by repository_dispatch)
    pr-review.yml          PR review (triggered by repository_dispatch)
    comment-watcher.yml    Comment poller (cron every 5min)
  runner/
    Dockerfile             Self-hosted runner Docker image
    setup.sh               Runner registration and setup
    entrypoint.sh          Runner container entrypoint
  .state/                  Persisted state files (gitignored, cached in Actions)
  .secrets/                Local secret files (gitignored)
  requirements.txt         Python dependencies (anthropic, httpx, requests)
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

Edit `CLAUDE_MODEL` in any script:

```python
CLAUDE_MODEL = "claude-opus-4-6"  # default
```

### Bot Trigger Keyword

Edit `BOT_TRIGGER` in `scripts/watch_comments.py`:

```python
BOT_TRIGGER = "@amd-bot"
```

### Authorized Users

Edit `AUTHORIZED_USERS` in `scripts/watch_comments.py`:

```python
AUTHORIZED_USERS = ["bingxche", "yctseng0211", "michaelzhang-ai"]
```

### Schedules

Edit the `cron` expressions in the workflow files:

- `ci-monitor.yml`: `'0 0,8,16 * * *'` (every 8 hours)
- `comment-watcher.yml`: `'*/5 * * * *'` (every 5 minutes)

### Pre-filter Threshold

If Claude's context window changes or you want to adjust when regex pre-filtering kicks in, edit `STEP_LOG_PREFILTER_THRESHOLD` in `monitor_ci.py`:

```python
STEP_LOG_PREFILTER_THRESHOLD = 150_000  # characters per step
```
