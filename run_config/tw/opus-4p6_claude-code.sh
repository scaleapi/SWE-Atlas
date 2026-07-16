#!/bin/bash
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARBOR_BIN="${HARBOR_BIN:-harbor}"
: "${HARBOR_AGENT_ALLOWED_HOST:?Set HARBOR_AGENT_ALLOWED_HOST in .env}"

"$HARBOR_BIN" run \
  -p ./data/tw \
  -a claude-code \
  -m "anthropic/claude-opus-4-6" \
  -e modal \
  --ek 'keepalive=["-c","sleep infinity"]' \
  -k 3 \
  -n 24 \
  --ak reasoning_effort=high \
  --ak disallowed_tools=WebSearch,WebFetch \
  --allow-agent-host "${HARBOR_AGENT_ALLOWED_HOST}" \
  -o results/tw/ \
  --job-name "opus-4p6_claude-code"
