#!/usr/bin/env python3
"""Full-suite grading helper for the rf benchmark: baked baseline, staleness stamp, infra marker.

Lives directly under /tests (no subdirectory), because test.sh's tests_sha() opens every top-level entry of /tests
as a file and a subdirectory would break it.

  baked_base.py synth OUT      write the baseline results JSON (run_script format) from the baked stanza,
                               but only after the 3-field staleness stamp matches; else exit 3 (-> infra).
  baked_base.py stamp          print this container's actual stamp as JSON.
  baked_base.py base_commit    the commit test.sh should diff the agent's work against (root, or one rebuilt from
                               the stamped base_tree when the agent rewrote the root commit).
  baked_base.py infra REASON [PHASE RC CAP ELAPSED]   write /logs/verifier/infra_error.json.

The baked stanza is /tests/baked_base.json: {"tests": {name: "PASSED"|"FAILED"|"SKIPPED"}, "baked_base":
{"count": N, "stamp": {base_tree, runner, image_id}}}. synth writes the per-name statuses that were stable across the
live base runs the stanza was built from; names whose status varied between those runs are left out.

Fail closed: any stamp/stanza mismatch exits 3 with /tmp_artifact_storage/fs_reason; the caller (fs_hooks.sh) turns
that into an infra exit (no reward, non-zero exit -> harbor errors and re-runs the trial, never a wrong score). The
gate is base_tree + runner + image_id. image_id is the declared FS_IMAGE_ID (task.toml [verifier.env]); a mismatch
means the baseline was captured on a different image. The live fallback (FULL_SUITE_BASE=live) is always available
and is the reference.
"""
import hashlib, json, os, re, subprocess, sys

TESTS = os.environ.get("FS_TESTS_DIR", "/tests")
LOG = os.environ.get("FS_LOG_DIR", "/logs/verifier")
ART = os.environ.get("FS_ARTIFACT_DIR", "/tmp_artifact_storage")
STANZA = os.path.join(TESTS, "baked_base.json")
# Bytes that determine the baseline per-test statuses + how they are compared. A change to any of these can change
# the baked baseline's validity, so they are fingerprinted into the stamp and a drift fails closed.
RUNNER_FILES = ("run_script.sh", "compare_test_runs.py", "master_validator_script.sh")
SHA = re.compile(r"^[0-9a-f]{40}$")
PIN = {"GIT_AUTHOR_NAME": "base", "GIT_AUTHOR_EMAIL": "base@localhost", "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
       "GIT_COMMITTER_NAME": "base", "GIT_COMMITTER_EMAIL": "base@localhost", "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
       "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def runner_sha(tests_dir, read=None):
    read = read or (lambda f: open(os.path.join(tests_dir, f), "rb").read()
                    if os.path.isfile(os.path.join(tests_dir, f)) else None)
    h = hashlib.sha256()
    for f in RUNNER_FILES:
        b = read(f)
        if b is not None:
            h.update(f.encode() + b"\0" + sha(b).encode() + b"\n")
    return h.hexdigest()


def run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        return p.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        return "<%s>" % type(e).__name__


def root_commit(ws="."):
    return run("git -C %s rev-list --max-parents=0 HEAD | tail -1" % ws)


def base_tree(ws="."):
    root = os.environ.get("BASE_COMMIT", "")
    root = root if SHA.match(root) else root_commit(ws)
    return run("git -C %s rev-parse %s^{tree}" % (ws, root)) if SHA.match(root) else None


def is_tree(t, ws="."):
    return bool(t and SHA.match(t)) and run("git -C %s cat-file -t %s" % (ws, t)) == "tree"


def stamped_tree():
    try:
        return ((load_stanza()[0].get("stamp") or {}).get("base_tree")) or None
    except (LookupError, ValueError, OSError):
        return None


def base_commit(ws="."):
    """The live root commit, unless the agent rewrote it (amend/squash) and the stamped base tree still exists in the
    repo, in which case a commit is rebuilt from that tree with a pinned author/date/message (same sha every run)."""
    root, want = root_commit(ws), stamped_tree()
    if not is_tree(want, ws) or (SHA.match(root) and run("git -C %s rev-parse %s^{tree}" % (ws, root)) == want):
        return root
    try:
        p = subprocess.run(["git", "-C", ws, "commit-tree", "--no-gpg-sign", "-m", "base", want],
                           env=dict(os.environ, **PIN), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        c = p.stdout.decode().strip()
    except Exception:
        c = ""
    if not SHA.match(c):
        return root
    print("baked_base: root %s is not the image base tree; base rebuilt as %s" % (root[:12], c[:12]), file=sys.stderr)
    return c


def actual_stamp():
    return {"base_tree": base_tree(), "runner": runner_sha(TESTS), "image_id": os.environ.get("FS_IMAGE_ID")}


def load_stanza():
    if not os.path.isfile(STANZA):
        raise LookupError("baked_stanza_missing")
    d = json.load(open(STANZA))
    if not d.get("baked_base") or not isinstance(d.get("tests"), dict):
        raise LookupError("baked_stanza_missing")
    return d["baked_base"], dict(d["tests"]), d


def stale_fields(want, have):
    return [k for k in ("base_tree", "runner", "image_id") if not want.get(k) or want.get(k) != have.get(k)]


def fail(reason):
    try:
        os.makedirs(ART, exist_ok=True)
        open(os.path.join(ART, "fs_reason"), "w").write(reason)
    except OSError:
        pass
    print("baked_base: " + reason, file=sys.stderr)
    sys.exit(3)


def synth(out):
    try:
        have = json.load(open(os.path.join(LOG, "stamp_actual.json")))
    except (OSError, ValueError):
        have = actual_stamp()
    try:
        st, exp, _ = load_stanza()
    except LookupError as e:
        fail(str(e))
    except ValueError as e:
        fail("baked_stanza_unreadable:%s" % e)
    bad = stale_fields(st.get("stamp") or {}, have)
    if bad == ["base_tree"] and (st.get("stamp") or {}).get("base_tree") and not is_tree(st["stamp"]["base_tree"]):
        # runner + image match, only the base tree is gone from the repo: the agent rewrote history -> a real agent
        # problem, scored 0 (agent_fail), not infra.
        os.makedirs(ART, exist_ok=True)
        open(os.path.join(ART, "agent_fail"), "w").write("base_tree_gone")
        print("baked_base: image base tree gone from the repo (runner+image match): agent_fail", file=sys.stderr)
        sys.exit(3)
    if bad:
        fail("stale_stamp:" + ";".join(bad))
    if st.get("count") != len(exp):
        fail("baked_count_mismatch:%s!=%s" % (st.get("count"), len(exp)))
    buckets = {"PASSED": [], "FAILED": [], "SKIPPED": []}
    for n, s in sorted(exp.items()):
        if s not in buckets:
            fail("baked_bad_status:%s=%s" % (n, s))
        buckets[s].append({"name": n})
    doc = {"passed_test_count": len(buckets["PASSED"]), "failed_test_count": len(buckets["FAILED"]),
           "skipped_test_count": len(buckets["SKIPPED"]), "passed_tests": buckets["PASSED"],
           "failed_tests": buckets["FAILED"], "skipped_tests": buckets["SKIPPED"], "exit_code": 0,
           "baked_base": True}
    json.dump(doc, open(out, "w"), indent=2)
    print("baked baseline: %d tests (stamp ok) -> %s" % (len(exp), out))


def infra(reason, phase="", rc="", cap="", elapsed=""):
    num = lambda v: int(v) if str(v).lstrip("-").isdigit() else (v or None)
    d = {"reason": reason, "mode": os.environ.get("FULL_SUITE_MODE", "full"),
         "base": os.environ.get("FULL_SUITE_BASE", "baked"), "phase": phase or None, "rc": num(rc),
         "cap": num(cap), "elapsed": num(elapsed), "reward": None, "infra_error": True}
    os.makedirs(LOG, exist_ok=True)
    json.dump(d, open(os.path.join(LOG, "infra_error.json"), "w"), indent=2)


def main(a):
    if a[:1] == ["synth"] and len(a) >= 2:
        synth(a[1])
    elif a[:1] == ["base_commit"]:
        print(base_commit())
    elif a[:1] == ["stamp"]:
        print(json.dumps(actual_stamp(), indent=1))
    elif a[:1] == ["infra"] and len(a) >= 2:
        infra(*a[1:6])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
