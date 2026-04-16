# Resource Foundation Migration Guide (v0.12.0)

This guide covers the v0.12.0 Resource Foundation migration for **self-hosted operators**. The migration introduces a normalized `resources` entity with UUID primary keys, enforces cross-tenant isolation at the schema level, and maintains full backward compatibility for all external API contracts.

## What changes

| Layer | Before (pre-v0.12.0) | After (v0.12.0) |
|-------|----------------------|------------------|
| **Internal data model** | Satellite tables (`resource_events`, `resource_schemas`, `indexer_state`, `resource_tokens`) keyed by `resource_id` VARCHAR only | New `resources` table (UUID PK). Satellites gain `resource_pk` UUID FK pointing to `resources.id` |
| **External API** | `POST /api/v1/resources/{resource_id}/events` accepts slug | **No change** — slug accepted as before |
| **MCP tools** | `resource_id` string parameter | **No change** — string parameter accepted as before |
| **Tenant isolation** | No global uniqueness on `resource_id`; cross-tenant ingest possible (CWE-639) | Global partial UNIQUE index; workspace boundary enforced at DB + API layers |

### Why the slug is preserved

External consumers (CI pipelines, MCP clients, Resource Tokens) address resources by their human-readable slug (`my-github-repo`, `jira-project-x`). Forcing a UUID migration on every external caller would break existing integrations with no user-facing benefit. Instead, the API shim layer (`PermissionService.resolve_resource_by_slug`) translates the incoming slug + authenticated workspace into the internal UUID, and all internal relationships travel through the UUID primary key.

## Prerequisites

### 1. Check your data size

Estimated downtime depends on the number of rows in satellite tables:

| Dataset size | Approximate rows (across all satellite tables) | Expected duration |
|-------------|------------------------------------------------|-------------------|
| Small | up to ~10,000 | < 1 minute |
| Medium | ~10,000 to ~100,000 | 1 to 5 minutes |
| Large | > 100,000 | 5+ minutes (depends on hardware) |

The `CREATE INDEX CONCURRENTLY` steps (on `resource_events`) do **not** block reads or writes, but the transactional DDL portion (table creation, column adds, backfills) runs inside a transaction that holds locks for the duration of the backfill UPDATE statements.

### 2. Run the collision audit

Migration `a96` adds a global partial UNIQUE index on `contexts.resource_id` for active rows. If any active contexts share the same `resource_id` — whether within the same workspace or across workspaces — the migration will abort.

Run this query **before** upgrading:

```sql
SELECT resource_id,
       COUNT(*) AS active_count,
       COUNT(DISTINCT workspace_id) AS ws_count
FROM contexts
WHERE resource_id IS NOT NULL AND deleted_at IS NULL
GROUP BY resource_id
HAVING COUNT(*) > 1;
```

**If rows are returned**: resolve each duplicate by renaming or removing the conflicting `resource_id` values:

```sql
UPDATE contexts
SET resource_id = '<new-unique-slug>'
WHERE id = '<context-uuid-to-rename>';
```

### 3. Run the orphan audit

Migration `a97` backfills `resource_pk` on satellite tables by joining on `resource_id`. Satellite rows that reference a `resource_id` with no matching active context would be left with `resource_pk = NULL` permanently.

```sql
-- Check each satellite table for orphaned resource_id values
SELECT 'resource_events' AS table_name, resource_id, COUNT(*) AS row_count
FROM resource_events re
LEFT JOIN contexts c ON c.resource_id = re.resource_id
    AND c.deleted_at IS NULL
WHERE c.id IS NULL
GROUP BY resource_id

UNION ALL

SELECT 'resource_schemas', resource_id, COUNT(*)
FROM resource_schemas rs
LEFT JOIN contexts c ON c.resource_id = rs.resource_id
    AND c.deleted_at IS NULL
WHERE c.id IS NULL
GROUP BY resource_id

UNION ALL

SELECT 'indexer_state', resource_id, COUNT(*)
FROM indexer_state ist
LEFT JOIN contexts c ON c.resource_id = ist.resource_id
    AND c.deleted_at IS NULL
WHERE c.id IS NULL
GROUP BY resource_id

UNION ALL

SELECT 'resource_tokens', resource_id, COUNT(*)
FROM resource_tokens rt
LEFT JOIN contexts c ON c.resource_id = rt.resource_id
    AND c.deleted_at IS NULL
WHERE c.id IS NULL
GROUP BY resource_id;
```

**If rows are returned**: either create the corresponding active contexts first, or delete the orphaned satellite rows before upgrading.

### 4. Back up your database

```bash
pg_dump -Fc -f kagura_pre_v0120_$(date +%Y%m%d).dump "$DATABASE_URL"
```

## Migration steps

### Step 1: Pull the new version

```bash
git pull origin main  # or download the v0.12.0 release
```

### Step 2: Run migrations

```bash
make migrate
# or: alembic upgrade head
```

This executes two migrations in sequence:

1. **a96** — Adds the global partial UNIQUE index `ux_contexts_resource_id_active` on `contexts(resource_id)` for active rows. Uses `CREATE INDEX CONCURRENTLY` (zero-downtime).
2. **a97** — Creates the `resources` table, seeds it from active contexts, adds `resource_pk` FK columns on all satellite tables, backfills them, and creates three partial UNIQUE indexes.

Both migrations include pre-flight audits that abort with actionable error messages if data issues are detected (see Prerequisites above).

### Step 3: Restart API containers

```bash
docker compose restart api
# or your deployment-specific restart command
```

The updated `PermissionService.resolve_resource_by_slug` dependency and `verify_resource_token` workspace boundary enforcement take effect on restart.

### Step 4: Verify

1. **Check migration status**:
   ```bash
   alembic current
   # Should show: a97_resources_entity (head)
   ```

2. **Check the resources table was seeded**:
   ```sql
   SELECT COUNT(*) FROM resources;
   -- Should match: SELECT COUNT(DISTINCT resource_id) FROM contexts
   --               WHERE resource_id IS NOT NULL AND deleted_at IS NULL;
   ```

3. **Monitor logs** for `cross_tenant_ingest_attempt` warnings — ongoing hits indicate either active exploit attempts or legitimate callers whose token attribution needs review.

4. **Test an API call** to confirm slug acceptance is unchanged:
   ```bash
   curl -s -o /dev/null -w "%{http_code}" \
     -H "X-Resource-API-Key: <your-resource-api-key>" \
     -H "Content-Type: application/json" \
     -X POST "http://localhost:8080/api/v1/resources/<your-resource-id>/events" \
     -d '{"op": "upsert", "doc_id": "test", "version": 1, "payload": "test"}'
   # Should return 201
   ```

## API compatibility details

### Slug resolution shim

All per-resource endpoints (`/api/v1/resources/{resource_id}/...`) continue to accept the `resource_id` slug in the URL path. Internally, the shim layer resolves the slug:

1. **Input**: `resource_id` slug from URL + authenticated user
2. **Resolution**: `PermissionService.resolve_resource_by_slug()` finds the active context with this slug, verifies the caller has workspace membership
3. **Output**: internal `Context` object (with `resources.id` UUID available via FK)

**Error codes**:
- `404` — slug does not exist, or exists in a workspace the caller cannot access. The 404 (not 403) is intentional: it prevents cross-workspace existence leakage (CWE-639 / OWASP A01).
- `409` — collision detected during ingest (fail-secure behavior from the `a96` uniqueness constraint).

### MCP tools

MCP tool interfaces (`remember`, `recall`, `reference`, etc.) that accept a `resource_id` parameter continue to work with string slugs. The resolution path is identical to the REST API.

## Rollback

### Downgrading a97

Migration `a97` provides a full `downgrade()` that reverses every step:

- Drops the concurrent indexes on `resource_events`
- Drops the partial UNIQUE indexes on `resource_schemas` and `indexer_state`
- Removes `workspace_id` from `resource_tokens`
- Removes `resource_pk` from all satellite tables
- Drops the `resources` table

```bash
alembic downgrade a96_ctx_resource_id_unique
```

**Constraint**: the downgrade drops the `resource_pk` column and all data it holds. If application code has been updated to write `resource_pk` on new rows, those FK relationships are lost on downgrade. The `resource_id` VARCHAR columns remain intact, so data is still accessible through the legacy slug path.

### Downgrading a96

```bash
alembic downgrade a95_source_uri_declared_link
```

This drops the global partial UNIQUE index. **Warning**: downgrading a96 re-opens the cross-tenant ingest vulnerability. Only do this if absolutely necessary, and re-upgrade as soon as possible.

## Security context

v0.12.0 fixes a critical cross-tenant Resource ingest vulnerability (OWASP A01 / CWE-639). All self-hosted deployments with Resource Ingest enabled (any version since Issue #238) should upgrade to v0.12.0. See [SECURITY.md](../SECURITY.md#2026-04-14--cross-tenant-resource-ingest-fixed-in-v0120) for the full advisory, including the attack scenario and remediation details.

## Superseded issues

The following issues were filed during the v0.12.0 development cycle and have been addressed as part of the Resource Foundation refactor (epic #321):

| Issue | Description | Resolution |
|-------|-------------|------------|
| #318 | `resource_events` IntegrityError dispatch fix | Fixed in PR #346 — `constraint_name` attribute used for dispatch |
| #319 | Resource indexer per-context collection routing | Fixed in PR #342 (part of batch #334/#335) |
| #320 | Resource indexer per-context EmbeddingService selection | Fixed in PR #342 (part of batch #338) |

## Related documentation

- [SECURITY.md](../SECURITY.md) — Full security advisory for the cross-tenant fix
- [Resource Tokens Guide](resource-tokens-guide.md) — How to create and manage Resource Tokens
- [Resource Indexer Backfill Runbook](ops/resource-indexer-backfill.md) — Re-queue stuck indexer rows
- [API Reference](api-reference.md) — Full REST API documentation
