# Quality-first 3-agent and 5-agent defaults

Defined: 2026-09-07; five-seat roster updated: 2026-09-13. Scope: the existing technical/product/research-reasoning
council. The user requested saving the recommended combinations after the
twelve-task evaluation; this does not authorize a new paid evaluation.

## Choose the combination

| Profile | Preferred total roster | Use |
|---|---|---|
| `general` / 3-agent | GPT-6 Astra `max`, Fable 5.1 `max`, GLM 5.3 `max` | Quality-first ordinary decisions |
| `critical` / 5-agent | The same three, plus DeepSeek V4.1 Flash `max`, Kimi K3 `max` | Consequential decisions worth wider independent review |

The host counts as one seat. With Astra as the Codex host, launch two external
participants for `general` and four for `critical`. Do not launch three or five
external calls in addition to the host.

Assign perspectives, not presumed model superpowers:

| Seat | Main responsibility | Expected contribution |
|---|---|---|
| Astra / actual OpenAI host | Integrate the plan and decision boundaries | Executable steps, acceptance criteria, unresolved trade-offs |
| Fable 5.1 | Examine evidence and long-term consequences | Unsupported claims, second-order risks, conditions that change the decision |
| GLM 5.3 | Audit constraints, implementation, and safety boundaries | Authorization/tenant isolation, consistency, rollback and failure-injection checks |
| DeepSeek V4.1 Flash | Work through solution paths, calculations, and alternatives | Independently checked arithmetic, feasible competing plans, verification steps |
| Kimi K3 | Challenge framing and counterfactual assumptions | Strongest counterexample, hidden prerequisites, falsification experiment |

Every seat still answers the complete shared question independently with the
same evidence; roles do not create a sequential handoff or excuse missing
constraints. Flash is not the sole security reviewer: its paired test still
missed cache-isolation and safety-boundary details. Cross-check those with GLM
and the host. The host freezes its position before external answers, then
synthesizes; preserve the existing independent external review for critical
decisions. Judge evidence, not votes.

## Adapt to the actual host

The runner follows each profile's `priority` and skips families already seated.
It does not change the current task's model or reasoning setting.

| Actual host family | `general`: external participants | `critical`: external participants |
|---|---|---|
| Codex / OpenAI | Fable 5.1, GLM | Fable 5.1, GLM, Flash, Kimi |
| Claude Code / Anthropic | Astra, GLM | Astra, GLM, Flash, Kimi |
| Gemini CLI / Google | Astra, Fable 5.1 | Astra, Fable 5.1, GLM, Flash |

The preferred named roster is exact only when the actual host is the named
model at the intended effort. For example, Sol as host yields
Sol + Fable 5.1 + GLM, not Astra + Fable 5.1 + GLM. Record the actual host or state that its model
is unverified. Gemini-host adaptations are valid host-inclusive rosters, not
the identical preferred combination. If the exact preferred composition matters,
use an Astra or Fable 5.1 host; do not silently switch models or add a sixth seat.
For a Gemini host, the host also takes the assumption/counterexample role left
by Kimi. Synthesis always belongs to the actual host, not automatically Astra.

## Configuration and compatibility

`council-profiles.json` is the executable source of truth. `general` and
`critical` have separate priority lists so the three-seat defaults stay intact:

```text
general:  codex-astra -> claude -> glm -> kimi -> qwen -> gemini-flash -> deepseek -> gemini
critical: codex-astra -> claude -> glm -> deepseek-flash -> kimi -> qwen -> gemini-flash -> deepseek -> gemini
```

`codex-astra` requests `gpt-6-astra` with `max` through the existing Codex
adapter. `claude` requests `claude-fable-5-1` with `max`. GLM and Kimi use their
existing OpenCode `max` routes; Qwen retains `xhigh`. Do not confuse the GLM
candidate's `max` with the separate historical Pi judge's `high` setting, and
do not migrate a route to Pi merely because it was used for judging.

`deepseek-flash` requests `deepseek-flash` with thinking enabled and `max`
through `scripts/deepseek_http.py` at the official API, matching the transport
and generation settings used in the 2026-09-13 evaluation. The participant
wrapper/schema differs from that benchmark; do not transfer its exact score or
latency to every council. The helper allows one HTTP attempt, no redirects or
tools, and no local session store. The runner validates the final schema and
returned model, records tokens and unavailable cost, and keeps the existing
host-position/health gates. API route attestation is not proof that a mutable
provider alias or server-applied effort will remain unchanged forever.

Qwen stays available as an explicitly chosen replacement, not a sixth seat.
The historical `deepseek` participant is a different OpenCode/Pro route, not a
fallback proof of the old model: the provider announced that old Pro would
route to Flash from 2026-09-14 04:00 UTC. Never label a future alias call as the
historical V4 Pro benchmark or silently substitute it for `deepseek-flash`.

The old `codex` participant still requests Sol `max`, preserving `quick` and
`legacy`. It is also an explicit OpenAI replacement outside an OpenAI host.
`claude-fable-5-fallback` remains an explicit Anthropic replacement outside an
Anthropic host. A replacement occupies the same family seat: never call
`codex` with `codex-astra`, or Fable 5 with Fable 5.1, as independent agents.
Explicit participant overrides are `custom` runs; they do not inherit the
`critical` profile's enforced host-position and health checks. Keep those checks
when a critical task needs a changed roster; do not use an override to bypass them.

The priority list selects the initial roster; it is not an automatic failover
loop. Report unavailable seats and follow existing authorization, retry, cost,
and degraded-profile rules before any replacement call. An unavailable Astra
route must not silently become Sol while retaining an Astra label.

## Evidence and limits

The available twelve-task results support a provisional quality-first family
selection: Astra was the leading observed OpenAI entry; Fable 5.1 had a higher
point score than Fable 5 in the latest paired assessment; GLM led the remaining
families' observed scores, followed by Kimi and Qwen in that earlier set. Sol and Fable 5 remain
useful same-family alternatives, not extra independent seats.

On 2026-09-13 the user requested adding Flash to the five-seat discussion.
Its twelve-task paired result was 90.10 versus historical Pro answers at 78.72
(9 higher, 3 tied), with observed mean latency about 54 versus 124 seconds.
This supports a user-selected Flash trial in place of Qwen, not a proven
Flash-over-Qwen head-to-head or a synergy-optimal council. The three-seat roster
is unchanged. Moving Qwen out also removes the default Kimi/Qwen shared Alibaba
transport; distinct transport labels still do not prove total infrastructure
independence.

Do not describe these combinations as globally best or synergy-tested. The
combined display uses different judging rounds; GLM and Kimi still have one
eligible external judge each. The Fable supplement includes quote-only judge
recovery, not a retroactive first-pass success, and its primary resampling
interval does not establish a stable Fable 5.1 advantage. The two research tasks
test reasoning over supplied evidence, not autonomous search. No medical
effectiveness is established; retain the separate medical safety workflow.

The underlying evaluation inputs, transcripts, identity maps, and local result
paths are intentionally excluded from this public distribution. The numerical
summaries above describe limited prior observations, not an independently
reproduced public benchmark or an execution prerequisite. Use independently
authored synthetic cases and local validation before changing these defaults.

The model names and effort settings above are requested configuration. Retain
the existing per-adapter identity checks; a request pin or offline test is not
proof of the upstream serving model or of live route availability.
