# Kagura Memory Cloud - Development Guide

## Overview

Kagura Memory Cloud is a Universal AI Memory Platform — Remote MCP Server + Web Management UI.
This project uses **Kagura Memory Cloud itself** as a knowledge base. Static documentation is minimal; past patterns, troubleshooting, and design decisions are stored and searched as memories.

## Memory-First Development

**Use Kagura Memory Cloud MCP tools throughout all development work.**

- **Before implementing**: `recall(context_id="kagura-dev", query="...", k=10, use_rerank=false)` to find past patterns
- **After implementing**: `remember(context_id="kagura-dev", summary="...", type="pattern", importance=0.8, tags=[...])` to save learnings
- **On errors**: `recall(context_id="kagura-dev", query="{error message}", filters={"type": "troubleshooting"})` to check for known solutions
- **For detailed usage**: Call `get_context_info(context_id="kagura-dev")` or use `/kagura-memory:guide` skill
- **Coding standards**: Auto-loaded from `.claude/rules/` — no need to reference this file

## Development Workflow

See `.claude/rules/development-workflow.md` for full flow (auto-loaded).

**Key sequence**: Issue → Branch → Implement → `/quality` → `/simplify` → `/self-review` → PR → Merge

## Branch Strategy

```
main (default, protected)
├── {issue-number}-feat/*
├── {issue-number}-fix/*
├── {issue-number}-refactor/*
├── {issue-number}-test/*
└── {issue-number}-docs/*
```

- **Default base branch**: `main`
- **Merge strategy**: Squash merge for all feature/fix/docs branches
- **Branch lifespan**: Max 7 days (split if longer)
- **Keep up to date**: `git rebase origin/main`
- **Releases**: Tag-based (`v1.0.0`, `v1.1.0`, ...)
- **Direct commits to main**: Prohibited (branch protection enabled)

## Quality Commands

```bash
make lint            # Lint all (backend + frontend)
make test-local      # Backend tests
make test-frontend   # Frontend tests (Vitest)
make test-integration # Integration tests (DB, migrations)
```

## Writing Issues for the Harness

A three-agent harness (Planner / Generator / Evaluator) drives development
work end-to-end from issues. For the harness to run, issues must be
**harness-ready**: structured so the Planner can extract verifiable
acceptance criteria and translate them into contracts.

**Use the harness-ready template** when creating issues that should flow
through the harness:

```bash
gh issue create --template harness-ready.yml
```

**Write acceptance criteria as verifiable checkboxes.** Each `- [ ]` line
in the issue's acceptance criteria section becomes one contract entry.
Rules:

- Every criterion must be answerable by running a shell command and
  checking exit code. "POST /contexts returns 403 for cross-workspace
  access" is verifiable; "the API should feel secure" is not.
- Avoid subjective language ("works nicely", "looks good", "feels right").
  The Evaluator binds pass/fail to shell exit codes — subjective criteria
  will be dropped by the Planner's escalation path.
- Reference concrete endpoints, file paths, commands, or output strings.
- Link prior harness run IDs (`hr-YYYYMMDD-NNN`) when continuing work —
  the Planner's recall step will surface them automatically, but linking
  accelerates the search and documents intent.

**Skip the harness** (use `task.md` / `bug_report.md` / `feature_request.md`
instead) when:

- The issue is a raw bug report that has not yet been triaged into a
  concrete fix — convert to harness-ready only after triage proposes a
  concrete fix path.
- The work is pure design exploration or brainstorming with no decidable
  output.
- The scope touches meta-work that cannot be verified by running a test
  (e.g., changing licensing terms, renaming the repo).

The authoritative contract schema lives at
`.claude/rules/harness-contract.md` and the Planner agent at
`.claude/agents/harness-planner.md` (both landing via companion PRs
#251 and #253; reference them there until merged). Once merged, those
two files are the single source of truth for harness behavior.

## PR Review Trigger

Comment `/review` on the PR to run AI review + quality checks via GitHub Actions.

## References

- [README.md](README.md) — User-facing docs, setup guide, MCP client configuration
- [CONTRIBUTING.md](CONTRIBUTING.md) — Contribution guide, code style, testing
- [SECURITY.md](SECURITY.md) — Vulnerability reporting, security design
- `.claude/rules/` — Coding standards (auto-loaded by file path)
- `.claude/rules/development-workflow.md` — Development flow, commit discipline, PR rules (auto-loaded)
- `.claude/commands/` — Project-specific slash commands (`/test-unit`, `/test-e2e`, `/quality`, `/self-review`, `/issue-start`, `/self-maint`, `/api-docs-audit`, `/workflow`, `/release`, `/docker`, `/admin`)
- `.claude-plugin/` + `claude-skills/` — Kagura Memory Cloud plugin (marketplace-compatible)
  - Commands: `/kagura-memory:session-start`, `/kagura-memory:session-summary`, `/kagura-memory:recall`, `/kagura-memory:remember`, `/kagura-memory:guide`, `/kagura-memory:smoke-test`
- `/simplify` — Built-in skill (not a local command file)
- `.claude/agents/` — Specialized agents (`code-reviewer`, `test-runner`)
- `docs/` — Detailed documentation (concepts, architecture, API reference)
