# Full-suite grading hooks for the rf benchmark (sourced by master_validator_script.sh).
# Provides the baked baseline (suite-once) and the patched-phase time cap with a single retry. FULL_SUITE_BASE
# selects the baseline: baked (precomputed) or live (run in the container).
FULL_SUITE_MODE="${FULL_SUITE_MODE:-full}"
FULL_SUITE_BASE="${FULL_SUITE_BASE:-baked}"
FS_UTIL="${FS_UTIL:-/tests}"
FS_LOG="${FS_LOG:-/logs/verifier}"
FS_PY="${VERIFIER_PYTHON:-python3}"
# baked_base.py uses only the standard library, so it runs under the same isolated interpreter the rest of the grader
# uses (-I drops cwd and PYTHON* env, -S skips site, -B writes no bytecode): a module planted in the workspace cannot
# be imported on `import json`.
fs_py() { "$FS_PY" -I -S -B "$@"; }
# How much under the cap a 124/137 means the runner timed out internally rather than our `timeout` firing at the cap.
FS_CAP_TOL="${FS_CAP_TOL:-15}"

fs_infra() {
    # Write the infra marker and exit non-zero. test.sh turns a present infra_error.json into a no-reward, non-zero
    # exit, so harbor errors the trial and re-runs it -- never a wrong score.
    fs_py "$FS_UTIL/baked_base.py" infra "$@" \
        || printf '{"reason": "%s", "reward": null, "infra_error": true}\n' "$1" > "$FS_LOG/infra_error.json"
    log "VERDICT infra reason=$1 mode=$FULL_SUITE_MODE base=$FULL_SUITE_BASE phase=${2:-} rc=${3:-} cap=${4:-} elapsed=${5:-}"
    exit 3
}

fs_init() {
    mkdir -p "$FS_LOG"
    [ "$FULL_SUITE_MODE" = full ] || fs_infra "FULL_SUITE_MODE=$FULL_SUITE_MODE is not supported (only full)"
    [ "$FULL_SUITE_BASE" = baked ] || [ "$FULL_SUITE_BASE" = live ] || fs_infra "bad_base:$FULL_SUITE_BASE"
    [ -f "$FS_UTIL/baked_base.py" ] || fs_infra "missing:baked_base.py"
    # The stamp of this container (base_tree + runner + image_id); synth checks the baked stanza's stamp against it.
    fs_py "$FS_UTIL/baked_base.py" stamp > "$FS_LOG/stamp_actual.json" 2>/dev/null || rm -f "$FS_LOG/stamp_actual.json"
}

fs_baseline() {
    # $1 = output prefix. Returns 0 and writes ${1}_results.json from the baked stanza when FULL_SUITE_BASE=baked
    # (the stamp must match, else infra; a rewritten root commit with matching runner+image is agent_fail). Returns 1
    # to run the live baseline otherwise. Call at top level (`if fs_baseline ...; then ... else <live> fi`), never in a
    # subshell, so an agent_fail / infra exit reaches the whole script.
    [ "$FULL_SUITE_BASE" = baked ] || return 1
    log "=== BASELINE from the baked expectations (FULL_SUITE_BASE=baked) ==="
    if ! fs_py "$FS_UTIL/baked_base.py" synth "${1}_results.json"; then
        if [ -f "$ARTIFACT_DIR/agent_fail" ]; then
            log "VERDICT agent_fail reason=$(cat "$ARTIFACT_DIR/agent_fail") phase=baseline"
            exit 1
        fi
        fs_infra "$(cat "$ARTIFACT_DIR/fs_reason" 2>/dev/null || echo baked_base_failed)" baseline
    fi
    printf '0' > "${1}_runner_rc.txt"
    return 0
}

fs_mutation() {
    # $1 = output prefix. Runs the patched phase (capped at TEST_TIMEOUT_SEC inside run_tests_and_parse) and sets
    # FS_MUTATION_JSON. A cap overrun / early kill (rc 124/137) is retried ONCE. Outcomes:
    #   * rc 124/137 with elapsed well under the cap -> the runner timed out internally, not our cap: infra (re-run).
    #   * a second overrun at the cap, agent phase  -> the patched code is too slow: agent_fail (true negative), never
    #                                                  a false negative.
    #   * a second overrun at the cap, oracle phase -> gold is never "too slow" under a 3x p95 cap: infra (re-run).
    # $2 (agent|oracle), else FS_PHASE_KIND, selects the overrun outcome; defaults to agent. Call at top level, not in
    # $( ), so the agent_fail / infra exit reaches the whole script.
    local prefix="$1" rc f start elapsed cap kind="${2:-${FS_PHASE_KIND:-agent}}"
    cap="${TEST_TIMEOUT_SEC:-900}"
    start=$(date +%s)
    run_tests_and_parse "$prefix" >/dev/null
    elapsed=$(( $(date +%s) - start ))
    rc="$(cat "${prefix}_runner_rc.txt" 2>/dev/null || echo 0)"
    if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then
        # Our `timeout -k 30 $cap` only produces 124/137 once the run has reached the cap; a 124/137 well under the cap
        # is the runner's own internal timeout (a runner that calls `timeout` itself), which is infra, not a slow patch.
        if [ "$elapsed" -lt "$(( cap - FS_CAP_TOL ))" ]; then
            fs_infra "runner_timeout_before_cap" "${prefix##*/}" "$rc" "$cap" "$elapsed"
        fi
        log "Patched run hit the cap (rc=$rc, cap=${cap}s, elapsed=${elapsed}s); retrying once"
        for f in stdout.log stderr.log results.json runner_rc.txt; do
            mv -f "${prefix}_$f" "${prefix}_try1_$f" 2>/dev/null || true
        done
        start=$(date +%s)
        run_tests_and_parse "$prefix" >/dev/null
        elapsed=$(( $(date +%s) - start ))
        rc="$(cat "${prefix}_runner_rc.txt" 2>/dev/null || echo 0)"
        if [ "$rc" = 124 ] || [ "$rc" = 137 ]; then
            if [ "$elapsed" -lt "$(( cap - FS_CAP_TOL ))" ]; then
                fs_infra "runner_timeout_before_cap" "${prefix##*/}" "$rc" "$cap" "$elapsed"
            fi
            if [ "$kind" = oracle ]; then
                fs_infra "cap_overrun_oracle" "${prefix##*/}" "$rc" "$cap" "$elapsed"
            fi
            echo "cap_overrun" > "$ARTIFACT_DIR/agent_fail"
            log "VERDICT agent_fail reason=cap_overrun phase=${prefix##*/} rc=$rc cap=$cap elapsed=$elapsed"
            exit 1
        fi
    fi
    FS_MUTATION_JSON="${prefix}_results.json"
}
