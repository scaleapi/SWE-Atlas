#!/usr/bin/env python3
"""Rubric judge client. Speaks openai-chat (<base>/chat/completions:
reasoning_effort, max_completion_tokens, response_format) or anthropic-messages
(<host>/v1/messages: output_config.effort, adaptive thinking, max_tokens),
picked from JUDGE_BASE_URL and JUDGE_MODEL unless JUDGE_API_DIALECT says otherwise.
With no JUDGE_* set it falls back to OPENAI_API_KEY / OPENAI_API_BASE, DEFAULT_MODEL and
DEFAULT_EFFORT (high; JUDGE_REASONING_EFFORT overrides it). A rejected parameter is
renamed or dropped once and remembered.
Each verdict is the majority of JUDGE_VOTES independent replies (default 3, odd so it
never ties); majority() stops asking once one side has it.

Both dialects post over stdlib HTTP: the module is stdlib only, so it runs under -I -S -B.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "openai/gpt-6.1-sol"
DEFAULT_EFFORT = "high"
DEFAULT_VOTES = 3
MODEL_DEFAULT_EFFORT = "model default"
EFFORT_DROPPED_PATH = "/tmp/judge_effort_dropped"
MAX_TOKENS = 64000
FALLBACK_MAX_TOKENS = 8192
OPENAI_CHAT = "openai-chat"
ANTHROPIC_MESSAGES = "anthropic-messages"
DIALECTS = (OPENAI_CHAT, ANTHROPIC_MESSAGES)
# Per request, both dialects. Covers a slow call without sitting on a stalled one.
HTTP_TIMEOUT_SEC = 600
# Wall clock for judging, measured from the moment this module is imported. That
# is verifier start for qa and tw, but rf imports it in evaluate_rubrics.py, after
# the validator and the comparison, so rf's test phases fall outside it. A call in
# flight at the deadline can take HTTP_TIMEOUT_SEC more, so qa and tw verifiers need
# about 3000 s, and both set timeout_sec to 3600.
PASS_DEADLINE_SEC = 2400
_PASS_START = time.monotonic()


def pass_elapsed():
    return time.monotonic() - _PASS_START


def note_effort_dropped():
    try:
        with open(EFFORT_DROPPED_PATH, "w") as f:
            f.write("1\n")
    except OSError:
        pass


def effort_dropped():
    return os.path.exists(EFFORT_DROPPED_PATH)


def past_deadline():
    return pass_elapsed() >= PASS_DEADLINE_SEC


_NAME_WORDS = (
    "nsupported", "not supported", "nrecognized", "unknown parameter",
    "unexpected keyword", "extra inputs", "does not support", "not permitted", "not allowed",
)
_VALUE_WORDS = ("invalid value", "should be one of", "is not one of")


def env_value(name):
    value = os.environ.get(name, "").strip()
    if value.startswith("${") and value.endswith("}"):  # unexpanded template = unset
        return ""
    return value


def _log(message):
    print(f"  {message}", file=sys.stderr)


def split_url(base_url):
    rest = base_url.split("://", 1)[1] if "://" in base_url else base_url
    hostport, _, path = rest.partition("/")
    hostport = hostport.rsplit("@", 1)[-1]
    if hostport.startswith("["):
        host = hostport[1:].split("]", 1)[0]
    else:
        host = hostport.split(":", 1)[0]
    return host, ("/" + path if path else "")


def resolve_dialect(base_url, model, explicit=""):
    if explicit:
        if explicit not in DIALECTS:
            raise ValueError(
                f"JUDGE_API_DIALECT must be one of {', '.join(DIALECTS)}, got {explicit!r}"
            )
        return explicit
    host, path = split_url(base_url)
    if "anthropic" in host.lower():
        return ANTHROPIC_MESSAGES
    if path.rstrip("/"):
        return OPENAI_CHAT
    if "/" not in model and model.startswith("claude"):
        return ANTHROPIC_MESSAGES
    return OPENAI_CHAT


def resolve_votes(raw):
    if not raw:
        return DEFAULT_VOTES
    if not raw.isdigit() or int(raw) % 2 == 0:
        raise ValueError(f"JUDGE_VOTES must be an odd whole number (1, 3, 5, ...), got {raw!r}")
    return int(raw)


def majority(cast_vote, votes):
    """Ask for verdicts until one has a majority of `votes`. cast_vote() returns
    (verdict, reply), or None when no usable verdict came back, which ends the count
    with no verdict. Returns (verdict, the first reply that gave it, {verdict: count})."""
    tally = {}
    replies = {}
    while max(tally.values(), default=0) <= votes // 2:
        vote = cast_vote()
        if vote is None:
            return None, None, tally
        verdict, reply = vote
        tally[verdict] = tally.get(verdict, 0) + 1
        replies.setdefault(verdict, reply)
    verdict = max(tally, key=tally.get)
    return verdict, replies[verdict], tally


def vote_note(tally):
    """'votes YES 2, NO 1' when the votes split, None when they agree."""
    if len(tally) < 2:
        return None
    return "votes " + ", ".join(f"{k} {v}" for k, v in sorted(tally.items(), key=lambda kv: -kv[1]))


class JudgeConfig:
    def __init__(self, api_key, base_url, model, effort, effort_source, dialect, votes=DEFAULT_VOTES,
                 error=None):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.effort = effort
        self.effort_source = effort_source
        self.dialect = dialect
        self.votes = votes
        self.error = error

    @classmethod
    def from_env(cls):
        api_key = (env_value("JUDGE_API_KEY") or env_value("JUDGE_FALLBACK_API_KEY")
                   or env_value("OPENAI_API_KEY"))
        base_url = (env_value("JUDGE_BASE_URL") or env_value("OPENAI_API_BASE")).rstrip("/")
        model = env_value("JUDGE_MODEL") or DEFAULT_MODEL
        effort = env_value("JUDGE_REASONING_EFFORT")
        source = "env" if effort else "default"
        dialect = resolve_dialect(base_url, model, env_value("JUDGE_API_DIALECT"))
        try:
            votes, error = resolve_votes(env_value("JUDGE_VOTES")), None
        except ValueError as e:
            votes, error = None, str(e)
        return cls(api_key, base_url, model, effort or DEFAULT_EFFORT, source, dialect, votes, error)

    def missing(self):
        names = []
        if not self.api_key:
            names.append("JUDGE_API_KEY (or JUDGE_FALLBACK_API_KEY or OPENAI_API_KEY)")
        if not self.base_url:
            names.append("JUDGE_BASE_URL (or OPENAI_API_BASE)")
        if not self.model:
            names.append("JUDGE_MODEL")
        return names

    def describe(self):
        route = re.sub(r"//[^/@]*@", "//", self.base_url) or "<unset>"
        return (
            f"model={self.model or '<unset>'} dialect={self.dialect} "
            f"reasoning_effort={self.effort} ({self.effort_source}) votes={self.votes or 'invalid'} base_url={route}"
        )


class JudgeReply:
    def __init__(self, text, finish_reason, usage=None, model=None):
        self.text = text or ""
        self.finish_reason = finish_reason or ""
        self.usage = usage or {}
        self.model = model


def _retryable_status(status):
    """A 4xx says the request is wrong, so only these are worth sending again."""
    return status in (408, 409, 425, 429) or status >= 500


class JudgeError(Exception):
    pass


class JudgeRefused(JudgeError):
    """The judge rejected the request itself; sending it again cannot help."""
    pass


def _names_param(message, name):
    return re.search(r"(^|[\s\-(])" + re.escape(name) + r"[.:]", message) is not None


def _rejected_parameter(message, names, words):
    lowered = message.lower()
    if not any(word in lowered for word in words):
        return None
    for name in names:
        if name in message:
            return name
    return None


def _post_json(url, headers, body):
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"error": raw[:500]}
        return err.code, payload
    except (urllib.error.URLError, OSError) as err:
        raise JudgeError(f"judge unreachable at {url}: {err}") from err


class JudgeClient:
    def __init__(self, config):
        self.config = config
        self.calls = 0
        self._token_param = "max_completion_tokens"
        self._max_tokens = MAX_TOKENS
        self._send_effort = config.effort != MODEL_DEFAULT_EFFORT
        self._send_json = True
        self._send_thinking = True

    def complete(self, system_prompt, user_content, want_json=False):
        self.calls += 1
        if self.config.dialect == ANTHROPIC_MESSAGES:
            return self._complete_anthropic(system_prompt, user_content)
        return self._complete_openai(system_prompt, user_content, want_json)

    def _complete_openai(self, system_prompt, user_content, want_json):
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "content-type": "application/json",
            "authorization": "Bearer %s" % self.config.api_key,
        }
        for _ in range(4):
            body = {
                "model": self.config.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                self._token_param: self._max_tokens,
            }
            if self._send_effort:
                body["reasoning_effort"] = self.config.effort
            if want_json and self._send_json:
                body["response_format"] = {"type": "json_object"}
            status, payload = _post_json(url, headers, body)
            if status >= 400:
                error = payload.get("error")
                message = str(error.get("message") if isinstance(error, dict) else error or payload)
                if self._adapt_openai(message):
                    continue
                text = f"judge HTTP {status}: {json.dumps(payload)[:500]}"
                if not _retryable_status(status):
                    raise JudgeRefused(text)
                raise JudgeError(text)
            choice = (payload.get("choices") or [{}])[0]
            usage = payload.get("usage") or None
            return JudgeReply(
                (choice.get("message") or {}).get("content"),
                choice.get("finish_reason"),
                {
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                } if usage else None,
                payload.get("model"),
            )
        raise JudgeError("judge kept rejecting request parameters")

    def _adapt_openai(self, message):
        rejected = _rejected_parameter(message, (self._token_param, "response_format"), _NAME_WORDS)
        if rejected == self._token_param:
            other = "max_tokens" if rejected == "max_completion_tokens" else "max_completion_tokens"
            self._token_param = other
            _log(f"Judge rejected {rejected}; retrying with {other}")
            return True
        if rejected == "response_format" and self._send_json:
            self._send_json = False
            _log("Judge rejected response_format; retrying without it")
            return True
        if self._max_tokens != FALLBACK_MAX_TOKENS and ("max_tokens" in message or "max_completion_tokens" in message):
            self._max_tokens = FALLBACK_MAX_TOKENS
            _log(f"Judge rejected a cap of {MAX_TOKENS}; retrying with {FALLBACK_MAX_TOKENS}")
            return True
        if self._send_effort and _rejected_parameter(message, ("reasoning_effort",), _NAME_WORDS + _VALUE_WORDS):
            self._send_effort = False
            note_effort_dropped()
            _log(f"WARNING: judge rejected reasoning_effort={self.config.effort}; retrying without it")
            return True
        return False

    def _complete_anthropic(self, system_prompt, user_content):
        host_url = self.config.base_url
        if host_url.endswith("/v1"):
            host_url = host_url[:-3]
        url = host_url.rstrip("/") + "/v1/messages"
        headers = {
            "content-type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",  # required API version header, same for every model
        }
        for _ in range(3):
            body = {
                "model": self.config.model,
                "max_tokens": self._max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
            }
            if self._send_thinking:
                body["thinking"] = {"type": "adaptive"}
            if self._send_effort:
                body["output_config"] = {"effort": self.config.effort}
            status, payload = _post_json(url, headers, body)
            if status == 400:
                error = payload.get("error")
                message = str(error.get("message") if isinstance(error, dict) else error or payload)
                if self._send_thinking and _names_param(message, "thinking"):
                    self._send_thinking = False
                    _log("Judge rejected adaptive thinking; retrying without it")
                    continue
                if self._send_effort and (_names_param(message, "output_config") or _names_param(message, "effort")):
                    self._send_effort = False
                    note_effort_dropped()
                    _log(f"WARNING: judge rejected effort={self.config.effort}; retrying without it")
                    continue
                if self._max_tokens != FALLBACK_MAX_TOKENS and _names_param(message, "max_tokens"):
                    self._max_tokens = FALLBACK_MAX_TOKENS
                    _log(f"Judge rejected a cap of {MAX_TOKENS}; retrying with {FALLBACK_MAX_TOKENS}")
                    continue
            if status >= 400:
                text = f"judge HTTP {status}: {json.dumps(payload)[:500]}"
                if not _retryable_status(status):
                    raise JudgeRefused(text)
                raise JudgeError(text)
            text = "".join(
                block.get("text", "") for block in payload.get("content", [])
                if block.get("type") == "text"
            )
            stop = payload.get("stop_reason") or ""
            finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}.get(stop, stop)
            usage = payload.get("usage") or {}
            return JudgeReply(
                text, finish,
                {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")},
                payload.get("model"),
            )
        raise JudgeError("judge kept rejecting request parameters")
