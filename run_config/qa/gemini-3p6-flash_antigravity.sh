#!/bin/bash
# Gemini under Google's own Antigravity CLI (agy) rather than Claude Code or
# mini-swe-agent. Requires GEMINI_API_KEY in .env.
#
# The model id is what agy's client-side allowlist accepts in Gemini Developer
# API mode; newer ids depend on your key's entitlement and are rejected before
# any request is sent, so change it only if you have verified yours.
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${GEMINI_API_KEY:?Set GEMINI_API_KEY in .env}"

harbor run \
  -p ./data/qa \
  -a antigravity-cli \
  -m "google/gemini-3.6-flash" \
  -e modal \
  --ek 'keepalive=["-c","sleep infinity"]' \
  -k 1 \
  -n 12 \
  --ae "GEMINI_API_KEY=${GEMINI_API_KEY}" \
  --allow-agent-host generativelanguage.googleapis.com \
  --agent-include-logs '**' \
  -o results/qa/ \
  --job-name "gemini-3p6-flash_antigravity"
