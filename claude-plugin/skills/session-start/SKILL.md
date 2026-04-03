---
description: Resume work by recalling recent session context from Kagura Memory Cloud
---

Restore previous session context from Kagura Memory Cloud to quickly resume development work.

## Steps

### 1. Identify the working context

```
list_contexts()
```

If multiple contexts exist, pick the one most relevant to the current project directory. If unclear, ask the user.

### 2. Check current branch and recent git activity

```bash
git branch --show-current
git log --oneline -5
```

Use the branch name and recent commits to infer what work was in progress.

### 3. Recall recent session summaries

```
recall(context_id=..., query="session summary recent work progress status", k=5, filters={"type": "note"})
```

### 4. Recall decisions and patterns from recent work

```
recall(context_id=..., query="decision architecture pattern", k=5, filters={"type": "decision"})
```

### 5. Recall any open issues or blockers

```
recall(context_id=..., query="blocker issue TODO pending incomplete", k=5)
```

### 6. Check related GitHub issues

If issue numbers appear in recalled memories or the branch name:

```bash
gh issue view <number> --json title,state,body,labels
```

### 7. Present session context

Display a concise summary:

```
## Session Context Restored

**Branch**: {current_branch}
**Context**: {context_name}

### Last Session
{summary of most recent session memory — what was done, what was decided}

### Open Items
{any pending work, blockers, or TODOs found}

### Key Decisions
{recent decisions that are still relevant}

### Suggested Next Steps
{based on the above, suggest what to work on}
```

### 8. Guidelines

- **Be concise** — show only what's actionable, not a full history dump
- **Prioritize recency** — newer memories are more likely to be relevant
- **Cross-reference** — if a memory mentions an issue number, check its current state
- **Don't assume** — if the recalled context seems stale or ambiguous, ask the user what they're working on
