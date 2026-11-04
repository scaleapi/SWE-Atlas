#!/usr/bin/env python3
"""Scores of harbor jobs, and a check of an oracle and a nop job.

  summary.py <dir> [<dir> ...]           one row per job: tasks, trials, unfinished, errors, retries, not graded,
                                         not scored, pass@1, pass@1 (not scored = 0), pass@k
  summary.py --gate <dir> [<dir> ...]    check oracle and nop jobs over data/

A <dir> is a harbor job directory of any lane, or a directory above jobs (results/, results/qa/),
which stands for every job below it. oracle.sh and nop.sh print this table when they end, and it
also summarizes a run_config/job.yaml run's job directory.
A trial is "not scored" when its failure was infrastructure, not the submission: harbor ended it on
an environment, sandbox, agent-proxy or timeout error (it recorded an exception_info and no reward),
the judge could not be used (judge_unusable / not_graded), or the verifier left no reward and sealed
the run as infrastructure. pass@1 and pass@k leave those out, over the tasks with at least one scored
trial; pass@1 (not scored = 0) counts every trial, a not-scored one as 0. A verifier that was killed,
hung or whose host was sabotaged cannot produce that seal, and an agent that exited non-zero or timed
out the verifier cannot either, so those score 0 rather than being dropped; any other trial without a
reward (unfinished, no seal) counts as 0. The trials not scored are listed by reason after the table:
with retries on they should be rare; re-run any left.

A qa agent job gets a second row without the 7 tasks in REFUSED, which some models decline to
answer; the first row counts refused trials as failures.
--gate exits 1 unless every task in data/ has an oracle and a nop trial, every oracle trial scores 1,
every nop trial scores 0, no trial errored, and every trial ran on the task bytes now in data/
(the digest harbor records in each trial's lock.json). --data <dir> checks against another data/.
Every task lives directly in data/, named <lane>-<id>; a lane's tasks are the directories whose
name starts with that lane's prefix.
"""
import hashlib, json, os, subprocess, sys
from collections import Counter, defaultdict
from fnmatch import fnmatch
from functools import lru_cache

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANES = ("qa", "rf", "tw")
REFUSED = tuple("qa-6905333b74f22949d97" + t for t in ("ba9bb", "ba9c0", "ba9cb", "ba9f1", "baa19", "baa1d", "baa2c"))
EXPECTED = {"oracle": 1.0, "nop": 0.0}
# A trial that harbor ended on an exception is counted as 0 (the submission's own outcome) only for
# these types; every other exception — an environment, sandbox, image-pull (harbor and Modal both
# raise their own names here), agent-proxy or timeout failure, including a type not seen yet — is an
# infrastructure outcome left out of the score, the same set the launchers retry. A model refusal is
# a real 0 (the agent produced nothing), as is a missing/rejected key, an unusable model, an
# exhausted usage limit, a reply over the token or context budget, and a grader that could not parse
# or wrote an empty reward. RewardFileNotFoundError is handled separately: dropped only when sealed.
SCORED_ERRORS = ("AgentSafetyRefusalError", "AgentAuthenticationError", "ModelNotFoundError",
                 "ApiUsageLimitError", "OutputTokenExceededError", "ContextWindowExceededError",
                 "OutputLengthExceededError", "ContextLengthExceededError",
                 "VerifierOutputParseError", "RewardFileEmptyError")
# Errors a root agent could stage during grading (hang the verifier, exit the agent non-zero, delete
# the reward) to turn a would-be 0 into a dropped trial: left out of the score only when the verifier
# sealed the run as infrastructure, otherwise scored 0 so a forged one cannot buy an exclusion.
SEAL_REQUIRED = ("RewardFileNotFoundError", "VerifierTimeoutError", "NonZeroAgentExitCodeError")
# A verifier that wrote no reward is dropped only when it sealed the run as infrastructure: its
# verifier directory holds an infra_error.json whose verifier_run_id and seal_secret match the SEALED
# line the same run left in test-stdout.txt. The secret is generated after the agent's code is
# stopped, so a killed or sabotaged verifier cannot forge it.
SEALED_INFRA = "=== SEALED: not graded (infrastructure error) run=%s grader_exit=3 seal=%s "
IGNORED = ("*.pyc", ".DS_Store", "*.swp", "*.swo", "*~")


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def task_digest(task_dir):
    """harbor's task digest: sha256 over '<path>\\0<sha256>\\n' of the files harbor packages."""
    files = [n for n in ("task.toml", "instruction.md", "README.md", "trajectory.json") if os.path.isfile(os.path.join(task_dir, n))]
    for sub in ("environment", "tests", "solution", "steps"):
        for root, _, names in os.walk(os.path.join(task_dir, sub)):
            files += [os.path.relpath(os.path.join(root, n), task_dir) for n in names]
    outer = hashlib.sha256()
    for rel in sorted(f.replace(os.sep, "/") for f in files):
        if "__pycache__" in rel.split("/") or any(fnmatch(rel.rsplit("/", 1)[-1], p) for p in IGNORED):
            continue
        with open(os.path.join(task_dir, rel), "rb") as f:
            outer.update(f"{rel}\0{hashlib.sha256(f.read()).hexdigest()}\n".encode())
    return "sha256:" + outer.hexdigest()


def in_lane(name):
    """True for a flat-layout name (<lane>-<id>) that belongs to one of the lanes."""
    return any(name.startswith(lane + "-") for lane in LANES)


@lru_cache(maxsize=None)
def members(data, lane):
    """The lane's task names: the directories in the flat data/ named <lane>-<id>;
    None when data/ cannot be read."""
    try:
        return tuple(sorted(n for n in os.listdir(data)
                            if n.startswith(lane + "-") and os.path.isdir(os.path.join(data, n))))
    except OSError:
        return None


def lane_of(task, source, data):
    """A trial's lane: harbor's source when it names a lane, else the lane whose prefix the task's
    name (<lane>-<id>) carries."""
    if source in LANES:
        return source
    return next((lane for lane in LANES if task.startswith(lane + "-")), source)


def sealed_infra_reason(trial_dir):
    """The verifier's infra reason when it left no reward and sealed the run as infrastructure, else
    None. Both the marker and the SEALED line must carry the same verifier_run_id and seal_secret."""
    record = load(os.path.join(trial_dir, "verifier", "infra_error.json"))
    run_id, secret = record.get("verifier_run_id"), record.get("seal_secret")
    if (record.get("infra_error") is not True or not isinstance(run_id, str) or not run_id
            or not isinstance(secret, str) or not secret):
        return None
    try:
        with open(os.path.join(trial_dir, "verifier", "test-stdout.txt"), errors="replace") as f:
            if SEALED_INFRA % (run_id, secret) not in f.read():
                return None
    except OSError:
        return None
    return "not graded: " + str(record.get("reason"))[:80]


def not_scored_reason(trial_dir, error, reward, not_graded):
    """Why a trial is an infrastructure outcome to leave out of pass@1, else None."""
    if error in SEAL_REQUIRED:
        return sealed_infra_reason(trial_dir)
    if error and error not in SCORED_ERRORS:
        return error
    if reward is not None and not_graded:
        return "judge unusable"
    return None


def trials(job_dir, data):
    out = []
    for name in sorted(os.listdir(job_dir)):
        d = os.path.join(job_dir, name)
        if not os.path.isdir(d) or not in_lane(name):
            continue
        r, lock = load(os.path.join(d, "result.json")), load(os.path.join(d, "lock.json"))
        agent = (r.get("config") or {}).get("agent") or {}
        task = (r.get("task_name") or name.split("__")[0]).split("/")[-1]
        rewards = (r.get("verifier_result") or {}).get("rewards") or {}
        error = (r.get("exception_info") or {}).get("exception_type")
        not_graded = bool(rewards.get("not_graded") or rewards.get("judge_unusable"))
        out.append({
            "dir": d,
            "task": task,
            "lane": lane_of(task, r.get("source") or (lock.get("task") or {}).get("source"), data),
            "agent": agent.get("name") or (r.get("agent_info") or {}).get("name"),
            "model": agent.get("model_name"),
            "done": bool(r.get("finished_at")),
            "reward": rewards.get("reward"),
            "not_graded": not_graded,
            "error": error,
            "not_scored": not_scored_reason(d, error, rewards.get("reward"), not_graded),
            "digest": (lock.get("task") or {}).get("digest"),
            "judge": load(os.path.join(d, "verifier", "evaluation_results.json")).get("judge") or {},
        })
    return out


def scores(ts, scored_only=False):
    by_task = defaultdict(list)
    for t in ts:
        if scored_only and t["not_scored"]:
            continue
        by_task[t["task"]].append(0.0 if t["not_scored"] else (t["reward"] or 0.0))
    n = len(by_task) or 1
    k = max((len(v) for v in by_task.values()), default=0)
    pass1 = sum(sum(v) / len(v) for v in by_task.values()) / n
    passk = sum(any(r >= 1 for r in v) for v in by_task.values()) / n
    return len(by_task), k, pass1, passk


def row(cells):
    print("| " + " | ".join(str(c) for c in cells) + " |")


def job_agent(job, ts):
    """Agent and model of a job; for a job with no finished trial, from its config (oracle is harbor's default)."""
    for t in ts:
        if t["agent"]:
            return t["agent"], t["model"] or ""
    cfg = load(os.path.join(job, "config.json"))
    agent = (cfg.get("agents") or [{"name": "oracle"} if cfg else {}])[0]
    return agent.get("name") or "", agent.get("model_name") or ""


def summary(jobs):
    row(("lane", "job", "agent", "model", "tasks", "trials", "unfinished", "errors", "retries", "not graded",
         "not scored", "pass@1", "pass@1 (not scored = 0)", "pass@k"))
    row(("---",) * 14)
    reasons = Counter()
    for job in jobs:
        ts = trials(job, os.path.join(REPO, "data"))
        retries = ((load(os.path.join(job, "result.json")).get("stats")) or {}).get("n_retries", "")
        lanes = sorted({t["lane"] or "?" for t in ts})
        agent, model = job_agent(job, ts)
        variants = [(os.path.basename(job), ts)]
        if lanes == ["qa"] and agent not in EXPECTED:
            variants.append((os.path.basename(job) + f", without the {len(REFUSED)} refused tasks",
                             [t for t in ts if t["task"] not in REFUSED]))
        for name, sub in variants:
            done = [t for t in sub if t["done"]]
            n, _, p1_all, _ = scores(sub)
            n_scored, k, p1, pk = scores(sub, scored_only=True)
            errors = ", ".join(f"{e} {c}" for e, c in Counter(t["error"] for t in done if t["error"]).most_common())
            unscored = [t for t in sub if t["not_scored"]]
            if name == variants[0][0]:
                reasons.update((os.path.basename(job), t["not_scored"]) for t in unscored)
            row(("/".join(lanes), name, agent, model,
                 n, len(done), len(sub) - len(done), errors or 0, retries, sum(t["not_graded"] for t in done),
                 len(unscored), f"{p1:.1%}" + (f" ({n_scored} tasks)" if n_scored != n else ""),
                 f"{p1_all:.1%}", f"{pk:.1%} (k={k})"))
    if reasons:
        print(f"\n{sum(reasons.values())} trials not scored (re-run them before reporting):")
        for (job, why), c in reasons.most_common():
            print(f"  {job}: {why} x{c}")


def judge(t):
    j = t["judge"]
    if not j.get("model"):
        return "not recorded"
    votes = f"{j['votes']} votes" if j.get("votes") else "votes not recorded"
    return f"{j['model']} (effort {j.get('reasoning_effort')}, {votes})"


def dataset(data, lane):
    """Digest of each of the lane's tasks: the directories in the flat data/ named <lane>-<id>."""
    names = members(data, lane)
    if names is None:
        raise ValueError(f"cannot read {data}")
    if not names:
        raise ValueError(f"no tasks for lane {lane} in data/ (no {lane}-* directories)")
    return {name: task_digest(os.path.join(data, name)) for name in names}


def gate(jobs, data):
    got = defaultdict(list)
    retries = Counter()
    failures, totals, lane_ok = [], Counter(), {}
    for job in jobs:
        ts = trials(job, data)
        for t in ts:
            got[(t["lane"], t["agent"])].append(t)
            if t["lane"] not in LANES:
                failures.append((str(t["lane"]), t["agent"] or "-", os.path.relpath(t["dir"]), "task name has no lane prefix"))
        for lane, agent in {(t["lane"], t["agent"]) for t in ts}:
            retries[(lane, agent)] += ((load(os.path.join(job, "result.json")).get("stats")) or {}).get("n_retries", 0)
    try:
        sha = subprocess.run(["git", "-C", data, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", data, "status", "--porcelain", "--", "."], capture_output=True, text=True).stdout.strip()
    except OSError:
        sha, dirty = "", ""
    print(f"Oracle and nop check for {os.path.abspath(data)} at {sha or 'unknown commit'}{' (uncommitted changes)' if dirty else ''}\n")
    row(("lane", "tasks", "oracle: reward 1", "nop: reward 0", "errored", "retries", "on the bytes in data/", "judge", "gate"))
    row(("---",) * 9)
    for lane in LANES:
        try:
            want = dataset(data, lane)
        except ValueError as e:
            want = {}
            failures.append((lane, "-", "-", str(e)))
        cells, errored, stale, ok, judges = {}, 0, 0, bool(want), Counter()
        for agent, expected in EXPECTED.items():
            ts = got.get((lane, agent), [])
            seen = {t["task"] for t in ts}
            for task in sorted(set(want) - seen):
                failures.append((lane, agent, task, "no trial"))
            hits = 0
            for t in ts:
                judges[judge(t)] += 1
                why = []
                if want.get(t["task"]) is None:
                    why.append("task not in data/")
                elif t["digest"] != want[t["task"]]:
                    why.append("ran on other task bytes")
                    stale += 1
                if not t["done"]:
                    why.append("unfinished")
                if t["error"]:
                    why.append(t["error"])
                    errored += 1
                if t["reward"] != expected:
                    why.append(f"reward {t['reward']}")
                else:
                    hits += 1
                failures += [(lane, agent, os.path.relpath(t["dir"]), "; ".join(why))] if why else []
            cells[agent] = f"{hits}/{len(ts)}"
            ok = ok and len(ts) > 0 and hits == len(ts) and seen >= set(want)
            totals[agent + "_hits"] += hits
            totals[agent + "_trials"] += len(ts)
        ok = ok and errored == 0 and stale == 0
        lane_ok[lane] = ok
        totals["tasks"] += len(want)
        row((lane, len(want), cells["oracle"], cells["nop"], errored, retries[(lane, "oracle")] + retries[(lane, "nop")],
             "yes" if stale == 0 else f"no ({stale} trials)",
             "; ".join(j if len(judges) == 1 else f"{j}: {c}" for j, c in judges.most_common()), "pass" if ok else "FAIL"))
    passed = not failures and all(lane_ok.values())
    row(("all", totals["tasks"], f"{totals['oracle_hits']}/{totals['oracle_trials']}",
         f"{totals['nop_hits']}/{totals['nop_trials']}", "", "", "", "", "pass" if passed else "FAIL"))
    if failures:
        print(f"\n{len(failures)} failing:")
        for f in failures:
            print("  " + "  ".join(f))
    return passed


def find_jobs(paths):
    jobs = []
    for p in paths:
        names = sorted(os.listdir(p))
        tasks = [n for n in names if in_lane(n)]
        if any(os.path.isfile(os.path.join(p, n, "task.toml")) for n in tasks):
            continue
        if tasks:
            jobs.append(p)
        else:
            jobs += find_jobs([os.path.join(p, n) for n in names
                               if not n.startswith(".") and os.path.isdir(os.path.join(p, n))])
    return jobs


def main(argv):
    data = os.path.join(REPO, "data")
    if "--data" in argv:
        i = argv.index("--data")
        if i + 1 == len(argv):
            sys.exit(__doc__)
        data = argv[i + 1]
        del argv[i:i + 2]
    is_gate = "--gate" in argv
    jobs = [a for a in argv if a != "--gate"]
    if not jobs or any(a.startswith("-") or not os.path.isdir(a) for a in jobs):
        sys.exit(__doc__)
    jobs = [j for a in jobs for j in find_jobs([a]) or [a]]  # a dir with no jobs below is a job without trials yet
    if is_gate:
        sys.exit(0 if gate(jobs, data) else 1)
    summary(jobs)


if __name__ == "__main__":
    main(sys.argv[1:])
