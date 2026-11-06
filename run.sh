#!/usr/bin/env bash
# SWE Atlas entry point: ./run.sh <config.yaml> [harbor args]
#   ./run.sh configs/oracle.yaml -i qa-6905333b74f22949d97ba9cc   one reference solution (reward 1)
#   ./run.sh configs/claude-opus-5-5.yaml                         a model over all 284 tasks
# Extra arguments go to `harbor run`, e.g. -i 'qa-*' for one lane or -l 20 for 20 tasks.
set -euo pipefail

if [ ! -f "${1:-}" ]; then
  echo "usage: ./run.sh <config.yaml> [harbor args]" >&2
  echo "configs: $(cd "$(dirname "$0")" && ls configs/*.yaml | tr '\n' ' ')" >&2
  exit 1
fi
config="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; shift
cd "$(dirname "$0")"

[ -f .env ] || { echo "no .env: cp .env.example .env and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a
[ -n "${JUDGE_API_KEY:-${OPENAI_API_KEY:-}}" ] && [ -n "${JUDGE_BASE_URL:-${OPENAI_API_BASE:-}}" ] || {
  echo "judge not set: OPENAI_API_KEY and OPENAI_API_BASE (or JUDGE_API_KEY and JUDGE_BASE_URL) in .env" >&2
  exit 1
}

# -p data lets -i/-x filter tasks; the job name is the config name plus a timestamp.
exec harbor run -c "$config" -p data --env-file .env -y \
  --job-name "$(basename "$config" .yaml)-$(date +%Y%m%d-%H%M%S)" "$@"
