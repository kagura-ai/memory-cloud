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
   critical and warning findings before proceeding. Route everything the
   review raises but the PR does not fix by the Follow-ups rule below.
6. **Ship**: `/gh-issue-driven:ship` — runs gate2 (audit + cso + qa-lead + cto)
   and creates the PR linking the issue (`Closes #N` or `(#N)` in title). Only
   when the user explicitly asks.
7. **Post-PR review**: `/gh-issue-driven:review` drives the Copilot / code-review
   loop on the open PR.
8. **Merge**: Only after user approval. Never auto-merge.

Whole-milestone automation: `/gh-issue-driven:goal <milestone>` runs the loop
above for every open issue; `/gh-issue-driven:status` shows the current branch's
phase. Releases: `/release patch|minor|major` is the whole ceremony for this repo
(release branch → nine version files incl. `uv.lock` + CHANGELOG → PR + one Copilot round →
squash merge → annotated tag on the merge commit → GitHub Release → milestone
close → coverage upload). `/gh-issue-driven:tag` is not run here end-to-end: it
commits and pushes on the protected `main`, bumps only the plugin manifests and
aborts on the `version` field `.claude-plugin/marketplace.json` does not have —
use it at most in `dry-run` to draft release notes.

## Follow-ups (what a review raises but the PR does not fix)

Decided 2026-11-03 after v0.139.0 produced 18 follow-up issues, 15 of them
memos. Review observations do not become GitHub issues by default; they are
sorted into three buckets:

1. **Fix now**: a regression introduced by this milestone's own changes is fixed
   before the release, even at the cost of one more CI cycle. A pre-existing bug
   near the files being touched and small (tests included, tens of lines) is
   fixed in the same PR; the reviewer may add the commit.
2. **Bug for a milestone**: a pre-existing bug with a concrete failure scenario
   that is not small or not nearby becomes an issue with the next milestone set
   (the current one when the user says so). Never `backlog` without a milestone.
3. **Memo**: refactors, duplicated helpers, nits, docs sweeps, dead code and
   hardening ideas are saved to Kagura Memory (context `kagura-dev`) as
   `type="tech-debt"`; a question only the owner can answer as
   `type="needs-decision"`. Importance 0.45 / 0.5; summary starts
   `tech-debt (<area>): ` or `needs-decision (<area>): `; content is Problem /
   Proposal / Source / `Related issues: #…`; `context_summary` says when to pick
   it up; tags: the type, `status:open`, `source:review`, `issue:#N`, `pr:#N`,
   `area:*`, and `file:<repo path>` for every file concerned, so
   `/gh-issue-driven:start` recalls them when an issue in that area starts.
   Resolved memos get `status:done` (via `update_memory`, tags are replaced
   wholesale) or are superseded by the fixing PR's savepoint.

The orchestrator (or the person running the loop) writes the memos, one per
theme per PR; implementers and reviewers report their findings in the three
buckets. Implementers may make small adjacent consolidations (a duplicated
helper in the same area) instead of leaving a memo. At milestone planning,
`recall(filters={"type": "needs-decision"})` and `{"type": "tech-debt"}` show
what accumulated; only what the owner decides to do becomes an issue then.
GitHub issues are for bugs with a failure scenario and for user-impacting
decisions and features.

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
