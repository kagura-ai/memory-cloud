"""Memory Analysis pipeline (Issue #495 / umbrella #493).

B2 of the v0.15.0 atomic split. The pipeline pulls existing Qdrant
embeddings, clusters them on **high-dimensional** vectors (NOT the
UMAP-2D output), projects to 2D for visualization only, labels each
cluster via workspace-bound BYOK LLM calls, aggregates per-cluster
property statistics, and persists everything as a snapshot in a
single atomic transaction across ``memory_analyses`` /
``memory_analysis_clusters`` / ``memory_analysis_assignments`` /
``sleep_reports`` (with ``source='analysis'`` / ``paid_by='byok'``)
/ ``sleep_report_llm_usage`` (``phase='cluster_labeling'``).

Stage layout — one file per concern, mirroring ``services/sleep/``:

- ``byok_resolver``  Stage [A.5] BYOK key existence pre-flight
- ``preview``        Stage [A] cost estimate (called from API in #496)
- ``vector_pull``    Stage [C] Qdrant scroll + embedding homogeneity
- ``clusterer``      Stage [D] KMeans on high-dim embeddings
- ``projector``      Stage [E] UMAP for visualization only
- ``labeler``        Stage [G] LLM cluster labeling via ``llm_caller``
- ``llm_caller``     Within-OpenAI fallback + 502 wrap + frozenset guard
- ``property_stats`` Stage [H] tag/type/importance/time histograms
- ``prompts``        LLM prompt strings for cluster labeling
- ``reporter``       Stage [J] all-or-nothing transactional persist
- ``orchestrator``   Pipeline coordinator (Stage [B] through [K])

Phase 3 clarifications (2026-05-02): default model is ``gpt-5-nano``
with within-OpenAI fallback to ``gpt-5.5``. Gemini 2.5 Flash Lite is
deferred to v1.5 because ``LLMService`` does not yet have a Gemini
provider path. Idempotency is run-level (status='running' in
``memory_analyses`` for the same (workspace, context) raises 409).
"""
