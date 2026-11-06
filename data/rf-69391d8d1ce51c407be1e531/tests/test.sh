#!/bin/bash
set -uo pipefail
# ── Grading-time integrity (the same in qa, tw and rf; see verifier_report.py) ──
# Harbor can run this script inside the agent's container. Before grading, stop
# whatever the agent left running, drop anything planted in /logs/verifier or
# /tmp_artifact_storage and any file in /tests the task does not ship; after
# grading, keep the reward only if this run wrote it. The master validator and
# the test runs (agent code) never see the judge credentials or the run id, and
# whatever they leave running is stopped before the verdict is written.
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
    printf '%s\n' '{"reward": 0.0, "must_have_pass": 0.0, "overall_pass": 0.0, "rubrics_agg_score": 0.0, "tests_pass": 0.0, "tests_applicable": 1.0, "judge_unusable": 0.0, "not_graded": 1.0, "num_unrated_must_have": 0.0, "is_qna": 0.0, "is_tw": 0.0, "is_rf": 1.0}' > /logs/verifier/reward.json
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
        -name compare_test_runs.py -o -name config.json -o -name evaluate_rubrics.py \
        -o -name judge_client.py -o -name master_validator_script.sh \
        -o -name problematic_tests.json -o -name prompt.txt -o -name relevant_file_names.json \
        -o -name relevant_test_names.json -o -name rubrics.json \
        -o -name rubrics_system_prompt.txt -o -name rubrics_user_prompt_template.txt \
        -o -name run_script.sh -o -name test.sh -o -name test_files.json \
        -o -name test_patch.diff -o -name verifier_report.py \
        -o -name baked_base.py -o -name baked_base.json -o -name fs_hooks.sh \
        \) \) -print -exec rm -rf {} + 2>/dev/null)"
}
tests_sha() {
    "${PY[@]}" -c 'import hashlib, os
h = hashlib.sha256()
for n in sorted(os.listdir("/tests")):
    with open(os.path.join("/tests", n), "rb") as f:
        h.update(n.encode() + b"\0" + hashlib.sha256(f.read()).digest())
print(h.hexdigest())' 2>/dev/null
}
clean_tests
export VERIFIER_RUN_ID="$("${PY[@]}" -c 'import secrets; print(secrets.token_hex(16))' 2>/dev/null || echo "$$-$(date +%s)")"
"${PY[@]}" /tests/verifier_report.py prepare rf || true
clean_tests
[ -z "$VERIFIER_TESTS_REMOVED" ] || echo "  WARNING: removed from /tests after stopping processes:" $VERIFIER_TESTS_REMOVED
TESTS_SHA="$(tests_sha)"
rm -rf /tmp_artifact_storage
mkdir -p /tmp_artifact_storage
# Every git call below ignores configuration the agent could have planted: hooks,
# fsmonitor, filters, diff drivers, includes, ~/.gitconfig. harden-git rewrites
# the repository's own config as well; this wrapper is the second layer.
# (git 2.30 ignores GIT_CONFIG_GLOBAL, hence the private HOME; the config file inside it
# carries safe.directory = *, which git 2.35.2 to 2.37 only honour from a global file.)
export GIT_SAFE_HOME="$(mktemp -d)"
printf '[safe]\n\tdirectory = *\n' > "$GIT_SAFE_HOME/.gitconfig"   # the only global config git sees
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$GIT_SAFE_HOME/.gitconfig" GIT_TERMINAL_PROMPT=0
git() {
    HOME="$GIT_SAFE_HOME" XDG_CONFIG_HOME="$GIT_SAFE_HOME" command git --no-pager \
        -c core.hooksPath=/dev/null -c core.fsmonitor= -c core.attributesFile=/dev/null \
        -c core.excludesFile=/dev/null -c 'safe.directory=*' "$@"
}
scrub_args=()
for v in $(compgen -e); do
    case "$v" in JUDGE_*|OPENAI_*|EVAL_*|ANTHROPIC_*|VERIFIER_*) scrub_args+=(-u "$v") ;; esac
done

# Output contract shared with qa and tw: see /tests/verifier_report.py. This
# script prints the VERIFIER header and the TESTS section, evaluate_rubrics.py
# prints RUBRICS, and the final step prints MANIFEST (not applicable here) and
# the RESULT line while writing reward.txt, reward.json and
# evaluation_results.json. Debug material (agent patch, runner logs, the master
# validator's own log) goes under /logs/verifier/artifacts/, never to stdout.
"${PY[@]}" /tests/verifier_report.py header rf
echo ""
echo "=== TESTS ==="

# ── Detect workspace (git repo root) ──
WORKSPACE="/workspace"
if [ ! -d "$WORKSPACE/.git" ]; then
    WORKSPACE=$(git rev-parse --show-toplevel 2>/dev/null || true)
fi
if [ -z "$WORKSPACE" ] || [ ! -d "$WORKSPACE" ]; then
    for d in /code /app /workspace /testbed /src /calypso /repo \
             /home/circleci/wp-calypso /go/src/go.k6.io/k6 \
             /grafana /opt/netdata.git /app/source /app/suricata /src/suricata; do
        [ -d "$d/.git" ] && WORKSPACE="$d" && break
    done
fi
if [ -z "$WORKSPACE" ]; then
    echo "  skipped (no git workspace found)"
    echo ""
    echo "=== RUBRICS ==="
    echo "  skipped (no git workspace found)"
    echo ""
    echo "=== MANIFEST ==="
    echo "  not applicable to this track"
    "${PY[@]}" /tests/verifier_report.py fail rf "no git workspace found"
    "${PY[@]}" /tests/verifier_report.py seal rf 1
    exit 0
fi
echo "  Workspace: $WORKSPACE"
cd "$WORKSPACE"
"${PY[@]}" /tests/verifier_report.py harden-git "$WORKSPACE" || true

# ── Instance dockers have a squashed single-commit repo; its root commit is the base ──
export GIT_NO_REPLACE_OBJECTS=1
rm -f "$(git rev-parse --git-common-dir)/info/grafts"
BASE_COMMIT=081f72b5f1b99cf53866b7669623a8a60fd71857
echo "  Base commit (root): $BASE_COMMIT"
if ! git cat-file -e "$BASE_COMMIT^{commit}" 2>/dev/null; then
    echo "  skipped (base commit missing from the repository)"
    echo ""
    echo "=== RUBRICS ==="
    echo "  skipped (base commit missing from the repository)"
    echo ""
    echo "=== MANIFEST ==="
    echo "  not applicable to this track"
    "${PY[@]}" /tests/verifier_report.py fail rf "base commit $BASE_COMMIT missing from the repository"
    "${PY[@]}" /tests/verifier_report.py seal rf 1
    exit 0
fi

# ── go.sum lines the image build added (go mod download) belong to the base, not the patch ──
if git diff --no-ext-diff --numstat "$BASE_COMMIT" -- go.sum | awk '$2 != 0 {d = 1} END {exit !(NR && !d)}'; then
    GOSUM_INDEX="$(mktemp -d)/index"
    GIT_INDEX_FILE="$GOSUM_INDEX" git read-tree "$BASE_COMMIT"
    GIT_INDEX_FILE="$GOSUM_INDEX" git update-index --add go.sum
    GOSUM_TREE=$(GIT_INDEX_FILE="$GOSUM_INDEX" git write-tree)
    BASE_COMMIT=$(GIT_AUTHOR_NAME=verifier GIT_AUTHOR_EMAIL=verifier@localhost \
        GIT_COMMITTER_NAME=verifier GIT_COMMITTER_EMAIL=verifier@localhost \
        git commit-tree "$GOSUM_TREE" -p "$BASE_COMMIT" -m "go.sum as found")
    echo "  go.sum only gained lines; base with that go.sum: $BASE_COMMIT"
fi

# ── Capture agent diff (including new files) ──
git add -A
git diff --cached --no-ext-diff --no-textconv "$BASE_COMMIT" > /tmp_artifact_storage/agent.patch
git diff --cached --raw --no-abbrev -z --no-renames --diff-filter=A "$BASE_COMMIT" > /tmp_artifact_storage/agent_added.z
# The grader runs these from the workspace root; an agent change to one fails the tests gate.
GRADER_FILES_CHANGED=$(git diff --cached --name-only --no-renames "$BASE_COMMIT" -- \
    run_script.sh compare_test_runs.py master_validator_script.sh | tr '\n' ' ')
git reset HEAD --quiet 2>/dev/null || true

PATCH_SIZE=$(wc -c < /tmp_artifact_storage/agent.patch)
echo "  Agent patch size: $PATCH_SIZE bytes"

if [ "$PATCH_SIZE" -le 1 ]; then
    echo "  WARNING: Agent produced empty or near-empty patch"
fi

# ── Detect test-file modifications and generate filtered (source-only) diff ──
BASE_COMMIT="$BASE_COMMIT" "${PY[@]}" << 'PYEOF'
import json, os, subprocess, shutil, sys
sys.path.append("/tests")
import verifier_report as vr
GIT_ENV = vr.git_env()

tf_error = None
try:
    with open("/tests/test_files.json") as f:
        tf_data = json.load(f)
    test_files = set()
    # Accept every shape test_files.json comes in: {"files": [...]} or a bare list,
    # with entries that are strings or objects naming the file as "name" or "path".
    items = tf_data["files"] if isinstance(tf_data, dict) else tf_data
    if not isinstance(items, list):
        raise ValueError("no list of files")
    for item in items:
        name = (item.get("name") or item.get("path")) if isinstance(item, dict) else item
        if not isinstance(name, str) or not name:
            raise ValueError("unrecognised entry %r" % (item,))
        test_files.add(name)
except Exception as e:
    tf_error = "test_files.json unreadable: %s" % e
    print(f"  WARNING: {tf_error} (tests gate fails)")
    test_files = set()

result = subprocess.run(vr.git_args(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", os.environ["BASE_COMMIT"], "--name-only"]),
                        env=GIT_ENV, capture_output=True, text=True)
result_untracked = subprocess.run(
    vr.git_args(["ls-files", "--others", "--exclude-standard"]),
    env=GIT_ENV, capture_output=True, text=True,
)
modified_files = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
modified_files |= (
    set(result_untracked.stdout.strip().split("\n")) if result_untracked.stdout.strip() else set()
)

flagged = sorted(test_files & modified_files)

flag_data = {
    "test_files_in_csv": sorted(test_files),
    "agent_modified_files": sorted(modified_files),
    "flagged_test_files": flagged,
    "test_files_modified": len(flagged) > 0,
    "flagged_count": len(flagged),
    "test_files_error": tf_error,
}

with open("/tmp_artifact_storage/test_files_modified.json", "w") as f:
    json.dump(flag_data, f, indent=2)

if flagged:
    print(f"  WARNING: Agent modified {len(flagged)} test file(s):")
    for tf in flagged:
        print(f"    - {tf}")
else:
    print("  OK: Agent did not modify any test files.")

if flagged:
    subprocess.run(vr.git_args(["add", "-A"]), env=GIT_ENV, capture_output=True)
    exclude_args = vr.git_args(["diff", "--cached", "--no-ext-diff", "--no-textconv", os.environ["BASE_COMMIT"], "--"])
    for tf in flagged:
        exclude_args.append(f":!{tf}")
    result_filtered = subprocess.run(exclude_args, env=GIT_ENV, capture_output=True, text=True)
    subprocess.run(vr.git_args(["reset", "HEAD", "--quiet"]), env=GIT_ENV, capture_output=True)

    with open("/tmp_artifact_storage/agent_source_only.patch", "w") as f:
        f.write(result_filtered.stdout)
    print(f"  Filtered (source-only) patch size: {len(result_filtered.stdout)} bytes")
else:
    shutil.copy2("/tmp_artifact_storage/agent.patch", "/tmp_artifact_storage/agent_source_only.patch")
    print("  No filtering needed — source-only patch = full patch")

PYEOF

ART_DIR=/logs/verifier/artifacts
mkdir -p "$ART_DIR"
cp /tmp_artifact_storage/agent.patch "$ART_DIR/agent.patch" 2>/dev/null || true

# ── Stage artifacts for master validator ──
cp /tests/test_patch.diff /tmp_artifact_storage/test.patch
cp /tests/relevant_test_names.json /tmp_artifact_storage/relevant_test_names.json
cp /tests/relevant_file_names.json /tmp_artifact_storage/relevant_file_names.json

# Whatever the agent left at these paths (a file, symlink or directory, ignored or tracked in HEAD) fails
# the tests gate and is replaced by the grader's copy.
for f in run_script.sh compare_test_runs.py master_validator_script.sh; do
    if [ -e "$f" ] || [ -L "$f" ] || git cat-file -e "HEAD:$f" 2>/dev/null; then
        GRADER_FILES_CHANGED+="$f "
    fi
    rm -rf -- "$f"
done
cp /tests/run_script.sh ./run_script.sh
cp /tests/compare_test_runs.py ./compare_test_runs.py
cp /tests/master_validator_script.sh ./master_validator_script.sh
chmod +x ./run_script.sh ./master_validator_script.sh

# ── Run master validator in agent mode ──
# Its full log (build output, heartbeats, comparison summary) is an artifact of
# its own; stdout keeps the step lines every track prints, plus the tail of the
# log when the validator fails so the failure stays diagnosable from here.
export BASE_COMMIT="$BASE_COMMIT"
MV_LOG="$ART_DIR/master_validator.log"
echo "  [baseline+mutation] Running the master validator (log: artifacts/master_validator.log)..."
export PARALLEL_TESTS=0  # baseline and mutation runs are sequential
# The validator builds and runs the agent's code, so it gets no judge credentials
# and no run id, only the graders' interpreter.
env ${scrub_args[@]+"${scrub_args[@]}"} VERIFIER_PYTHON="$PYTHON" bash ./master_validator_script.sh --mode agent --relax > "$MV_LOG" 2>&1
MV_EXIT=$?
echo "  [baseline+mutation] Master validator exited with code $MV_EXIT"
if [ "$MV_EXIT" -ne 0 ]; then
    echo "  --- last 60 lines of artifacts/master_validator.log ---"
    tail -n 60 "$MV_LOG" | sed 's/^/    /'
    echo "  --- end of artifacts/master_validator.log ---"
fi
"${PY[@]}" /tests/verifier_report.py stop-agent-code || true

# Copy the validator's parsed per-phase result maps to /logs/verifier for offline inspection; these
# are copies of scratch artifacts and have no effect on the reward.
for f in agent_baseline_results.json agent_mutation_results.json agent_comparison_results.json \
         ref_baseline_results.json ref_mutation_results.json ref_comparison_results.json \
         iv_baseline_results.json iv_mutation_results.json iv_comparison_results.json; do
    cp "/tmp_artifact_storage/$f" "/logs/verifier/$f" 2>/dev/null || true
done

# A grading-mechanism infra marker (stale baked stamp, runner-internal timeout, oracle cap overrun) means this
# trial cannot be scored correctly; exit non-zero with no reward so harbor errors and re-runs it, never a wrong score.
if [ -s /logs/verifier/infra_error.json ]; then
    echo "  [infra] grading hit an infrastructure error; not scoring (harbor re-runs the trial):"
    sed 's/^/    /' /logs/verifier/infra_error.json
    exit 3
fi

# ── Keep the runner logs: the only record of why a suite ran empty or failed ──
for phase in baseline mutation; do
    for stream in stdout stderr; do
        cp "/tmp_artifact_storage/agent_${phase}_${stream}.log" "$ART_DIR/${phase}_${stream}.log" 2>/dev/null || true
    done
done

# The master validator ran agent code; a file in /tests it changed ends grading here.
if [ -z "$TESTS_SHA" ] || [ "$(tests_sha)" != "$TESTS_SHA" ]; then
    score_zero "a file in /tests changed during grading"
    exit 0
fi

# The master validator ran agent code; a workspace grader file that no longer matches /tests fails the tests gate.
for f in run_script.sh compare_test_runs.py master_validator_script.sh; do
    if [ -L "$f" ] || ! "${PY[@]}" -c 'import sys; a, b = (open(p, "rb").read() for p in sys.argv[1:]); sys.exit(a != b)' \
            "/tests/$f" "$f" 2>/dev/null; then
        GRADER_FILES_CHANGED+="$f "
    fi
done

# ── Compare relevant tests → shared tests block ──
echo "  [comparison] Every relevant test must be kept green (p2p) or added green (m2p)..."
MV_EXIT="$MV_EXIT" GRADER_FILES_CHANGED="$GRADER_FILES_CHANGED" "${PY[@]}" << 'PYEOF'
import json, os, re, subprocess, sys
sys.path.append("/tests")
import verifier_report as vr
comp_file = "/tmp_artifact_storage/agent_comparison_results.json"
rel_file = "/tmp_artifact_storage/relevant_test_names.json"
mv_exit = int(os.environ.get("MV_EXIT") or 1)
CELLS = {"p2p": "pass_to_pass", "p2f": "pass_to_fail", "p2m": "pass_to_missing", "f2p": "fail_to_pass",
         "f2f": "fail_to_fail", "f2m": "fail_to_missing", "m2p": "missing_to_pass", "m2f": "missing_to_fail"}
ACCEPT = ("p2p", "m2p")  # a relevant test passes the gate only when kept green or added green
def as_names(items):
    return [i if isinstance(i, str) else i["name"] for i in (items or []) if isinstance(i, str) or (isinstance(i, dict) and i.get("name"))]
def matches_strict(full_name, rn):
    """Exact: the relevant name is the whole test name, package- or file-qualified as the runner
    reports it. Never a part of a longer name."""
    return full_name == rn
def added_test_files(path="/tmp_artifact_storage/agent_added.z"):
    try:
        with open(path, "rb") as f:
            fields = f.read().split(b"\0")
    except Exception:
        return {}
    files = {}
    for meta, name in zip(fields[0::2], fields[1::2]):
        name = name.decode("utf-8", "replace")
        if re.search(r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|[._](test|spec)\.[^/]*$|\.uts$", name):
            blob = subprocess.run(vr.git_args(["cat-file", "blob", meta.split()[3].decode()]),
                                  env=vr.git_env(), capture_output=True)
            files[name] = blob.stdout.decode("utf-8", "replace")
    return files
def titled(name, text):
    """The name is a run of string literals of the text joined by spaces, as jest joins describe and test titles."""
    lits = {m.group(2) for m in re.finditer(r"""(['"`])(.+?)\1""", text)}
    words, ends = name.split(" "), {0}
    for i in range(1, len(words) + 1):
        if any(" ".join(words[j:i]) in lits for j in ends):
            ends.add(i)
    return len(words) in ends
def planted(rn, added):
    """A test file the agent added can stand in for a relevant test: it is the test's own file, or it
    declares the test's Go function in the same package, or it names the test's title."""
    if "::" in rn and rn.split("::", 1)[0] in added:
        return True
    go = re.match(r"(.+?)/((?:Test|Benchmark|Example|Fuzz)[^/]*)", rn)
    if go:
        word = go.group(2)
        texts = [t for f, t in added.items()
                 if f.endswith("_test.go") and (go.group(1) + "/").endswith("/" + os.path.dirname(f) + "/")]
    else:
        word = rn.split(" > ")[-1].split("::")[-1]
        texts = list(added.values())
    return any(re.search(r"(?<!\w)%s(?!\w)" % re.escape(word), t) or titled(word, t) for t in texts)
def matches_loose(full_name, rn):
    return matches_strict(full_name, rn) or rn in full_name or full_name in rn
def runner_rc(phase):
    try:
        with open("/tmp_artifact_storage/%s_runner_rc.txt" % phase) as f:
            return int(f.read().strip() or "0")
    except Exception:
        return None
def run_counts(summary, prefix):
    return {k: int(summary.get("%s_%s" % (prefix, k), 0) or 0) for k in ("passed", "failed", "skipped")}
def counts_from_run(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return {"passed": 0, "failed": 0, "skipped": 0}
    return {k: int(d.get("%s_test_count" % k, len(d.get("%s_tests" % k) or [])) or 0) for k in ("passed", "failed", "skipped")}
relevant_names = []
try:
    with open(rel_file) as f:
        relevant_names = as_names(json.load(f).get("tests", []))
except Exception as e:
    print("  WARNING: could not read relevant_test_names.json: %s" % e, file=sys.stderr)
# Declared drops (/tests/problematic_tests.json): neither required nor a violation; an unknown reason fails the gate.
try:
    with open("/tests/problematic_tests.json") as f:
        problematic = {k: (v or {}).get("reason") for k, v in json.load(f).items()}
    problematic_invalid = not all(r in ("flaky", "timeout", "broken_by_design") for r in problematic.values())
except FileNotFoundError:
    problematic, problematic_invalid = {}, False
except Exception:  # present but malformed: fail the gate
    problematic, problematic_invalid = {}, True
problematic = {} if problematic_invalid else {rn: problematic[rn] for rn in relevant_names if rn in problematic}
relevant_names = [rn for rn in relevant_names if rn not in problematic]
rc = {p: runner_rc(p) for p in ("agent_baseline", "agent_mutation")}
details = {"master_validator_exit_code": mv_exit, "runner_rc": rc, "baseline_truncated": rc["agent_baseline"] in (124, 137),
           "problematic_tests": problematic}
tests_reward = 0.0
try:
    with open(comp_file) as f:
        c = json.load(f)
except Exception as e:
    error = ("master validator exited with code %d; no comparison results (%s)" % (mv_exit, e)
             if mv_exit != 0 else "no comparison results: %s" % e)
    with open("/tmp_artifact_storage/comparison_results.json", "w") as f:
        json.dump({"error": error, "tests_reward": 0.0}, f)
    baseline_counts = counts_from_run("/tmp_artifact_storage/agent_baseline_results.json")
    mutation_counts = counts_from_run("/tmp_artifact_storage/agent_mutation_results.json")
    details["tests_executed"] = sum(baseline_counts.values()) + sum(mutation_counts.values()) > 0
    details.update({"baseline_empty": sum(baseline_counts.values()) == 0, "mutation_empty": sum(mutation_counts.values()) == 0})
    tests = vr.tests_block(False, "regression", baseline_counts, mutation_counts, vr.empty_transitions(),
                           relevant_names, vr.empty_transitions(), [], error=error, details=details)
else:
    cell = {k: as_names(c.get(v, [])) for k, v in CELLS.items()}
    accept_names = [t for k in ACCEPT for t in cell[k]]
    fail_names = [t for k in CELLS if k not in ACCEPT + ("f2f",) for t in cell[k]]
    # Every relevant test must be kept green (p2p) or added green (m2p) under an exact match. A test
    # failing before and after the patch (f2f) is neutral: the patch must not fix a broken test.
    # p2f, p2m, f2p, f2m, m2f and absent fail closed, as does a list with no accepted test at all,
    # and so does a relevant test that a test file the agent added could stand in for.
    planted_names = []
    if not relevant_names:
        violations = ["<no relevant tests declared>"]
    else:
        added = added_test_files()
        planted_names = [rn for rn in relevant_names if planted(rn, added)]
        accepted = [rn for rn in relevant_names
                    if rn not in planted_names and any(matches_strict(t, rn) for t in accept_names)]
        violations = [rn for rn in relevant_names
                      if rn in planted_names or any(matches_strict(t, rn) for t in fail_names)
                      or (rn not in accepted and not any(matches_strict(t, rn) for t in cell["f2f"]))]
        if not accepted:
            violations.append("<no relevant test is p2p or m2p>")
    seen = {t for names in cell.values() for t in names}
    absent = [rn for rn in violations if not any(matches_loose(t, rn) for t in seen)]
    if mv_exit != 0:
        violations.append("<master validator exited with code %d>" % mv_exit)
    if problematic_invalid:
        violations.append("<problematic_tests.json invalid>")
    # An edit to a protected test file (tests/test_files.json) fails the tests gate, as the
    # task text promises; the hidden tests still run and the rubrics are still judged.
    try:
        with open("/tmp_artifact_storage/test_files_modified.json") as f:
            tfm = json.load(f)
        protected_modified = list(tfm.get("flagged_test_files", []))
        if tfm.get("test_files_error"):
            protected_modified.append("<%s>" % tfm["test_files_error"])
    except Exception:
        protected_modified = ["<test_files_modified.json unreadable>"]
    for tf in protected_modified:
        violations.append("<protected test file modified: %s>" % tf)
    for gf in sorted(set(os.environ.get("GRADER_FILES_CHANGED", "").split())):
        violations.append("<grader file modified: %s>" % gf)
    details["protected_test_files_modified"] = protected_modified
    details["planted_relevant_tests"] = planted_names
    # A baseline run killed by TEST_TIMEOUT_SEC (rc 124/137) never reached some tests; a relevant
    # test then seen only as m2p was truncated, not added, and counts as failing.
    truncated = []
    if details["baseline_truncated"]:
        for rn in relevant_names:
            if rn not in violations and any(matches_strict(t, rn) for t in cell["m2p"]):
                violations.append(rn); truncated.append(rn)
    summary = c.get("summary", {}) or {}
    baseline_counts = run_counts(summary, "run1")
    mutation_counts = run_counts(summary, "run2")
    # An empty mutation run, or an empty baseline whose runner failed, compared nothing: fail closed.
    # (A baseline may be empty with rc 0 when every scoped test file is added by the test patch.)
    details.update({"baseline_empty": sum(baseline_counts.values()) == 0, "mutation_empty": sum(mutation_counts.values()) == 0})
    if details["mutation_empty"]:
        violations.append("<mutation ran no tests>")
    if details["baseline_empty"] and rc["agent_baseline"] != 0:
        violations.append("<baseline ran no tests>")
    tests_reward = 1.0 if not violations else 0.0
    filtered = {v: [t for t in cell[k] if any(matches_loose(t, rn) for rn in relevant_names)] for k, v in CELLS.items()}
    # The gate itself matches strictly; this view shows the relevant tests exactly as the gate saw them.
    filtered_strict = {v: [t for t in cell[k] if any(matches_strict(t, rn) for rn in relevant_names)] for k, v in CELLS.items()}
    with open("/tmp_artifact_storage/comparison_results.json", "w") as f:
        json.dump({"full_comparison": c, "filtered_comparison": filtered,
                   "filtered_summary": {k: len(v) for k, v in filtered.items()},
                   "filtered_summary_strict": {k: len(v) for k, v in filtered_strict.items()},
                   "relevant_test_count": len(relevant_names), "relevant_violations": violations,
                   "violation_count": len(violations), "uncollected_relevant_tests": absent,
                   "truncated_relevant_tests": truncated, "problematic_tests": problematic, "runner_rc": rc,
                   "tests_reward": tests_reward}, f, indent=2)
    details.update({"tests_executed": sum(baseline_counts.values()) + sum(mutation_counts.values()) > 0,
                    "relevant_violations": violations, "uncollected_relevant_tests": absent,
                    "truncated_relevant_tests": truncated,
                    "relevant_transitions_strict": {k: len(v) for k, v in filtered_strict.items()},
                    "new_tests": len(c.get("new_tests", []) or []), "removed_tests": len(c.get("removed_tests", []) or [])})
    if truncated:
        print("  Baseline run hit TEST_TIMEOUT_SEC (rc %s): %d relevant test(s) seen only as m2p scored as failing"
              % (rc["agent_baseline"], len(truncated)))
    if protected_modified:
        print("  Protected test files modified by the agent: %d (tests gate fails): %s" % (len(protected_modified), protected_modified[:10]))
    if planted_names:
        print("  Relevant tests a test file the agent added could stand in for: %d (tests gate fails): %s" % (len(planted_names), planted_names[:10]))
    print("  Relevant tests: %d declared, %d violating (must be p2p or m2p; f2f is neutral): %s" % (len(relevant_names), len(violations), violations[:20]))
    tests = vr.tests_block(tests_reward >= 1.0, "regression", baseline_counts, mutation_counts,
                           {v: len(cell[k]) for k, v in CELLS.items()}, relevant_names,
                           {k: len(v) for k, v in filtered.items()}, violations, details=details)
with open("/tmp_artifact_storage/tests_block.json", "w") as f:
    json.dump(tests, f)
vr.print_tests_summary(tests)
PYEOF
# ── Rubric evaluation (LLM-graded); prints the RUBRICS section ──
"${PY[@]}" /tests/evaluate_rubrics.py || echo "WARNING: evaluate_rubrics.py exited non-zero" >&2

echo ""
echo "=== MANIFEST ==="
echo "  not applicable to this track"

# ── Combine: reward = 1 iff the patch is not empty, the relevant tests hold AND every must-have passes ──
WORKSPACE="$WORKSPACE" PATCH_SIZE="$PATCH_SIZE" MV_EXIT="$MV_EXIT" "${PY[@]}" << 'PYEOF'
import json, os, sys
sys.path.append("/tests")
import verifier_report as vr


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


tests = load("/tmp_artifact_storage/tests_block.json")
rubric_data = load("/tmp_artifact_storage/rubrics_results.json")
modified = load("/tmp_artifact_storage/test_files_modified.json") or {}

error = None
rubrics = vr.rubrics_block([])
if rubric_data is None:
    error = "rubric evaluation produced no results"
elif "error" in rubric_data:
    error = rubric_data["error"]
elif "rubric_scores" in rubric_data:
    # The judge hands over the shared rubrics block plus the rf-only
    # empty-patch fields, which belong in details.
    rubrics = {k: v for k, v in rubric_data.items()
               if k not in ("empty_patch", "empty_patch_positive_passes", "note")}

details = {
    "workspace": os.environ.get("WORKSPACE"),
    "agent_patch_bytes": int(os.environ.get("PATCH_SIZE") or 0),
    "master_validator_exit_code": int(os.environ.get("MV_EXIT") or 1),
    "test_files_modified": modified.get("test_files_modified", False),
    "flagged_test_files": modified.get("flagged_test_files", []),
    "empty_patch": (rubric_data or {}).get("empty_patch"),
    # Positive rubrics the judge accepted on an empty patch: satisfiable by
    # doing nothing, so they need rewording. Filled by the nop run.
    "empty_patch_positive_passes": (rubric_data or {}).get("empty_patch_positive_passes", []),
}

must_have_pass = bool(rubrics and rubrics.get("pass"))
tests_pass = bool(tests and tests.get("pass"))
overall_pass = tests_pass and must_have_pass and details["agent_patch_bytes"] > 0
if not details["agent_patch_bytes"]:
    error = error or "empty agent patch"

vr.finish(vr.report("rf", overall_pass, rubrics=rubrics, tests=tests, manifest=None,
                    error=error, details=details))
PYEOF
rc=$?

if [ -z "$TESTS_SHA" ] || [ "$(tests_sha)" != "$TESTS_SHA" ]; then
    score_zero "a file in /tests changed during grading"
    exit 0
fi

# Keep the verdict only if this run wrote it; otherwise score 0 with the reason.
"${PY[@]}" /tests/verifier_report.py seal rf "$rc" || score_zero "the seal step failed"

exit 0
