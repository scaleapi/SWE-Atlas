#!/bin/bash
set -eo pipefail
WORKSPACE="/src"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
