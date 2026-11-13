# Google Connected Apps: the `google` Tool Profile

This page describes how the Remote MCP server is set up as a Google connected app ([#2773](https://github.com/kagura-ai/memory-cloud/issues/2773)): the MCP URL to register, what the `google` tool profile changes, and which tools to mark for user confirmation in the console. It describes the code; it makes no compliance claim.

Google's connected-apps policy for third-party MCP servers says that a tool must not "reference other tools or create new rules for when or how other tools should be used", that a server must not "expose dynamic discovery functions (e.g. list_tools, get_schema) or generic runtime dispatchers", that it "must never request, collect, or influence the model to provide user prompts, full conversation context, or conversation history", and that tools which alter persistent state need `destructiveHint: true` or the console's confirmation flag. The default (`core`) surface does not fit the first two clauses on purpose: its descriptions point at each other (`context_id` "from list_contexts()", `Manual: guide(...)`) and it lists `describe_tools`. The `google` profile is the same memory surface without those parts.

## 1. MCP URL

```
https://example.com/mcp?profile=google
https://example.com/mcp/w/<workspace_id>?profile=google
```

`?context=<context_id>` may be added (`…/mcp?profile=google&context=<context_id>`) to give the connection a default context. Do not add `?tools=`: an explicit allowlist wins over `profile`, and the URL is then no longer the google view. `?add_tools=` works as on the core profile, except that it cannot bring back `describe_tools` or `guide` (they are ignored and logged); a tool added that way is served with its standard text.

Authorization is the same OAuth 2.0 flow as for every connector. The operator settings Google's console needs — the exact redirect URIs of the connected-apps client and the domain verification file — are documented in [Deployment: Google connected apps](deployment.md#google-connected-apps-issue-2773).

## 2. What the `google` profile changes

Everything below applies only to a request whose URL selects `profile=google` without `tools=`. Every other URL — the bare `/mcp`, `?profile=core`, `?profile=full`, any `?tools=` allowlist — is served exactly as before.

| Area | Core profile | `google` profile |
|---|---|---|
| Tools listed | The core tools ([Tool Profiles](mcp-tools.md#tool-profiles)) | The core tools without `describe_tools` and `guide` |
| Input schemas | — | Identical to the core schemas (names, types, enums, limits, `required`); `tools/call` reads the profile only to refuse `describe_tools` and `guide` and for `recall`'s learning switch |
| Descriptions | Point at other tools and the manual | No description, at any depth, names another tool or says when to use one; `remember_batch` says "store what the user asks to keep" |
| `recall` | `destructiveHint: true`: learns associations (Hebbian reweighting, pruning) and promotes working memories | `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`: no learning writes and no promotion; only access counters move |
| Other annotations | `TOOL_ANNOTATIONS` | The same |
| Server instructions | Base text naming the entry calls, plus a guardrail digest when selected | A short base text that names no tool, and never a guardrail digest (the digest lists rules for when to call tools) |
| Replies | Caller errors carry `Manual: guide([...])`; `unknown_tool` names `describe_tools`; `get_context_info` / `bootstrap` carry the quick reference `instructions` | No manual pointer, a neutral `unknown_tool` help, no quick reference; sentences of a reply's `help`, `hint` and `message`, of write lint hints and of `persistence.detail` that point at another tool are dropped, and `plan_headroom[].help` names no tool |
| Service notices (#2762) | `bootstrap` carries the notices a person opted into when `MCP_NOTICES_ENABLED` is on | Never: the view carries no service notice, runs no notice query and uses none up |

Stored data is never rewritten: memory text, context summaries and usage guides, and the guardrail memories `bootstrap` and `get_context_info` return pass through unchanged, and a context name quoted in a reply (`Context 'list_tags' created successfully.`) is kept even when it spells a tool name. A success reply is never turned into a failure message.

The OAuth scope a call needs still follows the global annotation table, so a read-scope token cannot call `recall` on one URL and not on another.

## 3. Console: user confirmation

Google's console preselects "User confirmation required" for tools whose `destructiveHint` is `true`. On the `google` profile those are:

- `update_memory`, `forget`, `create_edge`, `update_context`, `delete_context`

Tick the flag by hand for the tools that only add data (`destructiveHint: false`, `readOnlyHint: false`):

- `remember`, `remember_batch`, `feedback`, `create_context`

The rest are read-only on this profile and need no confirmation: `recall`, `reference`, `recall_upcoming`, `load_pinned`, `get_context_info`, `list_contexts`, `list_tags`, `list`, `changes_since`, `bootstrap`.

## 4. Where it lives in the code

- `backend/src/mcp_server/tools/_profiles.py` — `PROFILES["google"]`, `is_google_view()`, the `add_tools` exclusion.
- `backend/src/mcp_server/tools/_google_profile.py` — the description overrides (each pinned to a fingerprint of the core text it replaces), the `recall` annotations, the instructions and the reply scrub.
- `backend/src/services/memory_service.py` — `recall(learning=False)`: no Hebbian pass, no promotion.
- `backend/tests/mcp_server/test_google_profile.py` — the policy checks: no description names another tool, schemas equal the core ones, a phrase blocklist for conversation content, instructions without a tool name or digest, read-only `recall`.

A profile is a view, not an authorization boundary ([Tool Profiles](mcp-tools.md#tool-profiles)), with one exception for this policy: On this view a `tools/call` naming `describe_tools` or `guide` is answered `unknown_tool`; every other registered tool stays callable under the usual role checks.
