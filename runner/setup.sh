#!/bin/bash
# One-command setup for amd-bot self-hosted GitHub Actions runner.
#
# Usage:
#   curl -fsSL <raw-url>/runner/setup.sh | bash -s -- --pat <GH_PAT>
#   OR
#   bash setup.sh --pat <GH_PAT> [--name <runner-name>] [--repo <owner/repo>]
set -euo pipefail

REPO="bingxche/sglang-ci-bot"
RUNNER_NAME="amd-ci-runner"
GH_PAT=""
RUNNER_VERSION="2.323.0"
MIN_DISK_MB=3000

while [[ $# -gt 0 ]]; do
    case $1 in
        --pat)  GH_PAT="$2"; shift 2 ;;
        --name) RUNNER_NAME="$2"; shift 2 ;;
        --repo) REPO="$2"; shift 2 ;;
        *)      echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$GH_PAT" ]; then
    echo "ERROR: --pat <GH_PAT> is required"
    echo "Usage: bash setup.sh --pat ghp_xxxx [--name my-runner] [--repo owner/repo]"
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

BUILDDIR=$(mktemp -d)
echo "==> Building in ${BUILDDIR}..."

cat > "${BUILDDIR}/Dockerfile" << 'DOCKERFILE'
FROM ubuntu:24.04

ARG RUNNER_VERSION=2.323.0
ARG TARGETARCH=x64

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git jq \
    python3.11 python3.11-venv python3-pip \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m runner

WORKDIR /home/runner/actions-runner

RUN curl -fsSL \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${TARGETARCH}-${RUNNER_VERSION}.tar.gz" \
    | tar xz \
    && ./bin/installdependencies.sh \
    && chown -R runner:runner /home/runner

RUN pip install --no-cache-dir --break-system-packages \
    anthropic httpx requests

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
docker build -t sglang-ci-runner:latest \
    --build-arg RUNNER_VERSION="${RUNNER_VERSION}" \
    "${BUILDDIR}"

rm -rf "${BUILDDIR}"

echo "==> Stopping old container (if any)..."
docker rm -f sglang-ci-runner 2>/dev/null || true

echo "==> Starting runner..."
docker run -d \
    --name sglang-ci-runner \
    --restart unless-stopped \
    --log-driver json-file \
    --log-opt max-size=50m \
    --log-opt max-file=3 \
    -v sglang-runner-toolcache:/home/runner/actions-runner/_tool \
    -e REPO_URL="https://github.com/${REPO}" \
    -e GH_PAT="${GH_PAT}" \
    -e RUNNER_NAME="${RUNNER_NAME}" \
    -e LABELS="self-hosted,amd-internal" \
    sglang-ci-runner:latest

echo ""
echo "============================================"
echo "  Runner deployed successfully!"
echo "============================================"
echo "  Container : sglang-ci-runner"
echo "  Runner    : ${RUNNER_NAME}"
echo "  Repo      : ${REPO}"
echo "  Labels    : self-hosted, amd-internal"
echo "  Log limit : 50MB x 3 files"
echo "  Tool cache: docker volume 'sglang-runner-toolcache'"
echo ""
echo "  View logs : docker logs -f sglang-ci-runner"
echo "  Stop      : docker stop sglang-ci-runner"
echo "  Remove    : docker rm -f sglang-ci-runner"
echo "  Cleanup   : docker volume rm sglang-runner-toolcache"
echo "============================================"
