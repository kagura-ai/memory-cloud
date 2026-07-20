# Documentation

> Kagura Memory Cloud is a team-scale **LLM Knowledge Base** — beyond RAG. It implements the 5-layer pattern from [Karpathy's LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) (Ingest → Compile → Index → Query → Enhance) using MCP as the compile API, BM25 + Qdrant + Hebbian as a triple-index, and Sleep Maintenance for batch consolidation. See [Architecture](architecture.md#llm-knowledge-base--5-layer-mapping) for the full mapping.

## Getting Started

- [Getting Started](getting-started.md) — Installation, setup, and first steps
- [Core Concepts](concepts.md) — Workspace, Context, Memory, Neural Memory, MCP Tools
- [MCP Client Setup](mcp-clients.md) — Claude Code / Desktop / Chat, ChatGPT, Gemini CLI, plugin & templates, WSL2 note
- [MCP Tools Reference](mcp-tools.md) — All 63 tools with required roles

## Architecture & API

- [Architecture](architecture.md) — System design, data flow, and tech stack (incl. **LLM Knowledge Base 5-layer mapping**)
- [API Reference](api-reference.md) — REST API endpoints, authentication, request/response examples
- [Derived-Layer Boundary](derived-layer-boundary.md) — Design rule: raw memories are exportable, the derived/learned layer is the moat (table classification + feature-review checklist)
- [Agent Registry & Context Bindings](design/agent-registry-and-bindings.md) — Implemented v0.49.0-preview registry, agent-bound key, and subtractive binding contract
- [`get_agent_bootstrap` Contract](design/agent-bootstrap-contract.md) — Implemented composed session-start bundle and fail-soft component contract
- [Agent Correlation Design](design/agent-otel-correlation.md) — Implemented W3C session/run/trace mapping and verified identity precedence (v0.49.0 preview)
- [`memory_access_events` Design](design/memory-access-events.md) — Implemented append-only agent audit schema/writer and current emission coverage
- [Multi-Platform Connectors](design/multi-platform-connectors.md) — Design note (#1390): how the connectors UI/contracts generalize for a 2nd chat provider
- [Slack Channel Picker](design/slack-channel-picker.md) — Design note (#1391): server-proxied channel listing for connector settings
- [Platform-Borne Connector LLM](design/platform-borne-connector-llm.md) — Design note (#1392): SaaS-default LLM lane with BYOK as override
- [Add-On Entitlements](design/addon-entitlements.md) — Design note (#1393): grant ledger upstream of the existing addon bonus columns
- [Admin-Definable Plan Tiers](design/admin-definable-plan-tiers.md) — Design note (#1394): DB-backed tiers beyond hardcoded S/M/L

## Guides

- [Chunking Guide](chunking-guide.md) — Best practices for structuring memories (the **Compile** layer in human terms)
- [Resource Tokens Guide](resource-tokens-guide.md) — External data ingestion via resource tokens (the **Ingest** layer)
- [Neural Memory Evaluation](neural-memory-evaluation.md) — Benchmark results, architecture decisions, known limitations (the **Enhance** layer evidence)
- [Sleep Maintenance](sleep-maintenance.md) — Background 6-phase cleanup cycle, sleep_mode, observability, and rollback (the **Compile / Enhance** consolidation layer)
- [Deployment](deployment.md) — Production deployment with Caddy reverse proxy (incl. the embedded LanceDB **"Kagura Lite"** backend, preview)
- [Troubleshooting](troubleshooting.md) — Environment-specific setup fixes (e.g. WSL2 + Claude Code MCP OAuth callback)
- [Agent Credential Runbook](ops/agent-credential-runbook.md) — Mint, rotate, revoke, expire, and kill-switch agent-bound workload keys
- [`memory_access_events` Retention Plan](ops/memory-access-events-retention.md) — Capacity triggers and partitioning/retention plan for the live audit table
