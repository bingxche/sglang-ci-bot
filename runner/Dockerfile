FROM python:3.12-slim

ARG RUNNER_VERSION=2.333.0
ARG TARGETARCH=x64

ENV DEBIAN_FRONTEND=noninteractive \
    DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git jq libicu-dev \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir anthropic httpx requests \
    && useradd -m runner \
    && mkdir -p /home/runner/actions-runner /workspace \
    && chown runner:runner /workspace \
    && curl -fsSL \
       "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${TARGETARCH}-${RUNNER_VERSION}.tar.gz" \
       | tar xz -C /home/runner/actions-runner \
    && chown -R runner:runner /home/runner

# Install Node.js + Claude Code CLI for agent mode
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

WORKDIR /home/runner/actions-runner
USER runner
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
