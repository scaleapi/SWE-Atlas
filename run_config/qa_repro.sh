#!/usr/bin/env bash
set -euo pipefail

# Optional check that the qa reference answers hold up at runtime in each task's pinned image.
# Not part of the release record, and nothing is written to data/. Every qa task is copied to
# $RESULTS_DIR/qa/.probes/$JOB_NAME/ (rebuilt on each run) with only instruction.md replaced: the
# agent gets the question, the reference answer (solution/answer.txt) and the rubric titles, runs
# every runtime claim of the answer and gives each claim a verdict (REPRODUCED, DIFFERS, FABRICATED
# or NOT-RUNNABLE) in /logs/agent/repro.json. The verifier does not run. Afterwards the verdicts are
# printed per task and written to <job>/claims.csv. A verdict is one model run: confirm it (a second
# run, or the saved command in agent/repro/<claim id>.txt) before acting on it.
#
#   bash run_config/qa_repro.sh -- -i qa-6905333b74f22949d97ba9cc
#   MODEL=anthropic/claude-sonnet-4-6 N=4 JOB_NAME=qa_repro_sonnet bash run_config/qa_repro.sh
#
# Anything after `--` goes to `harbor run`.
# Env: MODEL (anthropic/claude-haiku-5-5, through claude-code), K (1), N (8), JOB_NAME (qa_repro),
# RESULTS_DIR (results), HARBOR_AGENT_ALLOWED_HOST (required), DRY_RUN=1 to print the command,
# ENV_FILE (default .env), HARBOR_BIN, CLAUDE_CODE_VERSION (2.1.293).

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

LANE_ARGS=(); EXTRA=()
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--" ]]; then shift; EXTRA=("$@"); break; fi
  LANE_ARGS+=("$1"); shift
done
[[ ${#LANE_ARGS[@]} -gt 0 ]] || LANE_ARGS=(qa)
parse_lanes "${LANE_ARGS[@]}"
[[ "${LANES[*]}" == qa ]] || { echo "only qa has reference answers to reproduce" >&2; exit 1; }
load_env
MODEL="${MODEL:-anthropic/claude-haiku-5-5}"
K="${K:-1}"
N="${N:-8}"
JOB_NAME="${JOB_NAME:-qa_repro}"
RESULTS_DIR="${RESULTS_DIR:-$REPO_ROOT/results}"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.293}"
: "${HARBOR_AGENT_ALLOWED_HOST:?Set HARBOR_AGENT_ALLOWED_HOST (the agent API hostname)}"

probes="$RESULTS_DIR/qa/.probes/$JOB_NAME"
rm -rf "$probes"
mkdir -p "$probes"
lane_select qa
python3 - "$probes" "${LANE_SELECT[@]}" <<'PY'
import json, os, re, shutil, sys

PROMPT = """<uploaded_files>
/app
</uploaded_files>
The repository in /app is at the exact commit, in the exact environment, that the reference answer
below was written against. Your job is NOT to answer the question. Your job is to check, by actually
running things here, every runtime claim the reference answer makes, and to record what really happens.

<question>
{question}
</question>

<reference_answer>
{answer}
</reference_answer>

<grading_criteria>
{criteria}
</grading_criteria>

Do this:

1. List every runtime claim in the reference answer: each command it says it ran, and each output,
   log line, header, HTTP status, error code or message, response body, stored value, count or timing
   it says it observed. One observable per claim; split compound claims (a transcript with several
   reply lines is several claims). Skip statements that are only about reading the source, and skip
   opinions. The grading criteria are context only: do not turn them into claims.
2. For each claim, actually run the closest equivalent in this environment: start the service, send
   the request, run the command, read the stored data (adapt OS-specific syntax, paths, ports and file
   names; build what needs building). Reading or grepping the source is NOT a reproduction. If the
   answer shows a script whose printed "output" is hard-coded or computed without running the real
   system, that output is not runtime evidence: run the real system instead.
   Never write down what you expect to see: "observed" must be real output you got.
   Treat the reference answer's own scripts as suspect. Read a script before rerunning it. If it
   prints hard-coded values, sets the "observed" value itself (e.g. after = before), reads source text,
   or builds the result without exercising the running system (no server started, no real request,
   login, delivery or database write), set "simulated_in_answer" to true and do the real experiment
   instead (start the app and the services it needs, log in over HTTP, send the mail, query the store).
   Rerunning such a script is never a reproduction; its output only counts if the real experiment
   gives the same result.
3. Compare literally. Give each claim one verdict:
   - REPRODUCED: the real output matches the claim text, field for field. Only values that are random
     or clock-driven by nature (timestamps, UUIDs, random ids, generated keys, PIDs, durations) may
     differ.
   - DIFFERS: you ran it and anything else differs: reply text, status code, the protocol step at which
     a reply comes, header names, order or format, log line format, field names or values, counts.
     Put the actual output in "observed".
   - FABRICATED: the claimed output cannot be produced by the code at this commit: the message or
     format string does not exist anywhere in the source or its dependencies, the value is a
     placeholder or a truncated made-up string (e.g. "b=A1b2C3d4E5f6G7h8...", "abc123..."), or the
     claim contradicts what the code does. Put the evidence (grep result, file:line) in "note".
   - NOT-RUNNABLE: it cannot be run here after a real attempt. Say why in "note" (missing service,
     network blocked, needs more than ~15 minutes of waiting, ...). Where a long wait is involved you may
     use a faster equivalent (for example calling the code with a mocked clock) and say so in "note".
   When unsure between REPRODUCED and DIFFERS, choose DIFFERS and explain.
4. For each claim list the ids of the grading criteria (e.g. "1.3") whose pass depends on that claim;
   use [] when none does.

Rules: do not modify tracked files in /app (put scripts, configs, builds and data under /tmp/repro or
outside the repository; if a build writes into /app, remove those files afterwards). Work steadily and
aim to finish within about 60 minutes. Write /logs/agent/repro.json early and rewrite it after every few
claims so partial work survives a timeout. Save the full raw output of each check to
/logs/agent/repro/<claim id>.txt.

/logs/agent/repro.json must be valid JSON in exactly this shape:

```json
{
  "task": "{task}",
  "claims": [
    {
      "id": "C1",
      "claim": "short quote or tight paraphrase of the claim",
      "command": "exact command(s) you ran",
      "observed": "the key part of the real output (at most ~1500 characters)",
      "verdict": "REPRODUCED | DIFFERS | FABRICATED | NOT-RUNNABLE",
      "criteria": ["1.2"],
      "simulated_in_answer": false,
      "note": "why, adaptations made, evidence"
    }
  ],
  "summary": "two or three sentences on how well the reference answer holds up at runtime"
}
```
"""

out, _, data, *select = sys.argv[1:]
included = set(select[1::2])
for name in sorted(os.listdir(data)):
    src = os.path.join(data, name)
    if name not in included or not os.path.isfile(os.path.join(src, "solution/answer.txt")):
        continue
    dst = os.path.join(out, name)
    shutil.copytree(src, dst)
    instr = open(os.path.join(src, "instruction.md")).read()
    a, b = instr.find("<question>"), instr.find("</question>")
    with open(os.path.join(src, "tests/rubrics.json")) as f:
        criteria = "\n".join(r["title"].strip() for r in json.load(f))
    values = {"question": instr[a + 10:b].strip() if 0 <= a < b else instr.strip(),
              "answer": open(os.path.join(src, "solution/answer.txt")).read().strip(),
              "criteria": criteria, "task": name}
    with open(os.path.join(dst, "instruction.md"), "w") as f:
        f.write(re.sub(r"\{(question|answer|criteria|task)\}", lambda m: values[m[1]], PROMPT))
PY

echo "=== qa: reference-answer reproduction, claude-code / $MODEL (k=$K), probes in $probes" >&2
rc=0
run_harbor run -p "$probes" -a claude-code -m "$MODEL" -e modal -k "$K" -n "$N" \
  --ak version="$CLAUDE_CODE_VERSION" --ak disallowed_tools=WebSearch,WebFetch \
  --allow-agent-host "$HARBOR_AGENT_ALLOWED_HOST" --disable-verification --agent-timeout-multiplier 0.4 \
  -o "$RESULTS_DIR/qa/" --job-name "$JOB_NAME" -y ${EXTRA[@]+"${EXTRA[@]}"} || rc=$?
job="$RESULTS_DIR/qa/$JOB_NAME"
[[ "${DRY_RUN:-0}" != 1 && -d "$job" ]] || exit "$rc"

python3 - "$RUN_CONFIG" "${LANE_SELECT[1]}" "$job" <<'PY' || [[ $rc -ne 0 ]] || rc=1
import csv, json, os, re, sys
from collections import Counter

run_config, lane, job = sys.argv[1:]
sys.path.insert(0, run_config)
from summary import load, trials

VERDICTS = ("REPRODUCED", "DIFFERS", "FABRICATED", "NOT-RUNNABLE")
READS = re.compile(r"^\s*(grep|rg|sed|cat|head|tail|awk|find|ls|git\s+(grep|show))\b")
RUNS = re.compile(r"\b(python3?|node|npm|npx|go|cargo|make|curl|wget|redis-cli|psql|mysql|sqlite3|manage\.py|docker)\b|\./")
SOURCE = re.compile(r"(^|[\s'\"=])/app\b|\.(py|go|js|jsx|ts|tsx|rb|rs|c|h|cc|cpp|java|kt|php|cs|swift|scala)\b")
ARTIFACTS = re.compile(r"(?<![\w./-])/(?!app\b|dev/null)[\w.-]+/|\.log\b|\blogs?\b")


def verdict(v):
    v = re.sub(r"[\s_]+", "-", str(v or "").strip().upper())
    return next((x for x in VERDICTS if v[:6] == x[:6]), v or "MISSING")


def source_read_only(cmd):
    cmd = str(cmd or "")
    return bool(READS.match(cmd) and SOURCE.search(cmd)) and not RUNS.search(cmd) and not ARTIFACTS.search(cmd)


rows, ts = [], trials(job, os.path.join(os.path.dirname(run_config), "data"))
for t in ts:
    titles = {r["title"].split(":", 1)[0].strip(): r["title"]
              for r in load(os.path.join(lane, t["task"], "tests/rubrics.json")) or []}
    repro = load(os.path.join(t["dir"], "agent/repro.json"))
    cost = ((load(os.path.join(t["dir"], "result.json")).get("agent_result")) or {}).get("cost_usd")
    claims = repro.get("claims") if isinstance(repro, dict) else None
    claims = [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []
    counts = Counter(verdict(c.get("verdict")) for c in claims)
    status = t["error"] or ("" if claims else "no claims in agent/repro.json")
    print(f"{t['task']}  claims={len(claims)} " + " ".join(f"{v}={counts.get(v, 0)}" for v in VERDICTS)
          + (f"  cost=${cost:.2f}" if isinstance(cost, (int, float)) else "") + (f"  {status}" if status else ""))
    for c in claims:
        crit = c.get("criteria") or []
        crit = [str(x) for x in (crit if isinstance(crit, list) else [crit])]
        row = {"task": t["task"], "trial": os.path.basename(t["dir"]), "id": c.get("id"),
               "verdict": verdict(c.get("verdict")), "criteria": ";".join(crit),
               "source_read_only": source_read_only(c.get("command")),
               "simulated_in_answer": bool(c.get("simulated_in_answer")),
               "claim": str(c.get("claim") or ""), "observed": str(c.get("observed") or ""), "command": str(c.get("command") or ""),
               "note": str(c.get("note") or ""), "criteria_titles": " | ".join(titles.get(x, x) for x in crit)}
        rows.append(row)
        if row["verdict"] != "REPRODUCED" or row["simulated_in_answer"] or row["source_read_only"]:
            flags = "".join((" simulated in answer" if row["simulated_in_answer"] else "",
                             " source read only" if row["source_read_only"] else ""))
            print(f"   [{row['verdict']}{flags}] {row['id']} ({row['criteria'] or '-'}): {row['claim'][:160]}")
            print(f"       observed: {str(row['observed'])[:220]!r}")
with open(os.path.join(job, "claims.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["task"])
    w.writeheader()
    w.writerows(rows)
total = Counter(r["verdict"] for r in rows)
print(f"{len({t['task'] for t in ts})} tasks, {len(ts)} trials, {sum(not t['done'] for t in ts)} unfinished, "
      f"{sum(bool(t['error']) for t in ts)} errored; {len(rows)} claims: "
      + ", ".join(f"{v} {total.get(v, 0)}" for v in VERDICTS) + f"; {os.path.join(job, 'claims.csv')}")
PY
exit "$rc"
