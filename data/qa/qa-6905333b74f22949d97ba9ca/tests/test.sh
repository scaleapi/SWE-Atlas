#!/bin/bash
set -uo pipefail
# ── Grading-time integrity (the same in qa, tw and rf; see verifier_report.py) ──
# Harbor can run this script inside the agent's container. Before grading, stop
# whatever the agent left running, drop anything planted in /logs/verifier and
# any file in /tests the task does not ship; after grading, keep the reward only if
# this run wrote it.
# Interpreter: a system python3 (3.7 or later) run as -I -S -B: isolated mode, no
# site processing (no .pth, sitecustomize or user site from the container's
# site-packages, no PYTHON* variables) and no bytecode. The graders use only the
# standard library. Interpreters an agent would install (uv tools, venvs) are
# never used; without a system python3 the trial scores 0 with the reason.
score_zero() {
    local keep=" 1 " p=$$ s d pid cmd first lim stopped="" n=0 f ka=" " targets="" t
    while [ "$p" -gt 1 ] 2>/dev/null && s=$(cat "/proc/$p/stat" 2>/dev/null); do
        keep+="$p "; f=(${s##*) }); p=${f[1]:-0}
    done
    s=$(cat /proc/1/stat 2>/dev/null); f=(${s##*) })
    lim=$(( ${f[19]:-0} + 3 * $(getconf CLK_TCK 2>/dev/null || echo 100) ))
    for d in /proc/[0-9]*; do
        pid=${d#/proc/}
        case "$keep" in *" $pid "*) continue ;; esac
        s=$(cat "$d/stat" 2>/dev/null) && cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
        f=(${s##*) })
        [ -n "$cmd" ] && [ "${f[0]:-Z}" != Z ] || continue
        cmd=${cmd% }; first=${cmd%% *}; cmd="${first##*/}${cmd#"$first"}"
        case "$cmd" in "sh -c sleep infinity"|"bash -c sleep infinity"|"dash -c sleep infinity"|"sleep infinity"|"tail -f /dev/null")
            [ "${f[19]:-}" -le "$lim" ] 2>/dev/null && ka+="$pid " && continue ;;
        esac
        # A keepalive sh may fork its sleep late; spare it when its parent passed the check above.
        case "$cmd" in "sh -c sleep infinity"|"sleep infinity") targets+="$pid:${f[1]:-0} " ;; *) targets+="$pid:0 " ;; esac
    done
    for t in $targets; do
        case "$ka" in *" ${t#*:} "*) continue ;; esac
        pid=${t%:*}
        kill -9 "$pid" 2>/dev/null && stopped+="$pid " && n=$((n + 1))
    done
    for _ in 1 2 3 4 5; do
        p=""
        for pid in $stopped; do
            s=$(cat "/proc/$pid/stat" 2>/dev/null) && f=(${s##*) }) && [ "${f[0]:-Z}" != Z ] && p=1
        done
        [ -z "$p" ] && break
        sleep 1
    done
    echo "  Processes stopped: $n"
    rm -rf /logs/verifier/reward.txt /logs/verifier/reward.json /logs/verifier/evaluation_results.json
    printf '0\n' > /logs/verifier/reward.txt
    printf '%s\n' '{"reward": 0.0, "must_have_pass": 0.0, "overall_pass": 0.0, "rubrics_agg_score": 0.0, "tests_pass": 0.0, "tests_applicable": 0.0, "judge_unusable": 0.0, "not_graded": 1.0, "num_unrated_must_have": 0.0, "is_qna": 1.0, "is_tw": 0.0, "is_rf": 0.0}' > /logs/verifier/reward.json
    echo "=== SEALED: reward=0 reason=$1 ==="
}
PYTHON=""
for cand in /usr/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    case "$cand" in */.local/*|*/uv/*|*venv/*|*/.venv/*) continue ;; esac
    if "$cand" -I -S -B -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null; then
        PYTHON="$cand"; break
    fi
done
PY=("$PYTHON" -I -S -B)
export VERIFIER_PYTHON="$PYTHON"
mkdir -p /logs/verifier
if [ -z "$PYTHON" ]; then
    echo "=== INTEGRITY ==="
    echo "  no system python3 >= 3.7 for the graders; not grading on an agent-installed interpreter"
    score_zero "no system python3 >= 3.7 for the graders"
    exit 0
fi
export PYTHONDONTWRITEBYTECODE=1
find /tests -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
find /tests -name '*.pyc' -delete 2>/dev/null
clean_tests() {
    export VERIFIER_TESTS_REMOVED="$(find /tests/ -mindepth 1 -maxdepth 1 ! \( -type f \( \
        -name evaluate_answer.py -o -name judge_client.py -o -name prompt.txt \
        -o -name rubrics.json -o -name system_prompt.txt -o -name test.sh \
        -o -name user_prompt_template.txt -o -name verifier_report.py \
        \) \) -print -exec rm -rf {} + 2>/dev/null)"
}
clean_tests
export VERIFIER_RUN_ID="$("${PY[@]}" -c 'import secrets; print(secrets.token_hex(16))' 2>/dev/null || echo "$$-$(date +%s)")"
"${PY[@]}" /tests/verifier_report.py prepare qa || true
clean_tests
[ -z "$VERIFIER_TESTS_REMOVED" ] || echo "  WARNING: removed from /tests after stopping processes:" $VERIFIER_TESTS_REMOVED

"${PY[@]}" /tests/evaluate_answer.py
rc=$?

# Keep the verdict only if this run wrote it; otherwise score 0 with the reason.
# Without a reward.json harbor drops the trial instead of scoring it.
"${PY[@]}" /tests/verifier_report.py seal qa "$rc" || score_zero "the seal step failed"

exit 0
