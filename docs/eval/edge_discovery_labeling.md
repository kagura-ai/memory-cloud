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

> **Fill-order note**: §3.1–§3.3 are intentionally left as skeletons until
> after round 1. The §5 adversarial pool is designed to surface real
> disagreement structure; writing definitions now would speculate on which
> distinctions matter, when round 1's C rationale will tell us directly.
> Each subsection will be populated using actual borderline pairs from
> round 1, not invented examples.

### 3.1 `related_to` — `[TODO — populate from round 1 C rationale]`

Skeleton: precise definition, decision rule, 3 worked examples including 1
borderline case (`evidence-light` vs `evidence-strict` reading) — the
borderline case to be drawn from round 1 disagreement pairs.

### 3.2 `depends_on` — `[TODO — populate from round 1 C rationale]`

Skeleton: precise definition with directionality semantics. Phase 1 MUST
align with #373's directionality decision (see
`backend/src/services/sleep/edge_discovery.py:126` and PR #420). 3 worked
examples, including 1 case where direction matters and 1 where the pair is
symmetric and `related_to` is the correct fallback.

### 3.3 `learned_from` — `[TODO — populate from round 1 C rationale]`

Skeleton: precise definition. 3 worked examples. Particular care needed on
the boundary with `depends_on` — `learned_from` implies a knowledge-transfer
relationship, `depends_on` implies a structural prerequisite.

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

**The protocol's choice**: dual labeling, decided post-round-1 by the
adjudicator C (see §5 and §7.2). Both readings (A = strict, B = light) are
collected on every pair in round 1. C's tendency on the A–B disagreement
subset determines which reading the project converges on; the chosen
reading is then frozen as the §4 protocol for downstream rounds and Phase 2
eval.

The decision is documented in §10 once round 1 completes — until then this
section names the candidates only:

1. **Evidence-strict** — finalize if C aligns with A on ≥60% of disagreement pairs
2. **Evidence-light** — finalize if C aligns with B on ≥60% of disagreement pairs
3. **Re-iterate** — if C is balanced or incoherent (see §7.2), guideline is under-determined; rewrite §3 examples using C's per-pair rationale and re-run.

Note the deliberate absence of a "both, kept as parallel labels" outcome:
Phase 2 eval will compare the LLM Judge against ground truth, and ground
truth must be a single label. Carrying both readings into eval would just
defer the choice to a place where it would be even more expensive to make.

## 5. Annotator pool — adversarial LLM design (no human rater)

The naïve fix to the shared-bias problem from §4 is to require a human rater
in the pool. We have **explicitly chosen not to**, because in this project
the only feasible human rater is the author/maintainer and that single point
of failure (availability, attention-budget, motivated-reasoning) makes the
protocol non-runnable in practice. A protocol that depends on an unscalable
human is worse than one that names its limits.

Instead, the pool is **three deliberately divergent LLM raters**, designed
so that "agreement" is no longer the health signal:

| Rater | Role | Prompt direction |
|---|---|---|
| **A** | Evidence-strict labeler | Tightest possible reading: label as related only when the relationship is explicit in the text |
| **B** | Evidence-light labeler | Loosest defensible reading: label as related if a domain-aware reader would plausibly see the connection |
| **C** | Adjudicator | Neutral interpretive role: read both A's and B's labels for the same pair and decide which reading the pair belongs to (NOT a tie-breaker vote) |

A and B are **intentionally pushed apart**. Their prompts must diverge enough
that a 30%–60% disagreement rate emerges naturally on the calibration sample
(see §7). C is *not* a third independent labeler — C reads `(pair, A_label, B_label)`
and produces an interpretation, not a vote.

### Why this is not "fake IAA"

The shared-bias risk in `bf4238aa` is that homogeneous LLM raters can agree
*because they share a defaults-stack*, not because the pair is unambiguous.
The adversarial design defuses this by **inverting the success metric**:
when A and B agree on this protocol, that is a *signal of pair-clarity*, not
of rater-quality. When they disagree (the expected mode), C's interpretation
of the disagreement is the actual deliverable.

### Constraints

- A and B MUST use distinguishable prompts. If the same prompt is loaded for
  both raters by mistake, round 1 is invalid.
- C MUST NOT see the `unrelated` / `related_to` decision in isolation — C
  always sees both A and B labels together (this is what makes C an
  adjudicator rather than a third independent rater).
- Model-family diversity (e.g. one OpenAI, one Gemini for A and B) is
  preferred but not required. The prompt-divergence is the load-bearing
  axis; model diversity is a bonus.
- The decision to run with all-LLM is recorded here as the project's design
  choice, not deferred to per-round justification.

## 6. Sample design

### 6.1 Floor

Calibration round 1 uses **30 pairs** drawn from the 50 already in
`pilot_2026_04/_local/pairs.jsonl`. The classical-IAA argument for a
50-floor (Krippendorff α confidence interval) doesn't apply once the gate
is replaced by the §7 disagreement-zone diagnostic — disagreement-rate is
stable on smaller samples, and round 1's purpose is to surface
disagreement *structure* fast so §3 and §4 can be written from real data.

Subsequent rounds (if the iteration cap allows) may expand toward 50 if a
specific stratum is under-sampled or C's tendency is borderline.

### 6.2 Stratification

Sampling must be stratified by `(cosine_similarity_band, candidate_edge_type)`.
The bands match the candidate-generation logic in
`edge_discovery.py:60-61`:

| Band | Cosine range | Rationale |
|---|---|---|
| Low | `[0.6, 0.7)` | Borderline candidates; protocol must be stable here |
| Mid | `[0.7, 0.8)` | The judge's primary working range |
| High | `[0.8, 0.9)` | Near-duplicate territory; many `depends_on` / `learned_from` cases |

Per stratum target for round 1: at least 8 pairs each (so 24 with 6 left
over for whichever band is least represented in the existing 50). If the
candidate pool from production data is too sparse in any band, document the
gap and continue with what's available — do not back-fill with synthetic
pairs.

### 6.3 Existing pilot artifacts

`backend/tests/services/sleep/eval/pilot_2026_04/_local/pairs.jsonl`
contains 50 pairs from the prior pilot, each annotated by `openai` and
`gemini`, with `labeling_prompt_sha256` recorded. Round 1 should re-label a
stratified slice of these pairs under the new protocol rather than collecting
fresh data — the disagreement pattern visible in the existing annotations
(e.g. `pair_id=p0001` shows `unrelated` vs `inferential_causal` for the same
pair) is exactly the calibration target. Fresh data collection is needed only
if a stratum is not adequately represented in the existing 50.

## 7. The diagnostic gate (replaces classical IAA)

Krippendorff's α and Cohen's κ both assume raters are *trying to agree*.
With the adversarial pool in §5, A and B are deliberately pushed apart, so a
literal α/κ on `{A, B, C}` would either (a) be artificially low because A
and B are designed to diverge, or (b) be artificially high because C is
explicitly conditioned on A and B and so cannot contribute independent
signal. Either way, α/κ stops measuring what we care about.

Instead, the gate is a **two-stage diagnostic**:

### 7.1 Stage 1 — A vs B disagreement zone

Compute the disagreement rate between A and B on the calibration sample,
counting each pair's (related/not + edge_type) tuple as a single decision
(disagreement = labels differ on either component).

| A–B disagreement rate | Interpretation | Action |
|---|---|---|
| `< 30%` | **Shared-bias risk.** A's and B's prompts didn't push them apart enough; what looks like agreement is the LLM defaults-stack agreeing with itself. | Tighten A (more evidence-strict) and/or loosen B (more evidence-light); re-run round. |
| `30%–60%` | **Healthy zone.** Prompts produce real divergence; the disagreement carries genuine signal about which pairs are ambiguous. | Proceed to Stage 2. |
| `> 60%` | **Task-undefined risk.** The labeling task itself isn't crisp enough; A and B aren't disagreeing on hard cases, they're disagreeing because the question is unanswerable. | Tighten §4 protocol-choice rule; rewrite §3 edge-type definitions; re-run round. |

The 30/60 thresholds are chosen heuristically; this is **not a
reliability-coefficient gate** and we do not claim it is. It is a structural
sanity check designed to fail loudly in the two known failure modes
(homogeneous bias and task incoherence). The thresholds may be revisited
after round 1.

### 7.2 Stage 2 — C tendency interpretation

When Stage 1 is in the healthy zone, C's labels are interpreted *as a
distribution*, not as votes:

| C's tendency on disagreement pairs | Interpretation |
|---|---|
| C aligns with A (≥ 60% of A–B disagreement pairs) | Project converges on the **strict** reading; finalize §4 to evidence-strict. |
| C aligns with B (≥ 60% of A–B disagreement pairs) | Project converges on the **light** reading; finalize §4 to evidence-light. |
| C is roughly balanced (40%–60% either way) | C cannot distinguish the readings as currently specified — guideline is under-determined; rewrite §3 with C's per-pair rationale used to refine examples; re-run. |
| C is incoherent (different cases the same on different days, low confidence everywhere) | Adjudicator prompt itself is broken or the adjudicator model is too weak; swap C model or rewrite C prompt. |

Stage 2 is the **labeling decision**, not a rater-quality measurement. Its
output feeds directly into §4 (the strict-vs-light protocol choice) and §3
(the edge-type definitions, refined from C's per-pair rationale).

### 7.3 Iteration cap

Maximum **3 calibration rounds**. If round 3 still doesn't land Stage 1 in
the healthy zone, do **not** continue rewriting the guideline — the failure
is no longer about prompts. Open a follow-up issue scoped to "root-cause
analysis of why edge_discovery labels are not learnable as a consistent
task" and defer Phase 2 indefinitely.

## 8. Adjudication protocol

In the adversarial pool (§5), C is the adjudicator and adjudication is
*not* a tie-breaker vote — it is C's structural interpretation of the A vs
B disagreement pattern (see §7.2). For every pair in round N:

- A and B label independently, with no access to each other's output.
- C is then shown `(pair, A_label, B_label)` and produces:
  - C's own label (related/not + edge_type), and
  - A short rationale explaining whether the case feels strict, light, or
    genuinely ambiguous.
- Confidence rating per rater retained for downstream uncertainty
  quantification; not used in §7 gate computation.
- C's per-pair rationale on disagreement pairs feeds back into §3 worked
  examples and §4 protocol-choice — round 1 is what populates those
  sections.

There is no human tie-breaker in this protocol. If C is incoherent on a
pair (low confidence, contradicts itself across re-asks), that pair is
flagged as `unlabelable` in §10 round outcomes; it is not silently dropped.

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
- Sample size: 30 (drawn from `pilot_2026_04/_local/pairs.jsonl`)
- Stratum coverage: low [0.6,0.7) = —, mid [0.7,0.8) = —, high [0.8,0.9) = —
- Rater pool:
  - A (evidence-strict) = `<model> + <prompt sha>`
  - B (evidence-light) = `<model> + <prompt sha>`
  - C (adjudicator) = `<model> + <prompt sha>`
- Stage 1 — A–B disagreement rate: — (zone: —)
- Stage 2 — C tendency: — (aligned with A on N pairs, B on M, balanced/incoherent on K)
- §4 reading decision: —
- Unlabelable pairs (C incoherent): —
- Round-1 rationale extracts feeding §3 worked examples: —
- Decision (proceed to Phase 2 / re-run / defer Phase 2): —

## 11. References

- Issue [#375](https://github.com/kagura-ai/memory-cloud/issues/375) — this work
- Issue [#306](https://github.com/kagura-ai/memory-cloud/issues/306) — production observability (complementary, not replacement)
- Issue [#274](https://github.com/kagura-ai/memory-cloud/issues/274), [#249](https://github.com/kagura-ai/memory-cloud/issues/249) — predecessor pilots (closed; informed this redesign)
- PR [#371](https://github.com/kagura-ai/memory-cloud/pull/371) — observability merge that triggered this redesign
- PR [#420](https://github.com/kagura-ai/memory-cloud/pull/420) — `EdgeType` directionality (#373/#374); §3.2 must align with the directionality semantics introduced there
- Krippendorff, K. (2004). *Reliability in Content Analysis: Some Common Misconceptions and Recommendations.* Human Communication Research, 30(3) — referenced in §7 to explain why classical IAA does not apply to the adversarial pool used here
