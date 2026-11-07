#!/bin/bash
set -eo pipefail
WORKSPACE="/code"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
