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

`list_contexts()` returns a slim name→id directory (`id`, `name`, `is_private`, `is_locked`, `last_used_at` — no summaries), most recently used first. If you already know the context name, narrow it with `list_contexts(name_contains="...")`; if you already resolved the id earlier in this session, reuse it instead of listing again.

If only one context exists, use it. If multiple, pick the one whose name best matches the current project. When names alone don't settle it, call `get_context_info(context_id=...)` for the candidate only — do not load details for every context. If still unclear, ask the user.

### 2. Parse the input

- Extract a clear summary (first sentence or line, 10-500 chars)
- Determine the appropriate type. The `type` field is a free-form string, but standardize on this vocabulary so `recall(filters={"type": ...})` keeps working:
  - `decision`: Design decisions, architecture choices, rejected alternatives with WHY
  - `pattern`: Implementation patterns, reusable approaches, code examples
  - `bug-fix`: Bug fix details, root-cause notes
  - `troubleshooting`: Error fixes, workarounds, environment-specific gotchas
  - `learning`: General learnings, benchmark results, tool limitations
  - `note`: Status updates, milestone notes, roadmap changes
- Set importance based on impact (default: 0.8, design decisions: 0.9, core principles: 1.0)
- Generate relevant tags (technology, domain, feature area). **Call `list_tags(context_id=...)` first** to discover existing tag spellings so you reuse them instead of inventing drift (e.g. `troubleshoot` vs `troubleshooting`).

**Write for recall.** The summary is what search matches, so write the reusable conclusion, not the process, with the terms a later search would use — best at 100-250 characters.

- Good: "JWT expiry caused 401. Fixed with refresh token rotation and clock skew handling."
- Bad: "Discussed auth errors in today's meeting." / "JSONB index optimization" (too narrow — it will not match "database performance")

Split long material (over ~2,000 characters) into one memory per topic — "OAuth2 login implementation", "JWT token validation logic" — never "part 1/3", and link the pieces with shared tags. If the new memory replaces an earlier one, pass `supersedes=<old_memory_id>` instead of storing a near-duplicate. Never store secrets, credentials or PII; coordinates go in `details.location` only.

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

**Tool guardrails** — when a troubleshooting memory is about one specific tool call, mark it with `details.tool_trigger` so a client hook or the server digest can deliver it at the matching call (full contract: `docs/mcp-tools.md#tool-guardrails`):

```
remember(
  context_id=...,
  summary="Remove the worktree before `gh pr merge --delete-branch`; the merge succeeds but the command exits 1 when the branch is checked out in a worktree.",
  type="troubleshooting",
  importance=0.8,
  tags=["git", "worktree"],
  details={"tool_trigger": {"tool": "Bash|PowerShell", "match": "gh pr merge\\b.*--delete-branch", "action": "inform"}}
)
```

- One specific tool call per guardrail: `Bash|PowerShell` for shell traps, `mcp__.*__remember` for a write-tool trap, `Edit|Write` for a file trap. `tool` is a full match on the tool name; `match` is an unanchored regex over the command, the file path, or the compact JSON of the arguments (`on: "result"` matches the tool's output or error instead).
- Write the `summary` as the safe alternative, stated as a fact — it is the text the model reads.
- Use `action: "block"` only when the call itself does the damage (it hangs, destroys, or is irreversible); it needs `on: "pre"` and a `match` naming at least one literal character. Otherwise `inform`.
- Test the pattern against the command that actually failed before storing it; the server accepts a safe regex subset only and reports the offending construct as `invalid details.tool_trigger: <code>`.
- `details` is replaced wholesale on `update_memory` — resend `tool_trigger` when you update details. Marking, changing or deleting a guardrail needs the context editor role and a user API key.
- Keep a context at 20 or fewer tool guardrails.

### 4. Confirm

Show what was saved: summary, type, importance, tags. If the response carries a `lint` key, the write will recall badly (short / long / narrative summary, no tags, or a tag that near-duplicates an existing one) — apply the hint with `update_memory`. The memory is saved either way: `scope="working"` names its consolidation lifecycle, not whether the write landed.
