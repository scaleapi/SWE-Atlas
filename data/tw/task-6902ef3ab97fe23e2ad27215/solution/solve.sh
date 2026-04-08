#!/bin/bash
set -eo pipefail
cd /app
git apply /solution/addition.patch
mkdir -p /logs/agent
cat > /logs/agent/manifest.txt << 'MANIFEST_EOF'
<<TEST_MANIFEST>>
- file: scapy/utils.py
  tests:
    - Test hexdiff function
- file: test/regression.uts
  tests:
    - Test hexdiff function
<<TEST_MANIFEST>>
MANIFEST_EOF
