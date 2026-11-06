#!/bin/bash
set -eo pipefail
cd /app
git apply /solution/addition.patch
mkdir -p /logs/agent
cat > /logs/agent/manifest.txt << 'MANIFEST_EOF'
<<TEST_MANIFEST>>
- file: kitty_tests/screen.py
  tests:
    - test_prompt_marking
<<TEST_MANIFEST>>
MANIFEST_EOF
