---
description: Save new knowledge, patterns, or learnings to Kagura Memory Cloud
---

Save new knowledge, patterns, or learnings to Kagura Memory Cloud.

Save the following to memory: $ARGUMENTS

## Steps

### 1. Resolve the target context

```
list_contexts()
```

If only one context exists, use it. If multiple, pick the one most relevant to the current project. If unclear, ask the user.

### 2. Parse the input

- Extract a clear summary (first sentence or line, 10-500 chars)
- Determine the appropriate type:
  - `pattern`: Implementation patterns, code examples
  - `troubleshooting`: Error fixes, debugging solutions
  - `decision`: Design decisions, architecture choices
  - `learning`: General learnings
  - `bug-fix`: Bug fix details
- Set importance based on impact (default: 0.8, design decisions: 0.9, core principles: 1.0)
- Generate relevant tags (technology, domain, feature area)

### 3. Save

Use `remember` with the resolved context_id, parsed summary, content with details, and appropriate type/importance/tags.

Include `context_summary` to explain why this memory matters and how to use it (max 2000 chars). This field helps future recall understand the memory's purpose without reading the full content.

```
remember(
  context_id=...,
  summary="...",
  content="...",
  type="decision",
  importance=0.9,
  tags=["auth", "architecture"],
  context_summary="Why this matters and when to reference it."
)
```

**External source tracking** — when saving knowledge from a specific file, URL, or vault, set `source_uri` and `source_type` for traceability:

```
remember(
  context_id=...,
  summary="...",
  content="...",
  type="pattern",
  importance=0.8,
  tags=["obsidian", "architecture"],
  context_summary="...",
  source_uri="vault://my-vault/architecture/decisions.md",
  source_type="vault"
)
```

- `source_uri`: Origin URI (e.g. `file:///path/to/note.md`, `vault://my-vault/note`, `https://example.com/page`). Max 2048 chars.
- `source_type`: `"file"` | `"url"` | `"vault"` | `"api"` | `"manual"`

**Explicit linking** — connect related memories at creation time using `linked_memory_ids` or `linked_source_uris`:

```
remember(
  context_id=...,
  summary="...",
  content="...",
  type="decision",
  importance=0.9,
  tags=["auth"],
  context_summary="...",
  linked_memory_ids=["<existing-memory-uuid>"],
  linked_source_uris=["vault://my-vault/related-note.md"]
)
```

- `linked_memory_ids`: Creates `declared_link` edges (weight 1.0) to existing memories by ID. Use for known relationships like resolved `[[wikilinks]]`.
- `linked_source_uris`: Links by source_uri — resolved to memory_id at remember time. Unresolved URIs are silently skipped (the plugin can retry later when the target memory exists).

### 4. Confirm

Show what was saved: summary, type, importance, tags.
