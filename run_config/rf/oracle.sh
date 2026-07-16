#!/bin/bash
set -euo pipefail

# Load credentials
set -a
source "$(dirname "$0")/../../.env"
set +a

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HARBOR_BIN="${HARBOR_BIN:-harbor}"

"$HARBOR_BIN" run \
  -p ./data/rf \
  -a oracle \
  -e modal \
  --ek modal_vm_runtime=true \
  -n 48 \
  -o results/rf/ \
  --job-name "oracle" \
  -y
