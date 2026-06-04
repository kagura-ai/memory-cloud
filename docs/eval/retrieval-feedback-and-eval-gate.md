# Retrieval Feedback Signal & Eval Gate Policy (Issue #888)

Part of the AI Agent Memory Substrate epic (#885). This document covers the
retrieval **feedback signal** and the **policy** governing how it may be used.

## Feedback signal

`access_count` / `last_used` are weak, implicit proxies for "was this recall
useful". The feedback signal makes the judgment **explicit and attributable**:

- **MCP tool**: `feedback(context_id, memory_id, helpful, query?, note?)`
- **REST**: `POST /api/v1/contexts/{context_id}/feedback`
  (`{memory_id, helpful, query?, note?}`)
- **SDK**: tracked separately in `kagura-ai/kagura-memory-python-sdk#171`.

### Persistence — never pollutes recall

Feedback is an **append-only event log** in a dedicated table
(`retrieval_feedback`), never embedded and structurally excluded from `recall()`
— the same isolation principle as the agent session-state lane (#889). It is a
time series (repeated or contradicting signals are kept), so there is no unique
constraint; net-helpful aggregation is a read-time concern. Both `context_id` and
`memory_id` foreign keys cascade on delete, so feedback is erased with its
context or memory (GDPR/APPI erasure follows automatically).

### Access

Recording feedback is a **read-adjacent** action — anyone who can read a context
(`VIEWER`) consumes recall and may rate it — so the API gates on
`PermissionService.check_context_access(VIEWER)`, not write. The caller's
`user_id` is stored for attribution (abuse tracing + future signal weighting).

## Eval gate policy — HARD RULE

> **No self-update / auto-promotion loop ships before the golden retrieval eval
> gate (#344) is green.**

The feedback signal is the *prerequisite* for a future Eval→Skill self-update
loop, but closing that loop is **explicitly out of scope** until the eval harness
guards it. Without ground-truth eval, an auto-promotion loop driven by raw
feedback degrades into implicit noisy RL (the substrate optimizes for whatever
gets thumbs-up, including wrong-but-confident results).

Concretely:

1. **Recall-ranking changes** must keep the deterministic eval gate green. The
   `backend/tests/eval/` harness (#344) runs its leakage check, corpus-schema,
   stratification, and metric tests in normal CI — these guard corpus integrity
   and harness correctness on every PR.
2. **The live P@5 / MRR@10 measurement** (`make eval-retrieval`) is the numeric
   regression gate. It is **not** in CI yet (Qdrant + embedding + Sudachi
   cold-start is not CI-realistic — tracked in #336); until then it is a
   maintainer-run local gate whose baseline is committed under
   `backend/tests/eval/results/`.
3. **Any** mechanism that promotes, demotes, re-ranks, or rewrites memories based
   on feedback is gated behind (2) being a green, automated gate. Feedback is
   **collected** now; it is **not acted on automatically**.

Wiring the live eval as an automated CI gate is the #336 follow-up; closing the
Eval→Skill loop is a separate, later epic gated on all of the above.
