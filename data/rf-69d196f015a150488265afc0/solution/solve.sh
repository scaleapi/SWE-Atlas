#!/bin/bash
set -eo pipefail
WORKSPACE="/app/source"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
