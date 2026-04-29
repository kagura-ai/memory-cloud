# Neural Memory Evaluation

Evaluation of the Neural Memory (Hebbian learning + activation spreading) system, based on systematic benchmarking with the [Python SDK](https://github.com/kagura-ai/kagura-memory-python-sdk).

> Component-level labeling protocols for the LLM Judge surfaces feeding into these benchmarks live under [`docs/eval/`](eval/). See [`docs/eval/README.md`](eval/README.md) for the documentation map.

## Architecture

Neural Memory uses brain-inspired algorithms to build an association graph between memories:

- **Hebbian Learning**: "Neurons that fire together, wire together" — memories recalled together form edges
- **Activation Spreading**: Graph traversal discovers related memories via edge propagation
- **Semantic Gating**: Cosine similarity filter prevents noise edges (threshold: 0.5)

### Design Principle: explore-only

Neural Memory graph is used exclusively by `explore()` for related memory discovery. It does **not** modify `recall()` search scores.

**Why**: When graph signal was mixed into recall scoring (via UnifiedScorer), P@1 degraded from 88% to 79% after 5 epochs due to unbounded aggregate activation mass. Separating recall and explore eliminates this degradation entirely.

## Benchmark Results

### Recall Stability (137 memories, 113 queries)

| Epoch | P@1 | Hit@3 | Hit@5 |
|-------|-----|-------|-------|
| 1-7 | **88%** | 94% | 94% |

Zero degradation across 7 epochs.

### explore() Precision

| Corpus size | explore P@5 | recall P@5 | Unique value |
|-------------|-------------|------------|-------------|
| 137 memories | 21.3% | 25.0% | 2.1% |
| 487 memories | **57.8%** | 56.2% | — |

explore precision scales with corpus size. At 487 memories, graph traversal becomes competitive with semantic search.

### Parameter Optimization

#### Semantic Gating Threshold

| Threshold | Noise edges (sim 0.3-0.5) | explore P@5 |
|-----------|--------------------------|-------------|
| none | 57% | 20.2% |
| 0.4 | 52% | — |
| **0.5** | **2.5%** | **21.3%** |
| 0.6 | ~0% | — |

Threshold 0.5 eliminates the problematic 0.3-0.5 band while preserving useful edges.

#### Top-k Co-activation

| top_k | Edges (epoch 5) | explore P@5 |
|-------|----------------|-------------|
| **3** | **629** | **21.3%** |
| 5 | 1145 | 20.9% |
| 10 | 1659 | 19.3% |

top-k=3 produces fewest edges with highest precision. Higher k introduces noise without improving quality.

#### Beta (Graph Signal Weight)

| Epoch | beta=0.20 | beta=0.10 | beta=0.05 |
|-------|-----------|-----------|-----------|
| 4 | 87% | 88% | 88% |
| 5 | 80% | 79% | 80% |

Beta reduction delays but does not prevent degradation when graph signal is used in recall scoring. This led to the architectural decision to remove graph signal from recall entirely.

## Recommended Configuration

```
ENABLE_NEURAL_MEMORY=true
MIN_SIMILARITY_FOR_EDGE=0.5
TOP_K_COACTIVATION=3
```

## Known Limitations

1. **Positive feedback loop**: Hebbian learning is inherently self-reinforcing (Oja 1982, BCM theory). Semantic gating and top-k limits mitigate but do not eliminate this
2. **Scale dependence**: At 137 memories, explore's unique value (2.1%) is not statistically significant. Value becomes substantial at 487+ memories
3. **No negative feedback**: The system only strengthens edges (positive signal from co-activation). There is no mechanism to weaken edges when memories are retrieved but not used
4. **Benchmark limitations**: Same 113 queries repeated across epochs; real usage involves diverse queries

## Future Directions

- **Anti-Hebbian learning**: Weaken edges for memories recalled but not referenced (Contrastive Hebbian Learning)
- **Edge weight normalization**: Softmax-normalize outgoing weights per node to structurally prevent unbounded accumulation
- **Query-dependent attention**: Replace uniform spread_decay with query-aware attention weights
- **Large-scale validation**: 1000+ memory benchmarks to confirm scaling properties

## Related

- [Search Quality Benchmark](search-quality-benchmark.md) — Hybrid search baseline
- [Core Concepts: Neural Memory](concepts.md#neural-memory) — Architecture overview
- [PR #121](https://github.com/kagura-ai/memory-cloud/pull/121) — Implementation
- [SDK #76](https://github.com/kagura-ai/kagura-memory-python-sdk/issues/76) — Degradation benchmark data
- [SDK #77](https://github.com/kagura-ai/kagura-memory-python-sdk/issues/77) — explore() benchmark data
