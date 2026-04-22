#!/bin/bash
# One-command setup for sglang-ci-bot self-hosted GitHub Actions runners.
#
# Usage:
#   bash runner/setup.sh --pat <GH_PAT> [--bot-pat <BOT_PAT>] [--llm-gateway-key <KEY>] [--count N] [--name <runner-prefix>] [--image <dockerhub-image>] [--build]
#
# Examples:
#   # First time: build locally, spawn 10 runners (default)
#   bash runner/setup.sh --pat ghp_xxxx --build
#
#   # Spawn 5 runners with custom prefix
#   bash runner/setup.sh --pat ghp_xxxx --count 5 --name my-runner --build
#
#   # Push to Docker Hub after build (manual):
#   docker tag sglang-ci-bot-runner:latest bingxche/sglang-ci-bot-runner:latest
#   docker push bingxche/sglang-ci-bot-runner:latest
#
#   # Other machines: pull from Docker Hub (no build needed)
#   bash runner/setup.sh --pat ghp_xxxx --image bingxche/sglang-ci-bot-runner:latest
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

REPO="bingxche/sglang-ci-bot"
RUNNER_NAME="amd-ci-bot-runner"
GH_PAT=""
BOT_PAT=""
LLM_GATEWAY_KEY=""
LLM_GATEWAY_URL=""
RUNNER_VERSION="2.333.0"
IMAGE=""
FORCE_BUILD=false
MIN_DISK_MB=3000
LOCAL_TAG="sglang-ci-bot-runner:latest"
RUNNER_COUNT=10
POLL_INTERVAL=15
CLAUDE_ENV_FILE=""
USE_AGENT=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --pat)             GH_PAT="$2"; shift 2 ;;
        --bot-pat)         BOT_PAT="$2"; shift 2 ;;
        --llm-gateway-key) LLM_GATEWAY_KEY="$2"; shift 2 ;;
        --llm-gateway-url) LLM_GATEWAY_URL="$2"; shift 2 ;;
        --name)            RUNNER_NAME="$2"; shift 2 ;;
        --repo)            REPO="$2"; shift 2 ;;
        --image)           IMAGE="$2"; shift 2 ;;
        --count)           RUNNER_COUNT="$2"; shift 2 ;;
        --build)           FORCE_BUILD=true; shift ;;
        --claude-env)      CLAUDE_ENV_FILE="$2"; shift 2 ;;
        --use-agent)       USE_AGENT=true; shift ;;
        *)                 echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$GH_PAT" ]; then
    echo "ERROR: --pat <GH_PAT> is required"
    echo "Usage: bash runner/setup.sh --pat ghp_xxxx [--llm-gateway-key KEY] [--count 10] [--name my-runner] [--image user/repo:tag] [--build]"
    exit 1
fi

if ! [[ "$RUNNER_COUNT" =~ ^[0-9]+$ ]] || [ "$RUNNER_COUNT" -lt 1 ]; then
    echo "ERROR: --count must be a positive integer (got: $RUNNER_COUNT)"
    exit 1
fi

echo "==> Checking prerequisites..."
if ! command -v docker &>/dev/null; then
    echo "ERROR: docker is not installed. Install it first:"
    echo "  curl -fsSL https://get.docker.com | sh"
    exit 1
fi

DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo "/var/lib/docker")
AVAIL_MB=$(df -m "${DOCKER_ROOT}" 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$AVAIL_MB" ] && [ "$AVAIL_MB" -lt "$MIN_DISK_MB" ]; then
    echo "ERROR: Insufficient disk space on ${DOCKER_ROOT}"
    echo "  Available: ${AVAIL_MB}MB, Required: ${MIN_DISK_MB}MB (3GB)"
    exit 1
fi
echo "    Disk space OK: ${AVAIL_MB:-unknown}MB available on ${DOCKER_ROOT}"

echo "==> Verifying GH_PAT..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: token ${GH_PAT}" \
    "https://api.github.com/repos/${REPO}")
if [ "$HTTP_CODE" != "200" ]; then
    echo "ERROR: GH_PAT cannot access ${REPO} (HTTP ${HTTP_CODE})"
    exit 1
fi
echo "    Token verified."

# --- Get the image: pull from registry or build locally ---
RUN_IMAGE="${LOCAL_TAG}"

if [ -n "$IMAGE" ] && [ "$FORCE_BUILD" = false ]; then
    echo "==> Pulling image ${IMAGE}..."
    if docker pull "$IMAGE"; then
        docker tag "$IMAGE" "$LOCAL_TAG"
        RUN_IMAGE="${LOCAL_TAG}"
        echo "    Pull succeeded."
    else
        echo "    Pull failed, falling back to local build..."
        FORCE_BUILD=true
    fi
else
    FORCE_BUILD=true
fi

if [ "$FORCE_BUILD" = true ]; then
    echo "==> Building Docker image from ${SCRIPT_DIR}..."
    docker build -t "${LOCAL_TAG}" \
        --build-arg RUNNER_VERSION="${RUNNER_VERSION}" \
        "${SCRIPT_DIR}"

    echo ""
    echo "    TIP: Push to Docker Hub to skip builds on other machines:"
    echo "      docker tag ${LOCAL_TAG} bingxche/sglang-ci-bot-runner:latest"
    echo "      docker push bingxche/sglang-ci-bot-runner:latest"
    echo ""
fi

# --- Pre-download runner tarball for offline updates ---
RUNNER_TARBALL="/tmp/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
RUNNER_MOUNT_ARGS=()
if [ -f "$RUNNER_TARBALL" ]; then
    echo "==> Found runner tarball: ${RUNNER_TARBALL}"
    RUNNER_MOUNT_ARGS=(-v "${RUNNER_TARBALL}:/tmp/runner-update.tar.gz:ro")
else
    echo "==> No pre-downloaded runner tarball at ${RUNNER_TARBALL} (optional, skipping)"
fi

echo "==> Stopping old containers (if any)..."
for i in $(seq 1 "$RUNNER_COUNT"); do
    docker rm -f "${RUNNER_NAME}-${i}" 2>/dev/null || true
done
docker rm -f sglang-ci-bot-runner 2>/dev/null || true

ENTRYPOINT_PATH="${SCRIPT_DIR}/entrypoint.sh"

echo "==> Starting ${RUNNER_COUNT} runners..."
for i in $(seq 1 "$RUNNER_COUNT"); do
    CONTAINER_NAME="${RUNNER_NAME}-${i}"
    echo "    Starting ${CONTAINER_NAME}..."

    EXTRA_ARGS=()

    # Claude Code env vars for ALL runners (agent mode)
    if [ "$USE_AGENT" = true ]; then
        if [ -n "$CLAUDE_ENV_FILE" ]; then
            EXTRA_ARGS+=(--env-file "$CLAUDE_ENV_FILE")
        elif [ -n "$LLM_GATEWAY_KEY" ]; then
            EXTRA_ARGS+=(-e "ANTHROPIC_CUSTOM_HEADERS=Ocp-Apim-Subscription-Key: ${LLM_GATEWAY_KEY}")
        fi
    fi

    # Runner-1 specific: comment watcher daemon
    if [ "$i" -eq 1 ]; then
        EXTRA_ARGS+=(-e ENABLE_WATCHER=true -e POLL_INTERVAL="${POLL_INTERVAL}")
        if [ -n "$BOT_PAT" ]; then
            EXTRA_ARGS+=(-e BOT_PAT="${BOT_PAT}")
        fi
    fi

    docker run -d \
        --name "${CONTAINER_NAME}" \
        --restart unless-stopped \
        --log-driver json-file \
        --log-opt max-size=100m \
        --log-opt max-file=100 \
        -v "sglang-runner-toolcache-${i}:/home/runner/actions-runner/_tool" \
        -v "${ENTRYPOINT_PATH}:/entrypoint.sh:ro" \
        "${RUNNER_MOUNT_ARGS[@]}" \
        -e REPO_URL="https://github.com/${REPO}" \
        -e GH_PAT="${GH_PAT}" \
        -e RUNNER_NAME="${CONTAINER_NAME}" \
        -e LABELS="self-hosted,amd-internal" \
        "${EXTRA_ARGS[@]}" \
        "${RUN_IMAGE}"
done

echo ""
echo "============================================"
echo "  ${RUNNER_COUNT} runners deployed successfully!"
echo "============================================"
echo "  Containers  : ${RUNNER_NAME}-{1..${RUNNER_COUNT}}"
echo "  Watcher     : ${RUNNER_NAME}-1 (comment daemon, poll ${POLL_INTERVAL}s)"
echo "  CI Monitor  : ${RUNNER_NAME}-1 (workflow_dispatch trigger, every 15min)"
echo "  Repo        : ${REPO}"
echo "  Labels     : self-hosted, amd-internal"
echo "  Entrypoint : ${ENTRYPOINT_PATH} (bind-mounted, restart to apply changes)"
echo ""
echo "  View logs  : docker logs -f ${RUNNER_NAME}-1"
echo "  Restart all: for i in \$(seq 1 ${RUNNER_COUNT}); do docker restart ${RUNNER_NAME}-\$i; done"
echo "  Stop all   : for i in \$(seq 1 ${RUNNER_COUNT}); do docker rm -f ${RUNNER_NAME}-\$i; done"
echo "============================================"
