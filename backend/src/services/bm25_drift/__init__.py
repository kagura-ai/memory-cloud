"""BM25 IDF drift observability subsystem.

Issue #343: detects systematic divergence between memory-only IDF and the
collection-global IDF that Qdrant's Modifier.IDF computes. Writes one
Bm25IdfDriftLog row per context per cron cycle.
"""
