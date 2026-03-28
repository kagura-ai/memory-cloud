---
paths:
  - "**"
---

# Development Workflow

## Flow

1. **Start**: `gh issue view <number>` → understand the task
2. **Recall**: Search Kagura Memory Cloud for past related work
3. **Branch**: `gh issue develop <number> --checkout` or `git checkout -b {issue-number}-{type}/{description} main`
4. **Implement**: Write code keeping self-review criteria in mind from the start. Use `recall`/`remember` throughout.
5. **Quality**: Run `/quality` (lint, type-check, frontend tests)
6. **DB test**: If DB schema or queries changed, run `make test-integration`
7. **Simplify**: Run `/simplify` — review for code reuse, quality, and efficiency; fix any issues found
8. **Self-review**: Run `/self-review` — fix all `[C]` critical and `[W]` warning findings before proceeding
9. **PR**: Create PR linking the issue (`Closes #N` or `(#N)` in title). Only when user explicitly asks.
10. **Merge**: Only after user approval. Never auto-merge.

## Commit Discipline

- Commit frequently with meaningful messages
- Follow Conventional Commits: `<type>(<scope>): <subject> (#issue-number)`
- Each commit should be atomic — one logical change per commit
- Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
- Scopes: `api`, `mcp`, `auth`, `db`, `infra`, `frontend`, `docs`

## PR Rules

- Run `/quality` → `/self-review` before every PR (no exceptions)
- PR title: Under 70 characters, Conventional Commits format
- Squash merge to `main`
- Delete branch after merge
- Never skip self-review before merge
