#!/bin/bash
# Local runner for amd-bot
# Sets up environment and runs the specified script

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"

# Activate venv
source "$VENV_DIR/bin/activate"

# AMD Gateway config
export ANTHROPIC_BASE_URL="https://llm-api.amd.com/Anthropic"
export LLM_GATEWAY_KEY="${LLM_GATEWAY_KEY:-}"
export GH_PAT="${GH_PAT:-}"

# Read secrets from files if not in env
if [ -z "$LLM_GATEWAY_KEY" ] && [ -f "$PROJECT_DIR/.secrets/llm_gateway_key" ]; then
    export LLM_GATEWAY_KEY="$(cat "$PROJECT_DIR/.secrets/llm_gateway_key")"
fi
if [ -z "$GH_PAT" ] && [ -f "$PROJECT_DIR/.secrets/gh_pat" ]; then
    export GH_PAT="$(cat "$PROJECT_DIR/.secrets/gh_pat")"
fi

if [ -z "$LLM_GATEWAY_KEY" ]; then
    echo "ERROR: LLM_GATEWAY_KEY not set."
    echo "  Run: echo 'your_key' > $PROJECT_DIR/.secrets/llm_gateway_key"
    echo "  Or:  export LLM_GATEWAY_KEY='your_key'"
    exit 1
fi
if [ -z "$GH_PAT" ]; then
    echo "ERROR: GH_PAT not set."
    echo "  Run: echo 'your_token' > $PROJECT_DIR/.secrets/gh_pat"
    echo "  Or:  export GH_PAT='your_token'"
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)

case "${1:-help}" in
    monitor)
        echo "[$(date)] Running CI monitor..."
        python "$SCRIPT_DIR/monitor_ci.py" \
            --output "${2:-issue}" \
            --bot-repo "${BOT_REPO:-bingxche/sglang-ci-bot}" \
            --hours-back "${3:-24}" \
            2>&1 | tee "$LOG_DIR/monitor_${TIMESTAMP}.log"
        ;;
    review)
        if [ -z "${2:-}" ]; then
            echo "Usage: $0 review <pr_number> [focus]"
            exit 1
        fi
        echo "[$(date)] Reviewing PR #$2..."
        ARGS="$2"
        [ -n "${3:-}" ] && ARGS="$ARGS --focus \"$3\""
        eval python "$SCRIPT_DIR/review_pr.py" $ARGS \
            2>&1 | tee "$LOG_DIR/review_pr${2}_${TIMESTAMP}.log"
        ;;
    ci-status)
        if [ -z "${2:-}" ]; then
            echo "Usage: $0 ci-status <pr_number>"
            exit 1
        fi
        echo "[$(date)] Checking CI for PR #$2..."
        python "$SCRIPT_DIR/check_ci_for_pr.py" "$2" \
            2>&1 | tee "$LOG_DIR/ci_status_pr${2}_${TIMESTAMP}.log"
        ;;
    watch)
        echo "[$(date)] Watching for comments..."
        python "$SCRIPT_DIR/watch_comments.py" \
            --bot-repo "${BOT_REPO:-bingxche/sglang-ci-bot}" \
            --since-hours "${2:-1}" \
            2>&1 | tee "$LOG_DIR/watch_${TIMESTAMP}.log"
        ;;
    help)
        echo "Usage: $0 <command> [args]"
        echo ""
        echo "Commands:"
        echo "  monitor [output_mode] [hours_back]  - Monitor CI failures (default: issue, 24h)"
        echo "  review <pr_number> [focus]           - Review a PR"
        echo "  ci-status <pr_number>                - Check CI status for a PR"
        echo "  watch [hours_back]                   - Watch for bot commands in comments"
        echo ""
        echo "Examples:"
        echo "  $0 monitor                    # Monitor last 24h, create issues"
        echo "  $0 monitor stdout 48          # Monitor last 48h, print to terminal"
        echo "  $0 review 1234                # Review PR #1234"
        echo "  $0 review 1234 'AMD ROCm'     # Review with focus area"
        echo "  $0 ci-status 1234             # Check CI for PR #1234"
        ;;
esac
