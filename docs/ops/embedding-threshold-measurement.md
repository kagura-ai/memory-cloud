# Embedding Threshold Measurement Runbook

When to use: you need to recalibrate or audit the kNN seed similarity
threshold (`knn_seed_min_percentile` / `knn_seed_min_similarity`) for a
production context. Typical triggers:

- **Bootstrap**: a context first crosses the D3 gate (≥200 memories or
  ≥10k top-k observations) and needs an initial calibration.
- **Stability check**: time-anchored re-measurement (e.g. W+7 protocol
  from #407 Task 2) to confirm a TTL is still conservative.
- **Cross-model validation**: a new embedding model is being qualified
  for production and D1 (percentile-calibration model-independence)
  needs empirical confirmation on a real corpus.
- **Drift investigation**: edge-creation rate has shifted unexpectedly
  and you want to rule out / rule in calibration drift.

The measurement is **read-only** — it samples memories with
`ORDER BY random()`, runs Qdrant top-k searches, and writes a JSON
report to the container's `/tmp`. No DB writes, no Qdrant writes.

## Pre-flight

### 1. Find the active container color

The canonical source of truth is the local file (not `docker ps`):

```bash
ACTIVE=$(cat /opt/kagura-memory/active-color)   # blue|green
echo "kagura-api-${ACTIVE}"
```

Run this on the VM (`kagura-memory-vm`). The active-color file is
maintained by the deploy pipeline and is the same file
[deployment.md](../deployment.md) uses for the tag_cooccurrence backfill
runbook.

### 2. Note the bootstrap gate

The script logs `context_memory_count=N` on the first line of its
output, so you don't need a separate pre-check — just read it from the
run's stdout. But know the threshold before running:

Bootstrap gate (D3): the script warns if effective_memories < 200 or
observations_total < 10,000. At `--memories 200 --top-k 50` you need
roughly 220+ memories in the context to clear the gate cleanly (some
samples drop out because they have no top-k neighbors above noise).
Below that, the percentile estimate is still computed but should be
treated as advisory.

## Execution

Run from a workstation with `gcloud` configured for the
`kagura-492509` project. The full one-shot invocation:

```bash
CONTEXT=<context-uuid>
LABEL=<short-label>           # e.g. kagura_dev_w7
DATE=$(date -u +%Y-%m-%d)

gcloud compute ssh kagura-memory-vm \
  --zone asia-northeast1-a \
  --project kagura-492509 \
  --tunnel-through-iap \
  --command="
    set -e
    ACTIVE=\$(cat /opt/kagura-memory/active-color)
    echo \"=== measurement \${ACTIVE} \$(date -u +%Y-%m-%dT%H:%M:%SZ) ===\"
    docker exec kagura-api-\${ACTIVE} python /app/scripts/measure_embedding_threshold.py \\
      --context-id ${CONTEXT} \\
      --memories 200 \\
      --top-k 50 \\
      --output /tmp/measure_${LABEL}.json
    docker cp kagura-api-\${ACTIVE}:/tmp/measure_${LABEL}.json /tmp/measure_${LABEL}.json
  "
```

Default parameters (`--memories 200 --top-k 50`) are pinned by Phase A
design decision C3 (#240). Don't change them for stability comparisons —
they determine sample variance and any change invalidates W-vs-W diffs.

## Output retrieval

After the SSH command completes, the JSON is on the VM host. Pull it
locally and archive under a per-context directory:

```bash
mkdir -p ~/measurements/${LABEL%_*}    # e.g. ~/measurements/kagura_dev
gcloud compute scp \
  --zone asia-northeast1-a \
  --project kagura-492509 \
  --tunnel-through-iap \
  kagura-memory-vm:/tmp/measure_${LABEL}.json \
  ~/measurements/${LABEL%_*}/measure_${LABEL}_${DATE}.json
```

The local archive is the **system of record** — VM `/tmp/` is volatile,
container `/tmp/` is doubly volatile. If the measurement is for an issue
(e.g. #240 Phase A or #407 Task 2), also paste the JSON inside a
collapsible `<details>` block in the issue comment so the report
survives independent of disk state.

## Interpretation

### Read the script's stdout summary

The script prints a summary block to stdout. The two distributions to
compare:

- **Top-k neighbor distribution** — runtime-facing (D1). The percentile
  used for `knn_seed_min_percentile` (default 90.0) is read from this.
- **Random-pair baseline** — diagnostic-only (D1). It establishes the
  noise floor for this corpus. p25–p75 of random pairs gives you the
  noise band; anything in top-k below random p90 is statistically
  indistinguishable from noise.

### Sample variance you must keep in mind

`sample_memories` uses `ORDER BY func.random()` **without** a SQL-level
seed (the script's `--seed` argument only affects the diagnostic
`measure_random_pair` call, not the top-k sampling). Two consecutive
runs on the same context will sample different subsets, and the sample
SE for a p90 estimate at n=200 is approximately:

```
SE(p90) ≈ sqrt(0.9 × 0.1 / 200) ≈ 0.021
```

So a measured p90 difference of ±0.02 between two independent runs is
**at the noise floor** — it should be read as "drift not detected,"
not as "drift = 0." For week-over-week stability checks (W+7 protocol),
treat any |Δp90| < 0.02 as "TTL still conservative" and any |Δp90| ≥
0.04 as "investigate."

### Mixed signals when the population grows

If the context grew between two measurements, the top-k diff is a
**mix** of (a) corpus-level densification (more neighbors per memory →
top-k shifts up uniformly across percentiles) and (b) any model-internal
drift. The random-pair baseline drifting in the **opposite** direction
(downward, as the corpus diversifies) is the signature of pure
densification — see #407 Task 2 W+7 result for the canonical example.

To isolate model drift cleanly, repeat the protocol on a
**frozen-snapshot context** (one with no new ingests between W+0 and
W+7).

## Comment template for an issue comment

When posting results to an issue (Phase A acceptance, Task 2 W+7,
future cross-model checks), use this skeleton so successive comments
are diff-comparable by `grep`:

```markdown
## <Task name> — <window or context label>

### Parameters

- Context: `<uuid>` (`<context-name>`, <model>, <dimensions>d)
- `--memories 200 --top-k 50`
- Active container: `kagura-api-<color>` (post-#<PR> / commit `<sha>`)
- Baseline run (if applicable): <date> — link to prior comment

### Sample size shift (only if baseline exists)

| | <baseline-label> | <this-run-label> | Δ |
|---|---|---|---|
| context_memory_count | … | … | … |
| effective_memories | … / 200 | … / 200 | … |
| observations_total | … | … | … |

### Top-k neighbor distribution

| percentile | <baseline-label> | <this-run-label> | Δ |
|---|---|---|---|
| p25 | … | … | … |
| p50 | … | … | … |
| p75 | … | … | … |
| **p90** | **…** | **…** | **…** |
| p95 | … | … | … |
| p99 | … | … | … |

### Random-pair diagnostic

| percentile | <baseline-label> | <this-run-label> | Δ |
|---|---|---|---|
| p90 | … | … | … |
| p95 | … | … | … |
| p99 | … | … | … |

### Verdict

<one-line conclusion: "drift within noise" / "TTL still conservative" /
"flagged: drift exceeds threshold, propose tighter TTL" / etc.>

### Methodology caveats

1. Independent samples not paired (sample SE ~0.02 at n=200).
2. <population growth note if applicable>.
3. <bootstrap-gate informational warnings if applicable>.

### Recommendation

<concrete proposal — keep / change a default — or "no action">

---

<details>
<summary>Full JSON report</summary>

\```json
<paste container's /tmp/measure_*.json contents>
\```

</details>
```

## Cadence

Until Phase B (#240 successor) is finalized:

- **Bootstrap**: trigger on demand when a new context crosses D3.
- **Stability**: ad-hoc — operator decides when to re-validate.
- **Cross-model**: when a new embedding model is being qualified.

After Phase B is implemented:

- The runtime calibration job replaces the manual script for the
  bootstrap and lazy-TTL paths.
- This runbook stays useful for **audit** and **cross-model
  qualification** runs that the runtime job doesn't cover.
- Update this section once Phase B's calibration trigger set (the
  three triggers from C2 — Bootstrap / Admin manual / Lazy TTL) is in
  place.

## Troubleshooting

**`bootstrap_gate_below_threshold` warning** with effective_memories ~199:
normal sampling variance when sampling 200 from a smaller population
(e.g. 200/676). The percentile estimate is fine. If effective_memories
drops below 150, the warning is real — the corpus has too many isolated
memories with no neighbors above noise, and the percentile is
unreliable.

**Qdrant / Postgres version warnings** in the script output:
informational only — the script tolerates the version skew that
production currently runs.

**`docker exec` fails with "No such container"**: the active color
flipped mid-deploy or the deploy is in progress. Re-read
`/opt/kagura-memory/active-color` and retry. If the file is missing or
empty, the deploy pipeline is in an inconsistent state — escalate before
running ad-hoc commands.

**`gcloud compute ssh` IAP token expires** mid-run: rare for the
`--memories 200 --top-k 50` default (completes in well under the IAP
session window). If you bump `--memories` significantly higher,
wrap the docker exec in `tmux` or `nohup` on the VM to survive
disconnect.
