#!/usr/bin/env bash
set -euo pipefail

# Oracle (reference solution) over one or more lanes (default: qa rf tw, run one after another).
#
#   bash run_config/oracle.sh
#   bash run_config/oracle.sh rf -- -i rf-69391d8d1ce51c407be1e533
#
# Anything after `--` goes to every `harbor run`.
# Env: N (concurrency, 48), MAX_RETRIES (3, see common.sh), JOB_NAME (oracle), RESULTS_DIR (results), DRY_RUN=1 to print the
# commands, ENV_FILE (default .env), HARBOR_BIN. Judge: JUDGE_API_KEY / JUDGE_BASE_URL /
# JUDGE_MODEL, or the OPENAI_API_KEY / OPENAI_API_BASE / EVAL_MODEL set they default to.

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
  lane_in_filter "$lane" || { echo "=== $lane: no task matches the -i filter, skipped" >&2; continue; }
  echo "=== $lane: oracle" >&2
  lane_select "$lane"
  run_harbor run "${LANE_SELECT[@]}" -a oracle -e modal -n "$N" \
    -o "$RESULTS_DIR/$lane/" --job-name "${JOB_NAME:-oracle}" -y "${RETRY_ARGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"} || rc=$?
  JOB_DIRS+=("$RESULTS_DIR/$lane/${JOB_NAME:-oracle}")
done
[[ ${#JOB_DIRS[@]} -gt 0 ]] || { echo "no task in lanes ${LANES[*]} matches the -i filter" >&2; exit 1; }
print_summary
exit "$rc"
