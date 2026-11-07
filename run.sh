#!/usr/bin/env bash
# SWE Atlas entry point: ./run.sh <config.yaml> [qa|rf|tw] [harbor args]
#   ./run.sh configs/claude-opus-5-5.yaml                            all 284 tasks (data/qa, data/rf, data/tw)
#   ./run.sh configs/claude-opus-5-5.yaml rf                         one lane (data/rf)
#   ./run.sh configs/oracle.yaml qa -i task-6905333b74f22949d97ba9cc one task (reward 1)
# Extra arguments go to `harbor run`. Task filters (-i, -x, -l) need a lane.
set -euo pipefail

if [ ! -f "${1:-}" ]; then
  echo "usage: ./run.sh <config.yaml> [qa|rf|tw] [harbor args]" >&2
  echo "configs: $(cd "$(dirname "$0")" && ls configs/*.yaml | tr '\n' ' ')" >&2
  exit 1
fi
config="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; shift
cd "$(dirname "$0")"

lane=""
case "${1:-}" in qa|rf|tw) lane="$1"; shift ;; esac
if [ -z "$lane" ]; then
  for a in "$@"; do
    case "$a" in -i*|-x*|-l*|--include-task-name*|--exclude-task-name*|--n-tasks*)
      echo "task filters need a lane: ./run.sh <config.yaml> qa|rf|tw $*" >&2; exit 1 ;;
    esac
  done
fi

[ -f .env ] || { echo "no .env: cp .env.example .env and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a
[ -n "${JUDGE_API_KEY:-${OPENAI_API_KEY:-}}" ] && [ -n "${JUDGE_BASE_URL:-${OPENAI_API_BASE:-}}" ] || {
  echo "judge not set: OPENAI_API_KEY and OPENAI_API_BASE (or JUDGE_API_KEY and JUDGE_BASE_URL) in .env" >&2
  exit 1
}

# With a lane, -p data/<lane> replaces the config's datasets and lets -i/-x/-l filter tasks.
# The job name is the config name, the lane (if any) and a timestamp.
name="$(basename "$config" .yaml)${lane:+-$lane}-$(date +%Y%m%d-%H%M%S)"
exec harbor run -c "$config" ${lane:+-p "data/$lane"} --env-file .env -y --job-name "$name" "$@"
