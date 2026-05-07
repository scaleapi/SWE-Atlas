#!/bin/bash
set -eo pipefail
WORKSPACE="/workspace"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
