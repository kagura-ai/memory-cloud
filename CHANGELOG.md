# Changelog

Release notes are published on [GitHub Releases](https://github.com/kagura-ai/memory-cloud/releases).

## [v0.12.0](https://github.com/kagura-ai/memory-cloud/releases/tag/v0.12.0) — 2026-04-16

### Highlights

The Resource Foundation refactor (epic #321) normalizes the resource subsystem around a new `resources` entity with UUID primary keys, enforces cross-tenant isolation at both the database and API layers, and maintains full backward compatibility for all external API contracts (REST and MCP). See [`docs/resource-foundation-migration.md`](docs/resource-foundation-migration.md) for the self-hosted migration guide.

### Security
- **Critical (OWASP A01 / CWE-639)**: Fixed cross-tenant memory injection via Resource Ingest API (#322, parent epic #321). All self-hosted deployments with Resource Ingest enabled must upgrade. See `SECURITY.md` for the advisory and upgrade steps.

### Added
- **Resources entity** (#323): New `resources` table (UUID PK, workspace FK) as the authoritative source of truth for resource identity. Satellite tables (`resource_events`, `resource_schemas`, `indexer_state`, `resource_tokens`) gain `resource_pk` UUID FK columns with backfill from existing data.
- **API compatibility shim** (#326): `PermissionService.resolve_resource_by_slug()` translates incoming `resource_id` slugs to internal UUIDs with workspace boundary enforcement. External API URLs remain unchanged.
- **Resource Indexer API** (#326): `GET /api/v1/resources/{resource_id}/indexer-status` endpoint for monitoring indexer progress per resource.

### Fixed
- **Resource indexer per-context EmbeddingService selection** (#338): After #334 routed per-context Qdrant collections, `_apply_upsert` still called `EmbeddingService` with the global default model, so contexts using e.g. `qwen3-embedding:8b` (4096 dim) had upserts rejected on dim mismatch against the 4096-dim collection. The per-batch routing helper now resolves `(collection_name, embedding_service)` from a single `ContextSearchConfig` fetch (`_resolve_routing_for_context`) so both values come from the same config. Falls back to the legacy `kagura_memories` collection + the indexer's default `EmbeddingService` when no `ContextSearchConfig` row exists — matching `memory_service` exactly, so both services keep reading/writing the same collection for legacy contexts. Operators overriding `settings.embedding_model` must create a `ContextSearchConfig` row per context to opt into the per-context routing path.
- **Resource indexer per-context collection routing** (#334): `resource_indexer` was hardcoding the legacy `kagura_memories` (512-dim) collection at every Qdrant call site, so any context configured with a non-default embedding model (e.g. `qwen3-embedding:8b` → 4096 dim) would have its upserts rejected by Qdrant on dim mismatch. The indexer now resolves the per-context collection via `get_collection_name(model, dimensions)` from `ContextSearchConfig`, mirroring the existing `memory_service` pattern. Falls back to legacy `kagura_memories` when no `ContextSearchConfig` row exists. Layer C (per-context `EmbeddingService` selection) is addressed by #338.
- **Resource indexer Qdrant upsert** (#324): Resource ingest events were silently failing at the Qdrant write path with `"Not existing vector name error"` because the indexer upserted anonymous vectors into a named-vector collection (`dense` + sparse `bm25`). Fixed by switching to `PointStruct(vector={"dense": embedding})` and unmasked a latent `Memory(collection_name=...)` kwarg error left over from the Single Collection Migration. See `docs/ops/resource-indexer-backfill.md` for re-queue procedure for stuck `indexer_state` rows.

### Database
- Added migration `a96`: global partial UNIQUE index `ux_contexts_resource_id_active` on `contexts(resource_id)` for active rows. Zero-downtime (`CREATE INDEX CONCURRENTLY`). Aborts if pre-existing cross-workspace `resource_id` collisions are detected — operators must resolve duplicates before upgrading.
- Added migration `a97`: `resources` entity table, `resource_pk` UUID FK on satellite tables with backfill, `resource_tokens.workspace_id` shadow FK, and three partial UNIQUE indexes. Includes pre-flight audits for orphan rows and cross-workspace ambiguity.

### Documentation
- Added `docs/resource-foundation-migration.md`: self-hosted migration guide with prerequisites, step-by-step instructions, rollback procedures, and API compatibility details.
- Updated `SECURITY.md`: strengthened upgrade recommendation for self-hosted operators.

### Superseded issues
- #318 (resource_events IntegrityError dispatch) — fixed in PR #346
- #319 (resource indexer per-context collection routing) — fixed in PR #342
- #320 (resource indexer per-context EmbeddingService selection) — fixed in PR #342

## [v0.11.1](https://github.com/kagura-ai/memory-cloud/releases/tag/v0.11.1) — 2026-04-12
