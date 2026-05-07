#!/bin/bash
set -eo pipefail
WORKSPACE="/calypso"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
