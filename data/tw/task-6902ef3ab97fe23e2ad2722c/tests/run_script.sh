#!/bin/bash
# Task-local runner. Harbor uploads this at verify time; evaluate_tests.py
# prefers it over the image's /run_script.sh.
set -u
export DEBIAN_FRONTEND=noninteractive
APT_OPTS="-o Acquire::Check-Valid-Until=false -o Acquire::Check-Date=false"

install_libzbar() {
  if ldconfig -p 2>/dev/null | grep -q libzbar; then
    echo "libzbar already present"
    return 0
  fi
  echo "Installing libzbar0 (Check-Valid-Until=false; bullseye-security InRelease is expired)..."
  apt-get $APT_OPTS update -qq
  apt-get $APT_OPTS install -y -qq libzbar0
  ldconfig || true
  if ldconfig -p 2>/dev/null | grep -q libzbar; then
    echo "libzbar installed"
    return 0
  fi
  echo "apt install failed; trying snapshot.debian.org..." >&2
  echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20240101T000000Z bullseye main" \
    > /etc/apt/sources.list.d/snapshot-zbar.list
  apt-get $APT_OPTS -o Acquire::Retries=3 update -qq
  apt-get $APT_OPTS install -y -qq libzbar0
  ldconfig || true
}

install_libzbar
python3 - <<'PY'
try:
    from pyzbar import pyzbar  # noqa: F401
    print("pyzbar import OK")
except Exception as e:
    print(f"pyzbar import FAILED: {e}", flush=True)
PY

cd /app/src
export PIP_BREAK_SYSTEM_PACKAGES=1
export DJANGO_SETTINGS_MODULE=paperless.settings
export PAPERLESS_CI_TEST=1
export PAPERLESS_DISABLE_DBHANDLER=true
export PAPERLESS_CACHE_BACKEND=django.core.cache.backends.locmem.LocMemCache
export PYTHONUNBUFFERED=1

if [ $# -eq 0 ]; then
  echo "No test selectors given" >&2
  exit 2
fi

if [[ "$1" == *","* ]]; then
  IFS=',' read -r -a TEST_FILES <<< "$1"
else
  TEST_FILES=("$@")
fi

echo "Running selected tests: ${TEST_FILES[*]}"
rc=0
for test_path in "${TEST_FILES[@]}"; do
  test_path="${test_path#src/}"
  echo "Running test: $test_path"
  pytest -v --tb=short \
    --override-ini="addopts=" \
    -p no:sugar -p no:cacheprovider \
    --no-cov \
    "$test_path"
  pytest_rc=$?
  if [ "$pytest_rc" -ne 0 ]; then
    rc=$pytest_rc
  fi
done
exit "$rc"
