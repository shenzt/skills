from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import blind_judge_protocol as protocol
from blind_judge_protocol import (
    LABELS,
    ProtocolError,
    aggregate_submissions,
    build_bundle,
    canonical_bytes,
    expected_pairs,
    read_json,
    sha256_bytes,
    validate_answer_object,
    validate_submission,
)


@contextmanager
def _raises(error_type: type[BaseException], match: str | None = None):
    try:
        yield
    except error_type as exc:
        if match is not None and match not in str(exc):
            raise AssertionError(f"{match!r} not found in {str(exc)!r}") from exc
    else:
        raise AssertionError(f"expected {error_type.__name__}")


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_bytes(value))


def _make_spec(
    tmp_path: Path, *, max_tokens: int = 50_000, response_size: int = 40
) -> Path:
    candidates = []
    for index in range(7):
        response = tmp_path / f"response-{index}.txt"
        checker = tmp_path / f"checker-{index}.json"
        response.write_text(
            f"方案 {index}：" + chr(0x4E00 + index) * response_size, encoding="utf-8"
        )
        _write_json(
            checker,
            {
                "schema_version": 1,
                "status": "pass",
                "facts": [
                    {
                        "fact_id": "required-check",
                        "status": "pass",
                        "text": f"自动检查 {index} 已通过",
                    }
                ],
            },
        )
        candidates.append(
            {
                "candidate_id": f"candidate-{index}",
                "family_id": f"candidate-family-{index}",
                "identity_aliases": [f"private-model-alias-{index}"],
                "response_path": response.name,
                "checker_path": checker.name,
            }
        )
    spec = {
        "schema_version": 1,
        "packet_id": "sample-task",
        "task": {
            "prompt": "在约束内提出方案。",
            "context": "所有候选获得相同上下文。",
            "hard_constraints": ["不得捏造检查结果"],
        },
        "rubric": [
            {"id": "correctness", "name": "正确性", "description": "事实和结论正确"},
            {"id": "constraints", "name": "约束", "description": "满足硬约束"},
        ],
        "candidates": candidates,
        "judge_roster": [
            {
                "judge_id": judge_id,
                "family_id": family_id,
                "route_id": f"route-{judge_id}",
                "adapter_id": "test-adapter",
                "adapter_version": "1.0.0",
            }
            for judge_id, family_id in (
                ("judge-1", "judge-family-1"),
                ("judge-2", "judge-family-2"),
                ("judge-3", "judge-family-3"),
                ("judge-4", "judge-family-4"),
                ("judge-5", "judge-family-5"),
                ("judge-a", "judge-family-a"),
                ("judge-b", "judge-family-b"),
                ("judge-c", "same-judge-family"),
                ("judge-d", "same-judge-family"),
            )
        ],
        "order_recheck_policy": {
            "allowed_triggers": ["close", "conflict"],
            "max_rechecks_per_judge": 1,
        },
        "budget": {
            "max_total_tokens": max_tokens,
            "adapter_input_reserve_tokens": 512,
            "reserved_output_tokens": 16_384,
            "max_cash_usd": 2.0,
        },
    }
    path = tmp_path / "spec.json"
    _write_json(path, spec)
    return path


def _valid_answer(packet: dict, *, outcome: str = "a") -> dict:
    evidence = []
    response_evidence: dict[str, str] = {}
    checker_evidence: dict[str, str] = {}
    for row in packet["candidates"]:
        label = row["label"]
        response_id = f"response-{label.lower()}"
        checker_id = f"checker-{label.lower()}"
        response_evidence[label] = response_id
        checker_evidence[label] = checker_id
        evidence.extend(
            [
                {
                    "id": response_id,
                    "candidate": label,
                    "source": "response",
                    "quote": row["response"][:8],
                },
                {
                    "id": checker_id,
                    "candidate": label,
                    "source": "checker:required-check",
                    "quote": row["checker"]["facts"][0]["text"],
                },
            ]
        )
    assessments = []
    for label in LABELS:
        assessments.append(
            {
                "candidate": label,
                "dimensions": {
                    dimension["id"]: {
                        "verdict": "pass",
                        "evidence": [response_evidence[label], checker_evidence[label]],
                        "reason": "有候选文本和冻结检查证据。",
                    }
                    for dimension in packet["rubric"]
                },
                "decisive_errors": [],
                "hard_constraint_violations": [],
                "confidence": "high",
            }
        )
    return {
        "schema_version": 1,
        "packet_id": packet["packet_id"],
        "evidence": evidence,
        "assessments": assessments,
        "pairs": [
            {
                "a": a,
                "b": b,
                "outcome": outcome,
                "evidence": [response_evidence[a], response_evidence[b]],
                "reason": "双方完整输出均已核对。",
            }
            for a, b in expected_pairs()
        ],
    }


def _run_meta(
    judge_id: str,
    family_id: str,
    *,
    variant: str = "primary",
    attempt: int = 1,
    quality: str = "pass",
    cash: float | None = 0.1,
    cost_status: str = "known",
) -> dict:
    return {
        "judge_id": judge_id,
        "family_id": family_id,
        "attempt_number": attempt,
        "order_variant": variant,
        "quality_status": quality,
        "cash": cash,
        "cost_status": cost_status,
    }


def _materialize_meta(
    tmp_path: Path,
    bundle: Path,
    raw: bytes,
    config: dict,
    name: str,
) -> Path:
    manifest = read_json(bundle / "bundle-manifest.json")
    quality_path = tmp_path / f"{name}-quality.json"
    usage_path = tmp_path / f"{name}-usage.json"
    pricing_path = tmp_path / f"{name}-pricing.json"
    _write_json(
        quality_path,
        {"calibration": "pre-registered", "status": config["quality_status"]},
    )
    _write_json(
        usage_path,
        {"provider_total_tokens": 2_000, "source": "test-adapter-terminal-event"},
    )
    _write_json(
        pricing_path,
        {"route": f"route-{config['judge_id']}", "pricing_status": "frozen"},
    )
    meta = {
        "schema_version": 1,
        "adapter": {"adapter_id": "test-adapter", "version": "1.0.0"},
        "packet_sha256": manifest["packet_sha256"],
        "prompt_sha256": manifest["prompt_sha256"],
        "raw_answer_sha256": sha256_bytes(raw),
        "judge": {
            "judge_id": config["judge_id"],
            "family_id": config["family_id"],
            "route_id": f"route-{config['judge_id']}",
        },
        "attempt_number": config["attempt_number"],
        "order_variant": config["order_variant"],
        "quality": {
            "status": config["quality_status"],
            "evidence_path": quality_path.name,
            "evidence_sha256": sha256_bytes(quality_path.read_bytes()),
        },
        "usage": {
            "safe_input_tokens": 1_000,
            "safe_output_tokens": 1_000,
            "provider_total_tokens": 2_000,
            "conservative_cash_usd": config["cash"],
            "provider_cost_status": config["cost_status"],
            "evidence_path": usage_path.name,
            "evidence_sha256": sha256_bytes(usage_path.read_bytes()),
            "pricing_snapshot_path": pricing_path.name,
            "pricing_snapshot_sha256": sha256_bytes(pricing_path.read_bytes()),
        },
    }
    meta_path = tmp_path / f"{name}-meta.json"
    _write_json(meta_path, meta)
    return meta_path


def _submit(
    tmp_path: Path,
    bundle: Path,
    answer: dict,
    meta: dict,
    name: str,
) -> tuple[Path, dict]:
    answer_path = tmp_path / f"{name}-answer.json"
    output = tmp_path / f"{name}-submission"
    _write_json(answer_path, answer)
    meta_path = _materialize_meta(
        tmp_path, bundle, answer_path.read_bytes(), meta, name
    )
    return output, validate_submission(bundle, answer_path, meta_path, output)


def _case_build_freezes_blind_primary_and_exact_reverse(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    primary = tmp_path / "primary"
    reverse = tmp_path / "reverse"
    primary_manifest = build_bundle(spec, primary)
    reverse_manifest = build_bundle(
        spec, reverse, reverse_of=primary, reverse_trigger="conflict"
    )

    assert primary_manifest["status"] == reverse_manifest["status"] == "ready"
    assert (
        primary_manifest["content_set_sha256"] == reverse_manifest["content_set_sha256"]
    )
    assert (
        reverse_manifest["primary_packet_sha256"] == primary_manifest["packet_sha256"]
    )
    primary_private = read_json(primary / "identity-map.json")
    reverse_private = read_json(reverse / "identity-map.json")
    assert [row["candidate_id"] for row in reverse_private["labels"]] == list(
        reversed([row["candidate_id"] for row in primary_private["labels"]])
    )
    assert stat_mode(primary / "identity-map.json") == 0o600

    public_text = (primary / "packet.json").read_text(encoding="utf-8")
    for index in range(7):
        assert f"candidate-{index}" not in public_text
        assert f"candidate-family-{index}" not in public_text
    prompt_text = (primary / "prompt.txt").read_text(encoding="utf-8")
    assert '"rank"' not in prompt_text
    assert '"pair_outcome":["a","tie","b","insufficient"]' in prompt_text
    assert '"verdict":["concern","insufficient","pass"]' in prompt_text
    assert '"confidence":["high","low","medium"]' in prompt_text
    assert '"issue_item"' in prompt_text


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def _case_build_fails_closed_without_checker_and_when_18k_not_proven(
    tmp_path: Path,
) -> None:
    missing_dir = tmp_path / "missing"
    # Keep the fixture directory explicit so the failure manifest location is deterministic.
    source = tmp_path / "source"
    source.mkdir()
    spec = _make_spec(source)
    (source / "checker-6.json").unlink()
    with _raises(ProtocolError):
        build_bundle(spec, missing_dir)
    assert read_json(missing_dir / "bundle-manifest.json")["status"] == "failed-closed"

    large_source = tmp_path / "large-source"
    large_source.mkdir()
    large_spec = _make_spec(large_source, max_tokens=18_000, response_size=1_500)
    large_output = tmp_path / "large-output"
    manifest = build_bundle(large_spec, large_output)
    assert manifest["status"] == "failed-closed"
    assert "conservative-total-token-upper-bound-exceeded" in manifest["failures"]
    assert manifest["preflight"]["truncated"] is False
    assert manifest["preflight"]["split"] is False
    assert (large_output / "packet.json").is_file()
    tampered_manifest = read_json(large_output / "bundle-manifest.json")
    tampered_manifest["status"] = "ready"
    _write_json(large_output / "bundle-manifest.json", tampered_manifest)
    with _raises(ProtocolError, "relabeled ready"):
        build_bundle(
            large_spec,
            tmp_path / "tampered-reverse",
            reverse_of=large_output,
            reverse_trigger="conflict",
        )

    coverage_source = tmp_path / "coverage-source"
    coverage_source.mkdir()
    coverage_spec_path = _make_spec(coverage_source)
    coverage_spec = read_json(coverage_spec_path)
    coverage_spec["candidates"][0]["family_id"] = "judge-family-a"
    coverage_spec["candidates"][1]["family_id"] = "judge-family-b"
    coverage_spec["judge_roster"] = coverage_spec["judge_roster"][5:7]
    _write_json(coverage_spec_path, coverage_spec)
    coverage_manifest = build_bundle(coverage_spec_path, tmp_path / "coverage-output")
    assert coverage_manifest["status"] == "failed-closed"
    assert coverage_manifest["roster_failures"] == [
        "judge-roster-pair-coverage-insufficient"
    ]


def _case_first_answer_is_strict_and_gates_do_not_compensate(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    bundle = tmp_path / "bundle"
    build_bundle(spec, bundle)
    packet = read_json(bundle / "packet.json")
    answer = _valid_answer(packet)

    valid_dir, valid = _submit(
        tmp_path, bundle, answer, _run_meta("judge-1", "judge-family-1"), "valid"
    )
    assert valid["eligible"] is True
    assert (valid_dir / "canonical-answer.json").is_file()

    fenced_path = tmp_path / "fenced-answer.txt"
    fenced_path.write_text(
        "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```", encoding="utf-8"
    )
    meta_path = _materialize_meta(
        tmp_path,
        bundle,
        fenced_path.read_bytes(),
        _run_meta("judge-2", "judge-family-2"),
        "fenced",
    )
    fenced = validate_submission(
        bundle, fenced_path, meta_path, tmp_path / "fenced-submission"
    )
    assert fenced["gates"]["structure"]["status"] == "fail"
    assert fenced["gates"]["quality"]["status"] == "pass"
    assert fenced["gates"]["cost"]["status"] == "pass"
    assert not (tmp_path / "fenced-submission" / "canonical-answer.json").exists()

    _, retry = _submit(
        tmp_path,
        bundle,
        answer,
        _run_meta("judge-3", "judge-family-3", attempt=2),
        "retry",
    )
    assert retry["gates"]["structure"]["status"] == "fail"

    _, no_quality = _submit(
        tmp_path,
        bundle,
        answer,
        _run_meta("judge-4", "judge-family-4", quality="unknown"),
        "no-quality",
    )
    assert no_quality["gates"]["quality"]["status"] == "unknown"

    _, ambiguous_cost = _submit(
        tmp_path,
        bundle,
        answer,
        _run_meta(
            "judge-5",
            "judge-family-5",
            cash=0.0,
            cost_status="ambiguous-zero",
        ),
        "ambiguous-cost",
    )
    assert ambiguous_cost["gates"]["cost"]["status"] == "unknown"


def _case_validator_rejects_fabrication_third_candidate_and_rank(
    tmp_path: Path,
) -> None:
    spec = _make_spec(tmp_path)
    bundle = tmp_path / "bundle"
    build_bundle(spec, bundle)
    packet = read_json(bundle / "packet.json")

    fabricated = _valid_answer(packet)
    fabricated["evidence"][0]["quote"] = "不存在的原文"
    with _raises(ProtocolError, match="exact response substring"):
        validate_answer_object(fabricated, packet)

    third_party = _valid_answer(packet)
    third_party["pairs"][0]["evidence"].append("response-c")
    with _raises(ProtocolError, match="third candidate"):
        validate_answer_object(third_party, packet)

    ranked = _valid_answer(packet)
    ranked["rank"] = list(LABELS)
    with _raises(ProtocolError, match="keys mismatch"):
        validate_answer_object(ranked, packet)

    duplicate_pair = _valid_answer(packet)
    duplicate_pair["pairs"][-1] = duplicate_pair["pairs"][0]
    with _raises(ProtocolError, match="duplicate pair"):
        validate_answer_object(duplicate_pair, packet)


def _case_duplicate_json_key_is_not_a_first_answer(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    bundle = tmp_path / "bundle"
    build_bundle(spec, bundle)
    packet = read_json(bundle / "packet.json")
    answer = _valid_answer(packet)
    raw = json.dumps(answer, ensure_ascii=False)
    raw = raw.replace(
        '{"schema_version": 1,', '{"schema_version": 1, "schema_version": 1,', 1
    )
    answer_path = tmp_path / "duplicate.json"
    answer_path.write_text(raw, encoding="utf-8")
    meta_path = _materialize_meta(
        tmp_path,
        bundle,
        answer_path.read_bytes(),
        _run_meta("judge-1", "judge-family-1"),
        "duplicate",
    )
    admission = validate_submission(
        bundle, answer_path, meta_path, tmp_path / "submission"
    )
    assert admission["gates"]["structure"]["status"] == "fail"


def _case_aggregate_collapses_family_votes_and_never_forces_rank(
    tmp_path: Path,
) -> None:
    spec = _make_spec(tmp_path)
    bundle = tmp_path / "bundle"
    build_bundle(spec, bundle)
    packet = read_json(bundle / "packet.json")
    answer = _valid_answer(packet)
    runs = []
    for judge_id, family_id in (
        ("judge-a", "judge-family-a"),
        ("judge-b", "judge-family-b"),
    ):
        submission, admission = _submit(
            tmp_path,
            bundle,
            answer,
            _run_meta(judge_id, family_id),
            judge_id,
        )
        assert admission["eligible"]
        runs.append({"bundle_dir": str(bundle), "submission_dir": str(submission)})
    records = tmp_path / "records.json"
    _write_json(records, {"schema_version": 1, "runs": runs})
    aggregate = aggregate_submissions(records, tmp_path / "aggregate.json")
    assert aggregate["exact_ranking_authorized"] is False
    assert all(row["status"] == "decided" for row in aggregate["pairs"])
    assert len(aggregate["partial_order_edges"]) == 21
    assert aggregate["dominance_layers"] == [
        [f"candidate-{index}"] for index in range(7)
    ]

    one_family_runs = []
    for judge_id in ("judge-c", "judge-d"):
        submission, _ = _submit(
            tmp_path,
            bundle,
            answer,
            _run_meta(judge_id, "same-judge-family"),
            judge_id,
        )
        one_family_runs.append(
            {"bundle_dir": str(bundle), "submission_dir": str(submission)}
        )
    one_family_records = tmp_path / "one-family-records.json"
    _write_json(one_family_records, {"schema_version": 1, "runs": one_family_runs})
    limited = aggregate_submissions(one_family_records, tmp_path / "limited.json")
    assert all(row["status"] == "pair-family-limited" for row in limited["pairs"])


def _case_exact_reverse_direction_flip_is_order_unstable(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    primary = tmp_path / "primary"
    reverse = tmp_path / "reverse"
    build_bundle(spec, primary)
    build_bundle(spec, reverse, reverse_of=primary, reverse_trigger="conflict")
    primary_answer = _valid_answer(read_json(primary / "packet.json"), outcome="a")
    reverse_answer = _valid_answer(read_json(reverse / "packet.json"), outcome="a")

    primary_submission, _ = _submit(
        tmp_path,
        primary,
        primary_answer,
        _run_meta("judge-a", "judge-family-a"),
        "judge-a-primary",
    )
    reverse_submission, _ = _submit(
        tmp_path,
        reverse,
        reverse_answer,
        _run_meta("judge-a", "judge-family-a", variant="reverse"),
        "judge-a-reverse",
    )
    judge_b_submission, _ = _submit(
        tmp_path,
        primary,
        primary_answer,
        _run_meta("judge-b", "judge-family-b"),
        "judge-b-primary",
    )
    records = tmp_path / "records.json"
    _write_json(
        records,
        {
            "schema_version": 1,
            "runs": [
                {"bundle_dir": str(primary), "submission_dir": str(primary_submission)},
                {"bundle_dir": str(reverse), "submission_dir": str(reverse_submission)},
                {"bundle_dir": str(primary), "submission_dir": str(judge_b_submission)},
            ],
        },
    )
    aggregate = aggregate_submissions(records, tmp_path / "aggregate.json")
    assert all(row["status"] == "order-unstable" for row in aggregate["pairs"])
    assert aggregate["partial_order_edges"] == []


def _case_build_rejects_path_escape_alias_leak_and_survives_short_writes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    spec_path = _make_spec(source)
    spec = read_json(spec_path)
    spec["candidates"][0]["response_path"] = str(source / "response-0.txt")
    escaped_spec = source / "escaped.json"
    _write_json(escaped_spec, spec)
    with _raises(ProtocolError, "relative path"):
        build_bundle(escaped_spec, tmp_path / "escaped-output")

    spec = read_json(spec_path)
    (source / "response-0.txt").write_text(
        "This leaks private-model-alias-0.", encoding="utf-8"
    )
    with _raises(ProtocolError, "identity aliases"):
        build_bundle(spec_path, tmp_path / "alias-output")

    clean_source = tmp_path / "clean-source"
    clean_source.mkdir()
    clean_spec = _make_spec(clean_source)
    original_write = protocol.os.write

    def short_write(descriptor, data):
        return original_write(descriptor, data[:7])

    with mock.patch.object(protocol.os, "write", side_effect=short_write):
        manifest = build_bundle(clean_spec, tmp_path / "short-write-output")
    assert manifest["status"] == "ready"
    assert (
        read_json(tmp_path / "short-write-output" / "bundle-manifest.json") == manifest
    )


def _case_adapter_attestation_and_output_envelope_fail_closed(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    bundle = tmp_path / "bundle"
    build_bundle(spec, bundle)
    packet = read_json(bundle / "packet.json")
    answer = _valid_answer(packet)
    answer_path = tmp_path / "answer.json"
    _write_json(answer_path, answer)
    bad_config = _run_meta("judge-1", "wrong-family")
    bad_meta = _materialize_meta(
        tmp_path, bundle, answer_path.read_bytes(), bad_config, "bad-route"
    )
    with _raises(ProtocolError, "pinned roster"):
        validate_submission(bundle, answer_path, bad_meta, tmp_path / "bad-submission")
    failure = read_json(tmp_path / "bad-submission" / "admission.json")
    assert failure["eligible"] is False
    assert failure["gates"]["structure"]["status"] == "fail"

    version_meta = _materialize_meta(
        tmp_path,
        bundle,
        answer_path.read_bytes(),
        _run_meta("judge-1", "judge-family-1"),
        "bad-version",
    )
    version_data = read_json(version_meta)
    version_data["adapter"]["version"] = "9.9.9"
    _write_json(version_meta, version_data)
    with _raises(ProtocolError, "adapter version"):
        validate_submission(
            bundle,
            answer_path,
            version_meta,
            tmp_path / "bad-version-submission",
        )

    oversized = _valid_answer(packet)
    oversized["pairs"][0]["reason"] = "过长" * 10_000
    _, result = _submit(
        tmp_path,
        bundle,
        oversized,
        _run_meta("judge-2", "judge-family-2"),
        "oversized",
    )
    assert result["gates"]["structure"]["status"] == "fail"
    assert (
        "raw-answer-byte-envelope-exceeded" in result["gates"]["structure"]["reasons"]
    )
    assert result["gates"]["cost"]["status"] == "pass"


def _case_aggregate_revalidates_admission_and_reverse_link(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    primary = tmp_path / "primary"
    reverse = tmp_path / "reverse"
    build_bundle(spec, primary)
    build_bundle(spec, reverse, reverse_of=primary, reverse_trigger="conflict")
    primary_answer = _valid_answer(read_json(primary / "packet.json"))
    reverse_answer = _valid_answer(read_json(reverse / "packet.json"), outcome="b")
    primary_submission, _ = _submit(
        tmp_path,
        primary,
        primary_answer,
        _run_meta("judge-a", "judge-family-a"),
        "linked-primary",
    )
    reverse_submission, _ = _submit(
        tmp_path,
        reverse,
        reverse_answer,
        _run_meta("judge-a", "judge-family-a", variant="reverse"),
        "linked-reverse",
    )
    other_primary_submission, _ = _submit(
        tmp_path,
        primary,
        primary_answer,
        _run_meta("judge-b", "judge-family-b"),
        "other-primary",
    )
    reverse_only_records = tmp_path / "reverse-only-records.json"
    _write_json(
        reverse_only_records,
        {
            "schema_version": 1,
            "runs": [
                {
                    "bundle_dir": str(primary),
                    "submission_dir": str(other_primary_submission),
                },
                {"bundle_dir": str(reverse), "submission_dir": str(reverse_submission)},
            ],
        },
    )
    with _raises(ProtocolError, "no eligible linked primary"):
        aggregate_submissions(reverse_only_records, tmp_path / "reverse-only.json")
    records = tmp_path / "records.json"
    _write_json(
        records,
        {
            "schema_version": 1,
            "runs": [
                {"bundle_dir": str(primary), "submission_dir": str(primary_submission)},
                {"bundle_dir": str(reverse), "submission_dir": str(reverse_submission)},
            ],
        },
    )
    reverse_manifest_path = reverse / "bundle-manifest.json"
    reverse_manifest = read_json(reverse_manifest_path)
    reverse_manifest["primary_packet_sha256"] = "f" * 64
    _write_json(reverse_manifest_path, reverse_manifest)
    with _raises(ProtocolError, "not linked"):
        aggregate_submissions(records, tmp_path / "unlinked.json")

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    fresh_spec = _make_spec(fresh)
    fresh_bundle = fresh / "bundle"
    build_bundle(fresh_spec, fresh_bundle)
    fresh_answer = _valid_answer(read_json(fresh_bundle / "packet.json"))
    submission, _ = _submit(
        fresh,
        fresh_bundle,
        fresh_answer,
        _run_meta("judge-a", "judge-family-a"),
        "forged",
    )
    admission_path = submission / "admission.json"
    admission = read_json(admission_path)
    admission["judge"]["family_id"] = "forged-family"
    _write_json(admission_path, admission)
    forged_records = fresh / "records.json"
    _write_json(
        forged_records,
        {
            "schema_version": 1,
            "runs": [
                {"bundle_dir": str(fresh_bundle), "submission_dir": str(submission)}
            ],
        },
    )
    with _raises(ProtocolError, "admission judge mismatch"):
        aggregate_submissions(forged_records, fresh / "forged.json")


def _case_two_decisive_families_are_not_vetoed_by_one_abstention(
    tmp_path: Path,
) -> None:
    spec = _make_spec(tmp_path)
    bundle = tmp_path / "bundle"
    build_bundle(spec, bundle)
    packet = read_json(bundle / "packet.json")
    runs = []
    for judge_id, family_id, outcome in (
        ("judge-a", "judge-family-a", "a"),
        ("judge-b", "judge-family-b", "a"),
        ("judge-1", "judge-family-1", "insufficient"),
    ):
        submission, _ = _submit(
            tmp_path,
            bundle,
            _valid_answer(packet, outcome=outcome),
            _run_meta(judge_id, family_id),
            f"abstention-{judge_id}",
        )
        runs.append({"bundle_dir": str(bundle), "submission_dir": str(submission)})
    records = tmp_path / "records.json"
    _write_json(records, {"schema_version": 1, "runs": runs})
    aggregate = aggregate_submissions(records, tmp_path / "aggregate.json")
    assert all(row["status"] == "decided" for row in aggregate["pairs"])

    conflicting_submission, _ = _submit(
        tmp_path,
        bundle,
        _valid_answer(packet, outcome="b"),
        _run_meta("judge-b", "judge-family-b"),
        "conflicting-judge-b",
    )
    conflict_records = tmp_path / "conflict-records.json"
    _write_json(
        conflict_records,
        {
            "schema_version": 1,
            "runs": [
                runs[0],
                {
                    "bundle_dir": str(bundle),
                    "submission_dir": str(conflicting_submission),
                },
            ],
        },
    )
    disputed = aggregate_submissions(conflict_records, tmp_path / "disputed.json")
    assert all(row["status"] == "disputed" for row in disputed["pairs"])


def _case_cli_build_validate_and_aggregate(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    bundle = tmp_path / "cli-bundle"
    build = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "blind_judge_protocol.py"),
            "build",
            "--spec",
            str(spec),
            "--output",
            str(bundle),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    answer = _valid_answer(read_json(bundle / "packet.json"))
    runs = []
    for judge_id, family_id in (
        ("judge-a", "judge-family-a"),
        ("judge-b", "judge-family-b"),
    ):
        answer_path = tmp_path / f"cli-{judge_id}-answer.json"
        _write_json(answer_path, answer)
        meta_path = _materialize_meta(
            tmp_path,
            bundle,
            answer_path.read_bytes(),
            _run_meta(judge_id, family_id),
            f"cli-{judge_id}",
        )
        submission = tmp_path / f"cli-{judge_id}-submission"
        validate = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "blind_judge_protocol.py"),
                "validate",
                "--bundle",
                str(bundle),
                "--answer",
                str(answer_path),
                "--run-meta",
                str(meta_path),
                "--output",
                str(submission),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert validate.returncode == 0, validate.stderr
        runs.append({"bundle_dir": str(bundle), "submission_dir": str(submission)})
    records = tmp_path / "cli-records.json"
    _write_json(records, {"schema_version": 1, "runs": runs})
    aggregate_path = tmp_path / "cli-aggregate.json"
    aggregate = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "blind_judge_protocol.py"),
            "aggregate",
            "--records",
            str(records),
            "--output",
            str(aggregate_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert aggregate.returncode == 0, aggregate.stderr
    result = read_json(aggregate_path)
    assert result["exact_ranking_authorized"] is False
    assert all(row["status"] == "decided" for row in result["pairs"])


class BlindJudgeProtocolTests(unittest.TestCase):
    def _run_case(self, function) -> None:
        with tempfile.TemporaryDirectory() as directory:
            function(Path(directory))

    def test_build_freezes_blind_primary_and_exact_reverse(self) -> None:
        self._run_case(_case_build_freezes_blind_primary_and_exact_reverse)

    def test_build_fails_closed_without_checker_and_when_18k_not_proven(self) -> None:
        self._run_case(_case_build_fails_closed_without_checker_and_when_18k_not_proven)

    def test_first_answer_is_strict_and_gates_do_not_compensate(self) -> None:
        self._run_case(_case_first_answer_is_strict_and_gates_do_not_compensate)

    def test_validator_rejects_fabrication_third_candidate_and_rank(self) -> None:
        self._run_case(_case_validator_rejects_fabrication_third_candidate_and_rank)

    def test_duplicate_json_key_is_not_a_first_answer(self) -> None:
        self._run_case(_case_duplicate_json_key_is_not_a_first_answer)

    def test_aggregate_collapses_family_votes_and_never_forces_rank(self) -> None:
        self._run_case(_case_aggregate_collapses_family_votes_and_never_forces_rank)

    def test_exact_reverse_direction_flip_is_order_unstable(self) -> None:
        self._run_case(_case_exact_reverse_direction_flip_is_order_unstable)

    def test_build_rejects_path_escape_alias_leak_and_survives_short_writes(
        self,
    ) -> None:
        self._run_case(
            _case_build_rejects_path_escape_alias_leak_and_survives_short_writes
        )

    def test_adapter_attestation_and_output_envelope_fail_closed(self) -> None:
        self._run_case(_case_adapter_attestation_and_output_envelope_fail_closed)

    def test_aggregate_revalidates_admission_and_reverse_link(self) -> None:
        self._run_case(_case_aggregate_revalidates_admission_and_reverse_link)

    def test_two_decisive_families_are_not_vetoed_by_one_abstention(self) -> None:
        self._run_case(_case_two_decisive_families_are_not_vetoed_by_one_abstention)

    def test_cli_build_validate_and_aggregate(self) -> None:
        self._run_case(_case_cli_build_validate_and_aggregate)


if __name__ == "__main__":
    unittest.main()
