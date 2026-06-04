---
description: Save session knowledge to Kagura Memory Cloud before ending a conversation
---

Summarize the current session's key learnings and save them to Kagura Memory Cloud.

## When to use

At the end of a development session, or when switching to a different task. Captures decisions, patterns, bugs, and plans that would be useful in future sessions.

## Steps

### 1. Identify related GitHub issues

Review the conversation and collect all GitHub issue numbers that were worked on, referenced, or discussed. Use `gh issue view <number>` if needed to confirm titles.

### 2. Identify what to remember

Review the conversation and categorize knowledge into one of the canonical types (see `remember` skill for the full vocabulary):

| Type | What to capture | Importance |
|------|----------------|------------|
| `decision` | Architecture decisions, approach choices, rejected alternatives with WHY | 0.8-1.0 |
| `pattern` | Implementation patterns, technical solutions, reusable approaches | 0.7-0.9 |
| `bug-fix` | Root cause analysis, regression lessons, "never do this again" | 0.8-0.9 |
| `troubleshooting` | Error solutions, workarounds, environment-specific fixes | 0.5-0.7 |
| `learning` | SDK benchmark results, performance findings, tool limitations | 0.6-0.8 |
| `note` | Milestone status, roadmap changes, issue relationships | 0.6-0.8 |
| `time` | Forward-looking / dated follow-ups (a deadline, "re-check on date X", a scheduled flag flip). Set `details={"trigger": {...}}` so `recall_upcoming` surfaces it at the right time instead of letting it decay into the backlog. | 0.6-0.8 |

### 3. Get the target context

```
list_contexts()
```

Ask the user which context to save to if unclear. Default: the project's development context.

### 4. Save each knowledge item

For each item, use `remember` with:

- **summary**: Searchable conclusion (not process). Include synonyms/related terms. 100-250 chars.
- **content**: Full details — what, why, how, evidence
- **type**: From the table above
- **importance**: Based on reusability across future sessions
- **tags**: `category:{domain}` + entity tags + writing variations for Japanese + `issue:#N` for each related issue
- **context_summary**: Why this matters, when to recall it. Include a `Related issues: #N, #M` line at the end of **content** linking to relevant GitHub issues.
- **linked_source_uris** (optional): If the knowledge relates to a specific file or document already in memory, link it by source_uri (e.g. `["vault://my-vault/related-note.md"]`). Unresolved URIs are silently skipped.

### 4a. Pinning standing guardrails (`delivery_mode="always"`) — sparingly

A pinned memory is loaded **deterministically every session** via `load_pinned` (see `session-start`). That makes it the right home for a true standing invariant — an active prod color, a non-negotiable policy, a release gate — but it also means every pinned memory costs context budget on every future session. Over-pinning degrades the signal of every session that follows.

Pin only when the knowledge must be seen *every* session, not merely recalled when relevant. Concretely:

- **Budget: keep a context at ≤7 pinned memories; treat 10 as a hard "stop and prune" line.** The server hard cap (`pinned_load_cap`, default 100) is a truncation safety net, **not** the editorial budget — do not let it stand in for judgment. Decisions, patterns, and status notes are NOT pin material; they belong in normal `recall`.
- **Pre-pin count check**: before pinning, call `load_pinned(context_id=...)`. If the context already has ≥7 pinned, unpin a stale one first.
- **Supersede = unpin old + pin new** (atomic). When a new invariant replaces an old one (e.g. active color `green` → `blue`), unpin the old one in the same step — never leave two competing invariants pinned:
  ```
  update_memory(memory_id=<old>, context_id=..., delivery_mode="on_recall")
  ```
  Then `remember(..., delivery_mode="always")` (or `update_memory(... delivery_mode="always")`) the new one.

To pin at save time: `remember(..., delivery_mode="always")`.

### 5. Guidelines

- **Write conclusions, not narratives** — "P2 failed because tags inflate BM25 scores" not "We tried P2 and it didn't work"
- **Include numbers** — "P@1: 76% → 89%" not "significant improvement"
- **Tag for discoverability** — future you will search by keyword, not by date
- **Separate concerns** — one memory per decision/pattern, not one giant dump
- **Skip ephemeral details** — don't save "ran make lint", do save "lint requires ruff check from project root"
- **Include rejected approaches** — knowing what NOT to do is as valuable as what to do
- **Save dev-env traps** — if you hit a dev environment gotcha (wrong file path, missing env var, non-existent compose file, etc.), save it as `type="troubleshooting"` with `tags=["dev-environment"]` so future sessions can avoid the same mistake

### 6. Report

After saving, print a summary:

```
## Session Knowledge Saved

Related issues: #12, #34

| # | Type | Summary | Importance |
|---|------|---------|------------|
| 1 | decision | ... | 0.9 |
| 2 | pattern | ... | 0.8 |
...

Total: N items saved to context {context_name}
```
