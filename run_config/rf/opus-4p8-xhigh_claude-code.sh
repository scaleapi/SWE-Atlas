#!/bin/bash
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARBOR_BIN="${HARBOR_BIN:-harbor}"
: "${HARBOR_AGENT_ALLOWED_HOST:?Set HARBOR_AGENT_ALLOWED_HOST in .env}"

"${HARBOR_BIN}" run \
  -p ./data/rf \
  -a claude-code \
  -m "anthropic/claude-opus-4-8" \
  -e modal \
  --ek modal_vm_runtime=true \
  -k 3 \
  -n 32 \
  --max-retries 3 \
  --ak reasoning_effort=xhigh \
  --ak disallowed_tools=WebSearch,WebFetch \
  --allow-agent-host "${HARBOR_AGENT_ALLOWED_HOST}" \
  -o results/rf/ \
  --job-name "opus-4p8-xhigh_claude-code" \
  -y
