---
paths:
  - "**"
---

# Development Workflow

## Flow (kagura-plugins workflow)

The issue→PR loop runs through the `kagura-plugins` marketplace plugins. The
local `.claude/commands/` now only hold project-specific utilities (`/quality`,
`/test-unit`, `/test-e2e`, `/self-maint`, `/api-docs-audit`, `/release`,
`/docker`, `/admin`).

1. **Start**: `/gh-issue-driven:start <number>` — fetches the issue, recalls
   related past work from Kagura Memory, runs the gate1 design review, and
   creates the typed branch (`{issue-number}-{type}/{description}`).
2. **Implement**: Write code keeping review criteria in mind from the start.
   Use `recall`/`remember` throughout. Follow TDD where it fits.
3. **Quality**: Run `/quality` (lint, type-check, frontend tests).
4. **DB test**: If DB schema or queries changed, run `make test-integration`.
5. **Review**: Run `/kagura-code-reviewer` (or `/code-review`) — fix all
   critical and warning findings before proceeding.
6. **Ship**: `/gh-issue-driven:ship` — runs gate2 (audit + cso + qa-lead + cto)
   and creates the PR linking the issue (`Closes #N` or `(#N)` in title). Only
   when the user explicitly asks.
7. **Post-PR review**: `/gh-issue-driven:review` drives the Copilot / code-review
   loop on the open PR.
8. **Merge**: Only after user approval. Never auto-merge.

Whole-milestone automation: `/gh-issue-driven:goal <milestone>` runs the loop
above for every open issue; `/gh-issue-driven:status` shows the current branch's
phase. Release tagging: `/gh-issue-driven:tag` (milestone notes) + `/release`
(project version-file bump).

## Commit Discipline

- Commit frequently with meaningful messages
- Follow Conventional Commits: `<type>(<scope>): <subject> (#issue-number)`
- Each commit should be atomic — one logical change per commit
- Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
- Scopes: `api`, `mcp`, `auth`, `db`, `infra`, `frontend`, `docs`

## PR Rules

- Run `/quality` → `/kagura-code-reviewer` (or `/code-review`) before every PR (no exceptions); `/gh-issue-driven:ship` then runs gate2 and opens the PR
- PR title: Under 70 characters, Conventional Commits format
- Squash merge to `main`
- Delete branch after merge
- Never skip review before merge
