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

exec ./run.sh
