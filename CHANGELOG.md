# Changelog

Release notes are published on [GitHub Releases](https://github.com/kagura-ai/memory-cloud/releases).

## [Unreleased] v0.12.0 — Resource Foundation (security-driven)

### Security
- **Critical (OWASP A01 / CWE-639)**: Fixed cross-tenant memory injection via Resource Ingest API (#322, parent epic #321). All self-hosted deployments with Resource Ingest enabled must upgrade. See `SECURITY.md` for the advisory and upgrade steps.

### Fixed
- **Resource indexer per-context EmbeddingService selection** (#338): After #334 routed per-context Qdrant collections, `_apply_upsert` still called `EmbeddingService` with the global default model, so contexts using e.g. `qwen3-embedding:8b` (4096 dim) had upserts rejected on dim mismatch against the 4096-dim collection. The per-batch routing helper now resolves `(collection_name, embedding_service)` from a single `ContextSearchConfig` fetch (`_resolve_routing_for_context`) so both values come from the same config. Falls back to the indexer's default-model `EmbeddingService` + legacy `kagura_memories` collection when no `ContextSearchConfig` row exists.
- **Resource indexer per-context collection routing** (#334): `resource_indexer` was hardcoding the legacy `kagura_memories` (512-dim) collection at every Qdrant call site, so any context configured with a non-default embedding model (e.g. `qwen3-embedding:8b` → 4096 dim) would have its upserts rejected by Qdrant on dim mismatch. The indexer now resolves the per-context collection via `get_collection_name(model, dimensions)` from `ContextSearchConfig`, mirroring the existing `memory_service` pattern. Falls back to legacy `kagura_memories` when no `ContextSearchConfig` row exists. Layer C (per-context `EmbeddingService` selection) is addressed by #338.
- **Resource indexer Qdrant upsert** (#324): Resource ingest events were silently failing at the Qdrant write path with `"Not existing vector name error"` because the indexer upserted anonymous vectors into a named-vector collection (`dense` + sparse `bm25`). Fixed by switching to `PointStruct(vector={"dense": embedding})` and unmasked a latent `Memory(collection_name=...)` kwarg error left over from the Single Collection Migration. See `docs/ops/resource-indexer-backfill.md` for re-queue procedure for stuck `indexer_state` rows.

### Database
- Added migration `a96`: global partial UNIQUE index `ux_contexts_resource_id_active` on `contexts(resource_id)` for active rows. Zero-downtime (`CREATE INDEX CONCURRENTLY`). Aborts if pre-existing cross-workspace `resource_id` collisions are detected — operators must resolve duplicates before upgrading.

## [v0.11.1](https://github.com/kagura-ai/memory-cloud/releases/tag/v0.11.1) — 2026-04-12
