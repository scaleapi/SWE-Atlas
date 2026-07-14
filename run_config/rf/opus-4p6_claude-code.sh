#!/bin/bash
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

harbor run \
  -p ./data/rf \
  -a claude-code \
  -m "anthropic/claude-opus-4-6" \
  -e modal \
  --ek modal_vm_runtime=true \
  -k 3 \
  -n 16 \
  --ak reasoning_effort=high \
  -o results/rf/ \
  --job-name "opus-4p6_claude-code"
