import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import urllib.error

from test_run_council import RUNNER, sample_position, process_capture

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deepseek_http.py"
SPEC = importlib.util.spec_from_file_location("deepseek_http", SCRIPT)
HTTP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HTTP)


def provider_response():
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps(sample_position()),
                    "reasoning_content": "PRIVATE REASONING DO NOT PERSIST"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                  "completion_tokens_details": {"reasoning_tokens": 12}},
        "untrusted_field": "DO NOT PERSIST"}


class DeepSeekHTTPTests(unittest.TestCase):
    def test_body_matches_evaluated_stateless_settings(self):
        self.assertEqual(HTTP.make_body("task"), {
            "model": "deepseek-flash", "messages": [{"role": "user", "content": "task"}],
            "thinking": {"type": "enabled"}, "reasoning_effort": "max",
            "response_format": {"type": "json_object"}, "stream": False})
        command = RUNNER.command_for("deepseek-flash", Path("/tmp/unused"))
        self.assertEqual(command, [sys.executable, "-I", "-B", str(SCRIPT)])

    def test_response_keeps_final_and_usage_not_reasoning_or_unknown_fields(self):
        result = HTTP.parse_response(json.dumps(provider_response()).encode())
        self.assertEqual(result["structured_output"], sample_position())
        self.assertEqual(result["usage"]["reasoning_tokens"], 12)
        self.assertNotIn("DO NOT PERSIST", json.dumps(result))
        position, usage, _ = RUNNER.parse_model_output("deepseek-flash", json.dumps(result), Path("/tmp/unused"))
        RUNNER.validate_structured(position)
        self.assertIsNone(usage["cost_usd"])
        self.assertFalse(usage["cost_available"])
        self.assertEqual(usage["server_effort_attestation"], "not-returned")

    def test_bad_provider_contracts_fail_closed(self):
        cases = []
        for model in (None, "deepseek-v4-pro"):
            row = provider_response(); row["model"] = model; cases.append(row)
        for finish in ("length", "tool_calls", None):
            row = provider_response(); row["choices"][0]["finish_reason"] = finish; cases.append(row)
        for field, value in (("tool_calls", [{"id": "bad"}]), ("function_call", {"name": "bad"}),
                             ("content", "[]"), ("content", '{"x":1,"x":2}'), ("content", '{"x":NaN}')):
            row = provider_response(); row["choices"][0]["message"][field] = value; cases.append(row)
        for usage in (None, {"prompt_tokens": True, "completion_tokens": 20, "total_tokens": 21},
                      {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 99}):
            row = provider_response(); row["usage"] = usage; cases.append(row)
        row = provider_response(); row["choices"] *= 2; cases.append(row)
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ValueError):
                HTTP.parse_response(json.dumps(row).encode())
        with self.assertRaises(ValueError):
            HTTP.parse_response(b"x" * (HTTP.MAX_BYTES + 1))

    def test_redirect_is_rejected(self):
        with self.assertRaises(ValueError):
            HTTP.RejectRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid")

    def test_transport_attempts_once_on_error_and_does_not_echo_details(self):
        opener = mock.MagicMock()
        opener.open.side_effect = urllib.error.HTTPError("https://secret.invalid", 429,
                                                        "secret-value", {}, None)
        with mock.patch.object(HTTP.urllib.request, "build_opener", return_value=opener), self.assertRaises(urllib.error.HTTPError):
            HTTP.invoke("task", "secret-value")
        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, HTTP.ENDPOINT)
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret-value"}, clear=True), \
             mock.patch.object(sys, "stdin", io.StringIO("task")), \
             mock.patch.object(HTTP, "invoke", side_effect=opener.open.side_effect), \
             mock.patch.object(sys, "stderr", io.StringIO()) as stderr:
            self.assertEqual(HTTP.main(), 1)
        self.assertNotIn("secret", stderr.getvalue())
        self.assertIn("429", stderr.getvalue())

    def test_credentials_are_route_only_with_explicit_precedence(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key", "OPENAI_API_KEY": "other-key",
                                         "ANTHROPIC_AUTH_TOKEN": "other-token", "PYTHONPATH": "/untrusted"}, clear=True):
            env = RUNNER.clean_env("deepseek-flash", {"DEEPSEEK_API_KEY": "explicit-key"})
        self.assertEqual(env["DEEPSEEK_API_KEY"], "explicit-key")
        self.assertNotIn("other-key", env.values())
        self.assertNotIn("other-token", env.values())
        self.assertNotIn("PYTHONPATH", env)

    def test_missing_credential_stops_before_process_and_is_not_usable(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(RUNNER, "_project_opencode_route_auth", return_value=None), \
             mock.patch.object(RUNNER, "run_process_monitored") as process:
            result = RUNNER.run_model("deepseek-flash", "task", Path(directory), 10, 0, Path(directory), False)
        process.assert_not_called()
        self.assertEqual(result.failure_category, "safety_preflight")
        self.assertFalse(result.parse_ok)

    def test_real_runner_path_validates_identity_schema_and_timeout(self):
        good = HTTP.parse_response(json.dumps(provider_response()).encode())
        bad_model = copy.deepcopy(good); bad_model["model"] = "deepseek-v4-pro"
        bad_schema = copy.deepcopy(good); bad_schema["structured_output"]["confidence"] = 2
        for payload, valid in ((good, True), (bad_model, False), (bad_schema, False)):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-key"}, clear=True), \
                 mock.patch.object(RUNNER, "run_process_monitored", return_value=process_capture(json.dumps(payload))):
                root = Path(directory)
                result = RUNNER.run_model("deepseek-flash", "task", root, 10, 0, root, False)
                self.assertEqual(result.parse_ok, valid)
                self.assertEqual((root / "deepseek-flash.json").exists(), valid)
                self.assertEqual(result.session_cleanup_status, "stateless-no-local-session")

    def test_timeout_and_output_limit_do_not_admit_a_seat(self):
        good = HTTP.parse_response(json.dumps(provider_response()).encode())
        for reason, output, status in (("hard_timeout", "", "timeout"),
                                      ("output_limit", json.dumps(good), "output-limit")):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory, \
                 mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-key"}, clear=True), \
                 mock.patch.object(RUNNER, "run_process_monitored", return_value=process_capture(
                     output, exit_code=-15, termination_reason=reason)) as process:
                root = Path(directory)
                result = RUNNER.run_model("deepseek-flash", "task", root, 10, 0, root, False)
                self.assertEqual(result.status, status)
                self.assertFalse(result.parse_ok)
                self.assertFalse((root / "deepseek-flash.json").exists())
                self.assertEqual(process.call_count, 1)

    def test_successful_http_read_is_bounded_and_single_attempt(self):
        opener = mock.MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.status = 200
        response.read.return_value = json.dumps(provider_response()).encode()
        with mock.patch.object(HTTP.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(HTTP.invoke("task", "unit-key")["model"], "deepseek-flash")
        response.read.assert_called_once_with(HTTP.MAX_BYTES + 1)
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
