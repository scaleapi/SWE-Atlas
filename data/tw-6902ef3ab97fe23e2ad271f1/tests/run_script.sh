#!/bin/bash
set -e

# Change to the app directory where the tests are located
cd /app
export CI=1
export PLAYWRIGHT_HEADLESS=true
export PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=0
export PLAYWRIGHT_SKIP_VALIDATION=1
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome

# Function to run all tests
run_all_tests() {
    local exit_code=0
    echo "Running all tests..."
    # Run client tests
    echo "Running client tests..."
    yarn test-client --verbose --passWithNoTests || exit_code=$((exit_code | $?))
    
    # Run integration tests
    echo "Running integration tests..."
    yarn test-integration --verbose --passWithNoTests || exit_code=$((exit_code | $?))
    
    # Run server tests
    echo "Running server tests..."
    yarn test-server --verbose --passWithNoTests || exit_code=$((exit_code | $?))
    
    # Run e2e tests
    echo "Running e2e tests..."
    xvfb-run -a yarn test:e2e --verbose --passWithNoTests || exit_code=$((exit_code | $?))
    
    return $exit_code
}

# Function to run selected tests
run_selected_tests() {
    local test_files=("$@")
    local exit_code=0
    
    for test_file in "${test_files[@]}"; do
        if [[ $test_file == *"e2e"* ]]; then
            cd /app/test/e2e
            xvfb-run -a yarn test "$test_file" --verbose --passWithNoTests || exit_code=$((exit_code | $?))
        elif [[ $test_file == *"client"* ]]; then
            cd /app
            yarn test-client "$test_file" --verbose --passWithNoTests || exit_code=$((exit_code | $?))
        elif [[ $test_file == *"integration"* ]]; then
            cd /app
            yarn test-integration "$test_file" --verbose --passWithNoTests || exit_code=$((exit_code | $?))
        elif [[ $test_file == *"server"* ]]; then
            cd /app
            yarn test-server "$test_file" --verbose --passWithNoTests || exit_code=$((exit_code | $?))
        elif [[ $test_file == *"packages"* ]]; then
            cd /app
            yarn test-packages "$test_file" --verbose --passWithNoTests || exit_code=$((exit_code | $?))
        else
            cd /app
            yarn test "$test_file" --verbose --passWithNoTests || exit_code=$((exit_code | $?))
        fi
    done
    
    return $exit_code
}

if [ $# -eq 0 ]; then
  (
    set +e
    run_all_tests
    echo "[INFO] Tests completed with exit code $?"
  )
  exit 0
fi


# Handle comma-separated input
if [[ "$1" == *","* ]]; then
  IFS=',' read -r -a TEST_FILES <<< "$1"
else
  TEST_FILES=("$@")
fi
# Run them all together
(
  set +e
  run_selected_tests "${TEST_FILES[@]}"
  echo "[INFO] Selected tests completed with exit code $?"
)
exit 0
