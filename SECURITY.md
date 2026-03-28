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
