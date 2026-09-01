#!/bin/bash
set -euo pipefail

# Export ANTHROPIC_CUSTOM_HEADERS if set (injected at runtime via -e flag)
export ANTHROPIC_CUSTOM_HEADERS="${ANTHROPIC_CUSTOM_HEADERS:-}"

REPO_URL="${REPO_URL:?REPO_URL is required}"
CONTROL_TOKEN="${SGLANG_PAT:-${GH_PAT:-}}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
LABELS="${LABELS:-self-hosted,amd-internal}"

WORKDIR_CLEANUP="${WORKDIR_CLEANUP:-true}"
if [ "$WORKDIR_CLEANUP" = "true" ] && [ -d "_work" ]; then
    echo "Cleaning up previous workspace..."
    rm -rf _work/*
fi

REPO_PATH="${REPO_URL#https://github.com/}"
RUNNER_CONFIG_MARKER=".configured-by-sglang-ci-bot-entrypoint"

# Some historical local images accidentally contain a stale runner registration.
# Trust only registrations created by this entrypoint in the current container;
# otherwise discard the baked-in credentials and register with the supplied
# one-time token.  The marker survives an ordinary container restart.
if [ -f .runner ] && [ ! -f "${RUNNER_CONFIG_MARKER}" ]; then
    echo "Discarding stale runner configuration from the container image..."
    rm -f .runner .credentials .credentials_rsaparams .service
fi

if [ -f .runner ]; then
    echo "Existing runner configuration found; skipping registration."
else
    RUNNER_TOKEN="${RUNNER_REGISTRATION_TOKEN:-}"
    if [ -z "${RUNNER_TOKEN}" ]; then
        CONTROL_TOKEN="${CONTROL_TOKEN:?SGLANG_PAT or GH_PAT is required to register the runner}"
        echo "Requesting registration token for ${REPO_PATH}..."
        RUNNER_TOKEN=$(curl -fsSL \
            -X POST \
            -H "Authorization: token ${CONTROL_TOKEN}" \
            -H "Accept: application/vnd.github+json" \
            "https://api.github.com/repos/${REPO_PATH}/actions/runners/registration-token" \
            | jq -r .token)
    else
        echo "Using pre-minted registration token for ${REPO_PATH}..."
    fi

    if [ -z "$RUNNER_TOKEN" ] || [ "$RUNNER_TOKEN" = "null" ]; then
        echo "ERROR: Failed to get registration token. Check your GH_PAT." >&2
        exit 1
    fi

    ./config.sh \
        --url "$REPO_URL" \
        --token "$RUNNER_TOKEN" \
        --name "$RUNNER_NAME" \
        --labels "$LABELS" \
        --unattended \
        --replace
    touch "${RUNNER_CONFIG_MARKER}"
fi

# Registration tokens are single-use/short-lived and are not needed by any
# daemon or Actions worker after the local runner configuration exists.
unset RUNNER_REGISTRATION_TOKEN RUNNER_TOKEN

# Clone/update bot repo once (shared by watcher)
if [ "${ENABLE_WATCHER:-}" = "true" ]; then
    CONTROL_TOKEN="${CONTROL_TOKEN:?SGLANG_PAT or GH_PAT is required for the watcher}"
    BOT_REPO_URL="https://github.com/${REPO_PATH}.git"
    WATCHER_STATE_DIR="${WATCHER_STATE_DIR:-/var/lib/sglang-ci-bot}"
    WATCHER_STATE_FILE="${WATCHER_STATE_FILE:-${WATCHER_STATE_DIR}/last_check.json}"
    mkdir -p "${WATCHER_STATE_DIR}"
    if [ ! -f "${WATCHER_STATE_FILE}" ] && [ -f /tmp/bot/.state/last_check.json ]; then
        cp /tmp/bot/.state/last_check.json "${WATCHER_STATE_FILE}"
    fi
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
    echo "Starting comment watcher daemon (poll every ${POLL_INTERVAL:-15}s)..."
    (
        while true; do
            SGLANG_PAT="${CONTROL_TOKEN}" \
            BOT_REPO_TOKEN="${CONTROL_TOKEN}" \
            BOT_TRIGGER_LOGIN="amd-bot" \
            BOT_ACTOR_LOGIN="bingxche" \
            BOT_CLAIM_LOGINS="amd-bot,bingxche" \
            WATCHER_STATE_FILE="${WATCHER_STATE_FILE}" \
            python3 /tmp/bot/scripts/watch_comments.py \
                --daemon \
                --poll-interval "${POLL_INTERVAL:-15}" \
                --bot-repo "${REPO_PATH}" || true
            echo "Comment watcher exited — restarting in 15s"
            sleep 15
        done
    ) &

    echo "Starting CI monitor trigger (every 30 minutes)..."
    (
        while true; do
            sleep 1800
            curl -fsSL -X POST \
                -H "Authorization: token ${CONTROL_TOKEN}" \
                -H "Accept: application/vnd.github+json" \
                "https://api.github.com/repos/${REPO_PATH}/actions/workflows/ci-monitor.yml/dispatches" \
                -d '{"ref":"main"}' || true
        done
    ) &
fi

# The listener and its Actions workers must not inherit the long-lived PAT.
# Only the narrowly-coded watcher/monitor subprocesses above retain a copy.
unset GH_PAT BOT_PAT SGLANG_PAT BOT_REPO_TOKEN CONTROL_TOKEN

exec ./run.sh
