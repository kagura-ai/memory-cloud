# MCP Tools Reference

See [MCP Client Setup](mcp-clients.md) for connecting a client, and [Core Concepts](concepts.md) for the memory model behind these tools.

64 tools across 13 categories. Workspace roles: **Owner** > Admin > Member > **Viewer** (read-only). Context roles: **Owner** > Editor > Viewer. Private contexts are visible only to the creator. Members may be restricted to specific contexts via allowlist.

## Tool Profiles

`tools/list` returns all 64 definitions by default. A client that loads every tool schema eagerly pays for the whole list in each session, so the endpoint URL — which the client's local MCP configuration already stores — can ask for fewer:

| Endpoint URL | `tools/list` returns | Approx. size |
|--------------|----------------------|--------------|
| `/mcp/w/{workspace_id}` (or `?profile=full`) | All 64 tools — the default, unchanged | ≈ 95k chars |
| `/mcp/w/{workspace_id}?profile=core` | The 12 core tools: `remember`, `update_memory`, `recall`, `reference`, `recall_upcoming`, `load_pinned`, `forget`, `explore`, `get_context_info`, `list_contexts`, `list_tags`, `feedback` | ≈ 32k chars (about 65% smaller) |
| `/mcp/w/{workspace_id}?tools=remember,recall,reference` | Exactly the named tools — an explicit allowlist, wins over `profile` | ≈ 15k chars for these three |

Sizes are the compact JSON of the `tools` array, measured at v0.78.0, which added a `title` and [annotations](#tool-annotations) to every tool (≈ 84k / 28k / 14k at v0.73.0, when the descriptions were trimmed; ≈ 111k / 45k / 23k at v0.72.0). Per-client instructions: [MCP Client Setup › List fewer tools](mcp-clients.md#list-fewer-tools).

- Tool names are comma-separated and case-sensitive; surrounding whitespace is trimmed, duplicates collapse, and at most 100 names are read. The result is always in registry order, whatever order the URL uses.
- Unknown names are ignored (and logged by the server), so a URL keeps working if a tool is later renamed or removed. If **no** name matches, or `profile` is anything other than `full` / `core`, `tools/list` fails with JSON-RPC `-32602` (invalid params) and a message naming the valid values.
- Both transports honour the parameters — session-based Streamable HTTP and stateless MCP 2026-07-28 — on `/mcp` as well as `/mcp/w/{workspace_id}`.

> **A profile is a view, not an authorization boundary.** It filters `tools/list` and nothing else. `tools/call` never reads it: a tool left out of the list stays callable by anyone whose role allows it. To restrict what a key can do, use workspace and context roles.

## Tool annotations

Every definition in `tools/list` carries a human-readable `title` and the standard MCP `annotations` object: the same `title` plus `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, all four sent on every tool (read-only tools send `destructiveHint: false` and `idempotentHint: true`). `annotations` exists since MCP 2025-03-26 and a top-level `title` since 2025-06-18; a client that does not know them ignores them. Together they add about 160 characters per tool, on every profile.

**Classification rule.** A tool is read-only when it changes nothing a user stored or can see. Changing stored memories, contexts, edges, files, secrets and grants, agents and bindings, settings, or learned state that changes later results is a modification. Usage and audit logging, access counters (`access_count`, `reference_count`, `last_used_at`) and sweeping already-expired state are not, although re-ranking and consolidation read those counters later. A tool is destructive when some argument can make it remove or overwrite existing data: soft delete, an overwritten value, a revoked grant and a graph edge reweighted or pruned all count, a tool that only adds rows does not. `idempotentHint` is true only when repeating a call with the same arguments changes nothing further; a call that restarts a relative expiry does not qualify. `openWorldHint` is true only for `setup_connector`, which stores a third-party chat platform's OAuth tokens for a connector that reads from it; the embedding, reranking and analysis model providers the server calls to process data it already holds do not count.

| Class | Tools |
|-------|-------|
| Destructive, safe to repeat | `create_edge`, `update_edge`, `delete_edge`, `update_context`, `delete_context`, `update_search_config`, `rollback_sleep_run`, `delete_file`, `update_agent`, `delete_agent`, `update_agent_binding`, `unbind_agent_context`, `secret_revoke_grant` |
| Destructive, not idempotent | `recall`, `get_agent_bootstrap` (learning writes, below), `update_memory` (`external_id` mode replaces the memory each call), `forget` (`query` mode deletes the next top-k), `merge_contexts`, `ingest_events`, `set_state` (`ttl_seconds` restarts the expiry each call), `secret_put` |
| Additive writes | `remember`, `feedback`, `record_measurement`, `create_context`, `setup_resource`, `setup_connector`, `analyze_context`, `init_file_upload`, `complete_file_upload` (idempotent), `register_agent`, `bind_agent_context`, `secret_register_pubkey` |
| Read-only | The other 31 tools |

- **`recall` is destructive.** It runs Hebbian learning over the memories it returns and promotes working memories that reach the promotion threshold; both change later results. The learning pass also overwrites and removes existing edges: it rewrites the weight of each edge it updates, including one you declared with `create_edge`, deletes an edge whose weight decays below the prune threshold, and evicts the weakest automatic edges past the per-memory cap. Under the rule that makes `create_edge` destructive, `recall` is too, and a repeat changes the weights again, so it is not idempotent. `get_agent_bootstrap` runs the same recall when given a `query`. A client that confirms destructive tools asks before these two as well. `reference` and `explore` only bump access counters; `load_pinned`, `load_guardrails`, `recall_upcoming` and `recall_nearby` write only usage and audit rows. All six stay read-only.
- `create_edge` is destructive because, on a pair that already has an edge, it applies your values over an automatic edge (and over a declared one with `overwrite=true`). `set_state` overwrites the value at its key, and with `ttl_seconds` each call restarts the expiry, so a repeat is not a no-op; `secret_put` revokes the grants the new version does not list. `secret_get` writes an audit entry and nothing else, so it is read-only.
- **Legacy `readOnly`.** The non-standard top-level `readOnly: true` of earlier releases is still sent for clients that read it, now derived from `readOnlyHint`: present exactly on the read-only tools. It is gone from `recall` and `get_agent_bootstrap` and new on `secret_get` and `secret_list`.
- **Hints, and the OAuth scope.** Annotations tell a client what a call does so it can decide when to ask for confirmation, and `readOnlyHint` also decides which [OAuth scope](#oauth-scopes) a call needs. The server's workspace and context role checks are unchanged, and a client may ignore the hints.

## OAuth scopes

With an OAuth access token, `tools/call` checks the token's scope before the tool runs, on session-based and stateless connections alike:

| Scope | Tools |
|-------|-------|
| `memory:read` | The 31 read-only tools (`readOnlyHint: true`), plus `recall` and `get_agent_bootstrap`: searches whose only writes are ranking updates |
| `memory:write` | Every other tool |

- A token without the scope gets HTTP `403` with `WWW-Authenticate: Bearer error="insufficient_scope", scope="…", resource_metadata="…"`. `scope` lists the scopes the token already has plus the missing one, so a client that re-authorizes with it keeps what it had. The body is a JSON-RPC error whose `data` carries `error: "insufficient_scope"`, `required_scope` and `help`; the code is `-32002` on session-based connections and `-32603` on stateless ones. The tool does not run. To recover, reconnect the server in the client and approve the missing scope.
- `initialize`, `tools/list`, `ping` and `server/discover` are not scope-gated, so a read-only token still lists every tool.
- `memory:delete` and `memory:admin` are not checked separately on MCP: deletes need `memory:write` like other writes, and the workspace and context role checks apply to every call as before.
- The scopes checked are the `memory:*` scopes in the token's stored scope (space- or comma-separated). A token whose stored scope names none — an empty scope, or one such as `openid offline_access` or a client-specific value — gets the `memory:*` scopes its OAuth client registered, or the DCR default scope (`openid memory:read memory:write memory:delete offline_access`) when the client registered none.
- API keys, agent-bound keys and session cookies carry no OAuth scope; only roles apply to them.

The 401 challenges, the token audience rule and session handling on `/mcp` are in [API Reference › Authentication and sessions on /mcp](api-reference.md#authentication-and-sessions-on-mcp).

## Memory (7)

| Tool | Description | Required Role |
|------|------------|---------------|
| `remember` | Store a new memory (summary + content + type; optional `delivery_mode`) | Member+ |
| `recall` | Search memories with Hybrid Search (supports `trust_tier` filter). Results are Layers 1-2; `related_tags` is `[{tag, count}]` | Viewer+ |
| `recall_nearby` | Deterministic WHERE-axis query — memories with `details.location` within `radius_m` of a point, nearest first | Viewer+ |
| `reference` | Get full 3-layer details of a memory | Viewer+ |
| `update_memory` | Update an existing memory in-place or upsert by external ID | Member+ |
| `forget` | Soft-delete a memory (retention bounded by the deployment's cleanup window, default 30 days) | Member+ |
| `explore` | Discover related memories via Neural Memory graph | Viewer+ |

> **Response format.** Every tool returns one JSON text block, serialized as compact UTF-8 — non-ASCII text (e.g. Japanese) arrives as-is, never as `\uXXXX` escapes, because the calling model pays for every character. Fields that are empty on most results are omitted rather than sent as `null` / `[]`: a `recall` result carries `context_summary`, `superseded_by`, `contradicts` and `supersede_candidate` only when they have a value, and `score` is rounded to 4 decimals. Treat an absent key as "none". The authoritative per-tool shape is the `Returns:` line of each tool description (`tools/list`).

## Agent Substrate (8)

The primitives an autonomous agent loop needs beyond a knowledge store — see [Concepts › Agent Memory Substrate](concepts.md#agent-memory-substrate).

| Tool | Description | Required Role |
|------|------------|---------------|
| `load_pinned` | Deterministically load always-load memories (`delivery_mode="always"`) — Goal / Guardrail / policy | Viewer+ |
| `load_guardrails` | Deterministically load a context's guardrail set for a client-side hook — the trusted-tier pinned set plus memories marked with `details.tool_trigger`, each lane capped on its own. See [Tool guardrails](#tool-guardrails) | Viewer+ |
| `recall_upcoming` | List upcoming Time Memories (`type="time"` — the lane is keyed on the type; the write path never sets `delivery_mode="on_trigger"`). Items are `{memory_id, summary, type, trigger}`; `include_details=true` returns the full `details` instead of `trigger` | Viewer+ |
| `set_state` | Upsert agent scratch state (key→value, optional TTL; excluded from recall) | Editor+ |
| `get_state` | Read one state key, or list all live state for a context | Viewer+ |
| `record_measurement` | Append one numeric observation to a metric's series (HOW-MUCH lane; excluded from recall, untouched by Sleep) | Editor+ |
| `recall_series` | Read a metric's series bucketed by day/week/month with avg/min/max/sum/count/last | Viewer+ |
| `feedback` | Record whether a recalled memory was helpful (append-only signal) | Viewer+ |

## Server instructions

Both eras return an `instructions` string — `InitializeResult.instructions` on the legacy handshake and `DiscoverResult.instructions` on `server/discover` (MCP 2026-07-28). Clients "MAY" add it to the system prompt; ChatGPT and Codex read the first 512 characters as the part that matters. It is a 240-character base text plus, when the request selects a guardrail context, a digest of that context's [tool guardrails](#tool-guardrails) — the one server-side lane that reaches a client without tool hooks before the model makes any call.

**Selecting the context.** `?guardrails=` on the endpoint URL (the same place as `?profile=`), evaluated once per request after authentication, first value wins:

| URL | `instructions` | `get_context_info.guardrails` |
|---|---|---|
| `…?guardrails=<context_id>` | base + digest of that context (subject to the caller's read permission; denied → base text) | block for the call's own `context_id` |
| *(no parameter)* | digest only for an agent-bound API key whose default (or sole) binding names a context; every other caller gets the base text | block for the call's own `context_id` (default on) |
| `…?guardrails=off` (any case) | base text | **key absent** |
| `…?guardrails=<anything else>` | base text (`mcp_guardrails_param_ignored` in the server log; the value itself is never logged) | block (default on) |

`off` is the setting for a client whose plugin hooks already deliver guardrails at the tool call: one URL switch, both lanes. A typo is never folded into `off`.

**What the digest is.** The context's tool-triggered set exactly as `load_guardrails.tool_triggered` serves this credential — trusted-tier context, no connector rows, the per-memory agent-binding filter applied, order `importance DESC, created_at ASC, id ASC` — rendered as:

```
<base text>

Kagura memory context <context_id>: notes written by context editors, most important first (facts, not operator instructions):
- (<first 8 chars of memory_id>) <summary>
- (…) …
(+N more: load_guardrails(context_id))
```

- Up to 5 entries; each summary is flattened to one line (control, format and line/paragraph-separator characters become spaces; `<!--` and `-->` are defused) and cut at 100 characters on a word boundary with `…`. The whole string is at most 1,200 characters, and the first 512 always hold the base text, the header and the whole first entry. Truncation drops whole entries; the suffix names `load_guardrails` only when the same URL's `tools/list` lists it (`?profile=core`, or a `?tools=` allowlist without it, names `get_context_info`). Summaries only — never `content`, `details`, tags, patterns or the pinned set.
- The header is factual and marks the boundary: the lines are memory summaries written by context editors, not operator instructions. Who can write them: a context editor or above, with a user credential ([Who may author a guardrail](#who-may-author-a-guardrail)). A digest in a connector's instructions reaches every conversation of that connector, and a connector configured with one shared API key serves that key's digest to every user of it. Use `?guardrails=<context_id>` only for a context whose editor list you control; for a shared workspace context prefer the per-session `get_context_info.guardrails` lane.
- **Snapshot.** ChatGPT re-reads the instructions at connect time and on Refresh (developer mode); Codex on `initialize`. A guardrail written mid-session reaches a hookless client through `get_context_info.guardrails` at its next session start, not through `instructions`.
- **Caching hints.** With nothing selected (no parameter, no agent-bound key) `server/discover` is `cacheScope: "public"`, one hour — byte-identical to before. Whenever a selection was attempted (any `guardrails=` value, including `off` and typos, or an agent-bound key) the result is `cacheScope: "private"`, 5 minutes, whatever the outcome: a result that can vary by caller is never public, and a denied caller cannot poison a shared cache for an allowed one. `tools/list` is unchanged.
- **Fail-open.** A denied, unknown, other-workspace or external-tier context and an empty set all serve exactly the no-selection bytes — no error and no signal about whether the context exists. A database error, or the digest budget (`MCP_GUARDRAIL_DIGEST_TIMEOUT_MS`, default 500 ms) expiring, serves the base text and never fails the handshake (`mcp_guardrail_digest_failed` warning). `MCP_GUARDRAIL_DIGEST_ENABLED=false` turns the `instructions` lane off deployment-wide; the `get_context_info` block and the export route stay served. With nothing selected the handshake opens no database session.
- **Audit.** A resolver deny for an agent credential writes the `memory_access_events` deny row (`operation: "load_guardrails"`), bounded by the client's 5-minute private cache; a served digest writes nothing — `mcp_guardrail_digest_served context_id=… entries=… era=… selection=explicit|binding` in the server log is the diagnostic.
- **Which lane is always visible.** `instructions` and the export block ([`GET /api/v1/memory/guardrails/digest`](api-reference.md#get-apiv1memoryguardrailsdigest), the Codex cloud lane) are read before the model acts. `get_context_info.guardrails` is visible only when the model follows the skill's session-start step, and only for that session — a client on `?profile=core` without `?guardrails=` has that lane alone. Preview what a credential receives with `GET /api/v1/memory/guardrails/digest?context_id=<uuid>&target=instructions`.

## Tool guardrails

A **tool guardrail** is a memory that a client-side hook injects into the model's context at the moment a matching tool call happens — before the call as a deny reason, or next to its result. The server owns two things: the `details.tool_trigger` marking, validated on every write, and the deterministic `load_guardrails` read whose result every client caches. **Matching happens only in the client.** The server compiles each pattern once to validate it and never runs it against any input; the read lane returns the pattern as data. This section is the client-neutral contract every adapter (Claude Code hooks, Codex hooks, hookless digests) implements verbatim.

### Marking a memory — `details.tool_trigger`

```json
"details": {
  "tool_trigger": {
    "tool":   "Bash|PowerShell",
    "on":     "pre",
    "match":  "gh pr merge\\b.*--delete-branch",
    "action": "inform"
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `tool` | yes | Regex, **full match** against the tool name the client reports (or one of its documented aliases). ≤ 128 characters. Examples: `Bash|PowerShell`, `mcp__.*__remember`, `Edit|Write`. |
| `on` | no (default `pre`) | `pre` — before the call; `result` — after it, against the tool's error text or serialized result. |
| `match` | no | Regex, **unanchored search** over the match subject (below). ≤ 200 characters. An empty string is rejected (it would match everything). |
| `action` | no (default `inform`) | `inform` — the summary reaches the model next to the tool result; `block` — the call is denied with the summary as the reason. `block` requires `on: "pre"` **and** a `match` that names at least one literal character and cannot match the empty string (a block always names a specific input, never a whole tool — `a*` or `rm?` would deny every call). |

- The key is **orthogonal** to `type` and `delivery_mode` (like `details.location`): any type may carry it, and a guardrail may also be pinned — it then appears in both lists of `load_guardrails`.
- The server **normalizes** what it stores: defaults are written back, keys are ordered `tool, on, match?, action`, `match` is omitted when not supplied (never stored as `null`). Every consumer therefore sees explicit values and never re-implements defaults.
- **Unmark** with `"tool_trigger": null` (the key is removed) or by resending `details` without the key. `details` is replaced wholesale on `update_memory` / `PATCH` — resend `tool_trigger` when you update details, or it is dropped.
- Connector-ingested content can never become a guardrail: the ingest path strips `tool_trigger`, and the read lane below excludes connector rows anyway.
- Sleep maintenance never merges, archives or re-scores a guardrail; `load_guardrails` orders by the importance the author set.

### Who may author a guardrail

A guardrail is injected into every member's agent session without appearing in the chat, so it is held to a stricter rule than an ordinary memory write:

- **Context editor or above** — a workspace owner/admin, a context member with the editor or owner role, or the creator of a private context. The rule applies to adding, changing or removing `tool_trigger`, and to **any** edit or delete of a memory that already carries one (a summary rewrite changes what gets injected). MCP returns `permission_denied` (`required_role: "editor"`), REST returns `403`; `forget` keeps its silent contract — a guardrail the caller may not delete is skipped, whether named by `memory_id` (`deleted_count: 0`) or matched inside a `forget(query=…)` sweep (the sweep deletes its other matches and does not count the guardrail).
- **A user credential** — an agent-bound API key can never write `tool_trigger` (`tool_trigger_requires_user_credential`, a `validation_error` / `422`): the row records no agent identity, so a guardrail written by an automation that ingested untrusted content would be indistinguishable from a human one.
- A `tool_trigger` written into a context whose `trust_tier` is not `trusted` is accepted (the memory is valid) but **never served** by `load_guardrails`.
- `context_is_locked` protects the context from deletion only; it does not freeze its guardrail set.
- A `supersedes` edge shadows a memory out of `recall` but does **not** affect guardrail delivery — `forget` it or remove `tool_trigger`.
- `merge_contexts` (owner-only, same workspace) copies `details` verbatim, so guardrails written into an external-tier context become servable once merged into a trusted one.

### Validation — error codes

Bad input is a `validation_error` on MCP and a `422` on REST. The message is `invalid details.tool_trigger: <code>: <sentence>`; the `<code>` token is stable, the sentence may change.

| Code | Rule |
|---|---|
| `tool_trigger_not_object` | `details.tool_trigger` must be a JSON object |
| `tool_trigger_unknown_key` | keys ⊆ `{tool, on, match, action}` |
| `tool_required`, `tool_not_string` | `tool` is a required non-empty string |
| `match_not_string`, `match_empty` | `match`, when present, is a non-empty string |
| `pattern_too_long` | `tool` ≤ 128 characters, `match` ≤ 200 |
| `on_invalid`, `action_invalid` | `on` ∈ `{pre, result}`, `action` ∈ `{inform, block}` |
| `block_requires_pre`, `block_requires_match`, `block_match_not_specific`, `block_match_nullable` | `block` needs `on: "pre"` and a `match` with at least one literal character that cannot match the empty string (`a*`, `a?`, `a{0,100}`, `(?:x\|y*)` are rejected; `a+`, `a*b` pass) |
| `pattern_control_char` | no U+0000–U+001F in a pattern, raw or escaped (`\x00`–`\x1f`, `\u0000`) |
| `tool_trigger_requires_user_credential` | see "Who may author" |
| `regex_*` | the safe-regex subset below |

### Safe-regex subset — what the server accepts

`tool` and `match` share one grammar: the subset that compiles and behaves the same in Python `re` and JavaScript `RegExp`, with no nested quantifiers and no ambiguous split between two unbounded runs — the two sources of super-linear backtracking. Everything not listed is rejected. The grammar does not make a backtracking engine linear in every case (a search still retries each start position), which is why the client's 8 KB subject cap and per-pattern budget below stay normative.

```
pattern     := [ "(?i)" ] alternation          ; the only inline flag, only at offset 0
alternation := sequence ( "|" sequence )*
sequence    := ( atom quantifier? )+
atom        := literal | "." | "^" | "$" | escape | class | group
group       := "(" alternation ")" | "(?:" alternation ")"
class       := "[" "^"? class_item+ "]"        ; literals, ranges, \d \D \w \W \s \S and escaped metacharacters
quantifier  := ( "*" | "+" | "?" | "{n}" | "{n,}" | "{n,m}" ) "?"?    ; lazy suffix allowed
escape      := "\" ( metachar | d D w W s S b B n t r f v | "x" HH | "u" HHHH )
metachar    := one of  \ . * + ? ( ) [ ] { } | ^ $ / -
```

| Rejected | Code |
|---|---|
| backreferences `\1`–`\9`, `\k<…>` | `regex_backreference` |
| lookaround `(?=` `(?!` `(?<=` `(?<!` | `regex_lookaround` |
| named groups `(?P<n>…)`, `(?<n>…)`, `(?P=n)` | `regex_named_group` |
| atomic groups `(?>…)`, possessive quantifiers `*+` `++` `?+` `{n,m}+` | `regex_possessive_or_atomic` |
| inline flags other than one leading `(?i)` — `(?s)`, `(?m)`, `(?x)`, `(?i:…)`, `(?-i)`, a second `(?i)`, `(?#comment)` | `regex_inline_flag` |
| escapes outside the table — `\A` `\Z` `\z` `\G` `\p{…}` `\0` octal `\Q`, malformed `\x` / `\u`, a lone surrogate `\uD800`–`\uDFFF`, `\b` inside `[…]` | `regex_unknown_escape` |
| a repetition bound above 100 (`{101}`, `{2,101}`, `{101,}`), or `n > m` | `regex_bound_too_large`, `regex_bound_inverted` |
| a quantifier on a group whose body contains a quantifier or `|` — `(a+)+`, `(a|ab)*`, `(x(y*))?`, even `(a|b)?` | `regex_nested_quantifier` |
| stacked quantifiers `a**`, `a+*`, `a{2}{3}`; a quantifier with nothing to repeat `*abc`, `(*)`, `^*`, `\b+` | `regex_stacked_quantifier`, `regex_dangling_quantifier` |
| two **unbounded** quantifiers (`*`, `+`, `{n,}`) with no mandatory atom between them — `.*.*`, `\w+\s*\w+`, `a+b+`, `(?:a+)b*`; zero-width atoms (`^ $ \b \B`) and nullable atoms (`?`, `{0,m}`) do not count as separators | `regex_adjacent_unbounded` |
| an unbounded quantifier that is followed by another one must be closed by an atom it cannot match itself — `\w+-\w+=` and `git\s+push\s+--force` are fine (`-` ∉ `\w`, `p` ∉ `\s`), `\w+a\w+=`, `.*a.*b` and `.*-.*=` are not (the first run's end is not forced: > 20 s on an 8 KB subject in Python and JavaScript alike). Groups count by their first characters; `(?i)` folds case | `regex_ambiguous_separator` |
| more than 4 unbounded quantifiers in one pattern; groups nested more than 8 deep | `regex_too_many_unbounded`, `regex_nesting_too_deep` |
| set operations or nesting inside `[…]` (`[[a]]`, `[a&&b]`, `[a--b]`, `[a~~b]`, `[a||b]`); an empty class `[]` / `[^]`; an empty group `()` / `(?:)` | `regex_class_unsupported`, `regex_class_empty`, `regex_empty_group` |
| anything else that is not in the grammar — unbalanced brackets, `a{`, `a{,5}`, a trailing `\`, an empty alternative `a|` | `regex_syntax` |

Accepted, for calibration: `Bash|PowerShell`, `mcp__.*__remember`, `Edit|Write`, `gh pr merge\b.*--delete-branch`, `(?i)git\s+push`, `git (?:pull|merge) --ff-only`, `a{2,100}`, `[^\s]+\.py$`, `git\s+push\s+--force` (a mandatory literal the first `\s+` cannot match separates the two `\s+`), `a+-b+`, `\w+-\w+`, `\d{4}-\d{2}`. Bounded repeats (`{n,m}`, m ≤ 100) are exempt from the adjacency rule — measured harmless even when adjacent and overlapping. `{n,}` is allowed with n ≤ 100 (it is `a{n}a*`). When a second unbounded quantifier is needed, put a character the first one cannot consume right after it (`-`, `/`, `=`, a space after `\S+`); `.*` can be followed by another `.*` only across a literal newline.

### Python / JavaScript matching deltas

The grammar guarantees that a pattern *compiles* on both sides; it does not make every construct *match* identically. Clients match with the JavaScript-equivalent semantics: **ASCII** `\d` `\w` `\s`, `$` only at the very end of the subject, and `.` excluding line terminators. A Python client compiles with `re.ASCII` and matches against the subject with trailing newlines stripped.

| Construct | Python `re` (str) | JavaScript `RegExp` (no `u`) |
|---|---|---|
| `abc$` against `"abc\n"` | matches (also before a trailing newline) | does not match |
| `\d` `\w` `\s` | Unicode-aware (`٣` is a digit) | ASCII only |
| `.` | everything but `\n` | everything but `\n` `\r` U+2028 U+2029 |

### `load_guardrails` — the deterministic read

MCP `load_guardrails(context_id, cap?)` and the REST twin `POST /api/v1/memory/guardrails` with body `{"context_id": "<uuid>", "cap"?: 1..1000}`. Read-only, rate-limit exempt, plain SQL — no search, no ranking, no embedding, no vector-store call, no Hebbian write.

```json
{
  "status": "success",
  "format": 1,
  "version": "3f9c1a7b2d4e6f80",
  "pinned":         [ <item>, ... ],
  "tool_triggered": [ <item>, ... ],
  "total_available": 7, "truncated": false, "cap": 50,
  "pinned_cap": 100, "pinned_total_available": 4, "pinned_truncated": false,
  "tool_triggered_total_available": 3, "tool_triggered_truncated": false,
  "context_id": "550e8400-e29b-41d4-a716-446655440000", "context_name": "...",
  "context_display_name": "...", "context_is_private": false, "context_is_locked": false
}
```

`item = {memory_id, summary, context_summary, type, importance, delivery_mode, tool_trigger, source_type, authored_by_caller, created_at, updated_at}` — one shape for both lists.

- **Two lanes, two caps.** `pinned` is the trusted-tier pinned set (`delivery_mode="always"`), bounded by the server's `pinned_load_cap` (default 100) — byte-identical to the agent-bootstrap pinned lane. `tool_triggered` is every memory carrying `details.tool_trigger`, bounded by `cap` (default `guardrail_load_cap`, 50; hard maximum 1000). The request `cap` applies to `tool_triggered` only, so a large pinned set can never crowd guardrails out of a capped response.
- **Order** inside each list: `importance DESC, created_at ASC, id ASC` — deterministic down to the id, so the cap and every consumer cut the same entries. Consumers keep this order and must not re-sort.
- **Both lists.** A memory that is both pinned and tool-triggered appears in both lists; its `pinned` entry has `tool_trigger: null`, its `tool_triggered` entry carries the object. Clients dedupe by `memory_id` and inject once.
- `total_available` = `pinned_total_available + tool_triggered_total_available`; `truncated` = either lane truncated; `cap` = the tool-triggered cap. The per-lane fields say which protection is incomplete. Totals are the context's set sizes before the binding filter.
- **Trusted only, unconditionally.** Both lanes apply `Context.trust_tier == "trusted"` AND `source_type != "connector"`. There is no parameter to turn this off from any surface.
- **Layers.** Pinned items carry L1 + L2 (`summary`, `context_summary`); tool-triggered items are L1 only (`context_summary` is `null`). Never `content`, never `details` beyond `tool_trigger`, never tags or scores.
- **Provenance.** `source_type` and `authored_by_caller` (the caller wrote this row) let a client label a foreign-authored guardrail; `updated_at` falls back to `created_at`.
- **Guards.** Uniform `context_not_found` on any deny; the per-memory agent-binding filter narrows what an agent credential receives (`version` is computed after it, so it is per-credential). `memory_access_events` rows (`operation: "load_guardrails"`) are written for agent credentials only; human and API-key calls appear in tool-usage logging.
- **Profiles.** The tool is not in the `core` profile. Hooks call it through `tools/call`, which ignores the profile; a client on `?profile=core` cannot have the *model* call it, so a hookless digest for such clients has to travel through `get_context_info` or the server `instructions`, not through the skill calling the tool.

### Shared cache format (`format: 1`)

Every adapter writes and reads this one file in its own data directory (for example `<data>/guardrails/<context_id>.json`, written through a temporary file and a rename, mode `0600`, after validating `<context_id>` as a UUID):

```json
{
  "format": 1,
  "context_id": "550e8400-e29b-41d4-a716-446655440000",
  "fetched_at": "2026-09-22T09:00:00Z",
  "version": "3f9c1a7b2d4e6f80",
  "pinned": [
    {"memory_id": "…", "summary": "…", "importance": 0.9}
  ],
  "tool_triggered": [
    {"memory_id": "…", "summary": "…", "importance": 0.8,
     "tool_trigger": {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge\\b.*--delete-branch", "action": "inform"}}
  ]
}
```

**Shape.** Top-level keys are exactly `format`, `context_id`, `fetched_at`, `version`, `pinned`, `tool_triggered`. Item keys are exactly `memory_id`, `summary`, `importance`, plus `tool_trigger` on tool-triggered items. One optional additive item key is allowed on either list: `authored_by_caller` (boolean, copied from the response so a hook can label a foreign-authored guardrail); a consumer that does not know it ignores it, and its absence means "unknown", not `false`. No `content`, no `context_summary`, no tags. A pinned entry never carries `tool_trigger`; a memory in both lists is stored in both. `fetched_at` is the client clock in UTC (`Z`); the server does not supply it. Items are stored in the server's order.

**Forward compatibility.**

- Additive top-level or item fields never bump `format`. Consumers ignore unknown keys at every level (response, item, `tool_trigger`).
- A consumer **skips** (never fails on) an item whose `on` or `action` is a value it does not know, whose `tool_trigger` is not an object, or whose pattern its engine cannot compile. That is how a future `on` value ships without breaking installed hooks.
- `format` bumps only when an existing field changes meaning or is removed. A consumer that sees `format` greater than it knows treats the cache as absent (fail-open).
- `version` is opaque: equal means the served set is unchanged; compare `memory_id` sets and summaries to name what changed. For the record, the server computes it as `sha256(json.dumps(entries, sort_keys=True, separators=(",", ":"), ensure_ascii=False)).hexdigest()[:16]` over `[memory_id, summary, importance, delivery_mode, tool_trigger]` per served item, pinned list first, after the binding filter. The hookless digest surfaces — `get_context_info.guardrails.tool_triggered_version`, the export block's begin marker and the `X-Kagura-Guardrails-Tool-Triggered-Version` header of `GET /api/v1/memory/guardrails/digest` — carry **`tool_triggered_version`** instead: the same function over the tool-triggered items only, after the binding filter, in server order, over the whole set up to `guardrail_load_cap` (not only the entries a digest renders) — the value a client computes over `load_guardrails.tool_triggered` alone. It differs from `version` whenever the context has a trusted pinned memory, and the bare name `version` never appears in a digest; an empty set hashes to `4f53cda18c2baa0c`.

**Matching (normative, client-side).**

- `tool` is a full match against the reported tool name or a documented alias (Claude Code and Codex report shell calls as `Bash` and MCP tools as `mcp__<server>__<tool>`; Codex's `apply_patch` also matches `Edit` and `Write`).
- `match` is an unanchored search over the **first 8 KB** of the subject. `pre`: the command for a shell tool, the file path with `\` turned into `/` for a file tool, the compact JSON of the arguments for any other tool. `result`: the tool's error text or serialized result. A call can yield several subjects; a trigger matches when any of them matches.
- A leading `(?i)` is stripped and mapped to the engine's case-insensitive flag.
- The 8 KB subject cap and a per-pattern time budget are **normative**: the server bounds the patterns, but only the client bounds the engine. A pattern that exceeds the budget is skipped, and the hook as a whole fails open.
- Recommended injected form: `Kagura Memory guardrail (<first 8 chars of memory_id>): <summary>`, ≤ 500 characters per memory — a factual statement with provenance, never an imperative.

**Client adapters.** Every adapter runs the same core rules; only the event mapping and the delivery lane differ.

| Client | Delivery lane | Events → `on` | `pre` subjects (`tool_name` → aliases → subjects) | `result` subject | Deny / inform mapping |
|---|---|---|---|---|---|
| Claude Code (`kagura-memory` plugin hooks, `claude-hooks/hooks.json`) | hooks: one-time `permissionDecision: "deny"` with the memory as `permissionDecisionReason`, or `additionalContext` | `PreToolUse` → `pre`; `PostToolUse` (`tool_response`) and `PostToolUseFailure` (`error`, the lane for this server's `isError` results) → `result` | `Bash`, `PowerShell` → no aliases → `[tool_input.command]` when a string, else `[]`; `Write`, `Edit`, `Read` → `[tool_input.file_path]` with `\` → `/`; `NotebookEdit` → `[tool_input.notebook_path]` likewise; `mcp__<server>__<tool>` and every other tool → `[compact_json(tool_input)]` | `result_subject(tool_response)` / `result_subject(error)` | `block` (with the plugin's `max_action: block`) → deny once per `(session, agent, memory)`; everything else → context |
| Codex CLI / IDE / ChatGPT desktop (Codex plugin hooks, `plugins/kagura-memory/hooks/hooks.json`, #1620) | hooks: same output shapes as Claude Code | `PreToolUse` → `pre`; `PostToolUse` (`tool_response`) → `result` | `Bash` → `[tool_input.command]`; `apply_patch` → aliases `Edit`, `Write` → one subject per path from lines starting with `*** Add File: `, `*** Update File: `, `*** Delete File: `, `*** Move to: `, stripped, `\` → `/`; `mcp__<server>__<tool>` and every other tool → `[compact_json(tool_input)]` | `result_subject(tool_response)` | as Claude Code |
| Hookless clients (Claude Desktop / Chat, ChatGPT web, Codex cloud, Cursor, Gemini CLI, any other MCP client) | server lanes (#1621): `get_context_info.guardrails` at session start, the `instructions` digest when `?guardrails=<context_id>` selects a context, an `AGENTS.md` block for Codex cloud | none (no per-call matching) | — | — | the summaries are shown up front; nothing is denied |

Normative for every hook adapter (the server bounds the patterns, the client bounds the engine):

- `compact_json(v) = json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))` — sorted keys, so a pattern can rely on `"context_id":…` preceding `"summary":…` on every client.
- `result_subject(value)`: a string is used as is; any other JSON value is walked depth-first (objects in sorted key order, lists in order) and every string leaf is joined with `\n`; when the value is an object whose top-level `isError` is `true`, `\nisError=true` is appended. So a Claude Code Bash `tool_response` `{stdout, stderr, interrupted, isImage}` yields `stderr\nstdout`, and an MCP `CallToolResult` yields its `content[*].text` strings — `"status": "error"` inside the envelope text matches on every client.
- Subject normalisation before matching: `\r\n` → `\n`, trailing `\n`/`\r` stripped, then the first **8,192 characters**. Compile with the engine's ASCII semantics (`re.ASCII` in Python), a leading `(?i)` mapped to the case-insensitive flag and no other flag, so `$` matches only at the very end and `.` excludes `\n`.
- Two-phase compile: `tool` is compiled and full-matched first against the tool name and each alias; `match` is compiled only for items whose `tool` matched, then searched over each subject; an item without `match` fires on the tool match alone.
- Budget: **200 ms per pattern** (`signal.setitimer` in Python; a pattern that trips is skipped for the call and, after two trips, for the session) and **1,000 ms per call**; over budget → whatever matched so far is delivered and the hook fails open. Items whose pattern the engine cannot compile, whose `on`/`action` is unknown or whose `memory_id` is not a UUID are skipped, never fatal.
- Rendering: one framing line, then `Kagura Memory guardrail (<id8>[, by another member]): <summary>` lines — block lines first, then inform lines — with the summary flattened (control, format and line/paragraph-separator characters → one space, whitespace runs collapsed, `<!--`/`-->` defused) and cut at 500 characters on a word boundary. A deny reason ends with one factual trailer saying the hook did not evaluate the call and will not repeat the deny for the same guardrail in the session.
- Per call: every live `block` candidate (cache order), then `inform` candidates until the output holds 3 lines; the client's budget (Claude Code 9,000 characters of `additionalContext` / `permissionDecisionReason`, Codex 2,000 tokens) is applied to that list **before** any once-per-key marker is taken, dropping from the bottom, so a guardrail the output could not carry stays unmarked and is delivered at a later matching call — a `block` cut this way denies the re-issued call once more.

### Authoring guidance

- One specific tool call per guardrail. Use `Bash|PowerShell` for shell traps; `mcp__.*__remember` for a write-tool trap.
- Write the `summary` as the safe alternative, stated as a fact — it is the text the model reads.
- Use `block` only when the call itself does the damage (hangs, destroys, is irreversible). Otherwise `inform`.
- Test the pattern against the command that actually failed before storing it.
- Keep a context at 20 or fewer tool guardrails; `load_guardrails` caps the lane at 50 by default.

## Agent Control Plane (10, preview)

The v0.49.0 control plane builds on existing workspace RBAC: agents are registry resources, not principals, and context bindings can only remove access from an agent-bound member key. Registry, bindings, composed bootstrap, W3C Trace Context/baggage correlation, and the append-only audit foundation are implemented.

| Tool | Description | Required Role |
|------|------------|---------------|
| `register_agent` | Register a workspace-scoped agent | Owner/Admin |
| `list_agents` | List registered agents and lifecycle/enforcement status | Owner/Admin |
| `get_agent` | Get one registered agent | Owner/Admin |
| `update_agent` | Update metadata, `status`, or `enforcement_mode` | Owner/Admin |
| `delete_agent` | Permanently delete an agent and its bound keys; prefer `status="retired"` operationally | Owner/Admin |
| `bind_agent_context` | Add a purely subtractive context binding | Owner/Admin |
| `list_agent_bindings` | List an agent's context bindings | Owner/Admin |
| `update_agent_binding` | Update read/write/default binding policy | Owner/Admin |
| `unbind_agent_context` | Remove a binding (default-deny in enforce mode) | Owner/Admin |
| `get_agent_bootstrap` | Compose context guide + pinned + optional trusted recall + upcoming + state for session start | Agent-bound key or Owner/Admin |

> **Preview boundary:** per-memory type/source filters are enforced on the memory-read lanes (recall, recall_nearby, reference, forget, explore, load_pinned, upcoming) for enforce-mode agents as of [#1299](https://github.com/kagura-ai/memory-cloud/issues/1299) — and on the enumeration/aggregate surfaces (list, stats, access-patterns, get_cluster) as of [#1301](https://github.com/kagura-ai/memory-cloud/issues/1301) — `null` = all types, `[]` = deny-all; shadow mode records `would_deny` without filtering. `traceparent` plus W3C baggage correlation for `agent_id` / `session_id` / `run_id` is implemented, but server-side span export remains out of scope for P0. `memory_access_events` is live for bootstrap, load-pinned, feedback, recall, reference, remember, update, and forget with binding deny / `would_deny` persistence.

## Neural Edges (4)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_edges` | List edges connected to a memory | Viewer+ |
| `create_edge` | Create an edge between two memories | Member+ |
| `update_edge` | Update edge weight or type | Member+ |
| `delete_edge` | Delete an edge between two memories | Member+ |

## Contexts (7)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_context_info` | Get context metadata and guidelines | Viewer+ |
| `list_contexts` | Slim name→id directory of the contexts you can access, most recently used first (details are opt-in — see below) | Viewer+ |
| `create_context` | Create a new context | Owner/Admin |
| `update_context` | Update context settings (summary, usage guide, resource_id, is_public) | Editor+ |
| `delete_context` | Delete a context and all its memories | Owner/Admin |
| `merge_contexts` | Merge memories from source context into target context | Owner/Admin |
| `update_search_config` | Tune hybrid search weights, reranker settings, and query-intent routing (`routing_mode`) per context | Editor+ |

### `list_contexts` response shape

`list_contexts()` exists to turn a context **name** into an **id**, so by default each item is just `{id, name, is_private, is_locked, last_used_at}` — no `summary`, no `embedding_model`. A workspace with dozens of contexts stays a few thousand characters instead of overflowing an MCP client's tool-result limit. For one context's full summary, usage guide and search config call `get_context_info(context_id)`.

| Parameter | Effect |
|-----------|--------|
| `name_contains` | Case-insensitive substring match on the context name or display name (trimmed, ≤100 characters; blank = no filter). No match is a success with an empty list. |
| `include_stats` | Adds `memory_count` per item. |
| `include_summary` | Adds `summary` truncated to 300 characters; items that were cut also carry `summary_truncated: true` (a null summary stays null). |
| `include_details` | Adds the full `summary` (up to 2,000 characters each) and `embedding_model` — the previous default item shape ([#1600](https://github.com/kagura-ai/memory-cloud/issues/1600)). Wins over `include_summary`; combine it with `name_contains`. |

Envelope: `{status, contexts, count, total, limit, can_create}`, plus `hint` on an empty list (below). `count` is the number of contexts in the workspace (quota usage against `limit`; it can exceed what you are allowed to see and is not affected by `name_contains`), `total` is the number of contexts in this response. A non-boolean flag or an over-long `name_contains` returns a `validation_error`; an explicit `null` for any parameter is treated as omitted.

When you can see no context at all, the envelope also carries `hint`: one line saying that a workspace owner can create one with `create_context(name=...)` and an admin with `create_context(name=..., is_private=false)` (only owners can create private contexts, the default), that a member can ask an owner or admin for a context or for access, and that a client whose tool list has no `create_context` (for example under `?profile=core`) can create it in the web UI or reconnect without `?profile=core`. With no current workspace, `create_context` would fail with `workspace_required`, so the hint instead says to create or select a workspace in the web UI and call `list_contexts` again. It is absent whenever at least one context is visible, including when `name_contains` matches nothing, and when the access lookup itself failed (that still answers an empty list, as before, but is not an empty account). No context is ever created automatically ([#1658](https://github.com/kagura-ai/memory-cloud/issues/1658)).

## Tags (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_tags` | List tag vocabulary in a context (call before remember/recall to align tagging) | Viewer+ |

## Files / R2 Attachments (5)

| Tool | Description | Required Role |
|------|------------|---------------|
| `init_file_upload` | Reserve quota + return presigned PUT URL (R2, ≤100 MiB) | Member+ |
| `complete_file_upload` | Finalize upload after R2 PUT, verify sha256, mark as uploaded | Member+ |
| `list_files` | List uploaded, non-deleted files in the workspace (newest first) | Viewer+ |
| `get_file_download_url` | Issue presigned GET URL for a file | Viewer+ |
| `delete_file` | Soft-delete a file object | Member+ |

## Analyses — Memory Analysis (5)

Cluster memories into themes (kouchou-ai-style UMAP + KMeans + LLM labeling) for large-scale qualitative analysis.

| Tool | Description | Required Role |
|------|------------|---------------|
| `analyze_context` | Start an analysis run (or preview cost with `dry_run=true`) | Owner + Pro plan + BYOK + quota |
| `list_analyses` | List past analysis runs for a context | Owner |
| `get_analysis` | Get a completed analysis (clusters, labels, stats) | Owner |
| `get_active_analysis` | Get the in-flight analysis for a context (if any) | Owner |
| `get_cluster` | Drill into a single cluster's member memories | Owner |

Analysis runs are capped by `ANALYSIS_MAX_MEMORY_COUNT` (default 10,000; preview and start reject larger contexts). Since v0.47.0, cancellation is all-or-nothing, deleted-context runs are invisible on both REST and MCP, strict BYOK labeling never falls back to the platform key, and runs fail when more than half of labelable clusters fail labeling.

## Resources — External Data Ingestion (6)

| Tool | Description | Required Role |
|------|------------|---------------|
| `setup_resource` | Create public context + issue resource token | Owner/Admin + plan with the `resources` feature (XL) |
| `setup_connector` | Provision an ai-worker chat connector (resource + connector row + token) | Owner/Admin + plan with the `connectors` feature (XL); connector seat cap applies second |
| `list_resource_tokens` | List active resource tokens for your workspace | Owner/Admin |
| `ingest_events` | Batch upsert/delete events into a resource (max 100 events; session-auth MCP variant) | Member+ |
| `get_resource_impact` | Resource stats (tokens, memories, schema version) | Viewer+ |
| `get_resource_schema` | Field definitions for a resource | Viewer+ |

The `resources` / `connectors` feature gates (#1551) apply to **new creation only**: resources, resource tokens and connectors that already exist on a lower plan keep working — they stay listed, served, editable and ingestible (a resource token's `quota_events_per_hour` change is still bounded by the current tier's aggregate ceiling — see the resource-tokens guide). The per-plan token / connector caps bound only what a lower plan already holds; for plans with the feature they are the second gate at creation time.

## Secrets (5)

Zero-knowledge secret store: the server holds only `age` public recipient keys and opaque ciphertext, and **never decrypts**. Encryption/decryption happen client-side (the `kagura secret` CLI / SDK). `list` returns metadata only — there is no endpoint that returns a plaintext value.

| Tool | Description | Required Role |
|------|------------|---------------|
| `secret_register_pubkey` | Register your own `age` recipient public key (starts pending; an owner approves it before it can receive grants) | Member+ |
| `secret_put` | Store age-encrypted ciphertext + grant approved recipients (`recipients_snapshot` must match `grant_pubkey_ids`) | Owner/Admin |
| `secret_get` | Fetch ciphertext you hold an active grant for (decrypt locally; every fetch is recorded in a tamper-evident audit log) | Member+ |
| `secret_list` | List secret names + metadata (status, version, grant count, rotation flag) — never values | Owner/Admin |
| `secret_revoke_grant` | Revoke a recipient's grant and flag the secret `rotation_needed` (not retroactive — rotate upstream) | Owner/Admin |

## Sleep Maintenance (3)

Background consolidation of memories (decay, edge pruning, theme summarization).

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_sleep_history` | List past sleep runs | Viewer+ |
| `get_sleep_report` | Detailed sleep report with all actions | Viewer+ |
| `rollback_sleep_run` | Rollback all actions from a completed sleep run | Member+ (report owner only) |

## Usage (1)

| Tool | Description | Required Role |
|------|------------|---------------|
| `get_usage` | Get current workspace usage (memories, contexts, members, MCP calls/day) | Viewer+ |

## API-Key Bindings (2)

| Tool | Description | Required Role |
|------|------------|---------------|
| `list_my_bindings` | List your public-bound API keys (read-only; owner-scoped) | Viewer+ |
| `describe_binding` | Describe one binding by `key_id` XOR `context_id` (read-only; owner-scoped) | Viewer+ |

## Errors

A failed call is a tool result with `isError: true` whose text block is one JSON envelope:

```json
{"status": "error", "error": "<code>", "message": "<what happened>", "help": "<what to do next>"}
```

Branch on `error`; it is stable. `message` is written for people and may change. `help` is the next action, written for the calling model. The envelopes described below always carry `help`; many older argument-validation envelopes (`invalid_argument`, `missing_fields`, …) carry only `error` and `message`, and the message names the argument. The instructions that `get_context_info` and `get_agent_bootstrap` return include a short version of this section, so a calling model has it from the start of a session.

**Refusals.** The request was understood and refused. Repeating the same call fails the same way.

| `error` | Meaning | Next step |
|---------|---------|-----------|
| `invalid_argument`, `validation_error`, `invalid_arguments`, `missing_fields`, `invalid_context_id_format`, `invalid_memory_id_format` | An argument is missing, malformed or out of range; `message` names it | Fix the argument |
| `context_not_found`, `memory_not_found`, `not_found` | The id does not exist or is not visible to you (the two cases are deliberately indistinguishable) | `list_contexts`, `recall` or the matching `list_*` tool |
| `permission_denied` | Your workspace or context role does not allow the operation | Ask a workspace owner for a higher role |
| `quota_exceeded`, `rate_limit_exceeded` | A plan quota or the daily MCP call limit is used up; `quota_type`, `limit`, `resets_at` and similar fields say which | `get_usage`; wait for the reset |
| `plan_required`, `feature_not_available` | The workspace's plan or an operator switch does not include the feature (`gate`, `required_plan`) | None from the model; the workspace owner decides |
| `conflict` | The target's current state conflicts with the request, for example a locked context | Read the current state before calling again |
| `unknown_tool` | No tool has that name | `tools/list` |

**Server failures.** The server could not complete the call. The envelope adds these fields:

| Field | Meaning |
|-------|---------|
| `cause` | `timeout` (the tool did not finish within its time limit), `service_unavailable` (the database, search index, file storage or model provider could not be reached) or `internal_error` (anything else) |
| `correlation_id` | Identifies the failure in the server log: the request's W3C trace id when the client sent `traceparent`, otherwise a random id. Quote it when reporting a problem |
| `retryable` | `true` when repeating the call is safe: read-only tools, and the few writes listed below |
| `retry_after_seconds` | Suggested wait (5) before retrying a `retryable` call after a `timeout` or `service_unavailable` |
| `outcome` | `"unknown"` on tools that change data and are not safe to repeat: the change may or may not have been applied |

Where no tool-specific code exists, `error` equals `cause`. Tools that already had their own failure code keep it — `get_usage_error`, `secret_put_error`, `merge_contexts_error`, `list_tags_error`, `get_analysis_error` and the other `<tool>_error` codes — and `cause` carries the category.

Read-only tools are the ones whose `tools/list` definition sets `annotations.readOnlyHint` (or the older top-level `readOnly`) to `true`. Three writes are also safe to repeat and are marked `retryable`: `recall` and `get_agent_bootstrap`, whose only writes are the ranking updates any repeated query makes, and `secret_register_pubkey`, which refuses a key that is already registered. Any other write is never marked `retryable`: its `help` names the read that shows whether the change took effect (`recall` after `remember`, `reference` after `forget`, `list_contexts` after `create_context`, `list_edges` after `create_edge`, `list_resource_tokens` after `setup_connector`, …). Check it before calling again, so the change is not applied twice. `feedback` is append-only and has no read: calling it again may record the rating twice.

A server-failure envelope never contains exception text, exception types, stack traces, connection strings, file paths, storage keys or driver messages; the server log keeps them under the `correlation_id`. A refusal's `message` is the sentence the service wrote for the caller. `rollback_sleep_run` keeps going when one recorded action cannot be undone; its `partial_rollback` result lists each such action as `Action <id> (<type>) failed: <cause> (correlation_id <id>)` and carries the `correlation_id`. It also carries `help` and `retryable: false`: the report is marked `failed`, so a second `rollback_sleep_run` on it is refused with `invalid_status` rather than undoing an action twice, and `get_sleep_report` lists the run's recorded actions.

A failure outside tool execution — the transport itself — is a JSON-RPC error instead of a result. `error.message` is the same fixed sentence and `error.data` carries `error`, `help` and, for server failures, `cause`, `correlation_id` and the retry fields. The numeric code follows `data`: on session-based connections `-32001` for a `timeout`, `-32002` for `permission_denied`, `-32602` for `validation_error` and `-32603` otherwise; on stateless connections `-32602` for `validation_error` and `-32603` otherwise. A call refused for its [OAuth scope](#oauth-scopes) is one of these errors too, with HTTP `403` and `error: "insufficient_scope"` (`-32002` on session-based connections).

### Migration from the earlier error shapes

- An unexpected failure caught by the dispatcher used to return `{"status": "error", "error": "<exception text>"}` with no `message`. It now returns `timeout`, `service_unavailable` or `internal_error` with the fields above. A service-side refusal that reached the dispatcher (a bad argument, a missing record, a permission, quota or conflict refusal) now returns the matching code from the refusal table with its original message. A client that displayed `error` should display `message`; a client that matched on exception text should branch on `error` or `cause`.
- `get_context_info` used to put the exception text in `error`, and `list_contexts` returned it with no `message`. Both now use the codes above. `get_context_info` always answered with a fixed `message`, so a `ValueError` that reaches its catch-all is treated as a server failure; a malformed `context_id` is still refused as `invalid_context_id_format`.
- The `<tool>_error` codes are unchanged. Their `message` is now a fixed sentence instead of the exception text or "An internal error occurred.", with `cause`, `correlation_id` and the retry fields added. A refusal they carry (for example `merge_contexts_error` for two identical contexts) keeps its message.
- `init_file_upload`, `complete_file_upload` and `get_file_download_url` still report a storage failure as `service_unavailable`. The message is now the fixed sentence instead of the storage error text, and the server-failure fields are added.
- `analyze_context`, `get_analysis`, `list_analyses`, `get_active_analysis`, `get_cluster`, `list_my_bindings`, `describe_binding`, `get_agent_bootstrap` and the `secret_*` tools have always answered an unexpected failure with a fixed message. A `ValueError` that reaches their catch-all is still treated as a server failure, not echoed as a refusal.
- `rollback_sleep_run`'s `rollback_summary.errors` entries for a failed action no longer contain the exception text (see above). The `partial_rollback` message no longer suggests retrying, and the result adds `help` and `retryable: false`.
- The transport-level JSON-RPC error's `data` is no longer `{exception_type, details}`, and `message` no longer includes the exception text. On session-based connections a `ValueError` subclass (for example a JSON decode error) now gets `-32603` instead of `-32602`, an `httpx` timeout `-32001` instead of `-32603`, and a permission or validation refusal raised as a service exception `-32002` or `-32602` instead of `-32603`.
- A [tool guardrail](#tool-guardrails) with `on: "result"` whose `match` targeted the raw exception text of one of these tools no longer matches. Match the `error` code instead.

## Usage notes

The descriptions an agent receives from `tools/list` are paid for on every session, so they carry only what is needed to call a tool correctly: its purpose, when to use it instead of a neighbour, what each parameter means, the response keys, and the rules that must not be missed. The walkthroughs, rationale and longer examples live here. The plugin's `guide` skill carries the short version for an agent that wants it in-session.

### `recall`

**Which read tool.** `recall(query)` finds candidate memories and returns Layers 1-2 (summary + context summary). `reference(memory_id)` returns one memory in full (Layer 3). `explore(memory_id)` walks the graph to adjacent memories. `load_pinned()` returns the complete pinned set, unranked. `recall_upcoming()` and `recall_nearby()` are deterministic time / place queries, not search. The usual flow is recall → reference → explore; for a bug fix: `recall("<error message>")` → find similar past fixes → `reference()` for the details → apply.

**Search with a hypothetical answer (HyDE).** For question-style queries, generate the answer you would expect to find and search with that instead — typically 3-10% better results, up to 25% in the best cases, because a stored memory reads like an answer, not like a question.

- EN: "How to fix auth errors?" → search `"Auth errors are caused by expired JWT tokens. Use the refresh token to re-authenticate..."`
- JA: 「認証エラーの対処法」 → search `"認証エラーはJWTトークン期限切れが原因。リフレッシュトークンで再認証する..."`

**Expand the query.** Run the same question with related terms (「認証エラー」 → also `OAuth2`, `JWT`, `401 error`) and combine the result sets when you need coverage rather than one best hit.

**Filters.**

```
filters={"type": "code"}
filters={"tags": ["python", "fastapi"]}                       # ANY of the tags (exact match)
filters={"tags": ["python", "fastapi"], "tags_match": "all"}  # ALL of the tags
filters={"importance": {"gte": 0.8}, "scope": "persistent"}
filters={"created_after": "2026-03-01T00:00:00Z"}             # also created_before / updated_after / updated_before
filters={"source_uri_prefix": "vault://", "source_type": "vault"}
filters={"trust_tier": "trusted"}
filters={"near": {"lat": 35.68, "lon": 139.77, "radius_m": 500}}       # details.location within the radius
filters={"within": {"polygon": [{"lat": 35.6, "lon": 139.7}, ...]}}  # geofence, 3-128 vertices, ring auto-closed
```

- **Tag drift.** `tags_normalize: true` also matches stored spellings that differ only mechanically from yours — case, hyphen / underscore / space, simple plural — so `dev-environment` matches `Dev_Environment`. It does not match abbreviations (`dev-env`): when a tag filter returns nothing and similar tags exist, the response carries `tag_suggestions` (`{requested_tag: ["stored-tag (count)", ...]}`), so an empty result tells you whether the topic is missing or just spelled differently. A suggestion can also be a narrower or broader tag than yours — `session-cookie (4)` for a filter on `session`, or `session (30)` for a filter on `session-cookie` — since either is the actionable answer when the filter itself matched nothing. Tags of the same shape that differ from yours only in the values of their numbers are never suggested — `issue:#179` is a different identifier from `issue:#1599`, not another spelling of it. The filter itself is never widened. `list_tags` is the way to avoid the problem up front.
- **`trust_tier: "trusted"`** excludes external / connector-ingested memories. It is opt-in — a default recall returns them. Pass it on reads that decide what the agent does next, so untrusted content cannot act as an instruction (OWASP LLM01 / LLM03).
- **Geo.** `near` defaults to a 1000 m radius, clamped to 1 m-1000 km; a malformed `near` is a `validation_error`, and memories without a location never match. `within` composes with `near` (AND).

**Few or no results.** Shorten the query, remove filters, try related terms, lower the importance threshold, or switch to `search_mode="keyword"`.

**Search modes.** `hybrid` (default) combines semantic understanding with keyword matching (60% semantic + 40% BM25 unless the context's search config says otherwise) and applies Neural Memory boosting. `semantic` is vector similarity only — best when you know the concept but not the words; Neural Memory is skipped. `keyword` is BM25 only, with no embeddings — best for exact terms and for Japanese hiragana-only queries, where embedding models struggle.

**`confidence`.** A cheap triage hint for "is anything relevant here, or should I go external?" — not a correctness verdict. `level` (`high` / `moderate` / `low` / `none`) is driven by `top_score`, the best hit's absolute semantic cosine, and `prominence`, `(top_score − mean background cosine) / mean background cosine` — a ratio, so it is robust to an embedding model's cosine scale rather than a global cutoff.

- `none` / `low` (or `count` 0): likely nothing relevant is stored in this context. Prefer an external source over forcing an answer out of the results. This signal is reliable even without a reranker.
- `high` / `moderate`: relevant memory is likely present, so read the summaries and judge from their content. `level` measures topical match strength, so a closely related near-miss can also read `high`; it does not guarantee the exact fact is stored. To separate a near-miss from an exact match pass `use_rerank=true` (a cross-encoder) — plain cosine cannot.
- `relative_margin` is kept for transparency but inflates on off-topic queries; do not use it to decide relevance.
- Examples: `{"level": "high", "top_score": 0.92, "prominence": 0.55, "result_count": 20}`; an empty pool returns `{"level": "none", "top_score": null, "result_count": 0}`.

**`degraded: true`** means the semantic half of the search was unavailable (`degraded_reason` says which), so the result set is keyword-only and `confidence` was computed on a different basis. A degraded empty or low result means "the search was impaired", not "nothing is stored": retry later rather than concluding absence. A healthy search carries neither key.

**`updated_at`** on each result is the last time that fact changed (null if never edited since creation) — a staleness cue that costs no extra call.

**`supersede_candidate`.** At ingest the server checks whether a new memory's nearest neighbour is a near-identical earlier fact — i.e. whether the new memory reads like the updated version of it. If so, the newer memory carries `supersede_candidate: {memory_id, summary, similarity, detected_at}` (the candidate is the *older* fact) on `recall` and `reference`. It is a suggestion, never an auto-applied edge, and it is liveness-guarded: you only ever see a candidate you can already read.

- Accept: `create_edge(source_id=<the memory carrying it>, target_id=<supersede_candidate.memory_id>, edge_type="supersedes", context_id=...)`. The stale candidate is shadowed out of default recall — the strongest lever for update-correctness — and the server clears the suggestion.
- Reject: `update_memory(memory_id=<the memory carrying it>, dismiss_supersede_candidate=true, context_id=...)` when the two are deliberately separate (for example the same conclusion recorded at two altitudes). Neither memory is deleted or shadowed; only the suggestion is tombstoned. Detection resumes for the pair if their similarity later changes materially, and a suggestion for a different memory still surfaces.
- Ignore: it disappears by itself once accepted or once the candidate is deleted.

**`explore_hints`.** `include_explore_hints=true` adds up to three seed memories for a follow-up `explore()` — reason `top_result`, `high_centrality` or `unexplored_neighbor`. They bridge recall (precision search) and explore (graph discovery) without mixing their scoring; use them when the user asks "what else is related?".

**`include_superseded`** returns memories shadowed by a `supersedes` edge, annotated with `superseded_by`, for audit and history ([#1208](https://github.com/kagura-ai/memory-cloud/issues/1208)). **Cross-context recall** (`context_ids`, 2-20 contexts, [#81](https://github.com/kagura-ai/memory-cloud/issues/81)) requires one workspace, one privacy setting and one embedding model across the list.

### `remember`

**Three layers.** `summary` (10-500 characters, what recall matches) → `context_summary` (why this matters and how to use it) → `content` / `details` (the complete data, code or structured information).

**Write the summary as the reusable conclusion, not the process**, and include the synonyms a later search would use.

- Good: "Database performance: PostgreSQL JSONB GIN index optimization for faster queries"
- Good: "JWT expiry caused 401. Fixed with refresh token rotation and clock skew handling."
- Bad: "Discussed auth errors in today's meeting."
- Bad: "JSONB index optimization" — too narrow; it will not match "database performance".

**Chunk long documents by meaning.** The best summary length is 100-250 characters (max 500). For anything over ~2,000 characters create several memories, one per topic — "User authentication module", "Database models", "API routes" — never "Document part 1/3". Link the pieces with common tags or `context={"parent_doc_id": "...", "section": "intro"}`, and carry 50-100 characters of overlap from the adjacent section in `context_summary`.

```
Bad:  remember(summary="auth.py file", content=<entire 5000-line file>)
Good: remember(summary="OAuth2 login implementation",  content=<login function>,      tags=["auth", "oauth2"])
      remember(summary="JWT token validation logic",   content=<validation function>, tags=["auth", "jwt"])
      remember(summary="Session management utilities", content=<session helpers>,     tags=["auth", "session"])
```

**Enrich for search quality.** Tags: key entities and topics (`["OAuth2", "FastAPI", "Python"]`), plus category tags (`category:auth`, `category:料理`); for Japanese include kanji, katakana and hiragana variants (`["鯖", "サバ", "さば"]`). Call `list_tags` first and reuse stored spellings. Importance: critical 0.9-1.0, useful 0.6-0.8, reference 0.3-0.5. Type: a consistent vocabulary (`decision`, `pattern`, `bug-fix`, `troubleshooting`, `learning`, `note`, `code`). Context: background, related issues, reasoning. The more semantic metadata a memory carries, the better it ranks.

**Several domains in one context.** Use domain tags (`domain:personal`, `domain:work`), mark visibility in `context` (`{"visibility": "private", "shareable": false}`), and filter on recall with `filters={"tags": ["domain:work"]}`.

**Updating a fact — `supersedes`.** When you store the newer version of something already remembered, pass `supersedes=<old_memory_id>`. The old memory is shadowed out of default recall — not deleted: it stays reachable via `recall(include_superseded=true)` and `explore()`, and deleting the `supersedes` edge restores it. Prefer this to a near-duplicate: a duplicate leaves the stale and the fresh fact competing in recall, whereas a declared supersession makes the update authoritative. If you did not set it at write time, the server's near-duplicate detection surfaces a `supersede_candidate` later (see `recall` above).

**Durability — what `scope="working"` means.** A new memory is committed to the database before the call returns. It is saved; do not re-write it or wait. `scope` selects the consolidation lifecycle, not whether it was stored:

- `working` (the default for a normal write) — a nightly consolidation pass can promote it to `persistent`; `persistence.promotes_via` names the pass this server actually runs (null if none is enabled). That pass will not archive a working memory younger than `persistence.consolidation_archive_min_age_days`, and only one that has never been adopted.
- `persistent` — outside consolidation's reach. `delivery_mode="always"` pins straight here on write; that is a delivery guarantee, not a stronger durability guarantee than a working-scope write already has.

The age floor is scoped to consolidation and is not a retention SLA: separate near-duplicate merge maintenance can retire an unpinned memory at any age (its tags and edges move to the memory it merged into; `delivery_mode="always"` memories and tool guardrails (`details.tool_trigger`) never enter that pass), and `forget()` removes one on demand. The response carries a `persistence` block for the scope you actually got.

**Write lint.** `lint: [{code, hint, subject?}]` appears only when something about the write will hurt future recall — `summary_short`, `summary_long`, `summary_narrative`, `no_tags`, `tag_near_duplicate` (a tag that near-duplicates one already in the context). A near-duplicate is a mechanical variant, a prefix abbreviation (`dev-env` / `dev-environment`) or a typo within two edits — never two tags of the same shape that differ only in the values of their numbers, so a new `issue:#1599`, `v0.73.0` or `session-2026-09-21` is not flagged against other issue, version or date tags written the same way (a different count of numbers — `v0.73` / `v0.73.0`, `session-2026-09` / `session-2026-09-21` — or the same number padded differently — `sprint-07` / `sprint-7` — still goes through the prefix and typo rules). Nor is a compound tag flagged against its own leading segment(s), in either direction: `session-cookie` next to a stored `session`, `cache-layer-redis` next to `cache-layer`, `some-repo#62` or `session-2026-09-11` next to `some-repo` / `session` are a topic and a sub-topic, not two spellings of one tag (segments are split on whitespace, `_`, `-`, `/`, `:` and `#` — not `.`, so `node` / `node.js` still hints — and a partial segment such as `dev-env` / `dev-environment` or `deploy-check` / `deploy-checklist` is still an abbreviation). When several stored tags match, one that folds to exactly the written tag (`session_cookie` for `session-cookie`) is reported ahead of the most-used one. `tag_suggestions` on `recall` uses the same near-duplicate relation but does not skip the compound case — there, a narrower or broader stored tag is the useful answer. A clean write has no `lint` key. It is advisory: the memory is already stored, and acting on a hint means calling `update_memory()`.

**Never store secrets.** No API keys, tokens, passwords or client secrets; no private keys or certificates; no personally identifiable information; no OAuth refresh tokens or session cookies; no contents of `.env` files or environment variables with credentials. If the input contains such data, the agent refuses and asks for redaction first. The one exception is location ([#1331](https://github.com/kagura-ai/memory-cloud/issues/1331)): geographic coordinates in `details.location = {lat, lon, label?, text?}` are a first-class payload (the WHERE axis), stored deliberately when the user wants a memory tied to a place — `lat` / `lon` as JSON numbers, validated server-side, queryable via `recall_nearby`. Put coordinates only there, never in `context`, which is replicated into the search index's payload store.

The embedding is generated asynchronously after `remember` returns, so a new memory is not findable via `recall()` for a brief moment.

**Tool guardrails.** When a troubleshooting memory is about one specific tool call, mark it with `details.tool_trigger = {tool, on?, match?, action?}` so client hooks deliver it at that call; the server validates the patterns (a safe regex subset) and the write needs context editor or above. Contract, error codes and the shared cache format: [Tool guardrails](#tool-guardrails).

### `update_memory`

- **In place (`memory_id`)** keeps the memory ID, graph edges and creation timestamp, and re-embeds only when `summary`, `context_summary` or `content` changed (`re_embedded`). Use it when you hold a `memory_id` from `recall()`.
- **Upsert (`external_id`)** looks the memory up by `details.resource_id` within the context, for sync workflows with stable external identifiers. Not found → `operation: "created"`. Found → a new memory is written first and the old one soft-deleted, so the response carries a new `memory_id` and `operation: "replaced"`. Requires `summary`, `content` and `type`.
- `details` is replaced wholesale: resend `location` and `tool_trigger` when you update `details`, or they are dropped. Adding, changing or removing `tool_trigger` — and any edit of a memory that already carries one — needs context editor or above ([Tool guardrails](#tool-guardrails)).
- `delivery_mode="always"` pins, `"on_recall"` unpins (the memory stays persistent).

### `reference`

1. `recall()` to find relevant memories. 2. Read the summaries and pick the interesting ones. 3. `reference()` for the full content, structured context, provenance (`source_uri`, `source_type`, `client`) and declared links of each. 4. Present the complete picture.

**Response budget.** A `reference()` response is at most `max_chars` **characters** — not tokens, not bytes: Python string characters (Unicode code points) of the compact JSON text the tool returns, escapes included. The default is 20,000. The light fields always come back whole, so the one exception is a memory whose light fields alone take nearly all of `max_chars`: at their write-side limits they take about 6,000 characters (about 9,000 if the summaries are all quotes or newlines, which escape to two characters), and only control characters (six each when escaped) or a very long tag list push them further. The response then goes over `max_chars`, and a page you ask for still carries at least 500 characters. Claude Code warns when a tool result passes about 10k tokens and caps it at 25k tokens by default; English runs about four characters per token, but Japanese and other CJK text can come close to one token per character, so 20,000 characters stays under the cap in either case. A memory whose full response fits comes back exactly as before, with no extra keys.

| Parameter | Guidance |
|-----------|----------|
| `fields` | Heavy fields to return: any of `content`, `details`, `context`, `links` (`links` = `outgoing_links`, `incoming_links` and their `*_has_more` flags). Default: all four — or, when an offset is set and `fields` is omitted, only the paged field. The light fields (`memory_id`, `summary`, `context_summary`, `type`, `scope`, `importance`, `tags`, timestamps, provenance, `supersede_candidate`) always come back and are counted first. `fields=[]` returns only them |
| `max_chars` | Response budget, 10,000–100,000 characters (default 20,000). Raise it to fetch fewer, larger pages when the reader is not a model with an output limit |
| `content_offset` | Return `content` from this character — `0`, then each `content_next_offset` |
| `details_offset` / `context_offset` | Return `details` / `context` as compact JSON **text** (`details_json` / `context_json`) from this character — `0`, then each `*_next_offset`. Join the pages and parse the result |

Pass at most one offset per call. An offset past the end of its field, a negative or non-integer offset, an unknown field, or an offset for a field excluded by `fields` returns `invalid_argument` (the past-the-end case also carries `<field>_total_chars`).

**When something does not fit, it is marked, never cut silently.** `details`, `context` and `links` cannot be sliced, so they are placed first and come back whole when they fit (room for the other fields' markers and for the page you asked for is kept back first); the page you asked for, then `content` from the start, fill what is left. A large `details` that fits therefore comes back whole while `content` continues with `content_next_offset`.

- `content` too long → a slice from the start, with `content_offset`, `content_total_chars`, `content_truncated: true` and `content_next_offset`. If no slice fits — for example next to a `details` page — the `content` key is left out instead, with `content_omitted: true`, `content_total_chars` and `content_next_offset: 0`. Check `<field>_omitted` before reading any heavy field.
- `details` or `context` too large → the key is left out, and `details_omitted: true`, `details_total_chars` (length of its compact JSON) and `details_next_offset: 0` say so. They are never sliced mid-structure; page them as text instead.
- `links` too large → left out with `links_omitted: true` and `links_total_chars`. The server caps links at 50 per direction, so in practice `fields=["links"]` with a larger `max_chars` returns them whole; if `links_omitted` persists at `max_chars=100000`, read them with `list_edges`.

Every page carries `<field>_offset`, `<field>_total_chars`, `<field>_truncated` and `<field>_next_offset` (`null` on the last page). `updated_at` comes back on every call: if it changes between pages, the memory was edited — start again from offset 0.

```text
# 524,288 characters of plain-ASCII content plus details {"raw": <524,288 characters>};
# the unbounded response was 1,049,291 characters, the bounded one is 19,998.
reference(memory_id=M, context_id=C)
  → {…, "content": "<first 19,127 characters>", "content_offset": 0, "content_total_chars": 524288,
     "content_truncated": true, "content_next_offset": 19127,
     "details_omitted": true, "details_total_chars": 524298, "details_next_offset": 0, …}

reference(memory_id=M, context_id=C, content_offset=19127)      # only content comes back
  → {…, "content": "<next slice>", "content_offset": 19127, …, "content_next_offset": 38616}
  … repeat until content_next_offset is null; the slices joined are the full content.

reference(memory_id=M, context_id=C, details_offset=0)          # only details comes back
  → {…, "details_json": "{\"raw\":\"xxxx…", "details_offset": 0, "details_total_chars": 524298,
     "details_truncated": true, "details_next_offset": 19485}
  … repeat until details_next_offset is null; json.loads("".join(pages)) == details.
```

Every page is a full `reference()` call: context access and the memory-level permission check run again each time, so a page of a memory you cannot read is refused exactly like the first call (`context_not_found` / `memory_not_found`). Only a call without an offset that selects `content`, `details` or `context` counts as a use of the memory (its access and adoption counts, which feed recall ranking and sleep promotion); continuation pages and `fields=[]` / `fields=["links"]` calls do not, so reading a large memory in many pages counts once.

### `forget`

Always verify before deleting: show the memory's summary, warn when `importance > 0.8`, and get explicit approval. For bulk deletion: `recall()` to find candidates → review the list with the user → confirm → loop `forget(memory_id)`; query mode (top-k matches of a query) is for cleanup where that review is not needed. A target that was already deleted, has a wrong ID, or belongs to someone else is skipped silently — `deleted_count` is 0 — so check the ID with `recall()`. Deletion is soft; retention is bounded by the deployment's cleanup window (`CLEANUP_DELETED_MEMORIES_RETENTION_DAYS`, default 30 days), and the memory's graph edges are cleaned up with it.

### `explore`

| Parameter | Guidance |
|-----------|----------|
| `depth` | 1 = direct connections, 2 = recommended default, 3+ = broader but slower and less relevant (max 5) |
| `min_weight` | Typical edge weights are 0.02-0.05. `0.0` = all connections (good for a first look), `0.05` = most connections (default), `0.1` = stronger only, `0.3`+ = very strong only, may return nothing |
| `relation_types` | `neural_association` (Hebbian, automatic), `related_to`, `depends_on`, `learned_from`, `continues_from`, `references_file` (producer-asserted structural edges). Omit to follow every type |

`metadata.total_activated` is the number of nodes the traversal reached and `returned` the number left after `min_weight` filtering. `returned = 0` with `total_activated > 0` means the threshold is too high — lower `min_weight` to 0.0-0.05. Results are ranked by activation strength (graph-based relevance), top 10.

### `list_tags`

Tag filters match exactly, so semantically identical tags under different spellings (`troubleshoot` / `troubleshooting` / `trouble-shoot`) silently erode `recall(filters={"tags": [...]})` over time. `list_tags` is the primary mitigation: call it before `remember()` to reuse existing spellings, and before a tag-filtered `recall()` to build filters that match what is stored.

```
list_tags(context_id="...")                   # top 50 tags by usage count
list_tags(context_id="...", min_count=5)      # only frequently used tags
list_tags(context_id="...", prefix="auth")    # autocomplete: tags starting with "auth"
list_tags(context_id="...", sort="recent")    # most recently used first
list_tags(context_id="...", sort="alpha")     # alphabetical, case-folded
list_tags(context_id="...", with_tags=["python", "backend"])  # tags that co-occur with both
```

An empty context returns `tags: []` and `total: 0`, not an error. Soft-deleted memories are excluded and the workspace boundary is honoured for shared contexts. `prefix` escapes `%` and `_`, so it cannot be used as a wildcard probe.

`with_tags` drills down the same way as the Web UI tag cloud and `GET /api/v1/contexts/{id}/tags?with_tags=`: only memories carrying **all** of the given tags (exact match, surrounding whitespace trimmed) are counted, and the given tags themselves are left out of the result, so the list answers "what else is tagged alongside these". Counts are over that subset. It takes at most 50 tags of at most 200 characters each; more returns `invalid_argument`. An empty list is no filter.

### Edges

| `edge_type` | Meaning |
|-------------|---------|
| `related_to` (default) | General relationship |
| `depends_on` | Target depends on source |
| `learned_from` | Knowledge derived from source |
| `neural_association` | Created automatically by Hebbian learning — prefer `related_to` for manual edges |
| `continues_from` | Chronological / narrative successor between chat memories (producer-asserted, directional; [#782](https://github.com/kagura-ai/memory-cloud/issues/782)) |
| `references_file` | Structural reference from a chat memory to a file overview (producer-asserted, directional; #782) |
| `supersedes` | Source is the newer memory, target the outdated one: the target is shadowed out of default recall while the source lives ([#1208](https://github.com/kagura-ai/memory-cloud/issues/1208)). Also the way to accept a `supersede_candidate` |
| `contradicts` | Both sides stay visible, annotated — contradiction never hides |

Weights run from 0.0 to 3.0; the default 1.0 is a full-confidence manual edge, so `create_edge` usually needs only `source_id` and `target_id`.

**Calling `create_edge` on an existing pair** ([#1321](https://github.com/kagura-ai/memory-cloud/issues/1321)). The (source, target) pair is unique per user, so the outcome is deterministic. An existing auto edge (origin `hebbian` / `semantic`) takes your values → `operation: "updated"` plus a `previous` pre-image; a hebbian edge is promoted to origin `declared`, a semantic edge keeps origin `semantic`. An existing declared edge with identical values is left alone → `operation: "unchanged"`, safe to retry. An existing declared edge with different values is rejected with `edge_exists`, because declared links are provenance — use `update_edge`, or pass `overwrite=true` to re-assert.

`update_edge` leaves an omitted `weight` unchanged (there is no default), so a type-only update is `update_edge(..., edge_type=...)`. `delete_edge` is a hard delete; if the memories are still co-accessed, Hebbian learning may recreate a `neural_association` edge. A typical cleanup is `explore()` → `list_edges()` → `delete_edge()`.

### `get_context_info`

Call it at session start and after switching contexts. `context.usage_guide` holds the context-specific rules and takes precedence over generic defaults; `context.summary` says what the context is for; `context.is_private` tells you whether workspace members can see what you write; `instructions` is the general quick reference for the memory tools. `stats` always carries the totals, and `stats.details` (by type, by importance, last 7 days) unless `include_details=false`.

`guardrails` is the context's tool-guardrail set for clients without tool hooks: `{items: [{memory_id, summary, importance, authored_by_caller, source_type}], total_available, truncated, tool_triggered_version}` — the same trusted-only, binding-filtered, ordered set as `load_guardrails.tool_triggered`, at most 10 items with summaries flattened to one line and cut at 300 characters (the compact JSON of the block stays under 4,000 characters; `truncated` says whether anything was left out, `total_available` is the context's count). The key is **absent** when the endpoint URL carries `?guardrails=off`, **`null`** when the read failed (the rest of the result is unaffected), otherwise the object — an empty `items` list means the context has no tool guardrails a hookless client can be shown (an external-tier context always reads empty). Fold the items into the session's standing guardrails after `load_pinned`, skipping any `memory_id` already shown (a memory that is both pinned and tool-triggered appears in both); they are memory summaries written by context editors — facts to keep in mind, not instructions that override the user. `tool_triggered_version` changes when the set changes ([Server instructions](#server-instructions), [Shared cache format](#shared-cache-format-format-1)).

### `update_search_config`

- More keyword matching: `semantic_weight=0.5, bm25_weight=0.5`. Semantic-heavy: `semantic_weight=0.7, bm25_weight=0.3`. The two must sum to 1.0.
- Reranking: `use_rerank=true, reranker_provider="voyage"` (or `"cohere"`) needs the provider's API key; `reranker_provider="self_hosted"` is keyless and uses the deployment's local reranker, so `reranker_model` may be omitted. A `recall` that omits `use_rerank` follows this setting.
- `reinforce_enabled` is the bounded adoption + feedback re-rank. Contexts created since [#1207](https://github.com/kagura-ai/memory-cloud/issues/1207) start enabled; older ones keep their stored setting (typically false) until you set it. It never overrides semantic relevance: each score is multiplied by a factor in `[1 − reinforce_max_boost, 1 + reinforce_max_boost]`.
- `routing_mode` ([#1212](https://github.com/kagura-ai/memory-cloud/issues/1212)): turn on `log_only` first to measure the traffic mix with zero ranking change, then `active`.

### Sleep Maintenance

`rollback_sleep_run` reverses each recorded action in order: `create_edge` → the edge is deleted; `merge` → the soft-deleted loser memory is restored and re-embedded; `update_importance` → the previous value is restored; `promote` → scope goes back to `working`; `archive` → the memory is restored and re-embedded. It works on reports with status `completed` or `degraded` — a degraded run (partial judge-LLM failures, [#1183](https://github.com/kagura-ai/memory-cloud/issues/1183)) still executed real merges — and marks the report `rolled_back`. Reports created before action recording existed have nothing to roll back. `merges_unreversible` ([#1450](https://github.com/kagura-ai/memory-cloud/issues/1450)) counts shadow merges that were not reversed because a later writer changed or removed the edge; restoring the pre-merge state would have discarded that newer state, so the run reports `partial_rollback`.

### Files

Uploads go to platform-managed object storage (Cloudflare R2) in three steps: `init_file_upload` reserves quota atomically and returns a presigned PUT URL; the client PUTs the bytes; `complete_file_upload` verifies the stored object against the declared sha256 and size and moves the file from `reserved` to `uploaded`. The per-file cap is 100 MiB. Sending the same sha256 twice in one workspace returns a `conflict` that names the existing `file_id`. `delete_file` releases the quota immediately; the binary lingers for 7 days before the nightly sweeper removes it, with no client-visible effect.

### Resources and connectors

```
setup_resource(name="ec-products", resource_id="ec_products")
  → context created, token issued, ready for ingest_events()

setup_connector(connector_type="slack", resource_id="slack_general")
  → connector created, token issued; use idempotency_key="{connector_id}:..."

ingest_events(resource_id="ec_products", events=[
  {"op": "upsert", "doc_id": "PROD-1", "version": 1, "payload": {"name": "...", "price": 5980}},
  {"op": "delete", "doc_id": "PROD-999"}
])

get_resource_impact(resource_id="ec_products")   → {token_count: 2, memory_count: 500, current_schema_version: 3}
get_resource_schema(resource_id="ec_products")   → {schema_version: 3, field_definitions: [{name: "product_name", ...}]}
list_resource_tokens(resource_id="ec_products")  → {tokens: [{id: 1, resource_id: ..., is_active: true, ...}], total: 3}
```

`setup_connector` creates a resource, a connector row and a connector-scoped resource token in one operation; its `runtime` object is validated server-side (the schema in `tools/list` is advisory).

### Analyses

```
analyze_context(context_id="...", dry_run=True)   # cost preview, nothing is created
analyze_context(context_id="...")                 # starts the run → run_id
get_analysis(run_id="...")                        # poll until finished_at is set
list_analyses(context_id="...", limit=20)         # → {items: [...], next_cursor: "2026-04-30T12:34:56"}
get_cluster(run_id="...", cluster_index=3)        # label, representatives, paginated members
```

`get_analysis` answers `run_not_found` both for unknown ids and for runs in another workspace, so existence is not leaked.
