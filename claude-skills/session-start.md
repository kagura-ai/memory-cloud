---
description: Resume work by recalling recent session context from Kagura Memory Cloud
---

Restore previous session context to quickly resume development work. Uses git state as the primary signal and Memory Cloud for supplementary context.

## Steps

### 1. Gather context (run in parallel)

Run these three data-gathering blocks concurrently — they have no dependencies on each other.

**Git state** (Bash):

```bash
git branch --show-current
git log --oneline -5
git status --short
git diff --stat HEAD~3 2>/dev/null || true
```

Use the branch name, recent commits, modified files, and uncommitted changes to infer what work is in progress.

**GitHub issues** (Bash — skip if `gh` unavailable):

```bash
command -v gh >/dev/null 2>&1 && gh issue list --state open --limit 10 --json number,title,milestone --jq '.[] | "#\(.number) [\(.milestone.title // "no milestone")] \(.title)"' || echo "(gh CLI not available — skipping GitHub issues)"
```

**Memory Cloud** (MCP):

```
list_contexts()
```

If multiple contexts exist, pick the one most relevant to the current project. If unclear, ask the user.

Then recall recent memories (last 7 days). The 7-day window balances recency with coverage — long enough to span a typical work week including weekends, short enough to avoid stale context drowning out current work.

Calculate the date 7 days ago from today and use it as `created_after` filter. Run these recalls in parallel. Only the first query enables `include_explore_hints` — it covers broad session context where graph discovery adds value; the other two are narrow, targeted queries where explore hints would add overhead without benefit.

All three bootstrap recalls pass `trust_tier: "trusted"`. The recalled memories are fed back as "here is your context" and influence what you do next, so this is a behaviour-influencing read (OWASP LLM01/LLM03 indirect prompt injection): the filter excludes external/connector-ingested memories (Slack/Discord/etc.) from the bootstrap. It is a no-op on manual-only contexts and protective on connector-mixed workspaces.

```
recall(context_id=..., query="session summary progress decision", k=5, filters={"created_after": "{7_days_ago_ISO8601}", "trust_tier": "trusted"}, include_explore_hints=true)
```

```
recall(context_id=..., query="blocker issue TODO pending", k=5, filters={"created_after": "{7_days_ago_ISO8601}", "trust_tier": "trusted"})
```

```
recall(context_id=..., query="dev environment troubleshooting workaround", k=3, filters={"type": "troubleshooting", "tags": ["dev-environment"], "trust_tier": "trusted"})
```

After the recalls, load the deterministic always-on layer — these are NOT probabilistic recalls, so run them regardless of the 7-day window:

```
load_pinned(context_id=...)
```

- This returns the COMPLETE pinned set (`delivery_mode="always"` memories — standing guardrails/goals), deterministically and unranked. It is the counterpart to `recall`: the must-load-every-session layer.
- **If it returns zero pinned memories, OMIT the "📌 Standing guardrails" section entirely** — do not print the heading, and do not print "none"/"no pinned memories".
- Otherwise render each item with its `memory_id`, and append the unpin affordance line (see step 3 template). If `load_pinned` reports more than ~7 items, also append: `⚠ pinned set is large (N) — review for stale invariants to unpin.`

```
recall_upcoming(context_id=..., from="now")
```

- This returns forward-looking Time Memories (`type="time"`) whose trigger window is upcoming — dated follow-ups, deadlines, scheduled re-checks.
- **If it returns zero upcoming memories, OMIT the "⏰ Upcoming" section entirely** (same empty-suppression rule as above).

### 2. Check related GitHub issues

If issue numbers appear in the branch name, recent commits, or recalled memories (and `gh` is available):

```bash
gh issue view <number> --json title,state,body,labels
```

### 3. Present session context

Display a concise summary:

```
## Session Context Restored

**Branch**: {current_branch}
**Uncommitted changes**: {yes/no, summary if yes}
**Context**: {context_name}

### Recent Work (from git)
{what the recent commits and changes indicate}

### From Memory Cloud
{relevant memories from last 7 days, if any}

### 📌 Standing guardrails
{ONLY if load_pinned returned ≥1 item — omit this whole section when empty.
 List each pinned invariant with its memory_id, e.g. "- active prod color = green  (mem: abc1234)".
 End with: "Stale? unpin via update_memory(memory_id=..., context_id=..., delivery_mode="on_recall")".
 If the pinned set is large (>7), add "⚠ N pinned — review for stale invariants to unpin".}

### ⏰ Upcoming
{ONLY if recall_upcoming returned ≥1 item — omit this whole section when empty.
 List forward-looking Time Memories soonest-first.}

### Open Issues
{open issues, prioritized by milestone}

### Suggested Next Steps
{based on git state + memories + issues, suggest what to work on}
```

### 4. Guidelines

- **Git state is primary** — recent commits and uncommitted changes are the most reliable signal
- **Memory Cloud is supplementary** — adds context that git alone doesn't capture (decisions, rationale, blockers)
- **Be concise** — show only what's actionable, not a full history dump
- **If no recent memories** — that's fine, rely on git state and issues
- **Don't assume** — if context seems ambiguous, ask the user what they're working on
