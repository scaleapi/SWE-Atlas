#!/bin/bash
# Gemini under Google's own Antigravity CLI (agy) rather than Claude Code or
# mini-swe-agent. Requires GEMINI_API_KEY in .env.
#
# The model id is what agy's client-side allowlist accepts in Gemini Developer
# API mode; newer ids depend on your key's entitlement and are rejected before
# any request is sent, so change it only if you have verified yours.
#
# TW runs with network access restricted to an allowlist, so the agent's own
# API host has to be named explicitly -- see HARBOR_AGENT_ALLOWED_HOST in the
# README. For agy that host is generativelanguage.googleapis.com.
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARBOR_BIN="${HARBOR_BIN:-harbor}"
: "${GEMINI_API_KEY:?Set GEMINI_API_KEY in .env}"

"$HARBOR_BIN" run \
  -p ./data/tw \
  -a antigravity-cli \
  -m "google/gemini-3.6-flash" \
  -e modal \
  --ek 'keepalive=["-c","sleep infinity"]' \
  -k 1 \
  -n 12 \
  --ae "GEMINI_API_KEY=${GEMINI_API_KEY}" \
  --allow-agent-host "${HARBOR_AGENT_ALLOWED_HOST:-generativelanguage.googleapis.com}" \
  --agent-include-logs '**' \
  -o results/tw/ \
  --job-name "gemini-3p6-flash_antigravity"
