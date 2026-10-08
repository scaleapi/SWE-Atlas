# Sourced by pass_k.sh and oracle.sh.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_CONFIG="$REPO_ROOT/run_config"

# Run settings passed in the environment (MODEL=... bash run_config/pass_k.sh) take priority over
# .env, which takes priority over the scripts' defaults. Captured before any default is assigned.
RUN_VARS=(MODEL K EFFORT N JOB_NAME RESULTS_DIR DRY_RUN HARBOR_BIN
          CLAUDE_CODE_VERSION CODEX_VERSION MINI_SWE_AGENT_VERSION)
CALLER_RUN_ENV=()
for _var in "${RUN_VARS[@]}"; do
  if printenv "$_var" >/dev/null; then CALLER_RUN_ENV+=("$_var=$(printenv "$_var")"); fi
done
unset _var

HARBOR_BIN="${HARBOR_BIN:-harbor}"
ALL_LANES=(qa rf tw)

load_env() {
  local env_file="${ENV_FILE:-$REPO_ROOT/.env}" kv
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  else
    echo "warning: no env file at $env_file; using only variables already in the environment (set ENV_FILE to override)" >&2
  fi
  for kv in ${CALLER_RUN_ENV[@]+"${CALLER_RUN_ENV[@]}"}; do export "$kv"; done
}

# Lanes from the arguments before `--`; default all three.
parse_lanes() {
  LANES=()
  for arg in "$@"; do
    case "$arg" in
      qa|rf|tw) LANES+=("$arg") ;;
      *) echo "unknown lane: $arg (expected qa, rf or tw)" >&2; exit 1 ;;
    esac
  done
  [[ ${#LANES[@]} -gt 0 ]] || LANES=("${ALL_LANES[@]}")
}

slug() { echo "$1" | sed 's/[^A-Za-z0-9.-]/-/g'; }

# Run one harbor job, or print it when DRY_RUN=1.
run_harbor() {
  if [[ "${DRY_RUN:-0}" == 1 ]]; then
    printf '%q ' "$HARBOR_BIN" "$@"; echo
  else
    "$HARBOR_BIN" "$@"
  fi
}
