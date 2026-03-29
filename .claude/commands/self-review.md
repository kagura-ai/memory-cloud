Perform a pre-PR self-review of all changes.

You are an AI code reviewer reviewing your own diff before pushing. Focus on high-signal feedback — bugs, security, correctness. Skip formatting (linters handle that). Only comment on changed lines unless unchanged context is needed.

## Severity scale

| Level | Marker | Action |
|-------|--------|--------|
| Critical | `[C]` | Must fix before PR — security hole, data loss, crash |
| Warning | `[W]` | Should fix — bug risk, missing guard, poor practice |
| Info | `[I]` | Fix or justify — style, readability, minor improvement |

## Mindset

- **Hunt for problems** — don't confirm "it works", prove it can break
- **Each line**: ask "what if null/empty/large/concurrent/malicious?"
- If no problem: explain **why** it's safe
- If problem found: provide the **fix**, not just the complaint
- Prioritize high-signal feedback over quantity

## Steps

### 1. Get the diff

```bash
git diff main...HEAD
```

### 2. Correctness (highest priority)

For each changed function/block:
- **Edge cases**: null, empty string, zero, negative, max-length, Unicode, empty list
- **Error paths**: what happens when DB fails, API times out, permission denied?
- **Logic errors**: off-by-one, wrong operator, inverted condition, missing await
- **State corruption**: can partial failure leave DB/cache/Qdrant inconsistent?
- **Concurrency**: race conditions on shared resources (DB rows, files, cache keys)

### 3. Design principles

- **DRY**: Same logic duplicated across multiple places? Should it be shared?
- **KISS**: Unnecessarily complex? Simpler approach available?
- **SOLID**:
  - **S**: Each function/class has exactly one responsibility?
  - **O**: Can existing code be extended without modification?
  - **D**: Direct dependencies on concrete classes? (Use FastAPI `Depends`)
- **YAGNI**: Features ahead of actual need? Over-engineering?

### 4. Security

**Critical (must grep):**
```bash
git diff main...HEAD | grep -n 'f"SELECT\|f"INSERT\|f"UPDATE\|f"DELETE\|f"DROP'
git diff main...HEAD | grep -n '@router\.\(get\|post\|put\|patch\|delete\)'
git diff main...HEAD | grep -ni 'api_key\s*=\s*"\|password\s*=\s*"\|secret\s*=\s*"'
```
- SQL injection: f-string SQL (must use SQLAlchemy ORM/text())
- Auth gaps: new routes missing auth dependency
- Secrets: hardcoded keys, tokens, passwords
- RBAC: workspace/context permission checks present
- Input validation on new endpoints (Pydantic, length limits)
- Error responses don't leak internals (stack traces, file paths)
- MCP tool handlers: validate all args before DB operations

### 5. DB & Migrations

- **Transactions**: multi-table writes in transaction? Partial failure safe?
- **Migration**: has downgrade()? Idempotent? NOT NULL needs server_default?
- **N+1 queries**: DB queries inside loops?
- **Session lifecycle**: rollback on error? Session not shared across tool calls?

### 6. Integration paths

- **New files**: have unit tests? Error paths tested?
- **Modified functions**: check ALL callers (including early-return paths, error handlers)
- **Data flow**: input → normalize → store → search → output (same transforms at each step?)
- **Idempotent init**: new indexes/tables handle "already exists" case?
- **Schema changes**: all write paths populate new fields? Read paths handle absence?
- **Constraint references**: any ON CONFLICT ON CONSTRAINT referencing a constraint that exists in model AND migration?

### 7. Breaking changes

- API response shape changed? Fields removed/renamed?
- Required parameter added to existing endpoint?
- Enum value removed (existing DB data may reference it)?
- New env vars added → `.env.example` updated?

### 8. Performance

- Large datasets in arrays? (stream/paginate instead)
- External API calls inside loops?
- New blocking work on startup or per-request hot path?

### 9. Frontend (if changed)

- **Dark mode**: `dark:` prefixes on new elements?
- **i18n**: `useTranslations()` not hardcoded strings?
- **Responsive**: `sm:`, `md:` breakpoints where needed?
- **Loading/Error states**: data fetching has spinner and error display?
- **Accessibility**: interactive elements have `aria-label`?

### 10. Standards

```bash
git diff main...HEAD | grep -n '^\+.*\(print(\|console\.log\)'
```
- No print/console.log
- Backend: snake_case, type hints, structlog logger
- Frontend: PascalCase, no `any` type

### 11. Dependencies (if changed)

- License compatibility (MIT/Apache OK, GPL needs review)
- Actively maintained? (last release < 1 year)
- Version pinned appropriately?

### 12. Testing

- Sufficient coverage for new code?
- Edge cases covered?
- Existing tests pass? (`make test-unit`)

### 13. Excessive changes

- Changes minimal and focused?
- No "while I'm here" refactoring of unrelated code?
- No unused abstractions?

## Output format

```markdown
## Self-review: [branch-name]

### Files: X changed

### Findings

`[C]` file:line — Problem. **Fix:** solution.
`[W]` file:line — Problem. **Fix:** solution.
`[I]` file:line — Observation.

(If none: "No issues found.")

### Summary

| Check | Status |
|-------|--------|
| Correctness | ✅ / ⚠️ / ❌ |
| Design principles | ✅ / ⚠️ / ❌ |
| Security | ✅ / ⚠️ / ❌ |
| DB & Migrations | ✅ / ⚠️ / ❌ |
| Integration paths | ✅ / ⚠️ / ❌ |
| Breaking changes | ✅ / ⚠️ / ❌ |
| Performance | ✅ / ⚠️ / ❌ |
| Frontend | ✅ / ⚠️ / ❌ / N/A |
| Standards | ✅ / ⚠️ / ❌ |
| Dependencies | ✅ / ⚠️ / ❌ / N/A |
| Testing | ✅ / ⚠️ / ❌ |

### Verdict: Ready / Needs fixes (X critical, Y warnings)
```
