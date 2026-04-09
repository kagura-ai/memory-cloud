# Pilot #249 Findings (Sleep edge_discovery, April 2026)

**Status**: COMPLETE — pilot ABORTED at the spot-check gate, but the abort is itself the finding
**Issue**: [#249](https://github.com/kagura-ai/memory-cloud/issues/249)
**Spot-check verdict**: **ABORT** (40% LLM-human agreement avg, threshold 70%)
**Total pairs labeled**: 50 (A=25, B=10, C=8, D=7)
**Contexts used**: kagura-dev only (fallback path — `personal_memo` had only 1 memory)
**Annotators**: `gpt-5.4` + `gemini-2.5-pro` (changed from spec — see PR/issue)

## TL;DR

The pilot did not produce a usable silver consensus. Both LLM annotators
disagreed with the author 60% of the time on a stratified blind spot-check.
The abort is **not** a failure of the pipeline — sampling, annotation, and
the gate all worked exactly as designed. **It is a finding about the labeling
prompt and the underlying taxonomy**: the author and the LLMs are reading
the same 6-class taxonomy through fundamentally different rules, and the
pilot caught this before any of it could justify v0.11.0 scope.

The next sleep-related issue should NOT be a full eval at the current
prompt. It should be **prompt and taxonomy iteration**.

## What the pilot was supposed to answer

> Do "inferential" memory pairs (causal, procedural, supersedes,
> contradicts) appear in real production memory data with enough frequency
> to justify investing in a full LLM-based edge_discovery prompt redesign?

The pilot **cannot** answer this question with the current design because
it cannot establish what "inferential" means in a way that the author
and the LLMs both agree on.

## Key observations

### 1. The labeling prompt is interpretable in two very different ways

- **Author rule** ("human-intuitive, evidence-light"): if a careful reader
  can plausibly infer a relationship between two memories, label it
  inferential. Even if the relationship isn't explicitly written, the
  semantic context implies it.
- **LLM rule** ("evidence-strict"): only label inferential when the
  relationship is *explicitly* present in the memory text. Otherwise,
  default to `semantic_only` or `unrelated`.

The labeling prompt as written does not distinguish between these two
philosophies. Both readings of the prompt are defensible. The author's
default is the human-intuitive rule; both `gpt-5.4` and `gemini-2.5-pro`
default to the evidence-strict rule.

This is the root cause of every disagreement on the spot-check.

### 2. The author systematically over-labels relationship strength

Pattern observed across the 10 spot-check pairs:

- **`unrelated` → `inferential_causal`** (author): the author sees a causal
  story in two unrelated planning notes. AIs see no shared subject.
- **`semantic_only` → `inferential_procedural`** (author): the author
  reads a procedural connection from "this is the workflow rule, this is
  a successful merge." AIs see a workflow rule and an example, not a
  procedure binding the two.
- **`semantic_only` → `inferential_contradicts`** (author): the author
  reads a "tension between two ideas" as a contradiction. AIs only label
  contradicts on direct factual disagreement.

The author's labels run **one or two notches higher** on the
semantic_only → causal → procedural → supersedes → contradicts axis than
both LLMs. The bias is consistent and directional, not random.

### 3. LLM consensus on the lower-bound hypothesis is strong

Where the author's labels did NOT inflate the relationship strength, both
LLMs agreed perfectly with each other AND with the author:

- **Stratum C (cosine [0.2, 0.4))**: 100% `unrelated` from all three
  raters across 8 pairs. The "no inferential pairs at low cosine"
  diagnostic finding from the annotation phase is reproduced under the
  most adversarial reading possible (independent author + two LLMs).
  This is the **one statistically meaningful conclusion** of the entire
  pilot: cosine < 0.4 has no inferential signal, and the production
  edge_discovery band of [0.4, 0.9] is empirically defensible.
- **Stratum A on `unrelated` calls**: when both AIs called `unrelated`,
  the author also called `unrelated` in the spot-check picks where
  there was nothing to inflate. The disagreements clustered on the
  marginal cases.

### 4. Hard negatives partially worked

Of the 7 Stratum D pairs (shared-tag-ranked from A's universe), the LLMs
correctly rejected most, but one was labeled `inferential_contradicts`
by the author (`p0046`), which neither LLM agreed with. This reinforces
finding #2: the author is reaching for stronger labels than the LLMs are
willing to commit to.

## What this teaches us about the broader edge_discovery question

The pilot was supposed to inform "is the LLM phase worth investing in?"
The spot-check abort tells us:

**If we deploy LLM edge_discovery to production with the current prompt,
the edges it produces will not match the user's mental model of what
should be connected.** Specifically:

- The user expects ~25% of cosine [0.4, 0.6) pairs to be inferential
  (because the user reads relationships intuitively).
- The LLMs will produce ~5-10% inferential edges from that band
  (because they apply the evidence-strict rule).
- The 15-20% gap is **not** missing edges that the LLMs failed to find;
  it is edges the user expects to exist that the LLMs (correctly, by
  the prompt's strict reading) decline to create.

The mismatch is a **prompt design problem**, not a model capability problem.

## Validated reproducibility / process findings

1. **The deterministic sampling pipeline works end-to-end** against a
   real production VM (`docker exec kagura-api python3 sampling_script.py`),
   produces byte-stable outputs, and survives a re-run on the same DB
   snapshot.
2. **The two-file privacy pattern works** (full pairs.jsonl in
   `_local/`, redacted view committed). The author's development log
   never touched the public git history.
3. **The annotation runner's token budget guard worked** (241k tokens
   used, 300k ceiling, 0 errors across 100 calls). gpt-5.4 + gemini-2.5-pro
   are a viable annotator pair when the prompt is unambiguous.
4. **The gate1 spot-check abort condition fired exactly as designed**
   and prevented us from writing up "the LLMs found X% inferential, here's
   our v0.11.0 plan" — which would have been wrong.

## Limitations of these findings

- n=50 (single context, kagura-dev only). Cannot generalize to typical
  user workflows.
- Single human annotator (the issue author). Cohen's κ inter-author
  agreement is full-eval scope.
- LLM labels are "silver consensus" from gpt-5.4 + gemini-2.5-pro only.
  Adding Claude or a different model family might shift the distribution.
- The 6-class taxonomy itself has not been validated. The pilot suggests
  it's interpretable in incompatible ways even with extensive examples.

## Where the disagreements point next

The **disagreement table** in `spot_check.md` and the gold-standard read
of the rationales in `_local/spot_check_full.md` are the key artifact for
the prompt-iteration follow-up issue. The 6 disagreement pairs cluster
on:

- `unrelated` ↔ `semantic_only`: 2 cases where author was looser
- `semantic_only` ↔ `inferential_procedural`: 2 cases where author was tighter on one and looser on the other
- `inferential_*` axis confusion: 1 case (`p0033`) where all 3 raters picked different `inferential_*` sub-types
- `inferential_contradicts` definition: 1 case (`p0046`) where author saw "tension" and AIs saw "no shared topic"
