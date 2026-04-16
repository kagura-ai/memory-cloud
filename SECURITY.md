# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT open a public GitHub issue**
2. Use [GitHub Security Advisories](https://github.com/kagura-ai/memory-cloud/security/advisories/new) to report privately
3. Or contact: https://github.com/JFK

We aim to acknowledge reports within 48 hours and provide a fix within 7 days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest release | ✅ |
| Previous minor | ✅ (security fixes only) |
| Older | ❌ |

## Security Design

### Authentication

- **OAuth2** (Google, GitHub) for user login
- **API Keys** for programmatic access (SHA-256 hashed, Fernet encrypted at rest)
- **JWT** for session tokens (configurable expiry, HS256)
- **HttpOnly cookies** for session storage

### Authorization (RBAC)

Two-level role-based access control:

- **Workspace level**: Owner > Admin > Member > Viewer
- **Context level**: Owner > Editor > Viewer
- All API routes enforce authentication via FastAPI dependencies
- Workspace and context access validated on every request

### Data Isolation

3-level isolation ensures complete data separation:

1. **Workspace ID** — organization boundary
2. **Context ID** — project/topic boundary
3. **User ID** — personal boundary (for private contexts)

All Qdrant vector searches and PostgreSQL queries include isolation filters.

### Secrets Management

- All secrets loaded from environment variables (never hardcoded)
- API keys encrypted with Fernet (AES-128-CBC + HMAC-SHA256)
- API key plaintext never stored — only SHA-256 hash for lookup
- `.env` files excluded from git via `.gitignore`

### Rate Limiting

- Per-user rate limiting via Redis
- Tier-based limits (configurable per plan)
- Per-endpoint overrides for sensitive routes (auth, API key operations)
- Fail-open design (Redis failure doesn't block requests)

### Input Validation

- All SQL queries use SQLAlchemy ORM or parameterized `text()` — no f-string SQL
- Context names validated against `^[a-z0-9_-]+$`
- Request body validation via Pydantic models
- UUID format validation on all ID parameters

## Security Advisories

### 2026-04-14 — Cross-tenant Resource ingest (fixed in v0.12.0)

**Severity**: Critical (OWASP A01: Broken Access Control / CWE-639: Authorization Bypass Through User-Controlled Key)
**Affected versions**: all versions before v0.12.0 with Resource Ingest enabled (Issue #238 onward).
**Fixed in**: v0.12.0
**Discovered during**: internal design audit (#322 parent epic #321).
**Recommendation**: All self-hosted operators should upgrade to v0.12.0 as soon as possible. See [`docs/resource-foundation-migration.md`](docs/resource-foundation-migration.md) for the step-by-step migration guide.

#### Description

The Resource Ingest API (`POST /api/v1/resources/{resource_id}/events`) authenticated tokens by `(token_hash, resource_id)` only and never verified that the token's creator was a member of the workspace whose Context owned that `resource_id`. Because `contexts.resource_id` had no global uniqueness constraint, two workspaces could legitimately create Contexts with the same `resource_id` string. An authenticated attacker (self-signup + PRO plan) could then:

1. Create a Context in their own workspace with the same `resource_id` as a victim's Context
2. Obtain a Resource Token for that `resource_id` (the existing per-workspace CRUD check allowed this)
3. Send ingest events that the victim's indexer would consume and write into the victim's memory store

#### Remediation (v0.12.0)

1. **Ingest-path workspace boundary**: `verify_resource_token` now enforces `WorkspaceMember.user_id == ResourceToken.created_by AND WorkspaceMember.workspace_id == Context.workspace_id`. Mismatches return 403 and emit a `cross_tenant_ingest_attempt` structured warning log.
2. **Schema-level tenant isolation**: Alembic migration `a96` adds a global partial UNIQUE index `ux_contexts_resource_id_active ON contexts (resource_id) WHERE resource_id IS NOT NULL AND deleted_at IS NULL`. Cross-workspace `resource_id` collisions are now impossible at the database level.
3. **Audit logging**: Structured warnings are emitted for unbound resources, missing token attribution, and membership violations. No raw token material is ever logged — only the integer `token_id` (DB PK), the workspace UUIDs, and the request's client IP.

#### Upgrade steps for self-hosted operators

1. Before upgrading, run the collision audit query to detect any pre-existing active duplicates that would block the new UNIQUE index. Because the index is global on `resource_id` for active rows, **both same-workspace and cross-workspace duplicates** would abort the migration, so the query must match the index predicate with `COUNT(*) > 1`:
   ```sql
   SELECT resource_id,
          COUNT(*) AS active_count,
          COUNT(DISTINCT workspace_id) AS ws_count
   FROM contexts
   WHERE resource_id IS NOT NULL AND deleted_at IS NULL
   GROUP BY resource_id
   HAVING COUNT(*) > 1;
   ```
2. If rows are returned, resolve each active duplicate (`UPDATE contexts SET resource_id = ... WHERE id = ...`) before upgrading. The `a96` migration will abort if any active duplicates remain.
3. Run `make migrate` (or your standard Alembic upgrade step). `a96` uses `CREATE UNIQUE INDEX CONCURRENTLY` and does not hold a table lock.
4. Restart API containers to pick up the updated `verify_resource_token` dependency.
5. Monitor logs for `cross_tenant_ingest_attempt` warnings — ongoing hits indicate either active exploit attempts or legitimate callers whose token attribution needs review.
