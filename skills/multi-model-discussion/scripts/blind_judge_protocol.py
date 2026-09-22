#!/usr/bin/env python3
"""Deterministic, provider-free protocol for blind judging seven candidates.

This module deliberately does not execute a model or a harness.  It freezes a
blind packet, validates the judge's first terminal answer, applies independent
admission gates, and conservatively aggregates admitted family votes.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import stat
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

PROTOCOL = "blind-judge-7x21-v1"
SCHEMA_VERSION = 1
LABELS = tuple("ABCDEFG")
GATE_STATES = {"pass", "fail", "unknown"}
OUTCOMES = {"a", "tie", "b", "insufficient"}
VERDICTS = {"pass", "concern", "insufficient"}
CONFIDENCE = {"high", "medium", "low"}
STATUS = {"pass", "fail", "partial", "not_run"}
SLUG = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CHECKER_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_RAW_ANSWER_BYTES = 4 * 1024 * 1024


class ProtocolError(ValueError):
    """A fail-closed protocol error."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError(f"non-finite JSON number: {value}")


def strict_json_bytes(data: bytes, *, source: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"{source}: not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ProtocolError) as exc:
        raise ProtocolError(f"{source}: invalid strict JSON: {exc}") from exc


def read_json(path: Path) -> Any:
    try:
        return strict_json_bytes(path.read_bytes(), source=str(path))
    except OSError as exc:
        raise ProtocolError(f"cannot read {path}: {exc}") from exc


def canonical_bytes(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ProtocolError(f"cannot hash {path}: {exc}") from exc


def _require_object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{where} must be an object")
    return value


def _require_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProtocolError(f"{where} must be an array")
    return value


def _closed_keys(
    value: Mapping[str, Any],
    *,
    required: Iterable[str],
    optional: Iterable[str] = (),
    where: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        raise ProtocolError(f"{where} keys mismatch; missing={missing}, extra={extra}")


def _slug(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ProtocolError(f"{where} must be a lowercase stable ID")
    return value


def _text(value: Any, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ProtocolError(f"{where} must be a non-empty string")
    return value


def _finite_number(value: Any, where: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{where} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ProtocolError(f"{where} must be finite and >= {minimum}")
    return result


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProtocolError(f"{where} must be a positive integer")
    return value


def _resolve_path(base: Path, raw: Any, where: str) -> Path:
    path_text = _text(raw, where)
    path = Path(path_text)
    return path if path.is_absolute() else (base / path).resolve()


def _approved_input_file(
    base: Path,
    raw: Any,
    where: str,
    *,
    max_bytes: int,
) -> Path:
    path_text = _text(raw, where)
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ProtocolError(
            f"{where} must be a relative path beneath the spec directory"
        )
    base = base.resolve()
    unresolved = base / relative
    cursor = base
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProtocolError(f"{where} must not traverse a symlink")
    path = unresolved.resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ProtocolError(f"{where} escapes the approved spec directory") from exc
    if not path.is_file():
        raise ProtocolError(f"{where} must name an existing regular file")
    size = path.stat().st_size
    if size > max_bytes:
        raise ProtocolError(f"{where} exceeds the {max_bytes}-byte limit")
    return path


def _read_bounded(path: Path, max_bytes: int, where: str) -> bytes:
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise ProtocolError(f"{where} exceeds the {max_bytes}-byte limit")
        data = path.read_bytes()
    except OSError as exc:
        raise ProtocolError(f"cannot read {where}: {exc}") from exc
    if len(data) > max_bytes:
        raise ProtocolError(f"{where} exceeds the {max_bytes}-byte limit")
    return data


def expected_pairs(labels: Sequence[str] = LABELS) -> list[tuple[str, str]]:
    return list(itertools.combinations(labels, 2))


def _validate_task(value: Any) -> dict[str, Any]:
    task = _require_object(value, "task")
    _closed_keys(task, required=("prompt", "context", "hard_constraints"), where="task")
    prompt = _text(task["prompt"], "task.prompt")
    context = _text(task["context"], "task.context", allow_empty=True)
    constraints = _require_list(task["hard_constraints"], "task.hard_constraints")
    if not all(isinstance(item, str) and item.strip() for item in constraints):
        raise ProtocolError("task.hard_constraints must contain non-empty strings")
    return {"prompt": prompt, "context": context, "hard_constraints": constraints}


def _validate_rubric(value: Any) -> list[dict[str, str]]:
    rows = _require_list(value, "rubric")
    if not rows:
        raise ProtocolError("rubric must not be empty")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        row = _require_object(raw, f"rubric[{index}]")
        _closed_keys(
            row, required=("id", "name", "description"), where=f"rubric[{index}]"
        )
        dimension_id = _slug(row["id"], f"rubric[{index}].id")
        if dimension_id in seen:
            raise ProtocolError(f"duplicate rubric ID: {dimension_id}")
        seen.add(dimension_id)
        result.append(
            {
                "id": dimension_id,
                "name": _text(row["name"], f"rubric[{index}].name"),
                "description": _text(
                    row["description"], f"rubric[{index}].description"
                ),
            }
        )
    return result


def _validate_checker(value: Any, where: str) -> dict[str, Any]:
    checker = _require_object(value, where)
    _closed_keys(checker, required=("schema_version", "status", "facts"), where=where)
    if checker["schema_version"] != SCHEMA_VERSION:
        raise ProtocolError(f"{where}.schema_version must be {SCHEMA_VERSION}")
    if checker["status"] not in STATUS:
        raise ProtocolError(f"{where}.status is invalid")
    facts = _require_list(checker["facts"], f"{where}.facts")
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(facts):
        fact = _require_object(raw, f"{where}.facts[{index}]")
        _closed_keys(
            fact,
            required=("fact_id", "status", "text"),
            where=f"{where}.facts[{index}]",
        )
        fact_id = _slug(fact["fact_id"], f"{where}.facts[{index}].fact_id")
        if fact_id in seen:
            raise ProtocolError(f"{where}: duplicate checker fact ID {fact_id}")
        seen.add(fact_id)
        if fact["status"] not in STATUS | {"unknown"}:
            raise ProtocolError(f"{where}.facts[{index}].status is invalid")
        clean.append(
            {
                "fact_id": fact_id,
                "status": fact["status"],
                "text": _text(fact["text"], f"{where}.facts[{index}].text"),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": checker["status"],
        "facts": clean,
    }


def _validate_budget(value: Any) -> dict[str, Any]:
    budget = _require_object(value, "budget")
    _closed_keys(
        budget,
        required=(
            "max_total_tokens",
            "adapter_input_reserve_tokens",
            "reserved_output_tokens",
            "max_cash_usd",
        ),
        where="budget",
    )
    total = _positive_int(budget["max_total_tokens"], "budget.max_total_tokens")
    adapter_reserve = _positive_int(
        budget["adapter_input_reserve_tokens"],
        "budget.adapter_input_reserve_tokens",
    )
    reserve = _positive_int(
        budget["reserved_output_tokens"], "budget.reserved_output_tokens"
    )
    if adapter_reserve + reserve >= total:
        raise ProtocolError(
            "adapter and output reserves must leave room below max_total_tokens"
        )
    cash = _finite_number(budget["max_cash_usd"], "budget.max_cash_usd")
    return {
        "max_total_tokens": total,
        "adapter_input_reserve_tokens": adapter_reserve,
        "reserved_output_tokens": reserve,
        "max_cash_usd": cash,
    }


def _validate_aliases(value: Any, where: str) -> list[str]:
    aliases = _require_list(value, where)
    if not aliases:
        raise ProtocolError(f"{where} must contain at least one identity alias")
    clean: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(aliases):
        alias = _text(raw, f"{where}[{index}]").strip().casefold()
        if len(alias) < 3:
            raise ProtocolError(
                f"{where}[{index}] must contain at least three characters"
            )
        if alias in seen:
            raise ProtocolError(f"{where} contains a duplicate alias")
        seen.add(alias)
        clean.append(alias)
    return clean


def _validate_judge_roster(value: Any) -> list[dict[str, str]]:
    rows = _require_list(value, "judge_roster")
    if len(rows) < 2:
        raise ProtocolError("judge_roster must contain at least two pinned judges")
    clean: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        row = _require_object(raw, f"judge_roster[{index}]")
        _closed_keys(
            row,
            required=(
                "judge_id",
                "family_id",
                "route_id",
                "adapter_id",
                "adapter_version",
            ),
            where=f"judge_roster[{index}]",
        )
        judge_id = _slug(row["judge_id"], f"judge_roster[{index}].judge_id")
        if judge_id in seen:
            raise ProtocolError(f"duplicate judge_id: {judge_id}")
        seen.add(judge_id)
        clean.append(
            {
                "judge_id": judge_id,
                "family_id": _slug(
                    row["family_id"], f"judge_roster[{index}].family_id"
                ),
                "route_id": _slug(row["route_id"], f"judge_roster[{index}].route_id"),
                "adapter_id": _slug(
                    row["adapter_id"], f"judge_roster[{index}].adapter_id"
                ),
                "adapter_version": _text(
                    row["adapter_version"],
                    f"judge_roster[{index}].adapter_version",
                ),
            }
        )
    if len({row["family_id"] for row in clean}) < 2:
        raise ProtocolError("judge_roster must contain at least two distinct families")
    return clean


def _validate_order_recheck_policy(value: Any) -> dict[str, Any]:
    policy = _require_object(value, "order_recheck_policy")
    _closed_keys(
        policy,
        required=("allowed_triggers", "max_rechecks_per_judge"),
        where="order_recheck_policy",
    )
    triggers = _require_list(
        policy["allowed_triggers"], "order_recheck_policy.allowed_triggers"
    )
    if (
        not triggers
        or len(triggers) != len(set(triggers))
        or not set(triggers).issubset({"close", "conflict"})
    ):
        raise ProtocolError(
            "order_recheck_policy.allowed_triggers must be unique close/conflict values"
        )
    if policy["max_rechecks_per_judge"] != 1:
        raise ProtocolError("order_recheck_policy.max_rechecks_per_judge must be 1")
    return {
        "allowed_triggers": triggers,
        "max_rechecks_per_judge": 1,
    }


def _reject_identity_aliases(public_value: Any, aliases: Iterable[str]) -> None:
    public_text = canonical_bytes(public_value).decode("utf-8").casefold()
    leaked = sorted(alias for alias in set(aliases) if alias in public_text)
    if leaked:
        raise ProtocolError(f"public blind packet contains identity aliases: {leaked}")


def _wire_skeleton(packet_id: str, dimension_ids: Sequence[str]) -> dict[str, Any]:
    assessments = []
    for label in LABELS:
        assessments.append(
            {
                "candidate": label,
                "dimensions": {
                    dimension_id: {
                        "verdict": "insufficient",
                        "evidence": [],
                        "reason": "insufficient",
                    }
                    for dimension_id in dimension_ids
                },
                "decisive_errors": [],
                "hard_constraint_violations": [],
                "confidence": "low",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "packet_id": packet_id,
        "evidence": [],
        "assessments": assessments,
        "pairs": [
            {
                "a": a,
                "b": b,
                "outcome": "insufficient",
                "evidence": [],
                "reason": "insufficient",
            }
            for a, b in expected_pairs()
        ],
    }


def _wire_contract(dimension_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "closed_root_keys": [
            "schema_version",
            "packet_id",
            "evidence",
            "assessments",
            "pairs",
        ],
        "enums": {
            "verdict": sorted(VERDICTS),
            "confidence": sorted(CONFIDENCE),
            "pair_outcome": ["a", "tie", "b", "insufficient"],
        },
        "evidence_item": {
            "id": "lowercase-stable-id",
            "candidate": "A|B|C|D|E|F|G",
            "source": "response|checker:status|checker:<fact_id>",
            "quote": "exact-nonempty-frozen-text",
        },
        "dimension_ids_exactly": list(dimension_ids),
        "dimension_item": {
            "verdict": "pass|concern|insufficient",
            "evidence": ["evidence-id"],
            "reason": "nonempty-string",
        },
        "issue_item": {
            "text": "nonempty-string",
            "evidence": ["same-candidate-evidence-id"],
        },
        "pair_item": {
            "a": "lexically-earlier-label",
            "b": "lexically-later-label",
            "outcome": "a|tie|b|insufficient",
            "evidence": ["both-candidate-evidence-ids-unless-insufficient"],
            "reason": "nonempty-string",
        },
    }


def _build_prompt(packet: Mapping[str, Any]) -> str:
    dimension_ids = [row["id"] for row in packet["rubric"]]
    skeleton = _wire_skeleton(packet["packet_id"], dimension_ids)
    contract = _wire_contract(dimension_ids)
    return (
        "You are a blind quality judge. Evaluate all seven candidates against the same task, hard "
        "constraints, rubric, and frozen checker facts. Identity, style, length, confidence, and agreement "
        "with other candidates are not quality evidence. Do not browse, call tools, repair candidates, or "
        "infer missing checker results.\n\n"
        "Return exactly one JSON object as your first terminal answer: no BOM, Markdown fence, preface, "
        "retry, rank, score-derived total order, or extra keys. Use the exact schema shape shown below. "
        "Every evidence row must quote an exact non-empty substring from candidate.response with "
        'source="response", or the exact frozen checker status/fact text with source="checker:status" '
        'or source="checker:<fact_id>". Every non-insufficient pair outcome must cite both candidates '
        "and no third candidate. Pair labels must use the displayed lexical orientation and cover all 21 "
        "pairs exactly once. Ties and insufficient evidence are valid.\n\n"
        "CLOSED FIELD AND ENUM CONTRACT:\n"
        + json.dumps(contract, ensure_ascii=False, separators=(",", ":"))
        + "\n\nSCHEMA SHAPE (replace values but not keys):\n"
        + json.dumps(skeleton, ensure_ascii=False, separators=(",", ":"))
        + "\n\nFROZEN BLIND PACKET:\n"
        + json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def _packet_preflight(
    packet: Mapping[str, Any], prompt: str, budget: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    prompt_data = prompt.encode("utf-8")
    skeleton_data = canonical_bytes(
        _wire_skeleton(packet["packet_id"], [row["id"] for row in packet["rubric"]])
    )
    input_upper = len(prompt_data)
    output_minimum_upper = len(skeleton_data)
    estimated_total_upper = (
        input_upper
        + budget["adapter_input_reserve_tokens"]
        + budget["reserved_output_tokens"]
    )
    failures: list[str] = []
    if output_minimum_upper > budget["reserved_output_tokens"]:
        failures.append("minimum-valid-output-exceeds-reserved-output")
    if estimated_total_upper > budget["max_total_tokens"]:
        failures.append("conservative-total-token-upper-bound-exceeded")
    preflight = {
        "method": "utf8-bytes-as-conservative-token-upper-bound",
        "prompt_characters": len(prompt),
        "prompt_utf8_bytes": len(prompt_data),
        "input_token_upper_bound": input_upper,
        "adapter_input_reserve_tokens": budget["adapter_input_reserve_tokens"],
        "minimum_valid_output_characters": len(skeleton_data.decode("utf-8")),
        "minimum_valid_output_utf8_bytes": len(skeleton_data),
        "minimum_valid_output_token_upper_bound": output_minimum_upper,
        "reserved_output_tokens": budget["reserved_output_tokens"],
        "total_token_upper_bound_with_reserve": estimated_total_upper,
        "truncated": False,
        "split": False,
    }
    return failures, preflight


def _roster_coverage_failures(
    candidate_rows: Sequence[Mapping[str, Any]],
    judge_roster: Sequence[Mapping[str, Any]],
) -> list[str]:
    judge_families = {row["family_id"] for row in judge_roster}
    for left, right in itertools.combinations(candidate_rows, 2):
        eligible = judge_families - {left["family_id"], right["family_id"]}
        if len(eligible) < 2:
            return ["judge-roster-pair-coverage-insufficient"]
    return []


def _write_new(path: Path, data: bytes, *, private: bool = False) -> None:
    if path.exists() or path.is_symlink():
        raise ProtocolError(f"refusing to overwrite {path}")
    mode = 0o600 if private else 0o644
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, mode)
        try:
            offset = 0
            view = memoryview(data)
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise ProtocolError(f"short write while creating {path}")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if path.exists() or path.is_symlink():
            raise ProtocolError(f"refusing to overwrite {path}")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _prepare_output_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(path, stat.S_IRWXU)


def _candidate_commitment(
    candidate_id: str,
    family_id: str,
    aliases: Sequence[str],
    response_hash: str,
    checker_hash: str,
) -> bytes:
    return canonical_bytes(
        {
            "candidate_id": candidate_id,
            "family_id": family_id,
            "identity_aliases": list(aliases),
            "response_sha256": response_hash,
            "checker_sha256": checker_hash,
        }
    )


def build_bundle(
    spec_path: Path,
    output_dir: Path,
    *,
    order: Sequence[str] | None = None,
    reverse_of: Path | None = None,
    reverse_trigger: str | None = None,
) -> dict[str, Any]:
    """Build one immutable blind bundle and return its manifest.

    The directory and a failure manifest are retained when the conservative
    18k-style preflight fails; no content is truncated or split.
    """

    _prepare_output_dir(output_dir)
    manifest_path = output_dir / "bundle-manifest.json"
    try:
        spec = _require_object(read_json(spec_path), "spec")
        _closed_keys(
            spec,
            required=(
                "schema_version",
                "packet_id",
                "task",
                "rubric",
                "candidates",
                "judge_roster",
                "order_recheck_policy",
                "budget",
            ),
            where="spec",
        )
        if spec["schema_version"] != SCHEMA_VERSION:
            raise ProtocolError(f"spec.schema_version must be {SCHEMA_VERSION}")
        packet_id = _slug(spec["packet_id"], "spec.packet_id")
        task = _validate_task(spec["task"])
        rubric = _validate_rubric(spec["rubric"])
        judge_roster = _validate_judge_roster(spec["judge_roster"])
        order_recheck_policy = _validate_order_recheck_policy(
            spec["order_recheck_policy"]
        )
        budget = _validate_budget(spec["budget"])
        raw_candidates = _require_list(spec["candidates"], "spec.candidates")
        if len(raw_candidates) != len(LABELS):
            raise ProtocolError("spec.candidates must contain exactly seven candidates")

        base = spec_path.resolve().parent
        candidates: dict[str, dict[str, Any]] = {}
        commitments: list[bytes] = []
        for index, raw in enumerate(raw_candidates):
            item = _require_object(raw, f"spec.candidates[{index}]")
            _closed_keys(
                item,
                required=(
                    "candidate_id",
                    "family_id",
                    "identity_aliases",
                    "response_path",
                    "checker_path",
                ),
                where=f"spec.candidates[{index}]",
            )
            candidate_id = _slug(
                item["candidate_id"], f"spec.candidates[{index}].candidate_id"
            )
            family_id = _slug(item["family_id"], f"spec.candidates[{index}].family_id")
            identity_aliases = _validate_aliases(
                item["identity_aliases"],
                f"spec.candidates[{index}].identity_aliases",
            )
            if candidate_id in candidates:
                raise ProtocolError(f"duplicate candidate_id: {candidate_id}")
            response_path = _approved_input_file(
                base,
                item["response_path"],
                f"spec.candidates[{index}].response_path",
                max_bytes=MAX_RESPONSE_BYTES,
            )
            checker_path = _approved_input_file(
                base,
                item["checker_path"],
                f"spec.candidates[{index}].checker_path",
                max_bytes=MAX_CHECKER_BYTES,
            )
            try:
                response_bytes = _read_bounded(
                    response_path,
                    MAX_RESPONSE_BYTES,
                    f"response for {candidate_id}",
                )
                response = response_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ProtocolError(
                    f"cannot read UTF-8 response for {candidate_id}: {exc}"
                ) from exc
            if not response.strip():
                raise ProtocolError(f"candidate {candidate_id} has an empty response")
            checker_bytes = _read_bounded(
                checker_path,
                MAX_CHECKER_BYTES,
                f"checker for {candidate_id}",
            )
            checker = _validate_checker(
                strict_json_bytes(checker_bytes, source=str(checker_path)),
                f"checker[{candidate_id}]",
            )
            response_hash = sha256_bytes(response_bytes)
            checker_hash = sha256_bytes(canonical_bytes(checker))
            candidates[candidate_id] = {
                "candidate_id": candidate_id,
                "family_id": family_id,
                "identity_aliases": identity_aliases,
                "response": response,
                "response_sha256": response_hash,
                "checker": checker,
                "checker_sha256": checker_hash,
            }
            commitments.append(
                _candidate_commitment(
                    candidate_id,
                    family_id,
                    identity_aliases,
                    response_hash,
                    checker_hash,
                )
            )

        committed_order = list(order) if order is not None else list(candidates)
        if len(committed_order) != len(LABELS) or set(committed_order) != set(
            candidates
        ):
            raise ProtocolError(
                "committed order must contain every candidate_id exactly once"
            )

        order_variant = "primary"
        primary_packet_sha256: str | None = None
        if reverse_of is not None:
            _, primary_manifest, primary_identity = _identity_bundle(reverse_of)
            if primary_manifest.get("order_variant") != "primary":
                raise ProtocolError("reverse source must be a primary bundle")
            if order_recheck_policy != primary_manifest.get("order_recheck_policy"):
                raise ProtocolError("reverse source order-recheck policy mismatch")
            if reverse_trigger not in order_recheck_policy["allowed_triggers"]:
                raise ProtocolError(
                    "reverse build requires an allowed close/conflict trigger"
                )
            primary_order = [
                row["candidate_id"] for row in primary_identity.get("labels", [])
            ]
            if set(primary_order) != set(candidates) or len(primary_order) != len(
                LABELS
            ):
                raise ProtocolError("reverse source candidate set does not match spec")
            expected_content_set = primary_manifest.get("content_set_sha256")
            order_variant = "reverse"
            primary_packet_sha256 = primary_manifest.get("packet_sha256")
            if order is None:
                committed_order = list(reversed(primary_order))
            if committed_order != list(reversed(primary_order)):
                raise ProtocolError(
                    "reverse bundle order must be the exact primary reversal"
                )
        else:
            if reverse_trigger is not None:
                raise ProtocolError("reverse_trigger requires reverse_of")
            expected_content_set = None

        content_set_sha256 = sha256_bytes(b"".join(sorted(commitments)))
        if (
            expected_content_set is not None
            and content_set_sha256 != expected_content_set
        ):
            raise ProtocolError(
                "reverse source content-set commitment does not match spec"
            )

        public_candidates: list[dict[str, Any]] = []
        private_rows: list[dict[str, Any]] = []
        for label, candidate_id in zip(LABELS, committed_order):
            candidate = candidates[candidate_id]
            public_candidates.append(
                {
                    "label": label,
                    "response": candidate["response"],
                    "response_sha256": candidate["response_sha256"],
                    "checker": candidate["checker"],
                    "checker_sha256": candidate["checker_sha256"],
                }
            )
            private_rows.append(
                {
                    "label": label,
                    "candidate_id": candidate_id,
                    "family_id": candidate["family_id"],
                    "identity_aliases": candidate["identity_aliases"],
                    "response_sha256": candidate["response_sha256"],
                    "checker_sha256": candidate["checker_sha256"],
                }
            )

        public_blind_material = {
            "task": task,
            "rubric": rubric,
            "candidates": public_candidates,
        }
        private_aliases: list[str] = []
        for candidate in candidates.values():
            private_aliases.extend(
                [candidate["candidate_id"], candidate["family_id"]]
                + candidate["identity_aliases"]
            )
        _reject_identity_aliases(public_blind_material, private_aliases)

        packet = {
            "schema_version": SCHEMA_VERSION,
            "protocol": PROTOCOL,
            "packet_id": packet_id,
            "order_variant": order_variant,
            "task": task,
            "rubric": rubric,
            "candidates": public_candidates,
        }
        packet_data = canonical_bytes(packet)
        packet_hash = sha256_bytes(packet_data)
        prompt = _build_prompt(packet)
        prompt_data = prompt.encode("utf-8")
        packet_failures, preflight = _packet_preflight(packet, prompt, budget)
        roster_failures = _roster_coverage_failures(private_rows, judge_roster)
        failures = packet_failures + roster_failures

        identity_map = {
            "schema_version": SCHEMA_VERSION,
            "protocol": PROTOCOL,
            "packet_id": packet_id,
            "order_variant": order_variant,
            "packet_sha256": packet_hash,
            "content_set_sha256": content_set_sha256,
            "labels": private_rows,
            "judge_roster": judge_roster,
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "protocol": PROTOCOL,
            "status": "ready" if not failures else "failed-closed",
            "failures": failures,
            "packet_id": packet_id,
            "order_variant": order_variant,
            "packet_sha256": packet_hash,
            "prompt_sha256": sha256_bytes(prompt_data),
            "identity_map_sha256": sha256_bytes(canonical_bytes(identity_map)),
            "content_set_sha256": content_set_sha256,
            "primary_packet_sha256": primary_packet_sha256,
            "order_recheck_policy": order_recheck_policy,
            "order_recheck_trigger": reverse_trigger,
            "budget": budget,
            "packet_failures": packet_failures,
            "roster_failures": roster_failures,
            "preflight": preflight,
        }
        _write_new(output_dir / "packet.json", packet_data)
        _write_new(output_dir / "prompt.txt", prompt_data)
        _write_new(
            output_dir / "identity-map.json",
            canonical_bytes(identity_map),
            private=True,
        )
        _write_new(manifest_path, canonical_bytes(manifest))
        return manifest
    except Exception as exc:
        if not manifest_path.exists():
            failure = {
                "schema_version": SCHEMA_VERSION,
                "protocol": PROTOCOL,
                "status": "failed-closed",
                "failures": [str(exc)],
            }
            _write_new(manifest_path, canonical_bytes(failure))
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError(str(exc)) from exc


def _validate_frozen_packet(value: Any) -> dict[str, Any]:
    packet = _require_object(value, "packet")
    _closed_keys(
        packet,
        required=(
            "schema_version",
            "protocol",
            "packet_id",
            "order_variant",
            "task",
            "rubric",
            "candidates",
        ),
        where="packet",
    )
    if packet["schema_version"] != SCHEMA_VERSION or packet["protocol"] != PROTOCOL:
        raise ProtocolError("packet schema or protocol mismatch")
    _slug(packet["packet_id"], "packet.packet_id")
    if packet["order_variant"] not in {"primary", "reverse"}:
        raise ProtocolError("packet.order_variant is invalid")
    _validate_task(packet["task"])
    _validate_rubric(packet["rubric"])
    candidates = _require_list(packet["candidates"], "packet.candidates")
    if len(candidates) != len(LABELS):
        raise ProtocolError("packet must contain exactly seven candidates")
    for index, raw in enumerate(candidates):
        row = _require_object(raw, f"packet.candidates[{index}]")
        _closed_keys(
            row,
            required=(
                "label",
                "response",
                "response_sha256",
                "checker",
                "checker_sha256",
            ),
            where=f"packet.candidates[{index}]",
        )
        if row["label"] != LABELS[index]:
            raise ProtocolError("packet labels must be A through G in order")
        response = _text(row["response"], f"packet.candidates[{index}].response")
        if row["response_sha256"] != sha256_bytes(response.encode("utf-8")):
            raise ProtocolError(
                f"packet candidate {row['label']} response hash mismatch"
            )
        checker = _validate_checker(
            row["checker"], f"packet.candidates[{index}].checker"
        )
        if row["checker_sha256"] != sha256_bytes(canonical_bytes(checker)):
            raise ProtocolError(
                f"packet candidate {row['label']} checker hash mismatch"
            )
    return packet


def _load_bundle(
    bundle_dir: Path, *, require_ready: bool = True
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _require_object(
        read_json(bundle_dir / "bundle-manifest.json"), "bundle manifest"
    )
    packet = _validate_frozen_packet(read_json(bundle_dir / "packet.json"))
    _closed_keys(
        manifest,
        required=(
            "schema_version",
            "protocol",
            "status",
            "failures",
            "packet_id",
            "order_variant",
            "packet_sha256",
            "prompt_sha256",
            "identity_map_sha256",
            "content_set_sha256",
            "primary_packet_sha256",
            "order_recheck_policy",
            "order_recheck_trigger",
            "budget",
            "packet_failures",
            "roster_failures",
            "preflight",
        ),
        where="bundle manifest",
    )
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ProtocolError("bundle manifest schema mismatch")
    if manifest.get("protocol") != PROTOCOL or packet.get("protocol") != PROTOCOL:
        raise ProtocolError("bundle protocol mismatch")
    if manifest["status"] not in {"ready", "failed-closed"}:
        raise ProtocolError("bundle manifest status is invalid")
    if require_ready and manifest.get("status") != "ready":
        raise ProtocolError("bundle preflight is not ready")
    packet_hash = sha256_file(bundle_dir / "packet.json")
    if packet_hash != manifest.get("packet_sha256"):
        raise ProtocolError("packet hash mismatch")
    if packet.get("packet_id") != manifest.get("packet_id"):
        raise ProtocolError("packet ID mismatch")
    if packet["order_variant"] != manifest["order_variant"]:
        raise ProtocolError("packet and manifest order variants differ")
    policy = _validate_order_recheck_policy(manifest["order_recheck_policy"])
    if manifest["order_variant"] == "primary":
        if (
            manifest["primary_packet_sha256"] is not None
            or manifest["order_recheck_trigger"] is not None
        ):
            raise ProtocolError("primary manifest contains reverse-only fields")
    else:
        if not isinstance(
            manifest["primary_packet_sha256"], str
        ) or not SHA256.fullmatch(manifest["primary_packet_sha256"]):
            raise ProtocolError("reverse manifest primary link is invalid")
        if manifest["order_recheck_trigger"] not in policy["allowed_triggers"]:
            raise ProtocolError("reverse manifest trigger is not preregistered")
    prompt_path = bundle_dir / "prompt.txt"
    if sha256_file(prompt_path) != manifest.get("prompt_sha256"):
        raise ProtocolError("prompt hash mismatch")
    if prompt_path.read_bytes() != _build_prompt(packet).encode("utf-8"):
        raise ProtocolError("prompt does not match the frozen packet")
    budget = _validate_budget(manifest.get("budget"))
    packet_failures, preflight = _packet_preflight(
        packet, prompt_path.read_text(encoding="utf-8"), budget
    )
    if manifest["packet_failures"] != packet_failures:
        raise ProtocolError("bundle packet preflight failures were not reproduced")
    if manifest["preflight"] != preflight:
        raise ProtocolError("bundle preflight measurements were not reproduced")
    if packet_failures and manifest["status"] == "ready":
        raise ProtocolError(
            "bundle was relabeled ready despite packet preflight failure"
        )
    return packet, manifest


def _resolve_evidence(
    raw_rows: Any,
    packet_candidates: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows = _require_list(raw_rows, "answer.evidence")
    clean: list[dict[str, Any]] = []
    owners: dict[str, str] = {}
    for index, raw in enumerate(rows):
        row = _require_object(raw, f"answer.evidence[{index}]")
        _closed_keys(
            row,
            required=("id", "candidate", "source", "quote"),
            where=f"answer.evidence[{index}]",
        )
        evidence_id = _slug(row["id"], f"answer.evidence[{index}].id")
        if evidence_id in owners:
            raise ProtocolError(f"duplicate evidence ID: {evidence_id}")
        candidate = row["candidate"]
        if candidate not in packet_candidates:
            raise ProtocolError(f"unknown evidence candidate: {candidate}")
        source = _text(row["source"], f"answer.evidence[{index}].source")
        quote = _text(row["quote"], f"answer.evidence[{index}].quote")
        frozen = packet_candidates[candidate]
        if source == "response":
            if quote not in frozen["response"]:
                raise ProtocolError(
                    f"evidence {evidence_id} is not an exact response substring"
                )
        elif source == "checker:status":
            if quote != frozen["checker"]["status"]:
                raise ProtocolError(
                    f"evidence {evidence_id} does not match checker status"
                )
        elif source.startswith("checker:"):
            fact_id = source.split(":", 1)[1]
            facts = {
                item["fact_id"]: item["text"] for item in frozen["checker"]["facts"]
            }
            if fact_id not in facts or quote != facts[fact_id]:
                raise ProtocolError(
                    f"evidence {evidence_id} does not match checker fact {fact_id}"
                )
        else:
            raise ProtocolError(f"evidence {evidence_id} has an invalid source")
        owners[evidence_id] = candidate
        clean.append(
            {
                "id": evidence_id,
                "candidate": candidate,
                "source": source,
                "quote": quote,
            }
        )
    return clean, owners


def _evidence_ids(value: Any, where: str, owners: Mapping[str, str]) -> list[str]:
    ids = _require_list(value, where)
    if len(ids) != len(set(ids)) or not all(isinstance(item, str) for item in ids):
        raise ProtocolError(f"{where} must contain unique evidence IDs")
    unknown = [item for item in ids if item not in owners]
    if unknown:
        raise ProtocolError(f"{where} references unknown evidence IDs: {unknown}")
    return ids


def _validate_issue_rows(
    value: Any, where: str, candidate: str, owners: Mapping[str, str]
) -> list[dict[str, Any]]:
    rows = _require_list(value, where)
    clean: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        row = _require_object(raw, f"{where}[{index}]")
        _closed_keys(row, required=("text", "evidence"), where=f"{where}[{index}]")
        evidence = _evidence_ids(row["evidence"], f"{where}[{index}].evidence", owners)
        if not evidence or {owners[item] for item in evidence} != {candidate}:
            raise ProtocolError(f"{where}[{index}] evidence must belong to {candidate}")
        clean.append(
            {"text": _text(row["text"], f"{where}[{index}].text"), "evidence": evidence}
        )
    return clean


def validate_answer_object(answer: Any, packet: Mapping[str, Any]) -> dict[str, Any]:
    root = _require_object(answer, "answer")
    _closed_keys(
        root,
        required=("schema_version", "packet_id", "evidence", "assessments", "pairs"),
        where="answer",
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise ProtocolError(f"answer.schema_version must be {SCHEMA_VERSION}")
    if root["packet_id"] != packet["packet_id"]:
        raise ProtocolError("answer.packet_id does not match frozen packet")
    packet_candidates = {row["label"]: row for row in packet["candidates"]}
    if tuple(packet_candidates) != LABELS:
        raise ProtocolError("packet must contain labels A through G in order")
    evidence, owners = _resolve_evidence(root["evidence"], packet_candidates)
    dimension_ids = [row["id"] for row in packet["rubric"]]

    raw_assessments = _require_list(root["assessments"], "answer.assessments")
    if len(raw_assessments) != len(LABELS):
        raise ProtocolError("answer.assessments must contain exactly seven rows")
    assessments: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for index, raw in enumerate(raw_assessments):
        row = _require_object(raw, f"answer.assessments[{index}]")
        _closed_keys(
            row,
            required=(
                "candidate",
                "dimensions",
                "decisive_errors",
                "hard_constraint_violations",
                "confidence",
            ),
            where=f"answer.assessments[{index}]",
        )
        candidate = row["candidate"]
        if candidate not in packet_candidates or candidate in seen_candidates:
            raise ProtocolError(
                f"invalid or duplicate assessment candidate: {candidate}"
            )
        seen_candidates.add(candidate)
        dimensions = _require_object(
            row["dimensions"], f"assessment[{candidate}].dimensions"
        )
        if set(dimensions) != set(dimension_ids):
            raise ProtocolError(
                f"assessment[{candidate}] must cover every rubric dimension exactly once"
            )
        clean_dimensions: dict[str, Any] = {}
        for dimension_id in dimension_ids:
            dimension = _require_object(
                dimensions[dimension_id], f"assessment[{candidate}].{dimension_id}"
            )
            _closed_keys(
                dimension,
                required=("verdict", "evidence", "reason"),
                where=f"assessment[{candidate}].{dimension_id}",
            )
            verdict = dimension["verdict"]
            if verdict not in VERDICTS:
                raise ProtocolError(
                    f"assessment[{candidate}].{dimension_id}.verdict is invalid"
                )
            cited = _evidence_ids(
                dimension["evidence"],
                f"assessment[{candidate}].{dimension_id}.evidence",
                owners,
            )
            if any(owners[item] != candidate for item in cited):
                raise ProtocolError(f"assessment[{candidate}] cites another candidate")
            if verdict != "insufficient" and not cited:
                raise ProtocolError(
                    f"assessment[{candidate}].{dimension_id} needs evidence"
                )
            clean_dimensions[dimension_id] = {
                "verdict": verdict,
                "evidence": cited,
                "reason": _text(
                    dimension["reason"],
                    f"assessment[{candidate}].{dimension_id}.reason",
                ),
            }
        if row["confidence"] not in CONFIDENCE:
            raise ProtocolError(f"assessment[{candidate}].confidence is invalid")
        assessments.append(
            {
                "candidate": candidate,
                "dimensions": clean_dimensions,
                "decisive_errors": _validate_issue_rows(
                    row["decisive_errors"],
                    f"assessment[{candidate}].decisive_errors",
                    candidate,
                    owners,
                ),
                "hard_constraint_violations": _validate_issue_rows(
                    row["hard_constraint_violations"],
                    f"assessment[{candidate}].hard_constraint_violations",
                    candidate,
                    owners,
                ),
                "confidence": row["confidence"],
            }
        )

    raw_pairs = _require_list(root["pairs"], "answer.pairs")
    if len(raw_pairs) != 21:
        raise ProtocolError("answer.pairs must contain exactly 21 rows")
    pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    expected = set(expected_pairs())
    for index, raw in enumerate(raw_pairs):
        row = _require_object(raw, f"answer.pairs[{index}]")
        _closed_keys(
            row,
            required=("a", "b", "outcome", "evidence", "reason"),
            where=f"answer.pairs[{index}]",
        )
        pair = (row["a"], row["b"])
        if pair not in expected or pair in seen_pairs:
            raise ProtocolError(f"invalid, reversed, or duplicate pair: {pair}")
        seen_pairs.add(pair)
        if row["outcome"] not in OUTCOMES:
            raise ProtocolError(f"pair {pair} has an invalid outcome")
        cited = _evidence_ids(row["evidence"], f"pair[{pair}].evidence", owners)
        cited_owners = {owners[item] for item in cited}
        if not cited_owners.issubset(set(pair)):
            raise ProtocolError(f"pair {pair} cites a third candidate")
        if row["outcome"] != "insufficient" and cited_owners != set(pair):
            raise ProtocolError(f"pair {pair} needs evidence from both candidates")
        pairs.append(
            {
                "a": pair[0],
                "b": pair[1],
                "outcome": row["outcome"],
                "evidence": cited,
                "reason": _text(row["reason"], f"pair[{pair}].reason"),
            }
        )
    if seen_pairs != expected:
        raise ProtocolError("answer.pairs does not cover the frozen pair set")
    return {
        "schema_version": SCHEMA_VERSION,
        "packet_id": packet["packet_id"],
        "evidence": evidence,
        "assessments": assessments,
        "pairs": pairs,
    }


def _gate(state: str, reasons: Sequence[str]) -> dict[str, Any]:
    if state not in GATE_STATES:
        raise AssertionError(state)
    return {"status": state, "reasons": list(reasons)}


def _attested_file(
    base: Path,
    path_value: Any,
    hash_value: Any,
    where: str,
) -> tuple[bytes, str]:
    if not isinstance(hash_value, str) or not SHA256.fullmatch(hash_value):
        raise ProtocolError(f"{where}.sha256 must be a lowercase SHA-256")
    path = _approved_input_file(
        base,
        path_value,
        f"{where}.path",
        max_bytes=MAX_EVIDENCE_BYTES,
    )
    data = _read_bounded(path, MAX_EVIDENCE_BYTES, where)
    actual = sha256_bytes(data)
    if actual != hash_value:
        raise ProtocolError(f"{where} hash mismatch")
    return data, actual


def _validate_run_meta(
    value: Any,
    manifest: Mapping[str, Any],
    identity: Mapping[str, Any],
    raw_hash: str,
    base: Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    meta = _require_object(value, "judge-run")
    _closed_keys(
        meta,
        required=(
            "schema_version",
            "adapter",
            "packet_sha256",
            "prompt_sha256",
            "raw_answer_sha256",
            "judge",
            "attempt_number",
            "order_variant",
            "quality",
            "usage",
        ),
        where="judge-run",
    )
    if meta["schema_version"] != SCHEMA_VERSION:
        raise ProtocolError(f"judge-run.schema_version must be {SCHEMA_VERSION}")
    for key, expected in (
        ("packet_sha256", manifest["packet_sha256"]),
        ("prompt_sha256", manifest["prompt_sha256"]),
        ("raw_answer_sha256", raw_hash),
    ):
        if meta[key] != expected:
            raise ProtocolError(
                f"judge-run.{key} does not match the committed artifact"
            )
    adapter = _require_object(meta["adapter"], "judge-run.adapter")
    _closed_keys(
        adapter,
        required=("adapter_id", "version"),
        where="judge-run.adapter",
    )
    clean_adapter = {
        "adapter_id": _slug(adapter["adapter_id"], "judge-run.adapter.adapter_id"),
        "version": _text(adapter["version"], "judge-run.adapter.version"),
    }
    judge = _require_object(meta["judge"], "judge-run.judge")
    _closed_keys(
        judge,
        required=("judge_id", "family_id", "route_id"),
        where="judge-run.judge",
    )
    clean_judge = {
        "judge_id": _slug(judge["judge_id"], "judge-run.judge.judge_id"),
        "family_id": _slug(judge["family_id"], "judge-run.judge.family_id"),
        "route_id": _slug(judge["route_id"], "judge-run.judge.route_id"),
    }
    roster = {row["judge_id"]: row for row in identity["judge_roster"]}
    pinned = roster.get(clean_judge["judge_id"])
    if pinned is None:
        raise ProtocolError("judge-run judge is not in the pinned roster")
    if any(clean_judge[key] != pinned[key] for key in ("family_id", "route_id")):
        raise ProtocolError(
            "judge-run family or route does not match the pinned roster"
        )
    if clean_adapter["adapter_id"] != pinned["adapter_id"]:
        raise ProtocolError("judge-run adapter does not match the pinned roster")
    if clean_adapter["version"] != pinned["adapter_version"]:
        raise ProtocolError(
            "judge-run adapter version does not match the pinned roster"
        )
    attempt = _positive_int(meta["attempt_number"], "judge-run.attempt_number")
    order_variant = meta["order_variant"]
    if order_variant not in {"primary", "reverse"}:
        raise ProtocolError("judge-run.order_variant must be primary or reverse")
    quality = _require_object(meta["quality"], "judge-run.quality")
    _closed_keys(
        quality,
        required=("status", "evidence_path", "evidence_sha256"),
        where="judge-run.quality",
    )
    if quality["status"] not in GATE_STATES:
        raise ProtocolError("judge-run.quality.status is invalid")
    quality_data, quality_hash = _attested_file(
        base,
        quality["evidence_path"],
        quality["evidence_sha256"],
        "judge-run.quality.evidence",
    )
    usage = _require_object(meta["usage"], "judge-run.usage")
    _closed_keys(
        usage,
        required=(
            "safe_input_tokens",
            "safe_output_tokens",
            "provider_total_tokens",
            "conservative_cash_usd",
            "provider_cost_status",
            "evidence_path",
            "evidence_sha256",
            "pricing_snapshot_path",
            "pricing_snapshot_sha256",
        ),
        where="judge-run.usage",
    )
    for key in ("safe_input_tokens", "safe_output_tokens", "provider_total_tokens"):
        value = usage[key]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ProtocolError(
                f"judge-run.usage.{key} must be null or a non-negative integer"
            )
    cash = usage["conservative_cash_usd"]
    if cash is not None:
        cash = _finite_number(cash, "judge-run.usage.conservative_cash_usd")
    if usage["provider_cost_status"] not in {
        "known",
        "verified-free",
        "ambiguous-zero",
        "unknown",
    }:
        raise ProtocolError("judge-run.usage.provider_cost_status is invalid")
    usage_data, usage_hash = _attested_file(
        base,
        usage["evidence_path"],
        usage["evidence_sha256"],
        "judge-run.usage.evidence",
    )
    pricing_data, pricing_hash = _attested_file(
        base,
        usage["pricing_snapshot_path"],
        usage["pricing_snapshot_sha256"],
        "judge-run.usage.pricing_snapshot",
    )
    clean_usage = {
        key: usage[key]
        for key in (
            "safe_input_tokens",
            "safe_output_tokens",
            "provider_total_tokens",
            "conservative_cash_usd",
            "provider_cost_status",
        )
    }
    clean_usage["conservative_cash_usd"] = cash
    clean_usage["evidence_sha256"] = usage_hash
    clean_usage["pricing_snapshot_sha256"] = pricing_hash
    clean = {
        "schema_version": SCHEMA_VERSION,
        "adapter": clean_adapter,
        "packet_sha256": manifest["packet_sha256"],
        "prompt_sha256": manifest["prompt_sha256"],
        "raw_answer_sha256": raw_hash,
        "judge": clean_judge,
        "attempt_number": attempt,
        "order_variant": order_variant,
        "quality": {"status": quality["status"], "evidence_sha256": quality_hash},
        "usage": clean_usage,
        "bundle_order_variant": manifest["order_variant"],
    }
    artifacts = {
        "quality-evidence.bin": quality_data,
        "usage-evidence.bin": usage_data,
        "pricing-snapshot.bin": pricing_data,
    }
    return clean, artifacts


def _cost_gate(usage: Mapping[str, Any], budget: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    input_tokens = usage["safe_input_tokens"]
    output_tokens = usage["safe_output_tokens"]
    provider_total = usage["provider_total_tokens"]
    cash = usage["conservative_cash_usd"]
    cost_status = usage["provider_cost_status"]
    if input_tokens is None or output_tokens is None or provider_total is None:
        return _gate("unknown", ["safe-token-usage-missing"])
    if provider_total < input_tokens + output_tokens:
        reasons.append("provider-total-below-component-usage")
    if provider_total > budget["max_total_tokens"]:
        reasons.append("token-ceiling-exceeded")
    if cash is None or cost_status in {"unknown", "ambiguous-zero"}:
        return _gate(
            "unknown" if not reasons else "fail",
            reasons + ["safe-cash-evidence-missing"],
        )
    if cash == 0 and cost_status != "verified-free":
        return _gate(
            "unknown" if not reasons else "fail",
            reasons + ["zero-cost-not-verified-free"],
        )
    if cash > 0 and cost_status == "verified-free":
        reasons.append("verified-free-conflicts-with-positive-cost")
    if cash > budget["max_cash_usd"]:
        reasons.append("cash-ceiling-exceeded")
    return _gate("fail" if reasons else "pass", reasons)


def validate_submission(
    bundle_dir: Path,
    raw_answer_path: Path,
    run_meta_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate a committed first answer and persist its independent gates."""

    _prepare_output_dir(output_dir)
    admission_path = output_dir / "admission.json"
    raw: bytes | None = None
    canonical: dict[str, Any] | None = None
    structure_reasons: list[str] = []
    try:
        raw = _read_bounded(raw_answer_path, MAX_RAW_ANSWER_BYTES, "raw first answer")
        raw_hash = sha256_bytes(raw)
        _write_new(output_dir / "raw-answer.bin", raw)
        packet, manifest, identity = _identity_bundle(bundle_dir)
        meta_bytes = _read_bounded(
            run_meta_path,
            MAX_EVIDENCE_BYTES,
            "judge-run adapter manifest",
        )
        meta, evidence_artifacts = _validate_run_meta(
            strict_json_bytes(meta_bytes, source=str(run_meta_path)),
            manifest,
            identity,
            raw_hash,
            run_meta_path.resolve().parent,
        )
        attestation_data = canonical_bytes(meta)
        _write_new(output_dir / "adapter-attestation.json", attestation_data)
        for artifact_name, artifact_data in evidence_artifacts.items():
            _write_new(output_dir / artifact_name, artifact_data, private=True)
        if meta["attempt_number"] != 1:
            structure_reasons.append("not-first-attempt")
        if meta["order_variant"] != manifest["order_variant"]:
            structure_reasons.append("order-variant-mismatch")
        if len(raw) > manifest["budget"]["reserved_output_tokens"]:
            structure_reasons.append("raw-answer-byte-envelope-exceeded")
        try:
            canonical = validate_answer_object(
                strict_json_bytes(raw, source="first answer"), packet
            )
        except ProtocolError as exc:
            structure_reasons.append(str(exc))
        structure = _gate("fail" if structure_reasons else "pass", structure_reasons)
        quality_status = meta["quality"]["status"]
        quality_reasons: list[str] = []
        if quality_status != "pass":
            quality_reasons.append("quality-gate-not-passed")
        quality = _gate(quality_status, quality_reasons)
        cost = _cost_gate(meta["usage"], manifest["budget"])
        eligible = all(gate["status"] == "pass" for gate in (structure, quality, cost))
        admission = {
            "schema_version": SCHEMA_VERSION,
            "protocol": PROTOCOL,
            "status": "admitted" if eligible else "excluded",
            "eligible": eligible,
            "packet_id": packet["packet_id"],
            "packet_sha256": manifest["packet_sha256"],
            "content_set_sha256": manifest["content_set_sha256"],
            "order_variant": manifest["order_variant"],
            "adapter": meta["adapter"],
            "judge": meta["judge"],
            "attempt_number": meta["attempt_number"],
            "raw_answer_sha256": raw_hash,
            "canonical_answer_sha256": sha256_bytes(canonical_bytes(canonical))
            if canonical
            else None,
            "adapter_attestation_sha256": sha256_bytes(attestation_data),
            "gates": {"structure": structure, "quality": quality, "cost": cost},
            "quality": meta["quality"],
            "usage": meta["usage"],
            "budget": manifest["budget"],
        }
        if canonical is not None and structure["status"] == "pass":
            _write_new(output_dir / "canonical-answer.json", canonical_bytes(canonical))
        _write_new(admission_path, canonical_bytes(admission))
        return admission
    except Exception as exc:
        if not admission_path.exists():
            admission = {
                "schema_version": SCHEMA_VERSION,
                "protocol": PROTOCOL,
                "status": "excluded",
                "eligible": False,
                "raw_answer_sha256": sha256_bytes(raw) if raw is not None else None,
                "gates": {
                    "structure": _gate("fail", [str(exc)]),
                    "quality": _gate("unknown", ["validation-aborted"]),
                    "cost": _gate("unknown", ["validation-aborted"]),
                },
            }
            _write_new(admission_path, canonical_bytes(admission))
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError(str(exc)) from exc


def _identity_bundle(
    bundle_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    packet, manifest = _load_bundle(bundle_dir)
    identity = _require_object(
        read_json(bundle_dir / "identity-map.json"), "identity map"
    )
    if sha256_file(bundle_dir / "identity-map.json") != manifest.get(
        "identity_map_sha256"
    ):
        raise ProtocolError("identity-map hash mismatch")
    if identity.get("packet_sha256") != manifest["packet_sha256"]:
        raise ProtocolError("identity map is not linked to packet")
    _closed_keys(
        identity,
        required=(
            "schema_version",
            "protocol",
            "packet_id",
            "order_variant",
            "packet_sha256",
            "content_set_sha256",
            "labels",
            "judge_roster",
        ),
        where="identity map",
    )
    if identity["schema_version"] != SCHEMA_VERSION:
        raise ProtocolError("identity map schema mismatch")
    for key in ("protocol", "packet_id", "order_variant", "content_set_sha256"):
        if identity[key] != manifest[key]:
            raise ProtocolError(f"identity map {key} mismatch")
    rows = identity.get("labels")
    if not isinstance(rows, list) or [row.get("label") for row in rows] != list(LABELS):
        raise ProtocolError("identity map labels are invalid")
    public_rows = {row["label"]: row for row in packet["candidates"]}
    candidate_ids: set[str] = set()
    for index, row in enumerate(rows):
        _closed_keys(
            row,
            required=(
                "label",
                "candidate_id",
                "family_id",
                "identity_aliases",
                "response_sha256",
                "checker_sha256",
            ),
            where=f"identity map.labels[{index}]",
        )
        candidate_id = _slug(
            row["candidate_id"], f"identity map.labels[{index}].candidate_id"
        )
        _slug(row["family_id"], f"identity map.labels[{index}].family_id")
        _validate_aliases(
            row["identity_aliases"],
            f"identity map.labels[{index}].identity_aliases",
        )
        if candidate_id in candidate_ids:
            raise ProtocolError("identity map contains duplicate candidate IDs")
        candidate_ids.add(candidate_id)
        if row["response_sha256"] != public_rows[row["label"]]["response_sha256"]:
            raise ProtocolError("identity map response hash mismatch")
        if not isinstance(row["checker_sha256"], str) or not SHA256.fullmatch(
            row["checker_sha256"]
        ):
            raise ProtocolError("identity map checker hash is invalid")
        if row["checker_sha256"] != public_rows[row["label"]]["checker_sha256"]:
            raise ProtocolError("identity map checker hash mismatch")
    identity["judge_roster"] = _validate_judge_roster(identity["judge_roster"])
    commitments = [
        _candidate_commitment(
            row["candidate_id"],
            row["family_id"],
            row["identity_aliases"],
            row["response_sha256"],
            row["checker_sha256"],
        )
        for row in rows
    ]
    if sha256_bytes(b"".join(sorted(commitments))) != manifest["content_set_sha256"]:
        raise ProtocolError("identity map content-set commitment mismatch")
    roster_failures = _roster_coverage_failures(rows, identity["judge_roster"])
    if manifest["roster_failures"] != roster_failures:
        raise ProtocolError("bundle roster failures were not reproduced")
    reproduced_failures = manifest["packet_failures"] + roster_failures
    if manifest["failures"] != reproduced_failures:
        raise ProtocolError("bundle failure list was not reproduced")
    expected_status = "ready" if not reproduced_failures else "failed-closed"
    if manifest["status"] != expected_status:
        raise ProtocolError("bundle readiness does not match recomputed gates")
    return packet, manifest, identity


def _identity_outcome(
    pair: Mapping[str, Any], labels: Mapping[str, Mapping[str, Any]]
) -> str:
    if pair["outcome"] in {"tie", "insufficient"}:
        return pair["outcome"]
    winner_label = pair["a"] if pair["outcome"] == "a" else pair["b"]
    return labels[winner_label]["candidate_id"]


def _dominance_layers(
    nodes: Sequence[str], edges: Sequence[tuple[str, str]]
) -> list[list[str]] | None:
    incoming = {node: 0 for node in nodes}
    outgoing: dict[str, set[str]] = {node: set() for node in nodes}
    for winner, loser in edges:
        if winner == loser or loser in outgoing[winner]:
            continue
        outgoing[winner].add(loser)
        incoming[loser] += 1
    remaining = set(nodes)
    layers: list[list[str]] = []
    while remaining:
        layer = sorted(node for node in remaining if incoming[node] == 0)
        if not layer:
            return None
        layers.append(layer)
        for node in layer:
            remaining.remove(node)
            for target in outgoing[node]:
                incoming[target] -= 1
    return layers


def _verify_admitted_submission(
    submission_dir: Path,
    packet: Mapping[str, Any],
    manifest: Mapping[str, Any],
    identity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    admission = _require_object(
        read_json(submission_dir / "admission.json"), "admission"
    )
    _closed_keys(
        admission,
        required=(
            "schema_version",
            "protocol",
            "status",
            "eligible",
            "packet_id",
            "packet_sha256",
            "content_set_sha256",
            "order_variant",
            "adapter",
            "judge",
            "attempt_number",
            "raw_answer_sha256",
            "canonical_answer_sha256",
            "adapter_attestation_sha256",
            "gates",
            "quality",
            "usage",
            "budget",
        ),
        where="admission",
    )
    if (
        admission["schema_version"] != SCHEMA_VERSION
        or admission["protocol"] != PROTOCOL
    ):
        raise ProtocolError("admission schema or protocol mismatch")
    if admission["status"] != "admitted" or admission["eligible"] is not True:
        raise ProtocolError("submission is not admitted")
    for key in ("packet_id", "packet_sha256", "content_set_sha256", "order_variant"):
        if admission[key] != manifest[key]:
            raise ProtocolError(f"admission {key} mismatch")
    if admission["attempt_number"] != 1 or admission["budget"] != manifest["budget"]:
        raise ProtocolError("admission attempt or budget mismatch")
    gates = _require_object(admission["gates"], "admission.gates")
    if set(gates) != {"structure", "quality", "cost"} or any(
        not isinstance(gates[name], dict) or gates[name].get("status") != "pass"
        for name in gates
    ):
        raise ProtocolError("admitted submission does not have three passing gates")

    attestation_path = submission_dir / "adapter-attestation.json"
    if sha256_file(attestation_path) != admission["adapter_attestation_sha256"]:
        raise ProtocolError("adapter attestation hash mismatch")
    attestation = _require_object(read_json(attestation_path), "adapter attestation")
    if attestation.get("packet_sha256") != manifest["packet_sha256"]:
        raise ProtocolError("adapter attestation packet mismatch")
    if attestation.get("prompt_sha256") != manifest["prompt_sha256"]:
        raise ProtocolError("adapter attestation prompt mismatch")
    if attestation.get("order_variant") != manifest["order_variant"]:
        raise ProtocolError("adapter attestation order mismatch")
    if attestation.get("attempt_number") != 1:
        raise ProtocolError("adapter attestation is not the first attempt")
    if admission["adapter"] != attestation.get("adapter"):
        raise ProtocolError("admission adapter mismatch")
    if admission["judge"] != attestation.get("judge"):
        raise ProtocolError("admission judge mismatch")
    roster = {row["judge_id"]: row for row in identity["judge_roster"]}
    judge = admission["judge"]
    pinned = roster.get(judge.get("judge_id")) if isinstance(judge, dict) else None
    if pinned is None or any(
        judge.get(key) != pinned[key] for key in ("family_id", "route_id")
    ):
        raise ProtocolError("admission judge is not pinned to this bundle")
    if admission["adapter"].get("adapter_id") != pinned["adapter_id"]:
        raise ProtocolError("admission adapter is not pinned to this bundle")
    if admission["adapter"].get("version") != pinned["adapter_version"]:
        raise ProtocolError("admission adapter version is not pinned to this bundle")

    raw_path = submission_dir / "raw-answer.bin"
    raw = _read_bounded(raw_path, MAX_RAW_ANSWER_BYTES, "stored raw first answer")
    raw_hash = sha256_bytes(raw)
    if raw_hash != admission["raw_answer_sha256"] or raw_hash != attestation.get(
        "raw_answer_sha256"
    ):
        raise ProtocolError("raw first-answer hash mismatch")
    canonical = validate_answer_object(
        strict_json_bytes(raw, source="stored raw first answer"), packet
    )
    canonical_path = submission_dir / "canonical-answer.json"
    canonical_data = canonical_bytes(canonical)
    if canonical_path.read_bytes() != canonical_data:
        raise ProtocolError(
            "canonical answer is not derived from the stored raw answer"
        )
    if sha256_bytes(canonical_data) != admission["canonical_answer_sha256"]:
        raise ProtocolError("canonical answer commitment mismatch")

    quality_hash = sha256_file(submission_dir / "quality-evidence.bin")
    usage_hash = sha256_file(submission_dir / "usage-evidence.bin")
    pricing_hash = sha256_file(submission_dir / "pricing-snapshot.bin")
    if quality_hash != admission["quality"].get("evidence_sha256"):
        raise ProtocolError("quality evidence commitment mismatch")
    if usage_hash != admission["usage"].get("evidence_sha256"):
        raise ProtocolError("usage evidence commitment mismatch")
    if pricing_hash != admission["usage"].get("pricing_snapshot_sha256"):
        raise ProtocolError("pricing snapshot commitment mismatch")
    if admission["quality"] != attestation.get("quality"):
        raise ProtocolError("quality attestation mismatch")
    if admission["usage"] != attestation.get("usage"):
        raise ProtocolError("usage attestation mismatch")
    if admission["quality"].get("status") != "pass":
        raise ProtocolError("admitted quality gate is not backed by a pass attestation")
    if _cost_gate(admission["usage"], manifest["budget"])["status"] != "pass":
        raise ProtocolError("admitted cost gate cannot be recomputed as pass")
    return admission, canonical, attestation


def aggregate_submissions(records_path: Path, output_path: Path) -> dict[str, Any]:
    spec = _require_object(read_json(records_path), "aggregate records")
    _closed_keys(spec, required=("schema_version", "runs"), where="aggregate records")
    if spec["schema_version"] != SCHEMA_VERSION:
        raise ProtocolError(
            f"aggregate records schema_version must be {SCHEMA_VERSION}"
        )
    raw_runs = _require_list(spec["runs"], "aggregate records.runs")
    if not raw_runs:
        raise ProtocolError("aggregate records.runs must not be empty")

    observations: dict[tuple[str, tuple[str, str]], list[dict[str, Any]]] = defaultdict(
        list
    )
    candidate_families: dict[str, str] = {}
    content_set: str | None = None
    judge_reports: list[dict[str, Any]] = []
    packet_ids: set[str] = set()
    primary_hashes: set[str] = set()
    reverse_links: list[str] = []
    judge_routes: dict[str, tuple[str, str, str, str]] = {}
    seen_judge_variants: set[tuple[str, str]] = set()
    eligible_run_links: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for index, raw in enumerate(raw_runs):
        row = _require_object(raw, f"aggregate records.runs[{index}]")
        _closed_keys(
            row,
            required=("bundle_dir", "submission_dir"),
            where=f"aggregate records.runs[{index}]",
        )
        base = records_path.resolve().parent
        bundle_dir = _resolve_path(base, row["bundle_dir"], "bundle_dir")
        submission_dir = _resolve_path(base, row["submission_dir"], "submission_dir")
        packet, manifest, identity = _identity_bundle(bundle_dir)
        if manifest["order_variant"] == "primary":
            if manifest.get("primary_packet_sha256") is not None:
                raise ProtocolError("primary bundle must not link another primary")
            primary_hashes.add(manifest["packet_sha256"])
        else:
            link = manifest.get("primary_packet_sha256")
            if not isinstance(link, str) or not SHA256.fullmatch(link):
                raise ProtocolError("reverse bundle has no valid primary link")
            reverse_links.append(link)
        admission = _require_object(
            read_json(submission_dir / "admission.json"), "admission"
        )
        if admission.get("protocol") != PROTOCOL:
            raise ProtocolError("admission protocol mismatch")
        if (
            admission.get("eligible") is True
            and admission.get("packet_sha256") != manifest["packet_sha256"]
        ):
            raise ProtocolError("admission is not linked to bundle")
        if content_set is None:
            content_set = manifest["content_set_sha256"]
        elif content_set != manifest["content_set_sha256"]:
            raise ProtocolError(
                "aggregate runs do not share one content-set commitment"
            )
        packet_ids.add(manifest["packet_id"])
        label_rows = {item["label"]: item for item in identity["labels"]}
        for item in identity["labels"]:
            existing = candidate_families.setdefault(
                item["candidate_id"], item["family_id"]
            )
            if existing != item["family_id"]:
                raise ProtocolError("candidate family changed across bundles")
        report = {
            "judge": admission.get("judge"),
            "adapter": admission.get("adapter"),
            "order_variant": manifest["order_variant"],
            "eligible": admission.get("eligible") is True,
            "gates": admission.get("gates"),
            "usage": admission.get("usage"),
        }
        judge_reports.append(report)
        if admission.get("eligible") is not True:
            continue
        admission, answer, attestation = _verify_admitted_submission(
            submission_dir, packet, manifest, identity
        )
        judge = admission["judge"]
        route_tuple = (
            judge["family_id"],
            judge["route_id"],
            admission["adapter"]["adapter_id"],
            admission["adapter"]["version"],
        )
        previous_route = judge_routes.setdefault(judge["judge_id"], route_tuple)
        if previous_route != route_tuple:
            raise ProtocolError("judge route tuple changed across submissions")
        judge_variant = (judge["judge_id"], manifest["order_variant"])
        if judge_variant in seen_judge_variants:
            raise ProtocolError("duplicate judge/order submission")
        seen_judge_variants.add(judge_variant)
        eligible_run_links[judge["judge_id"]][manifest["order_variant"]] = {
            "packet_sha256": manifest["packet_sha256"],
            "primary_packet_sha256": manifest.get("primary_packet_sha256"),
            "order_recheck_trigger": manifest.get("order_recheck_trigger"),
        }
        for pair in answer["pairs"]:
            left = label_rows[pair["a"]]["candidate_id"]
            right = label_rows[pair["b"]]["candidate_id"]
            canonical_pair = tuple(sorted((left, right)))
            observations[(judge["judge_id"], canonical_pair)].append(
                {
                    "judge_id": judge["judge_id"],
                    "judge_family": judge["family_id"],
                    "judge_route": judge["route_id"],
                    "adapter_id": attestation["adapter"]["adapter_id"],
                    "order_variant": manifest["order_variant"],
                    "packet_sha256": manifest["packet_sha256"],
                    "primary_packet_sha256": manifest.get("primary_packet_sha256"),
                    "outcome": _identity_outcome(pair, label_rows),
                }
            )

    if len(packet_ids) != 1:
        raise ProtocolError("aggregate runs must use one packet_id")
    if any(link not in primary_hashes for link in reverse_links):
        raise ProtocolError(
            "reverse submission is not linked to an included primary bundle"
        )
    for judge_id, variants in eligible_run_links.items():
        if "reverse" not in variants:
            continue
        if "primary" not in variants:
            raise ProtocolError(
                f"eligible reverse for {judge_id} has no eligible linked primary"
            )
        if (
            variants["reverse"]["primary_packet_sha256"]
            != variants["primary"]["packet_sha256"]
        ):
            raise ProtocolError(
                f"eligible reverse for {judge_id} links another primary"
            )
        if variants["reverse"]["order_recheck_trigger"] not in {"close", "conflict"}:
            raise ProtocolError(f"eligible reverse for {judge_id} has no valid trigger")
    candidate_ids = sorted(candidate_families)
    if len(candidate_ids) != len(LABELS):
        raise ProtocolError("aggregate candidate set must contain seven candidates")

    pair_results: list[dict[str, Any]] = []
    edges: list[tuple[str, str]] = []
    for pair in itertools.combinations(candidate_ids, 2):
        family_votes: dict[str, list[str]] = defaultdict(list)
        unstable = False
        judge_details: list[dict[str, Any]] = []
        judge_ids = sorted({key[0] for key in observations if key[1] == pair})
        for judge_id in judge_ids:
            rows = observations[(judge_id, pair)]
            judge_family = rows[0]["judge_family"]
            if judge_family in {
                candidate_families[pair[0]],
                candidate_families[pair[1]],
            }:
                judge_details.append(
                    {"judge_id": judge_id, "status": "same-family-excluded"}
                )
                continue
            outcomes = [row["outcome"] for row in rows]
            if len(rows) > 1:
                variants = {row["order_variant"]: row for row in rows}
                if set(variants) != {"primary", "reverse"}:
                    raise ProtocolError(
                        "order recheck must contain one primary and one reverse"
                    )
                if (
                    variants["reverse"]["primary_packet_sha256"]
                    != variants["primary"]["packet_sha256"]
                ):
                    raise ProtocolError(
                        "judge reverse is not linked to its primary run"
                    )
            substantive = set(outcomes)
            if len(rows) > 1 and len(substantive) > 1:
                unstable = True
                judge_details.append(
                    {
                        "judge_id": judge_id,
                        "status": "order-unstable",
                        "outcomes": outcomes,
                    }
                )
                continue
            outcome = outcomes[0]
            family_votes[judge_family].append(outcome)
            judge_details.append(
                {"judge_id": judge_id, "status": "eligible", "outcome": outcome}
            )

        collapsed: dict[str, str] = {}
        family_conflict = False
        for family, votes in family_votes.items():
            substantive_votes = set(votes) - {"insufficient"}
            if len(substantive_votes) > 1:
                family_conflict = True
            elif substantive_votes:
                collapsed[family] = next(iter(substantive_votes))
            else:
                collapsed[family] = "insufficient"
        if unstable:
            status = "order-unstable"
            winner = None
        elif len(family_votes) < 2:
            status = "pair-family-limited"
            winner = None
        elif family_conflict:
            status = "disputed"
            winner = None
        else:
            decisive_votes = [
                vote for vote in collapsed.values() if vote != "insufficient"
            ]
            unique_votes = set(decisive_votes)
            if len(decisive_votes) < 2:
                status = "insufficient-evidence"
                winner = None
            elif len(unique_votes) > 1:
                status = "disputed"
                winner = None
            elif next(iter(unique_votes)) == "tie":
                status = "tie"
                winner = None
            else:
                status = "decided"
                winner = next(iter(unique_votes))
                loser = pair[1] if winner == pair[0] else pair[0]
                edges.append((winner, loser))
        pair_results.append(
            {
                "candidates": list(pair),
                "status": status,
                "winner": winner,
                "eligible_family_votes": collapsed,
                "judge_details": judge_details,
            }
        )

    layers = _dominance_layers(candidate_ids, edges)
    aggregate = {
        "schema_version": SCHEMA_VERSION,
        "protocol": PROTOCOL,
        "publication_status": "Provisional",
        "exact_ranking_authorized": False,
        "reason": "one frozen task cannot establish a stable exact ranking",
        "packet_id": next(iter(packet_ids)),
        "content_set_sha256": content_set,
        "candidates": [
            {
                "candidate_id": candidate_id,
                "family_id": candidate_families[candidate_id],
            }
            for candidate_id in candidate_ids
        ],
        "judge_runs": judge_reports,
        "pairs": pair_results,
        "partial_order_edges": [list(edge) for edge in sorted(set(edges))],
        "dominance_layers": layers,
        "cycle_detected": layers is None,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise ProtocolError(f"refusing to overwrite {output_path}")
    _write_new(output_path, canonical_bytes(aggregate))
    return aggregate


def _parse_order(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="freeze a seven-candidate blind bundle")
    build.add_argument("--spec", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--order", help="comma-separated candidate IDs")
    build.add_argument("--reverse-of", type=Path)
    build.add_argument("--reverse-trigger", choices=("close", "conflict"))

    validate = subparsers.add_parser(
        "validate", help="validate and admit one first answer"
    )
    validate.add_argument("--bundle", type=Path, required=True)
    validate.add_argument("--answer", type=Path, required=True)
    validate.add_argument("--run-meta", type=Path, required=True)
    validate.add_argument("--output", type=Path, required=True)

    aggregate = subparsers.add_parser(
        "aggregate", help="aggregate admitted family votes"
    )
    aggregate.add_argument("--records", type=Path, required=True)
    aggregate.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_bundle(
                args.spec,
                args.output,
                order=_parse_order(args.order),
                reverse_of=args.reverse_of,
                reverse_trigger=args.reverse_trigger,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["status"] == "ready" else 2
        if args.command == "validate":
            result = validate_submission(
                args.bundle, args.answer, args.run_meta, args.output
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["eligible"] else 3
        result = aggregate_submissions(args.records, args.output)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except ProtocolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
