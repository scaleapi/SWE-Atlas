#!/usr/bin/env python3
"""Harbor verifier for test-writing tasks.

Runs inside the container after the agent finishes. Evaluates test quality via:
1. Rubrics evaluation (LLM grading of test code)
2. Manifest correctness (LLM check that manifest matches patch)
3. Mutation testing (baseline tests pass, pass again after a behaviour-preserving
   edit, then fail after skeleton swap)

Per-task files expected in /tests/:
  run_script.sh, parse_script.py, compare_test_runs.py,
  skeleton_code_swap.patch, rubrics.json, prompt.txt,
  rubrics_system_prompt.txt, rubrics_user_prompt_template.txt,
  manifest_system_prompt.txt, manifest_user_prompt_template.txt,
  judge_client.py, verifier_report.py

Judge env vars: JUDGE_API_KEY, JUDGE_BASE_URL, JUDGE_MODEL, plus the optional
JUDGE_API_DIALECT, JUDGE_REASONING_EFFORT and JUDGE_VOTES. judge_client.py falls
back to JUDGE_FALLBACK_API_KEY, OPENAI_API_KEY, OPENAI_API_BASE and its default model.
"""

import difflib
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time

TESTS_DIR = "/tests"
WORK_DIR = "/app"
# Grader-internal python steps run on this interpreter with the same flags as
# test.sh: isolated, no site processing, no bytecode.
PYI = shlex.quote(sys.executable) + " -I -S -B"
# Intermediate runner output lives in a directory only this process knows, not at
# fixed /tmp paths the agent could have pre-created.
TMP_DIR = tempfile.mkdtemp(prefix="tw-verifier-")

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import verifier_report as vr  # noqa: E402

TRACK = "tw"


MAX_RETRIES = 8
# judge_client.py (next to this file) speaks the judge endpoint's dialect.
import judge_client  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd, cwd=WORK_DIR, timeout=300, env=None):
    """Run a shell command and return (stdout, stderr, returncode).

    Everything run here executes agent-authored code (tests, builds) or runs
    inside the agent's repository, so the judge credentials are removed from the
    environment first. The command gets its own process group and writes to
    files rather than pipes, so a server a test leaves running cannot hold the
    run open; the whole group is killed when the command exits or times out.
    """
    fd_out, out_path = tempfile.mkstemp(dir=TMP_DIR, suffix=".stdout")
    fd_err, err_path = tempfile.mkstemp(dir=TMP_DIR, suffix=".stderr")
    timed_out = False
    try:
        with os.fdopen(fd_out, "wb") as out, os.fdopen(fd_err, "wb") as err:
            p = subprocess.Popen(cmd, shell=True, cwd=cwd, env=vr.scrub_env(env),
                                 stdout=out, stderr=err, start_new_session=True)
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                rc = 124
            finally:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
                p.wait()
        with open(out_path, encoding="utf-8", errors="replace") as f:
            stdout = f.read()
        with open(err_path, encoding="utf-8", errors="replace") as f:
            stderr = f.read()
    finally:
        for path in (out_path, err_path):
            try:
                os.unlink(path)
            except OSError:
                pass
    if timed_out:
        stderr += f"\n[timeout after {timeout}s]"
    return stdout, stderr, rc


def run_git(args, cwd=WORK_DIR, timeout=300):
    """git with agent-controlled configuration (hooks, filters, fsmonitor, ~/.gitconfig) disabled."""
    return run(vr.git_shell(args), cwd=cwd, timeout=timeout, env=vr.git_env())


def _clip(text, limit=8000):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[{len(text) - limit} chars truncated]"


RUNNER_TARGET_SUFFIXES = (
    ".py", ".go", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".rs", ".java",
    ".kt", ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".uts",
)


def _is_runner_target(path):
    """Whether a runner can collect tests from this path."""
    return (path or "").lower().endswith(RUNNER_TARGET_SUFFIXES)


def _runner_targets(test_files, relevant_test_names):
    """Prefer pytest node ids from the manifest so we don't run the whole file."""
    node_ids = [
        name for name in (relevant_test_names or [])
        if "::" in name or name.endswith(".py")
    ]
    if node_ids:
        return node_ids
    # is_test_file() is deliberately wide: anything under tests/ counts, which
    # is what decides whether the agent wrote tests at all. A runner needs the
    # narrower set. Handing pytest a fixture the prompt asked for - a sample
    # .eml, say - collects nothing and the gate scores an empty run. Files of
    # an extension this list does not know still go through unfiltered.
    collectable = [f for f in (test_files or []) if _is_runner_target(f)]
    return collectable or list(test_files)


def read_file(path):
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


class GraderFileChanged(Exception):
    pass


def file_digests(paths):
    digests = {}
    for path in paths:
        try:
            with open(path, "rb") as f:
                digests[path] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            digests[path] = None
    return digests


def check_unchanged(digests):
    for path, digest in file_digests(digests).items():
        if digest != digests[path]:
            raise GraderFileChanged(f"{path} changed during grading")


def is_test_file(filepath):
    """Check if a file path looks like a test file."""
    name = filepath.split("/")[-1].lower()
    path = filepath.lower().lstrip("./")
    rooted_path = f"/{path}"
    if path.startswith("test/") or path.startswith("tests/"):
        return True
    if name.endswith("_test.go") or name.startswith("test_") or name.endswith("_test.py"):
        return True
    if ".test." in name or ".spec." in name:
        return True
    if "/tests/" in rooted_path or "/test/" in rooted_path or "_tests/" in rooted_path:
        return True
    if name.endswith(".uts"):
        return True
    return False


def get_test_files_from_diff(diff):
    """Extract test file paths from a git diff."""
    matches = re.findall(r'^\+\+\+ b/(.+)$', diff, re.MULTILINE)
    return [f for f in set(matches) if is_test_file(f)]


def _normalize_test_identifier(name):
    """Normalize test identifiers for exact-form matching only."""
    value = (name or "").strip()
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
        value = value[1:-1].strip()
    value = value.replace(" > ", " | ")
    # describe('resemblesUrl()') is a legal Jest suite name and the instruction
    # tells agents to write "describe block > test name", so a pair of empty
    # parentheses must not decide a match either way.
    value = re.sub(r"\(\s*\)", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.lower()


def _strip_pytest_param_suffix(name):
    value = name or ""
    while value.endswith("]") and "[" in value:
        value = value[: value.rfind("[")]
    return value


def _candidate_test_name_forms(full_name):
    """Generate exact candidate forms from runner output."""
    forms = {full_name}
    if " | " in full_name:
        parts = [p.strip() for p in full_name.split(" | ") if p.strip()]
        if parts:
            forms.add(parts[-1])  # test title
        if len(parts) >= 2:
            forms.add(" | ".join(parts[-2:]))  # suite | test
    if "::" in full_name:
        parts = [p.strip() for p in full_name.split("::") if p.strip()]
        if parts:
            forms.add(parts[-1])  # method
        if len(parts) >= 2:
            forms.add("::".join(parts[-2:]))  # Class::method
            forms.add(".".join(parts[-2:]))   # Class.method
    if "/" in full_name:
        # Go subtests: the runner prints TestParent/subtest, while a manifest
        # may name the subtest or the helper behind it on its own. Only the
        # runner side sheds the parent; a manifest name that carries one stays
        # qualified, so two parents sharing a subtest name do not collide.
        parts = [p.strip() for p in full_name.split("/") if p.strip()]
        if parts:
            forms.add(parts[-1])
        if len(parts) >= 2:
            forms.add("/".join(parts[-2:]))
    if " - " in full_name:
        # Some runners append descriptions: test_name - description
        forms.add(full_name.split(" - ", 1)[0].strip())
    expanded = set(forms)
    for form in forms:
        stripped = _strip_pytest_param_suffix(form)
        if stripped and stripped != form:
            expanded.add(stripped)
    return expanded


def _candidate_relevant_name_forms(relevant_name):
    """Generate exact candidate forms from manifest identifiers."""
    forms = {relevant_name}
    if "/" in relevant_name:
        forms.add("/".join(part.replace(" ", "_") for part in relevant_name.split("/")))
    if " > " in relevant_name:
        parts = [p.strip() for p in relevant_name.split(" > ") if p.strip()]
        if len(parts) >= 2:
            forms.add(" | ".join(parts[-2:]))
            forms.add(" > ".join(parts[-2:]))
    if " | " in relevant_name:
        parts = [p.strip() for p in relevant_name.split(" | ") if p.strip()]
        if len(parts) >= 2:
            forms.add(" | ".join(parts[-2:]))
    if "::" in relevant_name:
        parts = [p.strip() for p in relevant_name.split("::") if p.strip()]
        if parts:
            forms.add(parts[-1])
        if len(parts) >= 2:
            forms.add("::".join(parts[-2:]))
            forms.add(".".join(parts[-2:]))
    if "." in relevant_name:
        parts = [p.strip() for p in relevant_name.split(".") if p.strip()]
        if parts:
            forms.add(parts[-1])
        if len(parts) >= 2:
            forms.add(".".join(parts[-2:]))
    return forms


def matches_relevant_test_name(full_name, relevant_names):
    """Match runner test names against relevant identifiers (no fuzzy fallback)."""
    if not relevant_names:
        return False

    full_forms = {_normalize_test_identifier(x) for x in _candidate_test_name_forms(full_name)}
    for rn in relevant_names:
        rn_forms = {_normalize_test_identifier(x) for x in _candidate_relevant_name_forms(rn)}
        if full_forms & rn_forms:
            return True
    return False


def filter_tests_to_relevant(tests, relevant_names):
    """Keep only tests that match extracted relevant test names."""
    if not relevant_names:
        return []
    return [t for t in tests if matches_relevant_test_name(t.get("name", ""), relevant_names)]


def extract_relevant_test_names_from_manifest(manifest_text):
    """Extract relevant test names from manifest content."""
    if not manifest_text:
        return []

    names = []

    # Try JSON manifests first.
    try:
        parsed = json.loads(manifest_text)
        if isinstance(parsed, dict):
            tests = parsed.get("tests", [])
            if isinstance(tests, list):
                for item in tests:
                    if isinstance(item, dict):
                        name = str(item.get("name", "")).strip()
                    else:
                        name = str(item).strip()
                    if name:
                        names.append(name)
            if names:
                return sorted(set(names))
    except Exception:
        pass

    # Fallback to bullet-list parsing.
    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        item = line[1:].strip()
        if not item or item.lower().startswith("file:") or item.lower() in {"tests:", "test:"}:
            continue
        if len(item) >= 2 and ((item[0] == '"' and item[-1] == '"') or (item[0] == "'" and item[-1] == "'")):
            item = item[1:-1].strip()
        if item:
            names.append(item)

    return sorted(set(names))


def get_changed_files_from_diff(diff):
    """Every path a git diff touches, deletions and renames included."""
    files = set(re.findall(r'^\+\+\+ b/(.+)$', diff, re.MULTILINE))
    files |= set(re.findall(r'^--- a/(.+)$', diff, re.MULTILINE))
    return sorted(files)


def get_manifest():
    """Read the test manifest written by the agent to /logs/agent/manifest.txt."""
    content = read_file("/logs/agent/manifest.txt")
    if not content:
        return ""
    # Strip <<TEST_MANIFEST>> tags if present
    if "<<TEST_MANIFEST>>" in content:
        parts = content.split("<<TEST_MANIFEST>>")
        if len(parts) >= 3:
            return parts[-2].strip()
        elif len(parts) >= 2:
            return parts[1].strip()
    return content.strip()


def _normalize_test_status(status):
    """Normalize parser-emitted test statuses for robust policy checks."""
    if status is None:
        return ""
    return str(status).strip().upper()


# ---------------------------------------------------------------------------
# LLM evaluation
# ---------------------------------------------------------------------------

def _parse_llm_response(text):
    """Parse the judge's JSON reply and return the object.

    Strict json.loads rejects two things judges do routinely: literal control
    characters inside strings (tabs copied out of diff lines) and wrapping the
    object in a ```json fence whose body holds further fences, which truncates
    the block at the inner fence. Scanning candidate objects with raw_decode
    also makes trailing prose after the object harmless.
    """
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder(strict=False)
    start = text.find("```json") + 7 if "```json" in text else 0
    fallback = None
    idx = text.find("{", start)
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            if any(key in obj for key in ("ratings", "answer")):
                return obj
            if fallback is None:
                fallback = obj
        idx = text.find("{", idx + 1)
    return fallback


def llm_call(client, model, system_prompt, user_content, want_json=True):
    """Make an LLM call with retries."""
    for attempt in range(MAX_RETRIES):
        if judge_client.past_deadline():
            # Leaving the loop reports the rubric unrated, not failed.
            print(
                f"  Judging exceeded its {judge_client.PASS_DEADLINE_SEC}s budget "
                f"({judge_client.pass_elapsed():.0f}s elapsed); leaving the rest unrated",
                file=sys.stderr,
            )
            break
        try:
            reply = client.complete(system_prompt, user_content, want_json=want_json)
            text = reply.text
            if text.strip():
                return text
            # A call can succeed and still carry no content: a reasoning judge
            # that spends its whole max_tokens budget on thinking comes back
            # with finish_reason "length" and an empty message. Downstream that
            # is indistinguishable from a parse failure, so the rubric used to
            # be dropped silently. Retry, and say why in the log.
            wait = min(2 ** (attempt + 1), 60)
            print(f"  LLM retry {attempt+1}/{MAX_RETRIES}: empty content "
                  f"(finish_reason={reply.finish_reason}), waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
        except judge_client.JudgeRefused:
            raise
        except Exception as e:
            wait = min(2 ** (attempt + 1), 60)
            print(f"  LLM retry {attempt+1}/{MAX_RETRIES}: {e}, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
    return ""


# An echoed rubric_statement this dissimilar to the one sent means the judge graded
# a criterion of its own (the rule and threshold qa uses).
STATEMENT_MATCH_THRESHOLD = 0.9


def _normalized(text):
    # Case, whitespace, punctuation and the "1.2:" numbering are presentation.
    text = re.sub(r"^\s*\d+(\.\d+)*:\s*", "", text.lower())
    return " ".join(re.sub(r"[^a-z0-9%./_\- ]+", " ", text).split())


def _negations(text):
    # "n't" is "n t" once normalized.
    return len(re.findall(r"\b(?:not|no|never|without|cannot)\b|n t\b", text))


def _statement_matches(echoed, title):
    """Whether a rating's echoed rubric_statement is the criterion that was sent.
    Nothing echoed is nothing to compare; an echo that differs in negation never
    matches; a truncated echo of at least 60% of the statement still matches, as
    does trivial rewording."""
    if not isinstance(echoed, str) or not echoed.strip():
        return True
    a, b = _normalized(echoed), _normalized(title)
    if _negations(a) != _negations(b):
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if short and short in long_ and len(short) >= 0.6 * len(long_):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= STATEMENT_MATCH_THRESHOLD


def _read_rating(parsed, title):
    """Pull (score, score_value, score_status, evaluable) out of a parsed reply."""
    score = None
    score_value = None
    score_status = None
    if parsed and "ratings" in parsed:
        ratings = parsed["ratings"]
        # A rating for a different criterion than the one sent is no verdict.
        if (isinstance(ratings, list) and ratings and isinstance(ratings[0], dict)
                and _statement_matches(ratings[0].get("rubric_statement"), title)):
            score = {
                "score": ratings[0].get("score"),
                "status": ratings[0].get("status"),
                "justification": ratings[0].get("justification"),
            }
            if score.get("score") is not None:
                score_value = str(score["score"]).strip()
            if score.get("status") is not None:
                score_status = str(score["status"]).strip().upper()
    # The verdict is the status; a score that contradicts it makes the reply unusable.
    evaluable = score_status in {"YES", "NO"} and (
        score_value is None or score_value == ("1" if score_status == "YES" else "0"))
    return score, score_value, score_status, evaluable


def cast_vote(client, model, system_prompt, user_content, rubric_id, title):
    """One judge verdict for a rubric: (status, score), or None if none came back."""
    for attempt in range(MAX_RETRIES):
        # JSON mode first; through some proxies a long prompt comes back as an envelope
        # with no verdict, so from the third attempt ask for a plain reply (as qa does).
        text = llm_call(client, model, system_prompt, user_content, want_json=attempt < 2)
        if not text.strip():
            # llm_call already retried errors and empty replies; nothing
            # more is coming back for this rubric.
            return None
        score, _, score_status, evaluable = _read_rating(_parse_llm_response(text), title)
        if evaluable:
            return score_status, score
        # A reply that parses but carries no usable verdict is asked again
        # before the rubric is given up.
        print(f"  Rubric {rubric_id}: retry {attempt+1}/{MAX_RETRIES}, "
              "reply carried no usable verdict", file=sys.stderr)
    return None


def evaluate_rubrics(client, model, problem_statement, model_patch, rubrics):
    """Evaluate each rubric against the agent's test code."""
    system_prompt = read_file(os.path.join(TESTS_DIR, "rubrics_system_prompt.txt"))
    user_template = read_file(os.path.join(TESTS_DIR, "rubrics_user_prompt_template.txt"))

    results = []
    for rubric in rubrics:
        annotations = rubric.get("annotations", {})
        rubric_type = annotations.get("type", "")
        importance = annotations.get("importance", rubric.get("importance", "must have"))

        # Skip high-level intent headers (not actual rubrics)
        if rubric_type == "high level intent":
            continue

        is_negative = "negative" in rubric_type

        user_content = user_template.format(
            problem_statement=problem_statement,
            model_answer=model_patch,
            title=json.dumps(rubric["title"]),
        )
        score_status, score, votes = judge_client.majority(
            lambda: cast_vote(client, model, system_prompt, user_content, rubric["id"],
                              rubric["title"]),
            client.config.votes)
        rubric_evaluable = score_status is not None

        # Positive rubric: behavior present (status "YES") = pass.
        # Negative rubric: behavior absent (status "NO") = pass.
        kind = "negative" if is_negative else "positive"
        if rubric_evaluable:
            rubric_passes = (score_status == "NO") if is_negative else (score_status == "YES")
            entry = vr.rubric_entry(rubric["id"], rubric["title"], importance, kind,
                                    reward=1 if rubric_passes else 0,
                                    justification=score.get("justification"))
        else:
            # The judge never rated this rubric. It is recorded with an error
            # and, if it is a must-have, fails the task below.
            entry = vr.rubric_entry(rubric["id"], rubric["title"], importance, kind,
                                    error=vr.UNRATED_ERROR)
        results.append(entry)
        vr.print_rubric(entry, judge_client.vote_note(votes))

    # Every declared must-have has to be rated and pass; one whose reply never
    # parsed counts as failed. A total judge failure is flagged separately so
    # it can be told apart from a real failure of the submission.
    block = vr.rubrics_block(results, judge_attempted=client.calls > 0)
    if block["judge_unusable"]:
        print("ERROR: judge returned no usable rating for any of the "
              f"{block['num_must_have']} must-have rubrics; this verdict "
              "reflects a judge failure, not the submission", file=sys.stderr)
    elif not client.calls:
        print("ERROR: the judging budget ran out before a single rubric was "
              f"sent; all {block['num_must_have']} must-have rubrics went "
              "unrated and the gate fails on them", file=sys.stderr)
    elif block["num_unrated_must_have"]:
        print(f"ERROR: judge returned no usable rating for {block['num_unrated_must_have']} of "
              f"{block['num_must_have']} must-have rubrics; the task fails on them",
              file=sys.stderr)
    return block


def evaluate_manifest(client, model, model_patch, manifest):
    """Check if the manifest matches the actual tests in the patch."""
    system_prompt = read_file(os.path.join(TESTS_DIR, "manifest_system_prompt.txt"))
    user_template = read_file(os.path.join(TESTS_DIR, "manifest_user_prompt_template.txt"))

    user_content = user_template.format(model_edits=model_patch, model_answer=manifest)

    def cast_manifest_vote():
        parsed = _parse_llm_response(llm_call(client, model, system_prompt, user_content))
        if parsed and "answer" in parsed:
            return ("YES" if parsed["answer"] == "YES" else "NO"), parsed.get("justification", "")
        return None

    answer, justification, votes = judge_client.majority(cast_manifest_vote, client.config.votes)
    if answer is None:
        return {"pass": False, "justification": "Failed to parse LLM response", "votes": votes}
    return {"pass": answer == "YES", "justification": justification, "votes": votes}


# ---------------------------------------------------------------------------
# Equivalent variant: the tests must also survive a change that keeps behaviour
# ---------------------------------------------------------------------------
#
# The mutation gate asks "do the tests fail when the file changes?", which a test that
# hashes the source file answers without running the code. So the graded tests also run
# against the real code with one comment inserted inside the skeleton's first hunk in
# every touched file, with random text on every run. Behaviour is unchanged, so every
# relevant test that passed at baseline must pass again; a test that fails, errors or
# disappears there reacts to the bytes of the file, not to what it does. This closes
# byte-exact detectors (whole-file hashes, sizes, and function-source hashes when the
# comment lands inside the function). Added lines that visibly read the file under test
# or introspect its code fail the gate as well (detector_patterns()); the judge covers
# the rest.

COMMENT_SYNTAX = {
    ".py": ("# ", ""), ".sh": ("# ", ""), ".rb": ("# ", ""), ".pl": ("# ", ""),
    ".go": ("// ", ""), ".ts": ("// ", ""), ".tsx": ("// ", ""), ".js": ("// ", ""),
    ".jsx": ("// ", ""), ".mjs": ("// ", ""), ".java": ("// ", ""), ".rs": ("// ", ""),
    ".kt": ("// ", ""), ".cs": ("// ", ""), ".scala": ("// ", ""), ".swift": ("// ", ""),
    ".php": ("// ", ""),
    ".c": ("/* ", " */"), ".h": ("/* ", " */"), ".cc": ("/* ", " */"), ".cpp": ("/* ", " */"),
    ".hpp": ("/* ", " */"), ".m": ("/* ", " */"),
}

DETECTOR_PATTERNS = [
    ("source introspection", r"inspect\.get(source|file)|__code__|co_code|\bdis\.(dis|Bytecode|get_instructions)\b|\blinecache\b"),
    ("skeleton marker", r"SKELETON|skeleton\.invalid"),
    ("hash library", r"\bhashlib\b|\bsha(1|224|256|384|512)\b|\bmd5\b|crypto/sha|crypto/md5|createHash\("),
    ("file metadata", r"os\.stat\(|os\.path\.getsize|getmtime\(|\bstatSync\(|os\.Stat\("),
]
SCORED_DETECTORS = ("reads the file under test", "source introspection", "skeleton marker")


def _skeleton_hunks(patch_text):
    """{path: [{"start", "removed": [original line numbers]}, ...]} from a unified diff."""
    files = {}
    path = None
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            path = None
            continue
        if not in_hunk and line.startswith("+++ "):
            p = line[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            path = None if p == "/dev/null" else p
            if path is not None:
                files.setdefault(path, [])
            continue
        if line.startswith("@@ ") and path is not None:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            in_hunk = True
            files[path].append({"start": int(m.group(1)), "removed": [], "cursor": int(m.group(1))})
            continue
        if in_hunk and path is not None and files.get(path):
            h = files[path][-1]
            if line.startswith("\\"):
                continue
            if line.startswith("-"):
                h["removed"].append(h["cursor"])
                h["cursor"] += 1
            elif line.startswith("+"):
                pass
            else:
                h["cursor"] += 1
    return files


def _unsafe_insertion(lines_before, path):
    """True when a new line here could land inside a string, a block comment or a continuation."""
    ext = os.path.splitext(path)[1].lower()
    text = "".join(lines_before)
    if ext == ".py" and (text.count('"""') % 2 or text.count("'''") % 2):
        return True
    if ext in (".go", ".ts", ".tsx", ".js", ".jsx", ".mjs") and text.count("`") % 2:
        return True
    if ext in (".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".go", ".ts", ".tsx", ".js", ".jsx",
               ".mjs", ".java", ".rs", ".kt", ".cs", ".scala", ".swift", ".php"):
        if text.count("/*") > text.count("*/"):
            return True
    if lines_before and lines_before[-1].rstrip("\r\n").endswith("\\"):
        return True
    return False


def _comment_line(path, indent, token):
    ext = os.path.splitext(path)[1].lower()
    if ext not in COMMENT_SYNTAX:
        return None
    open_, close = COMMENT_SYNTAX[ext]
    return f"{indent}{open_}verifier variant {token}{close}\n"


def apply_equivalent_variant(skeleton_patch_text, token):
    """Insert one random comment inside the first skeleton hunk of every touched file.

    Returns (changes, originals): changes is one record per file (mode in-hunk, eof,
    missing, binary or unsupported), originals maps path to the bytes to restore.
    """
    changes, originals = [], {}
    for rel, hunks in _skeleton_hunks(skeleton_patch_text).items():
        path = os.path.join(WORK_DIR, rel)
        if not os.path.isfile(path):
            changes.append({"file": rel, "mode": "missing"})
            continue
        with open(path, "rb") as f:
            data = f.read()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            changes.append({"file": rel, "mode": "binary"})
            continue
        lines = text.splitlines(keepends=True)
        candidates = [ln for h in hunks for ln in (h["removed"] or [h["start"]])]
        target, mode = None, None
        for ln in candidates:
            idx = ln - 1
            if 0 <= idx <= len(lines) and not _unsafe_insertion(lines[:idx], rel):
                target, mode = idx, "in-hunk"
                break
        comment = None
        if target is not None:
            indent = re.match(r"[ \t]*", lines[target] if target < len(lines) else "").group(0)
            comment = _comment_line(rel, indent, token)
        if comment is None:
            comment = _comment_line(rel, "", token)
            if comment is None:
                changes.append({"file": rel, "mode": "unsupported"})
                continue
            target, mode = len(lines), "eof"
            if lines and not lines[-1].endswith("\n"):
                comment = "\n" + comment
        new_text = "".join(lines[:target]) + comment + "".join(lines[target:])
        originals[path] = data
        with open(path, "wb") as f:
            f.write(new_text.encode("utf-8"))
        changes.append({"file": rel, "mode": mode, "line": target + 1})
    return changes, originals


def restore_files(originals):
    for path, data in originals.items():
        with open(path, "wb") as f:
            f.write(data)


def detector_patterns(model_patch, source_files):
    """Added lines in the agent's patch that look at the implementation's bytes, not its behaviour."""
    hits = []
    names = [os.path.basename(p) for p in source_files if p]
    current = None
    for line in model_patch.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            current = p[2:] if p.startswith("b/") else p
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        kind = None
        if any(re.search(r"(?<![\w-])" + re.escape(n) + r"(?!\w)", body) for n in names) and re.search(
                r"\b(open|Open|ReadFile|readFile|readFileSync|fopen|read_text|read_bytes|Stat|stat|getsize)\s*\(", body):
            kind = "reads the file under test"
        elif re.search(r"\bopen\s*\(\s*[\w.]+\.__file__\s*[,)]|\(\s*[\w.]+\.__file__\s*\)\.read_(text|bytes)\s*\(", body):
            kind = "reads the file under test"
        for label, rx in DETECTOR_PATTERNS:
            if kind is None and re.search(rx, body):
                kind = label
        if kind:
            hits.append({"file": current, "kind": kind, "line": body.strip()[:160]})
    return hits


# ---------------------------------------------------------------------------
# Mutation testing
# ---------------------------------------------------------------------------

def run_mutation_testing(test_files, model_patch, manifest):
    """Run baseline and mutation phases, return comparison results."""
    parse_script = os.path.join(TESTS_DIR, "parse_script.py")
    compare_script = os.path.join(TESTS_DIR, "compare_test_runs.py")
    skeleton_patch = os.path.join(TESTS_DIR, "skeleton_code_swap.patch")
    relevant_test_names = extract_relevant_test_names_from_manifest(manifest)
    # Runner output stays with the trial: the only record of why a suite ran empty.
    art = vr.artifacts_dir()
    targets = _runner_targets(test_files, relevant_test_names)
    test_files_arg = " ".join(shlex.quote(t) for t in targets)

    # The runner comes from /tests, uploaded after the agent finished. The image's
    # /run_script.sh is a file the agent could have rewritten, so it is not used.
    run_script = os.path.join(TESTS_DIR, "run_script.sh")
    if not os.path.exists(run_script):
        raise RuntimeError("tests/run_script.sh is missing; refusing to grade with the image's run script")

    os.chmod(run_script, 0o755)
    shipped = file_digests([run_script, parse_script, compare_script, skeleton_patch])
    print(f"  [runner] {run_script}")
    print(f"  [targets] {targets}")

    # --- Baseline ---
    # Stale bytecode from a reverted source edit must not reach the baseline.
    run(f"find {WORK_DIR} \\( -name node_modules -o -name .git \\) -prune -o -name __pycache__ -type d -prune -exec rm -rf {{}} +", timeout=120)
    print("  [baseline] Running tests...")
    stdout, stderr, rc = run(f"{run_script} {test_files_arg}", timeout=600)
    with open(f"{art}/baseline_stdout.log", "w") as f:
        f.write(stdout)
    with open(f"{art}/baseline_stderr.log", "w") as f:
        f.write(stderr)
    baseline_rc = rc
    print(f"  [baseline] runner rc={rc} stdout_chars={len(stdout)} stderr_chars={len(stderr)}")

    print("  [baseline] Parsing results...")
    check_unchanged(shipped)
    run(f"{PYI} {parse_script} {art}/baseline_stdout.log {art}/baseline_stderr.log {TMP_DIR}/baseline_output.json")
    baseline = json.loads(read_file(f"{TMP_DIR}/baseline_output.json") or '{"tests": []}')

    # --- Equivalent variant ---
    token = secrets.token_hex(8)
    check_unchanged(shipped)
    variant_changes, originals = apply_equivalent_variant(read_file(skeleton_patch), token)
    variant_files = [c for c in variant_changes if c.get("mode") in ("in-hunk", "eof")]
    print(f"  [equivalent] Behaviour-preserving variant applied to {len(variant_files)} file(s): "
          + ", ".join(f"{c['file']} ({c['mode']}" + (f" line {c['line']}" if c.get("line") else "") + ")" for c in variant_changes))
    equivalent = {"tests": []}
    equivalent_rc = None
    if variant_files:
        run(f"find {WORK_DIR} \\( -name node_modules -o -name .git \\) -prune -o -name __pycache__ -type d -prune -exec rm -rf {{}} +", timeout=120)
        print("  [equivalent] Running tests...")
        check_unchanged(shipped)
        stdout, stderr, rc = run(f"{run_script} {test_files_arg}", timeout=600)
        with open(f"{art}/equivalent_stdout.log", "w") as f:
            f.write(stdout)
        with open(f"{art}/equivalent_stderr.log", "w") as f:
            f.write(stderr)
        equivalent_rc = rc
        print(f"  [equivalent] runner rc={rc} stdout_chars={len(stdout)} stderr_chars={len(stderr)}")
        check_unchanged(shipped)
        run(f"{PYI} {parse_script} {art}/equivalent_stdout.log {art}/equivalent_stderr.log {TMP_DIR}/equivalent_output.json")
        equivalent = json.loads(read_file(f"{TMP_DIR}/equivalent_output.json") or '{"tests": []}')
        restore_files(originals)
        run(f"find {WORK_DIR} \\( -name node_modules -o -name .git \\) -prune -o -name __pycache__ -type d -prune -exec rm -rf {{}} +", timeout=120)
    else:
        print("  [equivalent] WARNING: no file touched by the skeleton could take the variant; "
              "this gate is not applied", file=sys.stderr)

    # --- Mutation ---
    print("  [mutation] Applying skeleton swap...")
    check_unchanged(shipped)
    stdout, stderr, rc = run_git(["apply", "-v", skeleton_patch])
    if rc != 0:
        print(f"  [mutation] WARNING: skeleton patch failed to apply; the implementation "
              f"under test differs from the shipped image: {stderr}", file=sys.stderr)
        baseline_tests = baseline.get("tests", [])
        return vr.tests_block(
            False, "mutation", vr.run_counts(baseline_tests),
            {"passed": 0, "failed": 0, "skipped": 0},
            vr.transitions(baseline_tests, []), [], vr.empty_transitions(), [],
            error=f"skeleton patch failed to apply (implementation under test "
                  f"differs from the shipped image): {stderr}",
            details={"runner": run_script, "manifest_test_names": relevant_test_names,
                     "baseline_tests": baseline_tests},
        )

    print("  [mutation] Running tests...")
    check_unchanged(shipped)
    stdout, stderr, rc = run(f"{run_script} {test_files_arg}", timeout=600)
    with open(f"{art}/mutation_stdout.log", "w") as f:
        f.write(stdout)
    with open(f"{art}/mutation_stderr.log", "w") as f:
        f.write(stderr)
    mutation_rc = rc
    print(f"  [mutation] runner rc={rc} stdout_chars={len(stdout)} stderr_chars={len(stderr)}")

    print("  [mutation] Parsing results...")
    check_unchanged(shipped)
    run(f"{PYI} {parse_script} {art}/mutation_stdout.log {art}/mutation_stderr.log {TMP_DIR}/mutation_output.json")
    mutation = json.loads(read_file(f"{TMP_DIR}/mutation_output.json") or '{"tests": []}')

    # --- Comparison ---
    print("  [comparison] Comparing baseline vs mutation...")
    check_unchanged(shipped)
    run(f"{PYI} {compare_script} {TMP_DIR}/baseline_output.json {TMP_DIR}/mutation_output.json {TMP_DIR}/comparison_output.json")
    comparison = json.loads(read_file(f"{TMP_DIR}/comparison_output.json") or '{}')

    # Determine pass using relevant tests only (instead of every test in touched files).
    baseline_tests = baseline.get("tests", [])
    mutation_tests = mutation.get("tests", [])
    mutation_by_name = {
        t.get("name"): _normalize_test_status(t.get("status"))
        for t in mutation_tests if t.get("name")
    }

    baseline_relevant = filter_tests_to_relevant(baseline_tests, relevant_test_names)
    mutation_relevant = filter_tests_to_relevant(mutation_tests, relevant_test_names)

    baseline_relevant_passed = {
        t["name"]
        for t in baseline_relevant
        if t.get("name") and _normalize_test_status(t.get("status")) == "PASSED"
    }
    # A mutation is considered "killed" when the relevant test does not pass.
    killed_statuses = {"FAILED", "ERROR"}
    mutation_not_failed = sorted(
        name for name in baseline_relevant_passed
        if name in mutation_by_name
        and mutation_by_name[name] not in killed_statuses
    )
    # Diagnostic: a relevant test that passed at baseline and is absent from
    # the mutation run rather than failed in it.
    relevant_missing = sorted(name for name in baseline_relevant_passed
                              if name not in mutation_by_name)

    all_pass_to_fail = len(baseline_relevant_passed) > 0 and len(mutation_not_failed) == 0

    # Every relevant test that passed at baseline must be present and pass on the
    # behaviour-preserving variant; missing, failed and errored all count against it.
    equivalent_by_name = {
        t.get("name"): _normalize_test_status(t.get("status"))
        for t in equivalent.get("tests", []) if t.get("name")
    }
    equivalent_not_passed = sorted(
        name for name in baseline_relevant_passed
        if equivalent_by_name.get(name) != "PASSED"
    ) if variant_files else []
    if variant_files:
        print(f"  [equivalent] {len(baseline_relevant_passed) - len(equivalent_not_passed)}/{len(baseline_relevant_passed)} "
              f"relevant tests still pass on the behaviour-preserving variant"
              + (f"; NOT passing (react to the file's bytes, not its behaviour): {equivalent_not_passed[:10]}"
                 if equivalent_not_passed else ""))
    gate_pass = all_pass_to_fail and not equivalent_not_passed

    # The relevant tests as the runner names them; the manifest's own spelling
    # stays in details.manifest_test_names.
    relevant_names = sorted({
        t["name"] for t in baseline_relevant + mutation_relevant if t.get("name")
    })
    return vr.tests_block(
        gate_pass, "mutation",
        vr.run_counts(baseline_tests), vr.run_counts(mutation_tests),
        vr.transitions(baseline_tests, mutation_tests),
        relevant_names,
        vr.transitions(baseline_tests, mutation_tests, names=relevant_names),
        mutation_not_failed,
        details={
            "runner": run_script,
            "manifest_test_names": relevant_test_names,
            "baseline_tests": baseline_tests,
            "mutation_tests": mutation_tests,
            "comparison": comparison,
            "baseline_passed_count": len([t for t in baseline_tests if t.get("status") == "PASSED"]),
            "pass_to_fail_count": len(comparison.get("pass_to_fail", [])),
            "baseline_relevant_total_count": len(baseline_relevant),
            "baseline_relevant_passed_count": len(baseline_relevant_passed),
            "mutation_relevant_total_count": len(mutation_relevant),
            "runner_targets": targets,
            "relevant_missing_under_mutation": relevant_missing,
            "equivalent_variant": {
                "applied": bool(variant_files),
                "changes": variant_changes,
                "not_passed": equivalent_not_passed,
                "rc": equivalent_rc,
                "tests": equivalent.get("tests", []),
                "stdout_tail": _clip(read_file(f"{art}/equivalent_stdout.log")) if variant_files else "",
                "stderr_tail": _clip(read_file(f"{art}/equivalent_stderr.log")) if variant_files else "",
            },
            "baseline_rc": baseline_rc,
            "mutation_rc": mutation_rc,
            "baseline_stdout_tail": _clip(read_file(f"{art}/baseline_stdout.log")),
            "baseline_stderr_tail": _clip(read_file(f"{art}/baseline_stderr.log")),
            "mutation_stdout_tail": _clip(read_file(f"{art}/mutation_stdout.log")),
            "mutation_stderr_tail": _clip(read_file(f"{art}/mutation_stderr.log")),
        },
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _skip_llm_sections(reason):
    vr.print_section("RUBRICS")
    vr.print_skipped(reason)
    vr.print_section("MANIFEST")
    vr.print_skipped(reason)


def main():
    judge = judge_client.JudgeConfig.from_env()
    api_key, base_url, model = judge.api_key, judge.base_url, judge.model
    print(f"Judge config: {judge.describe()}", file=sys.stderr)
    vr.print_header(TRACK)
    if judge.error:
        vr.print_section("TESTS")
        vr.print_skipped("invalid judge configuration")
        _skip_llm_sections("invalid judge configuration")
        vr.fail_early(TRACK, judge.error)
        return

    # The repository is the agent's: drop hooks, filters and other config that could
    # run code during the git calls below, then stage everything to capture new files.
    git_record = vr.harden_git(WORK_DIR)
    print(f"  Git configuration in {WORK_DIR}: {len(git_record['dropped_keys'])} key(s) dropped, "
          f"{len(git_record['hooks_removed'])} hook(s) removed"
          + (f"; {git_record['error']}" if git_record["error"] else ""), file=sys.stderr)
    run_git(["add", "-A"])
    model_patch, _, _ = run_git(["diff", "--cached", "--no-ext-diff", "--no-textconv", "HEAD"])
    if not model_patch.strip():
        # Try diff against init commit
        root_commit, _, _ = run_git(["rev-list", "--max-parents=0", "HEAD"])
        root_commit = root_commit.strip().splitlines()[0] if root_commit.strip() else "HEAD"
        model_patch, _, _ = run_git(["diff", "--cached", "--no-ext-diff", "--no-textconv", root_commit])

    # Keep the graded patch with the trial. /logs/artifacts is collected before
    # the verifier runs, so a file written there never leaves the sandbox.
    if model_patch.strip():
        with open(os.path.join(vr.artifacts_dir(), "agent.patch"), "w") as f:
            f.write(model_patch)

    vr.print_section("TESTS")

    if not model_patch.strip():
        vr.print_skipped("no changes detected")
        _skip_llm_sections("no changes detected")
        vr.fail_early(TRACK, "no changes detected", details={"model_patch_length": 0})
        return

    test_files = get_test_files_from_diff(model_patch)
    manifest = get_manifest()

    print(f"  Test files: {test_files}")
    print(f"  Manifest present: {bool(manifest)}")
    details = {
        "test_files": test_files,
        "model_patch_length": len(model_patch),
        "manifest_present": bool(manifest),
    }

    if not test_files:
        vr.print_skipped("no test files in diff")
        _skip_llm_sections("no test files in diff")
        vr.fail_early(TRACK, "no test files found in diff", details=details)
        return

    # The mutation phase swaps the implementation under test for a broken one.
    # A patch that edits that same file cannot be graded: the swap would not
    # apply, and the tests would run against code the task did not ship. Report
    # it as the rule violation it is rather than as a failed patch.
    skeleton_patch = read_file(os.path.join(TESTS_DIR, "skeleton_code_swap.patch"))
    source_under_test = set(get_changed_files_from_diff(skeleton_patch))
    touched = sorted(source_under_test & set(get_changed_files_from_diff(model_patch)))
    if touched:
        details["rule_violation"] = {"kind": "modified_source_under_test", "files": touched}
        vr.print_skipped("rule violation")
        _skip_llm_sections("rule violation")
        vr.fail_early(TRACK, "rule violation: modified the implementation under test: "
                      + ", ".join(touched), details=details)
        return

    # Tests that look at the implementation's bytes rather than its behaviour. Reading the
    # file under test, introspecting its code or matching the skeleton's markers fails the
    # tests gate; hashes and file metadata, which honest tests use too, are only reported.
    detectors = detector_patterns(model_patch, source_under_test)
    details["detector_patterns"] = detectors
    reads_source = [d for d in detectors if d["kind"] in SCORED_DETECTORS]
    if detectors:
        print(f"  WARNING: {len(detectors)} added test line(s) look at the implementation's bytes "
              f"rather than its behaviour: " + "; ".join(f"{d['kind']}: {d['line'][:80]}" for d in detectors[:5]))

    # --- 1. Mutation testing (no LLM needed) ---
    try:
        tests = run_mutation_testing(test_files, model_patch, manifest)
    except GraderFileChanged as e:
        print(f"  [integrity] Tests gate fails: {e}")
        empty = {"passed": 0, "failed": 0, "skipped": 0}
        tests = vr.tests_block(False, "mutation", empty, empty, vr.empty_transitions(), [],
                               vr.empty_transitions(), [], error=str(e))
    vr.stop_agent_code()
    if reads_source:
        tests["pass"] = False
        tests["details"]["reads_source_under_test"] = reads_source
        print(f"  [detectors] Tests gate fails: {len(reads_source)} added line(s) read or introspect "
              f"the implementation under test instead of running it")
    vr.print_tests_summary(tests)

    # --- 2. Rubrics evaluation and 3. manifest correctness (LLM) ---
    rubrics_block = None
    manifest_block = None
    error = None

    if api_key and base_url and model:
        client = judge_client.JudgeClient(judge)

        rubrics = json.loads(read_file(os.path.join(TESTS_DIR, "rubrics.json")) or "[]")
        problem_statement = read_file(os.path.join(TESTS_DIR, "prompt.txt"))

        vr.print_section("RUBRICS")
        if rubrics:
            rubrics_block = evaluate_rubrics(client, model, problem_statement, model_patch, rubrics)
            vr.print_rubrics_summary(rubrics_block)
        else:
            vr.print_skipped("no rubrics for this task")

        vr.print_section("MANIFEST")
        note = None
        if manifest:
            verdict = evaluate_manifest(client, model, model_patch, manifest)
            manifest_block = vr.manifest_block(verdict["pass"], verdict.get("justification", ""))
            note = judge_client.vote_note(verdict["votes"])
        else:
            print("  No manifest found")
            manifest_block = vr.manifest_block(False, "No manifest produced")
        vr.print_manifest_summary(manifest_block, note)
    else:
        error = "JUDGE_API_KEY, JUDGE_BASE_URL and JUDGE_MODEL must all be set; LLM evaluations skipped"
        print(f"ERROR: {error}", file=sys.stderr)
        _skip_llm_sections("no judge credentials or model")

    # --- Final reward ---
    rubrics_pass = bool(rubrics_block and rubrics_block["pass"])
    manifest_pass = bool(manifest_block and manifest_block["pass"])
    overall_pass = rubrics_pass and manifest_pass and tests["pass"]
    vr.finish(vr.report(TRACK, overall_pass, rubrics=rubrics_block, tests=tests,
                        manifest=manifest_block, error=error, details=details))


if __name__ == "__main__":
    try:
        main()
    except judge_client.JudgeRefused as e:
        # Not a failed answer: the judge would not look at it.
        vr.fail_early(TRACK, f"judge refused to grade this task: {e}")
        sys.exit(1)
