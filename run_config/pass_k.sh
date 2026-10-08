#!/usr/bin/env bash
set -euo pipefail

# pass@k for one agent over one or more lanes (default: qa rf tw, run one after another).
#
#   bash run_config/pass_k.sh claude-code
#   bash run_config/pass_k.sh codex rf tw
#   MODEL=anthropic/claude-opus-4-8 EFFORT=xhigh N=32 bash run_config/pass_k.sh claude-code rf -- \
#     --max-retries 3 --agent-timeout-multiplier 3 --ae CLAUDE_ENABLE_STREAM_WATCHDOG=0
#
# Agents: claude-code, codex, mini-swe-agent. Anything after `--` goes to every `harbor run`.
# Env: MODEL, K (3), EFFORT (high), N (concurrency), HARBOR_AGENT_ALLOWED_HOST (required for
# rf and tw), JOB_NAME, RESULTS_DIR (results), DRY_RUN=1 to print the commands,
# ENV_FILE (default .env), HARBOR_BIN.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# The versions the benchmark is validated on; harbor installs the latest release otherwise.
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.293}"
CODEX_VERSION="${CODEX_VERSION:-0.142.1}"
MINI_SWE_AGENT_VERSION="${MINI_SWE_AGENT_VERSION:-2.4.6}"

AGENT="${1:?usage: pass_k.sh <claude-code|codex|mini-swe-agent> [qa] [rf] [tw] [-- harbor args]}"
shift
LANE_ARGS=(); EXTRA=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--" ]]; then shift; EXTRA=("$@"); break; fi
  LANE_ARGS+=("$1"); shift
done
parse_lanes ${LANE_ARGS[@]+"${LANE_ARGS[@]}"}
load_env

case "$AGENT" in
  claude-code)    MODEL="${MODEL:-anthropic/claude-opus-4-6}";        N="${N:-24}" ;;
  mini-swe-agent) MODEL="${MODEL:-openai/anthropic/claude-opus-4-6}"; N="${N:-16}" ;;
  codex)          MODEL="${MODEL:?set MODEL for codex, e.g. MODEL=openai/gpt-5.4}"; N="${N:-16}" ;;
  *) echo "unknown agent: $AGENT (expected claude-code, codex or mini-swe-agent)" >&2; exit 1 ;;
esac
K="${K:-3}"
RESULTS_DIR="${RESULTS_DIR:-$REPO_ROOT/results}"
EFFORT="${EFFORT:-high}"

rc=0
for lane in "${LANES[@]}"; do
  args=(run -p "$REPO_ROOT/data/$lane" -a "$AGENT" -m "$MODEL" -e modal -k "$K" -n "$N"
        --ak reasoning_effort="$EFFORT")
  case "$AGENT" in
    claude-code)    args+=(--ak version="$CLAUDE_CODE_VERSION") ;;
    codex)          args+=(--ak version="$CODEX_VERSION" --ak web_search=disabled) ;;
    mini-swe-agent) args+=(--ak version="$MINI_SWE_AGENT_VERSION"
                           --ak config_file="$RUN_CONFIG/mswea/$lane.yaml") ;;
  esac
  # rf and tw restrict the agent's network to an allowlist; the agent's API host must be on it.
  if [[ "$lane" != qa ]]; then
    : "${HARBOR_AGENT_ALLOWED_HOST:?Set HARBOR_AGENT_ALLOWED_HOST for $lane (the agent API hostname)}"
    args+=(--allow-agent-host "$HARBOR_AGENT_ALLOWED_HOST")
    [[ "$AGENT" == claude-code ]] && args+=(--ak disallowed_tools=WebSearch,WebFetch)
  fi
  args+=(-o "$RESULTS_DIR/$lane/" --job-name "${JOB_NAME:-$(slug "$MODEL")_${AGENT}_${EFFORT}}" -y)
  echo "=== $lane: $AGENT / $MODEL (effort $EFFORT, k=$K)" >&2
  run_harbor "${args[@]}" ${EXTRA[@]+"${EXTRA[@]}"} || rc=$?
done
exit "$rc"
