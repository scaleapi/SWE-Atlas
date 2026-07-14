#!/bin/bash
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARBOR_BIN="${HARBOR_BIN:-/mnt/efs/mohitraghavendra/.local/bin/harbor}"

"$HARBOR_BIN" run \
  -p ./data/qa \
  -a oracle \
  -e modal \
  --ek 'keepalive=["-c","sleep infinity"]' \
  -n 48 \
  -o results/qa/ \
  --job-name "oracle" \
  -y
