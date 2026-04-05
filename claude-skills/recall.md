---
description: Search Kagura Memory Cloud for relevant past knowledge and patterns
---

Search Kagura Memory Cloud for relevant past knowledge and patterns.

Use the Kagura Memory Cloud MCP tools to search for: $ARGUMENTS

## Steps

### 1. Resolve the target context

```
list_contexts()
```

If only one context exists, use it. If multiple, pick the one most relevant to the current project. If unclear, ask the user.

### 2. Search

Use `recall` with the resolved context_id, query="$ARGUMENTS", k=10.

### 3. Display results

Show results in a table: memory_id, summary, type, importance, tags.

### 4. Follow up

- If relevant results found, suggest using `reference` for detailed content on the most relevant match
- If no results found, suggest broader search terms or different search_mode (keyword, semantic)
