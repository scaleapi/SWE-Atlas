#!/usr/bin/env python3
"""One output contract for the qa, tw and rf verifiers.

Every track leaves the same five files under /logs/verifier/ and prints the
same sections to stdout, so a trial can be read without knowing its track.

    reward.txt               "1\\n" or "0\\n"
    reward.json              the harbor metrics (same twelve keys in every track)
    evaluation_results.json  the verdict, in the schema below
    test-stdout.txt          harbor's capture of the verifier's stdout+stderr
    integrity.json           what prepare/seal found: processes stopped, files
                             removed from this directory and from /tests, git
                             config dropped (tw, rf)
    artifacts/               track debug material, never read by the grader:
                             agent.patch (tw, rf), baseline_/mutation_{stdout,stderr}.log
                             (tw, rf), master_validator.log (rf); absent for qa

Harbor tars /logs/verifier recursively, so the subfolder survives. /logs/artifacts
does not work for this: harbor collects it before the verifier runs.

evaluation_results.json (schema_version 1):

    {
      "schema_version": 1,
      "track": "qa" | "tw" | "rf",
      "verifier_run_id": str | null,  # VERIFIER_RUN_ID of the test.sh run that wrote this
      "reward": 0 | 1,
      "pass": bool,                  # the verdict reward.txt reports
      "error": null | str,           # why the verifier stopped early, if it did
      "judge": {"model": str, "dialect": str, "reasoning_effort": str, "votes": int | null},
      "rubrics": null | {            # null only when tw skips the judge
        "pass": bool,                # every must-have was rated and passed
        "agg_score": float,          # passed / rated, 0.0 when nothing was rated
        "judge_unusable": bool,      # the judge was asked and rated none of the must-haves
        "not_graded": bool,          # no rubric was rated, whatever the reason
        "num_rubrics": int,
        "num_must_have": int,
        "num_rated": int,
        "num_unrated": int,          # UNRATED + SKIPPED
        "num_skipped": int,          # left unjudged on purpose (rf negatives on an empty patch)
        "num_unrated_must_have": int,# must-haves the judge was asked about but never rated
        "num_passed": int,
        "rubric_scores": [{            # flat, mirrors the top level's reward/error
          "id": str, "title": str,
          "importance": "must have" | "nice to have",
          "type": "positive" | "negative",
          "reward": 0 | 1,           # 1 when the rubric is satisfied; 0 when not or ungraded
          "error": null | str,       # null when graded; otherwise why not:
                                     # "judge returned no usable rating", or
                                     # "not judged: <reason>" when skipped on purpose
          "justification": str | null   # the judge's reasoning; null when ungraded
        }]
      },
      "tests": null | {              # null for qa, which runs no tests
        "pass": bool,
        "kind": "mutation" | "regression",   # tw: relevant tests must fail after the
                                             # skeleton swap; rf: they must not regress
        "baseline": {"passed": int, "failed": int, "skipped": int},
        "mutation": {"passed": int, "failed": int, "skipped": int},
        "transitions": {<bucket>: int},          # whole suite
        "relevant_tests": [str],
        "relevant_transitions": {<bucket>: int}, # relevant tests only
        "blocking_tests": [str],     # relevant tests that make the gate fail
        "error": null | str,
        "details": {...}             # track-specific
      },
      "manifest": null | {"pass": bool, "justification": str},   # tw only
      "details": {...},              # track-specific
      "integrity": null | {...}      # the integrity.json record at the time of writing
    }

    <bucket> is one of pass_to_pass, pass_to_fail, pass_to_missing, fail_to_pass,
    fail_to_fail, fail_to_missing, missing_to_pass, missing_to_fail. A skipped test
    counts as missing in that run.

stdout layout, in this order:

    === INTEGRITY ===                          # printed by test.sh before the grader
      Processes stopped before grading: 0
      /logs/verifier was empty before grading

    === VERIFIER (track=<track>) ===
    Judge config: model=<model> dialect=<dialect> reasoning_effort=<effort> votes=<votes>

    === TESTS ===
      ...
      Tests pass: True|False (...)               # or "  not applicable to this track"

    === RUBRICS ===
      Rubric <id>: PASS|FAIL|UNRATED|SKIPPED (<type>, <importance>[, <error>][, <note>][, votes YES 2, NO 1])
      Rubrics pass: True|False (agg_score: 0.909, 10/11 passed, 0 unrated must-have)

    === MANIFEST ===
      Manifest pass: True|False[ (votes YES 2, NO 1)]   # or "  not applicable to this track"

    === RESULT: reward=1 pass=True (rubrics=PASS, tests=n/a, manifest=n/a) ===

    === SEALED: reward=1 run=<id> grader_exit=0 processes_stopped_after_grading=0 ===

Only the standard library is used, so the module imports under any task image.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# Under -I the script directory is not on sys.path, and judge_client lives next to this file.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

SCHEMA_VERSION = 1
TRACKS = ("qa", "tw", "rf")
VERIFIER_DIR = "/logs/verifier"
ARTIFACTS_DIR = VERIFIER_DIR + "/artifacts"

TRANSITION_BUCKETS = (
    "pass_to_pass",
    "pass_to_fail",
    "pass_to_missing",
    "fail_to_pass",
    "fail_to_fail",
    "fail_to_missing",
    "missing_to_pass",
    "missing_to_fail",
)

# stdout is block-buffered under harbor while stderr is not, so without this
# a warning printed to stderr lands above the section it belongs to.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# stdout
# ---------------------------------------------------------------------------

def out(line=""):
    print(line, flush=True)


def warn(line):
    print(line, file=sys.stderr, flush=True)


def judge_config():
    """(model, reasoning_effort, dialect, votes) as judge_client resolves them."""
    import judge_client
    cfg = judge_client.JudgeConfig.from_env()
    effort = judge_client.MODEL_DEFAULT_EFFORT if judge_client.effort_dropped() else cfg.effort
    return cfg.model, effort, cfg.dialect, cfg.votes


def print_header(track):
    model, effort, dialect, votes = judge_config()
    out("=== VERIFIER (track=%s) ===" % track)
    out("Judge config: model=%s dialect=%s reasoning_effort=%s votes=%s" % (model, dialect, effort, votes or "invalid"))


def print_section(name):
    out()
    out("=== %s ===" % name)


def print_not_applicable():
    out("  not applicable to this track")


def print_skipped(reason):
    """A section the verifier stopped before reaching."""
    out("  skipped (%s)" % reason)


def rubric_word(entry):
    """PASS, FAIL, UNRATED or SKIPPED, read off the record."""
    if entry["error"] is None:
        return "PASS" if entry["reward"] == 1 else "FAIL"
    return "SKIPPED" if entry["error"].startswith(SKIPPED_PREFIX) else "UNRATED"


def rubric_line(entry, note=None):
    """The one-line record of a rubric verdict, the same in every track."""
    parts = [entry["type"], entry["importance"]]
    if entry["error"] is not None:
        parts.append(entry["error"])
    if note:
        parts.append(note)
    return "  Rubric %s: %s (%s)" % (entry["id"], rubric_word(entry), ", ".join(parts))


def print_rubric(entry, note=None):
    out(rubric_line(entry, note))


def print_rubrics_summary(rubrics):
    out("  Rubrics pass: %s (agg_score: %.3f, %d/%d passed, %d unrated must-have)" % (
        rubrics["pass"], rubrics["agg_score"], rubrics["num_passed"],
        rubrics["num_rated"], rubrics["num_unrated_must_have"]))


def print_tests_summary(tests):
    if tests.get("error"):
        out("  Tests pass: %s (kind=%s, error: %s)" % (tests["pass"], tests["kind"], tests["error"]))
        return
    rel = tests.get("relevant_transitions") or {}
    if tests["kind"] == "mutation":
        killed = len(tests.get("relevant_tests") or []) - len(tests.get("blocking_tests") or [])
        variant = (tests.get("details") or {}).get("equivalent_variant") or {}
        extra = ""
        if variant.get("not_passed"):
            extra = ", %d failed the behaviour-preserving variant" % len(variant["not_passed"])
        out("  Tests pass: %s (kind=mutation, %d/%d relevant tests killed by the skeleton swap%s)" % (
            tests["pass"], killed, len(tests.get("relevant_tests") or []), extra))
    else:
        out("  Tests pass: %s (kind=regression, relevant P2F=%d M2F=%d of %d relevant tests)" % (
            tests["pass"], rel.get("pass_to_fail", 0), rel.get("missing_to_fail", 0),
            len(tests.get("relevant_tests") or [])))


def print_manifest_summary(manifest, note=None):
    out("  Manifest pass: %s%s" % (manifest["pass"], " (%s)" % note if note else ""))


def _gate(block):
    if block is None:
        return "n/a"
    return "PASS" if block.get("pass") else "FAIL"


def print_result(report):
    line = "=== RESULT: reward=%d pass=%s (rubrics=%s, tests=%s, manifest=%s)" % (
        report["reward"], report["pass"], _gate(report["rubrics"]),
        _gate(report["tests"]), _gate(report["manifest"]))
    if report.get("error"):
        line += " error=%s" % json.dumps(report["error"])
    out()
    out(line + " ===")


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def rubric_importance(rubric):
    """'must have' unless the rubric says otherwise, in either place authors put it."""
    annotations = rubric.get("annotations") or {}
    return annotations.get("importance") or rubric.get("importance") or "must have"


def rubric_type(rubric):
    annotations = rubric.get("annotations") or {}
    return "negative" if "negative" in str(annotations.get("type", "")).lower() else "positive"


UNRATED_ERROR = "judge returned no usable rating"
SKIPPED_PREFIX = "not judged: "


def skipped_error(reason):
    return SKIPPED_PREFIX + reason


def rubric_entry(rubric_id, title, importance, kind, reward=None, justification=None, error=None):
    """One rubric_scores element, flat, mirroring the top level's reward/error.

    Graded: reward 0/1 with the judge's justification and error null. Not
    graded: error says why (UNRATED_ERROR, or skipped_error(reason)), reward
    is 0 and justification null.
    """
    if kind not in ("positive", "negative"):
        raise ValueError("bad rubric type %r" % (kind,))
    if error is None:
        if reward not in (0, 1):
            raise ValueError("graded rubric needs reward 0 or 1, got %r" % (reward,))
        reward = int(reward)
    else:
        reward = 0
        justification = None
    return {
        "id": rubric_id,
        "title": title,
        "importance": importance,
        "type": kind,
        "reward": reward,
        "error": error,
        "justification": justification,
    }


def rubrics_block(entries, num_unrated_must_have=None, judge_attempted=True):
    """Aggregate rubric entries into the shared rubrics block.

    Each track keeps its own gate semantics; this only counts. The gate itself
    (pass) is the shared one: every must-have rated and passed. Pass
    num_unrated_must_have when the track counts it differently from the
    plain "must-have and not evaluable" (rf excludes the skipped negatives).
    """
    def is_skipped(e):
        return e["error"] is not None and e["error"].startswith(SKIPPED_PREFIX)

    must_haves = [e for e in entries if e["importance"] == "must have"]
    rated = [e for e in entries if e["error"] is None]
    passed = [e for e in rated if e["reward"] == 1]
    skipped = [e for e in entries if is_skipped(e)]
    unrated_must = [e for e in must_haves if e["error"] is not None and not is_skipped(e)]
    if num_unrated_must_have is None:
        num_unrated_must_have = len(unrated_must)
    judged_must = [e for e in must_haves if not is_skipped(e)]
    return {
        "pass": bool(must_haves) and all(e in passed for e in must_haves),
        "agg_score": (len(passed) / float(len(rated))) if rated else 0.0,
        "judge_unusable": (bool(judge_attempted) and bool(judged_must)
                           and not any(e["error"] is None for e in judged_must)),
        "not_graded": not rated,
        "num_rubrics": len(entries),
        "num_must_have": len(must_haves),
        "num_rated": len(rated),
        "num_unrated": len(entries) - len(rated),
        "num_skipped": len(skipped),
        "num_unrated_must_have": num_unrated_must_have,
        "num_passed": len(passed),
        "rubric_scores": entries,
    }


def _state(status):
    status = str(status or "").strip().upper()
    if status in ("PASSED", "PASS", "OK"):
        return "pass"
    if status in ("FAILED", "FAIL", "ERROR"):
        return "fail"
    return "missing"


def run_counts(tests):
    """{'passed', 'failed', 'skipped'} over a list of {name, status} records."""
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    for t in tests or []:
        state = _state(t.get("status"))
        if state == "pass":
            counts["passed"] += 1
        elif state == "fail":
            counts["failed"] += 1
        else:
            counts["skipped"] += 1
    return counts


def transitions(baseline_tests, mutation_tests, names=None):
    """Bucket counts between two runs of {name, status} records.

    names restricts the count to those test names; None means every test seen.
    """
    b = {t["name"]: _state(t.get("status")) for t in baseline_tests or [] if t.get("name")}
    m = {t["name"]: _state(t.get("status")) for t in mutation_tests or [] if t.get("name")}
    universe = set(b) | set(m)
    if names is not None:
        universe &= set(names)
    counts = {bucket: 0 for bucket in TRANSITION_BUCKETS}
    for name in universe:
        key = "%s_to_%s" % (b.get(name, "missing"), m.get(name, "missing"))
        if key in counts:
            counts[key] += 1
    return counts


def empty_transitions():
    return {bucket: 0 for bucket in TRANSITION_BUCKETS}


def tests_block(passed, kind, baseline, mutation, transitions_all, relevant_tests,
                relevant_transitions, blocking_tests, error=None, details=None):
    if kind not in ("mutation", "regression"):
        raise ValueError("bad tests kind %r" % kind)
    return {
        "pass": bool(passed),
        "kind": kind,
        "baseline": baseline,
        "mutation": mutation,
        "transitions": transitions_all,
        "relevant_tests": sorted(relevant_tests or []),
        "relevant_transitions": relevant_transitions,
        "blocking_tests": sorted(blocking_tests or []),
        "error": error,
        "details": details or {},
    }


def manifest_block(passed, justification):
    return {"pass": bool(passed), "justification": justification or ""}


def report(track, passed, rubrics=None, tests=None, manifest=None, error=None, details=None):
    if track not in TRACKS:
        raise ValueError("bad track %r" % track)
    model, effort, dialect, votes = judge_config()
    return {
        "schema_version": SCHEMA_VERSION,
        "track": track,
        "reward": 1 if passed else 0,
        "pass": bool(passed),
        "error": error,
        "judge": {"model": model, "dialect": dialect, "reasoning_effort": effort, "votes": votes},
        "rubrics": rubrics,
        "tests": tests,
        "manifest": manifest,
        "details": details or {},
        "verifier_run_id": os.environ.get(RUN_ID_VAR) or None,
        "integrity": _load_integrity(),
    }


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

def artifacts_dir(path=ARTIFACTS_DIR):
    """The per-trial folder for logs and patches worth keeping; created on demand."""
    if not os.path.isdir(path):
        os.makedirs(path)
    return path


def harbor_rewards(report_obj):
    """The metrics harbor averages over the job.

    Harbor treats a key a trial omits as 0, so every track emits the same set.
    """
    rubrics = report_obj.get("rubrics") or {}
    tests = report_obj.get("tests") or {}
    track = report_obj["track"]
    return {
        "reward": float(report_obj["reward"]),
        "must_have_pass": 1.0 if rubrics.get("pass") else 0.0,
        "overall_pass": 1.0 if report_obj["pass"] else 0.0,
        "rubrics_agg_score": float(rubrics.get("agg_score") or 0.0),
        "tests_pass": 1.0 if tests.get("pass") else 0.0,
        "tests_applicable": 0.0 if track == "qa" else 1.0,
        "judge_unusable": 1.0 if rubrics.get("judge_unusable") else 0.0,
        "not_graded": 1.0 if rubrics.get("not_graded") else 0.0,
        # Must-have rubrics the judge never rated. Each one fails the task, so
        # a non-zero value marks a verdict worth re-checking.
        "num_unrated_must_have": float(rubrics.get("num_unrated_must_have") or 0),
        "is_qna": 1.0 if track == "qa" else 0.0,
        "is_tw": 1.0 if track == "tw" else 0.0,
        "is_rf": 1.0 if track == "rf" else 0.0,
    }


def write(report_obj, out_dir=VERIFIER_DIR):
    """Write reward.txt, reward.json and evaluation_results.json."""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    # Each file is written whole and renamed into place, so a reader (harbor, or
    # seal) never sees a half-written reward.
    _write_atomic(os.path.join(out_dir, "evaluation_results.json"), json.dumps(report_obj, indent=2))
    _write_atomic(os.path.join(out_dir, "reward.txt"), "%d\n" % report_obj["reward"])
    _write_atomic(os.path.join(out_dir, "reward.json"), json.dumps(harbor_rewards(report_obj), indent=2))


def _write_atomic(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def finish(report_obj, out_dir=VERIFIER_DIR):
    """Write the files and print the RESULT line."""
    write(report_obj, out_dir)
    print_result(report_obj)
    return report_obj


def fail_early(track, error, rubrics=None, tests=None, manifest=None, details=None,
               out_dir=VERIFIER_DIR):
    """Score 0 with a reason, printing the sections the verifier never reached."""
    warn("ERROR: %s" % error)
    if rubrics is None:
        # Nothing was graded, so this 0 is not a statement about the submission.
        rubrics = rubrics_block([])
    return finish(report(track, False, rubrics=rubrics, tests=tests, manifest=manifest,
                         error=error, details=details), out_dir)


# ---------------------------------------------------------------------------
# grading-time integrity
# ---------------------------------------------------------------------------
#
# Harbor can run these verifiers inside the agent's own container (shared-verifier
# mode), so when test.sh starts the agent may have left processes running,
# files in /logs/verifier, or git configuration that executes commands, and the
# agent's own test code runs again during grading. test.sh calls `prepare`
# before grading and `seal` after it. The graders run every git command through
# git_args()/git_shell() with git_env(), and every piece of agent code (test
# runners, builds) with scrub_env(), which drops the judge credentials and the
# run id. Once the agent's code has run, `stop-agent-code` stops whatever it left
# running, before the verdict is written.
#
#   prepare <track>   stop every process outside this verifier's ancestry (PID 1,
#                     its keepalive and harbor's LiteLLM relay excepted), empty /logs/verifier, record
#                     what was found, with the files test.sh removed from /tests
#                     ($VERIFIER_TESTS_REMOVED), in /logs/verifier/integrity.json
#   stop-agent-code   stop every process outside this verifier's ancestry, as
#                     prepare does, and record it in integrity.json
#   harden-git <dir>  rewrite <dir>/.git/config keeping only harmless sections,
#                     remove hooks and info/attributes
#   seal <track> <rc> stop anything started during grading, then keep the
#                     verdict only if this run wrote it (VERIFIER_RUN_ID matches
#                     and the three files agree) and the grader exited 0;
#                     otherwise write reward 0 with the reason

RUN_ID_VAR = "VERIFIER_RUN_ID"
TESTS_REMOVED_VAR = "VERIFIER_TESTS_REMOVED"
INTEGRITY_FILE = VERIFIER_DIR + "/integrity.json"
SECRET_PREFIXES = ("JUDGE_", "OPENAI_", "EVAL_", "ANTHROPIC_", "VERIFIER_")
GIT_SAFE_HOME_VAR = "GIT_SAFE_HOME"
# Per-call overrides. core.fsmonitor= (empty) disables the hook on every git version
# without naming an executable; core.fsmonitor=false would run a program called
# "false" on git < 2.36. safe.directory is honoured from -c (protected config).
GIT_FLAGS = [
    "--no-pager",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.excludesFile=/dev/null",
    "-c", "safe.directory=*",
]
# .git/config sections kept by harden_git; every other section (filter.*,
# diff.*, merge.*, alias.*, include.*, includeIf.*, credential.*, url.*, ...)
# is dropped, as are these keys inside core.
GIT_KEEP_SECTIONS = ("core", "extensions", "user", "init", "index", "status", "submodule")
GIT_DROP_KEYS = {
    "core.hookspath", "core.fsmonitor", "core.fsmonitorhookversion",
    "core.attributesfile", "core.excludesfile", "core.sshcommand", "core.gitproxy",
    "core.askpass", "core.editor", "core.pager", "core.alternaterefscommand",
    "core.worktree",
}
# The container's own init chain is spared: on docker that is PID 1 `sh -c "sleep
# infinity"` and its sleep, on modal `dumb-init` -> `sh -c "sleep infinity"` -> sleep.
# A process belongs to it only if all three hold: its parent is in the chain, its
# command line is one of harbor's keepalive shapes, and it started within a few
# seconds of PID 1. An agent can reparent and rename a process, but cannot backdate
# its start time, and the agent phase starts well after the container does.
# A keepalive `sh -c "sleep infinity"` can fork its sleep seconds later, so a child of
# one of these shapes (LATE_KEEPALIVE_ARGV) is spared at any start time when its
# parent is a keepalive that passed the start-time check (never PID 1 itself, which
# adopts orphans).
KEEPALIVE_ARGV = (
    ["sh", "-c", "sleep infinity"], ["bash", "-c", "sleep infinity"], ["dash", "-c", "sleep infinity"],
    ["sleep", "infinity"], ["tail", "-f", "/dev/null"],
)
LATE_KEEPALIVE_ARGV = (["sleep", "infinity"], ["sh", "-c", "sleep infinity"])
# Harbor's LiteLLM relay is spared too: for runs whose model URL is the LiteLLM proxy,
# harbor starts `python3 /tmp/_harbor_litellm_relay_<port>.py` in the sandbox and points
# JUDGE_BASE_URL at it, so stopping it leaves the judge unreachable. A process is the
# relay when its interpreter is python and one of its arguments is that script path.
RELAY_SCRIPT_PREFIX = "/tmp/_harbor_litellm_relay_"
RELAY_SCRIPT_SUFFIX = ".py"
INIT_CHAIN_MAX_DELAY_SEC = 3
PF_KTHREAD = 0x00200000
STOP_SCAN_PASSES = 50

_SAFE_HOME = None


GIT_SAFE_CONFIG = "[safe]\n\tdirectory = *\n"


def _safe_home():
    """A private HOME for git: the agent's ~/.gitconfig is never read, and the only global
    config is ours, with safe.directory = *, which every git version honours (2.35.2 to
    2.37 ignore -c safe.directory, and images where the verifier is not the repository's
    owner relied on their own global config for this)."""
    global _SAFE_HOME
    if _SAFE_HOME is None:
        _SAFE_HOME = os.environ.get(GIT_SAFE_HOME_VAR) or tempfile.mkdtemp(prefix="verifier-home-")
        os.environ[GIT_SAFE_HOME_VAR] = _SAFE_HOME
    cfg = os.path.join(_SAFE_HOME, ".gitconfig")
    if not os.path.isfile(cfg):
        try:
            with open(cfg, "w") as f:
                f.write(GIT_SAFE_CONFIG)
        except OSError as e:
            warn("WARNING: could not write %s: %s" % (cfg, e))
    return _SAFE_HOME


def scrub_env(base=None):
    """A copy of the environment without the judge credentials and the verifier's own variables."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key.startswith(SECRET_PREFIXES):
            del env[key]
    return env


def git_env(base=None):
    """Environment for a git call: no system or XDG configuration, and only our global config."""
    env = dict(os.environ if base is None else base)
    home = _safe_home()
    env.update({
        "HOME": home,
        "XDG_CONFIG_HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.path.join(home, ".gitconfig"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        GIT_SAFE_HOME_VAR: home,
    })
    return env


def git_args(args):
    return ["git"] + GIT_FLAGS + list(args)


def git_shell(args):
    """git_args as one shell string, for callers that run through a shell."""
    return " ".join(shlex.quote(a) for a in git_args(args))


def _git(args, cwd=None):
    return subprocess.run(git_args(args), cwd=cwd, env=git_env(), capture_output=True,
                          text=True, errors="replace")


def _keep_git_key(key):
    lower = key.lower()
    section = lower.split(".", 1)[0]
    if lower in GIT_DROP_KEYS:
        return False
    if section == "submodule" and lower.endswith(".update"):
        return False
    return section in GIT_KEEP_SECTIONS


def _sanitize_git_config(path):
    """Rewrite one git config file with only the kept keys; return the dropped ones."""
    listing = _git(["config", "--file", path, "--list", "--no-includes", "-z"])
    pairs = []
    if listing.returncode == 0:
        for entry in listing.stdout.split("\0"):
            if not entry:
                continue
            key, _, value = entry.partition("\n")
            pairs.append((key, value))
    else:
        warn("WARNING: could not read %s (%s); replacing it with a minimal config"
             % (path, listing.stderr.strip()))
        pairs = [("core.repositoryformatversion", "0"), ("core.filemode", "true"),
                 ("core.bare", "false"), ("core.logallrefupdates", "true")]
    kept = [(k, v) for k, v in pairs if _keep_git_key(k)]
    dropped = sorted(set(k for k, v in pairs if not _keep_git_key(k)))
    tmp = path + ".verifier.tmp"
    with open(tmp, "w"):
        pass
    for key, value in kept:
        _git(["config", "--file", tmp, "--add", key, value])
    os.replace(tmp, path)
    return dropped


def harden_git(repo):
    """Neutralise agent-controlled git configuration in repo before the graders run git there.

    Returns a record for integrity.json. Never raises: a repository the graders
    cannot read is reported and left to them to fail on.
    """
    record = {"repo": repo, "git_dir": None, "dropped_keys": [], "hooks_removed": [],
              "attributes_removed": False, "error": None}
    probe = _git(["rev-parse", "--absolute-git-dir"], cwd=repo)
    if probe.returncode != 0:
        record["error"] = "not a git repository: %s" % probe.stderr.strip()
        _update_integrity({"git": record})
        return record
    git_dir = probe.stdout.strip()
    record["git_dir"] = git_dir
    try:
        for name in ("config", "config.worktree"):
            cfg = os.path.join(git_dir, name)
            if os.path.isfile(cfg):
                record["dropped_keys"] += ["%s: %s" % (name, k) for k in _sanitize_git_config(cfg)]
        hooks = os.path.join(git_dir, "hooks")
        if os.path.isdir(hooks):
            for entry in sorted(os.listdir(hooks)):
                path = os.path.join(hooks, entry)
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
                if not entry.endswith(".sample"):
                    record["hooks_removed"].append(entry)
        attributes = os.path.join(git_dir, "info", "attributes")
        if os.path.lexists(attributes):
            os.unlink(attributes)
            record["attributes_removed"] = True
    except OSError as e:
        record["error"] = "while sanitising %s: %s" % (git_dir, e)
        warn("WARNING: " + record["error"])
    _update_integrity({"git": record})
    return record


def _procs():
    """{pid: {ppid, state, comm, argv, starttime, kthread}} for every process in /proc."""
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open("/proc/%d/stat" % pid) as f:
                stat = f.read()
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except (OSError, ValueError):
            continue
        close = stat.rfind(")")
        fields = stat[close + 2:].split()
        if len(fields) < 2:
            continue
        try:
            starttime = int(fields[19])  # field 22 of /proc/<pid>/stat, in clock ticks since boot
        except (IndexError, ValueError):
            starttime = 0
        procs[pid] = {"ppid": int(fields[1]), "state": fields[0],
                      "comm": stat[stat.find("(") + 1:close], "argv": argv, "starttime": starttime,
                      "kthread": len(fields) > 6 and fields[6].isdigit() and int(fields[6]) & PF_KTHREAD != 0}
    return procs


def _init_chain(procs):
    """PID 1 plus the keepalive processes under it (see KEEPALIVE_ARGV)."""
    chain = {1} if 1 in procs else set()
    if not chain:
        return chain
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        ticks = 100
    limit = procs[1]["starttime"] + INIT_CHAIN_MAX_DELAY_SEC * ticks
    shapes = [list(a) for a in KEEPALIVE_ARGV]
    late_shapes = [list(a) for a in LATE_KEEPALIVE_ARGV]
    timed = set()
    grew = True
    while grew:
        grew = False
        for pid, info in procs.items():
            if pid in chain or info["ppid"] not in chain or not info["argv"]:
                continue
            argv = [os.path.basename(info["argv"][0])] + info["argv"][1:]
            if argv in shapes and info["starttime"] <= limit:
                chain.add(pid)
                timed.add(pid)
                grew = True
            elif argv in late_shapes and info["ppid"] in timed:
                chain.add(pid)
                grew = True
    return chain


def _is_harbor_relay(info):
    """True for harbor's LiteLLM relay (see RELAY_SCRIPT_PREFIX)."""
    argv = info["argv"]
    if len(argv) < 2 or not os.path.basename(argv[0]).startswith("python"):
        return False
    for arg in argv[1:]:
        port = arg[len(RELAY_SCRIPT_PREFIX):-len(RELAY_SCRIPT_SUFFIX)]
        if (arg.startswith(RELAY_SCRIPT_PREFIX) and arg.endswith(RELAY_SCRIPT_SUFFIX)
                and port.isdigit()):
            return True
    return False


def _describe(pid, info):
    return "%d %s" % (pid, " ".join(info["argv"])[:120] or info["comm"])


def _alive(pid, info):
    if info["state"] != "Z":
        return True
    try:
        return len(os.listdir("/proc/%d/task" % pid)) > 1
    except OSError:
        return False


def stop_foreign_processes():
    """SIGKILL every process that is not this verifier's ancestry, the container's init
    chain or harbor's LiteLLM relay.

    Each one is first stopped with SIGSTOP and /proc is scanned again until a pass
    finds no new process (at most STOP_SCAN_PASSES), so none can fork its way out;
    then all of them are killed. Kernel threads (PF_KTHREAD) and zombies with no live
    thread are left alone. Returns what was stopped, what could not be and which
    relay processes were spared, for integrity.json.
    """
    procs = _procs()
    keep = _init_chain(procs) | {1}
    pid = os.getpid()
    while pid in procs and pid not in keep:
        keep.add(pid)
        pid = procs[pid]["ppid"]
    targets, seen, stopped, failed, spared = {}, set(), [], [], {}
    for _ in range(STOP_SCAN_PASSES):
        for p, info in procs.items():
            if p not in keep and _is_harbor_relay(info):
                spared[(p, info["starttime"])] = _describe(p, info)
        new = {p: info for p, info in procs.items()
               if p not in keep and (p, info["starttime"]) not in seen
               and (p, info["starttime"]) not in spared
               and not info["kthread"] and _alive(p, info)}
        if not new:
            break
        for p, info in new.items():
            seen.add((p, info["starttime"]))
            try:
                os.kill(p, signal.SIGSTOP)
                targets[p] = info
            except ProcessLookupError:
                pass
            except OSError as e:
                failed.append(_describe(p, info) + " (%s)" % e.strerror)
        procs = _procs()
    for p, info in targets.items():
        try:
            os.kill(p, signal.SIGKILL)
            stopped.append(_describe(p, info))
        except ProcessLookupError:
            pass
        except OSError as e:
            failed.append(_describe(p, info) + " (%s)" % e.strerror)
    deadline = time.time() + 5
    now = {}
    while targets and time.time() < deadline:
        now = _procs()
        if not any(p in now and _alive(p, now[p]) for p in targets):
            break
        time.sleep(0.05)
    survivors = [_describe(p, targets[p]) for p in targets
                 if p in now and _alive(p, now[p]) and _describe(p, targets[p]) not in failed]
    return {"stopped": stopped, "not_stopped": failed + survivors, "spared": sorted(spared.values())}


def _open_output_inodes():
    """(dev, inode) of this process's stdout and stderr: harbor opens test-stdout.txt
    in /logs/verifier before test.sh starts, and that file must survive the sweep."""
    inodes = set()
    for fd in (1, 2):
        try:
            st = os.fstat(fd)
            inodes.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    return inodes


def clear_verifier_dir(out_dir=VERIFIER_DIR):
    """Remove everything in /logs/verifier except the live stdout capture; return the names removed."""
    if os.path.islink(out_dir):
        os.unlink(out_dir)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
        return []
    keep = _open_output_inodes()
    names = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        try:
            st = os.lstat(path)
            if (st.st_dev, st.st_ino) in keep:
                continue
        except OSError:
            pass
        names.append(name)
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except OSError as e:
            warn("WARNING: could not remove %s: %s" % (path, e))
    return names


def _load_integrity():
    try:
        with open(INTEGRITY_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _update_integrity(fields):
    data = _load_integrity() or {}
    data.update(fields)
    try:
        if not os.path.isdir(VERIFIER_DIR):
            os.makedirs(VERIFIER_DIR)
        _write_atomic(INTEGRITY_FILE, json.dumps(data, indent=2))
    except OSError as e:
        warn("WARNING: could not write %s: %s" % (INTEGRITY_FILE, e))
    return data


def prepare(track):
    """Before grading: stop leftover processes, empty /logs/verifier, print INTEGRITY."""
    out("=== INTEGRITY ===")
    procs = stop_foreign_processes()
    cleared = clear_verifier_dir()
    tests_removed = os.environ.get(TESTS_REMOVED_VAR, "").split()
    record = {"run_id": os.environ.get(RUN_ID_VAR), "track": track,
              "prepare": {"processes": procs, "verifier_dir_cleared": cleared,
                          "tests_removed": tests_removed}}
    _update_integrity(record)
    out("  Processes stopped before grading: %d%s" % (
        len(procs["stopped"]), "" if not procs["stopped"] else " (" + "; ".join(procs["stopped"][:5]) + ")"))
    if procs["not_stopped"]:
        out("  WARNING: %d process(es) could not be stopped: %s"
            % (len(procs["not_stopped"]), "; ".join(procs["not_stopped"][:5])))
    if procs["spared"]:
        out("  Harbor relay processes left running: %d (%s)"
            % (len(procs["spared"]), "; ".join(procs["spared"][:5])))
    if cleared:
        out("  WARNING: /logs/verifier was not empty before grading; removed: %s" % ", ".join(cleared[:10]))
    else:
        out("  /logs/verifier was empty before grading")
    if tests_removed:
        out("  WARNING: /tests held files the task does not ship; removed: %s" % ", ".join(tests_removed[:10]))
    return record


def stop_agent_code():
    """After the grader ran the agent's code: stop whatever it left running."""
    procs = stop_foreign_processes()
    _update_integrity({"after_agent_code": {"processes": procs}})
    out("  Processes stopped after the agent's code ran: %d%s" % (
        len(procs["stopped"]), "" if not procs["stopped"] else " (" + "; ".join(procs["stopped"][:5]) + ")"))
    if procs["not_stopped"]:
        out("  WARNING: %d process(es) could not be stopped: %s"
            % (len(procs["not_stopped"]), "; ".join(procs["not_stopped"][:5])))
    return procs


def seal(track, grader_rc):
    """After grading: stop anything started meanwhile and make sure the verdict is this run's."""
    procs = stop_foreign_processes()
    run_id = os.environ.get(RUN_ID_VAR) or None
    problem = None
    reward = None
    try:
        with open(os.path.join(VERIFIER_DIR, "evaluation_results.json")) as f:
            results = json.load(f)
        with open(os.path.join(VERIFIER_DIR, "reward.json")) as f:
            metrics = json.load(f)
        with open(os.path.join(VERIFIER_DIR, "reward.txt")) as f:
            text = f.read().strip()
        reward = int(results.get("reward"))
        if run_id and results.get("verifier_run_id") != run_id:
            problem = "the verdict on disk was not written by this verifier run"
        elif float(metrics.get("reward")) != float(reward) or text != str(reward):
            problem = "reward.txt, reward.json and evaluation_results.json disagree"
    except (OSError, ValueError, TypeError) as e:
        problem = "no readable verdict from this run (%s: %s)" % (type(e).__name__, e)
    if grader_rc != 0 and problem is None and reward != 0:
        problem = "the grader exited with code %d" % grader_rc
    _update_integrity({"seal": {"grader_exit_code": grader_rc, "processes": procs,
                                "tests_removed_after_prepare": os.environ.get(TESTS_REMOVED_VAR, "").split(),
                                "problem": problem}})
    if problem is not None:
        fail_early(track, "no trustworthy verdict: %s; scored 0" % problem,
                   details={"grader_exit_code": grader_rc})
        reward = 0
    out("=== SEALED: reward=%d run=%s grader_exit=%d processes_stopped_after_grading=%d%s ==="
        % (reward, run_id or "-", grader_rc, len(procs["stopped"]),
           "" if problem is None else " (verdict replaced: %s)" % problem))
    return problem is None


# ---------------------------------------------------------------------------
# CLI, for test.sh
# ---------------------------------------------------------------------------

def _main(argv):
    if len(argv) >= 2 and argv[0] == "header":
        print_header(argv[1])
        return 0
    if len(argv) >= 3 and argv[0] == "fail":
        fail_early(argv[1], argv[2])
        return 0
    if len(argv) >= 2 and argv[0] == "prepare":
        prepare(argv[1])
        return 0
    if argv[:1] == ["stop-agent-code"]:
        stop_agent_code()
        return 0
    if len(argv) >= 2 and argv[0] == "harden-git":
        record = harden_git(argv[1])
        out("  Git configuration in %s: %d key(s) dropped, %d hook(s) removed%s" % (
            argv[1], len(record["dropped_keys"]), len(record["hooks_removed"]),
            "" if not record["error"] else "; " + record["error"]))
        return 0
    if len(argv) >= 3 and argv[0] == "seal":
        try:
            rc = int(argv[2])
        except ValueError:
            rc = 1
        seal(argv[1], rc)
        return 0
    print("usage: verifier_report.py header <track> | fail <track> <error> | prepare <track> | "
          "stop-agent-code | harden-git <dir> | seal <track> <grader_exit_code>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
