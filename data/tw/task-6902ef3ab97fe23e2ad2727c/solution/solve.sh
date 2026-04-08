#!/bin/bash
set -eo pipefail
cd /app
git apply /solution/addition.patch
mkdir -p /logs/agent
cat > /logs/agent/manifest.txt << 'MANIFEST_EOF'
<<TEST_MANIFEST>>
- file: cloudapi/api_test.go
  tests:
    - TestCreateTestRun
<<TEST_MANIFEST>>
MANIFEST_EOF
