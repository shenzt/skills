# Research and Blind-Judging Protocol

Use this reference when a council depends on current external evidence, when a
decision seat asks for more research, or when the task is a formal comparison
of several model or agent outputs. It supplements the council runner; it does
not add a `--research-profile` option to `run_council.py`.

## Two independent controls

Research intensity controls evidence collection. The council profile controls
the number of independent decision seats. Select them separately.

| Research intensity | Evidence workflow |
|---|---|
| `closed` | Use stable facts or an already complete evidence package; do not browse |
| `focused` | Follow one authoritative-source route for a current or narrow fact |
| `parallel` | Use two or three genuinely different evidence routes plus verification |
| `clinical` | Use the medical source hierarchy and human gate in `high-stakes-medical.md` |

| Decision profile | Total decision seats, including host |
|---|---:|
| `quick` | 2 |
| `general` | 3 |
| `critical` | 5 |

Do not multiply the two axes. Three research routes followed by three decision
seats are not nine independent seats. A five-seat critical decision also does
not require five researchers. A current but easily reversible product choice
may correctly use `parallel + quick`; a consequential choice over a complete,
frozen record may use `closed + critical`.

Use `focused` unless distinct evidence routes can be named in advance and the
likely decision value justifies their cost. Useful parallel routes include:

- official documentation, standards, release notes, or primary data;
- implementation evidence, source code, reproducible tests, or papers;
- adverse evidence, failure reports, counterexamples, and operational limits;
- local or Chinese-language deployment evidence when it changes feasibility.

Do not send several researchers the same open-ended search prompt and call the
duplicated results independent.

## Freeze one shared Evidence Pack

Research agents collect evidence; they do not recommend the final choice. The
research host de-duplicates and verifies their material, then freezes one
Evidence Pack before any comparable decision positions are collected.

At minimum, record:

```text
question_and_scope
atomic_claim
source_url, title, publisher, source_type
published_at and accessed_at
support_location_or_short_excerpt
relationship: supports | contradicts | context_only
scope_and_limitations
freshness_or_version_status
verification_status
conflict_group
decision_material_unknowns
excluded_key_sources_and_reasons
evidence_pack_version
evidence_pack_sha256
```

The support location must let another reviewer open the source and check the
claim. Inference belongs in a separate field and must not masquerade as source
evidence. Preserve counterevidence, unresolved conflicts, and important
exclusions so the shared packet does not silently anchor every seat to the
research host's preferred framing.

Give the host position and every external decision seat the exact same packet
bytes. Keep browsing and other research tools closed during the decision pass.
If the packet changes, its version and SHA-256 must change too; positions from
different packet versions are not comparable.

## One controlled research delta

A decision seat that finds a potentially outcome-changing fact gap must not
search privately. It may submit:

```text
question
why_material
expected_decision_change
acceptable_source_scope
current_evidence_refs
```

`expected_decision_change` must say what kind of result would change the
current recommendation. This prevents an unbounded search for post-hoc support.
The research layer verifies the request and may accept at most one delta round
per decision by default. If accepted, it freezes Evidence Pack vN+1 and reruns
every host and external position used in that comparison. If the gap remains,
report the uncertainty, run an experiment, or retain a human decision gate.

## Formal comparison of seven model or agent outputs

Ranking seven outputs is a benchmark workflow, not an ordinary host synthesis.
The unit under evaluation is the full deployment system: model, harness,
reasoning variant, tools, and checker environment. Do not attribute a result to
the bare model when those parts differ.

### Frozen quality packet

Give each quality judge, in one listwise call:

- the same task and decision-relevant context;
- the same rubric and hard constraints;
- all seven complete final outputs;
- the same sanitized harness or `submit.py` checker results;
- stable anonymous labels and a committed order;
- an output schema that permits ties and insufficient evidence.

Keep model identities, provider names, prices, token counts, and latency out of
the quality packet. Evaluate efficiency in a separate cost gate. The private
identity map must remain unavailable to each judge until its valid output is
committed.

Use two external judge families. The host may validate and aggregate but does
not count as a blind judge when it knows the identity map or authored one of
the candidates. Each judge must return:

- exactly seven candidate assessments;
- all 21 unordered candidate-pair outcomes, exactly once;
- one of `a_wins`, `tie`, `b_wins`, or `insufficient` for every pair;
- candidate-specific evidence and a task-relevant reason for each outcome.

For any outcome other than `insufficient`, the judge must cite evidence from
both candidates. Evidence must resolve to an exact excerpt or checker fact in
the frozen packet. Do not reward length, terminology density, confident tone,
or agreement with other candidates. Do not derive a forced `1..7` order from
dimension totals.

### Independent admission gates

| Gate | Minimum evidence to pass |
|---|---|
| Structure | First answer matches schema; seven candidates and 21 unique pairs are complete; every citation resolves |
| Quality | Pre-registered semantic canaries pass; decisive errors and hard constraints are found without a verbosity shortcut |
| Cost | Token and cash ceilings are frozen before the call; safe usage is retained and remains within both ceilings |

These gates are non-compensating. Excellent reasoning cannot excuse invalid
structure or an exceeded budget. A schema repair is not a first-attempt
structure pass. Missing or provider-reported zero cost is `unknown` or
`ambiguous-zero`, never proof that the call was free. A judge that fails any
gate does not enter the conclusion and is not automatically retried. If the
contract itself was ambiguous, repair the contract and run a separately named
canary rather than silently replacing the failed assessment.

### Family eligibility, order checks, and publication

Blind labels reduce explicit identity bias but do not erase family or style
bias. A judge from the same model family as a candidate cannot by itself
authorize a pair involving that candidate. Aggregate pair by pair after family
exclusions. Fewer than two eligible judge families is `pair-family-limited`,
not a win. Add a targeted third family only when that missing pair would change
a real choice and the extra call is worth its bounded cost.

When the two judges conflict on a material boundary, or when a result is close,
perform at most one pre-registered exact-reverse order recheck. A direction
flip is `order-unstable` and does not authorize a winner. Non-transitive or
sparse results should be published as a partial order or tier.

A small task set may support a `Provisional` tier but not a stable exact rank.
Report research and decision tracks separately. For each judge, disclose its
family, quality/structure/cost gate status, actual safe token usage, cost
status, and eligible pair coverage. Never hide a failed judge inside an
aggregate score.

## Pi remains a route-specific shadow

Pi is a harness, not a model capability or a built-in web-research service.
The admission unit is the exact tuple:

```text
Pi version + provider endpoint + model route + reasoning variant
```

A passing Kimi route does not admit GLM, Qwen, or Pi globally. For each route,
pin and verify the CLI, endpoint, model, reasoning payload, no-tools mode,
no-session or verified cleanup, terminal `stop`, event allowlist, first-answer
schema, safe usage/cost evidence, and held-out quality non-inferiority. Keep the
existing production adapter for any route that has not independently passed.
Do not delete OpenCode merely because one Pi canary succeeded.

Pi research requires a separately audited search extension, skill, SDK, or
external search service. A no-tool decision canary says nothing about that
research stack. Compare Pi and OpenCode using the same model, endpoint,
reasoning variant, prompt, evidence, and search backend before attributing a
difference to the harness.

## Current implementation boundary

`run_council.py` collects decision positions; it still does not construct an
Evidence Pack. `scripts/blind_judge_protocol.py` now builds, preflights,
validates, admits, and aggregates the formal seven-candidate protocol without
calling any model or provider. Read `references/blind-judge-runner.md` for its
strict file contracts. Provider execution, semantic quality calibration, and
research remain separate adapters or evidence sources; never imply the offline
runner proved them.

When changing this protocol, compare the old and new skill on the same held-out
research-routing, ranking, and adapter-migration prompts. Grade task utility,
unsupported claims, structure, family eligibility, order stability, latency,
tokens, and cost separately. Generate a static review artifact before deciding
that a change is an improvement, and do not treat one stochastic win as stable.
