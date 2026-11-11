#!/usr/bin/env bash
# Render docs/architecture.mmd to docs/architecture.png, and record which
# source it was rendered from (tests/test_readme.py checks the two match).
# Needs Docker; uses the mermaid-cli image with its bundled Chromium.
set -euo pipefail
cd "$(dirname "$0")"
cfg=$(mktemp -d)
trap 'rm -rf "$cfg"' EXIT
cp architecture.mmd "$cfg/"
echo '{"executablePath":"/usr/bin/chromium","args":["--no-sandbox","--disable-setuid-sandbox"]}' > "$cfg/puppeteer.json"
chmod -R a+rwX "$cfg"
docker run --rm -v "$cfg:/data" minlag/mermaid-cli:11.4.2 \
    -p /data/puppeteer.json -i /data/architecture.mmd -o /data/architecture.png -b white -s 2
cp "$cfg/architecture.png" architecture.png
# Hash with line endings normalized, so a Windows (CRLF) checkout matches CI.
tr -d '\r' < architecture.mmd | sha256sum | cut -d' ' -f1 > architecture.png.source-sha256
echo "Rendered docs/architecture.png"
