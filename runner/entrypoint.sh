#!/bin/bash
set -euo pipefail

# Export ANTHROPIC_CUSTOM_HEADERS if set (injected at runtime via -e flag)
export ANTHROPIC_CUSTOM_HEADERS="${ANTHROPIC_CUSTOM_HEADERS:-}"

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

# Clone/update bot repo once (shared by watcher)
if [ "${ENABLE_WATCHER:-}" = "true" ]; then
    BOT_REPO_URL="https://${GH_PAT}@github.com/${REPO_PATH}.git"
    if [ -d /tmp/bot/.git ]; then
        echo "Syncing bot repo to origin/main..."
        git -C /tmp/bot remote set-url origin "${BOT_REPO_URL}"
        git -C /tmp/bot fetch --prune origin main
        git -C /tmp/bot reset --hard origin/main
        git -C /tmp/bot clean -fd
    else
        rm -rf /tmp/bot
        git clone --branch main "${BOT_REPO_URL}" /tmp/bot
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

    echo "Starting CI monitor trigger (every 30 minutes)..."
    (
        while true; do
            sleep 1800
            curl -fsSL -X POST \
                -H "Authorization: token ${GH_PAT}" \
                -H "Accept: application/vnd.github+json" \
                "https://api.github.com/repos/${REPO_PATH}/actions/workflows/ci-monitor.yml/dispatches" \
                -d '{"ref":"main"}' || true
        done
    ) &
fi

exec ./run.sh
