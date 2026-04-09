# Pilot #249 — Next Steps

**Status**: COMPLETE — verdict is NO-GO on a full eval at the current prompt
**Issue**: [#249](https://github.com/kagura-ai/memory-cloud/issues/249)
**Spot-check**: ABORT (40% LLM-human agreement, threshold 70%)

## Recommendation

**[X] NO-GO (yet)** — do not commission a full eval until the following are resolved:

1. **The labeling prompt is interpretable in two incompatible ways.** The
   author and the LLMs default to different "evidence strength" rules
   (human-intuitive vs evidence-strict). A full eval against the current
   prompt would just measure which rule a given annotator happened to
   read into the prompt — not whether the LLM phase has a target.

2. **The 6-class taxonomy needs disambiguation rules baked into the
   prompt itself.** Specifically: `unrelated` ↔ `semantic_only`,
   `semantic_only` ↔ `inferential_procedural`, and
   `inferential_contradicts` need decision-procedure language tight
   enough that two careful raters cannot reasonably disagree. The
   pilot's "decision procedure" section was not enough.

3. **A full eval should not start from scratch.** The pilot's sampling
   pipeline, annotation runner, spot-check CLI, and privacy model all
   work and are reusable. The only thing that needs replacing is
   `labeling_prompt.md`. The follow-up issue can scope to "v2 prompt +
   re-spot-check" without re-implementing the infrastructure.

## What goes in the follow-up issue

Title: `chore(sleep-eval): pilot #249 v2 — labeling prompt iteration after spot-check abort`

Body should cover:

- Link to this `findings.md` and the disagreement breakdown
- Link to `_local/spot_check_full.md` (gitignored locally; reference for the operator)
- The 4 disagreement clusters identified in `findings.md`
- Proposed prompt changes:
  - **Explicit "evidence-strict" language**: "label `inferential_*` only
    if a one-sentence quote from the source memory text would convince
    a skeptical reader. If you would have to say 'the author probably
    meant', drop one notch."
  - **Pairwise disambiguation tests**: for each ambiguous pair
    (e.g. `semantic_only` vs `inferential_procedural`), add an example
    that shows what tips the balance, with explicit wording: "this is
    `semantic_only` because the procedure is in the source but the dst
    is just an outcome, not a step in the procedure."
  - **Confidence calibration**: confidence < 0.6 ⇒ default to the
    weaker label. (Author currently labels with implicit confidence
    that doesn't match the AIs' explicit confidence values.)
  - **`contradicts` definition tightening**: must be a direct factual
    contradiction (X said A, Y said not-A), not "different priorities"
    or "tension between approaches."
- Rerun protocol: same `_local/pairs.jsonl` (no need to re-sample, the
  50 pairs are still valid), re-run `run_annotation.py` against v2 prompt,
  re-run `run_spot_check.py` against the same 10 picks (seed=4242 keeps
  picks identical for direct before/after comparison).
- Decision criterion for the v2 spot-check: average ≥ 70% to proceed,
  same as v1.

## What this pilot could NOT answer

- **Whether LLM-based edge_discovery has real value in the cosine
  [0.4, 0.6) band.** Both annotators DID find pairs they label inferential
  there, but the spot-check shows their reading and the author's reading
  diverge by 60%. So the 20-32% inferential rate from the annotation
  phase is unreliable as a "real" prevalence estimate.

- **What prevalence the user's mental model would actually produce on
  a larger sample.** A single 10-pair spot-check is not a survey of the
  user's labeling distribution.

- **Whether `personal_memo` (or any non-development context) has
  meaningfully different inferential prevalence.** The pilot ran in
  fallback mode (kagura-dev only) because `personal_memo` had 1 memory.

## Validated artifacts (worth keeping for the follow-up)

- `sampling_script.py` — works against production via docker exec, fully
  deterministic, byte-stable across re-runs.
- `pilot_llm.py` + `run_annotation.py` — gpt-5.4 + gemini-2.5-pro
  annotation runner with hard token budget. 0 errors across 100 calls.
- `run_spot_check.py` — interactive CLI, two-file privacy pattern,
  deterministic picks. The abort exit code worked as designed.
- `redact_pairs.py` — two-file pattern (full in `_local/`, redacted
  committed). Reusable for v2.
- `_local/pairs.jsonl` — the 50 sampled pairs are still valid. v2 just
  needs new annotations + new spot-check.
- `_local/spot_check_full.md` — full author + LLM rationales for the 10
  spot-check pairs. **The single most useful artifact for prompt v2
  design** — read it carefully when iterating the prompt.

## Validated process findings (independent of prompt issue)

1. The two-file privacy model worked. No memory content leaked into the
   public repo.
2. The deterministic sampling + production parity test caught no
   regressions in `_is_synthetic_seed_edge`.
3. The token budget guard (300k ceiling, 150 calls) was adequate for the
   default 2-annotator setup.
4. The gate1 spot-check abort condition is **the most valuable part of
   the design**. It saved us from writing up unreliable findings as if
   they were a basis for a v0.11.0 scope decision.

## Stratum C — the one validated finding

The pilot's only finding that survives the spot-check abort is **Stratum C**:

> 100% of 8 pairs in cosine [0.2, 0.4) labeled `unrelated` by all three
> independent raters (author + gpt-5.4 + gemini-2.5-pro).

This is statistically meaningless on its own (n=8) but **directionally
strong**: under the most adversarial labeling conditions possible (the
author who over-labels and two LLMs that under-label), nobody saw any
inferential signal in the cosine < 0.4 band. The current production
edge_discovery cosine band of [0.6, 0.9] (and the proposed extension to
[0.4, 0.9]) is empirically defensible at the lower bound — there is no
hidden regime of inferential pairs that the band excludes.
