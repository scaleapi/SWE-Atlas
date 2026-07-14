#!/bin/bash
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

harbor run \
  -p ./data/tw \
  -a claude-code \
  -m "anthropic/claude-opus-4-6" \
  -e modal \
  --ek 'keepalive=["-c","sleep infinity"]' \
  -k 3 \
  -n 24 \
  --ak reasoning_effort=high \
  -o results/tw/ \
  --job-name "opus-4p6_claude-code"
