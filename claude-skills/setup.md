---
description: Configure and verify the Kagura Memory MCP connection and the guardrail hooks
---

Configure Kagura Memory Cloud for this client and prove it works: the MCP connection, the plugin's
`userConfig`, and the one URL that ties them together. `/kagura-memory:guide` explains; this skill
**acts** — it reports what is in effect, asks before every change, and finishes by running the hook
once so the result is a number, not silence.

Mode: $ARGUMENTS

`--check` (also `check`, `doctor`) → read-only. Run **Step 1** and **Step 6** only, report, stop.
Change no file, write no configuration, install nothing, ask for no credential.

Claude Code only. Codex CLI setup is the "Tool guardrails (hooks)" section of
`plugins/kagura-memory/skills/kagura-memory/SKILL.md`.

## Rules

- **Never print an API key**, in full or in part, in a report, a command you run, or a file. When a
  key is needed, take it from a variable or a file already on the machine and pass it by expansion
  (`"$VAR"`, `"$(cat …)"`) so the value never enters this conversation.
- **Never read a key out of a config file to build a request the user did not ask for.** Say where
  the key goes; do not go and fetch it.
- When you read an MCP config file, project out `url`, `type` and *whether* an `Authorization`
  header exists — never the header's value.
- **`claude mcp get` prints configured headers with their values**, so a Bearer entry puts its key on
  your screen. Always pipe it through the redaction in Step 1, and do the same for any other command
  that can echo a header.
- Ask before every write, name the exact file, and show the before/after of the one value changing.

## Steps

### 1. Detect the effective MCP entry

```bash
claude mcp list
# claude mcp get prints configured headers WITH their values, so redact them before
# reading the output: every header line is indented four spaces, nothing else is.
claude mcp get kagura-memory | sed -E 's/^(    [A-Za-z0-9_-]+:).*/\1 <redacted>/'
```

`claude mcp list` prints every configured server with its URL and health, and — when one name is
defined in more than one scope — an `MCP config diagnostics` block with a `[Conflicting scopes]`
warning. `claude mcp get <name>` prints only the entry that **wins**: `Scope`, `Status`, `Type`,
`URL`, plus `Headers:` and `OAuth:` when the entry has them. The `sed` blanks the header *values* and
leaves `Scope` / `Status` / `Type` / `URL` untouched; a redacted `Authorization:` line is still the
signal that this entry carries a Bearer key. Never run `claude mcp get` unfiltered.

Precedence, strongest first. A stronger scope shadows a weaker one of the same name silently, which
is why editing the wrong file appears to do nothing:

| Scope | Where it lives |
|---|---|
| `local` | `~/.claude.json` → `projects["<absolute project path>"].mcpServers` |
| `project` | `.mcp.json` in the project root, and the user-level `~/.claude/.mcp.json` |
| `user` | `~/.claude.json` → `mcpServers` |

To read a `.mcp.json` without touching header values:

```bash
python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for n,s in (d.get("mcpServers") or {}).items():
    print(n, s.get("type"), s.get("url"), "bearer" if (s.get("headers") or {}).get("Authorization") else "no-header")' .mcp.json
```

Report, as a block:

- **Effective entry** — name, the file it comes from, the scope.
- **Auth mode** — an `Authorization` header on the entry → **Bearer key**. No header (Claude Code
  shows `Needs authentication`, or `Connected` after `/mcp` sign-in) → **OAuth**.
- **URL** — the path (`/mcp`, or `/mcp/w/<workspace-id>`) and each query parameter separately
  (`profile`, `guardrails`).
- **Shadowed entries** — every other scope defining the same name, with its file and URL. Fix:
  `claude mcp remove <name> -s <scope>` on the one that should not be there. OAuth tokens are stored
  per endpoint, so two entries with different URLs cannot share a sign-in.
- **Duplicate plugin installs** — `claude plugin list` and look for two `kagura-memory` rows, e.g. a
  claude.ai-synced install next to a marketplace install; Claude Code picks one and warns. Which one
  carries the hooks: `claude plugin details kagura-memory@<marketplace>` — the inventory line
  `Hooks (4)  SessionStart, PreToolUse, PostToolUse, PostToolUseFailure`. Uninstall the other.

In `--check` mode, skip Steps 2–5 and go straight to Step 6: take `server_url` from the entry found
here, and resolve the context id with `list_contexts()` if it is not already known — a read, not a
change.

### 2. Choose the context

```
list_contexts()
```

Pick the context whose guardrails should apply — the one matching this project, or the single one if
there is only one — and confirm the name with the user. Never ask for a pasted UUID; you have the id
from `list_contexts`. `get_context_info(context_id=...)` confirms the choice and shows how many tool
guardrails it already carries. One context serves all projects in v1.

### 3. Derive `server_url`

**`server_url` is the MCP endpoint, not the site root.** The hook POSTs `tools/call load_guardrails`
to `server_url` verbatim, so anything that is not the endpoint answers `http 404` or `http 405` and
no guardrail is ever delivered.

Derive it from the effective URL in Step 1 — never invent it, never ask the user to:

- Keep the scheme, host and **path** exactly as they are: `https://<host>/mcp` or
  `https://<host>/mcp/w/<workspace-id>`.
- Drop the query, except that `guardrails=off` may stay. A `guardrails=<context-id>` left in
  `server_url` makes the hook print a warning at every session start.

Then show the values to enter — three of them, and where the fourth comes from:

| Option | Value |
|---|---|
| `server_url` | the endpoint derived above |
| `context_id` | the UUID from Step 2 |
| `max_action` | `block` (default), or `inform` to never deny |
| `api_key` | **the user enters this** — Workspace → Integrations → API Keys, `kagura_…` |

The hooks authenticate with a Bearer key only, so an **OAuth** MCP entry still needs an API key
here; it is a separate credential from the MCP sign-in. A user key can read and author guardrails;
an agent-bound key can only read them.

Applying it:

- **Not installed yet** — give the user this line to run in their own terminal, so the key never
  passes through the conversation:

  ```
  claude plugin install kagura-memory@kagura-memory-cloud --config server_url=<endpoint> --config context_id=<uuid> --config max_action=block --config api_key=<your key>
  ```

- **Already installed** — `/plugin` → kagura-memory → Configure, and paste the four values. Claude
  Code keeps them in the user's settings and keychain; they never reach the repository, and a
  project's `.claude/settings.json`, `.mcp.json`, `~/.claude.json` and any `KAGURA_*` variable are
  never read by the hooks.

### 4. Align `?guardrails=` on the MCP URL

With the hooks on, the hooks are this client's guardrail lane, so the server should not also send a
digest of the same memories: the `.mcp.json` **URL** wants `?guardrails=off` (`&guardrails=off` when
it already has a query, such as `?profile=core`). This is a change to the MCP entry, not to
`server_url`.

**Warn before changing it, and get an explicit yes:**

> Changing the URL of an MCP entry that is authenticated with **OAuth** disconnects that server.
> Claude Code stores OAuth tokens per endpoint, so the new URL starts with no token and every Kagura
> tool disappears until you re-run `/mcp` and sign in again. A Bearer-key entry is not affected.

Offer to skip it — the duplicate digest is harmless, only wasteful. If the user agrees:

- Entry in a `.mcp.json` file (project root or `~/.claude/.mcp.json`) → change **only** the `url`
  string in place. Do not touch `headers` and do not rewrite the file.
- Entry in `~/.claude.json` (`local` / `user` scope) → do not hand-edit that file; it holds the whole
  client state. For an OAuth entry, `claude mcp remove <name> -s <scope>` then
  `claude mcp add --transport http <name> "<new url>" -s <scope>`. For a Bearer entry, print both
  commands and let the **user** run the `add` with its `--header`, so the key stays out of this
  conversation.

Afterwards: `/mcp` → the entry → authenticate, then `claude mcp get <name>` to confirm `Connected`.

### 5. Restart the hooks

The hooks read their configuration at `SessionStart`, so a new configuration takes effect in a
**new** session — start one, or `/clear`. Step 6 does not wait for that: it runs the hook itself.

### 6. Verify

#### 6a. Endpoint probe — no credentials

```bash
curl -s -o /dev/null -w '%{http_code}\n' -m 10 -X POST \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"load_guardrails","arguments":{}}}' \
  '<server_url>'
```

Sends no key and no context id. Read the status:

| Code | Meaning |
|---|---|
| `401`, `403` | the MCP endpoint is there and wants credentials — **the URL is right** |
| `404`, `405` | not an MCP endpoint; a site root answers `405`. Fix the path to end in `/mcp` or `/mcp/w/<workspace-id>` |
| any other `4xx` | an MCP endpoint answered and rejected the probe on its merits (`400` a malformed session, `406` a stricter `Accept`) — the path is right; go to 6b |
| `5xx` | the endpoint is there but the server is failing — retry, then check the deployment |
| `000`, timeout | host unreachable: DNS, TLS, proxy, or the server is down |
| `200` | something that answers an unauthenticated `tools/call` — not the Kagura endpoint |

#### 6b. Run the hook once

Only with the user's explicit go-ahead, and only with a key already on the machine — an exported
variable, or a file the user names. Never paste a key into the command text.

The plugin root is the directory holding `.claude-plugin/plugin.json` and
`plugins/kagura-memory/hooks/`; for an installed plugin:

```bash
python3 -c 'import json,os
d=json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json")))
[print(k, e["installPath"]) for k,v in (d.get("plugins") or {}).items() if "kagura-memory" in k for e in v]'
```

(It only supplies the version in the User-Agent — a root that cannot be found is not fatal.) Then:

`$KAGURA_SETUP_API_KEY` below is whatever already holds the key on this machine — an exported
variable, or `"$(cat "<the file the user named>")"`. Substitute the expansion, never the key.

```bash
KAGURA_SETUP_DATA="$(mktemp -d)"
printf '%s' '{"hook_event_name":"SessionStart","source":"startup","session_id":"kagura-setup-check"}' \
| CLAUDE_PLUGIN_ROOT="<plugin root>" \
  CLAUDE_PLUGIN_DATA="$KAGURA_SETUP_DATA" \
  CLAUDE_PLUGIN_OPTION_SERVER_URL="<server_url>" \
  CLAUDE_PLUGIN_OPTION_CONTEXT_ID="<context uuid>" \
  CLAUDE_PLUGIN_OPTION_API_KEY="$KAGURA_SETUP_API_KEY" \
  python3 -I -S "<plugin root>/plugins/kagura-memory/hooks/kagura_guardrails.py" --client claude
```

One JSON line, or nothing. Read it:

| What comes back | What it means |
|---|---|
| `additionalContext`: `Kagura Memory: N tool guardrails active for context <uuid> (fetched)` | **verified** — report N and the context name |
| `server_url must be the MCP endpoint, …` | `server_url` is not the endpoint (the `404`/`405` case) |
| `server unreachable and no usable cache` | the fetch failed for another reason — key, TLS, network |
| `<field> is missing or invalid` | that value did not parse: a `context_id` that is not a UUID, an empty key |
| `this URL also requests the server digest (guardrails=<context>)` | drop `guardrails=<id>` from `server_url`, or set it to `off` |
| nothing at all | none of the three values reached the hook — check the variable spelling |
| `N` is 0, no message | connected; the context simply has no `details.tool_trigger` memories yet |

Optional dry run — the SessionStart above cached the set in `$KAGURA_SETUP_DATA`, so a sample tool
call shows whether anything matches it. No network:

```bash
printf '%s' '{"hook_event_name":"PreToolUse","session_id":"kagura-setup-check","tool_name":"Bash","tool_input":{"command":"git push --force"},"tool_use_id":"toolu_setup"}' \
| CLAUDE_PLUGIN_ROOT="<plugin root>" \
  CLAUDE_PLUGIN_DATA="$KAGURA_SETUP_DATA" \
  CLAUDE_PLUGIN_OPTION_SERVER_URL="<server_url>" \
  CLAUDE_PLUGIN_OPTION_CONTEXT_ID="<context uuid>" \
  CLAUDE_PLUGIN_OPTION_API_KEY="$KAGURA_SETUP_API_KEY" \
  python3 -I -S "<plugin root>/plugins/kagura-memory/hooks/kagura_guardrails.py" --client claude
```

Empty output means no guardrail matched that call — not a failure.

**Clean up, always**, including when a step above failed:

```bash
rm -rf "$KAGURA_SETUP_DATA"
```

It held a fetched guardrail cache. Never point this check at the plugin's real data directory.

### 7. Report

```
Kagura Memory setup
  MCP entry    kagura-memory — local (~/.claude.json → projects[<project>]) — OAuth — https://<host>/mcp
  Shadowed     project (~/.claude/.mcp.json) — Bearer — https://<host>/mcp/w/<workspace-id>
  Plugin       kagura-memory@kagura-memory-cloud 0.74.0 — Hooks (4)
  server_url   https://<host>/mcp
  context      <name> (<uuid>)
  ?guardrails= off — yes
  Endpoint     401 — endpoint reachable, credentials required
  Hooks        fetched 7 tool guardrails for <name>
```

Then the remaining manual steps, if any: the OAuth sign-in, the API key to paste into `/plugin`, the
shadowed entry to remove, the duplicate install to uninstall. In `--check` mode, end with the
verdict only and state plainly that nothing was changed; if no key was available, say
`hook fetch not verified — no API key in this shell` rather than asking for one.

## When nothing happens

Silence is this plugin's failure mode — a misconfiguration looks exactly like a context with no
guardrails. In order:

1. `claude plugin details kagura-memory@<marketplace>` — does the install that Claude Code uses list
   `Hooks (4)`?
2. `ls "$HOME/.claude/plugins/data/"kagura-memory-*/guardrails/` — a `<context_id>.json` means a
   session-start fetch has succeeded at least once.
3. Step 6a — is `server_url` the endpoint at all?
4. Step 6b — what does the hook actually say?
5. `python3 --version` — 3.9+, on `PATH`, and not inside the project. Windows is unsupported for
   these hooks.
