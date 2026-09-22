# Output Protocol

Use this reference when composing the user-facing answer and durable report.
The council exists to improve the decision, not to make the reader consume its
meeting transcript.

## Core rule: two layers

1. **Chat layer:** conclusion-first, concise, and actionable.
2. **Artifact layer:** complete enough to audit, revisit, and supersede.

Do not expose a matrix of process modes and presentation depths by default.
Infer the task class from the request. The user can explicitly ask for a quick
answer, full transcript, or deeper debate, but good defaults should require no
configuration.

## Task classes

| Class | Use when | Main output |
|---|---|---|
| `decision` | Choosing among consequential options | Recommendation, trade-offs, consequences, validation gates |
| `review` | Reviewing code, plans, specs, or artifacts | Evidence-backed findings ordered by severity |
| `verify` | Checking factual or technical claims | Claim/evidence/status table with authoritative sources |
| `explore` | Mapping an ambiguous space without a required choice | Option map, tensions, unknowns, useful experiments |

`debate` is not a default task class. It is an optional follow-up when an
unresolved, decision-critical conflict survives independent analysis.

## Risk profiles and seat accounting

The risk profile controls council size. A seat is one independent decision
position, and the current host is always one of the seats. Do not report the
external-call count as the council size.

| Profile | Total seats, including host | Use when | Output expectation |
|---|---:|---|---|
| `quick` | 2 | Low-impact, reversible choices | One round, short context, concise answer; artifact only when useful |
| `general` | 3 | Ordinary decisions without a critical trigger | Independent positions, host synthesis, normal provenance |
| `critical` | 5 | Irreversible, expensive, externally consequential, safety-, security-, privacy-, or high-uncertainty decisions | Full decision artifact, explicit health, dissent, evidence, and validation gates |
| `legacy` | 3 | Compatibility with the previous fixed council behavior | Preserve the legacy contract; do not present it as the recommended risk-scaled default |

External participants must not duplicate the current host's model family. Seat
count is therefore `1 host + N usable external participants`, not merely the
number of processes that were launched.

For `critical`, record both `requested` and `usable` seats. The profile is
`healthy` only when the host's schema-valid independent position was hashed
before collection, all five seats are usable, at least four effective provider
families are represented, and at least three independent transport domains are
represented. A real critical invocation with a missing or invalid host position
stops before any external call with `pending_host_position` or
`invalid_host_position`. An underfilled external collection is `degraded`.
Name the missing position or diversity, lower confidence as appropriate, and do
not describe any non-healthy critical run as complete. Inspect
`profile_health.external_collection` separately from the final status.

Medical routing and release gates are defined only in
[`high-stakes-medical.md`](high-stakes-medical.md). Treat that file as the
normative medical protocol rather than duplicating a shortened medical policy
here.

## Chat contract

Default to 5-12 lines. Use the user's language. Lead with the answer, not the
process.

```markdown
**结论**
{One decisive recommendation or, for exploration, the most useful framing.}

**依据**
- {One to three decision-relevant reasons}

**关键分歧**
{The strongest unresolved dissent or uncertainty. Omit only when immaterial.}

**下一步**
{One concrete action, experiment, or validation gate.}

完整记录：[report.md](/absolute/path/report.md)
{Profile-health warning when unavailable seats or lost diversity affect confidence.}
```

Rules:

- Do not lead with participant names, round counts, or council mechanics.
- Do not paste full participant responses into chat.
- Mention a failed participant prominently when its absence materially reduces
  model diversity or evidence coverage.
- Always disclose a degraded `critical` profile in chat, even if the remaining
  positions agree.
- Do not claim "high confidence" because two or three models agreed.
- When the user asked only for comparison, do not force a decision.
- For code review, use findings-first review formatting instead of this generic
  card.

## Confidence

The host owns the final confidence judgment. Use `High`, `Medium`, or `Low` and
state its basis. Evaluate four factors independently:

| Factor | Question |
|---|---|
| Evidence quality | Are important claims verified by code, tests, or authoritative sources? |
| Constraint coverage | Did the analysis cover the user's decision criteria and real constraints? |
| Material disagreement | Does unresolved dissent change the recommended action? |
| Reversibility | Can the decision be tested or reversed cheaply if wrong? |

Model agreement is only a note under material disagreement. It is not a vote
count and not a substitute for evidence.

## Decision artifact

Keep the main body near one or two pages. Use informative headings that state
the point rather than generic headings such as "Analysis."

```markdown
# Decision: {Decision stated as an action}

| Field | Value |
|---|---|
| Status | Proposed |
| Recommendation | {One sentence} |
| Confidence | {High/Medium/Low} |
| Confidence basis | {Evidence / coverage / disagreement / reversibility} |
| Risk profile | {quick/general/critical/legacy} |
| Council availability | {N/M usable seats, including host; failures} |
| Profile health | {healthy/degraded; provider-family and transport-domain coverage} |
| Date | {YYYY-MM-DD} |

## Why this option best fits the constraints
...

## The decision accepts these trade-offs
| Consequence | Impact | Mitigation or trigger to revisit |
|---|---|---|

## The strongest dissent remains unresolved
...

## Validate before committing further
1. ...

## Sources and provenance
- [E1] ...
- Candidate artifacts: ...

## Appendix: Candidate assessment
Summarize each anonymized candidate's strongest contribution and main weakness.
Link to raw artifacts instead of copying them.
```

`Status` is `Proposed` unless the user or an authorized team accepts it. A
model council recommendation must not silently become an accepted project
decision.

## Review artifact

Follow the host's normal code-review convention: findings first, ordered by
severity, with file/line or artifact evidence. Deduplicate overlapping model
findings. A finding raised by one model can still be critical; verify it rather
than lowering it because it lacks votes.

```markdown
# Multi-Model Review: {Artifact}

## Findings
### Critical
...

### High
...

## Disputed or rejected findings
{Only items useful for understanding a real disagreement}

## Coverage and remaining risk
...

## Provenance
{Participants, failures, source artifacts}
```

## Verification artifact

Do not manufacture opposing positions for factual questions.

```markdown
# Verification: {Question}

## Answer
...

## Claim status
| Claim | Status | Best evidence | Remaining uncertainty |
|---|---|---|---|

## Conflicting evidence
...

## Sources and availability
...
```

Use `Verified`, `Supported`, `Disputed`, or `Unverified`. Prefer primary and
authoritative sources.

## Exploration artifact

Do not force a verdict when the user wants the option space.

```markdown
# Exploration: {Topic}

## The option space has these viable paths
...

## The real tensions are
...

## All candidates may be missing
...

## Evidence that would collapse the uncertainty
...

## Useful next experiment
...
```

## Provenance and appendices

- Keep bounded CLI envelopes and structured positions in the round directory.
  For OpenCode, keep only the canonical sanitized event/session summary, never
  the original JSONL stream or arbitrary event text payloads.
- Link to them from the report with absolute or repository-relative paths.
- Record each participant's requested and observed adapter, provider/model
  route, reasoning level, provider family, transport domain, and result status.
- Record the core-prompt digest, selected profile, requested/usable seat counts,
  host-position digest/validation, diversity counts, manifest schema/runner
  versions, and the reasons for any non-healthy status.
- Attribute externally verified facts to sources, not to model names.
- Preserve a valuable minority view even when it is not adopted.
- Omit council chronology unless a position changed because of new evidence.
- Never use a separate "convergence" section as proof of correctness.

## Runtime safety and OpenCode provenance

Treat orchestration controls as part of the audit record, not as invisible
implementation detail.

- Give every external participant only the frozen task packet through standard
  input. Use an isolated working directory, a private ephemeral HOME/XDG data
  root, a minimal route-specific environment, and an isolated configuration;
  do not expose the workspace or the user's global session database by default.
- An OpenCode participant must run with `--pure` and a deny-all primary agent.
  Preflight must show that effective tools are disabled before any task content
  is sent. Reject an OpenCode CLI version whose event/session contract has not
  been validated by the live adapter tests.
- Load local provider keys only from an explicitly supplied owner-only
  `--env-file`. Parse plain allowlisted `KEY=VALUE` assignments without a shell,
  normalize documented aliases, and project only the current route's key.
  Native Claude Code, Codex, and Gemini authentication must ignore this file.
- Pin the provider/model route and reasoning variant. Reject the result when
  requested and observed route or reasoning differ, when a tool event appears,
  when any JSONL event type or shape is outside the strict allowlist, or when a
  session identifier cannot be audited.
- After extracting the sanitized result envelope, delete the OpenCode session
  and verify that it no longer appears. Cleanup failure is a participant
  failure, not a warning that can be ignored.
- Remove the entire per-participant temporary root in `finally`, including
  process-launch, timeout, malformed-event, missing-session, and cleanup-error
  paths. Redact the literal and JSON-escaped prompt from diagnostics.
- Do not place prompts, secrets, arbitrary command arguments, or unrestricted
  inherited environment values in subprocess arguments or durable logs.
- Treat a provider-reported zero cost as `unknown` unless a verified price
  source proves that the route is free. Local catalog estimates are not bills.
- These controls reduce accidental access and cross-run leakage. They are not a
  proof of containment against a compromised CLI, provider, or operating
  system.
