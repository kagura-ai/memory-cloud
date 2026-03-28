---
description: Show current development workflow state and suggest next step
---

Analyze the current development state and suggest the next action.

## Steps

### 1. Gather state

Run these commands and collect the results:

```bash
git branch --show-current
git status --short
git stash list
git log main..HEAD --oneline 2>/dev/null
git diff --stat main..HEAD 2>/dev/null
```

Also check:
- Is there an open PR for this branch? (`gh pr list --head <branch> --state open --json number,title,statusCheckRollup`)
- Are there unpushed commits? (`git log origin/<branch>..HEAD --oneline 2>/dev/null`)
- Are there open issues? (`gh issue list --state open --limit 5`)

### 2. Determine flow position and next action

Based on the gathered state, determine where we are in the workflow:

| State | Position | Next action |
|-------|----------|-------------|
| On `main`, no uncommitted changes | **Idle** | Pick an issue: `gh issue list --state open` → `/issue-start <number>` |
| On `main`, with uncommitted changes | **Uncommitted work on dev** | Create a branch first, then commit |
| On `main`, with stashed changes | **Stashed work** | Review stash: `git stash show -p` → apply or drop |
| On feature branch, uncommitted changes | **In progress** | Commit changes |
| On feature branch, committed, not pushed | **Ready to push** | Push branch |
| On feature branch, pushed, no PR | **Ready for quality** | Run `/quality` → `/simplify` → `/self-review` → create PR |
| On feature branch, PR open, CI pending | **CI running** | Wait for CI |
| On feature branch, PR open, CI passed | **Ready to merge** | Merge PR (user approval required) |
| On feature branch, PR open, CI failed | **CI failed** | Check failures, fix, push |

### 3. Output

Format the output as:

```
## Workflow Status

**Branch**: <branch name> (issue #N if applicable)
**Changes**: <uncommitted/committed/pushed/PR open>
**Stash**: <N entries / empty>
**CI**: <passing/failing/pending/not run>

## Next step

<describe the next action to take>
<offer to run it if appropriate>
```

### 4. Execute (if user confirms)

If the next step is clear and low-risk (e.g., running /quality, pushing), offer to execute it.
Do NOT auto-execute: merging PRs, creating releases, or destructive operations.
