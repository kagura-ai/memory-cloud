# Pilot #249 Findings (Sleep edge_discovery, April 2026)

**Status**: DRAFT — fill in after running annotation + spot-check
**Issue**: [#249](https://github.com/kagura-ai/memory-cloud/issues/249)
**Spot-check verdict**: <PASS | FAIL> (avg agreement: <n>%)
**Total pairs labeled**: 50 (A=25, B=10, C=8, D=7)
**Contexts used**: <kagura-dev only | kagura-dev + personal_memo>

## TL;DR

<2-3 sentences — highest-signal qualitative takeaway. The one thing the reader should walk
away with after 30 seconds.>

## Observations by stratum

### Stratum A (primary, cos ∈ [0.4, 0.6), no existing edge)

This is the k-NN gap stratum where LLM discovery has to earn its keep.

- Claude-opus-4-6 label distribution: TODO
- gpt-4o label distribution: TODO
- Where they agreed: TODO (count + most common label)
- Where they disagreed (with pair_ids): TODO
- Most striking inferential pair (pair_id): TODO — quote both summaries + the label rationale
- Most striking false-positive (pair_id): TODO

### Stratum B (cos ∈ [0.6, 0.9], existing edges allowed — current sleep target)

This is what the production edge_discovery currently sees.

- Label distributions: TODO
- Did the LLMs find inferential structure beyond what the existing edges already capture? TODO
- Pairs where existing_edge_type ≠ what the LLMs labeled: TODO

### Stratum C (cos ∈ [0.2, 0.4), diagnostic — does the cosine-band framing have a blind spot?)

**Key question**: did the models find ANY inferential edges at cosine < 0.4?

- If **yes**: how many, and what kind? This is the strongest signal that the production
  filter is excluding the regime where LLM discovery matters most. Note specific pair_ids.
- If **no**: the cosine band [0.4, 0.9] is the right scope and Stratum C confirms it.

### Stratum D (hard negatives, shared-tag rank from A's universe — annotator validation only)

**Excluded from main prevalence reading.** These are pairs constructed to LOOK similar (high
shared-tag overlap) but be inferentially unrelated.

- Did the models fall for the trap? (count of `inferential_*` labels here)
- Tag overlap vs label pattern: TODO

## Inter-annotator agreement (Stratum A + B + C only — D excluded)

| Label class | claude count | gpt-4o count | both_agree | only_claude | only_gpt | disagree_pair_ids |
|---|---|---|---|---|---|---|
| unrelated | | | | | | |
| semantic_only | | | | | | |
| inferential_causal | | | | | | |
| inferential_procedural | | | | | | |
| inferential_supersedes | | | | | | |
| inferential_contradicts | | | | | | |

Cohen's κ (model-vs-model on 6-class, A+B+C only): TODO — compute in stdlib

## Cost + wall-clock

- Total LLM calls: <n> (cap 120)
- Total tokens: <n> (cap 200,000)
- Estimated cost: $<n> (envelope <$5)
- Sampling wall-clock: <n>s
- Annotation wall-clock: <n>min

## Qualitative anecdotes

<2-4 specific pair_ids where the LLMs disagreed with the author OR with each other,
and what that tells us about the 6-class taxonomy or the prompt.>

### Anecdote 1 — pair_id <pXXXX>

> src.summary: …
> dst.summary: …
> claude: <label> ("<rationale>")
> gpt-4o: <label> ("<rationale>")
> author: <label> ("<rationale>")

What this teaches us: …

## Limitations

- n=50 is a probe, not a measurement. No CIs, no significance tests.
- LLM labels are "silver consensus" — not gold. Two correlated annotators may be wrong together.
- Single human spot-check (n=10). Cohen's κ inter-annotator-reliability is full-eval scope.
- <kagura-dev only OR multi-context — note which>
- Specific caveats discovered during the run: TODO
