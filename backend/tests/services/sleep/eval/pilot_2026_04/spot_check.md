# Pilot #249 — author spot-check

**Generated**: 2026-04-09T06:54:09.653860+00:00
**Seed**: 4242
**Pairs sampled**: 10

## Verdict

- author vs openai : **4/10** (40%)
- author vs gemini : **4/10** (40%)
- average           : **40%**
- threshold         : **70%**

## VERDICT: ABORT

Average LLM-human label agreement is BELOW the 70% threshold from
the gate1 design review. The silver consensus is **broken**.

### Off-ramp actions (operator)

1. Mark the pilot PR as draft with the comment:

   > Spot-check failed at 40% average
   > agreement (threshold 70%). The annotations are retained for
   > post-mortem but should not be treated as silver consensus.
   > Filing follow-up issue for prompt iteration.

2. File follow-up GitHub issue:

   ```
   gh issue create --title 'chore(sleep-eval): pilot #249 spot-check failed — prompt iteration needed' \
       --body '<link to this spot_check.md, labeling_prompt.md SHA, disagreement examples>'
   ```

3. Do NOT delete `_local/pairs.jsonl` — the failure mode is itself
   a finding. Document it in `findings.md` under "Attempt 1: prompt
   did not transfer to author intuition" with specific disagreement
   pair_ids from the table below.

4. Do NOT open a follow-up full-eval issue. `next_steps.md` must
   recommend AGAINST scaling up until prompt + taxonomy are revised.

## Per-pair comparison

| pair_id | stratum | cos | author | openai | gemini | a==o | a==g |
|---|---|---|---|---|---|---|---|
| p0002 | A | 0.459 | inferential_causal | unrelated | unrelated | ✗ | ✗ |
| p0009 | A | 0.402 | unrelated | unrelated | unrelated | ✓ | ✓ |
| p0012 | A | 0.404 | unrelated | unrelated | unrelated | ✓ | ✓ |
| p0018 | A | 0.535 | inferential_procedural | inferential_causal | inferential_procedural | ✗ | ✓ |
| p0021 | A | 0.431 | semantic_only | inferential_procedural | inferential_procedural | ✗ | ✗ |
| p0025 | A | 0.428 | semantic_only | unrelated | unrelated | ✗ | ✗ |
| p0027 | B | 0.701 | inferential_supersedes | inferential_supersedes | semantic_only | ✓ | ✗ |
| p0033 | B | 0.632 | inferential_procedural | inferential_supersedes | inferential_causal | ✗ | ✗ |
| p0040 | C | 0.264 | unrelated | unrelated | unrelated | ✓ | ✓ |
| p0046 | D | 0.497 | inferential_contradicts | semantic_only | semantic_only | ✗ | ✗ |

_(rationales redacted from committed view — see `_local/spot_check_full.md` for the full version with author and LLM rationales. LLM rationales paraphrase source memory content.)_
