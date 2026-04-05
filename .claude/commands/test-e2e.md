Run E2E browser tests using Playwright.

Requires Docker services to be running (frontend + API + DB).

## Arguments

- `$ARGUMENTS` — optional: specific test file or class (e.g., `test_search_settings`, `TestStickySaveBar`)

## Steps

1. Verify Docker services are running:
   ```
   docker compose ps --format '{{.Name}} {{.Status}}' | grep -E 'kagura-(web|api)' | head -5
   ```
   If services are not running, tell the user to run `/docker` first.

2. Verify frontend and API are reachable:
   ```
   curl -sf http://localhost:3000 -o /dev/null && echo "frontend: ok" || echo "frontend: unreachable"
   curl -sf http://localhost:8080/api/v1/system/health -o /dev/null && echo "api: ok" || echo "api: unreachable"
   ```

3. Run E2E tests:
   - If `$ARGUMENTS` is provided:
     ```
     cd backend && python -m pytest tests/e2e/ -m e2e --no-cov -v -k "$ARGUMENTS" --timeout=120
     ```
   - If no arguments:
     ```
     cd backend && python -m pytest tests/e2e/ -m e2e --no-cov -v --timeout=120
     ```

4. Report results:
   - Total tests passed/failed
   - Any failing test names with error summaries
   - If tests fail, take a screenshot suggestion: re-run with `--screenshot on` for debugging
