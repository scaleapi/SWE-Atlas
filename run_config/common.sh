# Sourced by oracle.sh and nop.sh.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_CONFIG="$REPO_ROOT/run_config"

# Run settings passed in the environment (N=... bash run_config/oracle.sh) take priority over
# .env, which takes priority over the scripts' defaults. Captured before any default is assigned.
RUN_VARS=(MODEL K EFFORT N JOB_NAME RESULTS_DIR DRY_RUN HARBOR_BIN MAX_RETRIES
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

# The `harbor run` arguments that select one lane's tasks. All tasks live flat in data/, named
# <lane>-<id>; a lane is every directory whose name starts with "<lane>-". The other lanes are
# excluded, so an -i filter passed after `--` selects tasks within the lane.
lane_select() {
  local d found=0 other
  LANE_SELECT=(-p "$REPO_ROOT/data")
  for other in "${ALL_LANES[@]}"; do [[ "$other" == "$1" ]] || LANE_SELECT+=(-x "$other-*"); done
  for d in "$REPO_ROOT/data/$1-"*; do [[ -d "$d" ]] && found=1; done
  [[ "$found" == 1 ]] || { echo "no tasks for lane $1 in data/$1-*" >&2; exit 1; }
}

# The verifiers of all three lanes read the judge from JUDGE_API_KEY, JUDGE_BASE_URL, JUDGE_MODEL,
# JUDGE_REASONING_EFFORT and JUDGE_VOTES. Derive them from OPENAI_API_KEY / OPENAI_API_BASE /
# EVAL_MODEL when they aren't given, so one set of judge variables in .env serves every lane.
# With nothing set, the judge is openai/gpt-6.1-sol at high reasoning effort, and each verdict
# is the majority of 3 votes.
# anthropic/claude-opus-4-7 content-filters some qa tasks' rubrics, which zeroes a trial with
# judge_unusable=1 rather than grading it.
export_judge_env() {
  export JUDGE_API_KEY="${JUDGE_API_KEY:-${OPENAI_API_KEY:-}}"
  export JUDGE_BASE_URL="${JUDGE_BASE_URL:-${OPENAI_API_BASE:-}}"
  export JUDGE_MODEL="${JUDGE_MODEL:-${EVAL_MODEL:-openai/gpt-6.1-sol}}"
  export JUDGE_REASONING_EFFORT="${JUDGE_REASONING_EFFORT:-${EVAL_REASONING_EFFORT:-high}}"
  export JUDGE_FALLBACK_API_KEY="${JUDGE_FALLBACK_API_KEY:-}"
}

# Every run retries the failures that are infrastructure, not the submission. harbor records an
# environment, sandbox, healthcheck, agent-setup, agent-proxy (rate limit, 5xx, overload, dropped
# connection, DNS/TLS), agent-timeout or verifier failure as the trial's exception_info, so the
# trial ends as an error with no reward rather than a scored 0. set_retry_args tells harbor to
# re-run such a trial up to MAX_RETRIES times (default 3, set MAX_RETRIES=0 to turn retries off);
# each retry deletes the failed attempt first, so no stale reward is left and nothing is double
# counted, and a high-concurrency run converges without manual reruns. Real outcomes stay as they
# ran and are never retried: a model refusal, a missing/rejected key, an unknown or unusable model,
# an exhausted account usage limit, a reply over the token or context budget, and a reply the judge
# could not parse. harbor retries every exception except the ones listed, so an infrastructure
# failure type we have not met yet is retried too. summary.py leaves the trials still erroring for
# an infrastructure reason out of pass@1.
# On rf and tw the verifier shares the container with the agent's grading-time code as root, so a
# determined agent could force a verifier failure to be re-sampled; the provisional reward 0 and the
# seal check in the verifier (the grader side of this robustness work) are what close that, and
# MAX_RETRIES bounds how many extra attempts it could ever buy. summary.py only drops a no-reward
# trial that its verifier sealed as infrastructure, never a bare missing reward.
NO_RETRY_EXCEPTIONS=(AgentSafetyRefusalError AgentAuthenticationError ModelNotFoundError
                     ApiUsageLimitError OutputTokenExceededError ContextWindowExceededError
                     OutputLengthExceededError ContextLengthExceededError
                     VerifierOutputParseError RewardFileEmptyError)
# retry.yaml carries the backoff (harbor has no CLI flag for it, so the retries would otherwise fire a
# constant 1 s apart and could storm a rate-limited proxy); -c reads it and the --max-retries and
# --retry-exclude below still win over it.
set_retry_args() {
  local e n="${MAX_RETRIES:-3}"
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "MAX_RETRIES must be a non-negative whole number, got '$n'" >&2; exit 1; }
  RETRY_ARGS=(-c "$RUN_CONFIG/retry.yaml" --max-retries "$n")
  for e in "${NO_RETRY_EXCEPTIONS[@]}"; do RETRY_ARGS+=(--retry-exclude "$e"); done
}

# Fail early when judge credentials are missing; the verifiers write reward 0 instead of crashing,
# which would silently zero a whole run.
require_judge_env() {
  local missing="" v
  [[ $# -gt 0 ]] || return 0
  for v in JUDGE_API_KEY JUDGE_BASE_URL JUDGE_MODEL; do [[ -n "${!v:-}" ]] || missing="$missing $v"; done
  [[ -z "$missing" ]] || { echo "judge credentials missing:$missing (set JUDGE_* or OPENAI_API_KEY/OPENAI_API_BASE in .env)" >&2; exit 1; }
  [[ -z "${JUDGE_VOTES:-}" || "$JUDGE_VOTES" =~ ^[0-9]*[13579]$ ]] || { echo "JUDGE_VOTES must be an odd whole number (1, 3, 5, ...), got '$JUDGE_VOTES'" >&2; exit 1; }
}

# Run one harbor job, or print it when DRY_RUN=1.
run_harbor() {
  if [[ "${DRY_RUN:-0}" == 1 ]]; then
    printf '%q ' "$HARBOR_BIN" "$@"; echo
  else
    "$HARBOR_BIN" "$@"
  fi
}

# Jobs this script ran; print_summary ends the script with one table over all of them.
JOB_DIRS=()
print_summary() {
  local dirs=() d
  for d in ${JOB_DIRS[@]+"${JOB_DIRS[@]}"}; do [[ -d "$d" ]] && dirs+=("$d"); done
  if [[ "${DRY_RUN:-0}" == 1 ]]; then
    printf '%q ' python3 "$RUN_CONFIG/summary.py" ${JOB_DIRS[@]+"${JOB_DIRS[@]}"}; echo
  elif [[ ${#dirs[@]} -gt 0 ]]; then
    echo "=== summary" >&2
    python3 "$RUN_CONFIG/summary.py" "${dirs[@]}" || true
  fi
}
