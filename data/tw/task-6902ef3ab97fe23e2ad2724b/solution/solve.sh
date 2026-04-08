#!/bin/bash
set -eo pipefail
cd /app
git apply /solution/addition.patch
mkdir -p /logs/agent
cat > /logs/agent/manifest.txt << 'MANIFEST_EOF'
<<TEST_MANIFEST>>
- file: cmd/sts-handlers_test.go
  tests:
    - TestIAMInternalIDPSTSServerSuite/Test:_1,_ServerType:_ErasureSD
    - TestIAMInternalIDPSTSServerSuite/Test:_3,_ServerType:_ErasureSD
    - TestIAMInternalIDPSTSServerSuite/Test:_5,_ServerType:_Erasure
    - TestIAMInternalIDPSTSServerSuite/Test:_7,_ServerType:_ErasureSet
<<TEST_MANIFEST>>
MANIFEST_EOF
