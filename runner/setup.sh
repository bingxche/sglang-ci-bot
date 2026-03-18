#!/bin/bash
# One-command setup for sglang-ci-bot self-hosted GitHub Actions runner.
#
# Usage:
#   bash setup.sh --pat <GH_PAT> [--name <runner-name>] [--image <dockerhub-image>] [--build]
#
# Examples:
#   # First time: build locally
#   bash setup.sh --pat ghp_xxxx --build
#
#   # Push to Docker Hub after build (manual):
#   docker tag sglang-ci-runner:latest bingxche/sglang-ci-runner:latest
#   docker push bingxche/sglang-ci-runner:latest
#
#   # Other machines: pull from Docker Hub (no build needed)
#   bash setup.sh --pat ghp_xxxx --image bingxche/sglang-ci-runner:latest
set -euo pipefail

REPO="bingxche/sglang-ci-bot"
RUNNER_NAME="amd-ci-runner"
GH_PAT=""
RUNNER_VERSION="2.323.0"
IMAGE=""
FORCE_BUILD=false
MIN_DISK_MB=3000
LOCAL_TAG="sglang-ci-runner:latest"

while [[ $# -gt 0 ]]; do
    case $1 in
        --pat)   GH_PAT="$2"; shift 2 ;;
        --name)  RUNNER_NAME="$2"; shift 2 ;;
        --repo)  REPO="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        --build) FORCE_BUILD=true; shift ;;
        *)       echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$GH_PAT" ]; then
    echo "ERROR: --pat <GH_PAT> is required"
    echo "Usage: bash setup.sh --pat ghp_xxxx [--name my-runner] [--image user/repo:tag] [--build]"
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
    BUILDDIR=$(mktemp -d)
    trap "rm -rf ${BUILDDIR}" EXIT

    cat > "${BUILDDIR}/Dockerfile" << 'DOCKERFILE'
FROM python:3.12-slim

ARG RUNNER_VERSION=2.323.0
ARG TARGETARCH=x64

ENV DEBIAN_FRONTEND=noninteractive \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git jq \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir anthropic httpx requests \
    && useradd -m runner \
    && mkdir -p /home/runner/actions-runner \
    && curl -fsSL \
       "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${TARGETARCH}-${RUNNER_VERSION}.tar.gz" \
       | tar xz -C /home/runner/actions-runner \
    && chown -R runner:runner /home/runner

WORKDIR /home/runner/actions-runner
USER runner
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
DOCKERFILE

    cat > "${BUILDDIR}/entrypoint.sh" << 'ENTRYPOINT'
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
ENTRYPOINT
    chmod +x "${BUILDDIR}/entrypoint.sh"

    echo "==> Building Docker image..."
    docker build -t "${LOCAL_TAG}" \
        --build-arg RUNNER_VERSION="${RUNNER_VERSION}" \
        "${BUILDDIR}"

    echo ""
    echo "    TIP: Push to Docker Hub to skip builds on other machines:"
    echo "      docker tag ${LOCAL_TAG} <your-dockerhub-user>/sglang-ci-runner:latest"
    echo "      docker push <your-dockerhub-user>/sglang-ci-runner:latest"
    echo ""
fi

echo "==> Stopping old container (if any)..."
docker rm -f sglang-ci-runner 2>/dev/null || true

echo "==> Starting runner..."
docker run -d \
    --name sglang-ci-runner \
    --restart unless-stopped \
    --log-driver json-file \
    --log-opt max-size=100m \
    --log-opt max-file=100 \
    -v sglang-runner-toolcache:/home/runner/actions-runner/_tool \
    -e REPO_URL="https://github.com/${REPO}" \
    -e GH_PAT="${GH_PAT}" \
    -e RUNNER_NAME="${RUNNER_NAME}" \
    -e LABELS="self-hosted,amd-internal" \
    "${RUN_IMAGE}"

echo ""
echo "============================================"
echo "  Runner deployed successfully!"
echo "============================================"
echo "  Container : sglang-ci-runner"
echo "  Runner    : ${RUNNER_NAME}"
echo "  Repo      : ${REPO}"
echo "  Labels    : self-hosted, amd-internal"
echo "  Log limit : 100MB x 100 files (10GB max)"
echo ""
echo "  View logs : docker logs -f sglang-ci-runner"
echo "  Stop      : docker stop sglang-ci-runner"
echo "  Remove    : docker rm -f sglang-ci-runner"
echo "  Cleanup   : docker volume rm sglang-runner-toolcache"
echo "============================================"
