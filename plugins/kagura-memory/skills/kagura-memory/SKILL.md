---
name: kagura-memory
description: Use Kagura Memory Cloud from Codex for long-term project memory. Trigger when the user asks to start or restore a session, recall past decisions or bug fixes, save session knowledge, remember a decision or troubleshooting note, inspect memory contexts, or verify the kagura-memory MCP connection.
---

# Kagura Memory

Use Kagura Memory Cloud MCP tools as Codex's persistent project memory. Local git state and files remain the source of truth for current code; memory is supplemental context for decisions, rationale, and reusable lessons.

Claude Code slash commands map to natural-language Codex requests:

- `/kagura-memory:session-start` -> "kagura memory session start" or "restore my Kagura Memory session"
- `/kagura-memory:session-summary` -> "save a Kagura Memory session summary"
- `/kagura-memory:recall` -> "recall from Kagura Memory ..."
- `/kagura-memory:remember` -> "remember this in Kagura Memory ..."
- `/kagura-memory:guide` -> "show the Kagura Memory guide"
- `/kagura-memory:smoke-test` -> "smoke test Kagura Memory"

## Tool Availability

Use the available Kagura Memory MCP namespace, typically exposed as tools such as:

- `list_contexts`
- `get_context_info`
- `recall`
- `reference`
- `explore`
- `remember`
- `update_memory`
- `forget`
- context, edge, sleep, and usage tools when exposed

If the MCP tools are not available:

1. Run `codex mcp list` to check whether `kagura-memory` is configured.
2. If missing, configure the HTTP MCP server in `~/.codex/config.toml` with `[mcp_servers.kagura-memory]`.
3. Set `url` to your Memory Cloud endpoint: a self-hosted deployment uses `https://<your-domain>/mcp/w/<workspace-id>` (or `http://localhost:8080/mcp/w/<workspace-id>` for local development).
4. Pass auth via `bearer_token_env_var` or an `[mcp_servers.kagura-memory.http_headers]` table with `Authorization = "Bearer <api-key>"`.
5. Restart Codex so the tools are loaded.

Never print API keys or bearer tokens. When showing config, redact secrets.

## Resolve Context

Start by calling `list_contexts(include_stats=true)` when the context is unknown.

Pick the context whose `name`, `summary`, or recent usage matches the current repository or task. If several contexts are plausible and the choice affects writes, ask the user.

After choosing a context, call `get_context_info(context_id=..., include_details=true)` once per session or after switching contexts. Follow the context-specific `usage_guide` over generic defaults.

## Start Session

When the user asks to start, restore, or resume a Kagura Memory session:

1. Inspect local state in parallel where possible:
   - `git branch --show-current`
   - `git log --oneline -5`
   - `git status --short`
   - `git diff --stat HEAD~3` if available
2. Resolve the target context and load `get_context_info`.
3. Recall recent and actionable memory (compute a real ISO-8601 timestamp for `created_after` — never pass the literal placeholder):
   - `recall(context_id=..., query="session summary progress decision", k=5, filters={"created_after": "<ISO-8601 timestamp for 7 days ago>"})`
   - `recall(context_id=..., query="blocker issue TODO pending", k=5, filters={"created_after": "<ISO-8601 timestamp for 7 days ago>"})`
   - `recall(context_id=..., query="dev environment troubleshooting workaround", k=3, filters={"type": "troubleshooting"})`
4. If the branch, commits, or memories mention issue numbers and `gh` is available, inspect relevant issues.
5. Report concise restored context: branch, uncommitted changes, chosen memory context, recent work, memory highlights, open issues, and suggested next steps.

Keep memory highlights selective. Do not dump long histories.

## Recall

When the user asks what is remembered, asks for prior decisions, or needs similar past fixes:

1. Resolve the context.
2. Use `recall(context_id=..., query=..., k=5-10)`.
3. Choose `search_mode` deliberately:
   - `hybrid`: default for most queries.
   - `keyword`: exact terms, IDs, Japanese hiragana-only terms, or error strings.
   - `semantic`: concepts where wording may differ.
4. Use filters when the user asks for a type, date, tag, source URI, or known category. If tag spelling is uncertain, search broadly first rather than inventing tag filters.
5. For important hits, call `reference(context_id=..., memory_id=...)` to load full details.
6. For broad context-building, call `explore(context_id=..., memory_id=..., depth=2, min_weight=0.0)` on the strongest seed memory.

Show result summaries with `memory_id`, type, importance, and tags when the user needs to choose what to inspect.

## Remember

When saving new knowledge, decisions, bug fixes, troubleshooting notes, or session lessons:

1. Resolve the context. Ask before writing if the context is ambiguous.
2. Store reusable conclusions, not process narration.
3. Use this type vocabulary unless the context guide says otherwise:
   - `decision`: architecture choices, rejected alternatives, rationale.
   - `pattern`: reusable implementation approaches.
   - `bug-fix`: root cause and fix.
   - `troubleshooting`: environment gotchas and workarounds.
   - `learning`: benchmarks, tool limits, evaluation findings.
   - `note`: roadmap, status, milestone relationships.
4. Set importance by reuse value:
   - `1.0`: core principle or critical decision.
   - `0.8-0.9`: reusable decision, bug fix, or pattern.
   - `0.5-0.7`: troubleshooting or status note.
5. Include tags for category, entities, features, and related issues, for example `category:auth` or `issue:#802`.
6. When the knowledge comes from a specific file, URL, or vault, set the first-class `source_uri` and `source_type` (`"file"` | `"url"` | `"vault"` | `"api"` | `"manual"`) fields so it stays retrievable via `recall(filters={"source_uri_prefix": ...})` and `{"source_type": ...}`. Use `linked_source_uris` to connect related memories at creation time.
7. Include `Related issues: #N` in `content` when applicable.

Use `remember(context_id=..., summary=..., content=..., type=..., importance=..., tags=..., context_summary=..., source_uri=..., source_type=..., linked_source_uris=...)`.

Never store passwords, API keys, bearer tokens, private customer data, or unnecessary PII.

## Session Summary

At the end of a development session or before switching tasks:

1. Review the conversation and local work.
2. Identify durable items only: decisions, patterns, bug fixes, troubleshooting notes, learnings, and roadmap notes.
3. Save separate memories for separate reusable conclusions. Avoid one large transcript-style dump.
4. Include issue tags and a `Related issues:` line in content where relevant.
5. Report what was saved: context, count, type, summary, and importance.

Skip saving ephemeral actions such as "ran tests" unless there is a reusable environment trap or command pattern.

## Smoke Test

Use a read-only smoke test by default:

1. `list_contexts(include_stats=true)`
2. `get_usage()` when exposed
3. Choose a non-sensitive context and run a harmless `recall(context_id=..., query="smoke test memory", k=1)`

For a write smoke test, ask the user first because it creates and deletes test data. Then create a temporary context, capture the `id` returned by `create_context`, remember one test memory in it, recall it, reference it, and pass exactly that captured `id` to `delete_context`. Never pass a guessed or pre-existing context_id to `delete_context` — it soft-deletes the entire context and all of its memories.
