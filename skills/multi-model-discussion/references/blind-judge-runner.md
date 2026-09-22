# Offline blind-judge runner

`scripts/blind_judge_protocol.py` is the deterministic boundary around a formal
seven-candidate comparison. It never invokes a provider, model, harness,
browser, or search tool. Provider adapters may consume its frozen `prompt.txt`
and must return the first terminal answer unchanged.

## Commands

```bash
python3 scripts/blind_judge_protocol.py build \
  --spec judge-spec.json \
  --output run/primary

python3 scripts/blind_judge_protocol.py build \
  --spec judge-spec.json \
  --reverse-of run/primary \
  --reverse-trigger conflict \
  --output run/reverse

python3 scripts/blind_judge_protocol.py validate \
  --bundle run/primary \
  --answer judge-first-answer.json \
  --run-meta judge-run.json \
  --output run/submissions/judge-a-primary

python3 scripts/blind_judge_protocol.py aggregate \
  --records aggregate-records.json \
  --output run/aggregate.json
```

`build` exits `2` when its preflight is not ready. `validate` exits `3` when
any independent admission gate is not `pass`. Both retain failure evidence;
neither repairs, retries, truncates, or splits content.

## Build specification

The input is strict JSON. Extra keys fail closed.

```json
{
  "schema_version": 1,
  "packet_id": "stable-task-id",
  "task": {
    "prompt": "The shared task",
    "context": "Frozen shared context",
    "hard_constraints": ["A non-compensable constraint"]
  },
  "rubric": [
    {"id": "correctness", "name": "Correctness", "description": "What passes"}
  ],
  "candidates": [
    {
      "candidate_id": "private-system-id",
      "family_id": "private-family-id",
      "identity_aliases": ["provider model alias", "route alias"],
      "response_path": "candidate-1.txt",
      "checker_path": "candidate-1-checker.json"
    }
  ],
  "judge_roster": [
    {
      "judge_id": "judge-a",
      "family_id": "judge-family-a",
      "route_id": "provider-model-route",
      "adapter_id": "audited-adapter",
      "adapter_version": "1.4.0"
    },
    {
      "judge_id": "judge-b",
      "family_id": "judge-family-b",
      "route_id": "other-provider-route",
      "adapter_id": "audited-adapter",
      "adapter_version": "1.4.0"
    }
  ],
  "order_recheck_policy": {
    "allowed_triggers": ["close", "conflict"],
    "max_rechecks_per_judge": 1
  },
  "budget": {
    "max_total_tokens": 18000,
    "adapter_input_reserve_tokens": 512,
    "reserved_output_tokens": 8192,
    "max_cash_usd": 1.5
  }
}
```

There must be exactly seven candidates. Each checker is also strict JSON:

```json
{
  "schema_version": 1,
  "status": "pass",
  "facts": [
    {"fact_id": "tests", "status": "pass", "text": "59 tests passed"}
  ]
}
```

Allowed checker statuses are `pass`, `fail`, `partial`, and `not_run`; a fact
may additionally be `unknown`. Checker files must exist even when their status
is `not_run`. Candidate and checker files must be regular, non-symlink files
beneath the spec directory and remain within their size limits. Keep identity,
provider, route, price, token, and latency metadata out of task, response, and
checker content. List every candidate/model/provider/route spelling in
`identity_aliases`; a detected alias fails the build instead of claiming
blindness. The private identity map commits aliases and the pinned judge route
roster and is mode `0600`.

The judge roster must leave at least two distinct eligible external families
for every candidate pair after same-family exclusions. Otherwise the bundle is
failed-closed before a call. A reverse bundle is allowed only for the frozen
`close` or `conflict` policy trigger, must be the exact primary reversal, and
must retain the same pinned judge route and adapter version. Aggregation counts
no reverse result unless that same judge also has one admitted linked primary.

The preflight records Unicode characters and UTF-8 bytes separately. Without a
pinned tokenizer, it uses prompt UTF-8 bytes as a deliberately conservative
input-token ceiling, then adds the frozen output-token reserve. This is a safe
admission policy for ordinary byte-backed tokenizers, not a measured token
count or proof about every possible tokenizer. It also reserves adapter/chat
framing before the call. The provider adapter must cap generated output at the
same reserved token count; the validator independently requires raw UTF-8 bytes
to fit within that reserve. A packet over the cap is `failed-closed`; do not
make it fit by removing candidates, truncating evidence, or splitting one
listwise judgment into incomparable calls.

## First-answer admission metadata

The adapter writes one raw terminal answer and this strict metadata object:

```json
{
  "schema_version": 1,
  "adapter": {"adapter_id": "audited-adapter", "version": "1.4.0"},
  "packet_sha256": "<frozen packet hash>",
  "prompt_sha256": "<exact prompt hash>",
  "raw_answer_sha256": "<first terminal answer hash>",
  "judge": {
    "judge_id": "judge-a",
    "family_id": "judge-family-a",
    "route_id": "provider-model-route"
  },
  "attempt_number": 1,
  "order_variant": "primary",
  "quality": {
    "status": "pass",
    "evidence_path": "quality-evidence.json",
    "evidence_sha256": "<64 lowercase hex>"
  },
  "usage": {
    "safe_input_tokens": 7200,
    "safe_output_tokens": 4300,
    "provider_total_tokens": 11800,
    "conservative_cash_usd": 0.42,
    "provider_cost_status": "known",
    "evidence_path": "usage-evidence.json",
    "evidence_sha256": "<64 lowercase hex>",
    "pricing_snapshot_path": "pricing-snapshot.json",
    "pricing_snapshot_sha256": "<64 lowercase hex>"
  }
}
```

Quality evidence is produced by a separately preregistered calibration or
canary; the runner copies and commits it but does not pretend to grade its
semantics. Usage and the frozen pricing calculation are likewise copied and
committed. Judge family, route, and adapter must exactly match the private
roster, including the exact adapter version. Packet, prompt, first answer,
quality, usage, and pricing hashes are one
attested chain. These local commitments make tampering detectable; they are not
a cryptographic provider signature, so only an independently audited adapter
may author the manifest. Provider cost status is one of `known`,
`verified-free`, `ambiguous-zero`, or `unknown`. Zero cost passes only when
independently `verified-free`.

The raw answer must be exactly one JSON object, with no BOM, fence, preface,
duplicate key, schema repair, or extra field. It contains:

- one shared evidence pool whose response quotes are exact substrings and whose
  checker citations exactly match a frozen status or fact;
- exactly seven candidate assessments covering every rubric dimension;
- exactly the 21 canonical pairs `A/B` through `F/G` once each;
- bilateral evidence for every outcome other than `insufficient`;
- no rank or forced total order.

The structure, quality, and cost gates are independent and non-compensating.
Only an attempt-1 answer with all three gates at `pass` is admitted.

## Aggregation records and interpretation

```json
{
  "schema_version": 1,
  "runs": [
    {"bundle_dir": "run/primary", "submission_dir": "run/submissions/judge-a-primary"}
  ]
}
```

Aggregation unblinds only committed valid outputs. It excludes a judge's family
for every pair involving a same-family candidate, collapses duplicate seats of
one family to one family vote, and requires two eligible families per pair. An
exact primary/reverse direction flip is `order-unstable`. Conflict is
`disputed`; missing coverage is `pair-family-limited`; inadequate evidence is
`insufficient-evidence`. Output is always a `Provisional` partial order for one
task and always has `exact_ranking_authorized: false`. Before counting a vote,
aggregation rehashes all stored evidence, revalidates the raw answer against
the frozen packet, recomputes the cost gate, checks the pinned route tuple, and
proves that a reverse bundle links the included primary bundle.
