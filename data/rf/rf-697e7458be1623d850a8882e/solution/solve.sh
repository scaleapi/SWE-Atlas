#!/bin/bash
set -eo pipefail
WORKSPACE="/repo"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
