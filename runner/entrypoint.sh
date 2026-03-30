#!/bin/bash
set -euo pipefail

REPO_URL="${REPO_URL:?REPO_URL is required}"
GH_PAT="${GH_PAT:?GH_PAT is required}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
LABELS="${LABELS:-self-hosted,amd-internal}"

WORKDIR_CLEANUP="${WORKDIR_CLEANUP:-true}"
if [ "$WORKDIR_CLEANUP" = "true" ] && [ -d "_work" ]; then
    echo "Cleaning up previous workspace..."
    rm -rf _work/*
fi

REPO_PATH="${REPO_URL#https://github.com/}"

echo "Requesting registration token for ${REPO_PATH}..."
RUNNER_TOKEN=$(curl -fsSL \
    -X POST \
    -H "Authorization: token ${GH_PAT}" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${REPO_PATH}/actions/runners/registration-token" \
    | jq -r .token)

if [ -z "$RUNNER_TOKEN" ] || [ "$RUNNER_TOKEN" = "null" ]; then
    echo "ERROR: Failed to get registration token. Check your GH_PAT." >&2
    exit 1
fi

cleanup() {
    echo "Removing runner..."
    ./config.sh remove --token "$RUNNER_TOKEN" 2>/dev/null || true
}
trap cleanup EXIT SIGTERM SIGINT

./config.sh \
    --url "$REPO_URL" \
    --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$LABELS" \
    --unattended \
    --replace

# Clone/update bot repo once (shared by watcher + CI monitor)
if [ "${ENABLE_WATCHER:-}" = "true" ] || [ "${ENABLE_CI_MONITOR:-}" = "true" ]; then
    if [ -d /tmp/bot ]; then
        git -C /tmp/bot pull --ff-only 2>/dev/null || true
    else
        git clone "https://${GH_PAT}@github.com/${REPO_PATH}.git" /tmp/bot
    fi
    pip install -q -r /tmp/bot/requirements.txt 2>/dev/null
fi

if [ "${ENABLE_WATCHER:-}" = "true" ]; then
    WATCHER_TOKEN="${BOT_PAT:-$GH_PAT}"
    echo "Starting comment watcher daemon (poll every ${POLL_INTERVAL:-15}s)..."
    BOT_PAT="${WATCHER_TOKEN}" python3 /tmp/bot/scripts/watch_comments.py \
        --daemon \
        --poll-interval "${POLL_INTERVAL:-15}" \
        --bot-repo "${REPO_PATH}" &
fi

if [ "${ENABLE_CI_MONITOR:-}" = "true" ]; then
    CI_MONITOR_TOKEN="${BOT_PAT:-$GH_PAT}"
    echo "Starting CI monitor daemon (active poll: ${CI_MONITOR_POLL_INTERVAL:-60}s)..."
    BOT_PAT="${CI_MONITOR_TOKEN}" \
    LLM_GATEWAY_KEY="${LLM_GATEWAY_KEY:?LLM_GATEWAY_KEY required for CI monitor}" \
    LLM_GATEWAY_URL="${LLM_GATEWAY_URL:-https://llm-api.amd.com/Anthropic}" \
    python3 /tmp/bot/scripts/monitor_ci.py \
        --daemon \
        --poll-interval "${CI_MONITOR_POLL_INTERVAL:-60}" \
        --bot-repo "${REPO_PATH}" &
fi

exec ./run.sh
