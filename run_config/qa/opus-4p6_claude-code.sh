#!/bin/bash
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

harbor run \
  -p ./data/qa \
  -a claude-code \
  -m "anthropic/claude-opus-4-6" \
  -e modal \
  -k 3 \
  -n 24 \
  --ak reasoning_effort=high \
  -o results/qa/ \
  --job-name "opus-4p6_claude-code"
