---
description: Save new knowledge, patterns, or learnings to Kagura Memory Cloud
---

Save new knowledge, patterns, or learnings to Kagura Memory Cloud.

Save the following to memory: $ARGUMENTS

## Steps

**Load the tools once.** When this client lists the kagura-memory tools as deferred (a `ToolSearch` tool is present), load every tool this skill may call in one call before step 1:
`ToolSearch(query="select:mcp__kagura-memory__remember,mcp__kagura-memory__remember_batch,mcp__kagura-memory__recall,mcp__kagura-memory__update_memory")`
— add `mcp__kagura-memory__list_contexts` and `mcp__kagura-memory__get_context_info` to that same list only when step 1 has no default context to skip to. `mcp__kagura-memory__` is the prefix the client gives the tools of the server entry named `kagura-memory`; when your deferred tool list shows another prefix for these tools, use that one. A client that lists the tools directly has no `ToolSearch`: skip this paragraph. Never call `ToolSearch` a second time for these tools.

### 1. Resolve the target context

**When the connection has a default context** — the server instructions say "Default context: <name>; omit context_id", or the session-start line from the kagura-memory plugin hook begins "Kagura Memory: default context" (either way the MCP URL carries `?context=<context-id>`, or the credential carries a default) — skip this step: call `remember` / `remember_batch` without `context_id`. The response's `context_id` / `context_name` confirm which context it is; switch with `list_contexts(name_contains="...")` only when the user asks for another one.

Otherwise:

```
list_contexts()
```

`list_contexts()` returns a slim name→id directory (`id`, `name`, `is_private`, `is_locked`, `last_used_at` — no summaries), most recently used first, 100 per page (when `has_more` is true, pass `next_cursor` as `cursor`). If you already know the context name, narrow it with `list_contexts(name_contains="...")`; if you already resolved the id earlier in this session, reuse it instead of listing again.

If only one context exists, use it. If multiple, pick the one whose name best matches the current project; a conversation about the user's own life goes to the context the user names or a private one (`is_private`), not a shared project context. When names alone don't settle it, call `get_context_info(context_id=...)` for the candidate only — do not load details for every context. If still unclear, ask the user.

### 2. Parse the input

- Extract a clear summary (first sentence or line, 10-500 chars)
- Determine the appropriate type. The `type` field is a free-form string, but standardize on this vocabulary so `recall(filters={"type": ...})` keeps working:
  - `decision`: Design decisions, architecture choices, rejected alternatives with WHY
  - `pattern`: Implementation patterns, reusable approaches, code examples
  - `bug-fix`: Bug fix details, root-cause notes
  - `troubleshooting`: Error fixes, workarounds, environment-specific gotchas
  - `learning`: General learnings, benchmark results, tool limitations
  - `note`: Status updates, milestone notes, roadmap changes
  - `fact`: A stated fact about the user, a person, a thing or a place ("The user's dog is named Max")
  - `preference`: A like, dislike, habit or opinion the user states
  - `event`: Something that happened, with when it happened ("Attended Sam's dinner party on 2023-05-12")
  - `time`: A forward-looking plan, deadline or appointment with a date. Set `details={"trigger": {"year": 2026, "month": 11, "day": 3}}` (integers, `year` required) so `recall_upcoming` surfaces it. A plan with no date is a `fact`
- Set importance based on impact (default: 0.8, design decisions: 0.9, core principles: 1.0)
- Generate relevant tags (technology, domain, feature area, `issue:#N`), about 6 at most, and write with `tags_normalize=true` (step 3): the server stores each tag under the context's established spelling (case, separators, plural, kana variants) and reports each mapping in `lint` as `tag_normalized`. Do not call `list_tags` before a write; call it only when the user asks to review the tag vocabulary. An abbreviation (`dev-env` vs `dev-environment`) is not folded — a `tag_near_duplicate` lint names the stored tag; apply it with `update_memory`, so no spelling variants of the same tag remain.

**One fact per memory, specifics verbatim.** When the material is a conversation, save first what the user said about themselves and their world — facts, preferences, events, plans, counts, amounts, prices, durations, names — each as its own memory, with every name, number, date, place and quantity exactly as stated (a per-item price and a total are two facts; keep both). Never merge several personal facts into one summary. Save the assistant's suggestions only when they are specific (a named product, a recipe, a place, a figure), not generic advice. A name or value inside a stated fact is the fact itself; contact details, home addresses, and account, card or ID numbers stay out, as do secrets and credentials. Write many items with one `remember_batch` call.

**Relative dates.** When the material places an event relative to its own date ("last weekend", "the last week of August", "two weeks ago", "yesterday", "in June"), pass the phrase as stated in `details.event_phrase` and the material's date in `observed_at`; do not compute the dates yourself. The server resolves the phrase with a fixed rule set — today, yesterday, tomorrow; tonight, this morning, this afternoon and this evening (the material date), last night (the day before), the day after tomorrow and the day before yesterday; N days, weeks, months or years ago; last, this and next weekend, week, month and year; the first or last week of a month; a month name (the whole month); a weekday name (the most recent one before the material date); this <weekday> (that day in the material date's Monday–Sunday week) and next <weekday> (that day in the week after it); N weekends ago. A week is Monday–Sunday and a weekend Saturday–Sunday, counted from the material date's weekday: "last weekend" is the Saturday–Sunday just before the material date (on a Saturday or Sunday, the weekend before the current one). It stores `details.event_start` / `details.event_end` (`YYYY-MM-DD`; a single day has equal ends), and the write response (`remember`, each `remember_batch` item, `update_memory`) returns `event_start`, `event_end` and `event_weekday`. After a single `remember`, write them into the summary beside the phrase — from material date Mon 2024-03-11, "last weekend" comes back as 2024-03-09–10, written "last weekend (2024-03-09–10)". In a `remember_batch` the summaries are written before any response exists: keep the phrase as stated and add no date; the row carries the resolved days. A date in parentheses right after the phrase that differs from the resolved days draws an `event_date_mismatch` lint (the memory is stored as written). A bare weekday is the most recent one before the material date; when it names an upcoming day ("we go hiking on Sunday"), add `details.event_direction: "future"` and it is the first such day on or after the material date (`"past"` or no key keeps the default; every other phrase ignores it). A phrase outside the rules is stored as stated and the response says `event_unresolved: "unknown_phrase"`: keep the phrase as stated and do not compute a date for it. `observed_at` stays the material's date. With no material date, leave the phrase as stated: nothing resolves without `observed_at` (`event_unresolved: "no_observed_at"`), and the server never counts from the time you write. `recall` / `list` filter on the stored days with `event_after` / `event_before`, and `list(order_by="event_start")` sorts by them.

**Store what the material states, as it states it.** Do not fact-check it, add a note that a statement is wrong or outdated, or add dates, end years or qualifiers the material does not contain — the stored value is what the user or source said, and a later reader treats the store as the authority. When the material itself corrects an earlier statement, store the correction as it is worded there.

**Updating a fact.** When the material changes a fact that may already be stored (a new owner, a new value, a moved date), search before writing: `recall(context_id=..., query="<the subject>", k=3, full=0)` (no `context_id` on a default context). When a row states the same fact with another value, write the new one with `supersedes=<that memory_id>` (or edit that row with `update_memory`). Never store the new value next to the old one without either.

**When the fact held.** If the material is dated (a session date, a document date, a meeting date), put that date in `details.observed_at` as a date only, `YYYY-MM-DD` (`"2024-03-07"`, no time or offset, so exact filters and sorting work) — the date the fact was stated, never the time you write it. Leave it out when the material has no date. `observed_at="2024-03-07"` as an argument stores the same thing. `recall` / `list` filter it with `observed_after` / `observed_before`, `list(order_by="observed_at")` sorts by it, and `list(filters={"details.observed_at": ...})` matches it exactly.

**Write for recall.** The summary is what search matches, so write the reusable conclusion, not the process, with the terms a later search would use — best at 100-250 characters. A single stated fact can be shorter (50 characters or more): name who or what it is about so it stands alone.

- Good: "JWT expiry caused 401. Fixed with refresh token rotation and clock skew handling."
- Bad: "Discussed auth errors in today's meeting." / "JSONB index optimization" (too narrow — it will not match "database performance")

Split long material (over ~2,000 characters) into one memory per topic — "OAuth2 login implementation", "JWT token validation logic" — never "part 1/3", and link the pieces with shared tags. If the new memory replaces an earlier one, pass `supersedes=<old_memory_id>` instead of storing a near-duplicate. Never store secrets, credentials or PII; coordinates go in `details.location` only.

<!-- SYNC: keep the named-speaker rules and worked example identical in the Remember section of plugins/kagura-memory/skills/kagura-memory/SKILL.md. -->

**Named speakers.** When the material is a conversation between named people, save each person's statements about themselves and their world. Each summary starts with the name of the person it is about; never "the user", "they", "he" or "she" alone. Suggestions, advice, gifts, invitations and questions record both people and the direction. Attribute each fact to the speaker who said it; a fact about the other person keeps that person as its subject and names who said it. Check each summary's subject against the speaker label of its source line, resolving who the words refer to rather than copying the turn's speaker as the subject.

**Worked example (fictional speakers).**

```text
Neri: "I started pottery. Tovan, I suggest you try swimming."
Tovan: "I gave you a sketchbook, Neri. You won the local poetry prize."
```

Store four separate summaries:

- "Neri started pottery."
- "Neri suggested Tovan try swimming."
- "Tovan gave Neri a sketchbook."
- "Neri won the local poetry prize, according to Tovan."

The last fact is about Neri but was stated by Tovan; the gift goes from Tovan to Neri, and the suggestion goes from Neri to Tovan.

### 3. Save

Use `remember` with the resolved context_id, parsed summary, content, and appropriate type/importance/tags, and always `tags_normalize=true`. For several items, one `remember_batch(context_id=..., items=[{...}, ...], tags_normalize=true)` call takes the same fields per item (no `context_id`). Keep a call to about 20 items and split a longer list; read each item's `status`. Older servers (no `remember_batch` in the tool list): one `remember` per item. Say each thing once — the user waits while it is written:

- `content` (required): only what the summary leaves out (the why, the evidence, numbers, the rejected option); never restate the summary — when the summary holds the whole fact, one short line naming the evidence or source (a PR, a file, a command).
- `context_summary`: one sentence on when to recall it, not a second summary. Search reads it, so keep it rather than drop it.

```
remember(
  context_id=...,
  summary="...",
  content="...",
  type="decision",
  importance=0.9,
  tags=["auth", "architecture"],
  tags_normalize=true,
  context_summary="Why this matters and when to reference it.",
  details={"observed_at": "2024-03-07"}  # only when the material is dated
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
- `details` is replaced wholesale on `update_memory` — resend `tool_trigger` when you update details, or send `details_patch`, which merges into the stored details. Marking, changing or deleting a guardrail needs the context editor role and a user API key.
- Keep a context at 20 or fewer tool guardrails.

### 4. Confirm

Show what was saved: summary, type, importance, tags. If the response carries a `lint` key, the write will recall badly (short / long / narrative summary, no tags, or a tag that near-duplicates an existing one) — apply the hint with `update_memory`. A `tag_normalized` entry only reports that a tag was stored under the context's established spelling; change it with `update_memory` only when you meant a different tag. The memory is saved either way: `scope="working"` names its consolidation lifecycle, not whether the write landed.
