#!/usr/bin/env python3
"""Run external model-council participants through their CLIs in parallel."""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


MODELS = ("claude", "codex", "gemini")
HOSTS = MODELS
DEFAULT_CLAUDE_MODEL = "claude-fable-5-1"
DEFAULT_CLAUDE_EFFORT = "max"
REQUIRED_DIRECT_MODEL_ATTESTATION = frozenset({"claude-fable-5-1"})
DEFAULT_HARD_TIMEOUT_SECONDS = 900.0
DEFAULT_SILENCE_TIMEOUT_SECONDS = 0.0
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_AUX_OUTPUT_BYTES = 1024 * 1024
MAX_HOST_POSITION_BYTES = 1024 * 1024
MAX_USAGE_MODELS = 16
MAX_USAGE_VALUE = 1_000_000_000_000_000
MANIFEST_SCHEMA_VERSION = 2
RUNNER_VERSION = "3.3.0"
OUTPUT_POLL_INTERVAL_SECONDS = 0.05
SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = SKILL_DIR / "references" / "participant.schema.json"
GEMINI_POLICY_PATH = SKILL_DIR / "references" / "gemini-deny-all.toml"
COUNCIL_PROFILES_PATH = SKILL_DIR / "references" / "council-profiles.json"
OPENCODE_CONFIG_PATH = SKILL_DIR / "references" / "opencode-council.json"
OPENCODE_AGENT = "council-participant"
OPENCODE_AUTH_CONTENT_ENV = "OPENCODE_AUTH_CONTENT"
PARTICIPANT_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CHATGPT_CODEX_BINARIES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/ChatGPT.app/Contents/Resources/native/codex-macos"),
)
CODEX_BINARY_OVERRIDE_ENV = "MODEL_COUNCIL_CODEX_BIN"

# Keep enough process context for the installed CLIs and configured network,
# but do not hand every participant every credential in the host environment.
COMMON_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COLORTERM",
    "NO_COLOR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
}
PROVIDER_ENV_ALLOWLIST = {
    "claude": {
        "CLAUDE_CONFIG_DIR",
    },
    "codex": {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "CODEX_API_KEY",
        "CODEX_HOME",
    },
    "gemini": {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GEMINI_CLI_HOME",
    },
    "opencode": set(),
    "deepseek-http": {"DEEPSEEK_API_KEY"},
}
OPENCODE_ROUTE_ENV_ALLOWLIST = {
    "google": {
        "GEMINI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
    },
    "alibaba-cn": {
        "ALIBABA_API_KEY",
        "DASHSCOPE_API_KEY",
    },
    "zhipuai": {
        "ZHIPU_API_KEY",
        "ZHIPUAI_API_KEY",
    },
    "deepseek": {
        "DEEPSEEK_API_KEY",
    },
}
OPENCODE_ROUTE_ENV_PREFERENCE = {
    "google": (
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GEMINI_API_KEY",
    ),
    "alibaba-cn": (
        "DASHSCOPE_API_KEY",
        "ALIBABA_API_KEY",
    ),
    "zhipuai": ("ZHIPU_API_KEY", "ZHIPUAI_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
}
OPENCODE_ISOLATION_FLAGS = {
    "OPENCODE_PURE": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "1",
    "OPENCODE_AUTO_SHARE": "0",
}
OPENCODE_KNOWN_EVENT_TYPES = {
    "text",
    "reasoning",
    "step_start",
    "step_finish",
    "error",
}
MAX_OPENCODE_SESSION_CLEANUP = 32
MAX_PROVIDER_ENV_FILE_BYTES = 64 * 1024
SUPPORTED_OPENCODE_CLI_VERSIONS = frozenset({"1.17.11"})
CLAUDE_GATEWAY_PAIR = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
CLAUDE_ZERO_RETRY_ENV = {
    # The council owns retry decisions. Claude Code otherwise retries failed
    # API requests (and invalid --json-schema responses) inside one apparent
    # runner attempt, which defeats the call and cost ledger.
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "MAX_STRUCTURED_OUTPUT_RETRIES": "0",
}
OUTPUT_TRUNCATION_MARKER = b"<OUTPUT TRUNCATED: configured byte limit exceeded>\n"
PROVIDER_ENV_ASSIGNMENT = re.compile(
    r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(.*)$"
)
PROVIDER_ENV_ALIASES = {"ZHIPUAI_API_KEY": "ZHIPU_API_KEY"}
PROVIDER_ENV_FILE_KEYS = frozenset(
    {
        *(name for names in OPENCODE_ROUTE_ENV_PREFERENCE.values() for name in names),
        *PROVIDER_ENV_ALIASES,
    }
)


@dataclass(frozen=True)
class ParticipantSpec:
    participant_id: str
    adapter: str
    model: str
    variant: str
    reasoning_effort: str
    provider_family: str
    transport_domain: str
    role: str


@dataclass
class Result:
    model: str
    status: str
    exit_code: int | None
    duration_seconds: float
    output_file: str | None
    structured_file: str | None
    raw_file: str | None
    parse_ok: bool
    usage: dict[str, Any]
    stderr: str
    command: list[str]
    termination_reason: str | None
    activity: dict[str, Any]
    failure_category: str | None
    diagnostic_hint: str | None
    participant_id: str | None = None
    adapter: str | None = None
    requested_model: str | None = None
    requested_variant: str | None = None
    reasoning_effort: str | None = None
    observed_model: str | None = None
    observed_variant: str | None = None
    provider_family: str | None = None
    transport_domain: str | None = None
    role: str | None = None
    cli_version: str | None = None
    session_cleanup_status: str | None = None


@dataclass
class ProcessCapture:
    stdout: str
    stderr: str
    exit_code: int | None
    termination_reason: str | None
    stdout_bytes: int
    stderr_bytes: int
    activity_file_bytes: int
    first_output_seconds: float | None
    last_output_seconds: float | None


@dataclass
class OpenCodeEvents:
    response_text: str
    usage: dict[str, Any]
    session_ids: set[str]
    event_types: list[str]
    tool_events: list[str]
    errors: list[str]
    malformed_lines: list[int]
    protocol_errors: list[str]


@dataclass
class OpenCodeSessionInspection:
    observed_model: str | None
    observed_variant: str | None
    cleanup_status: str
    export_error: str | None


@dataclass(frozen=True)
class HostPositionAttestation:
    file: str | None
    sha256: str | None
    validated: bool
    status: str
    error: str | None


def load_council_config() -> dict[str, Any]:
    try:
        config = json.loads(COUNCIL_PROFILES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError(f"Could not load council profiles: {error}") from error
    if not isinstance(config, dict):
        raise ValueError("Council profiles must be a JSON object")
    participants = config.get("participants")
    profiles = config.get("profiles")
    priority = config.get("priority")
    if not isinstance(participants, dict) or not isinstance(profiles, dict):
        raise ValueError("Council profiles require participants and profiles objects")
    if not isinstance(priority, list) or not all(isinstance(item, str) for item in priority):
        raise ValueError("Council priority must be an array of participant ids")
    return config


def participant_spec(participant_id: str) -> ParticipantSpec:
    config = load_council_config()
    raw = config["participants"].get(participant_id)
    if not isinstance(raw, dict):
        raise ValueError(f"Unknown participant: {participant_id}")
    required = (
        "adapter",
        "model",
        "variant",
        "reasoning_effort",
        "provider_family",
        "transport_domain",
        "role",
    )
    if any(not isinstance(raw.get(field), str) or not raw[field] for field in required):
        raise ValueError(f"Invalid participant spec: {participant_id}")
    return ParticipantSpec(
        participant_id=participant_id,
        adapter=raw["adapter"],
        model=raw["model"],
        variant=raw["variant"],
        reasoning_effort=raw["reasoning_effort"],
        provider_family=raw["provider_family"],
        transport_domain=raw["transport_domain"],
        role=raw["role"],
    )


def detect_host() -> str | None:
    if os.environ.get("CLAUDECODE"):
        return "claude"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SHELL"):
        return "codex"
    if os.environ.get("GEMINI_CLI") or os.environ.get("GEMINI_SESSION_ID"):
        return "gemini"
    return None


def _safe_absolute_override(variable: str) -> str | None:
    value = os.environ.get(variable)
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{variable} must be an absolute executable path")
    resolved = candidate.resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"{variable} does not name an executable file")
    return str(resolved)


def adapter_executable(adapter: str) -> str:
    if adapter == "deepseek-http":
        return sys.executable
    if adapter == "codex":
        override = _safe_absolute_override(CODEX_BINARY_OVERRIDE_ENV)
        if override:
            return override
        for candidate in CHATGPT_CODEX_BINARIES:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return shutil.which("codex") or "codex"
    names = {
        "claude": "claude",
        "gemini": "gemini",
        "opencode": "opencode",
    }
    try:
        name = names[adapter]
    except KeyError as error:
        raise ValueError(f"Unsupported adapter: {adapter}") from error
    return shutil.which(name) or name


def command_for(model: str, raw_output_file: Path) -> list[str]:
    spec = participant_spec(model)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    executable = adapter_executable(spec.adapter)
    if spec.adapter == "deepseek-http":
        if spec.model != "deepseek-flash" or spec.reasoning_effort != "max":
            raise ValueError("Unsupported DeepSeek HTTP route or effort")
        return [executable, "-I", "-B", str(SKILL_DIR / "scripts" / "deepseek_http.py")]
    if spec.adapter == "claude":
        command = [
            executable,
            "-p",
            "--model",
            spec.model,
            "--effort",
            spec.reasoning_effort,
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--no-session-persistence",
            "--disable-slash-commands",
            "--safe-mode",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--agents",
            "{}",
            "--tools",
            "",
        ]
        if spec.model == "claude-fable-5-1":
            command.extend(
                [
                    "--no-chrome",
                    "--permission-mode",
                    "plan",
                    "--prompt-suggestions",
                    "false",
                ]
            )
        return command
    if spec.adapter == "codex":
        return [
            executable,
            "exec",
            "-",
            "--model",
            spec.model,
            "--config",
            f'model_reasoning_effort="{spec.reasoning_effort}"',
            "--strict-config",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--json",
            "--ignore-user-config",
            "--ignore-rules",
            "--output-schema",
            str(SCHEMA_PATH),
            "--output-last-message",
            str(raw_output_file),
        ]
    if spec.adapter == "gemini":
        return [
            executable,
            "-p",
            "",
            "--model",
            spec.model,
            "--approval-mode",
            "plan",
            "--policy",
            str(GEMINI_POLICY_PATH),
            "--output-format",
            "json",
        ]
    if spec.adapter == "opencode":
        return [
            executable,
            "run",
            "--pure",
            "--agent",
            OPENCODE_AGENT,
            "--model",
            spec.model,
            "--variant",
            spec.variant,
            "--format",
            "json",
            "--title",
            f"model-council-{model}-{uuid.uuid4().hex[:12]}",
            "Use the complete council task supplied on stdin.",
        ]
    raise ValueError(f"Unsupported adapter: {spec.adapter}")


def participant_prompt(model: str, prompt: str) -> str:
    spec = participant_spec(model)
    return (
        f"You are the {model.title()} participant in a model council. "
        f"Your assigned perspective is: {spec.role} "
        "You are not the orchestrator. Answer directly and independently. "
        "Do not invoke other models, CLIs, subagents, skills, or tools. "
        "Do not edit files. Identify at least one non-obvious alternative or "
        "assumption instead of merely repeating the likely consensus.\n\n"
        f"{prompt}\n\n"
        "Return only one JSON object matching this contract:\n"
        "- recommendation: concise recommended decision\n"
        "- analysis: substantive reasoning in the user's language\n"
        "- confidence: number from 0 to 1\n"
        "- assumptions: array of assumptions\n"
        "- evidence: array of objects with claim, support, and verification\n"
        "- risks: array of concrete risks\n"
        "- change_conditions: array describing what evidence would change your view"
    )


def _opencode_route(spec: ParticipantSpec) -> str:
    route, separator, model_id = spec.model.partition("/")
    if not separator or not route or not model_id:
        raise ValueError(f"Invalid OpenCode model route: {spec.model}")
    return route


def _parse_provider_env_value(raw_value: str, line_number: int) -> str:
    value = raw_value.strip()
    if not value:
        raise ValueError(f"Provider env file line {line_number} has an empty value")
    if value[0] in {"'", '"'}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as error:
            raise ValueError(
                f"Provider env file line {line_number} has invalid quoting"
            ) from error
        if not isinstance(parsed, str):
            raise ValueError(
                f"Provider env file line {line_number} must contain a string value"
            )
        value = parsed
    elif any(character.isspace() for character in value):
        raise ValueError(
            f"Provider env file line {line_number} has unsupported whitespace"
        )
    if (
        not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "$(" in value
        or "${" in value
        or "`" in value
    ):
        raise ValueError(f"Provider env file line {line_number} has an invalid value")
    return value


def load_provider_env_file(path: Path | None) -> dict[str, str]:
    """Parse a small route-credential file without invoking a shell.

    The file is explicit rather than auto-discovered so an unrelated project
    `.env` cannot silently become model-provider authority. Only credential
    names needed by the bundled OpenCode/DeepSeek HTTP routes are accepted. Claude Code,
    Codex, and Gemini native adapters continue to use their own authenticated
    CLI environments.
    """

    if path is None:
        return {}
    expanded = path.expanduser()
    try:
        metadata = expanded.lstat()
    except OSError as error:
        raise ValueError("Provider env file could not be inspected") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("Provider env file must not be a symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Provider env file must be a regular file")
    if metadata.st_size > MAX_PROVIDER_ENV_FILE_BYTES:
        raise ValueError("Provider env file exceeds the 64 KiB safety limit")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError("Provider env file permissions must be 0600 or stricter")
    try:
        text = expanded.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("Provider env file must be readable UTF-8 text") from error

    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if line_number == 1:
            line = line.removeprefix("\ufeff")
        if not line or line.startswith("#"):
            continue
        match = PROVIDER_ENV_ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ValueError(
                f"Provider env file line {line_number} is not a plain KEY=VALUE assignment"
            )
        source_name, raw_value = match.groups()
        if source_name not in PROVIDER_ENV_FILE_KEYS:
            raise ValueError(
                f"Provider env file line {line_number} uses unsupported key {source_name!r}"
            )
        name = PROVIDER_ENV_ALIASES.get(source_name, source_name)
        value = _parse_provider_env_value(raw_value, line_number)
        previous = parsed.get(name)
        if previous is not None and previous != value:
            raise ValueError(f"Provider env file defines conflicting values for {name}")
        parsed[name] = value
    return parsed


def _read_opencode_auth_map() -> dict[str, Any]:
    inline = os.environ.get(OPENCODE_AUTH_CONTENT_ENV)
    if inline:
        try:
            payload = json.loads(inline)
        except json.JSONDecodeError as error:
            raise ValueError("Stored OpenCode auth content is not valid JSON") from error
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        if data_home:
            auth_path = Path(data_home).expanduser() / "opencode" / "auth.json"
        else:
            source_home = os.environ.get("HOME")
            if not source_home:
                return {}
            auth_path = (
                Path(source_home).expanduser()
                / ".local"
                / "share"
                / "opencode"
                / "auth.json"
            )
        try:
            payload = json.loads(auth_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as error:
            raise ValueError("Could not safely read stored OpenCode auth") from error
    if not isinstance(payload, dict):
        raise ValueError("Stored OpenCode auth must be a JSON object")
    return payload


def _project_opencode_route_auth(route: str) -> dict[str, Any] | None:
    raw = _read_opencode_auth_map().get(route)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"Stored OpenCode auth for {route!r} is invalid")
    auth_type = raw.get("type")
    key = raw.get("key")
    # The production council routes currently use API credentials. Copy only
    # the two fields OpenCode needs, never opaque metadata or another provider.
    if auth_type != "api" or not isinstance(key, str) or not key:
        raise ValueError(
            f"Stored OpenCode auth for {route!r} is not a supported API credential"
        )
    return {"type": "api", "key": key}


def _opencode_route_config(spec: ParticipantSpec) -> dict[str, Any]:
    try:
        bundled = json.loads(OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError(f"Could not load bundled OpenCode config: {error}") from error
    if not isinstance(bundled, dict):
        raise ValueError("Bundled OpenCode config must be a JSON object")
    route = _opencode_route(spec)
    providers = bundled.get("provider")
    route_config = providers.get(route) if isinstance(providers, dict) else None
    if not isinstance(route_config, dict):
        raise ValueError(f"Bundled OpenCode config has no provider route {route!r}")

    # Construct a new object instead of passing the user's global OpenCode
    # config. That prevents plugins, MCP servers, instructions, and unrelated
    # provider routes from entering the participant process.
    config = {
        key: value for key, value in bundled.items() if key != "provider"
    }
    config.update(
        {
            "provider": {route: route_config},
            "enabled_providers": [route],
            "default_agent": OPENCODE_AGENT,
            "plugin": [],
            "mcp": {},
            "instructions": [],
            "share": "disabled",
            "snapshot": False,
            "autoupdate": False,
        }
    )
    return config


def _select_opencode_route_env(
    route: str,
    provider_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    candidates = OPENCODE_ROUTE_ENV_PREFERENCE.get(route)
    if not candidates:
        raise ValueError(f"No credential policy exists for OpenCode route {route!r}")
    sources: tuple[Mapping[str, str], ...] = (
        *((provider_env,) if provider_env else ()),
        os.environ,
    )
    for name in candidates:
        for source in sources:
            value = source.get(name)
            if not value:
                continue
            if route == "google":
                return {"GOOGLE_GENERATIVE_AI_API_KEY": value}
            canonical_name = PROVIDER_ENV_ALIASES.get(name, name)
            return {canonical_name: value}
    return {}


def configure_opencode_isolation(env: dict[str, str], root: Path) -> None:
    root.chmod(0o700)
    paths = {
        "HOME": root / "home",
        "XDG_DATA_HOME": root / "data",
        "XDG_STATE_HOME": root / "state",
        "XDG_CACHE_HOME": root / "cache",
        "XDG_CONFIG_HOME": root / "config",
        "XDG_RUNTIME_DIR": root / "runtime",
        "TMPDIR": root / "tmp",
    }
    for path in paths.values():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    env.update({name: str(path) for name, path in paths.items()})
    env["TEMP"] = str(paths["TMPDIR"])
    env["TMP"] = str(paths["TMPDIR"])
    env["OPENCODE_CONFIG_DIR"] = str(paths["XDG_CONFIG_HOME"] / "opencode")
    env["OPENCODE_DB"] = str(paths["XDG_DATA_HOME"] / "opencode" / "opencode.db")
    env.update(OPENCODE_ISOLATION_FLAGS)


def clean_env(
    model: str,
    provider_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    spec = participant_spec(model)
    if spec.adapter not in PROVIDER_ENV_ALLOWLIST:
        raise ValueError(f"Unsupported adapter: {spec.adapter}")
    allowed = COMMON_ENV_ALLOWLIST | PROVIDER_ENV_ALLOWLIST[spec.adapter]
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.setdefault("PATH", os.defpath)
    env["NO_COLOR"] = "1"

    if spec.adapter == "claude":
        token = os.environ.get(CLAUDE_GATEWAY_PAIR[0])
        endpoint = os.environ.get(CLAUDE_GATEWAY_PAIR[1])
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        # Never detach a custom endpoint from its credential. Prefer the
        # explicit gateway bearer pair, support API-key gateways as a pair,
        # and retain a standalone API key only for the default endpoint.
        if endpoint and token:
            env[CLAUDE_GATEWAY_PAIR[0]] = token
            env[CLAUDE_GATEWAY_PAIR[1]] = endpoint
        elif endpoint and api_key:
            env["ANTHROPIC_API_KEY"] = api_key
            env[CLAUDE_GATEWAY_PAIR[1]] = endpoint
        elif api_key and not endpoint:
            env["ANTHROPIC_API_KEY"] = api_key
        env.update(CLAUDE_ZERO_RETRY_ENV)
    elif spec.adapter == "deepseek-http":
        route_env = _select_opencode_route_env("deepseek", provider_env)
        if not route_env:
            auth = _project_opencode_route_auth("deepseek")
            if auth:
                route_env = {"DEEPSEEK_API_KEY": auth["key"]}
        if not route_env:
            raise ValueError("No DeepSeek credential; supply DEEPSEEK_API_KEY or an explicit owner-only --env-file")
        env.update(route_env)
    elif spec.adapter == "opencode":
        # HOME/TMP values are source-machine paths. Never pass them to
        # OpenCode; configure_opencode_isolation replaces them with a fresh,
        # participant-specific root immediately before preflight.
        for key in ("HOME", "TMPDIR", "TEMP", "TMP"):
            env.pop(key, None)
        route = _opencode_route(spec)
        route_env = _select_opencode_route_env(route, provider_env)
        env.update(route_env)
        projected_auth = None if route_env else _project_opencode_route_auth(route)
        if projected_auth is None and not route_env:
            raise ValueError(
                f"No isolated credential is available for OpenCode route {route!r}; "
                "configure its provider API key before retrying"
            )
        config = _opencode_route_config(spec)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            config, ensure_ascii=False, separators=(",", ":")
        )
        env[OPENCODE_AUTH_CONTENT_ENV] = json.dumps(
            {route: projected_auth} if projected_auth is not None else {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return env


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        process.wait(timeout=3)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_process(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
) -> subprocess.Popen[str]:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=stdout,
        stderr=stderr,
        text=True,
        cwd=cwd,
        env=env,
        **kwargs,
    )


def remove_generated_artifacts(paths: Sequence[Path]) -> None:
    """Remove participant artifacts that the runner exclusively owns."""
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def remove_empty_artifacts(paths: Sequence[Path]) -> None:
    for path in paths:
        try:
            if path.stat().st_size == 0:
                path.unlink()
        except FileNotFoundError:
            pass


def _total_size(paths: Sequence[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


def _bounded_text(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace")
    marker = OUTPUT_TRUNCATION_MARKER
    prefix_size = max(0, limit - len(marker))
    return (data[:prefix_size] + marker[: limit - prefix_size]).decode(
        "utf-8", errors="replace"
    )


def _truncate_file(path: Path, limit: int) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size <= limit:
        return
    marker = OUTPUT_TRUNCATION_MARKER
    prefix_size = max(0, limit - len(marker))
    with path.open("rb") as stream:
        prefix = stream.read(prefix_size)
    bounded = prefix + marker[: limit - len(prefix)]
    with path.open("r+b") as stream:
        stream.seek(0)
        stream.write(bounded)
        stream.truncate()


def _best_effort_terminate(process: subprocess.Popen[str]) -> None:
    try:
        terminate_process_tree(process)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_process_monitored(
    command: list[str],
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    hard_timeout: float,
    silence_timeout: float,
    activity_paths: Sequence[Path] = (),
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> ProcessCapture:
    """Run a CLI while monitoring stdout/stderr growth and optional silence.

    Structured-output CLIs may legitimately stay quiet until they finish, so a
    silence timeout of zero disables early termination. The hard timeout always
    applies. Output is spooled to files to avoid pipe deadlocks and to make byte
    growth observable while the child is running.
    """
    if not _is_finite_number(hard_timeout) or hard_timeout <= 0:
        raise ValueError("hard_timeout must be positive and finite")
    if not _is_finite_number(silence_timeout) or silence_timeout < 0:
        raise ValueError("silence_timeout must be non-negative and finite")
    if max_output_bytes <= 0:
        raise ValueError("max_output_bytes must be positive")

    started = time.monotonic()
    termination_reason: str | None = None
    first_output_at: float | None = None
    last_output_at: float | None = None
    process: subprocess.Popen[str] | None = None

    with tempfile.TemporaryDirectory(prefix="model-council-") as directory:
        stdout_path = Path(directory) / "stdout.txt"
        stderr_path = Path(directory) / "stderr.txt"
        monitored_paths = (stdout_path, stderr_path, *activity_paths)

        with stdout_path.open("w", encoding="utf-8") as stdout_stream, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_stream:
            try:
                process = start_process(
                    command,
                    cwd,
                    env,
                    stdout=stdout_stream,
                    stderr=stderr_stream,
                )
                if process.stdin is not None:
                    try:
                        process.stdin.write(prompt)
                        process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    finally:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass

                observed_size = _total_size(monitored_paths)
                if observed_size:
                    first_output_at = time.monotonic()
                    last_output_at = first_output_at

                while process.poll() is None:
                    now = time.monotonic()
                    current_size = _total_size(monitored_paths)
                    if current_size > observed_size:
                        if first_output_at is None:
                            first_output_at = now
                        last_output_at = now
                        observed_size = current_size

                    if current_size > max_output_bytes:
                        termination_reason = "output_limit"
                        _best_effort_terminate(process)
                        break

                    if now - started >= hard_timeout:
                        termination_reason = "hard_timeout"
                        _best_effort_terminate(process)
                        break

                    silence_anchor = last_output_at or started
                    if silence_timeout > 0 and now - silence_anchor >= silence_timeout:
                        termination_reason = "silence_timeout"
                        _best_effort_terminate(process)
                        break

                    time.sleep(OUTPUT_POLL_INTERVAL_SECONDS)

                if process.poll() is None:
                    if termination_reason is None:
                        process.wait()
                    else:
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            _best_effort_terminate(process)
                            process.wait(timeout=3)
            except Exception:
                if process is not None:
                    try:
                        still_running = process.poll() is None
                    except Exception:
                        still_running = True
                    if still_running:
                        _best_effort_terminate(process)
                raise
            finally:
                if process is not None and process.stdin is not None:
                    try:
                        if not process.stdin.closed:
                            process.stdin.close()
                    except OSError:
                        pass

            stdout_stream.flush()
            stderr_stream.flush()

        finished = time.monotonic()
        stdout_bytes = stdout_path.stat().st_size
        stderr_bytes = stderr_path.stat().st_size
        activity_file_bytes = _total_size(activity_paths)
        final_output_bytes = stdout_bytes + stderr_bytes + activity_file_bytes
        if final_output_bytes > max_output_bytes:
            termination_reason = "output_limit"
            for path in activity_paths:
                _truncate_file(path, max_output_bytes)
        if first_output_at is None and final_output_bytes > 0:
            first_output_at = finished
            last_output_at = finished

        if process is None:
            raise RuntimeError("Participant process was not started")

        return ProcessCapture(
            stdout=_bounded_text(stdout_path, max_output_bytes),
            stderr=_bounded_text(stderr_path, max_output_bytes),
            exit_code=process.returncode,
            termination_reason=termination_reason,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            activity_file_bytes=activity_file_bytes,
            first_output_seconds=(
                round(first_output_at - started, 3) if first_output_at is not None else None
            ),
            last_output_seconds=(
                round(last_output_at - started, 3) if last_output_at is not None else None
            ),
        )


def parse_json_text(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            candidate = "\n".join(lines[1:-1]).strip()
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def parse_codex_usage(jsonl: str) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "turn.completed"
            and isinstance(event.get("usage"), dict)
        ):
            usage = _known_numeric_fields(
                event["usage"],
                {
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                },
            )
    return {
        "usage": usage,
        "cost_usd": None,
        "cost_available": False,
        "source": "codex_jsonl",
    }


def _valid_event_identifier(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256


def _valid_event_time(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _strict_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> bool:
    keys = set(value)
    return required <= keys and keys <= required | set(optional)


def _validate_opencode_part_common(
    part: dict[str, Any],
    *,
    expected_type: str,
    top_session_id: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> str | None:
    common = {"id", "messageID", "sessionID", "type"}
    if not _strict_keys(part, required=common | required, optional=optional):
        return f"{expected_type} part keys were not allowlisted"
    if part.get("type") != expected_type:
        return f"{expected_type} part type mismatch"
    for key in ("id", "messageID", "sessionID"):
        if not _valid_event_identifier(part.get(key)):
            return f"{expected_type} part {key} was invalid"
    if part["sessionID"] != top_session_id:
        return f"{expected_type} part sessionID mismatch"
    snapshot = part.get("snapshot")
    if snapshot is not None and not _valid_event_identifier(snapshot):
        return f"{expected_type} snapshot was invalid"
    return None


def _validate_opencode_event_shape(event: dict[str, Any]) -> str | None:
    event_type = event.get("type")
    if event_type not in OPENCODE_KNOWN_EVENT_TYPES:
        return "event type is not allowlisted"

    if event_type == "error":
        if not _strict_keys(
            event,
            required={"type", "timestamp", "sessionID", "error"},
        ):
            return "error event keys were not allowlisted"
        if not _valid_event_time(event.get("timestamp")):
            return "error event timestamp was invalid"
        if not _valid_event_identifier(event.get("sessionID")):
            return "error event sessionID was invalid"
        if not isinstance(event.get("error"), dict):
            return "error event payload was not an object"
        return None

    if not _strict_keys(
        event,
        required={"type", "timestamp", "sessionID", "part"},
    ):
        return f"{event_type} event keys were not allowlisted"
    if not _valid_event_time(event.get("timestamp")):
        return f"{event_type} event timestamp was invalid"
    session_id = event.get("sessionID")
    if not _valid_event_identifier(session_id):
        return f"{event_type} event sessionID was invalid"
    part = event.get("part")
    if not isinstance(part, dict):
        return f"{event_type} event part was not an object"

    if event_type == "step_start":
        return _validate_opencode_part_common(
            part,
            expected_type="step-start",
            top_session_id=session_id,
            required=set(),
            optional={"snapshot"},
        )
    if event_type in {"text", "reasoning"}:
        error = _validate_opencode_part_common(
            part,
            expected_type=event_type,
            top_session_id=session_id,
            required={"text"},
            optional={"time"},
        )
        if error:
            return error
        if not isinstance(part.get("text"), str):
            return f"{event_type} text was not a string"
        timing = part.get("time")
        if timing is not None:
            if not isinstance(timing, dict) or not _strict_keys(
                timing, required={"start"}, optional={"end"}
            ):
                return f"{event_type} time shape was invalid"
            if any(not _valid_event_time(value) for value in timing.values()):
                return f"{event_type} time value was invalid"
        return None
    if event_type == "step_finish":
        error = _validate_opencode_part_common(
            part,
            expected_type="step-finish",
            top_session_id=session_id,
            required={"reason", "tokens", "cost"},
            optional={"snapshot"},
        )
        if error:
            return error
        if not isinstance(part.get("reason"), str) or len(part["reason"]) > 256:
            return "step-finish reason was invalid"
        tokens = part.get("tokens")
        if not isinstance(tokens, dict) or not _strict_keys(
            tokens,
            required={"input", "output", "reasoning", "cache"},
            optional={"total"},
        ):
            return "step-finish tokens shape was invalid"
        if any(
            _safe_usage_number(tokens.get(key), integer=True) is None
            for key in ("input", "output", "reasoning")
        ):
            return "step-finish token value was invalid"
        if "total" in tokens and _safe_usage_number(
            tokens.get("total"), integer=True
        ) is None:
            return "step-finish total token value was invalid"
        cache = tokens.get("cache")
        if not isinstance(cache, dict) or not _strict_keys(
            cache, required={"write", "read"}
        ):
            return "step-finish cache token shape was invalid"
        if any(
            _safe_usage_number(cache.get(key), integer=True) is None
            for key in ("write", "read")
        ):
            return "step-finish cache token value was invalid"
        if _safe_usage_number(part.get("cost")) is None:
            return "step-finish cost was invalid"
        return None
    return "allowlisted event type has no shape policy"


def parse_opencode_events(jsonl: str) -> OpenCodeEvents:
    response_parts: list[str] = []
    session_ids: set[str] = set()
    event_types: list[str] = []
    tool_events: list[str] = []
    errors: list[str] = []
    malformed_lines: list[int] = []
    protocol_errors: list[str] = []
    tokens: dict[str, Any] = {}
    cost_usd: float | None = None
    cost_status = "unavailable"

    for line_number, line in enumerate(jsonl.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed_lines.append(line_number)
            continue
        if not isinstance(event, dict):
            malformed_lines.append(line_number)
            continue

        shape_error = _validate_opencode_event_shape(event)
        if shape_error:
            protocol_errors.append(f"line {line_number}: {shape_error}")

        session_id = event.get("sessionID")
        if isinstance(session_id, str) and session_id:
            session_ids.add(session_id)
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type:
            event_types.append(event_type)
        else:
            event_type = ""
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        part_type = part.get("type") if isinstance(part.get("type"), str) else ""
        marker = f"{event_type}:{part_type}".lower()
        if "tool" in marker:
            tool_events.append(marker)
        if event_type == "error" or event.get("error"):
            # Error payloads are provider-controlled and may contain request
            # headers or credentials. Persist only a fixed label/count.
            errors.append("provider-error-event")
        if event_type == "text" and isinstance(part.get("text"), str):
            response_parts.append(part["text"])
        if event_type == "step_finish":
            if isinstance(part.get("tokens"), dict):
                token_payload = part["tokens"]
                tokens = _known_numeric_fields(
                    token_payload,
                    {"input", "output", "reasoning", "total"},
                    integer=True,
                )
                cache = token_payload.get("cache")
                if isinstance(cache, dict):
                    cache_values = _known_numeric_fields(
                        cache, {"read", "write"}, integer=True
                    )
                    if "read" in cache_values:
                        tokens["cache_read"] = cache_values["read"]
                    if "write" in cache_values:
                        tokens["cache_write"] = cache_values["write"]
            cost = part.get("cost")
            safe_cost = _safe_usage_number(cost)
            if safe_cost is not None and safe_cost > 0:
                cost_usd = float(safe_cost)
                cost_status = "reported_positive_estimate"
            elif safe_cost == 0:
                # OpenCode derives this field from catalog metadata. Several
                # paid routes report zero when their price entry is absent, so
                # zero is unknown telemetry rather than evidence of a free call.
                cost_usd = None
                cost_status = "unreported_zero_not_free"

    response_text = "".join(response_parts).strip()
    if "<tool_call>" in response_text.lower():
        tool_events.append("embedded-text:tool_call")
    known_event_types = sorted(set(event_types) & OPENCODE_KNOWN_EVENT_TYPES)
    unexpected_event_type_count = len(
        set(event_types) - OPENCODE_KNOWN_EVENT_TYPES
    )
    return OpenCodeEvents(
        response_text=response_text,
        usage={
            "usage": tokens,
            "cost_usd": cost_usd,
            "cost_available": cost_status == "reported_positive_estimate",
            "cost_status": cost_status,
            "source": "opencode_jsonl",
            "event_types": known_event_types,
            "unexpected_event_type_count": unexpected_event_type_count,
        },
        session_ids=session_ids,
        event_types=sorted(set(event_types)),
        tool_events=tool_events,
        errors=errors,
        malformed_lines=malformed_lines,
        protocol_errors=protocol_errors,
    )


def render_opencode_raw_summary(
    events: OpenCodeEvents,
    core_prompt: str,
    wrapped_prompt: str,
    secret_values: Sequence[str],
    *,
    include_response_text: bool,
) -> str:
    known_types = sorted(set(events.event_types) & OPENCODE_KNOWN_EVENT_TYPES)
    unexpected_count = len(set(events.event_types) - OPENCODE_KNOWN_EVENT_TYPES)
    payload = {
        "source": "opencode_jsonl_sanitized_summary",
        "response_text_bytes": len(events.response_text.encode("utf-8")),
        "response_text_sha256": hashlib.sha256(
            events.response_text.encode("utf-8")
        ).hexdigest(),
        "response_text": (
            _redact_sensitive_text(
                events.response_text,
                core_prompt,
                wrapped_prompt,
                secret_values,
            )
            if include_response_text
            else None
        ),
        "known_event_types": known_types,
        "unexpected_event_type_count": unexpected_count,
        "tool_event_count": len(events.tool_events),
        "error_event_count": len(events.errors),
        "malformed_line_numbers": events.malformed_lines[:100],
        "protocol_error_count": len(events.protocol_errors),
        "session_id_count": len(events.session_ids),
        "usage": events.usage,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def run_auxiliary(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float = 30.0,
    max_output_bytes: int = DEFAULT_AUX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    if not _is_finite_number(timeout) or timeout <= 0:
        raise ValueError("auxiliary timeout must be positive and finite")
    capture = run_process_monitored(
        command,
        "",
        cwd,
        env,
        timeout,
        0,
        max_output_bytes=max_output_bytes,
    )
    if capture.termination_reason == "hard_timeout":
        raise subprocess.TimeoutExpired(command, timeout)
    if capture.termination_reason == "output_limit":
        return subprocess.CompletedProcess(
            command,
            1,
            capture.stdout,
            "Auxiliary output exceeded its byte limit.",
        )
    return subprocess.CompletedProcess(
        command,
        capture.exit_code if capture.exit_code is not None else 1,
        capture.stdout,
        capture.stderr,
    )


def _parse_json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} was not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} was not a JSON object")
    return value


def opencode_preflight(
    spec: ParticipantSpec,
    executable: str,
    cwd: Path,
    env: dict[str, str],
) -> str:
    version_result = run_auxiliary([executable, "--version"], cwd, env)
    if version_result.returncode != 0 or not version_result.stdout.strip():
        raise ValueError("OpenCode version preflight failed")
    version = version_result.stdout.strip().splitlines()[-1]
    if version not in SUPPORTED_OPENCODE_CLI_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_OPENCODE_CLI_VERSIONS))
        raise ValueError(
            f"OpenCode {version!r} has no validated event contract; "
            f"install a tested version ({supported}) or update the adapter tests"
        )

    agent_result = run_auxiliary(
        [executable, "--pure", "debug", "agent", OPENCODE_AGENT], cwd, env
    )
    if agent_result.returncode != 0:
        raise ValueError("OpenCode deny-all agent preflight failed")
    agent = _parse_json_object(agent_result.stdout, "OpenCode effective agent")
    tools = agent.get("tools")
    if (
        agent.get("name") != OPENCODE_AGENT
        or agent.get("mode") != "primary"
        or not isinstance(tools, dict)
        or not tools
        or any(value is not False for value in tools.values())
    ):
        raise ValueError("OpenCode effective agent is not a deny-all primary agent")

    config_result = run_auxiliary(
        [executable, "--pure", "debug", "config"], cwd, env
    )
    if config_result.returncode != 0:
        raise ValueError("OpenCode effective configuration preflight failed")
    config = _parse_json_object(config_result.stdout, "OpenCode effective configuration")
    provider, separator, model_id = spec.model.partition("/")
    if not separator or not provider or not model_id:
        raise ValueError(f"Invalid OpenCode model route: {spec.model}")
    effective_providers = config.get("provider")
    if (
        not isinstance(effective_providers, dict)
        or set(effective_providers) != {provider}
        or config.get("enabled_providers") != [provider]
        or config.get("default_agent") != OPENCODE_AGENT
        or config.get("plugin") != []
        or config.get("mcp") != {}
        or config.get("instructions") != []
        or config.get("share") != "disabled"
        or config.get("snapshot") is not False
    ):
        raise ValueError("OpenCode effective configuration is not route-only and inert")
    try:
        settings = config["provider"][provider]["models"][model_id]["variants"][
            spec.variant
        ]
    except (KeyError, TypeError) as error:
        raise ValueError(
            f"OpenCode variant was not resolved for {spec.participant_id}"
        ) from error
    observed_effort = (
        settings.get("reasoningEffort") if isinstance(settings, dict) else None
    )
    if observed_effort != spec.reasoning_effort:
        raise ValueError(
            "OpenCode reasoning effort mismatch: "
            f"expected {spec.reasoning_effort!r}, got {observed_effort!r}"
        )
    return version


def _session_ids_from_list(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        candidates = payload.get("sessions")
    else:
        candidates = payload
    if not isinstance(candidates, list):
        raise ValueError("OpenCode session list was not an array")
    session_ids: set[str] = set()
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            raise ValueError(f"OpenCode session list item {index} was not an object")
        direct_id = item.get("id")
        alternate_id = item.get("sessionID")
        if (
            direct_id is not None
            and alternate_id is not None
            and direct_id != alternate_id
        ):
            raise ValueError(f"OpenCode session list item {index} had conflicting IDs")
        session_id = direct_id if direct_id is not None else alternate_id
        if not _valid_event_identifier(session_id):
            raise ValueError(f"OpenCode session list item {index} had no valid ID")
        session_ids.add(session_id)
    return session_ids


def _session_ids_from_list_stdout(stdout: str) -> set[str]:
    # OpenCode 1.17.11 prints no bytes, rather than `[]`, when the isolated
    # session store is empty. A successful blank response therefore proves an
    # empty list; non-empty output must still be valid JSON with the expected
    # list shape.
    if not stdout.strip():
        return set()
    return _session_ids_from_list(json.loads(stdout))


def inspect_and_cleanup_opencode_session(
    executable: str,
    session_ids: set[str],
    cwd: Path,
    env: dict[str, str],
) -> OpenCodeSessionInspection:
    # The JSONL stream is provider-controlled. Resolve every delete target
    # from the fresh isolated DB instead, then use the stream IDs only as an
    # attestation equality check. A hard cap prevents adversarial cleanup DoS;
    # the enclosing TemporaryDirectory remains the final purge boundary.
    try:
        listed = run_auxiliary(
            [executable, "session", "list", "--pure", "--format", "json"],
            cwd,
            env,
        )
        if listed.returncode != 0:
            raise ValueError("isolated session list command failed")
        store_session_ids = _session_ids_from_list_stdout(listed.stdout)
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError):
        return OpenCodeSessionInspection(
            None,
            None,
            "isolated-session-list-failed-purge-required",
            "isolated session list failed before export and cleanup",
        )

    if len(store_session_ids) > MAX_OPENCODE_SESSION_CLEANUP:
        return OpenCodeSessionInspection(
            None,
            None,
            "isolated-session-cleanup-limit-exceeded-purge-required",
            "isolated session cleanup limit exceeded",
        )

    identity_matches = (
        len(session_ids) == 1
        and len(store_session_ids) == 1
        and session_ids == store_session_ids
    )
    if identity_matches:
        identity_error = "session-cleanup"
    elif not session_ids:
        identity_error = "no-unique-session-id"
    elif len(session_ids) != 1:
        identity_error = "ambiguous-session-ids"
    elif len(store_session_ids) != 1:
        identity_error = "isolated-store-session-count-mismatch"
    else:
        identity_error = "jsonl-session-id-mismatch"

    observed_models: set[str] = set()
    observed_variants: set[str] = set()
    export_error: str | None = None if identity_matches else identity_error
    if identity_matches:
        session_id = next(iter(store_session_ids))
        try:
            exported = run_auxiliary(
                [executable, "export", session_id, "--pure", "--sanitize"], cwd, env
            )
            if exported.returncode != 0:
                export_error = "sanitized export failed"
            else:
                payload = _parse_json_object(exported.stdout, "OpenCode sanitized export")
                messages = payload.get("messages")
                if not isinstance(messages, list):
                    export_error = "sanitized export did not contain messages"
                else:
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        info = (
                            message.get("info")
                            if isinstance(message.get("info"), dict)
                            else {}
                        )
                        model = (
                            info.get("model")
                            if isinstance(info.get("model"), dict)
                            else {}
                        )
                        provider_id = model.get("providerID") or info.get("providerID")
                        model_id = model.get("modelID") or info.get("modelID")
                        if isinstance(provider_id, str) and isinstance(model_id, str):
                            observed_models.add(f"{provider_id}/{model_id}")
                        variant = info.get("variant")
                        if isinstance(variant, str) and variant:
                            observed_variants.add(variant)
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            export_error = f"sanitized export failed: {error}"

    delete_ok = True
    for session_id in sorted(store_session_ids):
        try:
            deleted = run_auxiliary(
                [executable, "session", "delete", session_id, "--pure"], cwd, env
            )
            delete_ok = delete_ok and deleted.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            delete_ok = False

    store_empty_verified = False
    try:
        after = run_auxiliary(
            [executable, "session", "list", "--pure", "--format", "json"],
            cwd,
            env,
        )
        if after.returncode == 0:
            store_empty_verified = not _session_ids_from_list_stdout(after.stdout)
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError):
        store_empty_verified = False

    if identity_matches and delete_ok and store_empty_verified:
        cleanup_status = "deleted-verified"
    elif delete_ok and store_empty_verified:
        cleanup_status = f"{identity_error}-cleaned-verified"
    else:
        cleanup_status = f"{identity_error}-purge-required"

    observed_model = next(iter(observed_models)) if len(observed_models) == 1 else None
    observed_variant = (
        next(iter(observed_variants)) if len(observed_variants) == 1 else None
    )
    if len(observed_models) != 1:
        export_error = export_error or "sanitized export had no unique observed model"
    if len(observed_variants) != 1:
        export_error = export_error or "sanitized export had no unique observed variant"
    return OpenCodeSessionInspection(
        observed_model,
        observed_variant,
        cleanup_status,
        export_error,
    )


def parse_model_output(
    model: str,
    stdout: str,
    raw_output_file: Path,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    adapter = participant_spec(model).adapter
    if adapter == "deepseek-http":
        envelope = parse_json_text(stdout)
        if envelope.get("source") != "deepseek-stateless-http-v1" or envelope.get("finish_reason") != "stop":
            raise ValueError("DeepSeek response contract or terminal stop missing")
        usage = {
            "usage": _known_numeric_fields(envelope.get("usage"),
                {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
                 "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}, integer=True),
            "cost_usd": None, "cost_available": False,
            "cost_status": "unreported_not_free", "source": "deepseek_official_http",
            "requested_effort": "max", "server_effort_attestation": "not-returned",
        }
        return envelope.get("structured_output"), usage, stdout
    if adapter == "claude":
        envelope = parse_json_text(stdout)
        if envelope.get("is_error"):
            raise ValueError(str(envelope.get("result", "Claude returned an error")))
        structured = envelope.get("structured_output")
        if not isinstance(structured, dict):
            structured = parse_json_text(str(envelope.get("result", "")))
        safe_cost = _safe_usage_number(envelope.get("total_cost_usd"))
        usage = {
            "usage": _known_numeric_fields(
                envelope.get("usage"),
                {
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "web_search_requests",
                },
                integer=True,
            ),
            "cost_usd": float(safe_cost) if safe_cost is not None else None,
            "cost_available": safe_cost is not None,
            "source": "claude_json",
        }
        return structured, usage, stdout

    if adapter == "gemini":
        envelope = parse_json_text(stdout)
        if envelope.get("error"):
            raise ValueError(f"Gemini returned an error: {envelope['error']}")
        structured = parse_json_text(str(envelope.get("response", "")))
        return structured, _sanitize_gemini_usage(envelope.get("stats")), stdout

    if adapter == "opencode":
        events = parse_opencode_events(stdout)
        return parse_json_text(events.response_text), events.usage, stdout

    raw = raw_output_file.read_text(encoding="utf-8") if raw_output_file.exists() else stdout
    return parse_json_text(raw), parse_codex_usage(stdout), raw


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def _safe_usage_number(value: Any, *, integer: bool = False) -> int | float | None:
    if not _is_finite_number(value):
        return None
    if integer and not isinstance(value, int):
        return None
    if value < 0 or value > MAX_USAGE_VALUE:
        return None
    return value


def _known_numeric_fields(
    value: Any,
    allowed: set[str],
    *,
    integer: bool = False,
) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, int | float] = {}
    for key in sorted(allowed):
        metric = _safe_usage_number(value.get(key), integer=integer)
        if metric is not None:
            output[key] = metric
    return output


def _sanitize_gemini_usage(value: Any) -> dict[str, Any]:
    token_fields = {
        "input",
        "prompt",
        "candidates",
        "total",
        "cached",
        "thoughts",
        "tool",
    }
    api_fields = {"totalRequests", "totalErrors", "totalLatencyMs"}
    token_totals = {field: 0 for field in token_fields}
    api_totals = {field: 0 for field in api_fields}
    accepted_models = 0

    models = value.get("models") if isinstance(value, dict) else None
    if isinstance(models, dict) and len(models) <= MAX_USAGE_MODELS:
        for raw_model in models.values():
            if not isinstance(raw_model, dict):
                continue
            tokens = _known_numeric_fields(
                raw_model.get("tokens"), token_fields, integer=True
            )
            api = _known_numeric_fields(raw_model.get("api"), api_fields, integer=True)
            if not tokens and not api:
                continue
            accepted_models += 1
            for key, metric in tokens.items():
                token_totals[key] += int(metric)
            for key, metric in api.items():
                api_totals[key] += int(metric)

    return {
        "usage": {
            key: metric
            for key, metric in sorted(token_totals.items())
            if metric <= MAX_USAGE_VALUE and (metric > 0 or accepted_models > 0)
        },
        "api": {
            key: metric
            for key, metric in sorted(api_totals.items())
            if metric <= MAX_USAGE_VALUE and (metric > 0 or accepted_models > 0)
        },
        "model_count": accepted_models,
        "cost_usd": None,
        "cost_available": False,
        "source": "gemini_json",
    }


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return _is_finite_number(value)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"Unsupported JSON Schema type: {expected!r}")


def _validate_schema(value: Any, schema: dict[str, Any], path: str) -> None:
    expected_types = schema.get("type")
    if expected_types is not None:
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not isinstance(expected_types, list) or not all(
            isinstance(item, str) for item in expected_types
        ):
            raise ValueError(f"Invalid bundled schema type at {path}")
        if not any(_matches_json_type(value, item) for item in expected_types):
            expected = " or ".join(expected_types)
            raise ValueError(f"{path}: expected {expected}")

    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: value does not match const")
    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or value not in choices:
            raise ValueError(f"{path}: value is not in enum")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError(f"Invalid bundled object schema at {path}")
        if not all(isinstance(field, str) for field in required):
            raise ValueError(f"Invalid bundled required fields at {path}")

        missing = [field for field in required if field not in value]
        if missing:
            raise ValueError(f"{path}: missing required field {missing[0]!r}")

        for field, item in value.items():
            item_path = f"{path}.{field}"
            if field in properties:
                field_schema = properties[field]
                if not isinstance(field_schema, dict):
                    raise ValueError(f"Invalid bundled schema at {item_path}")
                _validate_schema(item, field_schema, item_path)
            elif additional is False:
                raise ValueError(f"{path}: unexpected field {field!r}")
            elif isinstance(additional, dict):
                _validate_schema(item, additional, item_path)
            elif additional is not True:
                raise ValueError(f"Invalid additionalProperties at {path}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path}: too many items")
        items = schema.get("items")
        if items is False and value:
            raise ValueError(f"{path}: items are not allowed")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_schema(item, items, f"{path}[{index}]")
        elif items not in (None, True):
            raise ValueError(f"Invalid bundled array schema at {path}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path}: string is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path}: string is too long")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not _is_finite_number(value):
            raise ValueError(f"{path}: number must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path}: value is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path}: value is above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{path}: value is below exclusive minimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ValueError(f"{path}: value is above exclusive maximum")


def validate_structured(data: dict[str, Any]) -> None:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise ValueError(f"Could not load bundled participant schema: {error}") from error
    if not isinstance(schema, dict):
        raise ValueError("Bundled participant schema must be an object")
    _validate_schema(data, schema, "$")


def attest_host_position(path: Path | None) -> HostPositionAttestation:
    if path is None:
        return HostPositionAttestation(
            file=None,
            sha256=None,
            validated=False,
            status="not_provided",
            error=None,
        )

    resolved = path.expanduser().resolve()
    try:
        size = resolved.stat().st_size
        if size > MAX_HOST_POSITION_BYTES:
            return HostPositionAttestation(
                file=str(resolved),
                sha256=None,
                validated=False,
                status="invalid",
                error="host position exceeds the configured byte limit",
            )
        raw = resolved.read_bytes()
    except OSError:
        return HostPositionAttestation(
            file=str(resolved),
            sha256=None,
            validated=False,
            status="invalid",
            error="host position could not be read",
        )

    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
        data = _parse_json_object(text, "host position")
        validate_structured(data)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return HostPositionAttestation(
            file=str(resolved),
            sha256=digest,
            validated=False,
            status="invalid",
            error="host position is not valid participant-schema JSON",
        )
    return HostPositionAttestation(
        file=str(resolved),
        sha256=digest,
        validated=True,
        status="validated",
        error=None,
    )


def render_markdown(model: str, data: dict[str, Any]) -> str:
    lines = [
        f"# {model.title()} Position",
        "",
        "## Recommendation",
        str(data["recommendation"]),
        "",
        f"**Confidence:** {float(data['confidence']):.2f}",
        "",
        "## Analysis",
        str(data["analysis"]),
        "",
        "## Assumptions",
    ]
    lines.extend(f"- {item}" for item in data["assumptions"])
    lines.extend(["", "## Evidence"])
    for item in data["evidence"]:
        if isinstance(item, dict):
            lines.append(
                f"- **{item.get('claim', '')}**: {item.get('support', '')} "
                f"(Verify: {item.get('verification', '')})"
            )
        else:
            lines.append(f"- {item}")
    lines.extend(["", "## Risks"])
    lines.extend(f"- {item}" for item in data["risks"])
    lines.extend(["", "## What Would Change This View"])
    lines.extend(f"- {item}" for item in data["change_conditions"])
    return "\n".join(lines).rstrip() + "\n"


def display_command(model: str, command: list[str]) -> list[str]:
    visible = command.copy()
    if participant_spec(model).adapter == "claude" and "--json-schema" in visible:
        index = visible.index("--json-schema") + 1
        visible[index] = str(SCHEMA_PATH)
    return visible


def classify_failure(raw: str, stderr: str) -> str:
    message = f"{raw}\n{stderr}".lower()
    unavailable_markers = (
        "insufficient balance",
        "failed to authenticate",
        "authentication",
        "rate limit",
        "quota",
    )
    if any(marker in message for marker in unavailable_markers):
        return "unavailable"
    return "error"


def diagnose_failure(
    raw: str,
    stderr: str,
    termination_reason: str | None,
    output_bytes: int,
    parse_error: str,
) -> tuple[str | None, str | None]:
    """Add an actionable diagnosis without pretending every failure is remote."""
    if termination_reason == "output_limit":
        return (
            "output_limit",
            "The participant exceeded the configured output byte limit. Inspect the "
            "bounded raw artifact; truncated output is never accepted as structured data.",
        )

    message = f"{raw}\n{stderr}".lower()
    network_markers = (
        "connectionrefused",
        "connection refused",
        "econnrefused",
        "network is unreachable",
        "temporary failure in name resolution",
        "name or service not known",
        "could not resolve host",
        "enotfound",
    )
    authentication_markers = (
        "failed to authenticate",
        "authentication",
        "unauthorized",
        "invalid api key",
    )
    quota_markers = (
        "insufficient balance",
        "rate limit",
        "quota",
    )

    if any(marker in message for marker in network_markers):
        return (
            "network_environment",
            "The current execution environment may block model-network access. "
            "Do not only lengthen the timeout: use a minimal no-tools health probe, "
            "then retry outside a restricted sandbox only with the required user approval.",
        )
    if any(marker in message for marker in authentication_markers):
        return "authentication", "Repair CLI authentication before retrying the council."
    if any(marker in message for marker in quota_markers):
        return "quota_or_rate_limit", "Resolve quota, balance, or rate limits before retrying."
    if termination_reason and output_bytes == 0:
        return (
            "zero_output_timeout",
            "No bytes were observed. This can be a blocked environment or a healthy "
            "final-only provider; verify with a minimal probe before changing timeout policy.",
        )
    if termination_reason:
        return (
            "timeout_after_output",
            "The participant emitted output before termination. Keep a valid partial result; "
            "retry only if its coverage is insufficient.",
        )
    if parse_error:
        return "invalid_output", "Inspect the raw response and schema compatibility before retrying."
    return None, None


def read_cli_version(executable: str, cwd: Path, env: dict[str, str]) -> str | None:
    try:
        completed = run_auxiliary([executable, "--version"], cwd, env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0 or not text:
        return None
    return text.splitlines()[-1][:500]


def infer_direct_observed_model(spec: ParticipantSpec, stdout: str) -> str | None:
    candidates: set[str] = set()
    if spec.adapter == "deepseek-http":
        try:
            value = parse_json_text(stdout).get("model")
            return value if isinstance(value, str) else None
        except (json.JSONDecodeError, ValueError):
            return None
    if spec.adapter == "claude":
        try:
            envelope = parse_json_text(stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        model_usage = envelope.get("modelUsage")
        if isinstance(model_usage, dict):
            for key, value in model_usage.items():
                if isinstance(key, str) and key:
                    candidates.add(key)
                if isinstance(value, dict):
                    canonical = value.get("canonicalModel")
                    if isinstance(canonical, str) and canonical:
                        candidates.add(canonical)
        model = envelope.get("model")
        if isinstance(model, str) and model:
            candidates.add(model)
    elif spec.adapter == "codex":
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            for key in ("model", "model_id", "modelID"):
                value = event.get(key)
                if isinstance(value, str) and value:
                    candidates.add(value)
    elif spec.adapter == "gemini":
        try:
            envelope = parse_json_text(stdout)
        except (json.JSONDecodeError, ValueError):
            return None
        stats = envelope.get("stats")
        if isinstance(stats, dict):
            models = stats.get("models")
            if isinstance(models, dict):
                candidates.update(key for key in models if isinstance(key, str) and key)
    return next(iter(candidates)) if len(candidates) == 1 else None


def direct_model_attestation_error(spec: ParticipantSpec, stdout: str) -> str | None:
    if spec.adapter == "deepseek-http":
        return (None if infer_direct_observed_model(spec, stdout) == spec.model
                else "DeepSeek response model did not match the exact requested route")
    if spec.model not in REQUIRED_DIRECT_MODEL_ATTESTATION:
        return None
    try:
        envelope = parse_json_text(stdout)
    except (json.JSONDecodeError, ValueError):
        return "direct model attestation envelope was missing or invalid"
    model_usage = envelope.get("modelUsage")
    if not isinstance(model_usage, dict) or set(model_usage) != {spec.model}:
        return (
            "direct model attestation modelUsage keys did not exactly match "
            f"{spec.model!r}"
        )
    usage_row = model_usage.get(spec.model)
    if (
        not isinstance(usage_row, dict)
        or usage_row.get("canonicalModel") != spec.model
    ):
        return (
            "direct model attestation canonicalModel did not exactly match "
            f"{spec.model!r}"
        )
    envelope_model = envelope.get("model")
    if envelope_model is not None and envelope_model != spec.model:
        return (
            "direct model attestation top-level model did not exactly match "
            f"{spec.model!r}"
        )
    return None


def _result_metadata(spec: ParticipantSpec) -> dict[str, Any]:
    return {
        "participant_id": spec.participant_id,
        "adapter": spec.adapter,
        "requested_model": spec.model,
        "requested_variant": spec.variant,
        "reasoning_effort": spec.reasoning_effort,
        "provider_family": spec.provider_family,
        "transport_domain": spec.transport_domain,
        "role": spec.role,
    }


def _json_escaped_forms(value: str) -> set[str]:
    return {
        json.dumps(value, ensure_ascii=False)[1:-1],
        json.dumps(value, ensure_ascii=True)[1:-1],
    }


def _redact_sensitive_text(
    text: str,
    core_prompt: str,
    wrapped_prompt: str,
    secret_values: Sequence[str] = (),
) -> str:
    replacements: dict[str, str] = {}
    for sensitive in (wrapped_prompt, core_prompt):
        if sensitive:
            replacements[sensitive] = "<PROMPT REDACTED>"
            for escaped in _json_escaped_forms(sensitive):
                if escaped:
                    replacements[escaped] = "<PROMPT REDACTED>"
    for sensitive in secret_values:
        if isinstance(sensitive, str) and len(sensitive) >= 4:
            replacements[sensitive] = "<CREDENTIAL REDACTED>"
            for escaped in _json_escaped_forms(sensitive):
                if escaped:
                    replacements[escaped] = "<CREDENTIAL REDACTED>"

    sanitized = text
    for sensitive in sorted(replacements, key=len, reverse=True):
        sanitized = sanitized.replace(sensitive, replacements[sensitive])
    return sanitized


def _redact_json_value(
    value: Any,
    core_prompt: str,
    wrapped_prompt: str,
    secret_values: Sequence[str],
) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(
            value, core_prompt, wrapped_prompt, secret_values
        )
    if isinstance(value, list):
        return [
            _redact_json_value(item, core_prompt, wrapped_prompt, secret_values)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _redact_json_value(item, core_prompt, wrapped_prompt, secret_values)
            for key, item in value.items()
        }
    return value


def _secret_values_from_env(spec: ParticipantSpec, env: dict[str, str]) -> tuple[str, ...]:
    names: set[str] = set()
    if spec.adapter == "deepseek-http":
        names.add("DEEPSEEK_API_KEY")
    if spec.adapter == "opencode":
        route = _opencode_route(spec)
        names.update(OPENCODE_ROUTE_ENV_PREFERENCE.get(route, ()))
        names.add("GOOGLE_GENERATIVE_AI_API_KEY")
    elif spec.adapter == "claude":
        names.update((*CLAUDE_GATEWAY_PAIR, "ANTHROPIC_API_KEY"))
    elif spec.adapter == "codex":
        names.update(PROVIDER_ENV_ALLOWLIST["codex"])
    elif spec.adapter == "gemini":
        names.update(PROVIDER_ENV_ALLOWLIST["gemini"])

    values = {env[name] for name in names if env.get(name)}
    inline_auth = env.get(OPENCODE_AUTH_CONTENT_ENV)
    if inline_auth:
        try:
            auth = json.loads(inline_auth)
        except json.JSONDecodeError:
            auth = {}
        if isinstance(auth, dict):
            for credential in auth.values():
                if isinstance(credential, dict):
                    key = credential.get("key")
                    if isinstance(key, str) and key:
                        values.add(key)
    return tuple(sorted(values, key=len, reverse=True))


def _all_known_secret_values(
    extra_values: Sequence[str] = (),
) -> tuple[str, ...]:
    names = {
        "ANTHROPIC_API_KEY",
        *CLAUDE_GATEWAY_PAIR,
        *(name for values in PROVIDER_ENV_ALLOWLIST.values() for name in values),
        *(name for values in OPENCODE_ROUTE_ENV_PREFERENCE.values() for name in values),
    }
    values = {
        os.environ[name]
        for name in names
        if isinstance(os.environ.get(name), str) and len(os.environ[name]) >= 4
    }
    values.update(
        value for value in extra_values if isinstance(value, str) and len(value) >= 4
    )
    inline_auth = os.environ.get(OPENCODE_AUTH_CONTENT_ENV)
    if inline_auth:
        values.add(inline_auth)
        try:
            payload = json.loads(inline_auth)
        except json.JSONDecodeError:
            payload = None

        def collect(item: Any, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(item, str) and len(item) >= 4:
                values.add(item)
            elif isinstance(item, list):
                for child in item[:64]:
                    collect(child, depth + 1)
            elif isinstance(item, dict):
                for child in list(item.values())[:64]:
                    collect(child, depth + 1)

        collect(payload)
    return tuple(sorted(values, key=len, reverse=True))


def _sanitize_result(
    result: Result,
    core_prompt: str,
    wrapped_prompt: str,
    secret_values: Sequence[str],
) -> Result:
    sanitized = _redact_json_value(
        asdict(result), core_prompt, wrapped_prompt, secret_values
    )
    return Result(**sanitized)


def _sanitize_manifest(
    manifest: dict[str, Any],
    core_prompt: str,
    participant_ids: Sequence[str],
    extra_secret_values: Sequence[str] = (),
) -> dict[str, Any]:
    secret_values = _all_known_secret_values(extra_secret_values)
    sanitized = _redact_json_value(manifest, core_prompt, "", secret_values)
    for participant_id in participant_ids:
        try:
            wrapped = participant_prompt(participant_id, core_prompt)
        except (OSError, TypeError, ValueError):
            continue
        sanitized = _redact_json_value(sanitized, "", wrapped, secret_values)
    if not isinstance(sanitized, dict):
        raise ValueError("Sanitized manifest was not an object")
    return sanitized


def run_model(
    model: str,
    prompt: str,
    output_dir: Path,
    timeout: float,
    silence_timeout: float,
    cwd: Path,
    dry_run: bool,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    provider_env: Mapping[str, str] | None = None,
) -> Result:
    spec = participant_spec(model)
    output_file = output_dir / f"{model}.md"
    structured_file = output_dir / f"{model}.json"
    raw_file = output_dir / f"{model}.raw.txt"
    generated_artifacts = (output_file, structured_file, raw_file)
    command = command_for(model, raw_file)
    started = time.monotonic()
    configured_activity = {
        "hard_timeout_seconds": timeout,
        "silence_timeout_seconds": silence_timeout or None,
        "max_output_bytes": max_output_bytes,
    }

    # A real rerun into the same round directory must not inherit an earlier
    # participant result, including when the requested CLI is now unavailable.
    # Dry runs remain non-mutating.
    if not dry_run:
        remove_generated_artifacts(generated_artifacts)

    command_path = Path(command[0])
    command_available = (
        command_path.is_file() and os.access(command_path, os.X_OK)
        if command_path.is_absolute()
        else shutil.which(command[0]) is not None
    )
    if not command_available:
        return Result(
            model=model,
            status="missing",
            exit_code=None,
            duration_seconds=0.0,
            output_file=None,
            structured_file=None,
            raw_file=None,
            parse_ok=False,
            usage={},
            stderr=f"CLI not found: {command[0]}",
            command=display_command(model, command),
            termination_reason=None,
            activity=configured_activity,
            failure_category="missing_cli",
            diagnostic_hint=f"Install or expose the {command[0]} CLI before retrying.",
            session_cleanup_status=(
                "not-created-missing-cli" if spec.adapter == "opencode" else None
            ),
            **_result_metadata(spec),
        )

    if dry_run:
        return Result(
            model=model,
            status="dry-run",
            exit_code=None,
            duration_seconds=0.0,
            output_file=str(output_file),
            structured_file=str(structured_file),
            raw_file=str(raw_file),
            parse_ok=False,
            usage={},
            stderr="",
            command=display_command(model, command),
            termination_reason=None,
            activity=configured_activity,
            failure_category=None,
            diagnostic_hint=None,
            session_cleanup_status=(
                "not-started-dry-run" if spec.adapter == "opencode" else None
            ),
            **_result_metadata(spec),
        )

    wrapped_prompt = participant_prompt(model, prompt)
    try:
        env = clean_env(model, provider_env)
    except (OSError, TypeError, ValueError) as error:
        if spec.adapter not in {"opencode", "deepseek-http"}:
            raise
        known_secrets = _all_known_secret_values(
            tuple(provider_env.values()) if provider_env else ()
        )
        safe_error = _redact_sensitive_text(
            str(error), prompt, wrapped_prompt, known_secrets
        )
        result = Result(
            model=model,
            status="error",
            exit_code=None,
            duration_seconds=round(time.monotonic() - started, 3),
            output_file=None,
            structured_file=None,
            raw_file=None,
            parse_ok=False,
            usage={},
            stderr=f"Provider isolation preflight failed: {safe_error}"[-3000:],
            command=display_command(model, command),
            termination_reason=None,
            activity=configured_activity,
            failure_category="safety_preflight",
            diagnostic_hint=(
                "Refusing to launch without a route-only credential and isolated "
                "provider configuration. Configure the requested provider and retry."
            ),
            session_cleanup_status="not-created-isolation-preflight-failed",
            **_result_metadata(spec),
        )
        return _sanitize_result(result, prompt, wrapped_prompt, known_secrets)
    secret_values = tuple(
        dict.fromkeys(
            (
                *_secret_values_from_env(spec, env),
                *(provider_env.values() if provider_env else ()),
            )
        )
    )
    cli_version: str | None = None
    observed_model: str | None = None
    observed_variant: str | None = None
    session_cleanup_status: str | None = None
    opencode_protocol_errors: list[str] = []
    opencode_events: OpenCodeEvents | None = None
    direct_attestation_error: str | None = None

    with contextlib.ExitStack() as stack:
        process_cwd = cwd
        if spec.adapter == "opencode":
            isolated = stack.enter_context(
                tempfile.TemporaryDirectory(prefix=f"model-council-{model}-")
            )
            process_cwd = Path(isolated)
            try:
                configure_opencode_isolation(env, process_cwd)
                cli_version = opencode_preflight(spec, command[0], process_cwd, env)
            except (OSError, subprocess.TimeoutExpired, TypeError, ValueError) as error:
                safe_error = _redact_sensitive_text(
                    str(error), prompt, wrapped_prompt, secret_values
                )
                result = Result(
                    model=model,
                    status="error",
                    exit_code=None,
                    duration_seconds=round(time.monotonic() - started, 3),
                    output_file=None,
                    structured_file=None,
                    raw_file=None,
                    parse_ok=False,
                    usage={},
                    stderr=f"OpenCode safety preflight failed: {safe_error}"[-3000:],
                    command=display_command(model, command),
                    termination_reason=None,
                    activity=configured_activity,
                    failure_category="safety_preflight",
                    diagnostic_hint=(
                        "Refusing to launch until the effective OpenCode agent is deny-all "
                        "and the requested variant has the exact configured reasoning effort."
                    ),
                    cli_version=cli_version,
                    session_cleanup_status="not-created-preflight-failed",
                    **_result_metadata(spec),
                )
                return _sanitize_result(
                    result, prompt, wrapped_prompt, secret_values
                )
        elif spec.adapter == "deepseek-http":
            cli_version = "deepseek-stateless-http-v1"
            session_cleanup_status = "stateless-no-local-session"
        else:
            cli_version = read_cli_version(command[0], process_cwd, env)

        try:
            capture = run_process_monitored(
                command,
                wrapped_prompt,
                process_cwd,
                env,
                timeout,
                silence_timeout,
                activity_paths=(raw_file,),
                max_output_bytes=max_output_bytes,
            )
        except Exception as error:
            safe_error = _redact_sensitive_text(
                f"{type(error).__name__}: {error}",
                prompt,
                wrapped_prompt,
                secret_values,
            )
            result = Result(
                model=model,
                status="error",
                exit_code=None,
                duration_seconds=round(time.monotonic() - started, 3),
                output_file=None,
                structured_file=None,
                raw_file=None,
                parse_ok=False,
                usage={},
                stderr=f"Participant process failure: {safe_error}"[-3000:],
                command=display_command(model, command),
                termination_reason=None,
                activity=configured_activity,
                failure_category="runner_exception",
                diagnostic_hint=(
                    "The participant process could not be launched or monitored safely. "
                    "Its isolated runtime store was purged before returning."
                ),
                cli_version=cli_version,
                session_cleanup_status=(
                    "isolated-store-purged-after-runner-exception"
                    if spec.adapter == "opencode"
                    else None
                ),
                **_result_metadata(spec),
            )
            return _sanitize_result(result, prompt, wrapped_prompt, secret_values)
        stdout = capture.stdout.strip()

        if spec.adapter == "opencode":
            opencode_events = parse_opencode_events(stdout)
            try:
                inspection = inspect_and_cleanup_opencode_session(
                    command[0], opencode_events.session_ids, process_cwd, env
                )
            except (OSError, subprocess.TimeoutExpired, TypeError, ValueError) as error:
                inspection = OpenCodeSessionInspection(
                    None,
                    None,
                    "cleanup-inspection-failed",
                    f"OpenCode session inspection failed: {error}",
                )
            observed_model = inspection.observed_model
            observed_variant = inspection.observed_variant
            session_cleanup_status = inspection.cleanup_status
            if opencode_events.malformed_lines:
                opencode_protocol_errors.append(
                    "malformed JSONL lines: "
                    + ",".join(
                        str(item) for item in opencode_events.malformed_lines[:20]
                    )
                )
            if opencode_events.protocol_errors:
                opencode_protocol_errors.append(
                    "strict event allowlist violations: "
                    + "; ".join(opencode_events.protocol_errors[:20])
                )
            if opencode_events.errors:
                opencode_protocol_errors.append(
                    f"error event count: {len(opencode_events.errors)}"
                )
            if opencode_events.tool_events:
                opencode_protocol_errors.append(
                    f"tool event count: {len(opencode_events.tool_events)}"
                )
            if len(opencode_events.session_ids) != 1:
                opencode_protocol_errors.append(
                    "OpenCode did not emit one unique sessionID"
                )
            if inspection.export_error:
                opencode_protocol_errors.append(inspection.export_error)
            if observed_model != spec.model:
                opencode_protocol_errors.append(
                    f"observed route mismatch: expected {spec.model!r}, "
                    f"got {observed_model!r}"
                )
            if observed_variant != spec.variant:
                opencode_protocol_errors.append(
                    f"observed variant mismatch: expected {spec.variant!r}, "
                    f"got {observed_variant!r}"
                )
            if session_cleanup_status != "deleted-verified":
                opencode_protocol_errors.append(
                    "session cleanup was not deleted-verified: "
                    f"{session_cleanup_status}"
                )
            if not opencode_events.response_text:
                opencode_protocol_errors.append("OpenCode emitted no text response")

    if spec.adapter != "opencode":
        observed_model = infer_direct_observed_model(spec, stdout)
    diagnostic_stderr = _redact_sensitive_text(
        capture.stderr.strip(), prompt, wrapped_prompt, secret_values
    )
    if spec.adapter == "opencode" and capture.stderr.strip():
        stderr = f"OpenCode stderr content withheld ({capture.stderr_bytes} bytes)"
    else:
        stderr = diagnostic_stderr
    parse_ok = False
    usage: dict[str, Any] = {}
    parse_error = ""
    structured: dict[str, Any] | None = None
    raw = stdout
    if capture.termination_reason == "output_limit":
        if raw_file.exists() and raw_file.stat().st_size:
            raw = raw_file.read_text(encoding="utf-8", errors="replace")
        parse_error = (
            f"Output exceeded {max_output_bytes} bytes; parsing truncated output is disabled"
        )
    else:
        try:
            if spec.adapter == "opencode":
                if opencode_events is None:
                    raise ValueError("OpenCode JSONL was not inspected")
                if opencode_protocol_errors:
                    raise ValueError("; ".join(opencode_protocol_errors))
                structured = parse_json_text(opencode_events.response_text)
                usage = opencode_events.usage
                raw = stdout
            else:
                structured, usage, raw = parse_model_output(model, stdout, raw_file)
            structured = _redact_json_value(
                structured, prompt, wrapped_prompt, secret_values
            )
            validate_structured(structured)
            if spec.adapter != "opencode":
                # Identity attestation is a gate on an otherwise usable direct
                # response. It must not mask a network, quota, timeout, output
                # limit, or malformed-response diagnosis that failed earlier.
                direct_attestation_error = direct_model_attestation_error(
                    spec, stdout
                )
                if direct_attestation_error:
                    raise ValueError(direct_attestation_error)
            parse_ok = True
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
            parse_error = str(error)

    if spec.adapter == "opencode" and opencode_events is not None and stdout.strip():
        # Never persist provider JSONL text fragments. A prompt or credential
        # split across streaming events cannot be safely removed with exact
        # per-event replacement. Keep a canonical, content-safe protocol
        # summary built only after response text has been reassembled.
        raw_for_disk = render_opencode_raw_summary(
            opencode_events,
            prompt,
            wrapped_prompt,
            secret_values,
            include_response_text=(
                capture.termination_reason != "output_limit"
                and not opencode_events.malformed_lines
            ),
        )
        raw_file.write_text(raw_for_disk.strip() + "\n", encoding="utf-8")
        _truncate_file(raw_file, max_output_bytes)
    elif raw.strip():
        raw_for_disk = _redact_sensitive_text(raw, prompt, wrapped_prompt, secret_values)
        raw_file.write_text(raw_for_disk.strip() + "\n", encoding="utf-8")
        _truncate_file(raw_file, max_output_bytes)
    if parse_ok and structured is not None:
        structured_file.write_text(
            json.dumps(structured, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        output_file.write_text(render_markdown(model, structured), encoding="utf-8")

    remove_empty_artifacts(generated_artifacts)

    if capture.exit_code == 0 and parse_ok and capture.termination_reason is None:
        status = "ok"
    elif parse_ok:
        status = "partial"
    elif capture.termination_reason == "hard_timeout":
        status = "timeout"
    elif capture.termination_reason == "silence_timeout":
        status = "silent-timeout"
    elif capture.termination_reason == "output_limit":
        status = "output-limit"
    else:
        status = classify_failure(raw, diagnostic_stderr)

    error_details = (
        stderr[-3000:]
        if stderr and (capture.exit_code or capture.termination_reason is not None)
        else ""
    )
    if capture.termination_reason == "hard_timeout":
        error_details = f"{error_details}\nTimed out after {timeout:g}s".strip()
    elif capture.termination_reason == "silence_timeout":
        error_details = (
            f"{error_details}\nNo stdout/stderr activity for {silence_timeout:g}s"
        ).strip()
    elif capture.termination_reason == "output_limit":
        error_details = (
            f"{error_details}\nOutput exceeded {max_output_bytes} bytes; process terminated"
        ).strip()
    if parse_error:
        error_details = f"{error_details}\nParse error: {parse_error}".strip()
    error_details = _redact_sensitive_text(
        error_details, prompt, wrapped_prompt, secret_values
    )

    activity = {
        **configured_activity,
        "stdout_bytes": capture.stdout_bytes,
        "stderr_bytes": capture.stderr_bytes,
        "activity_file_bytes": capture.activity_file_bytes,
        "first_output_seconds": capture.first_output_seconds,
        "last_output_seconds": capture.last_output_seconds,
    }
    observed_output_bytes = (
        capture.stdout_bytes + capture.stderr_bytes + capture.activity_file_bytes
    )
    failure_category: str | None = None
    diagnostic_hint: str | None = None
    if status != "ok":
        failure_category, diagnostic_hint = diagnose_failure(
            raw,
            diagnostic_stderr,
            capture.termination_reason,
            observed_output_bytes,
            parse_error,
        )
        if direct_attestation_error:
            failure_category = "model_attestation"
            diagnostic_hint = (
                "The direct provider did not prove the exact pinned model. "
                "Do not count this response as a usable council seat."
            )
        if (
            spec.adapter == "opencode"
            and opencode_protocol_errors
            and capture.termination_reason != "output_limit"
        ):
            failure_category = "opencode_protocol"
            diagnostic_hint = (
                "OpenCode output is fail-closed: inspect the sanitized protocol summary "
                "and repair tool policy, route/variant attestation, or verified session "
                "cleanup."
            )

    result = Result(
        model=model,
        status=status,
        exit_code=capture.exit_code,
        duration_seconds=round(time.monotonic() - started, 3),
        output_file=str(output_file) if output_file.exists() else None,
        structured_file=str(structured_file) if structured_file.exists() else None,
        raw_file=str(raw_file) if raw_file.exists() else None,
        parse_ok=parse_ok,
        usage=usage,
        stderr=error_details,
        command=display_command(model, command),
        termination_reason=capture.termination_reason,
        activity=activity,
        failure_category=failure_category,
        diagnostic_hint=diagnostic_hint,
        observed_model=observed_model,
        observed_variant=observed_variant,
        cli_version=cli_version,
        session_cleanup_status=session_cleanup_status,
        **_result_metadata(spec),
    )
    return _sanitize_result(result, prompt, wrapped_prompt, secret_values)


def runner_exception_result(
    model: str,
    error: Exception,
    output_dir: Path,
    timeout: float,
    silence_timeout: float,
    max_output_bytes: int,
    prompt: str = "",
    provider_env: Mapping[str, str] | None = None,
) -> Result:
    spec: ParticipantSpec | None = None
    try:
        spec = participant_spec(model)
        metadata = _result_metadata(spec)
    except Exception:
        metadata = {"participant_id": model}
    try:
        command = display_command(
            model, command_for(model, output_dir / f"{model}.raw.txt")
        )
    except Exception:
        command = []
    wrapped_prompt = ""
    secret_values: tuple[str, ...] = _all_known_secret_values(
        tuple(provider_env.values()) if provider_env else ()
    )
    try:
        if prompt:
            wrapped_prompt = participant_prompt(model, prompt)
        exception_env = clean_env(model, provider_env)
        if spec is not None:
            secret_values = tuple(
                dict.fromkeys(
                    (*secret_values, *_secret_values_from_env(spec, exception_env))
                )
            )
    except Exception:
        pass
    message = _redact_sensitive_text(
        f"Runner exception ({type(error).__name__}): {error}",
        prompt,
        wrapped_prompt,
        secret_values,
    )
    result = Result(
        model=model,
        status="error",
        exit_code=None,
        duration_seconds=0.0,
        output_file=None,
        structured_file=None,
        raw_file=None,
        parse_ok=False,
        usage={},
        stderr=message[-3000:],
        command=command,
        termination_reason=None,
        activity={
            "hard_timeout_seconds": timeout,
            "silence_timeout_seconds": silence_timeout or None,
            "max_output_bytes": max_output_bytes,
        },
        failure_category="runner_exception",
        diagnostic_hint=(
            "The local participant runner failed. Inspect stderr and retry only after "
            "repairing the process-launch, cleanup, or filesystem error."
        ),
        **metadata,
    )
    return _sanitize_result(result, prompt, wrapped_prompt, secret_values)


def _parse_participant_ids(value: str, *, option: str) -> list[str]:
    raw_items = [item.strip().lower() for item in value.split(",")]
    if not raw_items or any(not item for item in raw_items):
        raise ValueError(f"{option} requires a comma-separated list without empty entries")
    for item in raw_items:
        if not PARTICIPANT_SLUG.fullmatch(item):
            raise ValueError(f"Invalid participant id for {option}: {item!r}")
    return list(dict.fromkeys(raw_items))


def parse_models(value: str | None, host: str) -> list[str]:
    if value is not None:
        models = _parse_participant_ids(value, option="--models")
        unknown = sorted(set(models) - set(MODELS))
        if unknown:
            raise ValueError(f"Unknown models: {', '.join(unknown)}")
        if host in models:
            raise ValueError(f"Refusing to call current host through CLI: {host}")
        return models
    return [model for model in MODELS if model != host]


def parse_participants(value: str, host: str) -> list[str]:
    participant_ids = _parse_participant_ids(value, option="--participants")
    config = load_council_config()
    known = set(config["participants"])
    unknown = sorted(set(participant_ids) - known)
    if unknown:
        raise ValueError(f"Unknown participants: {', '.join(unknown)}")

    host_family = participant_spec(host).provider_family
    seen_families: dict[str, str] = {}
    for participant_id in participant_ids:
        spec = participant_spec(participant_id)
        if spec.provider_family == host_family:
            raise ValueError(
                "Refusing a participant from the current host provider family: "
                f"{participant_id} ({host_family})"
            )
        previous = seen_families.get(spec.provider_family)
        if previous is not None:
            raise ValueError(
                "Participants must have distinct provider families: "
                f"{previous} and {participant_id} are both {spec.provider_family}"
            )
        seen_families[spec.provider_family] = participant_id
    return participant_ids


def participants_for_profile(profile: str, host: str) -> list[str]:
    config = load_council_config()
    profiles = config["profiles"]
    raw_profile = profiles.get(profile)
    if not isinstance(raw_profile, dict):
        raise ValueError(f"Unknown profile: {profile}")
    total_seats = raw_profile.get("total_seats")
    if not isinstance(total_seats, int) or total_seats < 2:
        raise ValueError(f"Invalid total_seats for profile: {profile}")
    host_family = participant_spec(host).provider_family

    if profile == "legacy":
        candidates = raw_profile.get("participants")
    else:
        candidates = raw_profile.get("priority", config.get("priority"))
    if not isinstance(candidates, list):
        raise ValueError(f"Profile {profile} has no participant candidates")

    selected: list[str] = []
    seen_families = {host_family}
    for participant_id in candidates:
        if not isinstance(participant_id, str) or not PARTICIPANT_SLUG.fullmatch(
            participant_id
        ):
            raise ValueError(f"Invalid participant id in profile {profile}")
        spec = participant_spec(participant_id)
        if spec.provider_family in seen_families:
            continue
        selected.append(participant_id)
        seen_families.add(spec.provider_family)
        if len(selected) == total_seats - 1:
            break
    if len(selected) != total_seats - 1:
        raise ValueError(
            f"Profile {profile} cannot fill {total_seats} provider-distinct seats"
        )
    return selected


def resolve_selection(
    host: str,
    *,
    profile: str | None,
    participants: str | None,
    models: str | None,
) -> tuple[list[str], str, str]:
    selected_options = sum(value is not None for value in (profile, participants, models))
    if selected_options > 1:
        raise ValueError("--profile, --participants, and --models are mutually exclusive")
    if profile is not None:
        return participants_for_profile(profile, host), profile, f"profile:{profile}"
    if participants is not None:
        return parse_participants(participants, host), "custom", "participants"
    if models is not None:
        return parse_models(models, host), "legacy", "models"
    implicit_profile = load_council_config().get("implicit_cli_profile")
    if implicit_profile != "legacy":
        raise ValueError("Compatibility default must remain the legacy profile")
    return (
        participants_for_profile(implicit_profile, host),
        implicit_profile,
        "implicit_legacy",
    )


def build_profile_health(
    profile: str,
    host: str,
    participant_ids: list[str],
    results: list[Result],
    *,
    dry_run: bool,
    host_position: HostPositionAttestation,
    external_collection_started: bool = True,
) -> dict[str, Any]:
    config = load_council_config()
    raw_profile = config["profiles"].get(profile)
    target_total_seats = (
        raw_profile.get("total_seats") if isinstance(raw_profile, dict) else None
    )
    host_spec = participant_spec(host)
    usable = [result for result in results if result.parse_ok]
    host_position_required = bool(
        isinstance(raw_profile, dict)
        and raw_profile.get("requires_validated_host_position") is True
    )
    host_seat_usable = not host_position_required or host_position.validated
    effective_families = {
        result.provider_family for result in usable if result.provider_family
    }
    effective_transports = {
        result.transport_domain for result in usable if result.transport_domain
    }
    if host_seat_usable:
        effective_families.add(host_spec.provider_family)
        effective_transports.add(host_spec.transport_domain)
    requested_total_seats = 1 + len(participant_ids)
    effective_total_seats = int(host_seat_usable) + len(usable)

    if dry_run:
        external_status = "not_evaluated"
        checks: dict[str, bool | None] = {
            "total_seats": None,
            "minimum_effective_families": None,
            "minimum_effective_transports": None,
        }
    elif not external_collection_started:
        external_status = "not_started"
        checks = {
            "total_seats": None,
            "minimum_effective_families": None,
            "minimum_effective_transports": None,
        }
    elif profile == "critical":
        gate = raw_profile.get("health_gate") if isinstance(raw_profile, dict) else {}
        minimum_families = (
            gate.get("minimum_effective_families") if isinstance(gate, dict) else None
        )
        minimum_transports = (
            gate.get("minimum_effective_transports") if isinstance(gate, dict) else None
        )
        checks = {
            "total_seats": effective_total_seats == target_total_seats == 5,
            "minimum_effective_families": (
                isinstance(minimum_families, int)
                and len(effective_families) >= minimum_families
            ),
            "minimum_effective_transports": (
                isinstance(minimum_transports, int)
                and len(effective_transports) >= minimum_transports
            ),
        }
        external_status = "healthy" if all(checks.values()) else "degraded"
    else:
        expected_total_seats = (
            target_total_seats
            if isinstance(target_total_seats, int)
            else requested_total_seats
        )
        checks = {
            "total_seats": effective_total_seats == expected_total_seats,
            "minimum_effective_families": None,
            "minimum_effective_transports": None,
        }
        external_status = "healthy" if checks["total_seats"] else "degraded"

    if dry_run:
        status = "not_evaluated"
        profile_target_met: bool | None = None
    elif host_position.status == "invalid":
        status = "invalid_host_position"
        profile_target_met = False
    elif host_position_required and not host_position.validated:
        status = "pending_host_position"
        profile_target_met = False
    else:
        status = external_status
        profile_target_met = status == "healthy"

    final_checks = {
        **checks,
        "validated_host_position": (
            host_position.validated if host_position_required else None
        ),
    }

    return {
        "status": status,
        "profile_target_met": profile_target_met,
        "profile": profile,
        "target_total_seats": target_total_seats,
        "requested_total_seats": requested_total_seats,
        "effective_total_seats": effective_total_seats,
        "requested_participant_ids": participant_ids,
        "usable_participant_ids": [result.participant_id for result in usable],
        "effective_provider_families": sorted(effective_families),
        "effective_transport_domains": sorted(effective_transports),
        "checks": final_checks,
        "external_collection": {
            "status": external_status,
            "started": external_collection_started,
            "requested_external_participants": len(participant_ids),
            "usable_external_participants": len(usable),
            "checks": checks,
        },
        "host_position": {
            "required": host_position_required,
            "provided": host_position.file is not None,
            "validated": host_position.validated,
            "status": host_position.status,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        choices=(*HOSTS, "auto"),
        required=True,
        help="Current host model; it is excluded from CLI calls.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--profile",
        choices=tuple(load_council_config()["profiles"]),
        help=(
            "Council risk profile: quick=2 total seats, general=3, critical=5, "
            "legacy=the original three-provider roster."
        ),
    )
    selection.add_argument(
        "--participants",
        help="Explicit comma-separated participant ids; provider families must be distinct.",
    )
    selection.add_argument(
        "--models",
        help="Compatibility alias for explicit claude,codex,gemini participants.",
    )
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "Explicit 0600 provider credential file containing only supported "
            "OpenCode/DeepSeek HTTP route KEY=VALUE entries. It is parsed without a shell and "
            "never supplies Claude Code, Codex, or Gemini native credentials."
        ),
    )
    parser.add_argument(
        "--host-position-file",
        type=Path,
        help=(
            "Structured JSON position written by the current host before external "
            "collection; required for a real critical profile run."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_HARD_TIMEOUT_SECONDS,
        help="Hard per-participant timeout in seconds (default: 900).",
    )
    parser.add_argument(
        "--silence-timeout",
        type=float,
        default=DEFAULT_SILENCE_TIMEOUT_SECONDS,
        help=(
            "Terminate a participant after this many seconds without stdout/stderr "
            "or output-file growth; 0 disables this check (default: 0)."
        ),
    )
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        default=DEFAULT_MAX_OUTPUT_BYTES,
        help=(
            "Terminate a participant after combined stdout/stderr/output-file bytes "
            "exceed this limit (default: 8388608)."
        ),
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    host = detect_host() if args.host == "auto" else args.host
    if host is None:
        print("Could not auto-detect host; pass --host explicitly.", file=sys.stderr)
        return 2

    try:
        models, active_profile, selection_source = resolve_selection(
            host,
            profile=args.profile,
            participants=args.participants,
            models=args.models,
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    if not models:
        print("No external models selected.", file=sys.stderr)
        return 2
    if not _is_finite_number(args.timeout) or args.timeout <= 0:
        print("--timeout must be positive and finite.", file=sys.stderr)
        return 2
    if not _is_finite_number(args.silence_timeout) or args.silence_timeout < 0:
        print("--silence-timeout must be non-negative and finite.", file=sys.stderr)
        return 2
    if args.max_output_bytes <= 0:
        print("--max-output-bytes must be positive.", file=sys.stderr)
        return 2
    required_references = (
        SCHEMA_PATH,
        GEMINI_POLICY_PATH,
        COUNCIL_PROFILES_PATH,
        OPENCODE_CONFIG_PATH,
    )
    if any(not path.is_file() for path in required_references):
        print("A bundled council schema, policy, or profile is missing.", file=sys.stderr)
        return 2

    try:
        provider_env = load_provider_env_file(args.env_file)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    prompt_path = args.prompt_file.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    cwd = args.cwd.expanduser().resolve()
    if not prompt_path.is_file():
        print(f"Prompt file not found: {prompt_path}", file=sys.stderr)
        return 2
    if not cwd.is_dir():
        print(f"Working directory not found: {cwd}", file=sys.stderr)
        return 2

    prompt = prompt_path.read_text(encoding="utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    host_position = attest_host_position(args.host_position_file)

    def persist_manifest(
        results: list[Result],
        profile_health: dict[str, Any],
    ) -> dict[str, Any]:
        host_spec = participant_spec(host)
        manifest = {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "runner_version": RUNNER_VERSION,
            "host": host,
            "requested_models": models,
            "requested_participants": models,
            "profile": active_profile,
            "selection_source": selection_source,
            "profile_health": profile_health,
            "host_participant": {
                "participant_id": host,
                "provider_family": host_spec.provider_family,
                "transport_domain": host_spec.transport_domain,
                "role": "Current host, context holder, and primary judge.",
            },
            "host_position_file": host_position.file,
            "host_position_sha256": host_position.sha256,
            "host_position_validated": host_position.validated,
            "host_position_status": host_position.status,
            "host_position_error": host_position.error,
            "provider_env_file_provided": args.env_file is not None,
            "provider_env_keys_loaded": sorted(provider_env),
            "prompt_file": str(prompt_path),
            "core_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "output_dir": str(output_dir),
            "hard_timeout_seconds": args.timeout,
            "silence_timeout_seconds": args.silence_timeout or None,
            "max_output_bytes": args.max_output_bytes,
            "results": [asdict(result) for result in results],
        }
        manifest = _sanitize_manifest(
            manifest,
            prompt,
            models,
            tuple(provider_env.values()),
        )
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return manifest

    raw_profile = load_council_config()["profiles"].get(active_profile)
    critical_host_position_required = bool(
        active_profile == "critical"
        and isinstance(raw_profile, dict)
        and raw_profile.get("requires_validated_host_position") is True
    )
    if (
        not args.dry_run
        and critical_host_position_required
        and not host_position.validated
    ):
        profile_health = build_profile_health(
            active_profile,
            host,
            models,
            [],
            dry_run=False,
            host_position=host_position,
            external_collection_started=False,
        )
        persist_manifest([], profile_health)
        print(
            "A real critical run requires a valid --host-position-file before "
            "any external participant is started.",
            file=sys.stderr,
        )
        return 1

    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=len(models)) as executor:
        futures = {
            executor.submit(
                run_model,
                model,
                prompt,
                output_dir,
                args.timeout,
                args.silence_timeout,
                cwd,
                args.dry_run,
                args.max_output_bytes,
                provider_env,
            ): model
            for model in models
        }
        for future in as_completed(futures):
            model = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = runner_exception_result(
                    model,
                    error,
                    output_dir,
                    args.timeout,
                    args.silence_timeout,
                    args.max_output_bytes,
                    prompt,
                    provider_env,
                )
            results.append(result)

    requested_order = {participant_id: index for index, participant_id in enumerate(models)}
    results.sort(key=lambda result: requested_order[result.model])
    profile_health = build_profile_health(
        active_profile,
        host,
        models,
        results,
        dry_run=args.dry_run,
        host_position=host_position,
    )
    persist_manifest(results, profile_health)

    usable = sum(result.parse_ok for result in results)
    if args.dry_run:
        return 0
    if active_profile == "critical" and profile_health["status"] != "healthy":
        return 1
    if profile_health["status"] == "invalid_host_position":
        return 1
    return 0 if usable > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
