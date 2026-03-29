# Search Quality Benchmark

Comprehensive search accuracy and performance benchmark for Kagura Memory Cloud, tested with the [Python SDK](https://github.com/kagura-ai/kagura-memory-python-sdk).

## Test Environment

- **SDK**: Python SDK v0.4.1
- **Server**: localhost (Docker Compose)
- **Data**: 5 domains x 12 memories = 60 memories
- **Queries**: 10 per domain = 50 queries
- **Metrics**: Top-1 accuracy, Top-3 accuracy
- **Search**: Hybrid Search (60% semantic embedding + 40% BM25 keyword)
- **Embedding**: OpenAI `text-embedding-3-small` (512 dimensions)
- **Tokenizer**: Sudachi (Japanese morphological analysis + lemmatization)

## Domain-Level Accuracy (Single Context, Rerank OFF)

| Domain | Top-1 | Top-3 |
|--------|-------|-------|
| Legal | 10/10 (100%) | 10/10 |
| Construction | 10/10 (100%) | 10/10 |
| Healthcare | 9/10 (90%) | 10/10 |
| Education | 9/10 (90%) | 10/10 |
| IT | 10/10 (100%) | 10/10 |
| **TOTAL** | **48/50 (96%)** | **49/50 (98%)** |

### Miss Cases

- "手指衛生のタイミング" (hand hygiene timing) — Construction's "安全衛生管理" (safety management) ranked 1st due to shared keywords "衛生" (hygiene) and "安全" (safety) across domains
- "教員の残業上限" (teacher overtime limits) — Legal's "労働基準法の時間外労働" (labor standards overtime) ranked 1st due to "残業" (overtime) and "上限" (limits) matching legal domain

Both misses are **cross-domain interference** — common terms appearing in multiple domains.

## Rerank Effect

Same 60 memories, 50 queries. Voyage AI `rerank-2` provider.

| Setting | Top-1 | Top-3 | Avg Latency |
|---------|-------|-------|-------------|
| Rerank OFF | 48/50 (96%) | 49/50 (98%) | 468 ms |
| Rerank ON | 48/50 (96%) | 49/50 (98%) | 667 ms (+200ms) |

**Findings:**
- On already high-accuracy data (96%), reranking provides limited precision improvement
- Reranking improved "手指衛生" from outside top-5 to rank=4 (helps ambiguous queries)
- Latency overhead: ~200ms per query

## Context Separation Effect

60 memories in 1 context vs 5 domain-specific contexts:

| Pattern | Top-1 | Top-3 |
|---------|-------|-------|
| Mixed (1 context, 60 memories) | 48/50 (96%) | 49/50 (98%) |
| Separated (5 contexts, 12 each) | **49/50 (98%)** | 49/50 (98%) |

**Improved cases:**
- "教員の残業上限" — Mixed: Legal's labor law ranked 1st / Separated: Education's work reform ranked 1st
- "手指衛生のタイミング" — Mixed: Construction's safety ranked 1st / Separated: Healthcare's hygiene ranked within top-5

**Conclusion:** Context separation eliminates cross-domain interference for terms like "safety" and "work style" that appear across multiple domains.

## Three Strategies Compared

| Strategy | Accuracy | Context Usage | Latency | Recommended When |
|----------|----------|---------------|---------|-----------------|
| Context separation | **98%** (best) | High (one per domain) | Unchanged | Context quota allows it |
| Rerank ON (single context) | 96% + ambiguous improvement | **Low (one context)** | +200ms | Saving context quota |
| Default (no separation, no rerank) | 96% | Low | Fastest | Simple use cases, low latency |

## Search Weight Tuning (update_search_config)

Same 50 queries with 3 weight configurations:

| Setting | Top-1 | Notes |
|---------|-------|-------|
| Default (semantic=0.6, bm25=0.4) | 96% | Baseline |
| Balanced (semantic=0.5, bm25=0.5) | 96% | Score shifts, rank unchanged |
| BM25-heavy (semantic=0.3, bm25=0.7) | 96% | Same |

**Conclusion:** Weight adjustment has minimal impact on ranking. **Summary quality** (keyword inclusion) is the primary driver of search accuracy.

## Summary Writing Determines Accuracy

Same query "データベースのパフォーマンス改善" (database performance improvement):

| Summary Style | Top-1 Rank |
|---------------|-----------|
| "PostgreSQLのJSONBインデックス最適化テクニック" (narrow technical) | rank=5 (miss) |
| "PostgreSQL JSONBのGINインデックスでデータベースパフォーマンス改善" (keyword-rich) | **rank=1** |

The difference: keywords "データベース" (database), "パフォーマンス" (performance), "改善" (improvement) present in the summary.

The `remember` MCP tool description includes guidance for writing search-optimized summaries. All MCP clients (Claude, ChatGPT, Gemini) automatically follow this guidance when storing memories.

## Performance (60 memories, localhost)

| Operation | Latency |
|-----------|---------|
| Remember | 243 ms |
| Recall (k=5) | 468 ms avg |
| Recall + Rerank | 667 ms avg (+200ms) |
| Recall (k=1) | 258 ms |
| Recall (k=20) | 1,204 ms |
| Reference | 8 ms |
| Explore (Neural edge) | 19 ms |
| Forget | 14 ms |

Latency increases sharply at k=20+. Recommended: k=5 to k=10.

## Neural Memory (Explore)

- 106 edges across 18 nodes confirmed
- Edge generation: co-recall 10+ times triggers Hebbian learning (min_co_activation_count=2)
- Explore depth=2: 9 related memories retrieved
- Activation range: 0.04 - 0.22
- Background processing: 188 co-activations → 90 Hebbian updates

## Best Practices

### 1. Include search keywords in summary

Write summaries with words users will search for. Be explicit.

```
Bad:  "最適化テクニック" (optimization technique)
Good: "データベースパフォーマンス改善: PostgreSQL JSONB GINインデックスでクエリ高速化"
      (database performance improvement: PostgreSQL JSONB GIN index for faster queries)
```

### 2. Separate domains into different contexts

Mixing legal, construction, healthcare memories in one context causes interference on shared terms ("safety", "compliance", "overtime"). Context separation improved Top-1 from 96% to 98%.

### 3. Use reranking when context separation isn't possible

If context quota is limited, enable reranking (`use_rerank=true`) to mitigate cross-domain interference. Trade-off: +200ms latency.

### 4. Use k=5 to k=10

k=20+ causes latency spikes (1,200ms) with diminishing accuracy returns. k=5 is sufficient for most use cases.

### 5. Prioritize summary quality over weight tuning

Adjusting semantic/BM25 weights via `update_search_config` has minimal ranking impact. Invest time in better summaries instead.
