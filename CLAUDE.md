# Kagura Memory Cloud - Development Guide

## Overview

Kagura Memory Cloud is a Universal AI Memory Platform — Remote MCP Server + Web Management UI.
This project uses **Kagura Memory Cloud itself** as a knowledge base. Static documentation is minimal; past patterns, troubleshooting, and design decisions are stored and searched as memories.

## Memory-First Development

**Use Kagura Memory Cloud MCP tools throughout all development work.**

- **Before implementing**: `recall(context_id="kagura-dev", query="...", k=10, use_rerank=true)` to find past patterns
- **After implementing**: `remember(context_id="kagura-dev", summary="...", type="pattern", importance=0.8, tags=[...])` to save learnings
- **On errors**: `recall(context_id="kagura-dev", query="{error message}", filters={"type": "troubleshooting"})` to check for known solutions
- **For detailed usage**: Call `kagura_memory_usage_guide` tool or `get_context_info(context_id="kagura-dev")`
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

## PR Review Trigger

Comment `/review` on the PR to run AI review + quality checks via GitHub Actions.

## References

- [README.md](README.md) — User-facing docs, setup guide, MCP client configuration
- [CONTRIBUTING.md](CONTRIBUTING.md) — Contribution guide, code style, testing
- [SECURITY.md](SECURITY.md) — Vulnerability reporting, security design
- `.claude/rules/` — Coding standards (auto-loaded by file path)
- `.claude/rules/development-workflow.md` — Development flow, commit discipline, PR rules (auto-loaded)
- `.claude/commands/` — Slash commands (`/test`, `/quality`, `/self-review`, `/recall`, `/remember`, `/issue-start`, `/self-maint`, `/api-docs-audit`, `/workflow`, `/release`, `/guide`, `/setup`, `/docker`, `/admin`, `/mcp-smoke-test`)
- `/simplify` — Built-in skill (not a local command file)
- `.claude/agents/` — Specialized agents (`code-reviewer`, `test-runner`)
- `docs/` — Detailed documentation (concepts, architecture, API reference)
