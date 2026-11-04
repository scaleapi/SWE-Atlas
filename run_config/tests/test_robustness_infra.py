#!/usr/bin/env python3
"""Checks that infrastructure failures end a trial as a retried error, not a scored 0.

Run with the harbor venv's python (it imports harbor):
    <venv>/bin/python run_config/tests/test_robustness_infra.py

Part A  set_retry_args in common.sh builds the retry flags the launchers pass.
Part B  harbor's own TrialQueue, configured with those flags, retries every infrastructure,
        agent-proxy and timeout error (and an unknown one) and never retries a real outcome -- and
        harbor's untouched default would not retry an agent timeout, which is why we override it.
Part C  summary.py leaves the still-erroring infrastructure trials out of pass@1, counts a real
        reward 0, an unsealed missing reward and an unsealed judge-unusable trial as 0, and drops a
        verifier-sealed infra trial.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_CONFIG = os.path.dirname(HERE)

sys.path.insert(0, RUN_CONFIG)
import summary  # noqa: E402

from harbor.models.job.config import RetryConfig  # noqa: E402
from harbor.trial.queue import TrialQueue  # noqa: E402

RESULTS = []


def check(part, name, cond, detail=""):
    RESULTS.append((part, name, bool(cond), detail))
    print("  %s [%s] %s%s" % ("PASS" if cond else "FAIL", part, name, "" if cond else "  -- " + detail))


# ── Part A: the flags common.sh builds ───────────────────────────────────────
def retry_args(max_retries=None):
    env = dict(os.environ)
    if max_retries is not None:
        env["MAX_RETRIES"] = str(max_retries)
    else:
        env.pop("MAX_RETRIES", None)
    out = subprocess.run(
        ["bash", "-c", 'source "$1"/common.sh; set_retry_args; printf "%s\\n" "${RETRY_ARGS[@]}"', "_", RUN_CONFIG],
        capture_output=True, text=True, env=env)
    return out.returncode, out.stdout.strip().split("\n") if out.stdout.strip() else []


def parse_flags(tokens):
    mr, excludes, includes = None, [], []
    i = 0
    while i < len(tokens):
        if tokens[i] == "--max-retries":
            mr = tokens[i + 1]; i += 2
        elif tokens[i] == "--retry-exclude":
            excludes.append(tokens[i + 1]); i += 2
        elif tokens[i] == "--retry-include":
            includes.append(tokens[i + 1]); i += 2
        else:
            i += 1
    return mr, excludes, includes


EXPECT_NO_RETRY = {"AgentSafetyRefusalError", "AgentAuthenticationError", "ModelNotFoundError",
                   "ApiUsageLimitError", "OutputTokenExceededError", "ContextWindowExceededError",
                   "OutputLengthExceededError", "ContextLengthExceededError",
                   "VerifierOutputParseError", "RewardFileEmptyError"}
# Infrastructure, agent-proxy and timeout errors harbor raises (trial/errors.py, verifier/verifier.py,
# environments/base.py, agents/installed/base.py), plus an unseen type, must all be retried.
EXPECT_RETRY = {"EnvironmentStartTimeoutError", "HealthcheckError", "SandboxBuildFailedError",
                "AgentSetupTimeoutError", "AddTestsDirError", "DownloadVerifierDirError",
                "AgentTimeoutError", "VerifierTimeoutError", "RewardFileNotFoundError",
                "ApiRateLimitError", "ApiInternalServerError", "ApiOverloadedError",
                "ApiConnectionClosedError", "ApiResponseStalledError", "NetworkConnectionError",
                "UnknownApiError", "ApiProviderResourceNotFoundError", "NonZeroAgentExitCodeError",
                "SomeFutureProxyErrorWeHaveNotSeen"}


def part_a():
    rc, tokens = retry_args()  # no MAX_RETRIES -> default
    mr, excludes, _ = parse_flags(tokens)
    check("A", "default max-retries is 3", rc == 0 and mr == "3", "got rc=%s mr=%r" % (rc, mr))
    check("A", "exclude list is exactly the real-outcome set", set(excludes) == EXPECT_NO_RETRY,
          "got %r" % sorted(excludes))
    check("A", "no --retry-include (harbor retries all else)", "--retry-include" not in tokens)
    rc0, t0 = retry_args(0)
    mr0, _, _ = parse_flags(t0)
    check("A", "MAX_RETRIES=0 turns retries off", rc0 == 0 and mr0 == "0", "got rc=%s mr=%r" % (rc0, mr0))
    rcbad, _ = retry_args("three")
    check("A", "a non-numeric MAX_RETRIES is rejected", rcbad != 0, "got rc=%s" % rcbad)
    return set(excludes)


# ── Part B: harbor's own retry decision ───────────────────────────────────────
def part_b(excludes):
    cfg = RetryConfig(max_retries=3, include_exceptions=None, exclude_exceptions=set(excludes))
    q = TrialQueue(n_concurrent=1, retry_config=cfg)
    for name in sorted(EXPECT_RETRY):
        check("B", "retry " + name, q._should_retry_exception(name) is True)
    for name in sorted(EXPECT_NO_RETRY):
        check("B", "no retry " + name, q._should_retry_exception(name) is False)
    # Contrast: harbor's untouched default would not retry an agent timeout or a no-reward verifier.
    dq = TrialQueue(n_concurrent=1, retry_config=RetryConfig(max_retries=3))
    check("B", "default would NOT retry AgentTimeoutError (so we override it)",
          dq._should_retry_exception("AgentTimeoutError") is False)
    check("B", "default would NOT retry RewardFileNotFoundError (so we override it)",
          dq._should_retry_exception("RewardFileNotFoundError") is False)


# ── Part C: summary.py exclusion ──────────────────────────────────────────────
def make_trial(job, name, reward=None, error=None, not_graded=False, infra=None, sealed=None):
    d = os.path.join(job, name)
    v = os.path.join(d, "verifier")
    os.makedirs(v, exist_ok=True)
    rewards = {}
    if reward is not None:
        rewards["reward"] = float(reward)
    if not_graded:
        rewards["not_graded"] = 1.0
    result = {"task_name": name, "source": "rf", "finished_at": "2026-01-01T00:00:00Z",
              "verifier_result": {"rewards": rewards},
              "exception_info": ({"exception_type": error} if error else None),
              "config": {"agent": {"name": "sota", "model_name": "m"}}}
    with open(os.path.join(d, "result.json"), "w") as f:
        json.dump(result, f)
    if infra is not None:
        with open(os.path.join(v, "infra_error.json"), "w") as f:
            json.dump(infra, f)
    with open(os.path.join(v, "test-stdout.txt"), "w") as f:
        f.write((sealed or "") + "\n")
    return d


def part_c():
    job = tempfile.mkdtemp(prefix="rbtest-")
    rid, secret = "runid-0001", "abc123"
    make_trial(job, "rf-pass", reward=1)                                   # real pass
    make_trial(job, "rf-fail", reward=0)                                   # real fail (count 0)
    make_trial(job, "rf-timeout", error="AgentTimeoutError")               # infra -> drop
    make_trial(job, "rf-proxy", error="ApiOverloadedError")               # infra -> drop
    make_trial(job, "rf-env", error="EnvironmentStartTimeoutError")        # infra -> drop
    make_trial(job, "rf-modalimg", error="ImageBuildError")                # Modal image pull -> drop
    make_trial(job, "rf-unknown", error="SomeFutureInfraError")            # unseen infra -> drop
    make_trial(job, "rf-judge", reward=0, not_graded=True)                  # judge unusable, no seal -> count 0
    make_trial(job, "rf-judge-sealed", reward=0, not_graded=True,
               infra={"infra_error": True, "verifier_run_id": rid, "seal_secret": secret, "reason": "judge down"},
               sealed=summary.SEALED_INFRA % (rid, secret) + "processes_stopped_after_grading=0 ===")  # sealed -> drop
    make_trial(job, "rf-refusal", error="AgentSafetyRefusalError")         # real outcome -> count 0
    make_trial(job, "rf-ctxlen", error="ContextLengthExceededError")       # in-process overflow -> count 0
    make_trial(job, "rf-outlen", error="OutputLengthExceededError")        # in-process overflow -> count 0
    # Forgeable from a root container: dropped only when the verifier sealed it, else scored 0.
    make_trial(job, "rf-vtimeout", error="VerifierTimeoutError")           # no seal -> count 0
    make_trial(job, "rf-agentexit", error="NonZeroAgentExitCodeError")     # no seal -> count 0
    make_trial(job, "rf-vtimeout-sealed", error="VerifierTimeoutError",
               infra={"infra_error": True, "verifier_run_id": rid, "seal_secret": secret, "reason": "verifier down"},
               sealed=summary.SEALED_INFRA % (rid, secret) + "processes_stopped_after_grading=0 ===")  # sealed -> drop
    make_trial(job, "rf-nornf", error="RewardFileNotFoundError")           # no seal -> count 0
    make_trial(job, "rf-sealed", error="RewardFileNotFoundError",
               infra={"infra_error": True, "verifier_run_id": rid, "seal_secret": secret, "reason": "judge down"},
               sealed=summary.SEALED_INFRA % (rid, secret) + "processes_stopped_after_grading=0 ===")  # sealed -> drop
    make_trial(job, "rf-forged", reward=0, error="RewardFileNotFoundError",
               infra={"infra_error": True, "verifier_run_id": rid, "seal_secret": "WRONG"},
               sealed=summary.SEALED_INFRA % (rid, secret))                   # mismatched seal -> count 0

    ts = {t["task"]: t for t in summary.trials(job, os.path.join(summary.REPO, "data"))}
    drop = {"rf-timeout", "rf-proxy", "rf-env", "rf-modalimg", "rf-unknown", "rf-judge-sealed",
            "rf-sealed", "rf-vtimeout-sealed"}
    keep = {"rf-pass", "rf-fail", "rf-refusal", "rf-ctxlen", "rf-outlen",
            "rf-vtimeout", "rf-agentexit", "rf-nornf", "rf-forged", "rf-judge"}
    for name in drop:
        check("C", name + " is not scored (infra)", bool(ts[name]["not_scored"]), ts[name]["not_scored"])
    for name in keep:
        check("C", name + " is scored", not ts[name]["not_scored"], str(ts[name]["not_scored"]))

    all_ts = list(ts.values())
    n_all, _, p1_all, _ = summary.scores(all_ts)
    n_scored, _, p1, pk = summary.scores(all_ts, scored_only=True)
    # Scored tasks: pass(1) + 9 x 0 = 1/10. The unsealed verifier-timeout, agent-exit and judge-unusable
    # count as 0 (forgeable, not dropped); agent-timeout, env/proxy/image and the sealed trials are dropped.
    check("C", "pass@1 over scored trials is 1/10 (8 infra trials dropped)", abs(p1 - 1 / 10) < 1e-9,
          "p1=%.4f n_scored=%d" % (p1, n_scored))
    check("C", "scored-trial count is 10 of 18", n_scored == 10 and n_all == 18,
          "n_scored=%d n_all=%d" % (n_scored, n_all))
    # Counting every trial as 0: 1 pass / 18.
    check("C", "pass@1 (not scored = 0) counts all 18 trials", abs(p1_all - 1 / 18) < 1e-9, "p1_all=%.4f" % p1_all)


def main():
    print("Part A: retry flags from common.sh")
    excludes = part_a()
    print("Part B: harbor TrialQueue retry decisions")
    part_b(excludes)
    print("Part C: summary.py pass@1 exclusion")
    part_c()
    failed = [r for r in RESULTS if not r[2]]
    print("\n%d checks, %d passed, %d failed" % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
