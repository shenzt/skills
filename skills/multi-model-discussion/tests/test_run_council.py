import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_council.py"
SPEC = importlib.util.spec_from_file_location("run_council", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


class RunCouncilTests(unittest.TestCase):
    def setUp(self):
        # Keep OpenCode unit tests hermetic: the runner sees a fake route-only
        # credential and every model process/auxiliary call remains mocked.
        patcher = mock.patch.dict(
            os.environ,
            {"DASHSCOPE_API_KEY": "UNIT-TEST-DASHSCOPE-KEY"},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_participant_schema_avoids_cli_incompatible_meta_schema(self):
        schema = json.loads(RUNNER.SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("$schema", schema)

    def test_claude_command_pins_fable_5_1_model(self):
        command = RUNNER.command_for("claude", Path("/tmp/unused"))
        self.assertIn("--model", command)
        self.assertEqual(
            command[command.index("--model") + 1],
            "claude-fable-5-1",
        )
        self.assertIn("--effort", command)
        self.assertEqual(
            command[command.index("--effort") + 1],
            "max",
        )
        self.assertNotIn("--max-budget-usd", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertIn("--no-chrome", command)
        self.assertEqual(
            command[command.index("--permission-mode") + 1],
            "plan",
        )
        self.assertNotIn("--fallback-model", command)

    def test_fable_5_fallback_is_explicit_and_pins_exact_model(self):
        config = RUNNER.load_council_config()
        self.assertNotIn("claude-fable-5-fallback", config["priority"])
        for profile in config["profiles"].values():
            self.assertNotIn(
                "claude-fable-5-fallback", profile.get("participants", [])
            )
            self.assertNotIn(
                "claude-fable-5-fallback", profile.get("priority", [])
            )
        command = RUNNER.command_for(
            "claude-fable-5-fallback", Path("/tmp/unused")
        )
        self.assertEqual(
            command[command.index("--model") + 1],
            "claude-fable-5",
        )
        self.assertEqual(command[command.index("--effort") + 1], "max")
        self.assertNotIn("--max-budget-usd", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--no-chrome", command)
        self.assertNotIn("--fallback-model", command)

    def test_all_claude_manifest_commands_replace_inline_schema_with_path(self):
        for participant in ("claude", "claude-fable-5-fallback"):
            with self.subTest(participant=participant):
                command = RUNNER.command_for(participant, Path("/tmp/unused"))
                visible = RUNNER.display_command(participant, command)
                self.assertEqual(
                    visible[visible.index("--json-schema") + 1],
                    str(RUNNER.SCHEMA_PATH),
                )

    def test_default_fable_5_1_requires_exact_model_attestation(self):
        for label, model_usage, expected_ok in (
            ("missing", None, False),
            ("wrong", {"claude-fable-5": {}}, False),
            ("missing-canonical", {"claude-fable-5-1": {}}, False),
            (
                "wrong-canonical",
                {
                    "claude-fable-5-1": {
                        "canonicalModel": "claude-fable-5"
                    }
                },
                False,
            ),
            (
                "exact",
                {
                    "claude-fable-5-1": {
                        "canonicalModel": "claude-fable-5-1"
                    }
                },
                True,
            ),
        ):
            payload = {
                "is_error": False,
                "structured_output": sample_position(),
                "usage": {"output_tokens": 10},
            }
            if model_usage is not None:
                payload["modelUsage"] = model_usage
            command = [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write({json.dumps(json.dumps(payload))})",
            ]
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as directory,
            ):
                output_dir = Path(directory) / "out"
                output_dir.mkdir()
                with mock.patch.object(RUNNER, "command_for", return_value=command):
                    result = RUNNER.run_model(
                        "claude",
                        "test prompt",
                        output_dir,
                        3,
                        0,
                        Path(directory),
                        False,
                    )
                self.assertEqual(result.status == "ok", expected_ok)
                self.assertEqual(result.parse_ok, expected_ok)
                if expected_ok:
                    self.assertEqual(result.observed_model, "claude-fable-5-1")
                else:
                    self.assertEqual(result.failure_category, "model_attestation")
                    self.assertIsNone(result.output_file)
                    self.assertIsNone(result.structured_file)

    def test_clean_env_isolates_provider_credentials(self):
        source = {
            "PATH": os.defpath,
            "HOME": "/tmp/test-home",
            "ANTHROPIC_API_KEY": "anthropic-api-key",
            "ANTHROPIC_AUTH_TOKEN": "claude-gateway-token",
            "ANTHROPIC_BASE_URL": "https://claude-gateway.invalid",
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_BASE_URL": "https://openai-gateway.invalid",
            "CODEX_API_KEY": "codex-key",
            "GEMINI_API_KEY": "gemini-key",
            "GOOGLE_API_KEY": "google-key",
            "GOOGLE_GENERATIVE_AI_API_KEY": "google-generative-key",
            "AWS_SECRET_ACCESS_KEY": "must-not-leak",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "12000",
            "MAX_THINKING_TOKENS": "9000",
            "CLAUDECODE": "1",
            "CODEX_THREAD_ID": "thread-secret",
            "GEMINI_SESSION_ID": "session-secret",
        }
        with mock.patch.dict(os.environ, source, clear=True):
            claude_env = RUNNER.clean_env("claude")
            codex_env = RUNNER.clean_env("codex")
            gemini_env = RUNNER.clean_env("gemini")

        self.assertNotIn("ANTHROPIC_API_KEY", claude_env)
        self.assertEqual(
            claude_env["ANTHROPIC_AUTH_TOKEN"], "claude-gateway-token"
        )
        self.assertEqual(
            claude_env["ANTHROPIC_BASE_URL"], "https://claude-gateway.invalid"
        )
        self.assertEqual(claude_env["CLAUDE_CODE_MAX_RETRIES"], "0")
        self.assertEqual(claude_env["MAX_STRUCTURED_OUTPUT_RETRIES"], "0")
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", claude_env)
        self.assertNotIn("MAX_THINKING_TOKENS", claude_env)
        self.assertEqual(codex_env["OPENAI_API_KEY"], "openai-key")
        self.assertEqual(codex_env["CODEX_API_KEY"], "codex-key")
        self.assertEqual(gemini_env["GEMINI_API_KEY"], "gemini-key")
        self.assertEqual(gemini_env["GOOGLE_API_KEY"], "google-key")
        self.assertEqual(
            gemini_env["GOOGLE_GENERATIVE_AI_API_KEY"], "google-generative-key"
        )

        for env, forbidden in (
            (
                claude_env,
                (
                    "OPENAI_API_KEY",
                    "CODEX_API_KEY",
                    "GEMINI_API_KEY",
                    "GOOGLE_API_KEY",
                    "GOOGLE_GENERATIVE_AI_API_KEY",
                ),
            ),
            (
                codex_env,
                (
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                    "GEMINI_API_KEY",
                    "GOOGLE_API_KEY",
                    "GOOGLE_GENERATIVE_AI_API_KEY",
                ),
            ),
            (
                gemini_env,
                (
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                    "OPENAI_API_KEY",
                    "CODEX_API_KEY",
                ),
            ),
        ):
            with self.subTest(forbidden=forbidden):
                for key in (
                    *forbidden,
                    "AWS_SECRET_ACCESS_KEY",
                    "CLAUDECODE",
                    "CODEX_THREAD_ID",
                    "GEMINI_SESSION_ID",
                ):
                    self.assertNotIn(key, env)

    def test_clean_env_keeps_claude_credentials_with_their_endpoint(self):
        for incomplete in (
            {"ANTHROPIC_AUTH_TOKEN": "token-only"},
            {"ANTHROPIC_BASE_URL": "https://endpoint-only.invalid"},
        ):
            with self.subTest(incomplete=incomplete), mock.patch.dict(
                os.environ,
                {"PATH": os.defpath, **incomplete},
                clear=True,
            ):
                env = RUNNER.clean_env("claude")
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
                self.assertNotIn("ANTHROPIC_BASE_URL", env)

        with mock.patch.dict(
            os.environ,
            {"PATH": os.defpath, "ANTHROPIC_API_KEY": "official-key"},
            clear=True,
        ):
            direct_env = RUNNER.clean_env("claude")
        self.assertEqual(direct_env["ANTHROPIC_API_KEY"], "official-key")
        self.assertNotIn("ANTHROPIC_BASE_URL", direct_env)

        with mock.patch.dict(
            os.environ,
            {
                "PATH": os.defpath,
                "ANTHROPIC_API_KEY": "gateway-key",
                "ANTHROPIC_BASE_URL": "https://gateway.invalid",
            },
            clear=True,
        ):
            api_gateway_env = RUNNER.clean_env("claude")
        self.assertEqual(api_gateway_env["ANTHROPIC_API_KEY"], "gateway-key")
        self.assertEqual(
            api_gateway_env["ANTHROPIC_BASE_URL"], "https://gateway.invalid"
        )

    def test_provider_env_file_is_explicit_private_and_route_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "DASHSCOPE_API_KEY='dashscope-file-key'",
                        'ZHIPU_API_KEY="zhipu-file-key"',
                        "DEEPSEEK_API_KEY=deepseek-file-key",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            provider_env = RUNNER.load_provider_env_file(env_file)

            with mock.patch.dict(os.environ, {"PATH": os.defpath}, clear=True):
                kimi_env = RUNNER.clean_env("kimi", provider_env)
                glm_env = RUNNER.clean_env("glm", provider_env)
                deepseek_env = RUNNER.clean_env("deepseek", provider_env)
                claude_env = RUNNER.clean_env("claude", provider_env)

        self.assertEqual(kimi_env["DASHSCOPE_API_KEY"], "dashscope-file-key")
        self.assertEqual(glm_env["ZHIPU_API_KEY"], "zhipu-file-key")
        self.assertEqual(deepseek_env["DEEPSEEK_API_KEY"], "deepseek-file-key")
        self.assertNotIn("ZHIPUAI_API_KEY", glm_env)
        for key in provider_env:
            self.assertNotIn(key, claude_env)
        self.assertNotIn("DASHSCOPE_API_KEY", glm_env)
        self.assertNotIn("DEEPSEEK_API_KEY", kimi_env)

    def test_provider_env_file_rejects_unsafe_permissions_syntax_and_conflicts(self):
        cases = {
            "shell-syntax": "DASHSCOPE_API_KEY=$(id) extra\n",
            "unsupported-key": "ANTHROPIC_API_KEY=must-not-load\n",
            "conflicting-alias": (
                "ZHIPU_API_KEY=first\nZHIPUAI_API_KEY=second\n"
            ),
        }
        for label, content in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                env_file = Path(directory) / ".env"
                env_file.write_text(content, encoding="utf-8")
                env_file.chmod(0o600)
                with self.assertRaises(ValueError):
                    RUNNER.load_provider_env_file(env_file)

        if os.name != "nt":
            with tempfile.TemporaryDirectory() as directory:
                env_file = Path(directory) / ".env"
                env_file.write_text("DASHSCOPE_API_KEY=secret\n", encoding="utf-8")
                env_file.chmod(0o644)
                with self.assertRaisesRegex(ValueError, "0600"):
                    RUNNER.load_provider_env_file(env_file)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "provider.env"
                target.write_text("DASHSCOPE_API_KEY=secret\n", encoding="utf-8")
                target.chmod(0o600)
                link = root / ".env"
                link.symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    RUNNER.load_provider_env_file(link)

    def test_invalid_provider_env_file_fails_before_any_participant_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("harmless prompt", encoding="utf-8")
            env_file = root / ".env"
            env_file.write_text("DASHSCOPE_API_KEY=secret\n", encoding="utf-8")
            env_file.chmod(0o644)
            with (
                mock.patch.object(RUNNER, "run_model") as run_model,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--participants",
                        "kimi",
                        "--prompt-file",
                        str(prompt),
                        "--output-dir",
                        str(root / "out"),
                        "--env-file",
                        str(env_file),
                    ]
                )

        self.assertEqual(exit_code, 2)
        run_model.assert_not_called()

    def test_parse_claude_structured_output(self):
        payload = {
            "is_error": False,
            "structured_output": sample_position(),
            "usage": {"output_tokens": 10},
        }
        data, usage, _ = RUNNER.parse_model_output(
            "claude", json.dumps(payload), Path("/tmp/unused")
        )
        RUNNER.validate_structured(data)
        self.assertEqual(data["recommendation"], "Use the safer option")
        self.assertEqual(usage["usage"]["output_tokens"], 10)

    def test_parse_gemini_error(self):
        payload = {"response": "", "error": {"message": "quota exceeded"}}
        with self.assertRaisesRegex(ValueError, "Gemini returned an error"):
            RUNNER.parse_model_output(
                "gemini", json.dumps(payload), Path("/tmp/unused")
            )

    def test_parse_claude_error_envelope(self):
        payload = {
            "is_error": True,
            "result": "API Error: 403 Insufficient account balance",
        }
        with self.assertRaisesRegex(ValueError, "Insufficient account balance"):
            RUNNER.parse_model_output(
                "claude", json.dumps(payload), Path("/tmp/unused")
            )

    def test_parse_claude_fenced_result(self):
        payload = {
            "is_error": False,
            "result": f"```json\n{json.dumps(sample_position())}\n```",
            "usage": {"output_tokens": 10},
        }
        data, _, _ = RUNNER.parse_model_output(
            "claude", json.dumps(payload), Path("/tmp/unused")
        )
        RUNNER.validate_structured(data)
        self.assertEqual(data["recommendation"], "Use the safer option")

    def test_parse_codex_usage_from_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_file = Path(directory) / "codex-output.json"
            raw_file.write_text(json.dumps(sample_position()), encoding="utf-8")
            jsonl = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "test"}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 20,
                                "output_tokens": 30,
                                "reasoning_output_tokens": 5,
                            },
                        }
                    ),
                ]
            )
            data, usage, _ = RUNNER.parse_model_output(
                "codex", jsonl, raw_file
            )
        RUNNER.validate_structured(data)
        self.assertEqual(usage["usage"]["input_tokens"], 100)
        self.assertEqual(usage["usage"]["reasoning_output_tokens"], 5)
        self.assertFalse(usage["cost_available"])
        self.assertIsNone(usage["cost_usd"])
        self.assertEqual(usage["source"], "codex_jsonl")

    def test_native_usage_keeps_only_bounded_known_numeric_metrics(self):
        secret = "CREDENTIAL-MUST-NOT-ENTER-USAGE"
        claude_payload = {
            "is_error": False,
            "structured_output": sample_position(),
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "credential": secret,
                "nested": {"token": secret},
                "cache_read_input_tokens": float("inf"),
            },
            "modelUsage": {secret: {"outputTokens": 20}},
            "total_cost_usd": -1,
        }
        _, claude_usage, _ = RUNNER.parse_model_output(
            "claude", json.dumps(claude_payload), Path("/tmp/unused")
        )
        self.assertEqual(
            claude_usage["usage"], {"input_tokens": 10, "output_tokens": 20}
        )
        self.assertIsNone(claude_usage["cost_usd"])

        with tempfile.TemporaryDirectory() as directory:
            raw_file = Path(directory) / "codex.json"
            raw_file.write_text(json.dumps(sample_position()), encoding="utf-8")
            codex_jsonl = json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 12,
                        "credential": secret,
                        "nested": {"credential": secret},
                        "total_tokens": RUNNER.MAX_USAGE_VALUE + 1,
                    },
                }
            )
            _, codex_usage, _ = RUNNER.parse_model_output(
                "codex", codex_jsonl, raw_file
            )
        self.assertEqual(
            codex_usage["usage"], {"input_tokens": 11, "output_tokens": 12}
        )

        gemini_payload = {
            "response": json.dumps(sample_position()),
            "stats": {
                "models": {
                    secret: {
                        "api": {
                            "totalRequests": 1,
                            "totalErrors": 0,
                            "totalLatencyMs": 50,
                            "credential": secret,
                        },
                        "tokens": {
                            "input": 13,
                            "candidates": 14,
                            "total": 27,
                            "unknown": secret,
                        },
                        "roles": {"main": {"credential": secret}},
                    }
                },
                "tools": {"byName": {secret: 1}},
            },
        }
        _, gemini_usage, _ = RUNNER.parse_model_output(
            "gemini", json.dumps(gemini_payload), Path("/tmp/unused")
        )
        self.assertEqual(gemini_usage["model_count"], 1)
        self.assertEqual(gemini_usage["usage"]["input"], 13)
        self.assertEqual(gemini_usage["usage"]["candidates"], 14)

        for usage in (claude_usage, codex_usage, gemini_usage):
            serialized = json.dumps(usage, ensure_ascii=False, allow_nan=False)
            self.assertNotIn(secret, serialized)

    def test_result_and_manifest_recursively_redact_provider_credentials(self):
        secret = "PROVIDER-CREDENTIAL-IN-TELEMETRY"
        position = sample_position()
        position["analysis"] = f"provider echoed {secret}"
        payload = {
            "is_error": False,
            "structured_output": position,
            "usage": {"output_tokens": 10, "unknown": {"credential": secret}},
            "modelUsage": {
                "claude-fable-5-1": {
                    "outputTokens": 10,
                    "canonicalModel": "claude-fable-5-1",
                    "credential": secret,
                }
            },
        }
        command = [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({json.dumps(payload)!r})",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "result"
            output_dir.mkdir()
            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": os.defpath, "ANTHROPIC_API_KEY": secret},
                    clear=True,
                ),
                mock.patch.object(RUNNER, "command_for", return_value=command),
            ):
                result = RUNNER.run_model(
                    "claude", "safe prompt", output_dir, 3, 0, root, False
                )
            result_text = json.dumps(
                __import__("dataclasses").asdict(result), ensure_ascii=False
            )
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in output_dir.iterdir()
                if path.is_file()
            )

            prompt_file = root / "prompt.md"
            manifest_dir = root / "manifest"
            prompt_file.write_text("safe prompt", encoding="utf-8")
            malicious = fake_result("claude", ok=True)
            malicious.usage = {"nested": [{"credential": secret}]}
            malicious.stderr = f"echoed {secret}"
            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": os.defpath, "ANTHROPIC_API_KEY": secret},
                    clear=True,
                ),
                mock.patch.object(RUNNER, "run_model", return_value=malicious),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--models",
                        "claude",
                        "--prompt-file",
                        str(prompt_file),
                        "--output-dir",
                        str(manifest_dir),
                    ]
                )
            manifest_text = (manifest_dir / "manifest.json").read_text(
                encoding="utf-8"
            )

        self.assertTrue(result.parse_ok)
        self.assertEqual(exit_code, 0)
        for artifact in (result_text, persisted, manifest_text):
            self.assertNotIn(secret, artifact)
        self.assertIn("<CREDENTIAL REDACTED>", result_text)
        self.assertIn("<CREDENTIAL REDACTED>", manifest_text)

    def test_recursive_schema_validation_rejects_malformed_positions(self):
        malformed: list[tuple[str, dict[str, object]]] = []

        root_extra = sample_position()
        root_extra["tool_call"] = {"name": "exfiltrate"}
        malformed.append(("root additional property", root_extra))

        nested_extra = sample_position()
        nested_extra["evidence"][0]["payload"] = "hidden"
        malformed.append(("nested additional property", nested_extra))

        nested_missing = sample_position()
        del nested_missing["evidence"][0]["verification"]
        malformed.append(("nested missing field", nested_missing))

        bad_assumption = sample_position()
        bad_assumption["assumptions"] = [7]
        malformed.append(("wrong array item type", bad_assumption))

        boolean_confidence = sample_position()
        boolean_confidence["confidence"] = True
        malformed.append(("boolean is not a number", boolean_confidence))

        nonfinite_confidence = sample_position()
        nonfinite_confidence["confidence"] = float("nan")
        malformed.append(("non-finite number", nonfinite_confidence))

        RUNNER.validate_structured(sample_position())
        for label, position in malformed:
            with self.subTest(label=label), self.assertRaises(ValueError):
                RUNNER.validate_structured(position)

    @unittest.skipIf(os.name == "nt", "POSIX process-group test")
    def test_terminate_process_group(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess,time;"
                    "subprocess.Popen(['sleep','30']);"
                    "time.sleep(30)"
                ),
            ],
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)
        time.sleep(0.2)
        RUNNER.terminate_process_tree(process)
        process.wait(timeout=2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (PermissionError, ProcessLookupError):
                break
            time.sleep(0.05)
        else:
            self.fail(f"Process group {pgid} still exists after termination")

    def test_silence_timeout_terminates_early_and_removes_stale_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            for suffix in ("md", "json", "raw.txt"):
                (output_dir / f"claude.{suffix}").write_text(
                    "stale result", encoding="utf-8"
                )
            command = [
                sys.executable,
                "-c",
                "import time; time.sleep(5)",
            ]
            started = time.monotonic()
            with mock.patch.object(RUNNER, "command_for", return_value=command):
                result = RUNNER.run_model(
                    "claude",
                    "test prompt",
                    output_dir,
                    3,
                    0.2,
                    Path.cwd(),
                    False,
                )

            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(result.status, "silent-timeout")
            self.assertEqual(result.termination_reason, "silence_timeout")
            self.assertEqual(result.activity["stdout_bytes"], 0)
            self.assertEqual(result.activity["stderr_bytes"], 0)
            self.assertEqual(result.activity["activity_file_bytes"], 0)
            self.assertEqual(result.failure_category, "zero_output_timeout")
            self.assertIn("minimal probe", result.diagnostic_hint)
            self.assertIsNone(result.raw_file)
            self.assertFalse((output_dir / "claude.md").exists())
            self.assertFalse((output_dir / "claude.json").exists())
            self.assertFalse((output_dir / "claude.raw.txt").exists())

    def test_connection_refused_is_diagnosed_as_an_environment_failure(self):
        command = [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stderr.write('ConnectionRefused: network access denied\\n');"
                "sys.exit(1)"
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with mock.patch.object(RUNNER, "command_for", return_value=command):
                result = RUNNER.run_model(
                    "claude",
                    "test prompt",
                    output_dir,
                    3,
                    0,
                    Path.cwd(),
                    False,
                )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.failure_category, "network_environment")
        self.assertIn("restricted sandbox", result.diagnostic_hint)
        self.assertGreater(result.activity["stderr_bytes"], 0)

    def test_missing_cli_also_removes_stale_participant_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            for suffix in ("md", "json", "raw.txt"):
                (output_dir / f"claude.{suffix}").write_text(
                    "stale result", encoding="utf-8"
                )
            command = ["definitely-not-a-real-model-cli"]
            with mock.patch.object(RUNNER, "command_for", return_value=command):
                result = RUNNER.run_model(
                    "claude",
                    "test prompt",
                    output_dir,
                    3,
                    0,
                    Path.cwd(),
                    False,
                )

            self.assertEqual(result.status, "missing")
            self.assertFalse((output_dir / "claude.md").exists())
            self.assertFalse((output_dir / "claude.json").exists())
            self.assertFalse((output_dir / "claude.raw.txt").exists())

    def test_invalid_structured_output_keeps_only_nonempty_raw(self):
        invalid = sample_position()
        invalid["evidence"][0]["unexpected"] = "must be rejected"
        payload = {
            "is_error": False,
            "structured_output": invalid,
            "usage": {"output_tokens": 10},
        }
        command = [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({json.dumps(payload)!r})",
        ]

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with mock.patch.object(RUNNER, "command_for", return_value=command):
                result = RUNNER.run_model(
                    "claude",
                    "test prompt",
                    output_dir,
                    3,
                    0,
                    Path.cwd(),
                    False,
                )

            self.assertFalse(result.parse_ok)
            self.assertEqual(result.failure_category, "invalid_output")
            self.assertIsNotNone(result.raw_file)
            self.assertGreater(Path(result.raw_file).stat().st_size, 0)
            self.assertIsNone(result.structured_file)
            self.assertIsNone(result.output_file)
            self.assertFalse((output_dir / "claude.json").exists())
            self.assertFalse((output_dir / "claude.md").exists())

    def test_output_limit_never_accepts_a_truncated_valid_prefix(self):
        payload = {
            "is_error": False,
            "structured_output": sample_position(),
            "usage": {"output_tokens": 10},
            "modelUsage": {
                "claude-fable-5-1": {
                    "outputTokens": 10,
                    "canonicalModel": "claude-fable-5-1",
                }
            },
        }
        serialized = json.dumps(payload)
        byte_limit = len(serialized.encode("utf-8")) + 64
        script = (
            "import sys,time;"
            f"sys.stdout.write({serialized!r} + (' ' * 10000));"
            "sys.stdout.flush();"
            "time.sleep(5)"
        )
        command = [sys.executable, "-c", script]

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with mock.patch.object(RUNNER, "command_for", return_value=command):
                result = RUNNER.run_model(
                    "claude",
                    "test prompt",
                    output_dir,
                    3,
                    0,
                    Path.cwd(),
                    False,
                    byte_limit,
                )

            self.assertEqual(result.status, "output-limit")
            self.assertEqual(result.termination_reason, "output_limit")
            self.assertEqual(result.failure_category, "output_limit")
            self.assertFalse(result.parse_ok)
            self.assertGreater(result.activity["stdout_bytes"], byte_limit)
            self.assertEqual(result.activity["max_output_bytes"], byte_limit)
            self.assertIsNotNone(result.raw_file)
            self.assertGreater(Path(result.raw_file).stat().st_size, 0)
            self.assertLessEqual(Path(result.raw_file).stat().st_size, byte_limit)
            self.assertIsNone(result.structured_file)
            self.assertIsNone(result.output_file)
            self.assertIn("parsing truncated output is disabled", result.stderr)

    def test_participant_exceptions_still_write_manifest(self):
        failures = (
            ("process launch", "start_process", OSError("popen failed")),
            (
                "stale cleanup",
                "remove_generated_artifacts",
                PermissionError("cleanup denied"),
            ),
        )
        for label, target, error in failures:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prompt_file = root / "prompt.md"
                output_dir = root / "output"
                prompt_file.write_text("test prompt", encoding="utf-8")
                command = [sys.executable, "-c", "raise SystemExit(0)"]
                with (
                    mock.patch.object(RUNNER, "command_for", return_value=command),
                    mock.patch.object(RUNNER, target, side_effect=error),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    exit_code = RUNNER.main(
                        [
                            "--host",
                            "codex",
                            "--models",
                            "claude",
                            "--prompt-file",
                            str(prompt_file),
                            "--output-dir",
                            str(output_dir),
                            "--timeout",
                            "3",
                        ]
                    )

                self.assertEqual(exit_code, 1)
                manifest_file = output_dir / "manifest.json"
                self.assertTrue(manifest_file.is_file())
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                self.assertEqual(manifest["host"], "codex")
                self.assertEqual(manifest["requested_models"], ["claude"])
                self.assertEqual(len(manifest["results"]), 1)
                result = manifest["results"][0]
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["failure_category"], "runner_exception")
                self.assertFalse(result["parse_ok"])
                self.assertIn(type(error).__name__, result["stderr"])

    def test_runner_exception_manifest_redacts_prompt_and_provider_secret(self):
        prompt = "RUNNER-SECRET 第一行\n第二行"
        provider_secret = "PROVIDER-SECRET-NEVER-PERSIST"
        escaped_prompt = json.dumps(prompt, ensure_ascii=True)[1:-1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_file = root / "prompt.md"
            output_dir = root / "output"
            prompt_file.write_text(prompt, encoding="utf-8")
            error = RuntimeError(f"{prompt} / {escaped_prompt} / {provider_secret}")
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": os.defpath,
                        "HOME": str(root / "home"),
                        "ANTHROPIC_API_KEY": provider_secret,
                    },
                    clear=True,
                ),
                mock.patch.object(RUNNER, "run_model", side_effect=error),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--models",
                        "claude",
                        "--prompt-file",
                        str(prompt_file),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            manifest_text = (output_dir / "manifest.json").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        for forbidden in (prompt, escaped_prompt, provider_secret):
            self.assertNotIn(forbidden, manifest_text)
        self.assertIn("RuntimeError", manifest_text)
        self.assertIn("<PROMPT REDACTED>", manifest_text)
        self.assertIn("<CREDENTIAL REDACTED>", manifest_text)

    def test_valid_output_survives_silence_termination_as_partial_result(self):
        payload = {
            "is_error": False,
            "structured_output": sample_position(),
            "usage": {"output_tokens": 10},
            "modelUsage": {
                "claude-fable-5-1": {
                    "outputTokens": 10,
                    "canonicalModel": "claude-fable-5-1",
                }
            },
        }
        serialized = json.dumps(payload)
        script = (
            "import sys,time;"
            f"sys.stdout.write({serialized!r} + '\\n');"
            "sys.stdout.flush();"
            "time.sleep(5)"
        )
        command = [sys.executable, "-c", script]

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            with mock.patch.object(RUNNER, "command_for", return_value=command):
                result = RUNNER.run_model(
                    "claude",
                    "test prompt",
                    output_dir,
                    3,
                    0.2,
                    Path.cwd(),
                    False,
                )

            self.assertEqual(result.status, "partial")
            self.assertTrue(result.parse_ok)
            self.assertEqual(result.termination_reason, "silence_timeout")
            self.assertGreater(result.activity["stdout_bytes"], 0)
            self.assertIsNotNone(result.output_file)
            self.assertIsNotNone(result.structured_file)
            self.assertIsNotNone(result.raw_file)
            self.assertIn(
                "Use the safer option",
                Path(result.output_file).read_text(encoding="utf-8"),
            )

    def test_parser_defaults_to_long_hard_timeout_without_silence_cutoff(self):
        args = RUNNER.build_parser().parse_args(
            [
                "--host",
                "codex",
                "--prompt-file",
                "/tmp/prompt.md",
                "--output-dir",
                "/tmp/output",
            ]
        )
        self.assertEqual(args.timeout, 900)
        self.assertEqual(args.silence_timeout, 0)
        self.assertEqual(args.max_output_bytes, 8 * 1024 * 1024)

    def test_nonfinite_timeouts_are_rejected_before_any_participant_call(self):
        cases = (
            ("--timeout", "nan"),
            ("--timeout", "inf"),
            ("--silence-timeout", "nan"),
            ("--silence-timeout", "inf"),
        )
        for option, value in cases:
            with self.subTest(option=option, value=value), mock.patch.object(
                RUNNER, "run_model"
            ) as participant_call, contextlib.redirect_stderr(io.StringIO()):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--models",
                        "claude",
                        "--prompt-file",
                        "/tmp/not-read-for-invalid-timeout",
                        "--output-dir",
                        "/tmp/not-created-for-invalid-timeout",
                        option,
                        value,
                    ]
                )
            self.assertEqual(exit_code, 2)
            participant_call.assert_not_called()

    def test_auxiliary_output_is_bounded(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 10000); sys.stdout.flush()",
        ]
        completed_result = RUNNER.run_auxiliary(
            command,
            Path.cwd(),
            {"PATH": os.defpath},
            timeout=3,
            max_output_bytes=128,
        )
        self.assertNotEqual(completed_result.returncode, 0)
        self.assertLessEqual(len(completed_result.stdout.encode("utf-8")), 128)
        self.assertIn("byte limit", completed_result.stderr)

    def test_profiles_select_expected_provider_distinct_rosters_for_all_hosts(self):
        expected = {
            "codex": {
                "quick": ["kimi"],
                "general": ["claude", "glm"],
                "critical": ["claude", "glm", "deepseek-flash", "kimi"],
            },
            "claude": {
                "quick": ["codex"],
                "general": ["codex-astra", "glm"],
                "critical": ["codex-astra", "glm", "deepseek-flash", "kimi"],
            },
            "gemini": {
                "quick": ["codex"],
                "general": ["codex-astra", "claude"],
                "critical": ["codex-astra", "claude", "glm", "deepseek-flash"],
            },
        }
        for host, profiles in expected.items():
            for profile, roster in profiles.items():
                with self.subTest(host=host, profile=profile):
                    self.assertEqual(
                        RUNNER.participants_for_profile(profile, host), roster
                    )
                    families = {
                        RUNNER.participant_spec(host).provider_family,
                        *(
                            RUNNER.participant_spec(item).provider_family
                            for item in roster
                        ),
                    }
                    self.assertEqual(len(families), len(roster) + 1)

    def test_implicit_default_preserves_legacy_roster_and_selection_source(self):
        expected = {
            "claude": ["codex", "gemini"],
            "codex": ["claude", "gemini"],
            "gemini": ["claude", "codex"],
        }
        for host, roster in expected.items():
            with self.subTest(host=host):
                self.assertEqual(
                    RUNNER.participants_for_profile("legacy", host), roster
                )
                selected, profile, source = RUNNER.resolve_selection(
                    host, profile=None, participants=None, models=None
                )
                self.assertEqual(selected, roster)
                self.assertEqual(profile, "legacy")
                self.assertEqual(source, "implicit_legacy")
                explicit, explicit_profile, explicit_source = RUNNER.resolve_selection(
                    host,
                    profile="legacy",
                    participants=None,
                    models=None,
                )
                self.assertEqual(explicit, roster)
                self.assertEqual(explicit_profile, "legacy")
                self.assertEqual(explicit_source, "profile:legacy")

    def test_participant_ids_are_slug_safe_deduplicated_and_family_distinct(self):
        for malicious in ("../kimi", "kimi/../../x", "kimi.md", "/tmp/kimi"):
            with self.subTest(malicious=malicious), self.assertRaisesRegex(
                ValueError, "Invalid participant id"
            ):
                RUNNER.parse_participants(malicious, "codex")

        self.assertEqual(
            RUNNER.parse_participants("kimi,kimi,glm", "codex"),
            ["kimi", "glm"],
        )
        with self.assertRaisesRegex(ValueError, "distinct provider families"):
            RUNNER.parse_participants("gemini,gemini-flash", "codex")
        with self.assertRaisesRegex(ValueError, "current host provider family"):
            RUNNER.parse_participants("gemini-flash", "gemini")
        with self.assertRaisesRegex(ValueError, "distinct provider families"):
            RUNNER.parse_participants("codex,codex-astra", "claude")
        for participant in ("codex", "codex-astra"):
            with self.subTest(participant=participant), self.assertRaisesRegex(
                ValueError, "current host provider family"
            ):
                RUNNER.parse_participants(participant, "codex")

    def test_invalid_profile_priority_does_not_fall_back_silently(self):
        for invalid_priority in (None, "codex-astra", [], ["../codex-astra"]):
            config = RUNNER.load_council_config()
            config["profiles"]["general"]["priority"] = invalid_priority
            with self.subTest(priority=invalid_priority), mock.patch.object(
                RUNNER, "load_council_config", return_value=config
            ), self.assertRaises(ValueError):
                RUNNER.participants_for_profile("general", "claude")

    def test_explicit_selection_modes_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            RUNNER.resolve_selection(
                "codex",
                profile="general",
                participants="kimi,glm",
                models=None,
            )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            RUNNER.build_parser().parse_args(
                [
                    "--host",
                    "codex",
                    "--profile",
                    "general",
                    "--models",
                    "claude",
                    "--prompt-file",
                    "/tmp/prompt",
                    "--output-dir",
                    "/tmp/output",
                ]
            )

    def test_fixed_model_specs_and_native_commands_request_highest_effort(self):
        expected = {
            "codex-astra": ("gpt-6-astra", "max", "codex"),
            "codex": ("gpt-5.6-sol", "max", "codex"),
            "claude": ("claude-fable-5-1", "max", "claude"),
            "claude-fable-5-fallback": (
                "claude-fable-5",
                "max",
                "claude",
            ),
            "kimi": ("alibaba-cn/kimi/kimi-k3", "max", "opencode"),
            "glm": ("zhipuai/glm-5.3", "max", "opencode"),
            "qwen": ("alibaba-cn/qwen3.8-max", "xhigh", "opencode"),
            "gemini-flash": ("google/gemini-3.7-flash", "high", "opencode"),
            "deepseek": ("deepseek/deepseek-v4-pro", "max", "opencode"),
            "deepseek-flash": ("deepseek-flash", "max", "deepseek-http"),
            "gemini": ("gemini-3.1-pro-preview", "high", "gemini"),
        }
        for participant, (model, effort, adapter) in expected.items():
            with self.subTest(participant=participant):
                spec = RUNNER.participant_spec(participant)
                self.assertEqual(spec.model, model)
                self.assertEqual(spec.reasoning_effort, effort)
                self.assertEqual(spec.adapter, adapter)
                self.assertTrue(spec.role)
                self.assertTrue(spec.provider_family)
                self.assertTrue(spec.transport_domain)

        codex = RUNNER.command_for("codex", Path("/tmp/codex.raw.txt"))
        self.assertEqual(codex[codex.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="max"', codex)
        self.assertNotIn("sh", Path(codex[0]).name.lower())
        astra = RUNNER.command_for("codex-astra", Path("/tmp/astra.raw.txt"))
        self.assertEqual(astra[astra.index("--model") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="max"', astra)
        for safe_flag in (
            "--ephemeral", "--strict-config", "--ignore-user-config",
            "--ignore-rules", "--output-schema",
        ):
            self.assertIn(safe_flag, astra)
        self.assertEqual(astra[astra.index("--sandbox") + 1], "read-only")
        claude = RUNNER.command_for("claude", Path("/tmp/claude.raw.txt"))
        for safe_flag in ("--safe-mode", "--strict-mcp-config", "--agents"):
            self.assertIn(safe_flag, claude)
        mcp_config = json.loads(claude[claude.index("--mcp-config") + 1])
        self.assertEqual(mcp_config, {"mcpServers": {}})
        inline_schema = json.loads(claude[claude.index("--json-schema") + 1])
        bundled_schema = json.loads(RUNNER.SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(inline_schema["required"], bundled_schema["required"])

    def test_codex_override_requires_an_absolute_executable(self):
        with mock.patch.dict(
            os.environ,
            {RUNNER.CODEX_BINARY_OVERRIDE_ENV: "codex --dangerous"},
            clear=False,
        ), self.assertRaisesRegex(ValueError, "absolute executable path"):
            RUNNER.adapter_executable("codex")

    def test_opencode_environment_is_route_minimal_and_config_in_memory(self):
        source = {
            "PATH": os.defpath,
            "HOME": "/tmp/test-home",
            "GEMINI_API_KEY": "google-current-route",
            "GOOGLE_GENERATIVE_AI_API_KEY": "google-explicit",
            "DASHSCOPE_API_KEY": "alibaba-current-route",
            "ZHIPUAI_API_KEY": "zhipu-other-route",
            "DEEPSEEK_API_KEY": "deepseek-other-route",
            "ANTHROPIC_API_KEY": "must-not-leak",
            "OPENAI_API_KEY": "must-not-leak",
        }
        with mock.patch.dict(os.environ, source, clear=True):
            google = RUNNER.clean_env("gemini-flash")
            kimi = RUNNER.clean_env("kimi")

        self.assertEqual(
            google["GOOGLE_GENERATIVE_AI_API_KEY"], "google-explicit"
        )
        self.assertNotIn("DASHSCOPE_API_KEY", google)
        self.assertEqual(kimi["DASHSCOPE_API_KEY"], "alibaba-current-route")
        for forbidden in (
            "GEMINI_API_KEY",
            "GOOGLE_GENERATIVE_AI_API_KEY",
            "ZHIPUAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ):
            self.assertNotIn(forbidden, kimi)
        config = json.loads(kimi["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("HOME", kimi)
        self.assertEqual(config["enabled_providers"], ["alibaba-cn"])
        self.assertEqual(set(config["provider"]), {"alibaba-cn"})
        self.assertEqual(config["permission"]["*"], "deny")
        self.assertEqual(
            config["agent"][RUNNER.OPENCODE_AGENT]["mode"], "primary"
        )

    def test_opencode_stored_auth_is_projected_to_only_the_current_route(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            auth_path = home / ".local" / "share" / "opencode" / "auth.json"
            auth_path.parent.mkdir(parents=True)
            auth_path.write_text(
                json.dumps(
                    {
                        "alibaba-cn": {"type": "api", "key": "alibaba-only"},
                        "zhipuai": {"type": "api", "key": "must-not-copy"},
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"PATH": os.defpath, "HOME": str(home)},
                clear=True,
            ):
                env = RUNNER.clean_env("kimi")

        auth = json.loads(env[RUNNER.OPENCODE_AUTH_CONTENT_ENV])
        self.assertEqual(set(auth), {"alibaba-cn"})
        self.assertEqual(auth["alibaba-cn"]["key"], "alibaba-only")
        self.assertNotIn("must-not-copy", env[RUNNER.OPENCODE_AUTH_CONTENT_ENV])
        self.assertNotIn("HOME", env)

    def test_opencode_jsonl_parser_concatenates_text_and_flags_bad_events(self):
        response = json.dumps(sample_position())
        split = len(response) // 2
        valid = opencode_jsonl([response[:split], response[split:]])
        events = RUNNER.parse_opencode_events(valid)
        self.assertEqual(json.loads(events.response_text), sample_position())
        self.assertEqual(events.session_ids, {"ses_test"})
        self.assertFalse(events.tool_events)
        self.assertFalse(events.errors)
        self.assertFalse(events.malformed_lines)
        self.assertFalse(events.protocol_errors)

        optional_sdk_fields = RUNNER.parse_opencode_events(
            opencode_jsonl(
                [response], include_optional_step_fields=True, cost=0
            )
        )
        self.assertFalse(optional_sdk_fields.protocol_errors)
        self.assertEqual(
            optional_sdk_fields.usage["usage"],
            {
                "input": 100,
                "output": 200,
                "reasoning": 50,
                "total": 350,
                "cache_read": 0,
                "cache_write": 0,
            },
        )
        self.assertIsNone(optional_sdk_fields.usage["cost_usd"])
        self.assertFalse(optional_sdk_fields.usage["cost_available"])
        self.assertEqual(
            optional_sdk_fields.usage["cost_status"],
            "unreported_zero_not_free",
        )

        tool = RUNNER.parse_opencode_events(
            valid
            + json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "ses_test",
                    "part": {"type": "tool"},
                }
            )
            + "\n"
        )
        self.assertTrue(tool.tool_events)
        malformed = RUNNER.parse_opencode_events("not-json\n" + valid)
        self.assertEqual(malformed.malformed_lines, [1])
        error = RUNNER.parse_opencode_events(
            valid
            + json.dumps(
                {
                    "type": "error",
                    "sessionID": "ses_test",
                    "error": "provider failed",
                }
            )
            + "\n"
        )
        self.assertEqual(error.errors, ["provider-error-event"])

    def test_opencode_valid_run_is_attested_cleaned_and_never_puts_prompt_in_argv(self):
        secret_prompt = "SECRET-PROMPT-MUST-STAY-ON-STDIN"
        response = json.dumps(sample_position())
        stdout = opencode_jsonl([response[:20], response[20:]])
        capture = process_capture(stdout)
        observed: dict[str, object] = {}

        def fake_run_process(command, prompt, cwd, env, *args, **kwargs):
            observed["command"] = command
            observed["prompt"] = prompt
            observed["cwd"] = cwd
            observed["env"] = dict(env)
            observed["root_mode"] = stat.S_IMODE(Path(cwd).stat().st_mode)
            isolated_db = Path(env["OPENCODE_DB"])
            isolated_db.parent.mkdir(parents=True, exist_ok=True)
            isolated_db.write_text("transient prompt session", encoding="utf-8")
            return capture

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "out"
            output_dir.mkdir()
            requested_cwd = root / "requested"
            requested_cwd.mkdir()
            global_home = root / "global-home"
            global_data = global_home / ".local" / "share" / "opencode"
            global_data.mkdir(parents=True)
            (global_data / "auth.json").write_text(
                json.dumps(
                    {
                        "alibaba-cn": {
                            "type": "api",
                            "key": "ROUTE-ONLY-STORED-KEY",
                        },
                        "zhipuai": {"type": "api", "key": "OTHER-ROUTE-KEY"},
                    }
                ),
                encoding="utf-8",
            )
            global_db = global_data / "opencode.db"
            global_db.write_bytes(b"GLOBAL-DB-MUST-NOT-CHANGE")
            command = [sys.executable, "-c", "raise SystemExit(99)"]
            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": os.defpath, "HOME": str(global_home)},
                    clear=True,
                ),
                mock.patch.object(RUNNER, "command_for", return_value=command),
                mock.patch.object(
                    RUNNER, "run_process_monitored", side_effect=fake_run_process
                ),
                mock.patch.object(
                    RUNNER,
                    "run_auxiliary",
                    side_effect=opencode_auxiliary(),
                ),
            ):
                result = RUNNER.run_model(
                    "kimi",
                    secret_prompt,
                    output_dir,
                    3,
                    0,
                    requested_cwd,
                    False,
                )

            self.assertEqual(result.status, "ok")
            self.assertTrue(result.parse_ok)
            self.assertEqual(result.observed_model, "alibaba-cn/kimi/kimi-k3")
            self.assertEqual(result.observed_variant, "max")
            self.assertEqual(result.session_cleanup_status, "deleted-verified")
            self.assertNotIn(secret_prompt, " ".join(observed["command"]))
            self.assertIn(secret_prompt, observed["prompt"])
            self.assertNotEqual(Path(observed["cwd"]), requested_cwd)
            self.assertFalse(Path(observed["cwd"]).exists())
            self.assertEqual(observed["root_mode"], 0o700)
            isolated_env = observed["env"]
            isolation_root = Path(observed["cwd"])
            for name in (
                "HOME",
                "XDG_DATA_HOME",
                "XDG_STATE_HOME",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_RUNTIME_DIR",
                "TMPDIR",
            ):
                self.assertTrue(Path(isolated_env[name]).is_relative_to(isolation_root))
                self.assertFalse(Path(isolated_env[name]).exists())
            self.assertIn("OPENCODE_CONFIG_CONTENT", isolated_env)
            config = json.loads(isolated_env["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(set(config["provider"]), {"alibaba-cn"})
            self.assertEqual(config["enabled_providers"], ["alibaba-cn"])
            auth = json.loads(isolated_env[RUNNER.OPENCODE_AUTH_CONTENT_ENV])
            self.assertEqual(set(auth), {"alibaba-cn"})
            self.assertNotIn("OTHER-ROUTE-KEY", json.dumps(auth))
            self.assertEqual(global_db.read_bytes(), b"GLOBAL-DB-MUST-NOT-CHANGE")
            self.assertFalse((global_data / "opencode.db-wal").exists())
            self.assertNotIn(
                secret_prompt,
                Path(result.raw_file).read_text(encoding="utf-8"),
            )

    def test_opencode_missing_route_auth_fails_closed_before_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "out"
            output_dir.mkdir()
            command = [sys.executable, "-c", "raise SystemExit(99)"]
            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": os.defpath, "HOME": str(root / "empty-home")},
                    clear=True,
                ),
                mock.patch.object(RUNNER, "command_for", return_value=command),
                mock.patch.object(RUNNER, "run_process_monitored") as model_run,
                mock.patch.object(RUNNER, "run_auxiliary") as auxiliary,
            ):
                result = RUNNER.run_model(
                    "kimi",
                    "test prompt",
                    output_dir,
                    3,
                    0,
                    root,
                    False,
                )

        self.assertEqual(result.failure_category, "safety_preflight")
        self.assertEqual(
            result.session_cleanup_status,
            "not-created-isolation-preflight-failed",
        )
        self.assertIn("No isolated credential", result.stderr)
        model_run.assert_not_called()
        auxiliary.assert_not_called()

    def test_opencode_redacts_prompt_escaped_prompt_and_route_secret_everywhere(self):
        core_prompt = "SECRET prompt 第一行\n第二行"
        route_secret = "ROUTE-SECRET-NEVER-PERSIST"
        position = sample_position()
        position["analysis"] = f"echo {core_prompt} and {route_secret}"
        stdout = opencode_jsonl([json.dumps(position, ensure_ascii=False)])
        wrapped_prompt = RUNNER.participant_prompt("kimi", core_prompt)
        stderr = "\n".join(
            (
                core_prompt,
                json.dumps(core_prompt, ensure_ascii=True)[1:-1],
                wrapped_prompt,
                route_secret,
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "out"
            output_dir.mkdir()
            command = [sys.executable, "-c", "raise SystemExit(99)"]
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": os.defpath,
                        "HOME": str(root / "source-home"),
                    },
                    clear=True,
                ),
                mock.patch.object(RUNNER, "command_for", return_value=command),
                mock.patch.object(
                    RUNNER,
                    "run_process_monitored",
                    return_value=process_capture(stdout, stderr=stderr, exit_code=1),
                ),
                mock.patch.object(
                    RUNNER,
                    "run_auxiliary",
                    side_effect=opencode_auxiliary(),
                ),
            ):
                result = RUNNER.run_model(
                    "kimi",
                    core_prompt,
                    output_dir,
                    3,
                    0,
                    root,
                    False,
                    provider_env={"DASHSCOPE_API_KEY": route_secret},
                )
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in output_dir.iterdir()
                if path.is_file()
            ) + result.stderr

        self.assertTrue(result.parse_ok)
        self.assertEqual(result.status, "partial")
        for forbidden in (
            core_prompt,
            json.dumps(core_prompt, ensure_ascii=True)[1:-1],
            wrapped_prompt,
            route_secret,
        ):
            self.assertNotIn(forbidden, persisted)
        self.assertIn("<PROMPT REDACTED>", persisted)
        self.assertIn("<CREDENTIAL REDACTED>", persisted)

    def test_opencode_streamed_prompt_fragments_never_enter_raw_artifact(self):
        core_prompt = "ALPHA-SENSITIVE-BETA"
        position = sample_position()
        position["analysis"] = core_prompt
        response = json.dumps(position)
        split_at = response.index(core_prompt) + len("ALPHA-SENS")
        stdout = opencode_jsonl([response[:split_at], response[split_at:]])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "out"
            output_dir.mkdir()
            command = [sys.executable, "-c", "raise SystemExit(99)"]
            with (
                mock.patch.object(RUNNER, "command_for", return_value=command),
                mock.patch.object(
                    RUNNER,
                    "run_process_monitored",
                    return_value=process_capture(stdout),
                ),
                mock.patch.object(
                    RUNNER,
                    "run_auxiliary",
                    side_effect=opencode_auxiliary(),
                ),
            ):
                result = RUNNER.run_model(
                    "kimi",
                    core_prompt,
                    output_dir,
                    3,
                    0,
                    root,
                    False,
                )
            raw = Path(result.raw_file).read_text(encoding="utf-8")

        self.assertTrue(result.parse_ok)
        self.assertIn("opencode_jsonl_sanitized_summary", raw)
        self.assertIn("<PROMPT REDACTED>", raw)
        for fragment in (core_prompt, "ALPHA-SENS", "ITIVE-BETA"):
            self.assertNotIn(fragment, raw)

    def test_opencode_malformed_or_truncated_raw_omits_response_payload(self):
        secret = "MALFORMED-OR-TRUNCATED-PAYLOAD"
        position = sample_position()
        position["analysis"] = secret
        valid = opencode_jsonl([json.dumps(position)])
        cases = {
            "malformed": process_capture("not-json\n" + valid),
            "output-limit": process_capture(
                valid,
                exit_code=-15,
                termination_reason="output_limit",
            ),
        }
        for label, capture in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output_dir = root / "out"
                output_dir.mkdir()
                command = [sys.executable, "-c", "raise SystemExit(99)"]
                with (
                    mock.patch.object(RUNNER, "command_for", return_value=command),
                    mock.patch.object(
                        RUNNER, "run_process_monitored", return_value=capture
                    ),
                    mock.patch.object(
                        RUNNER,
                        "run_auxiliary",
                        side_effect=opencode_auxiliary(),
                    ),
                ):
                    result = RUNNER.run_model(
                        "kimi",
                        "different core prompt",
                        output_dir,
                        3,
                        0,
                        root,
                        False,
                        max_output_bytes=4096,
                    )
                raw = Path(result.raw_file).read_text(encoding="utf-8")

            self.assertFalse(result.parse_ok)
            self.assertNotIn(secret, raw)
            self.assertIsNone(json.loads(raw)["response_text"])

    def test_opencode_session_cleanup_only_trusts_isolated_store_and_is_capped(self):
        provider_controlled_ids = {
            f"ses_attacker_{index:06d}" for index in range(100_000)
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"OPENCODE_CONFIG_CONTENT": "{}"}
            calls: list[list[str]] = []
            with mock.patch.object(
                RUNNER,
                "run_auxiliary",
                side_effect=opencode_auxiliary(
                    store_session_ids={"ses_from_isolated_db"},
                    calls=calls,
                ),
            ):
                inspection = RUNNER.inspect_and_cleanup_opencode_session(
                    "opencode",
                    provider_controlled_ids,
                    root,
                    env,
                )

        delete_targets = [
            command[3]
            for command in calls
            if command[1:3] == ["session", "delete"]
        ]
        self.assertEqual(delete_targets, ["ses_from_isolated_db"])
        self.assertTrue(provider_controlled_ids.isdisjoint(delete_targets))
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            inspection.cleanup_status,
            "ambiguous-session-ids-cleaned-verified",
        )

        too_many = {f"ses_{index:02d}" for index in range(33)}
        calls = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            RUNNER,
            "run_auxiliary",
            side_effect=opencode_auxiliary(
                store_session_ids=too_many,
                calls=calls,
            ),
        ):
            inspection = RUNNER.inspect_and_cleanup_opencode_session(
                "opencode",
                {"ses_00"},
                Path(directory),
                {},
            )
        self.assertEqual(
            inspection.cleanup_status,
            "isolated-session-cleanup-limit-exceeded-purge-required",
        )
        self.assertFalse(
            any(command[1:3] == ["session", "delete"] for command in calls)
        )

    def test_opencode_session_list_contract_is_strict_but_accepts_blank_empty_store(self):
        for payload in ("", "  \n", "[]", '{"sessions":[]}'):
            with self.subTest(payload=payload):
                self.assertEqual(RUNNER._session_ids_from_list_stdout(payload), set())
        for payload in (
            '["not-an-object"]',
            '[{}]',
            '[{"id":"a","sessionID":"b"}]',
            '[{"id":17}]',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                RUNNER._session_ids_from_list_stdout(payload)

    def test_opencode_unknown_cli_version_fails_before_model_launch(self):
        spec = RUNNER.participant_spec("kimi")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            RUNNER,
            "run_auxiliary",
            side_effect=opencode_auxiliary(version="9.9.9"),
        ), self.assertRaisesRegex(ValueError, "no validated event contract"):
            RUNNER.opencode_preflight(
                spec,
                "opencode",
                Path(directory),
                RUNNER.clean_env("kimi"),
            )

    def test_opencode_fail_closed_rejects_tool_error_malformed_and_identity_failures(self):
        valid = opencode_jsonl([json.dumps(sample_position())])
        cases = {
            "tool": (
                valid
                + json.dumps(
                    {
                        "type": "tool_use",
                        "sessionID": "ses_test",
                        "part": {"type": "tool"},
                    }
                )
                + "\n",
                {},
            ),
            "error": (
                valid
                + json.dumps(
                    {
                        "type": "error",
                        "sessionID": "ses_test",
                        "error": "failed",
                    }
                )
                + "\n",
                {},
            ),
            "malformed": ("not-json\n" + valid, {}),
            "route": (valid, {"observed_model": "fake/wrong"}),
            "variant": (valid, {"observed_variant": "low"}),
            "cleanup": (valid, {"session_remains": True}),
            "multiple-sessions": (
                valid
                + json.dumps(
                    {
                        "type": "text",
                        "sessionID": "ses_other",
                        "part": {"type": "text", "text": ""},
                    }
                )
                + "\n",
                {},
            ),
            "unknown-command-event": (
                valid
                + json.dumps(
                    {
                        "type": "command.completed",
                        "sessionID": "ses_test",
                        "command": "echo unsafe",
                    }
                )
                + "\n",
                {},
            ),
            "unknown-shell-event": (
                valid
                + json.dumps(
                    {
                        "type": "shell",
                        "sessionID": "ses_test",
                        "part": {"type": "shell", "command": "id"},
                    }
                )
                + "\n",
                {},
            ),
            "known-event-extra-field": (
                valid
                + json.dumps(
                    {
                        "type": "text",
                        "timestamp": 99,
                        "sessionID": "ses_test",
                        "part": {
                            "id": "prt_extra",
                            "messageID": "msg_test",
                            "sessionID": "ses_test",
                            "type": "text",
                            "text": "",
                        },
                        "command": "unexpected",
                    }
                )
                + "\n",
                {},
            ),
            "step-part-extra-field": (
                valid
                + json.dumps(
                    {
                        "type": "step_start",
                        "timestamp": 100,
                        "sessionID": "ses_test",
                        "part": {
                            "id": "prt_step_extra",
                            "messageID": "msg_test",
                            "sessionID": "ses_test",
                            "type": "step-start",
                            "unexpected": "must-fail-closed",
                        },
                    }
                )
                + "\n",
                {},
            ),
            "step-type-spelling-mismatch": (
                valid
                + json.dumps(
                    {
                        "type": "step_start",
                        "timestamp": 101,
                        "sessionID": "ses_test",
                        "part": {
                            "id": "prt_step_mismatch",
                            "messageID": "msg_test",
                            "sessionID": "ses_test",
                            "type": "step_start",
                        },
                    }
                )
                + "\n",
                {},
            ),
        }
        for label, (stdout, auxiliary_options) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory) / "out"
                output_dir.mkdir()
                command = [sys.executable, "-c", "raise SystemExit(99)"]
                with (
                    mock.patch.object(RUNNER, "command_for", return_value=command),
                    mock.patch.object(
                        RUNNER,
                        "run_process_monitored",
                        return_value=process_capture(stdout),
                    ),
                    mock.patch.object(
                        RUNNER,
                        "run_auxiliary",
                        side_effect=opencode_auxiliary(**auxiliary_options),
                    ),
                ):
                    result = RUNNER.run_model(
                        "kimi",
                        "test prompt",
                        output_dir,
                        3,
                        0,
                        Path(directory),
                        False,
                    )

                self.assertFalse(result.parse_ok)
                self.assertEqual(result.failure_category, "opencode_protocol")
                self.assertIsNone(result.structured_file)
                self.assertIsNone(result.output_file)
                self.assertIsNotNone(result.raw_file)

    def test_opencode_preflight_rejects_config_or_agent_mismatch_before_launch(self):
        for label, options in (
            ("config", {"effective_effort": "low"}),
            ("config-isolation", {"extra_provider": True}),
            ("agent", {"tool_enabled": True}),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                output_dir = Path(directory) / "out"
                output_dir.mkdir()
                command = [sys.executable, "-c", "raise SystemExit(99)"]
                with (
                    mock.patch.object(RUNNER, "command_for", return_value=command),
                    mock.patch.object(RUNNER, "run_process_monitored") as model_run,
                    mock.patch.object(
                        RUNNER,
                        "run_auxiliary",
                        side_effect=opencode_auxiliary(**options),
                    ),
                ):
                    result = RUNNER.run_model(
                        "kimi",
                        "test prompt",
                        output_dir,
                        3,
                        0,
                        Path(directory),
                        False,
                    )
                self.assertEqual(result.failure_category, "safety_preflight")
                self.assertFalse(result.parse_ok)
                self.assertEqual(
                    result.session_cleanup_status, "not-created-preflight-failed"
                )
                model_run.assert_not_called()

    def test_opencode_timeout_still_exports_sanitizes_deletes_and_verifies(self):
        calls = []
        stdout = opencode_jsonl([json.dumps(sample_position())])
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "out"
            output_dir.mkdir()
            command = [sys.executable, "-c", "raise SystemExit(99)"]
            with (
                mock.patch.object(RUNNER, "command_for", return_value=command),
                mock.patch.object(
                    RUNNER,
                    "run_process_monitored",
                    return_value=process_capture(
                        stdout,
                        exit_code=-15,
                        termination_reason="hard_timeout",
                    ),
                ),
                mock.patch.object(
                    RUNNER,
                    "run_auxiliary",
                    side_effect=opencode_auxiliary(calls=calls),
                ),
            ):
                result = RUNNER.run_model(
                    "kimi",
                    "test prompt",
                    output_dir,
                    3,
                    0,
                    Path(directory),
                    False,
                )

        self.assertTrue(result.parse_ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.session_cleanup_status, "deleted-verified")
        self.assertTrue(any("export" in command for command in calls))
        self.assertTrue(any(command[1:3] == ["session", "delete"] for command in calls))
        self.assertTrue(any(command[1:3] == ["session", "list"] for command in calls))

    def test_manifest_records_general_profile_metadata_and_core_prompt_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_file = root / "prompt.md"
            output_dir = root / "output"
            prompt = "profile manifest test"
            prompt_file.write_text(prompt, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--profile",
                        "general",
                        "--prompt-file",
                        str(prompt_file),
                        "--output-dir",
                        str(output_dir),
                        "--dry-run",
                    ]
                )
            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["manifest_schema_version"], 2)
        self.assertEqual(manifest["runner_version"], RUNNER.RUNNER_VERSION)
        self.assertEqual(manifest["requested_models"], ["claude", "glm"])
        self.assertEqual(manifest["selection_source"], "profile:general")
        self.assertEqual(manifest["profile_health"]["status"], "not_evaluated")
        self.assertEqual(
            manifest["core_prompt_sha256"],
            __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest(),
        )
        for result in manifest["results"]:
            for field in (
                "participant_id",
                "adapter",
                "requested_model",
                "requested_variant",
                "reasoning_effort",
                "observed_model",
                "observed_variant",
                "provider_family",
                "transport_domain",
                "role",
                "cli_version",
                "session_cleanup_status",
            ):
                self.assertIn(field, result)

    def test_critical_host_position_preflight_fails_before_any_participant_call(self):
        cases = {
            "missing": (None, "pending_host_position", "not_provided"),
            "invalid": ("invalid", "invalid_host_position", "invalid"),
        }
        for label, (content, expected_health, expected_position) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prompt_file = root / "prompt.md"
                output_dir = root / "output"
                prompt_file.write_text("critical preflight", encoding="utf-8")
                argv = [
                    "--host",
                    "codex",
                    "--profile",
                    "critical",
                    "--prompt-file",
                    str(prompt_file),
                    "--output-dir",
                    str(output_dir),
                ]
                if content is not None:
                    host_file = root / "host-position.json"
                    host_file.write_text(
                        json.dumps({"recommendation": "incomplete"}),
                        encoding="utf-8",
                    )
                    argv.extend(["--host-position-file", str(host_file)])

                with (
                    mock.patch.object(RUNNER, "run_model") as participant_call,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    exit_code = RUNNER.main(argv)

                manifest = json.loads(
                    (output_dir / "manifest.json").read_text(encoding="utf-8")
                )
                self.assertEqual(exit_code, 1)
                participant_call.assert_not_called()
                self.assertEqual(manifest["results"], [])
                self.assertEqual(
                    manifest["profile_health"]["status"], expected_health
                )
                self.assertEqual(
                    manifest["profile_health"]["external_collection"]["status"],
                    "not_started",
                )
                self.assertEqual(
                    manifest["profile_health"]["effective_total_seats"], 0
                )
                self.assertFalse(manifest["profile_health"]["profile_target_met"])
                self.assertEqual(manifest["host_position_status"], expected_position)
                self.assertFalse(manifest["host_position_validated"])
                if content is not None:
                    self.assertIsNotNone(manifest["host_position_sha256"])

    def test_valid_host_position_allows_healthy_critical_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_file = root / "prompt.md"
            host_file = root / "host-position.json"
            output_dir = root / "output"
            prompt_file.write_text("critical decision", encoding="utf-8")
            host_bytes = json.dumps(sample_position()).encode("utf-8")
            host_file.write_bytes(host_bytes)

            with (
                mock.patch.object(
                    RUNNER,
                    "run_model",
                    side_effect=lambda participant_id, *args, **kwargs: fake_result(
                        participant_id, ok=True
                    ),
                ) as participant_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--profile",
                        "critical",
                        "--prompt-file",
                        str(prompt_file),
                        "--host-position-file",
                        str(host_file),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(participant_call.call_count, 4)
        self.assertEqual(manifest["profile_health"]["status"], "healthy")
        self.assertEqual(
            manifest["profile_health"]["external_collection"]["status"],
            "healthy",
        )
        self.assertTrue(manifest["profile_health"]["profile_target_met"])
        self.assertTrue(manifest["host_position_validated"])
        self.assertEqual(
            manifest["host_position_sha256"],
            __import__("hashlib").sha256(host_bytes).hexdigest(),
        )

    def test_critical_dry_run_does_not_require_host_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_file = root / "prompt.md"
            output_dir = root / "output"
            prompt_file.write_text("critical plan", encoding="utf-8")
            with (
                mock.patch.object(
                    RUNNER,
                    "run_model",
                    side_effect=lambda participant_id, *args, **kwargs: fake_result(
                        participant_id, ok=False
                    ),
                ) as participant_call,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--profile",
                        "critical",
                        "--prompt-file",
                        str(prompt_file),
                        "--output-dir",
                        str(output_dir),
                        "--dry-run",
                    ]
                )
            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(participant_call.call_count, 4)
        self.assertEqual(manifest["profile_health"]["status"], "not_evaluated")
        self.assertIsNone(manifest["profile_health"]["profile_target_met"])

    def test_critical_underfill_returns_failure_but_preserves_degraded_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt_file = root / "prompt.md"
            host_position_file = root / "host-position.json"
            output_dir = root / "output"
            prompt_file.write_text("critical decision", encoding="utf-8")
            host_position_file.write_text(
                json.dumps(sample_position()), encoding="utf-8"
            )

            def fake_run(participant_id, *args, **kwargs):
                return fake_result(participant_id, ok=participant_id != "deepseek-flash")

            with (
                mock.patch.object(RUNNER, "run_model", side_effect=fake_run),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                exit_code = RUNNER.main(
                    [
                        "--host",
                        "codex",
                        "--profile",
                        "critical",
                        "--prompt-file",
                        str(prompt_file),
                        "--host-position-file",
                        str(host_position_file),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["profile_health"]["status"], "degraded")
        self.assertEqual(manifest["profile_health"]["effective_total_seats"], 4)
        self.assertFalse(manifest["profile_health"]["checks"]["total_seats"])
        self.assertEqual(len(manifest["results"]), 4)
        self.assertEqual(sum(item["parse_ok"] for item in manifest["results"]), 3)

def completed(command, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def opencode_auxiliary(
    *,
    observed_model="alibaba-cn/kimi/kimi-k3",
    observed_variant="max",
    session_remains=False,
    effective_effort="max",
    tool_enabled=False,
    extra_provider=False,
    store_session_ids=None,
    session_list_fails=False,
    calls=None,
    version="1.17.11",
):
    stored_sessions = (
        {"ses_test"} if store_session_ids is None else set(store_session_ids)
    )

    def invoke(command, cwd, env, timeout=30):
        if calls is not None:
            calls.append(command.copy())
        if command[1:] == ["--version"]:
            return completed(command, f"{version}\n")
        if "debug" in command and "agent" in command:
            return completed(
                command,
                json.dumps(
                    {
                        "name": RUNNER.OPENCODE_AGENT,
                        "mode": "primary",
                        "tools": {"bash": tool_enabled, "web": False},
                    }
                ),
            )
        if "debug" in command and "config" in command:
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            config["provider"]["alibaba-cn"]["models"]["kimi/kimi-k3"][
                "variants"
            ]["max"]["reasoningEffort"] = effective_effort
            if extra_provider:
                config["provider"]["unexpected"] = {"models": {}}
            return completed(command, json.dumps(config))
        if "export" in command:
            if "--sanitize" not in command:
                raise AssertionError("OpenCode export must be sanitized")
            provider, model_id = observed_model.split("/", 1)
            return completed(
                command,
                json.dumps(
                    {
                        "messages": [
                            {
                                "info": {
                                    "model": {
                                        "providerID": provider,
                                        "modelID": model_id,
                                    },
                                    "variant": observed_variant,
                                }
                            }
                        ]
                    }
                ),
            )
        if command[1:3] == ["session", "delete"]:
            if not session_remains:
                stored_sessions.discard(command[3])
            return completed(command)
        if command[1:3] == ["session", "list"]:
            if session_list_fails:
                return completed(command, stderr="list failed", returncode=1)
            sessions = [{"id": item} for item in sorted(stored_sessions)]
            # Match OpenCode 1.17.11: an empty store produces successful blank
            # stdout, not a serialized empty array.
            return completed(command, json.dumps(sessions) if sessions else "")
        raise AssertionError(f"Unexpected auxiliary command: {command}")

    return invoke


def opencode_jsonl(text_parts, *, include_optional_step_fields=False, cost=0.001):
    step_start_part = {
        "id": "prt_start",
        "messageID": "msg_test",
        "sessionID": "ses_test",
        "type": "step-start",
    }
    if include_optional_step_fields:
        step_start_part["snapshot"] = "snapshot_start"
    rows = [
        {
            "type": "step_start",
            "timestamp": 1,
            "sessionID": "ses_test",
            "part": step_start_part,
        }
    ]
    rows.extend(
        {
            "type": "text",
            "timestamp": index + 2,
            "sessionID": "ses_test",
            "part": {
                "id": f"prt_text_{index}",
                "messageID": "msg_test",
                "sessionID": "ses_test",
                "type": "text",
                "text": part,
            },
        }
        for index, part in enumerate(text_parts)
    )
    tokens = {
        "input": 100,
        "output": 200,
        "reasoning": 50,
        "cache": {"write": 0, "read": 0},
    }
    step_finish_part = {
        "id": "prt_finish",
        "messageID": "msg_test",
        "sessionID": "ses_test",
        "type": "step-finish",
        "reason": "stop",
        "tokens": tokens,
        "cost": cost,
    }
    if include_optional_step_fields:
        step_finish_part["snapshot"] = "snapshot_finish"
        tokens["total"] = 350
    rows.append(
        {
            "type": "step_finish",
            "timestamp": len(text_parts) + 2,
            "sessionID": "ses_test",
            "part": step_finish_part,
        }
    )
    return "\n".join(json.dumps(row) for row in rows) + "\n"


def process_capture(stdout, *, stderr="", exit_code=0, termination_reason=None):
    return RUNNER.ProcessCapture(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        termination_reason=termination_reason,
        stdout_bytes=len(stdout.encode("utf-8")),
        stderr_bytes=len(stderr.encode("utf-8")),
        activity_file_bytes=0,
        first_output_seconds=0.01 if stdout else None,
        last_output_seconds=0.02 if stdout else None,
    )


def fake_result(participant_id, *, ok):
    spec = RUNNER.participant_spec(participant_id)
    return RUNNER.Result(
        model=participant_id,
        status="ok" if ok else "error",
        exit_code=0 if ok else 1,
        duration_seconds=0.1,
        output_file=f"/tmp/{participant_id}.md" if ok else None,
        structured_file=f"/tmp/{participant_id}.json" if ok else None,
        raw_file=f"/tmp/{participant_id}.raw.txt",
        parse_ok=ok,
        usage={},
        stderr="" if ok else "simulated failure",
        command=[spec.adapter],
        termination_reason=None,
        activity={},
        failure_category=None if ok else "simulated",
        diagnostic_hint=None,
        participant_id=participant_id,
        adapter=spec.adapter,
        requested_model=spec.model,
        requested_variant=spec.variant,
        reasoning_effort=spec.reasoning_effort,
        observed_model=spec.model if ok else None,
        observed_variant=spec.variant if ok else None,
        provider_family=spec.provider_family,
        transport_domain=spec.transport_domain,
        role=spec.role,
        cli_version="test",
        session_cleanup_status=(
            "deleted-verified" if spec.adapter == "opencode" and ok else None
        ),
    )


def sample_position():
    return {
        "recommendation": "Use the safer option",
        "analysis": "It has the smallest failure domain.",
        "confidence": 0.8,
        "assumptions": ["The workload is small"],
        "evidence": [
            {
                "claim": "The failure domain is smaller",
                "support": "The worker is isolated",
                "verification": "Kill the worker during a test",
            }
        ],
        "risks": ["Higher latency"],
        "change_conditions": ["Measured latency exceeds the SLO"],
    }


if __name__ == "__main__":
    unittest.main()
