---
paths:
  - "backend/**"
---

# Security Rules

## Authentication
- All API routes MUST use auth dependency (except `/`, `/health`, `/api/v1/system/info`, `/.well-known/*`, `/docs`, `/openapi.json`)
  - `/api/v1/system/info` is intentionally public: version + non-sensitive
    deployment feature flags the web UI reads before workspace context exists.
    It exposes no user/workspace data. Enforced public by
    `tests/api/test_system_info.py` + `tests/smoke/test_health.py`.
- Session-only routes: `Depends(get_current_user)`
- Session + API key routes: `Depends(APIKeyOrSessionUser)`
- Admin routes: verify `system_admin` or `workspace_admin` role after auth

## Authorization (RBAC)
- Always check workspace access: `PermissionService.check_workspace_access()`
- Context-level checks for memory operations
- The context resolvers (`resolve_context_for_workspace_read`, `check_context_access`,
  `check_context_write`, `check_context_owner`) take a required `capacity=CapacityPolicy`
  (#1948): `BLOCKED` for anything that reads or writes memory content (refused with
  `CAPACITY-001` after authorization, #1941), `ALLOWED` for the cleanup / metadata surface.
  Every REST route is classified in `tests/api/test_capacity_lock_route_table.py` — a new
  route must be added there (and MCP tools in `mcp_server/tools/_capacity_gate.py`)
- Never trust client-supplied workspace_id without verification

## Database Security
- NEVER use f-strings for SQL (`f"SELECT ... {user_input}"`)
- ALWAYS use SQLAlchemy ORM or `text()` with bound parameters
- ALWAYS use parameterized queries for any raw SQL

## Secrets Management
- NEVER hardcode API keys, passwords, or tokens in source code
- Use environment variables via `Settings` (pydantic-settings)
- API keys: SHA256 hash for storage, never store plaintext
- Encryption: Fernet (AES-128 CBC + HMAC) via `utils/encryption.py`

## CORS
- NEVER use wildcard origins (`allow_origins=["*"]`) in production
- Origins configured via `CORS_ORIGINS` env var (comma-separated list)

## Sessions
- HttpOnly cookies only for session tokens
- Session TTL: 7 days max
- Redis-backed session storage with proper cleanup
