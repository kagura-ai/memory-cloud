# Design sign-off: agent-provenance feedback & reinforcement eval gate (RFC-0002 F6)

- **Status**: Signed off (gating design for any P1 reinforcement-default change)
- **Issue**: [#1263](https://github.com/kagura-ai/memory-cloud/issues/1263) — gating item F6 of RFC-0002
  (Agent Memory & Context Control Plane; RFC text maintained locally, lands in
  `docs/rfc/0002-agent-memory-context-control-plane.md` when published)
- **Consumers**: eval-harness maintainers (`backend/tests/eval/`), operators of contexts bound
  to autonomous agents or serving fleet bootstrap queries, and anyone proposing a change to a
  reinforcement or ranking default
- **Depends on**: golden retrieval eval harness (#344, `backend/tests/eval/README.md`);
  reinforce rollout gate (#1069, `docs/eval/reinforce-rollout-gate.md`); feedback signal &
  eval-gate policy (#888, `docs/eval/retrieval-feedback-and-eval-gate.md`); forge-resistant
  host arbitration (#1065)

This document extends the #344 golden-retrieval harness and the #1069 reinforce rollout gate
with an **agent-provenance dimension**: what evidence is required before agent-emitted feedback
signals may drive any ranking-default change, and before any P1 reinforcement-default flip.
It also fixes the **interim operating posture** for contexts serving fleet bootstrap queries,
where a bootstrap-reinforcement loop can ratchet a poisoned memory's rank with no human in the
loop. Its final home is `docs/eval/agent-provenance-feedback-gate.md`, alongside the gate docs
it extends.

## Background (restated, self-contained)

**The reinforce re-rank** (#1048) is a bounded, per-context, fail-safe recall re-rank:
`MemoryService._maybe_reinforce_rerank` (`backend/src/services/memory_service.py`) re-sorts the
already relevance-filtered candidate pool by `hybrid_score * _reinforce_factor`, where
`_reinforce_factor` combines, importance-weighted:

- **adoption** — `reference_count` (#1046), bumped only by an explicit `reference()` call
  (the deliberate Layer-3 detail fetch); recall surfacing bumps `access_count` only, which is
  **not** a factor input;
- **retrieval feedback** — net-helpful tally from the append-only `retrieval_feedback` event
  log (#888), tanh-squashed;
- a positive **cold-start recency prior** that decays with age.

The factor is clamped to `[1 - reinforce_max_boost, 1 + reinforce_max_boost]`, so semantic
relevance always dominates and the re-rank never pulls in new hits. Any internal failure falls
back to the original hybrid ranking (fail-safe). When it fires, it emits the
`reinforce_rerank_applied` structured log (#1069) whose load-bearing regression signal is
`zero_adoption_in_topk`.

**Feedback provenance** (#1065) is server-stamped and two-valued —
`FEEDBACK_PROVENANCE_AGENT` / `FEEDBACK_PROVENANCE_HOST`
(`backend/src/models/retrieval_feedback.py`):

- The public feedback path (MCP `feedback` tool, `backend/src/mcp_server/tools/feedback.py`;
  REST `POST /api/v1/contexts/{context_id}/feedback`) never exposes the provenance field —
  `FeedbackService.record_feedback` (`backend/src/services/feedback_service.py`) stamps
  `'agent'` for it, always. An agent cannot forge `'host'`.
- `'host'` is stamped only by `FeedbackService.record_host_feedback` — the seam for a trusted
  host/cockpit to record a signal backed by an **independent verdict** (a check/test exit code
  or an operator HITL approval), never the agent's self-report. Today this seam is
  **service-layer only**: no MCP tool or REST route calls it. Exposing a host-arbitration
  transport surface is its own follow-up design, out of scope here.
- Recording feedback is deliberately read-adjacent (VIEWER-level): anyone who can consume
  recall may rate it. Attribution is per-event (`retrieval_feedback.user_id`).

**Host arbitration in ranking**: when a context's
`ContextSearchConfig.reinforce_require_host_arbitration` is `true`, the re-rank aggregates
feedback with `host_only=True` (`FeedbackService.aggregate_for_memories`), so agent-emitted
feedback contributes **zero** to ranking; only host-arbitrated signal counts.

### Where the settings actually live (current reality)

For the avoidance of doubt: all three reinforcement knobs are **per-context columns** on
`ContextSearchConfig` (`backend/src/models/config.py`), not deployment-level settings in
`backend/src/config/settings.py`:

| Column | Default (new rows) | Meaning |
|---|---|---|
| `reinforce_enabled` | `true` (since #1207) | the re-rank runs at all |
| `reinforce_max_boost` | `0.15` | the factor bound |
| `reinforce_require_host_arbitration` | `false` | only `provenance='host'` feedback moves ranking |

They are settable per context via the `update_search_config` MCP tool
(`backend/src/mcp_server/tools/search_config.py`) or REST
`PUT /api/v1/contexts/{context_id}/search-config`
(`backend/src/api/routes/context_search_config.py`). Consequently, a "reinforcement-default
flip" under this gate means a **column-default change plus migration**, not an env-var change;
new migrations chain from the repository's current alembic head (`e62_1245_assign_mem_idx`) at
implementation time — not from any revision id sketched in RFC drafts.

## Restated RFC-0002 decisions this gate depends on

Restated here so the document is self-contained (the RFC is maintained locally and unpublished):

- **Decision D28 — P0 does not change reinforcement defaults** (`reinforce_enabled`,
  `reinforce_require_host_arbitration`). The RFC instead *recommends* host arbitration for
  contexts bound to autonomous agents; because of the bootstrap-reinforcement loop (below),
  contexts serving fleet bootstrap queries SHOULD enable `reinforce_require_host_arbitration`
  (or disable reinforcement) until F6 eval evidence exists; and **any default flip is gated on
  eval evidence — this document is that gate**. Flipping arbitration ON globally without
  evidence would silently disable a shipped, deliberately-defaulted feature (#1207) for
  human-in-the-loop workspaces; suppressing reinforcement for the bootstrap lane alone was
  rejected for P0 because bootstrap reads genuinely influence behavior and a lane-specific
  carve-out would fork recall semantics.
- **Bootstrap purity (Decision D11)** — the designed `get_agent_bootstrap` recall component
  (`docs/design/agent-bootstrap-contract.md`, F2 sign-off, #1259 — pending merge at time of
  writing) is a pure composition over the normal recall chokepoint: it deliberately keeps
  recall's counter and reinforcement side effects, because bootstrap reads influence behavior
  and should reinforce.
- **Threat model T5 residual (forged feedback)** — with arbitration OFF (the default),
  agent-provenance feedback *does* move ranking within the bounded boost; a persistent agent
  inside a workspace can nudge results until arbitration is enabled.
- **Threat model T1 residual (in-tier poisoning) and the bootstrap-reinforcement loop** —
  `trust_tier` is context-granular: a poisoned memory written *inside* a trusted context
  through a legitimate credential is invisible to the trusted-only filter. Fleets re-issue
  recurring bootstrap queries at every session start, so a poisoned memory in a trusted
  context matching a fleet's bootstrap query is recalled every session of every agent; the
  fleet's standard follow-up behavior (`reference()` on surfaced hits, `feedback(helpful=true)`)
  converts each surfacing into adoption + feedback signal, ratcheting the memory's rank toward
  the boost cap **with no human in the loop**.
- **Epistemology** — agent self-reports are forgeable, host verdicts are not; the codebase
  already refuses to trust self-report (server-stamped provenance, `host_only` aggregation).
  A ranking default may only come to *trust* agent self-report on measured evidence.

### A precision note the gate must account for (verified against the code)

`reinforce_require_host_arbitration` gates **only the feedback term** of `_reinforce_factor`.
The adoption term (`reference_count`) is not provenance-filtered — `aggregate_for_memories`'
`host_only` flag filters `retrieval_feedback` rows, while `reference_count` lives on the memory
row and counts every `reference()` call regardless of who made it. **Arbitration therefore
halves the bootstrap-reinforcement loop but does not eliminate it**: a fleet whose workflow
auto-references surfaced hits still ratchets adoption. This is why Decision D28's
recommendation is disjunctive — "enable arbitration **or disable reinforcement**" — and why
Gate B below measures the adoption-only residual explicitly. Full de-fanging of the loop is
`reinforce_enabled=false`.

Also restated from the model docstring (`backend/src/models/config.py`): the arbitration flag
is itself editable via `update_search_config`, so it is only meaningful when set out-of-band by
an operator/cockpit **and** the untrusted agent lacks EDITOR/OWNER on the context — otherwise
the agent could flip it off.

## Scope and non-goals

**In scope**: the gate criteria, measurement protocol, interim operating posture, and rollback
conditions governing (a) any ranking-default change driven by agent-provenance feedback and
(b) any P1 reinforcement-default flip (column defaults above, or bootstrap-lane reinforcement
semantics).

**Non-goals**:

- Exposing a public transport for `record_host_feedback` (own design; the seam exists at the
  service layer and the harness can call it in-process).
- The P1 fleet dashboard and per-agent feedback-anomaly views (they consume the planned
  `memory_access_events` audit table, which is **not yet in the tree**; this gate is designed
  to run without it, using harness-local attribution).
- Tuning the bounded-boost design or the recall/explore signal boundary (#120).
- Content-level screening of in-tier poisoning (server-side DLP; P1, separate). Arbitration
  protects **ranking**, not content — poisoning containment remains `trust_tier` plus
  write-access hygiene.
- Human-labeled gold benchmarks (the #344 harness is a regression/self-consistency harness;
  its absolute numbers are drift signals, not quality claims).

## Gate A — agent-provenance signal quality (normative)

Gates: **any ranking-default change driven by agent-provenance feedback.**

A repeatable three-arm experiment on one ingested corpus (extending
`backend/tests/eval/fixtures/reinforce_corpus.yaml` and `reinforce_runner.py`):

| Arm | `reinforce_enabled` | `reinforce_require_host_arbitration` | Feedback seeding |
|---|---|---|---|
| **OFF** | false | — | none counted |
| **AGENT** | true | false | `record_feedback` (provenance `'agent'`, today's runner behavior) |
| **HOST** | true | true | same events re-seeded via `record_host_feedback` (provenance `'host'`) |

Gate conditions:

| Signal | Population / metric | Gate condition (default) |
|---|---|---|
| **#1069 conditions hold per arm** | `current_fact`, `rare`, zero-adoption surfacing (as in `docs/eval/reinforce-rollout-gate.md`) | AGENT and HOST arms each pass the existing `GateThresholds` (`backend/tests/eval/reinforce_gate.py`) vs OFF |
| **Forgery null test** | new `forged` population: decoy docs seeded with agent-provenance `helpful=true` bursts and **no** host feedback | HOST arm: decoy MRR@10 delta vs OFF attributable to feedback = **0** (feedback factor contribution exactly zero — the arbitration filter is airtight) |
| **Forgery uplift (residual quantification)** | same `forged` population | AGENT arm: decoy uplift vs OFF is **measured and reported** (report-only) — this is the quantified T5 residual and MUST be published in any default-change proposal |
| **Agent/host agreement** | canonical population where both provenances exist | directional agreement of agent net-helpful with host verdicts ≥ `min_agent_host_agreement` (default 0.80) — agent self-report may only earn ranking trust if it predicts independently-verdicted usefulness |

The `PASS ≠ improved` discipline from the reinforce rollout gate (#1084) carries over verbatim:
the verdict reports a strict `improved` flag separately, and a default-change proposal must
argue from `improved`, never bare `passed`. Thresholds live in a frozen, configurable
`GateThresholds`-style dataclass; defaults above are conservative starting points.

## Gate B — bootstrap-reinforcement loop replay (normative)

Gates: **any change to the fleet-bootstrap reinforcement posture**, including relaxing the
interim posture below, and (jointly with Gate A) any arbitration-default flip.

A session-replay experiment modeling the loop:

1. Seed the corpus with canonical gold docs plus one in-tier **poisoned decoy** crafted to
   match a fixed "fleet bootstrap query" (dummy fixture content only — e.g. a decoy doc id
   `poison-01` in the corpus YAML; never production data).
2. Run N synthetic sessions (default N=25). Each session issues the same trusted-only recall
   (the bootstrap recall shape), then replays the fleet's follow-up behavior on the top hit:
   one `reference()` (adoption) + one `feedback(helpful=true)` (agent provenance).
3. Record the decoy's rank and reinforce factor after every session — the **ratchet curve** —
   for arbitration OFF and arbitration ON.

Gate conditions:

| Signal | Condition |
|---|---|
| **Arbitration zeroes the feedback ratchet** | with arbitration ON, the decoy's trajectory is identical to a replay where no feedback is emitted (feedback term contributes 0 at every step) |
| **Adoption-only residual is quantified** | with arbitration ON, the residual ratchet from `reference()` adoption alone is measured; if the decoy displaces any gold canonical answer out of top-k (default k=5) within N sessions, the interim posture for auto-referencing fleets is `reinforce_enabled=false`, not arbitration alone |
| **Bound holds under adversarial seeding** | the decoy's factor never exceeds `1 + reinforce_max_boost` at any step (invariant check on the clamp) |
| **Cold-start floor survives the loop** | `zero_adoption_in_topk` (per the `reinforce_rerank_applied` telemetry fields) does not trend to 0 across sessions in the ON arms |

## Gate C — the P1 reinforcement-default flip gate (normative)

**No change to any reinforcement default may merge** — the `ContextSearchConfig` column
defaults for `reinforce_enabled`, `reinforce_max_boost`, or
`reinforce_require_host_arbitration`, nor any change to bootstrap-lane reinforcement semantics
(e.g. suppressing the bootstrap recall leg's side effects, the alternative Decision D28
rejected for P0) — unless **all** of:

1. **Green Gate A and Gate B runs** from real executions (never fabricated), with result JSONs
   committed under `backend/tests/eval/results/` (`agent-provenance-<date>.json`), following
   the house convention that runners exit non-zero on FAIL.
2. **The #344 harness stays green**: the deterministic CI layer (leakage, corpus schema,
   stratification, metric tests) on the PR, and the nightly contract gates (`ci_gates.py` +
   `fixtures/ci_baseline.json`) showing no `BREACH`.
3. **The proposal PR links the evidence**: the committed result files, which arm justifies the
   flip, and the measured AGENT-arm forgery uplift (the residual being accepted or removed).
4. **Staged rollout, never a blanket rewrite**: defaults apply to newly created config rows
   only; existing contexts keep their stored values and graduate context-by-context per the
   procedure in `docs/eval/reinforce-rollout-gate.md` (the #1207 precedent).

This extends — and never relaxes — the standing HARD RULE from
`docs/eval/retrieval-feedback-and-eval-gate.md`: no self-update / auto-promotion loop ships
before the golden retrieval eval gate (#344) is green. Agent-provenance feedback is
**collected and attributed now; it earns default ranking trust only through this gate.**

## Interim operating posture (normative, until F6 evidence exists)

- Contexts serving fleet bootstrap queries **SHOULD** set
  `reinforce_require_host_arbitration=true`, or set `reinforce_enabled=false`, via
  `update_search_config` or `PUT /api/v1/contexts/{context_id}/search-config`.
- Because arbitration gates only the feedback term, fleets whose workflow auto-calls
  `reference()` on surfaced hits **SHOULD prefer `reinforce_enabled=false`** until Gate B has
  quantified the adoption-only residual.
- The arbitration flag MUST be set out-of-band by an operator/cockpit, on contexts where the
  agents hold VIEWER (not EDITOR/OWNER) — otherwise the flag is advisory at best.
- Human-in-the-loop workspaces are unaffected: this posture is a recommendation for
  agent-bound / fleet-bootstrap contexts, not a default change (that is exactly what Decision
  D28 defers to this gate).

## Measurement protocol

- **Harness**: extend `backend/tests/eval/reinforce_runner.py` (or a sibling
  `agent_provenance_runner.py`) with the HOST arm, the `forged` population, and the Gate B
  session-replay driver; decision math stays pure and unit-tested (the
  `reinforce_gate.py` / `test_reinforce_gate.py` split), orchestration pinned DB-free with
  fakes; only live numbers need the stack (`make up`, `KAGURA_EVAL_LIVE=1` — the
  `make eval-reinforce` pattern).
- **Host-arm seeding** calls `FeedbackService.record_host_feedback` in-process (the runner
  already drives `FeedbackService.record_feedback` directly), with a dummy verdict reference
  (e.g. `check:exit=0`).
- **Results**: `backend/tests/eval/results/agent-provenance-<date>.json`, real runs only.
- **Promotion to CI**: once stable, the headline conditions become `ci_gates.py` contracts
  bounded by `fixtures/ci_baseline.json` under its baseline-governance rule (baseline updated
  only via a justifying PR; three-value PASS/BREACH/INFRA verdicts; staged
  `advisory` → `blocking` via the `EVAL_GATES_MODE` variable).

## Monitoring and rollback

- **Live telemetry**: the `reinforce_rerank_applied` structured log (structlog/JSON) —
  `zero_adoption_in_topk` is the starvation alarm; `factor_max` pinned at the cap plus a
  rising `boosted` count on a fleet-bootstrap context is the ratchet signature.
- **Burst attribution**: every feedback event is attributable
  (`retrieval_feedback.user_id`, plus provenance); until the P1 fleet dashboard lands,
  feedback bursts are investigated by querying `retrieval_feedback` directly.
- **Rollback is a config write, not a deploy**: flip the per-context setting back via
  `update_search_config`; the re-rank is fail-safe by construction, so disabling it can never
  break recall.
- **If a shipped default flip regresses** (nightly `BREACH`, or live starvation/ratchet
  signatures): revert the column default in a fix PR. Because defaults never rewrite stored
  rows, the blast radius of a default revert is contexts created since the flip — enumerate
  and correct them explicitly in the revert PR.

## Sign-off checklist (maps to #1263)

- [x] **Agent-provenance feedback signals are evaluated before any ranking-default change** —
      Gate A: provenance-split OFF/AGENT/HOST arms over the extended reinforce corpus, a
      forgery null test proving arbitration zeroes forged agent signal, the AGENT-arm forgery
      uplift published as the quantified residual, and an agent/host agreement floor before
      agent self-report may drive any default.
- [x] **Bootstrap-reinforcement loop risk addressed** — the loop is restated (fleet bootstrap
      queries re-run every session start; a poisoned in-tier memory matching them ratchets its
      rank with no human in the loop); interim posture: contexts serving fleet bootstraps
      SHOULD enable `reinforce_require_host_arbitration` (or disable reinforcement) until eval
      evidence exists — sharpened by the verified finding that arbitration gates only the
      feedback term, so auto-referencing fleets should prefer `reinforce_enabled=false`; Gate B
      replays the loop and quantifies both the feedback ratchet and the adoption-only residual.
- [x] **Any P1 reinforcement-default flip is gated** — Gate C: green Gate A + Gate B runs with
      committed result JSONs, the #344 deterministic layer and nightly contract gates clean,
      evidence linked in the proposal PR, and staged context-by-context rollout with no blanket
      rewrite of stored config rows.
