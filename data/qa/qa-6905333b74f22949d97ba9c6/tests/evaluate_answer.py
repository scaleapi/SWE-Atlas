#!/usr/bin/env python3
"""Standalone rubrics evaluator for harbor verifier.

Reads the agent's answer from /logs/agent/answer.txt, evaluates it against
rubrics in /tests/rubrics.json, and writes the verdict to /logs/verifier/ in
the shared format defined in verifier_report.py (reward.txt, reward.json,
evaluation_results.json, plus the sectioned stdout every track prints).

The judge is configured from the environment as judge_client.py describes.
Prompt templates are read from /tests/system_prompt.txt and
/tests/user_prompt_template.txt.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
from typing import Any, Optional


sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import verifier_report as vr  # noqa: E402
import judge_client  # noqa: E402

TRACK = "qa"
ANSWER_PATH = "/logs/agent/answer.txt"
RUBRICS_PATH = "/tests/rubrics.json"
PROMPT_PATH = "/tests/prompt.txt"
SYSTEM_PROMPT_PATH = "/tests/system_prompt.txt"
USER_PROMPT_TEMPLATE_PATH = "/tests/user_prompt_template.txt"


MAX_RETRIES = 8
# Separate budget for verdicts where the judge graded a criterion it invented
# instead of the one it was sent. Each costs one cheap call and says nothing
# about the transport, so it does not draw down MAX_RETRIES.
MAX_SUBSTITUTIONS = 12
# An echoed rubric_statement this dissimilar to the one sent counts as the judge
# grading a different criterion. Trivial rewording ("States" -> "Explains") sits
# above it; dropping or softening a check sits below it.
STATEMENT_MATCH_THRESHOLD = 0.9


def _normalize_status(value: Any) -> Optional[str]:
    if value is None:
        return None
    status = str(value).strip().upper()
    if status in {"YES", "Y", "TRUE", "1"}:
        return "YES"
    if status in {"NO", "N", "FALSE", "0"}:
        return "NO"
    return None


def _normalize_score(value: Any) -> Optional[str]:
    if value is None:
        return None
    score = str(value).strip()
    if score in {"1", "1.0"}:
        return "1"
    if score in {"0", "0.0"}:
        return "0"

    lowered = score.lower()
    if lowered in {"yes", "true"}:
        return "1"
    if lowered in {"no", "false"}:
        return "0"
    return None


def _score_from_status(status: Optional[str]) -> Optional[str]:
    if status == "YES":
        return "1"
    if status == "NO":
        return "0"
    return None


def _apply_negative_flip(raw_score: Optional[str], rubric_type: str) -> tuple[Optional[str], bool]:
    if raw_score not in {"0", "1"}:
        return None, False
    if "negative" in (rubric_type or "").lower():
        return ("0" if raw_score == "1" else "1"), True
    return raw_score, False


def _canonicalize_judge_result(parsed: dict[str, Any], rubric_type: str) -> Optional[dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None

    judge_score = {
        "rubric_statement": parsed.get("rubric_statement"),
        "status": parsed.get("status"),
        "score": parsed.get("score"),
        "justification": parsed.get("justification"),
    }

    normalized_status = _normalize_status(judge_score.get("status"))
    normalized_score = _normalize_score(judge_score.get("score"))
    status_score = _score_from_status(normalized_status)

    mismatch = (
        normalized_status is not None
        and normalized_score is not None
        and status_score != normalized_score
    )

    # Status is canonical if present; score is fallback.
    canonical_raw_score = status_score if status_score is not None else normalized_score
    effective_score, was_flipped = _apply_negative_flip(canonical_raw_score, rubric_type)

    if effective_score in {"0", "1"}:
        effective_status = "YES" if effective_score == "1" else "NO"
    elif canonical_raw_score in {"0", "1"}:
        effective_status = "YES" if canonical_raw_score == "1" else "NO"
    else:
        effective_status = normalized_status

    return {
        "rubric_statement": judge_score.get("rubric_statement"),
        "status": effective_status,
        "score": effective_score,
        "justification": judge_score.get("justification"),
        "judge_score": judge_score,
        "judge_score_canonical": canonical_raw_score,
        "judge_status_score_mismatch": mismatch,
        "was_flipped": was_flipped,
        "rubric_type": rubric_type,
    }


def _is_scored(score_obj: Any) -> bool:
    return isinstance(score_obj, dict) and str(score_obj.get("score")) in {"0", "1"}


def _normalized(text: str) -> str:
    # Case, whitespace and punctuation are presentation; the words are the criterion.
    return " ".join(re.sub(r"[^a-z0-9%./_\- ]+", " ", text.lower()).split())


def _statement_similarity(echoed: Any, title: str) -> float:
    if not isinstance(echoed, str) or not echoed.strip():
        return 0.0
    a, b = _normalized(echoed), _normalized(title)
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    # A truncated echo (typically the trailing "e.g." clause dropped) is still
    # the same criterion; a bare fragment like "Reports" is not.
    if short and short in long_ and len(short) >= 0.6 * len(long_):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _statement_matches(echoed: Any, title: str) -> bool:
    """The judge must echo the rubric statement it was asked to grade. A
    dissimilar echo means it graded a criterion of its own instead of the one
    sent, and a reworded one has usually dropped or softened a check (the
    observed failure mode: "reports whether X matches Y" echoed back as
    "explains Y"), so only trivial rewording passes."""
    if not isinstance(echoed, str) or not echoed.strip():
        return True  # nothing echoed to compare against
    return _statement_similarity(echoed, title) >= STATEMENT_MATCH_THRESHOLD


def _correction_note(title: str, echoed: Any) -> str:
    """Appended to the prompt after the judge graded a criterion it made up, so
    the retry names the detour instead of hoping a fresh sample lands on the
    criterion that was actually sent."""
    return (
        "\n\n# Correction\n"
        "A previous rating graded this statement, which is NOT the criterion:\n"
        + json.dumps(echoed if isinstance(echoed, str) else str(echoed))
        + "\nGrade only the criterion below and copy it verbatim into rubric_statement:\n"
        + json.dumps(title)
        + "\n"
    )


def evaluate_single_rubric(client, system_prompt, user_prompt_template,
                           problem_statement, model_answer, rubric):
    """Evaluate a single rubric criterion.

    Returns the parsed verdict, or a dict carrying only ``grading_failure`` (plus
    the substitution count) when no usable verdict came back. Callers treat the
    latter as unrated; the reason travels into evaluation_results.json.
    """
    # Strip numeric prefix (e.g. "1.1: ") from rubric title
    title = re.sub(r"^\d+(\.\d+)*:\s*", "", rubric["title"])
    user_content = user_prompt_template.format(
        problem_statement=problem_statement,
        model_answer=model_answer,
        title=json.dumps(title),
    )

    attempt = 0
    substitutions = 0
    echoed = None
    failure = None
    while attempt < MAX_RETRIES and substitutions < MAX_SUBSTITUTIONS:
        if judge_client.past_deadline():
            # Leaving the loop reports the rubric unrated, not failed.
            print(
                f"  Judging exceeded its {judge_client.PASS_DEADLINE_SEC}s budget "
                f"({judge_client.pass_elapsed():.0f}s elapsed); leaving the rest unrated",
                file=sys.stderr,
            )
            failure = "judging_deadline_exceeded"
            break
        content = user_content
        if echoed is not None:
            content = content + _correction_note(title, echoed)
        try:
            reply = client.complete(system_prompt, content)
            text = reply.text
            if not text.strip():
                # A reasoning judge that spends its whole max_tokens budget on
                # thinking returns finish_reason "length" and no content, which
                # is not a grading signal.
                attempt += 1
                failure = "empty_judge_response"
                print(f"  Retry {attempt}/{MAX_RETRIES}: empty content "
                      f"(finish_reason={reply.finish_reason}) for rubric "
                      f"{rubric['id']}", file=sys.stderr)
                continue
            parsed = _parse_response(text, title)
            status_score = _score_from_status(_normalize_status(parsed.get("status"))) if parsed else None
            parsed_score = _normalize_score(parsed.get("score")) if parsed else None
            if parsed and (status_score in {"0", "1"} or parsed_score in {"0", "1"}):
                if not _statement_matches(parsed.get("rubric_statement"), title):
                    # The judge graded a criterion of its own instead of the one
                    # it was sent. Ask again, naming the statement it wandered to.
                    substitutions += 1
                    echoed = parsed.get("rubric_statement")
                    failure = "judge_substituted_criterion"
                    print(
                        f"  Substitution {substitutions}/{MAX_SUBSTITUTIONS}: judge graded a "
                        f"different criterion for rubric {rubric['id']}: {str(echoed)[:160]!r}",
                        file=sys.stderr,
                    )
                    continue
                parsed["rubric_id"] = rubric["id"]
                parsed["judge_substitutions"] = substitutions
                return parsed
            attempt += 1
            failure = "invalid_judge_response"
            print(f"  Retry {attempt}/{MAX_RETRIES}: invalid response for rubric {rubric['id']}", file=sys.stderr)
        except judge_client.JudgeRefused:
            raise
        except Exception as e:
            attempt += 1
            failure = "judge_request_error: %s" % (str(e)[:200] or type(e).__name__)
            wait = min(2 ** attempt, 60)
            print(f"  Retry {attempt}/{MAX_RETRIES}: {e}, waiting {wait}s", file=sys.stderr)
            time.sleep(wait)

    return {
        "grading_failure": failure or "no_judge_verdict",
        "judge_substitutions": substitutions,
        "last_echoed_statement": echoed,
    }


def evaluate_rubric_by_majority(client, system_prompt, user_prompt_template,
                               problem_statement, model_answer, rubric):
    """The majority of client.config.votes verdicts from evaluate_single_rubric, with
    the substitutions of every vote counted and the vote tally under "votes"."""
    substitutions = 0
    failure = {}

    def cast_vote():
        nonlocal substitutions
        result = evaluate_single_rubric(client, system_prompt, user_prompt_template,
                                        problem_statement, model_answer, rubric)
        substitutions += result.get("judge_substitutions", 0)
        if result.get("grading_failure"):
            failure.update(result)
            return None
        raw = _score_from_status(_normalize_status(result.get("status"))) or _normalize_score(result.get("score"))
        return ("YES" if raw == "1" else "NO"), result

    _, result, votes = judge_client.majority(cast_vote, client.config.votes)
    result = dict(result or failure)
    result["judge_substitutions"] = substitutions
    result["votes"] = votes
    return result


def _load_json_object(text):
    """Extract the judge's JSON object from a raw reply.

    Strict json.loads rejects two things judges do routinely: literal control
    characters inside strings (tabs copied out of code) and wrapping the
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
            if "ratings" in obj:
                return obj
            if fallback is None:
                fallback = obj
        idx = text.find("{", idx + 1)
    return fallback


def _parse_response(text, title=None):
    """Parse JSON response and extract the rating for the requested rubric.

    The judge is sent exactly one criterion, but occasionally returns several
    ratings; picking blindly would grade the wrong criterion, so prefer the
    rating whose echoed rubric_statement best matches the requested title."""
    parsed = _load_json_object(text)
    if isinstance(parsed, dict) and "ratings" in parsed:
        ratings = parsed["ratings"]
        if isinstance(ratings, list) and len(ratings) > 0:
            r = ratings[0]
            if title is not None and len(ratings) > 1:
                r = max(
                    ratings,
                    key=lambda item: _statement_similarity(
                        item.get("rubric_statement") if isinstance(item, dict) else None,
                        title,
                    ),
                )
            if not isinstance(r, dict):
                return None
            return {
                "rubric_statement": r.get("rubric_statement"),
                "status": r.get("status"),
                "score": r.get("score"),
                "justification": r.get("justification"),
            }
    return None


def _rubric_entry(rubric, rubric_type, result, failure=None):
    """Fold the canonicalised judge verdict into the shared rubric record.

    reward is the effective verdict (after the flip for a negative rubric); the
    judge's justification travels with it. An unrated rubric carries the reason
    the judge never produced a usable verdict.
    """
    importance = vr.rubric_importance(rubric)
    kind = "negative" if "negative" in (rubric_type or "").lower() else "positive"
    if not _is_scored(result):
        error = vr.UNRATED_ERROR + (": " + failure if failure else "")
        return vr.rubric_entry(rubric["id"], rubric["title"], importance, kind,
                               error=error)
    judge = result.get("judge_score") or {}
    return vr.rubric_entry(rubric["id"], rubric["title"], importance, kind,
                           reward=1 if str(result["score"]) == "1" else 0,
                           justification=judge.get("justification"))


def main():
    judge = judge_client.JudgeConfig.from_env()
    print(f"Judge config: {judge.describe()}", file=sys.stderr)
    vr.print_header(TRACK)

    if judge.error:
        vr.print_section("RUBRICS")
        vr.print_skipped("invalid judge configuration")
        vr.fail_early(TRACK, judge.error)
        sys.exit(1)

    missing = judge.missing()
    if missing:
        print("ERROR: set " + ", ".join(missing), file=sys.stderr)
        sys.exit(1)

    # qa runs no tests and asks for no manifest; the sections are still printed
    # so a trial reads the same in every track.
    vr.print_section("TESTS")
    vr.print_not_applicable()

    vr.print_section("RUBRICS")

    # No answer → score 0
    if not os.path.exists(ANSWER_PATH):
        vr.print_skipped("no answer file")
        vr.print_section("MANIFEST")
        vr.print_not_applicable()
        vr.fail_early(TRACK, f"no answer file at {ANSWER_PATH}")
        return

    answer = open(ANSWER_PATH).read().strip()
    if "<<FINAL_ANSWER>>" in answer:
        parts = answer.split("<<FINAL_ANSWER>>")
        answer = parts[1].strip() if len(parts) >= 2 else answer

    if not answer:
        vr.print_skipped("empty answer")
        vr.print_section("MANIFEST")
        vr.print_not_applicable()
        vr.fail_early(TRACK, "empty answer", details={"answer_chars": 0})
        return

    # Read prompts and rubrics
    system_prompt = open(SYSTEM_PROMPT_PATH).read()
    user_prompt_template = open(USER_PROMPT_TEMPLATE_PATH).read()
    rubrics = json.load(open(RUBRICS_PATH))
    problem_statement = open(PROMPT_PATH).read().strip() if os.path.exists(PROMPT_PATH) else ""

    # Evaluate each rubric
    client = judge_client.JudgeClient(judge)
    entries = []
    substitutions = 0
    ungraded = []
    for rubric in rubrics:
        judge_result = evaluate_rubric_by_majority(
            client, system_prompt, user_prompt_template,
            problem_statement, answer, rubric,
        )
        rubric_type = str(rubric.get("annotations", {}).get("type", ""))
        substitutions += (judge_result or {}).get("judge_substitutions", 0)
        failure = (judge_result or {}).get("grading_failure")
        result = None if failure else _canonicalize_judge_result(judge_result, rubric_type)
        entry = _rubric_entry(rubric, rubric_type, result, failure)
        if entry["error"] is not None and entry["importance"] == "must have":
            ungraded.append("%s (%s)" % (rubric["id"], failure or "invalid judge score"))
        entries.append(entry)
        note = None
        if result is not None and not _is_scored(result):
            note = "invalid judge score"
        elif result is not None and result.get("judge_status_score_mismatch"):
            note = "status/score mismatch"
        note = ", ".join(n for n in (note, judge_client.vote_note(judge_result["votes"])) if n)
        vr.print_rubric(entry, note or None)

    # Every declared must-have has to be scored and score 1; an unscored
    # must-have fails the answer.
    rubrics_block = vr.rubrics_block(entries, judge_attempted=client.calls > 0)
    must_haves = rubrics_block["num_must_have"]
    unrated_must_haves = rubrics_block["num_unrated_must_have"]

    # Losing every must-have makes the gate a hard fail on an answer the judge
    # never rated. Flag it so the trial can be told apart from a real failure.
    if rubrics_block["judge_unusable"]:
        print(f"ERROR: judge returned no usable rating for any of the "
              f"{must_haves} must-have rubrics; this verdict reflects a "
              f"judge failure, not the submission", file=sys.stderr)
    elif not client.calls:
        print(f"ERROR: the judging budget ran out before a single rubric was "
              f"sent; all {must_haves} must-have rubrics went unrated and the "
              f"answer fails on them", file=sys.stderr)
    elif unrated_must_haves:
        print(f"ERROR: judge returned no usable rating for {unrated_must_haves} of "
              f"{must_haves} must-have rubrics; the answer fails on them: "
              f"{', '.join(ungraded)}", file=sys.stderr)
    vr.print_rubrics_summary(rubrics_block)

    vr.print_section("MANIFEST")
    vr.print_not_applicable()

    # qa has no tests or manifest, so the overall verdict is the must-have verdict.
    vr.finish(vr.report(TRACK, rubrics_block["pass"], rubrics=rubrics_block,
                        details={"answer_chars": len(answer),
                                 "judge_substitutions": substitutions}))


if __name__ == "__main__":
    try:
        main()
    except judge_client.JudgeRefused as e:
        # Not a failed answer: the judge would not look at it.
        vr.fail_early(TRACK, f"judge refused to grade this task: {e}")
        sys.exit(1)
