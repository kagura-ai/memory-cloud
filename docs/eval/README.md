# Evaluation Documentation

Index for offline-evaluation methodology in Kagura Memory Cloud. This area
covers how we measure whether the system is **doing the right thing**, as
distinct from **doing things at all** (production observability) or
**doing things well at the system level** (benchmarks).

## Scope of this directory

| File | Purpose | Status |
|---|---|---|
| `edge_discovery_labeling.md` | Labeling protocol for offline evaluation of the Sleep edge_discovery LLM Judge (issue #375 / #274 successor) | Skeleton — Phase 1 in progress |
| `retrieval-benchmark.md` | Reproducible multi-arm retrieval benchmark (BM25-only / vector-only / hybrid / hybrid+neural) on the frozen golden corpus — relative deltas, reproduction steps (issue #967) | Baseline committed 2026-06-10 |

## Where this fits in the documentation map

Three documents discuss "how the LLM-driven parts of the system are working." They are complementary, not redundant.

- **[`sleep-maintenance.md`](../sleep-maintenance.md)** — describes *what* the Sleep maintenance phases (including edge_discovery) do and how they run in production. Operational reference.
- **[`neural-memory-evaluation.md`](../neural-memory-evaluation.md)** — system-level benchmarks (P@1, Hit@k, explore P@5) measured end-to-end on labeled query sets. Answers "is the system precise enough overall?"
- **`docs/eval/`** (this directory) — component-level **ground-truth labeling protocols** for individual LLM-Judge surfaces. Answers "is the LLM Judge inside the system making correct decisions on labeled pairs?"

The system-level benchmarks in `neural-memory-evaluation.md` rely on the labeling protocols defined here. Without a reliable labeling protocol, downstream eval scores are uninterpretable.

## Why "labeling protocol" is its own deliverable

Issue #249 (the original eval pilot) and #274 (its v2 with adjusted prompts) both aborted because **labeling itself was inconsistent across raters** — not because the LLM Judge was failing. The two readings of the same pair (LLM-as-judge default = "evidence-strict; only what is explicit"; author default = "evidence-light; human-intuitive") differed by 1–2 notches systematically.

This is a *prompt-design problem at the labeling layer*, not a model-capability problem at the judge layer. Until the labeling protocol passes an Inter-Annotator Agreement (IAA) gate, no eval results downstream can be trusted to mean what they appear to mean.

That's the reason for separating Phase 1 (this protocol) from Phase 2+ (running the eval).

## Relation to production observability (`#306`)

PR #371 (which closed #306) added production metrics to `edge_discovery.py:execute()`: judge decision distributions, model usage, prompt revision stamping. That layer is **distribution-faithful but ground-truth-free** — it tells you what the LLM is doing, not what it should be doing.

Offline evaluation (this area) is the complementary layer: **ground-truth-bearing but distribution-controlled**. The two are stitched together at the `EDGE_DISCOVERY_PROMPT_REVISION` stamp (`backend/src/services/sleep/prompts.py`): each prompt revision deployed in production gets a corresponding offline eval run, so we can answer "did v2 of the prompt actually improve quality, holding model and data constant?"

## References

- Issue [#375](https://github.com/kagura-ai/memory-cloud/issues/375) — labeling protocol redesign (this work)
- Issue [#306](https://github.com/kagura-ai/memory-cloud/issues/306) — production observability (closed by [#371](https://github.com/kagura-ai/memory-cloud/pull/371))
- Issue [#274](https://github.com/kagura-ai/memory-cloud/issues/274) — eval pilot v2 (closed; informed this redesign)
- Issue [#249](https://github.com/kagura-ai/memory-cloud/issues/249) — original eval pilot (closed; informed this redesign)
