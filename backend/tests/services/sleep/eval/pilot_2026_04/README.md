# Pilot #249 — Sleep edge_discovery probe (April 2026)

**Issue**: [#249](https://github.com/kagura-ai/memory-cloud/issues/249)
**Branch**: `249-feat/chore-sleep-pilot-eval-probe-for-edge-di`
**Status**: Implementation in progress

## What this is

A 1-day **qualitative directional probe** for `services/sleep/edge_discovery`. It samples
50 memory pairs across 2 contexts × 4 strata, has them labeled by 2 LLM annotators
(`claude-opus-4-6` + `gpt-4o`), spot-checks against a human, and writes qualitative
findings. It does **not** produce statistical conclusions, prevalence estimates with CIs,
or pre-registered decision rules. The follow-up full eval is commissioned regardless of
pilot outcome.

See the [issue body](https://github.com/kagura-ai/memory-cloud/issues/249) for the full
spec and the gate1 design review history (CAIO + stats PhD + DS PhD).

## How to run (in order)

```bash
# Phase A — directory + README + templates (this commit)
# (no script execution)

# Phase C — labeling prompt committed BEFORE annotation runs
git log --oneline -- labeling_prompt.md   # must show a commit before run_annotation.py is invoked

# Phase B.1 — sampling
python sampling_script.py --user-id <uuid> --workspace-id <uuid> --dry-run    # verify counts
python sampling_script.py --user-id <uuid> --workspace-id <uuid>              # writes pairs.jsonl + snapshot.json

# Phase B.2 — annotation (requires ANTHROPIC_API_KEY + OPENAI_API_KEY in env)
python run_annotation.py --annotator both --max-calls 4 --dry-run             # smoke-test
python run_annotation.py --annotator both --max-calls 120                     # ~100 calls

# Phase B.3 — author blind spot-check
python run_spot_check.py --pairs pairs.jsonl --n 10 --seed 4242

# Phase B.4 — author writes findings.md and next_steps.md from templates
```

## Privacy model — why there are two `pairs.jsonl` files

The sampling context (`kagura-dev`) is the author's real development log. Committing
`src_summary` / `dst_summary` verbatim into a **public** GitHub repo would publish the
author's memories forever. So the pilot uses a **two-file pattern**:

| File | Content | Location | Committed? |
|---|---|---|---|
| `_local/pairs.jsonl` | Full — real `src_summary` + `dst_summary` content | `_local/` (gitignored) | ❌ never |
| `pairs.jsonl` | Redacted — summaries replaced with `"<redacted>"`, all other fields preserved | this dir | ✅ yes |
| `_local/snapshot.json` | Full snapshot (identical to committed copy — no memory content) | `_local/` | copy is committed |
| `snapshot.json` | Same as `_local/snapshot.json` | this dir | ✅ yes |

Derivation: after `sampling_script.py` writes the full `pairs.jsonl` to `_local/`, run
`python redact_pairs.py` to produce the committed `pairs.jsonl`. The script replaces
only the two `*_summary` fields and preserves everything else (pair_id, stratum,
cosine, tags, existing-edge metadata, Stratum D ranking audit fields, prompt hash,
annotations when present).

**For LLM annotation (Phase B.2)** `run_annotation.py` reads `_local/pairs.jsonl` so
the real text is available to claude-opus-4-6 and gpt-4o. It writes annotations BACK
to `_local/pairs.jsonl`. Re-running `redact_pairs.py` after annotation updates the
committed `pairs.jsonl` with the labels while keeping summaries redacted.

**For findings write-up** The author pulls anecdotes from `_local/pairs.jsonl`,
manually anonymizes quoted text before putting them into `findings.md`. The committed
`findings.md` is free of verbatim memory content.

## Directory naming — deviation from issue spec

The issue body originally said `backend/tests/sleep/eval/pilot_2026-04/`. **Two corrections**
were applied during implementation planning:

1. **Parent path**: the actual convention in this repo is `backend/tests/services/sleep/`
   (e.g. `test_edge_discovery.py` lives there). The issue spec missed the `services/` segment.
2. **Date format**: the issue spec used `pilot_2026-04` (hyphen). Python's import machinery
   cannot import a module from a directory whose name contains a hyphen, so the directory
   is renamed `pilot_2026_04` (underscore). Same April 2026 date, just legal as a Python
   module path so the determinism test can `importlib`-load `sampling_script.py`.

The issue body was updated to v2.1 with the corrected path. This README is the audit trail.

## Token budget + cost envelope

| | Calls | Tokens (est) | Cost ceiling |
|---|---|---|---|
| `claude-opus-4-6` | 50 (×retries) | ~78k | ~$2.00 |
| `gpt-4o` | 50 (×retries) | ~78k | ~$0.80 |
| **Total upper bound** | **120 (hard cap)** | **~200k (hard cap)** | **<$5** |

Hard caps:
- `MAX_CALLS_DEFAULT = 120` in `run_annotation.py` (CLI overridable downward only for dry runs)
- `TOKEN_CEILING = 200_000` checked before each call. Runaway bug fails closed within a few dollars.

## What happens if spot-check fails (LLM-human agreement < 70%)

`run_spot_check.py` exits with code 2 if average human-vs-LLM agreement is below the 70%
threshold from gate1. The off-ramp is operational, not vibes-based:

1. Mark this branch's PR as draft with comment linking the failed `spot_check.md`.
2. File a follow-up issue `chore(sleep-eval): pilot #249 spot-check failed — prompt iteration needed`,
   linking this directory and the labeling_prompt.md SHA.
3. Keep `pairs.jsonl` — the failure mode is itself a finding. Write it up in `findings.md`
   under "Attempt 1: prompt did not transfer to author intuition" with specific disagreement examples.
4. **Do NOT open the follow-up full-eval issue yet** — `next_steps.md` should explicitly
   recommend AGAINST scaling up until the prompt + taxonomy are revised.

## Per-cell n allocation

50 pairs total, 60/40 split (kagura-dev / personal_memo) within each stratum:

| Stratum | Cosine band | Filter | kagura-dev | personal_memo | Total |
|---------|-------------|--------|------------|---------------|-------|
| **A** (primary) | [0.4, 0.6) | no existing edge (mirrors prod `_is_synthetic_seed_edge`) | 15 | 10 | 25 |
| **B** (diagnostic) | [0.6, 0.9] | post-#248 filter | 6 | 4 | 10 |
| **C** (diagnostic) | [0.2, 0.4) | post-#248 filter | 5 | 3 | 8 |
| **D** (annotator validation, **excluded from main findings**) | n/a — shared-tag rank from A's universe | n/a | 4 | 3 | 7 |
| **Total** | | | **30** | **20** | **50** |

**Fallback**: if `personal_memo` has < 100 memories or doesn't exist, the personal_memo column
collapses and kagura-dev absorbs the full 50 (A=25/B=10/C=8/D=7). This is logged to
`snapshot.json` under `fallback_reason` and printed loudly to stdout.

## Gate1 refinements — landing locations

| # | Refinement | Landing file |
|---|------------|--------------|
| 1 | Per-cell n pinned | `sampling_script.py` (`ALLOCATION` constant) + this README + determinism test |
| 2 | Spot-check failure off-ramp | `run_spot_check.py` (verdict + exit code) + this README §"What happens if spot-check fails" |
| 3 | Token budget ceiling | `run_annotation.py` (`TOKEN_CEILING` + `MAX_CALLS_DEFAULT`) + this README §"Token budget" |
| 4 | Path verification | This README §"Directory naming" + the directory location itself |
| 5 | Stratum D provenance | `sampling_script.py::build_stratum_d` docstring + `pairs.jsonl` rows include `d_shared_tag_count` + `d_ranked_from_pair_id` |

## Reproducibility

- `numpy.random.default_rng(seed=42)` is the only RNG in `sampling_script.py`. Re-run with the
  same DB snapshot must produce a byte-identical `pairs.jsonl`.
- Spot-check uses an independent seed (`seed=4242`) so spot-check pair selection is decorrelated
  from sampling order but still reproducible.
- `snapshot.json` records: contexts resolved, total memories per context, allocation actually used,
  fallback reason if any, edge_discovery.py git SHA, numpy/python/qdrant-client versions, ISO timestamp.
- `pairs.jsonl` rows record the labeling_prompt.md SHA so a future re-annotation against a different
  prompt is detectable.

## Constraints

- Time-boxed: 1 working day. If it exceeds, scope is wrong — stop and re-plan.
- **No production code changes** in `services/sleep/`. This directory is read-only research.
- Single PR.
