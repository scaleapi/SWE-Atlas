#!/usr/bin/env python3
"""LLM-grade an agent's refactoring patch against per-task rubrics.

Runs inside the container, after test.sh has produced
`/tmp_artifact_storage/agent.patch`. Reads `/tests/rubrics.json` and
`/tests/prompt.txt`, takes the judge's majority verdict per rubric item, prints the
RUBRICS section of the shared verifier stdout and hands test.sh
`/tmp_artifact_storage/rubrics_results.json`: the shared `rubrics` block from
verifier_report.py plus `empty_patch` and `empty_patch_positive_passes`. test.sh
folds it into evaluation_results.json; nothing else reads it.

On an empty agent patch the negative rubrics are left unrated, since an absent
behaviour would pass them for free, while the positive rubrics are still
judged: a positive that passes on an empty patch is satisfiable by doing
nothing and is reported in `empty_patch_positive_passes` so it can be reworded.

Reward combination (test-based × rubric-based) happens in `test.sh`, not here.

Judge env vars: JUDGE_API_KEY, JUDGE_BASE_URL, JUDGE_MODEL, plus the optional
JUDGE_API_DIALECT, JUDGE_REASONING_EFFORT and JUDGE_VOTES. judge_client.py falls
back to JUDGE_FALLBACK_API_KEY, OPENAI_API_KEY, OPENAI_API_BASE and its default model.
"""

import difflib
import json
import os
import re
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import verifier_report as vr  # noqa: E402

TESTS_DIR = "/tests"
# Grade the FULL agent diff, not the source-only filter: a refactor can
# legitimately modify test files too.
PATCH_PATH = "/tmp_artifact_storage/agent.patch"
PATCH_FALLBACK = "/tmp_artifact_storage/agent_source_only.patch"
RESULTS_PATH = "/tmp_artifact_storage/rubrics_results.json"

MAX_RETRIES = 8
# judge_client.py (next to this file) speaks the judge endpoint's dialect.
import judge_client  # noqa: E402


def read_file(path):
    if os.path.exists(path):
        with open(path) as f:
            return f.read()
    return ""


def _parse_llm_response(text):
    """Extract the judge's JSON object from a raw reply.

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
            if any(key in obj for key in ("ratings",)):
                return obj
            if fallback is None:
                fallback = obj
        idx = text.find("{", idx + 1)
    return fallback


def llm_call(client, model, system_prompt, user_content, want_json=True):
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
            print(f"  LLM retry {attempt + 1}/{MAX_RETRIES}: empty content "
                  f"(finish_reason={reply.finish_reason}), waiting {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
        except judge_client.JudgeRefused:
            raise
        except Exception as e:
            wait = min(2 ** (attempt + 1), 60)
            print(f"  LLM retry {attempt + 1}/{MAX_RETRIES}: {e}, waiting {wait}s",
                  file=sys.stderr)
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
    """Pull (score, score_status, evaluable) out of a parsed reply."""
    score = None
    score_status = None
    if parsed and "ratings" in parsed:
        ratings = parsed["ratings"]
        # A rating for a different criterion than the one sent is no verdict.
        if (isinstance(ratings, list) and ratings and isinstance(ratings[0], dict)
                and _statement_matches(ratings[0].get("rubric_statement"), title)):
            score = {
                "status": ratings[0].get("status"),
                "score": ratings[0].get("score"),
                "justification": ratings[0].get("justification"),
            }
            if score.get("status") is not None:
                score_status = str(score["status"]).strip().upper()
    # The verdict is the status; a score that contradicts it makes the reply unusable.
    evaluable = score_status in {"YES", "NO"}
    if evaluable and score is not None and score.get("score") is not None:
        evaluable = str(score["score"]).strip() == ("1" if score_status == "YES" else "0")
    return score, score_status, evaluable


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
        score, score_status, evaluable = _read_rating(_parse_llm_response(text), title)
        if evaluable:
            return score_status, score
        # A reply that parses but carries no usable verdict is asked again
        # before the rubric is given up.
        print(f"  Rubric {rubric_id}: retry {attempt + 1}/{MAX_RETRIES}, "
              "reply carried no usable verdict", file=sys.stderr)
    return None


def evaluate_rubrics(client, model, problem_statement, model_patch, rubrics):
    system_prompt = read_file(os.path.join(TESTS_DIR, "rubrics_system_prompt.txt"))
    user_template = read_file(os.path.join(TESTS_DIR, "rubrics_user_prompt_template.txt"))

    patch_is_empty = not model_patch.strip()
    results = []
    for rubric in rubrics:
        annotations = rubric.get("annotations", {}) or {}
        rubric_type = annotations.get("type", "")
        importance = annotations.get(
            "importance",
            rubric.get("importance", "must have"),
        )

        # Skip high-level intent headers (not actual rubric checks).
        if rubric_type == "high level intent":
            continue

        is_negative = "negative" in rubric_type

        if is_negative and patch_is_empty:
            # A negative rubric asks whether a bad behaviour is present, and an
            # empty patch has no behaviour, so the judge would answer NO and the
            # rubric would pass for free. Leave it unrated; like an unparseable
            # reply it then counts for neither the gate nor the aggregate.
            entry = vr.rubric_entry(rubric.get("id"), rubric.get("title"), importance,
                                    "negative", error=vr.skipped_error("empty agent patch"))
            results.append(entry)
            vr.print_rubric(entry)
            continue

        user_content = user_template.format(
            problem_statement=problem_statement,
            model_answer=model_patch,
            title=json.dumps(rubric["title"]),
        )
        score_status, score, votes = judge_client.majority(
            lambda: cast_vote(client, model, system_prompt, user_content, rubric.get("id"),
                              rubric["title"]),
            client.config.votes)
        rubric_evaluable = score_status is not None

        # Positive rubric: behavior present (status "YES") = pass.
        # Negative rubric: behavior absent (status "NO") = pass.
        kind = "negative" if is_negative else "positive"
        if rubric_evaluable:
            rubric_passes = (score_status == "NO") if is_negative else (score_status == "YES")
            entry = vr.rubric_entry(rubric.get("id"), rubric.get("title"), importance, kind,
                                    reward=1 if rubric_passes else 0,
                                    justification=score.get("justification"))
        else:
            # The judge never rated this rubric. It is recorded with an error
            # and, if it is a must-have, fails the gate below.
            entry = vr.rubric_entry(rubric.get("id"), rubric.get("title"), importance, kind,
                                    error=vr.UNRATED_ERROR)
        results.append(entry)
        vr.print_rubric(entry, judge_client.vote_note(votes))

    # The nop run doubles as a rubric check: a positive rubric that the judge
    # accepts on an empty patch is satisfiable by doing nothing, and will pass
    # on a bad patch too. Name it so it gets reworded.
    empty_patch_positive_passes = [
        r["title"] for r in results
        if patch_is_empty and r["type"] == "positive" and r["error"] is None and r["reward"] == 1
    ]
    for title in empty_patch_positive_passes:
        print(f"WARNING: positive rubric passed on an empty patch, so doing nothing "
              f"satisfies it; reword it: {title}", file=sys.stderr)

    # Negatives skipped on an empty patch are unrated by design; the judge
    # failures are the unrated must-haves the judge was actually asked about.
    # Every declared must-have has to be rated and pass; one whose reply never
    # parsed counts as failed. On an empty patch the skipped negatives
    # therefore fail the gate, as the tests do.
    # A total judge failure is flagged separately so it can be told apart
    # from a real failure of the patch.
    block = vr.rubrics_block(results, judge_attempted=client.calls > 0)
    if block["judge_unusable"]:
        print("ERROR: judge returned no usable rating for any of the "
              f"{block['num_must_have'] - block['num_skipped']} must-have rubrics; this verdict "
              "reflects a judge failure, not the submission", file=sys.stderr)
    elif not client.calls:
        print("ERROR: the judging budget ran out before a single rubric was sent; all "
              f"{block['num_must_have'] - block['num_skipped']} must-have rubrics went "
              "unrated and the gate fails on them", file=sys.stderr)
    elif block["num_unrated_must_have"]:
        print(f"ERROR: judge returned no usable rating for {block['num_unrated_must_have']} of "
              f"{block['num_must_have'] - block['num_skipped']} must-have rubrics; the gate fails on them",
              file=sys.stderr)
    block["empty_patch"] = patch_is_empty
    block["empty_patch_positive_passes"] = empty_patch_positive_passes
    return block


def main():
    judge = judge_client.JudgeConfig.from_env()
    api_key, base_url, model = judge.api_key, judge.base_url, judge.model
    print(f"Judge config: {judge.describe()}", file=sys.stderr)

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    # test.sh printed the VERIFIER header and the TESTS section already.
    vr.print_section("RUBRICS")

    model_patch = read_file(PATCH_PATH) or read_file(PATCH_FALLBACK)
    rubrics_text = read_file(os.path.join(TESTS_DIR, "rubrics.json")) or "[]"
    problem_statement = read_file(os.path.join(TESTS_DIR, "prompt.txt"))

    try:
        rubrics = json.loads(rubrics_text)
    except json.JSONDecodeError as e:
        result = {"error": f"failed to parse rubrics.json: {e}"}
        with open(RESULTS_PATH, "w") as f:
            json.dump(result, f, indent=2)
        vr.print_skipped(result["error"])
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    if not rubrics:
        # Nothing to grade, so the gate is trivially met.
        result = vr.rubrics_block([])
        result["pass"] = True
        result["agg_score"] = 1.0
        result["empty_patch"] = not model_patch.strip()
        result["empty_patch_positive_passes"] = []
        result["note"] = "no rubrics for task"
        with open(RESULTS_PATH, "w") as f:
            json.dump(result, f, indent=2)
        print("  no rubrics for this task; trivially passing")
        return 0

    if not model_patch.strip():
        print("WARNING: empty agent patch; judging positive rubrics only, negatives "
              "are left unrated", file=sys.stderr)

    if judge.error:
        result = {"error": judge.error, "rubric_count": len(rubrics)}
        with open(RESULTS_PATH, "w") as f:
            json.dump(result, f, indent=2)
        vr.print_skipped("invalid judge configuration")
        print(f"ERROR: {judge.error}", file=sys.stderr)
        return 1

    if not (api_key and base_url and model):
        result = {"error": "missing JUDGE_API_KEY/JUDGE_BASE_URL/JUDGE_MODEL",
                  "rubric_count": len(rubrics)}
        with open(RESULTS_PATH, "w") as f:
            json.dump(result, f, indent=2)
        vr.print_skipped("no judge credentials or model")
        print("ERROR: no LLM credentials or judge model; rubric eval skipped", file=sys.stderr)
        return 1

    client = judge_client.JudgeClient(judge)

    try:
        result = evaluate_rubrics(client, model, problem_statement, model_patch, rubrics)
    except judge_client.JudgeRefused as e:
        result = {"error": f"judge refused to grade this task: {e}",
                  "rubric_count": len(rubrics)}
        with open(RESULTS_PATH, "w") as f:
            json.dump(result, f, indent=2)
        vr.print_skipped("judge refused to grade")
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1
    with open(RESULTS_PATH, "w") as f:
        json.dump(result, f, indent=2)
    vr.print_rubrics_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
