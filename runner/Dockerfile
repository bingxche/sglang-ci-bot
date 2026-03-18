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
