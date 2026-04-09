# Pilot #249 — Next Steps

**Status**: DRAFT — fill in after `findings.md`
**Issue**: [#249](https://github.com/kagura-ai/memory-cloud/issues/249)

## Recommendation

<Pick exactly one:>

- [ ] **GO** — file a full-eval issue with n ≥ 200 per stratum and gold labels from 2+ annotators.
- [ ] **GO with revisions** — file the full-eval issue but adjust <prompt | taxonomy | strata> first.
- [ ] **NO-GO (yet)** — do not scale up until the following blockers are resolved:
  - <blocker 1>
  - <blocker 2>

## Justification

<Cite specific findings.md sections that drove the recommendation. Be concrete: which
stratum's results, which pair_ids, what taxonomy issue. The justification must survive
scrutiny in code review.>

## If GO: proposed full-eval issue

Use this section as the body of the new issue if the recommendation above is GO or GO-with-revisions.

- **Title**: `feat(sleep-eval): full evaluation of edge_discovery filter (n≥200)`
- **Strata** (refine based on pilot's Stratum C diagnostic results):
  - Stratum A: cosine [0.4, 0.6), no existing edge → n=<TODO>
  - Stratum B: cosine [0.6, 0.9] → n=<TODO>
  - Stratum C: cosine [0.2, 0.4) → keep / drop based on pilot
  - Stratum D: hard negatives → n=<TODO>
- **Annotators**: 2 humans, adjudication on disagreement, Cohen's κ ≥ 0.6 required
- **Decision rules**: pre-registered with proper power analysis (Path A from gate1 stats review)
- **Budget**: <TODO — derive from pilot wall-clock + token cost × scale factor>
- **Timeline**: <TODO>

## Open questions for the full eval

(Things the pilot raised but couldn't answer.)

1. <Question 1>
2. <Question 2>

## What this pilot could NOT answer

- <Limitation 1>
- <Limitation 2>

## If NO-GO: what needs to happen first

- <Concrete action 1>
- <Concrete action 2>
