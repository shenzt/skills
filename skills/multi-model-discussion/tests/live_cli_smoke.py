#!/usr/bin/env python3
"""Opt-in end-to-end smoke test for real council CLIs.

The default validation mode reads an existing manifest and makes no external
calls. Passing --confirm-live-calls runs a small, non-sensitive prompt through
the selected paid providers, so it must never be enabled implicitly by unit
tests or CI.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNNER = SKILL_DIR / "scripts" / "run_council.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("live_smoke_runner", RUNNER)
if RUNNER_SPEC is None or RUNNER_SPEC.loader is None:
    raise RuntimeError("could not import the bundled council runner")
COUNCIL = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = COUNCIL
RUNNER_SPEC.loader.exec_module(COUNCIL)
DEFAULT_PARTICIPANTS = ("claude", "kimi", "glm")
EXPECTED_MODELS = {
    "claude": "claude-fable-5-1",
    "claude-fable-5-fallback": "claude-fable-5",
    "kimi": "alibaba-cn/kimi/kimi-k3",
    "glm": "zhipuai/glm-5.3",
}
FABLE_5_1_MINIMUM_CLAUDE_CODE = (2, 1, 255)
LIVE_PROMPT = """## Background

This is a harmless live adapter integration test. It contains no workspace,
personal, medical, confidential, or current-world information.

## Requirements and constraints

- Do not use tools, files, web access, agents, or other models.
- Keep the complete answer concise.
- Treat all facts below as self-contained.

## Question for this round

Choose between Option A, which is reversible and costs 1 unit, and Option B,
which is irreversible and costs 10 units. Both provide the same benefit.

## Evaluation criteria

- Prefer reversibility and lower cost.
- State one assumption, one risk, and one condition that would change the choice.
"""


def _participants(value: str) -> tuple[str, ...]:
    participants = tuple(item.strip() for item in value.split(",") if item.strip())
    unknown = sorted(set(participants) - set(EXPECTED_MODELS))
    if not participants or unknown or len(participants) != len(set(participants)):
        raise argparse.ArgumentTypeError(
            "participants must be a unique subset of "
            + ",".join(EXPECTED_MODELS)
        )
    return participants


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(manifest_path: Path, participants: tuple[str, ...]) -> None:
    manifest = _load_json(manifest_path)
    results = {
        item.get("participant_id"): item
        for item in manifest.get("results", [])
        if isinstance(item, dict)
    }
    failures: list[str] = []
    summary: dict[str, Any] = {}
    for participant in participants:
        result = results.get(participant)
        if not isinstance(result, dict):
            failures.append(f"{participant}: missing result")
            continue
        expected_model = EXPECTED_MODELS[participant]
        if result.get("status") != "ok" or result.get("parse_ok") is not True:
            failures.append(
                f"{participant}: status={result.get('status')!r}, "
                f"parse_ok={result.get('parse_ok')!r}"
            )
        if result.get("requested_model") != expected_model:
            failures.append(f"{participant}: requested model mismatch")
        if result.get("observed_model") != expected_model:
            failures.append(f"{participant}: observed model mismatch")

        if COUNCIL.participant_spec(participant).adapter == "claude":
            command = result.get("command")
            try:
                index = command.index("--mcp-config")
                mcp_config = json.loads(command[index + 1])
            except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
                mcp_config = None
            if mcp_config != {"mcpServers": {}}:
                failures.append("claude: MCP configuration was not an empty mcpServers map")
            for flag in (
                "--safe-mode",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--disable-slash-commands",
            ):
                if flag not in command:
                    failures.append(f"claude: missing required safety flag {flag}")
            try:
                tools = command[command.index("--tools") + 1]
            except (AttributeError, IndexError, TypeError, ValueError):
                tools = None
            if tools != "":
                failures.append("claude: tools were not disabled")
            try:
                schema_marker = command[command.index("--json-schema") + 1]
                bundled_schema = _load_json(
                    SKILL_DIR / "references" / "participant.schema.json"
                )
                if schema_marker == str(
                    SKILL_DIR / "references" / "participant.schema.json"
                ):
                    pinned_schema = bundled_schema
                else:
                    pinned_schema = json.loads(schema_marker)
            except (
                AttributeError,
                IndexError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pinned_schema = None
                bundled_schema = None
            if pinned_schema != bundled_schema or pinned_schema is None:
                failures.append("claude: structured-output schema was not pinned")
        else:
            if result.get("requested_variant") != "max":
                failures.append(f"{participant}: requested variant was not max")
            if result.get("observed_variant") != "max":
                failures.append(f"{participant}: observed variant was not max")
            if result.get("session_cleanup_status") != "deleted-verified":
                failures.append(f"{participant}: session cleanup was not deleted-verified")
            raw_file = result.get("raw_file")
            try:
                protocol = _load_json(Path(raw_file))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                protocol = None
            if not isinstance(protocol, dict):
                failures.append(f"{participant}: sanitized protocol summary unavailable")
            else:
                for field, expected in (
                    ("tool_event_count", 0),
                    ("protocol_error_count", 0),
                    ("unexpected_event_type_count", 0),
                    ("session_id_count", 1),
                ):
                    if protocol.get(field) != expected:
                        failures.append(
                            f"{participant}: {field}={protocol.get(field)!r}, "
                            f"expected {expected!r}"
                        )

        summary[participant] = {
            "status": result.get("status"),
            "parse_ok": result.get("parse_ok"),
            "observed_model": result.get("observed_model"),
            "observed_variant": result.get("observed_variant"),
            "session_cleanup_status": result.get("session_cleanup_status"),
            "duration_seconds": result.get("duration_seconds"),
        }

    if manifest.get("profile_health", {}).get("status") != "healthy":
        failures.append("profile health was not healthy")
    if failures:
        raise RuntimeError("live CLI smoke failed:\n- " + "\n- ".join(failures))
    print(json.dumps(summary, indent=2, sort_keys=True))


def run_live(
    participants: tuple[str, ...],
    host: str,
    timeout: float,
    root: Path,
    env_file: Path | None,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    prompt_file = root / "prompt.md"
    output_dir = root / "output"
    prompt_file.write_text(LIVE_PROMPT, encoding="utf-8")
    command = [
        sys.executable,
        str(RUNNER),
        "--host",
        host,
        "--participants",
        ",".join(participants),
        "--prompt-file",
        str(prompt_file),
        "--output-dir",
        str(output_dir),
        "--timeout",
        str(timeout),
        "--silence-timeout",
        "0",
    ]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout + 90,
    )
    manifest_path = output_dir / "manifest.json"
    if completed.returncode != 0 and not manifest_path.exists():
        raise RuntimeError(
            f"runner exited {completed.returncode} without a manifest: "
            f"{completed.stderr[-1000:]}"
        )
    return manifest_path


def run_preflight(
    participants: tuple[str, ...],
    env_file: Path | None,
) -> None:
    provider_env = COUNCIL.load_provider_env_file(env_file)
    summary: dict[str, Any] = {}
    for participant in participants:
        spec = COUNCIL.participant_spec(participant)
        command = COUNCIL.command_for(participant, Path(os.devnull))
        executable = command[0]
        resolved = Path(executable)
        available = (
            resolved.is_file() and os.access(resolved, os.X_OK)
            if resolved.is_absolute()
            else shutil.which(executable) is not None
        )
        if not available:
            raise RuntimeError(f"{participant}: CLI not found: {executable}")

        if spec.adapter == "claude":
            claude_env = COUNCIL.clean_env(participant, provider_env)
            version_result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                env=claude_env,
                timeout=30,
                check=False,
            )
            completed = subprocess.run(
                command,
                input="",
                capture_output=True,
                text=True,
                env=claude_env,
                timeout=30,
                check=False,
            )
            combined = f"{completed.stdout}\n{completed.stderr}".lower()
            if completed.returncode == 0 or "input must be provided" not in combined:
                raise RuntimeError(
                    "claude: empty-input preflight did not stop before inference"
                )
            if "invalid mcp configuration" in combined:
                raise RuntimeError("claude: MCP configuration was rejected")
            version_text = (
                version_result.stdout.strip() or version_result.stderr.strip()
            )
            if spec.model == "claude-fable-5-1":
                match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version_text)
                if (
                    not match
                    or tuple(map(int, match.groups()))
                    < FABLE_5_1_MINIMUM_CLAUDE_CODE
                ):
                    raise RuntimeError(
                        "claude: Fable 5.1 requires Claude Code 2.1.255 or newer"
                    )
            summary[participant] = {
                "cli_version": version_text or None,
                "mcp_config_accepted": True,
                "stopped_before_inference": True,
            }
            continue

        with tempfile.TemporaryDirectory(
            prefix=f"multi-model-live-preflight-{participant}-"
        ) as directory:
            root = Path(directory)
            env = COUNCIL.clean_env(participant, provider_env)
            COUNCIL.configure_opencode_isolation(env, root)
            version = COUNCIL.opencode_preflight(spec, executable, root, env)
        summary[participant] = {
            "cli_version": version,
            "route": spec.model,
            "variant": spec.variant,
            "no_model_call": True,
        }
    print(json.dumps(summary, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--participants",
        type=_participants,
        default=DEFAULT_PARTICIPANTS,
        help="unique subset of the live-smoke participant registry",
    )
    parser.add_argument("--host", choices=("claude", "codex", "gemini"), default="codex")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="explicit 0600 provider credential file passed to the council runner",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        help="validate an existing live-run manifest without external calls",
    )
    parser.add_argument(
        "--confirm-live-calls",
        action="store_true",
        help="explicitly authorize real provider calls using the bundled harmless prompt",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate real CLI flags/configuration without sending a model prompt",
    )
    parser.add_argument(
        "--keep-output-dir",
        type=Path,
        help="retain live smoke artifacts at this explicit path",
    )
    args = parser.parse_args()

    mode_count = sum(
        (args.manifest is not None, args.confirm_live_calls, args.preflight_only)
    )
    if mode_count != 1:
        parser.error(
            "choose exactly one of --manifest, --preflight-only, or --confirm-live-calls"
        )
    if args.manifest:
        validate_manifest(args.manifest.resolve(), args.participants)
        return 0
    if args.preflight_only:
        run_preflight(
            args.participants,
            args.env_file.expanduser().absolute() if args.env_file else None,
        )
        return 0

    if args.keep_output_dir:
        manifest = run_live(
            args.participants,
            args.host,
            args.timeout,
            args.keep_output_dir.resolve(),
            args.env_file.expanduser().absolute() if args.env_file else None,
        )
        validate_manifest(manifest, args.participants)
        return 0

    with tempfile.TemporaryDirectory(prefix="multi-model-live-smoke-") as directory:
        manifest = run_live(
            args.participants,
            args.host,
            args.timeout,
            Path(directory),
            args.env_file.expanduser().absolute() if args.env_file else None,
        )
        validate_manifest(manifest, args.participants)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
