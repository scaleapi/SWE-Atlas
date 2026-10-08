#!/usr/bin/env bash
set -euo pipefail

# Oracle (reference solution) over one or more lanes (default: qa rf tw, run one after another).
#
#   bash run_config/oracle.sh
#   bash run_config/oracle.sh rf -- -i task-69391d8d1ce51c407be1e533
#
# Anything after `--` goes to every `harbor run`.
# Env: N (concurrency, 48), JOB_NAME (oracle), RESULTS_DIR (results), DRY_RUN=1 to print the
# commands, ENV_FILE (default .env), HARBOR_BIN.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

LANE_ARGS=(); EXTRA=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--" ]]; then shift; EXTRA=("$@"); break; fi
  LANE_ARGS+=("$1"); shift
done
parse_lanes ${LANE_ARGS[@]+"${LANE_ARGS[@]}"}
load_env
N="${N:-48}"
RESULTS_DIR="${RESULTS_DIR:-$REPO_ROOT/results}"

rc=0
for lane in "${LANES[@]}"; do
  echo "=== $lane: oracle" >&2
  run_harbor run -p "$REPO_ROOT/data/$lane" -a oracle -e modal -n "$N" \
    -o "$RESULTS_DIR/$lane/" --job-name "${JOB_NAME:-oracle}" -y ${EXTRA[@]+"${EXTRA[@]}"} || rc=$?
done
exit "$rc"
