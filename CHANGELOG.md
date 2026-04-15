# Changelog

Release notes are published on [GitHub Releases](https://github.com/kagura-ai/memory-cloud/releases).

## [Unreleased] v0.12.0 — Resource Foundation (security-driven)

### Security
- **Critical (OWASP A01 / CWE-639)**: Fixed cross-tenant memory injection via Resource Ingest API (#322, parent epic #321). All self-hosted deployments with Resource Ingest enabled must upgrade. See `SECURITY.md` for the advisory and upgrade steps.

### Fixed
- **Resource indexer Qdrant upsert** (#324): Resource ingest events were silently failing at the Qdrant write path with `"Not existing vector name error"` because the indexer upserted anonymous vectors into a named-vector collection (`dense` + sparse `bm25`). Fixed by switching to `PointStruct(vector={"dense": embedding})` and unmasked a latent `Memory(collection_name=...)` kwarg error left over from the Single Collection Migration. See `docs/ops/resource-indexer-backfill.md` for re-queue procedure for stuck `resource_indexer_state` rows.

### Database
- Added migration `a96`: global partial UNIQUE index `ux_contexts_resource_id_active` on `contexts(resource_id)` for active rows. Zero-downtime (`CREATE INDEX CONCURRENTLY`). Aborts if pre-existing cross-workspace `resource_id` collisions are detected — operators must resolve duplicates before upgrading.

## [v0.11.1](https://github.com/kagura-ai/memory-cloud/releases/tag/v0.11.1) — 2026-04-12
