---
description: Resume work by recalling recent session context from Kagura Memory Cloud
---

Restore previous session context to quickly resume development work. Uses git state as the primary signal and Memory Cloud for supplementary context.

## Steps

### 1. Check current git state

```bash
git branch --show-current
git log --oneline -5
git status --short
git diff --stat HEAD~3 2>/dev/null || true
```

Use the branch name, recent commits, modified files, and uncommitted changes to infer what work is in progress.

### 2. Check open GitHub issues

```bash
gh issue list --state open --limit 10 --json number,title,milestone --jq '.[] | "#\(.number) [\(.milestone.title // "no milestone")] \(.title)"'
```

### 3. Identify the working context

```
list_contexts()
```

If multiple contexts exist, pick the one most relevant to the current project. If unclear, ask the user.

### 4. Recall recent memories (last 7 days)

Calculate the date 7 days ago from today and use it as `created_after` filter. Run these in parallel:

```
recall(context_id=..., query="session summary progress decision", k=5, filters={"created_after": "{7_days_ago_ISO8601}"})
```

```
recall(context_id=..., query="blocker issue TODO pending", k=5, filters={"created_after": "{7_days_ago_ISO8601}"})
```

### 5. Check related GitHub issues

If issue numbers appear in the branch name, recent commits, or recalled memories:

```bash
gh issue view <number> --json title,state,body,labels
```

### 6. Present session context

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

### Open Issues
{open issues, prioritized by milestone}

### Suggested Next Steps
{based on git state + memories + issues, suggest what to work on}
```

### 7. Guidelines

- **Git state is primary** — recent commits and uncommitted changes are the most reliable signal
- **Memory Cloud is supplementary** — adds context that git alone doesn't capture (decisions, rationale, blockers)
- **Be concise** — show only what's actionable, not a full history dump
- **If no recent memories** — that's fine, rely on git state and issues
- **Don't assume** — if context seems ambiguous, ask the user what they're working on
