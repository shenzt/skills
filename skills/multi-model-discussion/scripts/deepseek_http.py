#!/usr/bin/env python3
"""One-shot official DeepSeek Flash transport; stdin in, sanitized JSON out.

No tools, session store, SDK retries, redirect following, or generation caps.
The council parent supervises wall time and output size. The full provider
envelope and reasoning text never leave this subprocess.
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
VERSION = "deepseek-stateless-http-v1"
MAX_BYTES = 8 * 1024 * 1024


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non-finite JSON value")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)


def make_body(prompt):
    return {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "enabled"}, "reasoning_effort": "max",
            "response_format": {"type": "json_object"}, "stream": False}


def parse_response(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError("provider response exceeds byte limit")
    envelope = strict_json(raw)
    if not isinstance(envelope, dict) or envelope.get("model") != MODEL or envelope.get("error"):
        raise ValueError("provider model mismatch or error")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("expected one choice")
    choice = choices[0]
    message = choice.get("message")
    if choice.get("finish_reason") != "stop" or not isinstance(message, dict):
        raise ValueError("expected terminal stop")
    if message.get("role") != "assistant" or message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
        raise ValueError("tool call or refusal is not a council answer")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip() or "<tool_call>" in content.lower():
        raise ValueError("expected non-tool answer text")
    structured = strict_json(content)
    if not isinstance(structured, dict):
        raise ValueError("expected answer object")
    raw_usage = envelope.get("usage")
    if not isinstance(raw_usage, dict):
        raise ValueError("missing usage")
    usage = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                 "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
        value = raw_usage.get(name)
        if value is None and name.startswith("prompt_cache_"):
            continue
        if type(value) is not int or not 0 <= value <= 10**15:
            raise ValueError("invalid token usage")
        usage[name] = value
    if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
        raise ValueError("inconsistent token usage")
    details = raw_usage.get("completion_tokens_details")
    if isinstance(details, dict) and "reasoning_tokens" in details:
        value = details["reasoning_tokens"]
        if type(value) is not int or not 0 <= value <= usage["completion_tokens"]:
            raise ValueError("invalid reasoning usage")
        usage["reasoning_tokens"] = value
    return {"source": VERSION, "model": MODEL, "finish_reason": "stop",
            "structured_output": structured, "usage": usage}


class RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("provider redirects are forbidden")


def invoke(prompt, key):
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(make_body(prompt), ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": VERSION}, method="POST")
    opener = urllib.request.build_opener(
        RejectRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    with opener.open(request, timeout=900) as response:
        if response.status != 200:
            raise ValueError("unexpected HTTP status")
        return parse_response(response.read(MAX_BYTES + 1))


def main():
    try:
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key or any(ch in key for ch in ("\r", "\n", "\x00")):
            raise ValueError("missing or invalid API key")
        prompt = sys.stdin.read(MAX_BYTES + 1)
        if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_BYTES:
            raise ValueError("empty or oversized prompt")
        result = invoke(prompt, key)
    except Exception as error:
        # Never echo URLs, headers, error bodies, prompts, or API credentials.
        status = error.code if isinstance(error, urllib.error.HTTPError) else None
        print(json.dumps({"error_type": type(error).__name__, "http_status": status,
                          "cost_status": "unknown_after_attempt"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
