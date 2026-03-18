# sglang-ci-bot

Automated CI monitoring and PR review bot for [sglang](https://github.com/sgl-project/sglang), powered by Claude.

Designed for collaborators who don't have admin access to install GitHub Apps — runs entirely from a **separate personal repo** using GitHub Actions + Claude API + GitHub API.

## Features

### 1. CI Failure Monitor
- Runs on a schedule (every 8 hours by default)
- Monitors nightly and PR test workflows for failures
- Downloads job logs from failed runs
- Uses Claude to analyze root causes and suggest fixes
- Creates GitHub issues in this repo with analysis reports

### 2. PR Review
- Manually trigger a code review for any sglang PR
- Fetches PR diff and metadata via GitHub API
- Claude provides structured review (bugs, performance, security, testing)
- Posts review as a comment on the PR

### 3. Comment Watcher
- Polls sglang PR comments every 15 minutes
- Responds to `@sglang-ci-bot` mentions with commands:
  - `@sglang-ci-bot review` — full code review
  - `@sglang-ci-bot review-focus <areas>` — focused review
  - `@sglang-ci-bot ci-status` — summarize CI failures
  - `@sglang-ci-bot help` — show available commands

### 4. CI Status Check
- Check CI status for a specific PR
- Summarizes passed/failed/pending checks
- Analyzes failure logs with Claude

## Setup

### Prerequisites
- A GitHub account with collaborator access to sglang
- A Claude API key ([console.anthropic.com](https://console.anthropic.com))
- A GitHub Personal Access Token (PAT)

### Step 1: Create Your Bot Repo

```bash
# Fork or create a new repo from this template
gh repo create sglang-ci-bot --private --source=. --push
```

### Step 2: Create a GitHub Personal Access Token

1. Go to [github.com/settings/tokens](https://github.com/settings/tokens?type=beta) (Fine-grained tokens recommended)
2. Create a new token with these permissions:
   - **Repository access**: Select `sgl-project/sglang` + your bot repo
   - **Permissions**:
     - `Actions`: Read (to fetch workflow runs and logs)
     - `Issues`: Read and Write (to post comments)
     - `Pull requests`: Read and Write (to read PR data and post reviews)
     - `Contents`: Read (to fetch file contents)
3. Copy the token

### Step 3: Configure Secrets

In your bot repo, go to **Settings → Secrets and variables → Actions** and add:

| Secret Name | Value |
|---|---|
| `GH_PAT` | Your GitHub Personal Access Token |
| `ANTHROPIC_API_KEY` | Your Claude API key |

### Step 4: Enable Workflows

Push the code to your repo. GitHub Actions will automatically pick up the workflow files.

```bash
git add -A
git commit -m "Initial setup"
git push origin main
```

## Usage

### Manual PR Review

```bash
# Via GitHub CLI
gh workflow run pr-review.yml -f pr_number=1234

# With focus areas
gh workflow run pr-review.yml -f pr_number=1234 -f focus="AMD ROCm compatibility"
```

### Manual CI Monitor

```bash
# Check last 24 hours
gh workflow run ci-monitor.yml

# Check specific time window
gh workflow run ci-monitor.yml -f hours_back=48

# Monitor specific workflows
gh workflow run ci-monitor.yml -f workflows="nightly-test-amd.yml,pr-test-amd.yml"
```

### CI Status for a PR

```bash
gh workflow run ci-status-check.yml -f pr_number=1234
```

### Comment-based Commands

On any sglang PR, post a comment:
```
@sglang-ci-bot review
```

The bot checks every 15 minutes and will respond with a review.

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export GH_PAT="ghp_xxxx"
export ANTHROPIC_API_KEY="sk-ant-xxxx"

# Run CI monitor locally
python scripts/monitor_ci.py --output stdout --hours-back 24

# Review a PR locally (print to stdout)
python scripts/review_pr.py 1234 --no-post

# Check CI status locally
python scripts/check_ci_for_pr.py 1234 --no-post
```

## Customization

### Monitored Workflows

Edit `MONITORED_WORKFLOWS` in `scripts/monitor_ci.py`:

```python
MONITORED_WORKFLOWS = [
    "nightly-test-amd.yml",
    "nightly-test-nvidia.yml",
    "pr-test-amd.yml",
    # Add more as needed
]
```

### Claude Model

Change `CLAUDE_MODEL` in any script to use a different model:

```python
CLAUDE_MODEL = "claude-sonnet-4-20250514"  # or "claude-opus-4-20250514", etc.
```

### Bot Trigger Keyword

Edit `BOT_TRIGGER` in `scripts/watch_comments.py`:

```python
BOT_TRIGGER = "@sglang-ci-bot"  # Change to your preferred trigger
```

### Schedule

Edit the cron expressions in `.github/workflows/`:
- `ci-monitor.yml`: Default every 8 hours
- `comment-watcher.yml`: Default every 15 minutes

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Your Personal Repo (sglang-ci-bot)                  │
│                                                      │
│  ┌─────────────────┐  ┌──────────────────┐           │
│  │  CI Monitor      │  │  Comment Watcher │           │
│  │  (scheduled)     │  │  (every 15 min)  │           │
│  └────────┬────────┘  └────────┬─────────┘           │
│           │                    │                      │
│           ▼                    ▼                      │
│  ┌─────────────────────────────────────┐              │
│  │         GitHub API (REST)           │              │
│  │  - Fetch workflow runs & logs       │              │
│  │  - Fetch PR diffs & metadata        │              │
│  │  - Read/post comments               │              │
│  └────────────────┬────────────────────┘              │
│                   │                                   │
│                   ▼                                   │
│  ┌─────────────────────────────────────┐              │
│  │         Claude API (Anthropic)      │              │
│  │  - Analyze CI failure logs          │              │
│  │  - Review PR code changes           │              │
│  │  - Suggest fixes                    │              │
│  └────────────────┬────────────────────┘              │
│                   │                                   │
│                   ▼                                   │
│  ┌─────────────────────────────────────┐              │
│  │      Post Results via GitHub API    │              │
│  │  - PR comments (reviews)            │              │
│  │  - Issues (CI reports)              │              │
│  └─────────────────────────────────────┘              │
└──────────────────────────────────────────────────────┘
```

## Cost Considerations

- **Claude API**: ~$0.01-0.05 per CI analysis, ~$0.02-0.10 per PR review (depends on diff size)
- **GitHub Actions**: Free for public repos; 2000 min/month for private repos (free tier)
- **Comment watcher**: ~96 runs/day × ~30s each ≈ 48 min/day of Actions time

## Limitations

- Comment watcher has up to 15-minute latency (polling-based, not webhook)
- Large PRs (>3000 lines) may have truncated diffs
- GitHub API rate limits: 5000 requests/hour with PAT
- As a collaborator (not admin), you cannot install GitHub Apps or configure repo webhooks directly
