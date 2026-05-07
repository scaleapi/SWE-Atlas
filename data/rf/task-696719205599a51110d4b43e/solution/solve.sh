#!/bin/bash
set -eo pipefail
WORKSPACE="/go/src/go.k6.io/k6"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
