#!/bin/bash
set -eo pipefail
WORKSPACE="/home/circleci/wp-calypso"
cd "$WORKSPACE"
git apply /solution/gold.patch
echo "Applied gold patch."
