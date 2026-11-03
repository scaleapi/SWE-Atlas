#!/usr/bin/env bash
set -euo pipefail

# Do-nothing agent over one or more lanes (default: qa rf tw, run one after another). Every trial
# should score 0; with oracle.sh it makes the release record (see summary.py).
#
#   bash run_config/nop.sh
#   bash run_config/nop.sh rf -- -i rf-69391d8d1ce51c407be1e533
#
# Anything after `--` goes to every `harbor run`.
# Env: N (concurrency, 48), MAX_RETRIES (3, see common.sh), JOB_NAME (nop), RESULTS_DIR (results), DRY_RUN=1 to print the
# commands, ENV_FILE (default .env), HARBOR_BIN. Judge: as for oracle.sh.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

LANE_ARGS=(); EXTRA=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--" ]]; then shift; EXTRA=("$@"); break; fi
  LANE_ARGS+=("$1"); shift
done
parse_lanes ${LANE_ARGS[@]+"${LANE_ARGS[@]}"}
load_env
export_judge_env
require_judge_env "${LANES[@]}"
set_retry_args
N="${N:-48}"
RESULTS_DIR="${RESULTS_DIR:-$REPO_ROOT/results}"

rc=0
for lane in "${LANES[@]}"; do
  echo "=== $lane: nop" >&2
  lane_select "$lane"
  run_harbor run "${LANE_SELECT[@]}" -a nop -e modal -n "$N" \
    -o "$RESULTS_DIR/$lane/" --job-name "${JOB_NAME:-nop}" -y "${RETRY_ARGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"} || rc=$?
  JOB_DIRS+=("$RESULTS_DIR/$lane/${JOB_NAME:-nop}")
done
print_summary
exit "$rc"
