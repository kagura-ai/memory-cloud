# collection_name Field Deprecation Plan

## Status: DEPRECATED (as of v1.0.0)

## Timeline

### Phase 1: Migration to org_id/context_id (v1.0.0) ✅ COMPLETE
- **Migrations**: 062 (edges), 063 (memories)
- **Changes**: Added org_id and context_id columns to both tables
- **Data Migration**: Automatic population from collection_name
- **Code**: All functions now use org_id/context_id (collection_name params removed)

### Phase 2: Soft Deprecation (v1.1.0) - PLANNED
- **Warning**: Log warnings when collection_name is accessed
- **Documentation**: Mark as deprecated in schema comments
- **Migration 064**: Add deprecation notice to column

```sql
COMMENT ON COLUMN memories.collection_name IS
'DEPRECATED: Use org_id and context_id instead. Will be removed in v2.0.0';
```

### Phase 3: Hard Removal (v2.0.0) - PLANNED
- **Migration 065**: Drop collection_name columns
- **Breaking Change**: Old code using collection_name will fail

```sql
ALTER TABLE memories DROP COLUMN collection_name;
ALTER TABLE neural_memory_edges DROP COLUMN collection_name;
```

---

## Rationale

### Why Deprecate?

**Old Design** (Multi-collection):
```
Collection Name: kagura_org_{org_id}_context_{context_name}
- Parsing required to extract org_id/context
- String-based filtering (slower)
- Fragile (typos, format changes)
```

**New Design** (Single collection):
```
Payload Fields: org_id, context_id, user_id (UUIDs)
- Direct filtering (faster)
- Indexed UUIDs (better performance)
- Type-safe
```

### Performance Comparison

| Operation | Old (collection_name) | New (org_id/context_id) |
|-----------|----------------------|-------------------------|
| Parse org_id | O(n) string ops | O(1) direct access |
| Filter memories | String LIKE | UUID = (indexed) |
| Index performance | String index | UUID index (better) |

---

## Migration Impact

### Affected Tables
- ✅ `memories.collection_name` - Nullable, will be dropped in v2.0.0
- ✅ `neural_memory_edges.collection_name` - Nullable, will be dropped in v2.0.0

### Backward Compatibility (v1.0.0 - v1.x.x)
- collection_name column **remains** but is **not used**
- Data migration populates org_id/context_id automatically
- No code changes needed for existing deployments

### Breaking Changes (v2.0.0)
- collection_name column will be **dropped**
- Queries using collection_name will **fail**
- Must upgrade to org_id/context_id before v2.0.0

---

## Developer Guide

### Before (DEPRECATED)
```python
# ❌ Old way (don't use)
collection_name = f"kagura_org_{org_id}_context_{context_name}"
memories = await db.execute(
    select(Memory).where(Memory.collection_name == collection_name)
)
```

### After (CURRENT)
```python
# ✅ New way (use this)
memories = await db.execute(
    select(Memory).where(
        Memory.org_id == org_id,
        Memory.context_id == context_id,
    )
)
```

---

## Rollback Plan (Emergency)

If v1.0.0 deployment fails:

1. **Revert code** to previous version
2. **Rollback migrations** 062 and 063
3. **Restore Qdrant** snapshot (multi-collection design)

```bash
# Rollback SQL
psql -f migrations/063_add_org_context_to_memories_rollback.sql
psql -f migrations/062_add_org_context_to_edges_rollback.sql
```

---

## References

- **Issue**: Qdrant Single Collection Migration
- **Migrations**: 062, 063
- **Related Docs**: CLAUDE.md, README.md
