#!/bin/bash
# Quick smoke test for Claude Code agent in a runner container.
# Usage:
#   bash scripts/verify_agent.sh                      # test locally
#   docker exec amd-ci-bot-runner-1 bash /tmp/bot/scripts/verify_agent.sh  # test in container
#   docker run --rm --entrypoint bash -e ANTHROPIC_CUSTOM_HEADERS="..." \
#     bingxche/sglang-ci-bot-runner:claude-agent /path/to/verify_agent.sh  # test image

set -uo pipefail

PASS=0
FAIL=0
pass() { echo "  [PASS] $1"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=== Claude Code Agent Verification ==="
echo ""

# 1. claude CLI
echo "1. Claude Code CLI"
if command -v claude &>/dev/null; then
    VER=$(claude --version 2>&1 | head -1)
    pass "claude found: $VER"
else
    fail "claude not found in PATH"
fi

# 2. Node.js
echo "2. Node.js"
if command -v node &>/dev/null; then
    pass "node $(node --version)"
else
    fail "node not found"
fi

# 3. Environment variables
echo "3. Environment variables"
[ -n "${ANTHROPIC_API_KEY:-}" ]   && pass "ANTHROPIC_API_KEY set"        || fail "ANTHROPIC_API_KEY not set"
[ -n "${ANTHROPIC_BASE_URL:-}" ]  && pass "ANTHROPIC_BASE_URL set"       || fail "ANTHROPIC_BASE_URL not set"
[ -n "${ANTHROPIC_CUSTOM_HEADERS:-}" ] && pass "ANTHROPIC_CUSTOM_HEADERS set" || fail "ANTHROPIC_CUSTOM_HEADERS not set (inject via --llm-gateway-key at runtime)"
[ -z "${GH_PAT:-}${BOT_PAT:-}${SGLANG_PAT:-}${GITHUB_TOKEN:-}" ] && pass "No GitHub token exposed to agent" || fail "GitHub token is exposed to agent"

# 4. /workspace/sglang
echo "4. sglang repo"
if [ -d "/workspace/sglang/.git" ]; then
    HEAD=$(git -C /workspace/sglang log --oneline -1 2>/dev/null)
    pass "/workspace/sglang exists: $HEAD"
else
    fail "/workspace/sglang not found or not a git repo"
fi

# 5. API connectivity (one-word test)
echo "5. API connectivity"
if command -v claude &>/dev/null && [ -n "${ANTHROPIC_API_KEY:-}" ] && [ -n "${ANTHROPIC_CUSTOM_HEADERS:-}" ]; then
    REPLY=$(claude -p "Reply with exactly one word: working" --output-format text --max-turns 1 < /dev/null 2>/dev/null | tr -d '[:space:]' | head -c 20)
    if [ -n "$REPLY" ]; then
        pass "API responded: $REPLY"
    else
        fail "API returned empty response"
    fi
else
    fail "Skipped (missing claude or env vars)"
fi

# 6. Public GitHub API access
echo "6. Public GitHub API access"
HTTP=$(curl -s -o /dev/null -w "%{http_code}" https://api.github.com/repos/sgl-project/sglang 2>/dev/null)
if [ "$HTTP" = "200" ]; then
    pass "Public GitHub API OK (HTTP $HTTP)"
else
    fail "Public GitHub API returned HTTP $HTTP"
fi

echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] && echo "All checks passed." || echo "Some checks failed — see above."
exit $FAIL
