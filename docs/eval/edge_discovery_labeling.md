# Edge Discovery — Offline Eval Labeling Protocol

> **Status**: Skeleton (Phase 1 in progress on branch `375-feat/feat-eval-redesign-offline-eval-labeling`).
> Sections marked `[TODO]` are placeholders pending the IAA calibration round.

This document defines the labeling protocol for offline evaluation of the
Sleep Maintenance edge_discovery LLM Judge phase
(`backend/src/services/sleep/edge_discovery.py`). It is the deliverable for
Phase 1 of issue [#375](https://github.com/kagura-ai/memory-cloud/issues/375).

For why this is its own deliverable rather than part of an eval pipeline,
see [`README.md`](README.md) in this directory.

## 1. Scope and non-goals

**In scope (Phase 1)**:
- A written guideline that lets multiple raters reach reproducible labels on the same memory pair
- Edge-type definitions aligned with the production type system
- An Inter-Annotator Agreement (IAA) calibration round with explicit pass/fail tiers
- A documented decision: did calibration pass, with which rater pool, on which sample?

**Out of scope (deferred to Phase 2+)**:
- Running the offline eval itself on a held-out test set
- Comparing LLM Judge outputs against the labels produced under this protocol
- Per-prompt-revision A/B comparison framework
- Any production integration (Layer 2 alerts, Slack notifications, etc.)

The Phase 1 / Phase 2+ split is intentional: running an eval before the labels
are reliable produces results that *look* like measurements but cannot
distinguish "the judge is wrong" from "the labels are wrong." That is exactly
the failure mode that aborted #249 and #274.

## 2. The unit of labeling

A *pair* is two memories `(src, dst)` produced by the candidate-generation
step of `edge_discovery.execute()`: medium-similarity Qdrant neighbors
(cosine `[0.6, 0.9)`) that don't already share an edge of the relevant type.
The labeling task is to decide, given `(src, dst)`:

1. **Are they related?** (binary)
2. **If related, what edge type best describes the relationship?** (ternary, see §3)
3. **Confidence in the label.** (1–5 ordinal, retained for downstream uncertainty quantification — does not affect gate computation)

The unit aligns with what `edge_discovery.execute()` actually emits per pair, so
labels are directly comparable to judge outputs without any transformation
layer.

## 3. Edge type definitions

The edge-type vocabulary is the production `EdgeType` literal in
[`backend/src/services/sleep/edge_discovery.py`](../../backend/src/services/sleep/edge_discovery.py)
(line 123 at time of writing):

```python
EdgeType = Literal["related_to", "depends_on", "learned_from"]
```

The labeling guideline MUST use exactly these three types. Inventing
additional categories (e.g. `causes`, `contradicts`, `inferential_causal`,
`unrelated`) is forbidden — labels must be directly comparable to what the
production judge can emit. The fourth state is "no edge" (the answer to
question 1 above is "not related").

> **Note — historical artifacts**: the pilot data in
> `backend/tests/services/sleep/eval/pilot_2026_04/_local/pairs.jsonl` uses an
> earlier vocabulary (`unrelated`, `inferential_causal`, etc.). Re-labeling
> under this protocol must remap to the three production types above; pairs
> labeled with categories that no longer exist must be re-adjudicated, not
> mechanically remapped.

### 3.1 `related_to` — `[TODO]`

[TODO: precise definition, decision rule, 3 worked examples
including 1 borderline case (`evidence-light` vs `evidence-strict` reading).]

### 3.2 `depends_on` — `[TODO]`

[TODO: precise definition with directionality semantics. Phase 1 must
align with #373's directionality decision (see
`backend/src/services/sleep/edge_discovery.py:126` and PR #420). 3 worked
examples, including 1 case where direction matters and 1 where the pair is
symmetric and `related_to` is the correct fallback.]

### 3.3 `learned_from` — `[TODO]`

[TODO: precise definition. 3 worked examples. Particular care needed on
the boundary with `depends_on` — `learned_from` implies a knowledge-transfer
relationship, `depends_on` implies a structural prerequisite.]

## 4. The evidence-light / evidence-strict ambiguity

This is the primary problem the protocol must solve. The Kagura Memory
record `bf4238aa-e0f9-410d-8be1-d48c998052af` (cited in issue [#375](https://github.com/kagura-ai/memory-cloud/issues/375))
captured the root finding from pilot #249: the same labeling prompt is
reproducibly read in two ways, and the readings differ by 1–2 notches:

| Reading | Default for | Decision rule | Failure mode |
|---|---|---|---|
| **Evidence-light** | Human authors | "If a domain-aware reader could plausibly see the connection, label as related." | Over-labels `related_to` on weakly-coupled pairs; inflates positive class |
| **Evidence-strict** | LLMs (default prompt) | "Label as related only if the relationship is explicit in the text of one of the memories." | Under-labels; misses causal/conceptual links that humans take for granted |

A protocol that is silent on this distinction will produce labels whose
inter-rater disagreement is dominated by which reading each rater happened
to default to — not by the actual difficulty of the pair.

**The protocol's choice**: `[TODO — decide between]`
1. Evidence-strict only (and require all raters to switch to that reading)
2. Evidence-light with a stated set of allowed inferential leaps
3. Both, treated as separate labels, with the gate computed on each independently

The choice should be informed by the IAA calibration round (§7): pick the
reading where multi-prompt LLM raters and at least one human rater
*independently* converge.

## 5. Annotator pool composition

Raters in the calibration round must form a heterogeneous pool to avoid
mistaking shared bias for agreement. The current Phase 1 minimum:

- **At least one human rater** (typically the primary author or maintainer)
- **At least two LLM raters with deliberately diverse prompts** (e.g. one
  evidence-strict prompt, one evidence-light prompt, OR two different model
  families with the same prompt)
- **No fully homogeneous pools** — `LLM × N (same prompt)` and `LLM × N (same
  model family)` are explicitly disallowed, even with N large

If the operator wishes to use an all-LLM pool, the labeling document must
include a written justification explaining why the bias-correlation concern
from `bf4238aa` doesn't apply for this evaluation. The justification has to
identify the specific failure mode that all-LLM pooling would have on the
target metric.

## 6. Sample design

### 6.1 Floor

Calibration round 1 must use **at least 50 pairs**. This is the minimum
that gives a 95% confidence interval on Krippendorff's α tighter than ±0.15,
which is necessary for the tiered gate in §7 to actually distinguish tiers.

### 6.2 Stratification

Sampling must be stratified by `(cosine_similarity_band, candidate_edge_type)`.
The bands match the candidate-generation logic in
`edge_discovery.py:60-61`:

| Band | Cosine range | Rationale |
|---|---|---|
| Low | `[0.6, 0.7)` | Borderline candidates; protocol must be stable here |
| Mid | `[0.7, 0.8)` | The judge's primary working range |
| High | `[0.8, 0.9)` | Near-duplicate territory; many `depends_on` / `learned_from` cases |

Per stratum target: at least 12 pairs (so 50 total, slightly biased toward
mid). If the candidate pool from production data is too sparse in any band,
document the gap and continue with what's available — do not back-fill with
synthetic pairs.

### 6.3 Existing pilot artifacts

`backend/tests/services/sleep/eval/pilot_2026_04/_local/pairs.jsonl`
contains 50 pairs from the prior pilot, each annotated by `openai` and
`gemini`, with `labeling_prompt_sha256` recorded. Round 1 should re-label a
stratified slice of these pairs under the new protocol rather than collecting
fresh data — the disagreement pattern visible in the existing annotations
(e.g. `pair_id=p0001` shows `unrelated` vs `inferential_causal` for the same
pair) is exactly the calibration target. Fresh data collection is needed only
if a stratum is not adequately represented in the existing 50.

## 7. The IAA gate

Compute Krippendorff's α (or Cohen's κ for the 2-rater special case) on
the labels produced by the heterogeneous rater pool. Apply the tiered gate:

| α (or κ) | Tier | Action |
|---|---|---|
| `< 0.6` | Fail | Rewrite §3 / §4 of this document; re-calibrate from round 1 |
| `0.6 ≤ α < 0.8` | Tentative | Pass, but **double the calibration sample** (≥100 pairs) before declaring Phase 1 done. Document the tier explicitly in §10. |
| `α ≥ 0.8` | Substantive | Pass; Phase 1 complete; Phase 2 may begin. |

The reliability scale follows Krippendorff (2004): `0.667` is the
conventional minimum for *tentative* conclusions, `0.8` for *substantive*
conclusions. We round 0.667 down to 0.6 for the lowest tier boundary because
the binary edge-type confound (§4) means even 0.6 represents real signal
above the floor of "labels by coin-toss."

### 7.1 Iteration cap

Maximum **3 calibration rounds**. If round 3 still fails the `α ≥ 0.6`
threshold, do **not** continue rewriting the guideline — the failure is no
longer about wording. Open a follow-up issue scoped to "root-cause analysis
of why edge_discovery labels are not learnable as a consistent task" and
defer Phase 2 indefinitely.

## 8. Adjudication protocol

For pairs with disagreement after round 1:

- 3 raters per pair; majority vote on related/not + edge_type
- Tie-breaker: domain expert (issue author / repo maintainer) review
- Confidence rating per rater retained per pair; not used in voting but
  available for downstream uncertainty quantification

## 9. Round protocol

Each round:
1. Each rater labels the same N pairs **independently** — no shared scratchpad,
   no in-flight discussion
2. Per-rater label files committed to
   `backend/tests/services/sleep/eval/pilot_2026_04/_round_<N>/`
3. α/κ computed and recorded in `_round_<N>/iaa_summary.md`
4. Decision recorded in §10 below

## 10. Round outcomes

`[TODO — populated as rounds complete]`

### 10.1 Round 1 — `[TODO]`

- Date: —
- Sample size: —
- Rater pool: —
- α / κ: —
- Tier: —
- Decision: —

## 11. References

- Issue [#375](https://github.com/kagura-ai/memory-cloud/issues/375) — this work
- Issue [#306](https://github.com/kagura-ai/memory-cloud/issues/306) — production observability (complementary, not replacement)
- Issue [#274](https://github.com/kagura-ai/memory-cloud/issues/274), [#249](https://github.com/kagura-ai/memory-cloud/issues/249) — predecessor pilots (closed; informed this redesign)
- PR [#371](https://github.com/kagura-ai/memory-cloud/pull/371) — observability merge that triggered this redesign
- PR [#420](https://github.com/kagura-ai/memory-cloud/pull/420) — `EdgeType` directionality (#373/#374); §3.2 must align with the directionality semantics introduced there
- Krippendorff, K. (2004). *Reliability in Content Analysis: Some Common Misconceptions and Recommendations.* Human Communication Research, 30(3) — α scale conventions used in §7
