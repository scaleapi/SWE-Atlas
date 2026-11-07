#!/bin/bash
set -eo pipefail
WORKSPACE="/grafana"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
