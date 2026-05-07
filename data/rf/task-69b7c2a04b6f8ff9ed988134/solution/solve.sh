#!/bin/bash
set -eo pipefail
WORKSPACE="/app/suricata"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
