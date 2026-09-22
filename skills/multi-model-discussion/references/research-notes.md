# Research Notes

These sources and local evaluations support the v3 risk-scaled design. Re-check
them when CLI versions, model routes, reasoning variants, profile membership,
or the discussion protocol changes.

## CLI automation

- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference)
  documents print mode, JSON Schema output, tool restriction, disabled skills,
  and non-persistent sessions.
- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
  recommends `codex exec`, least-privilege sandboxing, ephemeral runs,
  structured output, JSONL events, and `--output-last-message`. Current JSONL
  `turn.completed` events expose token usage but not a USD price.
- [Gemini CLI headless mode](https://geminicli.com/docs/cli/headless/) documents
  the JSON envelope with response, stats, and error fields.
- [Gemini CLI policy engine](https://geminicli.com/docs/reference/policy-engine/)
  recommends deny rules for excluding tools; a global deny also removes tools
  from model context.
- [Gemini Agent Skills](https://geminicli.com/docs/cli/creating-skills/)
  documents `.agents/skills` as a portable workspace alias.

### OpenCode adapter boundary

The OpenCode participants introduced by v3 are held to a stricter local
runtime contract than an ordinary interactive OpenCode session:

- run with `--pure`, an isolated configuration and working directory, a private
  ephemeral HOME/XDG data root, a route-specific minimal environment, and a
  deny-all primary agent;
- preflight that tools are effectively disabled and that the registered
  reasoning variant equals the pinned variant;
- treat provider/model route, reasoning variant, tool-event absence, and a
  traceable session identifier as result-validity conditions;
- reduce the result to a canonical sanitized summary rather than persisting raw
  JSONL text fragments, delete the session, verify deletion, redact prompt
  material from diagnostics, and remove the temporary root in every exit path
  before accepting the participant result;

These are defense-in-depth controls against accidental workspace access,
configuration inheritance, and cross-run persistence. They do not establish a
hostile-code containment boundary or prove the identity or training lineage of
the remote model.

### Adapter release validation

Mocked unit tests are necessary but cannot prove compatibility with an installed
CLI. After changing Claude command construction or OpenCode event/session logic,
run the opt-in `tests/live_cli_smoke.py` against a harmless synthetic prompt.
The script requires `--confirm-live-calls` before making provider calls and
checks structured output, observed routes and variants, zero tool/protocol
events, and `deleted-verified` cleanup. It can also revalidate an existing live
manifest with `--manifest` without making another call. Never use personal or
workspace context for this plumbing test. Pass local route keys with an
explicit owner-only `--env-file`; the script never sources shell code.

### Pi adapter decision

[Pi Coding Agent](https://github.com/earendil-works/pi/tree/main/packages/coding-agent)
is a promising shadow adapter because `--no-session` and `--no-tools` remove
most of OpenCode's cleanup lifecycle. Its JSON mode still exposes the prompt,
so raw events would require the same content-free persistence policy. Pi's CLI
does not natively enforce the final participant JSON Schema, and its thinking
levels may be clamped by a model-specific map. Its GLM, Kimi, and Qwen built-in
providers also use different credentials/routes from the current direct
OpenCode routes. Keep OpenCode as the default until a pinned Pi version passes
route-equivalent, highest-effort, no-tool, no-session, schema, and cost-status
live gates; add Pi as a shadow adapter before changing a formal roster.

## Discussion protocol

- [Du et al., Improving Factuality and Reasoning through Multiagent Debate](https://proceedings.mlr.press/v235/du24e.html)
  supports independent candidates followed by peer critique, while showing
  value from multiple agents and rounds on selected reasoning/factuality tasks.
- [Liang et al., Encouraging Divergent Thinking through Multi-Agent Debate](https://aclanthology.org/2024.emnlp-main.992/)
  finds that moderate disagreement and adaptive stopping help, forced continued
  disagreement can hurt, and cross-model judges can be biased.
- [Judging the Judges](https://arxiv.org/abs/2406.07791) documents position bias
  and motivates order-swap consistency checks.
- [Judging with Many Minds](https://arxiv.org/abs/2505.19477) reports that
  multi-agent judging can amplify position, verbosity, chain-of-thought, and
  bandwagon biases, so debate should not be treated as automatically debiasing.

## Output and decision records

- [Karpathy's LLM Council](https://github.com/karpathy/llm-council) separates
  individual tabbed answers, anonymous peer review, and a distinct chairman
  synthesis instead of flattening everything into one transcript.
- [Martin Fowler's ADR guidance](https://martinfowler.com/bliki/ArchitectureDecisionRecord.html)
  recommends short, inverted-pyramid records containing the decision, context,
  serious alternatives, consequences, confidence, and reevaluation triggers,
  while keeping full advice separately.
- [Microsoft's ADR guidance](https://learn.microsoft.com/en-us/azure/well-architected/architect-role/architecture-decision-record)
  recommends pithy, assertive, factual records that stand alone and link to
  supplemental design material.
- [UK ONS content guidance](https://service-manual.ons.gov.uk/content/writing-for-users/structuring-content)
  recommends ordering online content by user need and importance, using
  informative headings, short paragraphs, and progressive disclosure.

## Community and X signals

Treat these as practitioner signals, not proof:

- An [X discussion about heterogeneous councils](https://x.com/alex_prompter/status/2043017641229451474)
  argues that different model families produce more useful independence than
  simulating a council inside one model.
- A [Karpathy post discussed on X](https://x.com/jaehwa/status/1997786320958324841)
  frames LLMs as simulators and recommends eliciting a useful group of
  perspectives rather than asking for one model persona's opinion.
- An [X post about multi-backend Markdown workflows](https://x.com/michabbb/status/1999989435547668696)
  emphasizes explicit file handoffs, non-interactive execution, and transparent
  logs across Claude, Codex, and Gemini.
- Community council implementations commonly provide quiet/standard/detailed
  output, quick versus deep processing, separate transcript artifacts, and one
  concrete next action. These patterns are useful interface hypotheses but
  still require local evaluation.

## Local iteration-3 technical pilot

The local iteration-3 benchmark is the empirical basis for trying a broader
technical roster, but its status is
`pilot_provisional_judge_family_limited`, not a permanent model ranking.

- Eight model+harness systems answered 10 prompt variants from six parent
  decision cases, with two runs per variant (160 formal answers).
- Two blind judge families completed the grading. A third judge did not return
  a usable packet, and the preregistered reliability gate failed. Small score
  differences therefore do not establish a stable total order.
- GPT-5.6-sol had the highest aggregate point estimate. Fable-5, Kimi K3, and
  GLM-5.3 formed a practically close next group under this pilot; the evidence
  does not authorize a unique long-term champion.
- Kimi, GLM, Qwen, and Fable contributed different accepted themes in this case
  set. That supports testing heterogeneous rosters, but it neither proves model
  ancestry nor guarantees that every novel observation is correct.
- Gemini 3.7 Flash was useful as a low-latency route in this pilot. Gemini 3.1
  Pro is retained as a compatibility/regression shadow rather than inferred to
  be universally inferior.

The detailed local evidence consists of the decision-roster report, aggregate
pilot, grading-reliability audit, and portfolio analysis. Those raw benchmark
artifacts and model responses are intentionally kept in the sibling evaluation
workspace rather than bundled into the portable skill. The claims retained
here are the conservative conclusions that survived those audits; validate
against independently authored synthetic cases before changing the roster or
promoting the pilot to a permanent ranking.

### What the pilot does and does not justify

The `quick`/`general`/`critical` profiles use 2/3/5 total seats, including the
host, as a risk-management policy. Iteration 3 supports the value of
heterogeneous coverage; it did not directly compare complete two-, three-, and
five-seat council outcomes. Profile sizes, diversity thresholds, latency, cost,
and decision lift therefore require separate regression and confirmation.

The technical roster should be treated as a provisional operating hypothesis:
exclude the host's provider family, record transport-domain correlation, and
mark a `critical` result degraded unless five seats, four effective provider
families, and three transport domains are usable. Agreement remains supporting
context, not evidence of correctness.

## Fable 5.1 production selection

On 2026-09-04, the owner selected Fable 5.1 as the production `claude` seat and
accepted its observed cost and remaining technical-task risk. The preceding
three-parent, four-prompt paired audit gave Fable 5.1 `21.5/24`; the two Fable 5
grader totals were `20.5/24` and `21.0/24`. Pairwise outcomes were mixed:
Fable 5.1 won the research and operations-base prompts, Fable 5 won the
technical-migration prompt, and the operations update was tied or weakly
favored Fable 5. Fable 5.1 cost about 49.6% more than the historical Fable 5
anchors in that small sample.

This is an accepted operating decision, not a claim of global model dominance
or statistical significance. The registry therefore pins `claude` to
`claude-fable-5-1`, retains `claude-fable-5-fallback` as an explicit rollback
route outside every default profile, and continues to require exact Fable 5.1
route attestation. The detailed responses and audit artifacts remain in the
sibling evaluation workspace rather than the portable skill.

## Medical evidence boundary

Iteration 3 did not evaluate clinical cases, clinical outcomes, emergency
triage, medication safety, clinician review, or medical-source fidelity. Its
technical ranking must not be reused as evidence that the same roster is safe
or superior for medical decisions.

The normative medical workflow is maintained in
[`high-stakes-medical.md`](high-stakes-medical.md). Medical capability remains
unvalidated until a separate, de-identified or synthetic case set passes its
hard safety gates and qualified human review. Model-judge scores alone cannot
close that validation gap.

## Practical conclusions

1. Generate independent positions before exposure to peer answers.
2. Prefer model diversity and evidence diversity over additional rounds.
3. Use moderate critique, anonymous attribution, and adaptive stopping.
4. Require explicit assumptions, confidence, risks, and falsification criteria.
5. Treat the host judge as informed but biased; use a second blind judge for
   consequential or close decisions.
6. Evaluate quality, calibration, bias, reliability, latency, and cost together.
7. Present the conclusion first and keep the transcript as linked provenance.
8. Infer the task class automatically; do not expose a combinatorial mode matrix.
9. Scale council size and disclosure to consequence: 2 total seats for
   reversible choices, 3 for ordinary decisions, and 5 for critical
   non-emergency decisions.
10. Count the host as a seat, exclude duplicate host-family calls, and report
    correlated provider and transport paths instead of equating endpoint count
    with independence.
11. Treat medical safety as a separate evidence and human-review problem, not
    as an extension of the technical pilot leaderboard.
