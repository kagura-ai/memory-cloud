Run the full test suite for the project.

Steps:
1. Run all tests (backend + basic checks):
   ```
   make test-local
   ```
2. Run frontend checks:
   ```
   cd frontend && npm run build
   ```
3. Report results:
   - Total tests passed/failed/skipped
   - Any failing test names and error summaries
   - Frontend lint/build status
