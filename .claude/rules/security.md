---
paths:
  - "backend/**"
---

# Security Rules

## Authentication
- All API routes MUST use auth dependency (except `/`, `/health`, `/.well-known/*`, `/docs`, `/openapi.json`)
- Session-only routes: `Depends(get_current_user)`
- Session + API key routes: `Depends(APIKeyOrSessionUser)`
- Admin routes: verify `system_admin` or `workspace_admin` role after auth

## Authorization (RBAC)
- Always check workspace access: `PermissionService.check_workspace_access()`
- Context-level checks for memory operations
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
