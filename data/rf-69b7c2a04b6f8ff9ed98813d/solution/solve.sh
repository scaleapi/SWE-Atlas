#!/bin/bash
set -eo pipefail
WORKSPACE="/opt/netdata.git"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
