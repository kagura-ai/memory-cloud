Perform a pre-PR self-review of all changes before creating a draft PR.

## Role distinction

- **self-review** (this command): Author's pre-flight checklist. Run BEFORE pushing. You are the author re-reading your own diff with fresh eyes.
- **code-reviewer** (agent): Third-party reviewer invoked on PRs. Reads code without author context.

Both use the same severity scale so findings are directly comparable.

## Severity scale

| Level | Marker | Meaning | Action |
|-------|--------|---------|--------|
| Critical | `[C]` | Security hole, data loss, crash in production | Must fix before PR |
| Warning | `[W]` | Bug risk, missing guard, poor practice | Should fix before PR |
| Info | `[I]` | Style, readability, minor improvement | Fix or justify |

## Mindset
- **Fresh perspective**: Review as if seeing the code for the first time
- **No assumptions**: Don't assume "it works" — verify correctness
- **Hunt for problems**: Don't wait for issues to appear — actively look for them
- Each check: read the actual code, ask "what if null/empty/large?" for each line
- If no problem found, explain **why** it's safe (not just "looks fine")
- If problem found, provide the **fix** (not just the complaint)

## Steps

### 1. Review the diff
```bash
git diff main...HEAD
```

### 2. Design principles check
- **DRY** (Don't Repeat Yourself): Is the same logic duplicated across multiple places? Should it be shared?
- **KISS** (Keep It Simple, Stupid): Is the implementation unnecessarily complex? Is there a simpler approach?
- **SOLID**:
  - **S**ingle Responsibility: Does each class/function have exactly one responsibility?
  - **O**pen/Closed: Can existing code be extended without modification?
  - **L**iskov Substitution: Are inheritance relationships properly substitutable?
  - **I**nterface Segregation: Are there unnecessary dependencies on unused methods?
  - **D**ependency Inversion: Are there direct dependencies on concrete classes? (Use FastAPI `Depends`)
- **YAGNI** (You Aren't Gonna Need It): Are there features implemented ahead of actual need? Over-engineering based on hypothetical future requirements?

### 3. Security review
Check for the following issues:

**Critical:**
- **SQL injection**: f-string SQL, missing parameterized queries (must use SQLAlchemy ORM/`text()`)
  ```bash
  git diff main...HEAD | grep -n 'f"SELECT\|f"INSERT\|f"UPDATE\|f"DELETE\|f"DROP\|f'\''SELECT\|f'\''INSERT'
  ```
- **Auth gaps**: Routes missing `Depends(get_current_user)` or `Depends(APIKeyOrSessionUser)`
  ```bash
  # Find new route definitions, verify each has auth dependency
  git diff main...HEAD | grep -n '@router\.\(get\|post\|put\|patch\|delete\)'
  ```
- **Secrets**: Hardcoded API keys, passwords, or tokens
  ```bash
  git diff main...HEAD | grep -ni 'api_key\s*=\s*"\|password\s*=\s*"\|token\s*=\s*"\|secret\s*=\s*"'
  ```

**Warning:**
- **RBAC**: Proper workspace/context-level permission checks (`PermissionService`)
- **Input validation**: New API endpoints validate input (Pydantic models, length limits, format checks)
- **XSS**: Unsanitized output in frontend (outside React/Next.js default protection)
- **CORS**: `allow_origins=["*"]` leaking into production config
- **Info leakage**: Internal details (stack traces, file paths) exposed in error responses

### 4. DB & Data integrity
**Critical:**
- **Transactions**: Multi-table writes wrapped in transaction? Partial failure leaves inconsistent state?
- **Migration safety**: If Alembic migration is included:
  - Can it be rolled back? (`downgrade()` implemented and tested?)
  - `NOT NULL` added to existing column? Requires `server_default` or data backfill
  - Column/table drop? Data backed up or confirmed unused?

**Warning:**
- **N+1 queries**: DB queries inside loops? Can they be batched with `selectinload`/`joinedload`?
- **Concurrency**: Multiple users editing same record — optimistic locking (`updated_at` check) needed?
- **Logging**: Enough log output for post-incident investigation? (request ID, input, error details)

### 5. Performance & Resilience
**Warning:**
- **Memory**: Large datasets loaded into arrays unnecessarily? Can results be streamed/paginated?
- **External calls**: S3/API calls inside loops? Minimize external requests during HTTP handling
- **Timeouts**: External HTTP calls have timeout set? Unbounded DB queries don't block?
- **Error resilience**: Does the page/endpoint survive DB connection failure or external API timeout? Proper try/except with fallback?

### 6. Frontend review
**Warning:**
- **Dark mode**: New UI elements have `dark:` prefixes? No hardcoded light-only colors?
- **i18n**: New user-visible text uses `useTranslations()`, not hardcoded strings?
- **Responsive**: Layout works on mobile? Uses `sm:`, `md:` breakpoints where needed?
- **Loading/Error states**: Data fetching has loading spinner and error display?

**Info:**
- **Accessibility**: Interactive elements have `aria-label`? Keyboard navigable?
- **SWR/Data fetching**: Appropriate revalidation settings? No stale data on navigation?

### 7. Coding standards
**Warning:**
- **Backend**: snake_case (functions), PascalCase (classes), type hints required, Google docstrings
- **Frontend**: PascalCase (components), TypeScript strict, no `any` type
- No leftover `print()`, `console.log`, or debug code
  ```bash
  git diff main...HEAD | grep -n '^\+.*\(print(\|console\.log\)'
  ```
- Logger: `structlog` via `get_logger()` (not `print()`)
- async/await: No synchronous DB calls in async context (except OAuth2 server)

### 8. Breaking changes & Compatibility
**Critical:**
- **API response shape change**: Fields removed/renamed in existing endpoints? Clients will break
  ```bash
  # Check for removed/renamed response model fields
  git diff main...HEAD -- '*.py' | grep -n '^\-.*Field\|^\-.*:\s*\(str\|int\|bool\|list\|dict\|Optional\)'
  ```

**Warning:**
- **Required parameter added**: New required field on existing request model? Existing clients won't send it
- **Enum value removed**: Existing data in DB may reference the removed value
- **URL path changed**: Existing integrations/bookmarks will 404
- If DB schema changes are needed, is an Alembic migration provided?
- If new environment variables are added, is `.env.example` updated?

### 9. Dependencies
**Info:**
- New package added? Check license compatibility (MIT/Apache OK, GPL requires review)
- Version pinned appropriately? (exact pin for apps, range for libraries)
- Is the package actively maintained? (last release < 1 year)

### 10. Excessive changes check
- Are changes minimal and focused?
- No "while I'm here" refactoring
- No unnecessary renames of existing variables/functions
- No unused abstractions or helpers
- No added docstrings, comments, or type annotations to unchanged files

### 11. Integration path check
**Warning:**
- **New files**: Does every new module have unit tests? Are error paths tested?
- **Existing function modification**: Check ALL callers — including early-return paths, conditional branches, and error handlers. Don't just read the function you changed.
- **Data flow consistency**: Trace the full path: input → normalization → storage → search → output. Are the same transformations applied at each step? (e.g., if text is NFKC-normalized before storage, is the query also normalized before search?)
- **Idempotent initialization**: If adding new resources (indexes, tables, queues), does the init function handle the case where the resource already exists? Check for early-return guards that skip your new code.
- **Payload/schema changes**: If adding new fields to stored data (DB columns, Qdrant payload), do all write paths populate the field? Do all read paths handle the field being absent (for old data)?

### 12. Testing
- Is test coverage sufficient for new code?
- Are edge cases covered?
- Do existing tests pass? (`make test-local`)

## Output format

```markdown
## Self-review results

### Files changed: X files

### Findings

(List each finding as:)
`[C]` path/to/file:42 - Description of finding
  **Fix:** Suggested resolution

`[W]` path/to/file:15 - Description of finding
  **Fix:** Suggested resolution

`[I]` path/to/file:88 - Description of finding
  **Fix:** Suggested resolution

(If no findings in a category, write: No issues found.)

### Summary

| Category | Status |
|----------|--------|
| Design principles | ✅ / ⚠️ / ❌ |
| Security & validation | ✅ / ⚠️ / ❌ |
| DB & Data integrity | ✅ / ⚠️ / ❌ |
| Performance & Resilience | ✅ / ⚠️ / ❌ |
| Frontend | ✅ / ⚠️ / ❌ |
| Coding standards | ✅ / ⚠️ / ❌ |
| Breaking changes | ✅ / ⚠️ / ❌ |
| Dependencies | ✅ / ⚠️ / ❌ |
| Excessive changes | ✅ / ⚠️ / ❌ |
| Integration paths | ✅ / ⚠️ / ❌ |
| Testing | ✅ / ⚠️ / ❌ |

### Verdict: Ready for PR / Needs fixes (X critical, Y warnings)
```
