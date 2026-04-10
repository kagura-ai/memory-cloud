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

### 4. Confirm

Show what was saved: summary, type, importance, tags.
