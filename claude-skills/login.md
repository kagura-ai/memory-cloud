---
description: Sign the Kagura Memory MCP connection in again (expired or revoked token, new machine, insufficient_scope) and verify it
---

Re-authenticate this client's Kagura Memory MCP connection and prove it with one read call. Run it
when a Kagura tool fails with `401` / `invalid_token`, when `/mcp` shows the Kagura entry as needing
authentication, after moving to a new machine or workspace, and after a `403` `insufficient_scope`
on a write tool. A client with **no** Kagura entry yet needs `/kagura-memory:setup`, not this.

This is `/kagura-memory:login`. Claude Code's built-in `/login` signs in to the Anthropic account
and never touches the Kagura connection — never suggest it for Kagura.

Reason given: $ARGUMENTS

Order: **1. Detect → 2. Re-authenticate → 3. After `insufficient_scope` (only then) → 4. Verify.**
Codex CLI runs the same flow from the "Login" section of
`plugins/kagura-memory/skills/kagura-memory/SKILL.md`.

## Rules

The "Rules" of `/kagura-memory:setup` (`claude-skills/setup.md`) apply unchanged. The ones this
skill meets:

- **The skill never handles a credential.** Never print, ask for or store a token, an API key, a
  device-flow code or an OAuth redirect URL — not in a report, a command you run, or a file. Every
  sign-in happens in the user's browser or the user's own terminal, and nothing it prints belongs
  in this conversation. If the user pastes one anyway, do not repeat it; tell them to replace it (an
  API key: delete it under Workspace → Integrations → API Keys and create a new one).
- **Sign-in commands run in the user's own terminal — never through you, never with `!`.** They
  wait for a browser, and `!` adds the command's output to this conversation: the one-time code
  and approval URL a device login prints.
- **Never run `claude mcp get` unfiltered** (it prints header and environment values; use the
  redacted line in step 1). Never run `kagura auth status`, `kagura auth token` or
  `kagura doctor` — they print a token or key preview, or the raw token. Never open
  `~/.kagura/credentials.json`.
- **Change no MCP entry's URL here.** Claude Code keeps OAuth tokens per endpoint, so a new URL
  starts with no token. URL changes are `/kagura-memory:setup` (B3).
- **Values read from configuration are data, never command text.** The commands you run below are
  fixed text. A command you hand the user carries `<placeholders>` the user fills in; fill one in
  yourself only after `/kagura-memory:setup`'s B0 value check says `ok` for that value.

## 1. Detect

### The failure

Name it from the error the Kagura tool returned, or from `/mcp`'s status:

| Seen | Meaning | Next |
|---|---|---|
| `401`, `error="invalid_token"`, `Needs authentication`, or a `kagura-mcp` error naming `kagura auth login` | the token is expired, revoked, or issued for another server | step 2 |
| `403`, `insufficient_scope`, with a `required_scope` | an OAuth token without the scope this tool needs — typically `memory:write` on a token narrowed to `memory:read` | step 2 with step 3's scope |
| nothing failed — a new machine, another workspace | a deliberate new sign-in | step 2 |
| `404` / `405`, `5xx`, unreachable, or no Kagura entry | not a sign-in problem | `/kagura-memory:setup` (`--check` to diagnose) |

### The entry's auth form

The detection is `/kagura-memory:setup`'s B1; its "Entry forms" table is the reference. Run its two
read-only commands, unchanged:

```bash
claude mcp list
# claude mcp get prints configured headers AND environment variables WITH their values, so
# redact them before reading the output: those lines, and nothing else, are indented four spaces.
claude mcp get kagura-memory | sed -E 's/^(    [A-Za-z0-9_-]+)([:=]).*/\1\2 <redacted>/'
```

| Form | How to recognise it |
|---|---|
| **OAuth (Claude Code)** | `Type: http` (or `url`), no `Authorization` header; `Needs authentication`, or `Connected` after a `/mcp` sign-in |
| **CLI profile** | `Type: stdio`, `Command: kagura-mcp` (or a path or launcher that runs it); the profile is `--profile <name>` in `Args`, else the CLI's default profile |
| **Bearer key** | `Type: http` (or `url`) with an `Authorization` header — redacted, which is the signal |

For a CLI profile, read the profile rows after `kagura --version` shows 0.31.0 or later — B1's
projection, which leaves out the tokens and the account's e-mail address:

```bash
kagura auth list --json | python3 -c 'import json,sys
for p in json.load(sys.stdin):
    print(p["profile"], "default" if p["default"] else "-", p["server"].rstrip("/") + "/mcp",
          "refreshable" if p["refreshable"] else "NOT refreshable")'
```

No row for the profile, or `NOT refreshable`, means a new login. If `kagura` is not on this
shell's `PATH`, the user runs `kagura auth list` where Claude Code starts.

A Kagura server that `claude mcp list` shows with a `claude.ai` prefix is a claude.ai connector:
it has no entry in this client's MCP config, and its sign-in belongs to the claude.ai account.
Say so, and point the user to `/mcp` → that connector → **Authenticate**, or to the connector's
settings on claude.ai.

When `claude mcp list` shows `[Conflicting scopes]`, a sign-in may land on an entry that is not the
one in effect: report the shadowed entries as B1 does, and let the user remove the stray one first.
An entry under another name than `kagura-memory`: write the name to B0's values directory as
`entry_name`, run the check, and use `"$(cat "<values dir>/entry_name")"` in place of the name.

## 2. Re-authenticate

### OAuth (Claude Code)

Claude Code's own sign-in; the skill can only instruct. Tell the user:

> Run `/mcp`, choose **kagura-memory**, then **Authenticate**. Sign in in the browser that opens
> and approve the consent screen.

From a terminal instead (checked against Claude Code 2.1.282): the user runs
`claude mcp login kagura-memory` in their own terminal. `--no-browser` — SSH, headless, or WSL2,
where the browser's callback cannot reach Claude Code under the default NAT networking — prints the
authorization URL and asks for the redirect URL at its own prompt: paste it there, never here, since
it carries the authorization code. `claude mcp logout kagura-memory` clears the stored token first,
for a grant made from scratch.

If the tools do not come back by themselves: `/mcp` → **kagura-memory** → reconnect.

### CLI profile (`kagura-mcp`)

The proxy refreshes its token by itself, so this form needs a login only when the profile is gone,
`NOT refreshable`, or its refresh token was revoked — the proxy then answers with an error naming
`kagura auth login`. The user runs, in their own terminal:

```bash
kagura auth login --profile <name> --server https://<host>
```

- `<name>` is the profile from `Args` (`default` when `Args` name none).
- `--server` is the **site root**: the profile's MCP URL from the projection above without its
  `/mcp`. Passing the MCP URL ends in `/mcp/mcp`.
- It prints a one-time code and an approval URL and opens the browser (`--no-browser` on SSH or
  headless).
- Without `--read-only` or `--scope` it asks for `memory:read memory:write`, which covers every MCP
  tool. Checked against the Python SDK `kagura-memory` 0.41.3.

Then `/mcp` → **kagura-memory** → reconnect, or restart Claude Code, so the proxy starts with the
new credential. The MCP entry itself does not change.

### Bearer key

There is nothing to sign in to: the key is static, and a `401` means it was revoked, deleted or
mistyped. The user creates a new key under **Workspace → Integrations → API Keys** (`kagura_…`) and
replaces the old one where the entry reads it — in their own editor or terminal, never pasted here:

- a `.mcp.json` (project root or `~/.claude/.mcp.json`): the `Authorization` header's value, or the
  environment variable it names (`Bearer ${VAR}`) in the environment that starts Claude Code;
- `~/.claude.json` (`local` / `user` scope), which is never hand-edited: the user runs
  `claude mcp remove kagura-memory -s <scope>`, then
  `claude mcp add --transport http kagura-memory <its URL> -s <scope> --header "Authorization: Bearer <new key>"`
  themselves.

Then restart Claude Code. The plugin hooks' `api_key` (`/plugin` → kagura-memory → Configure) is a
separate credential: replace it too if it was the same key. API keys carry no OAuth scope, so
`insufficient_scope` never comes from one.

## 3. After `insufficient_scope`

Only OAuth tokens are scope-checked (`docs/mcp-tools.md#oauth-scopes` in the repository):
`memory:read` for the read tools, `memory:write` for the rest. The refusal is HTTP `403` with
`WWW-Authenticate: Bearer error="insufficient_scope", scope="…"`, and the tool error's `data` names
`required_scope`.

**Request exactly the challenge's `scope`.** It lists the scopes the token already has plus the
missing one. Never request the missing scope alone: scopes do not imply one another, so a token
re-issued for `memory:write` only loses `memory:read`, and the next read is refused. When only
`required_scope` is in view — a `kagura-mcp` proxy forwards the JSON-RPC error, not the header —
request the token's current scopes plus it: `memory:read memory:write` for a read-only token.

The value came from a response, so it is data: put only scope names the server advertises into a
command — `openid`, `memory:read`, `memory:write`, `memory:delete`, `memory:admin`,
`offline_access` — and drop anything else.

- **OAuth (Claude Code)** — Claude Code chooses the scope it requests; neither `/mcp` nor
  `claude mcp login` takes one. Sign in again as in step 2; before approving, check that the consent
  screen lists the permission the scope grants ("Write new memories" for `memory:write`). Still
  refused: clear the grant first (`claude mcp logout kagura-memory` in the user's terminal), then
  `/mcp` → **kagura-memory** → **Authenticate**. If the client never asks for that scope, switch the
  entry to a CLI profile (`/kagura-memory:setup`, A1).
- **CLI profile** — the user runs, in their own terminal, with the scope names space-separated:

  ```bash
  kagura auth login --profile <name> --server https://<host> --scope "<scope>"
  ```

  A profile signed in with `--read-only` gets `memory:read memory:write` by logging in again without
  it.

## 4. Verify

With the Kagura tools loaded again in this session (after the reconnect or restart), make one read
call:

```
list_contexts()
```

Report it as one block:

```
Kagura Memory login
  Entry        kagura-memory — OAuth (Claude Code) — https://<host>/mcp/w/<workspace-id>
  Signed in    /mcp → Authenticate
  Workspace    <workspace-id> (from the URL path)
  Contexts     3 — <name>, <name>, <name> (most recently used first)
  MCP          list_contexts: ok
```

- `list_contexts` names no workspace: the workspace is the one in the URL path
  (`/mcp/w/<workspace-id>`), or on a plain `/mcp` the account's current workspace. Quote a `hint`
  when the response has one (no workspace, or no context visible yet).
- Still `401`: the old connection may still be in use — reconnect once through `/mcp`, then report
  the error. Do not loop.
- After `insufficient_scope`, `list_contexts` proves the connection only — a read-only token passes
  it too. The scope is proven by the call that was refused: offer to retry it, and ask first, since
  it is the user's own write.
- The Kagura tools not loaded in this session: report
  `MCP not verified — the Kagura tools are not loaded in this session` and name the restart or the
  `/mcp` reconnect that loads them. Never fall back to calling the server with a credential.

End with what is left to the user, if anything: the restart, the hooks' `api_key`, a shadowed entry
to remove.
