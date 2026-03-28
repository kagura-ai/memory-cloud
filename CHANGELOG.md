# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-03-25

### OSS Release

First open source release of Kagura Memory Cloud.

#### Added
- Apache License 2.0
- OSS documentation (README, CONTRIBUTING, SECURITY, concepts.md)
- Billing Plugin separation (BILLING_ENABLED flag, admin-only plan changes by default)
- Plan tier environment variable overrides (PLAN_FREE_MAX_CONTEXTS, etc.)
- MCP `create_context` tool (owner/admin only, with quota check)
- Alembic migration system (replaced custom SQL runner)
- Smoke/E2E/URL validation tests (190 tests)
- Makefile commands: test-smoke, test-e2e, test-urls, migrate-status
- `.env.example` with billing/plan override settings

#### Changed
- README.md rewritten for OSS (English-based, architecture docs, manual setup guides)
- Self-service plan changes require BILLING_ENABLED=true (admin-only by default)
- Removed SaaS-specific URLs from documentation

#### Removed
- `run_all_migrations.py` (replaced by Alembic)

## [0.1.0] - 2025-11-20

### Phase 0: Project Setup & Foundation (v4.4.0 Migration)

Initial release of Kagura Memory Cloud as a standalone Remote MCP Server.
Migrated from Kagura AI v4.4.0 codebase.

#### Added
- Initial project structure (#3)
- Backend directory structure (FastAPI + MCP Server)
- Frontend directory structure (Next.js 14)
- Infrastructure directory structure (Terraform)
- Development environment setup
  - docker-compose.yml
  - .env.example
  - .gitignore
  - .editorconfig
- Documentation
  - CLAUDE.md (AI Assistant development guide)
  - README.md (Project overview)
  - CONTRIBUTING.md (TBD)
- CI/CD基盤 (GitHub Actions) - TBD
- Branch strategy and protection rules - TBD

#### Changed
- N/A

#### Deprecated
- N/A

#### Removed
- N/A

#### Fixed
- N/A

#### Security
- N/A

---

## Version History

### [0.1.0] - 2025-11-20

First release - Project setup and foundation for Remote MCP Server.

**Completed:**
- Phase 0.1: リポジトリ初期化 (#3)
- Phase 0.2: ブランチ戦略設定 (#4)
- Phase 0.3: 開発環境構築 (#5)
- Phase 0.6: 技術スタック確定 (#8)

**Next:**
- v0.2.0: Core API Implementation (remember, recall, forget, reference)
- v0.3.0: Neural Memory + explore()
- v0.4.0: OAuth2 + API Keys
- v1.0.0: Production release

---

[Unreleased]: https://github.com/kagura-ai/memory-cloud/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kagura-ai/memory-cloud/releases/tag/v0.1.0
