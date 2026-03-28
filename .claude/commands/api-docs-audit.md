---
description: Audit OpenAPI documentation (tags, descriptions, endpoints) against actual API code
---

Audit the FastAPI OpenAPI configuration against actual router endpoints. Report inconsistencies and propose fixes.

## Audit Steps

### 1. Tag Consistency
- Read `backend/src/api/main.py` for `openapi_tags` definitions
- Read all `backend/src/api/routes/*.py` for `tags=` in APIRouter definitions
- Verify every router tag appears in `openapi_tags` (and vice versa)
- Check for duplicate tags (same tag in both `include_router` and router file)
- Verify tag naming convention is consistent (kebab-case)

### 2. Tag Descriptions
- For each tag in `openapi_tags`, verify the description matches the actual endpoints
- Check that MCP tools listed in `mcp_server/tools.py` have corresponding REST API endpoints in ReDoc
- Flag misleading or outdated descriptions

### 3. Endpoint Coverage
- List all `@router.get/post/put/delete/patch` decorators across all route files
- Verify every endpoint is tagged (not appearing under "default" in ReDoc)
- Check for orphaned endpoints that belong to no tag

### 4. Tag Ordering
- Verify `openapi_tags` ordering is logical (auth → workspace → memory → integrations → admin → system)
- Check if new route files have been added without updating `openapi_tags`

### 5. MCP ↔ REST Parity
- List all MCP tools from `mcp_server/tools.py`
- Verify each MCP tool has a corresponding REST API endpoint
- Flag MCP tools without REST equivalents (acceptable for MCP-only tools like usage_guide)

## Output Format

| # | Category | Issue | File | Fix |
|---|----------|-------|------|-----|

Categories: Tag Mismatch, Missing Tag, Duplicate Tag, Description Drift, Missing Endpoint, Ordering

After presenting findings, ask which fixes to apply.
