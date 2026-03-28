Run all quality checks on the codebase.

Steps:
1. Backend lint + format check:
   ```
   make lint
   ```
2. Backend type checking:
   ```
   make type-check
   ```
3. Frontend build check:
   ```
   cd frontend && npm run build
   ```
4. Frontend tests:
   ```
   cd frontend && npm test
   ```

Report any issues found with file paths and line numbers.
If issues are found, ask if the user wants them auto-fixed (`ruff check --fix`, `ruff format`, `make format`).
