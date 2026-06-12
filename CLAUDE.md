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

**Key sequence** (kagura-plugins workflow): Issue → `/gh-issue-driven:start` (gate1 + branch) → Implement (TDD, `recall`/`remember` throughout) → `/quality` → `/kagura-code-reviewer` (or `/code-review`) → `/gh-issue-driven:ship` (gate2 + PR) → `/gh-issue-driven:review` → Merge

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
- `.claude/rules/dev-environment.md` — Docker, test commands, Python env, Makefile targets (auto-loaded)
- `.claude/rules/development-workflow.md` — Development flow, commit discipline, PR rules (auto-loaded)
- `.claude/commands/` — Project-specific slash commands (`/test-unit`, `/test-e2e`, `/quality`, `/self-maint`, `/api-docs-audit`, `/release`, `/docker`, `/admin`). The issue→PR workflow (`/issue-start`, `/self-review`, `/workflow`) moved to the `kagura-plugins` marketplace — see below.
- **`kagura-plugins` marketplace** (enabled in `~/.claude/settings.json`) — the dev workflow now runs through these plugins:
  - `/gh-issue-driven:start` · `:ship` · `:review` · `:status` · `:tag` · `:goal` — Issue → branch → gate1 → implement → gate2 → PR → review → release
  - `/kagura-code-reviewer` — Ollama-powered diff review grounded in Kagura Memory (replaces the old `/self-review` + `/simplify` step)
  - `/kagura-engineer:*`, `/kagura-planner:plan`, `/claude-c-suite:*`, `/claude-phd-panel:*`
- `.claude-plugin/` + `claude-skills/` — Kagura Memory Cloud plugin (marketplace-compatible)
  - Commands: `/kagura-memory:session-start`, `/kagura-memory:session-summary`, `/kagura-memory:recall`, `/kagura-memory:remember`, `/kagura-memory:guide`, `/kagura-memory:smoke-test`
- `.claude/agents/` — Specialized agents (`code-reviewer`, `test-runner`)
- `docs/` — Detailed documentation (concepts, architecture, API reference)
