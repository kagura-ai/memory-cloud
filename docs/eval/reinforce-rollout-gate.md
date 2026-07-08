# Reinforce Rollout Gate (Issue #1069)

The bounded **reinforce** recall re-rank (#1048) — adoption (`reference_count`,
#1046) + retrieval feedback (#888) gently nudge a memory's recall standing —
ships **default-on for newly created contexts** since #1207
(`ContextSearchConfig.reinforce_enabled`; the pre-registered kagura-memory-eval
program attributed the update-correctness headline, +0.36 over vanilla RAG
BCa 95% [0.24, 0.50], entirely to this re-rank's cold-start recency prior).
Contexts created **before** #1207 keep their stored setting — the flip rewrites
no rows — and legacy contexts without a config row adopt the new default
lazily (recall materializes the config row via `create_or_get`), so only a
stored explicit `false` opts out. This document defines the **eval gate** that
decides whether enabling it on a pre-existing, explicitly-opted-out context is
safe, and the **staged rollout + monitoring** procedure that gate feeds.

The principle (from the milestone): *trust before integration*. Reinforce only
goes live where an eval shows it helps the canonical answers **without** burying
the long tail or starving brand-new memories.

## The gate (what "safe to enable" means)

A repeatable ON-vs-OFF A/B on one ingested corpus, flipping only
`reinforce_enabled`. Two query populations + one popularity-bias metric:

| Signal | Population / metric | Gate condition (default) |
|---|---|---|
| **Helps the canonical** | `current_fact` — gold is an adopted+confirmed answer competing with a near-duplicate phrasing | ON − OFF on MRR@10 ≥ `min_current_fact_uplift` (0.0 = no regression) |
| **Doesn't bury the tail** | `rare` — gold is a zero-adoption niche memory | OFF − ON on MRR@10 ≤ `max_rare_regression` (0.02) |
| **Doesn't starve new memories** | zero-adoption surfacing rate (share of top-k slots held by never-adopted docs) | ON ≥ `min_zero_adoption_retention` × OFF (0.90) |

Thresholds live in `GateThresholds` (`backend/tests/eval/reinforce_gate.py`) and
are configurable. The defaults are conservative (the gate passes on
*non-regression* of the current-fact population); an experiment wanting a
stronger "improves" claim raises `min_current_fact_uplift` above 0. The verdict
always reports the strict `improved` flag separately, so the distinction is never
hidden.

> **PASS ≠ "reinforce helps" (#1084).** With the default `min_current_fact_uplift = 0.0`,
> a *no-op* reinforce — or a silently-broken `reinforce_enabled` toggle — produces
> `passed: true, improved: false` (uplift 0 is non-regression). **Graduate a context
> on `gate.current_fact.improved == true`, not on bare `passed`** (or set
> `min_current_fact_uplift > 0` so `passed` *implies* improved). The runner also
> emits `off_on_arms_identical`; a `true` there means reinforce changed nothing
> (neutered toggle / no seeded signal / no headroom) — never graduate on it.

### Why these three

The compounding "+lift" claim's standard kill-shot is *"the boost just measures
that popular things were marked popular."* The gate pre-empts it: the `rare`
no-regression band and the zero-adoption retention floor are the controls that
separate "reinforce encodes useful adoption" from "reinforce introduces
popularity bias." A run that lifts `current_fact` **but** fails either control is
a FAIL, not a win.

## How to run

```bash
make up                 # live stack: postgres + qdrant + embeddings
make eval-reinforce     # KAGURA_EVAL_LIVE=1 python -m tests.eval.reinforce_runner
```

Writes `backend/tests/eval/results/reinforce-<date>.json` from a **real run
only** (never fabricated) and **exits non-zero if the gate FAILS**. The harness:

1. ingests `fixtures/reinforce_corpus.yaml` (15 memory docs, 10 queries × 2 populations);
2. seeds adoption (`reference()` ×5) + net-helpful feedback (×3) on the canonical
   `meta.adopted_docs` — the rare docs stay untouched (zero-adoption);
3. scores the **OFF** arm (reinforce pinned off explicitly — since #1207 a
   lazily-materialized config row defaults to enabled, so the runner can no
   longer rely on "no config row yet");
4. flips `reinforce_enabled = true`, `reinforce_max_boost = 0.15`;
5. scores the **ON** arm and evaluates the gate.

The decision math + metrics are pure and unit-tested (`test_reinforce_gate.py`);
the seed→OFF→ON→gate orchestration is pinned DB-free with fakes
(`test_reinforce_runner.py`). Only the live numbers need the stack.

## Staged rollout (gate-gated, no blanket flip)

1. **Pick a high-traffic, trusted context** with real adoption + feedback signal
   (a re-rank with no signal is a no-op — leave cold/low-signal contexts off).
2. **Run the gate** above against a corpus representative of that context. Enable
   only when `gate.current_fact.improved` is **true** (and `off_on_arms_identical`
   is false) — a bare `passed` can be a vacuous non-regression, not a win.
3. **Enable** via REST `PUT /api/v1/contexts/{id}/search-config`
   (`reinforce_enabled: true`) or the `update_search_config` MCP tool.
4. **Monitor** the telemetry below; **disable** on regression.
5. **Graduate** context-by-context. There is **no** blanket rewrite of
   pre-existing contexts. (#1207 later flipped the default for *newly created*
   contexts on the strength of the pre-registered update-correctness eval;
   the graduation procedure above remains the path for contexts that predate
   it or that were explicitly opted out.)

## Monitoring (telemetry, #1069)

When the re-rank fires, recall emits a `reinforce_rerank_applied` structured log
(structlog/JSON — the codebase's observability backbone; there is no Prometheus).
Per recall, per context:

| Field | Watch for |
|---|---|
| `reordered`, `top1_changed` | the re-rank is actually active after you flip it on |
| `factor_min` / `factor_max` / `factor_mean` | how hard it is pushing (bounded by `max_boost`) |
| `boosted` / `demoted` | how many candidates moved which way |
| `zero_adoption_in_topk` | **the regression alarm** — if this trends toward 0, reinforce is starving brand-new memories out of the user-visible slice; disable and re-tune |

`zero_adoption_in_topk` is the live counterpart of the gate's
`zero_adoption_surfacing_rate`: the gate proves the cold-start floor holds on the
eval corpus, the log proves it still holds on live traffic.

## Out of scope

- Blanket rewrite of pre-existing contexts' stored setting. (The *default for
  new contexts* flipped to on in #1207 — evidence-driven, not blanket: existing
  rows and explicit opt-outs are untouched.)
- Tuning the bounded design or the #120 boundary (separate follow-up if the
  bounded nudge proves insufficient once felt in production).
- The forge-resistance of the signal for untrusted autonomous agents — that is
  #1065 (a host-arbitrated, forge-resistant reinforce signal), the milestone's
  second half.
