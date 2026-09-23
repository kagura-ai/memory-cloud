---
description: Configure and verify the Kagura Memory MCP connection and the guardrail hooks
---

Configure Kagura Memory Cloud for this client and prove it works: the MCP connection, the context
whose guardrails apply, the guardrail lane, and — in Claude Code — the plugin's `userConfig` and its
hooks. `/kagura-memory:guide` explains; this skill **acts**: it reports what is in effect, asks
before every change, and ends with numbers from the server, not silence.

Mode: $ARGUMENTS

The skill has two parts. **Part A** is harness-neutral: it names only MCP tools and the Kagura CLI.
**Part B** is the Claude Code adapter — every `claude …` command, the MCP scope table, `userConfig`,
the plugin commands and the hook dry run live there and nowhere else. Codex CLI setup is the
"Tool guardrails (hooks)" section of `plugins/kagura-memory/skills/kagura-memory/SKILL.md`.

## Mode

**Check mode** is read-only. It applies when the arguments contain `--check`, `check` or `doctor`,
**or** when the user asks in their own words to *check*, *diagnose* or *doctor* the setup ("is my
Kagura setup OK?", "diagnose the hooks") — only Claude Code passes arguments to a skill, so the
request's wording counts as much as a flag. In check mode change no file, write no configuration,
create no context, install nothing, run no login and ask for no credential. Anything else is
**setup mode**.

## Run order

| Setup mode (Claude Code) | Check mode (Claude Code) |
|---|---|
| B1 detect → A1 account and connection → A2 context → B2 `userConfig` → A3 lane (B3 applies it) → B4 restart → A4 verify through MCP → B5 hook check → A5 report | B1 detect → A2 context, read only → A4 verify through MCP → B5 hook check → A5 report |

## Rules

- **Never print an API key or a token**, in full or in part, in a report, a command you run, or a
  file. When a key is needed, take it from a variable or a file already on the machine and pass it by
  expansion (`"$VAR"`, `"$(cat …)"`) so the value never enters this conversation.
- **Never read a key out of a config file to build a request the user did not ask for.** Say where
  the key goes; do not go and fetch it.
- When you read an MCP config file, project out `type`, `url`, `command`, the Kagura proxy's `args`,
  and *whether* an `Authorization` header exists — never a header's or an environment variable's value.
- **Commands that echo a secret are redacted or not run here.** The Claude Code one is in B1. Of the
  Kagura CLI, `kagura auth status` and `kagura doctor` print a token or key *preview*, and
  `kagura auth token` prints the raw token: the user runs those in their own terminal, never you.
  `kagura auth list --json` carries no secret and is the one to read.
- **Never open or edit `~/.kagura/credentials.json`.** It holds the CLI's refresh tokens; the CLI
  owns it. Change it only through `kagura auth …` commands the user runs.
- Ask before every write, name the exact file, and show the before/after of the one value changing.

---

# Part A — Core (any harness)

### A1. Account and connection — hand them to the CLI

When the client has **no Kagura MCP entry** (Claude Code: B1), or the user has **no account
yet**, do not build either by hand. The Kagura CLI owns both: it signs in with the OAuth device flow, keeps a refreshing
credential profile, and writes the client's MCP entry. The user runs these in their **own terminal**
— the login waits for a browser, and nothing it prints belongs in this conversation.

1. **Install** — Python package `kagura-memory` **0.31.0 or later**, which ships two commands:
   `kagura` (the CLI) and `kagura-mcp` (the refresh-aware stdio proxy the MCP entry launches).

   ```bash
   pip install -U "kagura-memory>=0.31.0"
   # or, without installing, for the CLI commands alone:
   uvx --from "kagura-memory>=0.31.0" kagura auth login --server https://<host>
   ```

   `uvx` is enough to sign in, but the MCP entry launches `kagura-mcp` on every start, so that
   command must stay on the `PATH` of whatever starts the client — a `pip install` (or
   `uv tool install "kagura-memory>=0.31.0"`) does that, a one-off `uvx` run does not.

   The TypeScript package `kagura-memory` (npm, **0.8.0 or later**; `npx kagura-memory …`) ships a
   mirroring bin named `kagura-memory`, not `kagura`. It signs in the same way
   (`kagura-memory auth login`, whose `--server` takes the MCP URL `https://<host>/mcp`), but it has
   no `kagura-mcp`: its `setup claude` refuses `--profile` and writes only the static API-key entry.
   For the refreshing entry, use the Python package.

2. **Sign in** — `kagura auth login --server https://<host>`

   `--server` is the **site root** for the Python CLI: the profile's MCP URL becomes
   `<server>/mcp`, so passing `…/mcp` here ends in `/mcp/mcp`. The command prints a code and a URL
   and opens the browser (`--no-browser` on SSH / headless). `--profile <name>` keeps a second
   account apart; the first profile is `default`.

   On a deployment with closed sign-up, the account must exist first: open the invite link in the
   browser and sign up there, then run the login.

3. **Connect the client** — the harness's own setup command. Claude Code: B1, "No Kagura entry".

Then restart the client so it loads the new entry, and run this skill again. Do not re-implement
the login, write a credential, or call the server with a token yourself.

### A2. Choose the context

```
list_contexts()
```

Pick the context whose guardrails should apply — the one matching this project, or the single one if
there is only one — and confirm the name with the user. Never ask for a pasted UUID; you have the id
from `list_contexts`. One context serves all projects in v1.

**No context at all** — a brand-new account has a workspace and no context. Say so, and offer to
create one:

1. Ask for a name — lowercase letters, digits, `-` and `_`, unique in the workspace; suggest this
   project's directory name.
2. Show `create_context(name="<name>")` and get an explicit yes. Never create a context silently,
   and never more than one.
3. Take `context_id` from the result. Only a workspace owner or admin can create one; on a
   permission error, say so and stop — someone with that role creates it.

In check mode, report `no context yet` and go on; do not offer to create one.

### A3. Pick the guardrail lane

A context's tool guardrails reach the agent by exactly one of two lanes:

| Lane | When | MCP URL |
|---|---|---|
| **Hooks** — the client runs the plugin's hook script before each tool call | the harness has hooks and they are configured (Claude Code: B2) | carries `guardrails=off`, so the server does not also send a digest of the same memories |
| **Server digest** — `get_context_info` carries a `guardrails` block | no hooks, or they are not set up | no `guardrails` parameter |

Recommend the hooks lane when the harness supports them. The duplicate digest is harmless, only
wasteful, so the user may skip the URL change; B3 applies it for Claude Code, including the case
where the change needs a re-authentication.

### A4. Verify through MCP

This proves the connection and the context in any harness, with whatever credential the MCP entry
uses — an OAuth sign-in, a CLI profile or a Bearer key. It needs no key in this shell, so it works
even when the hooks' API key lives only in the harness's keychain. It reads, never writes, so it
runs in check mode too.

```
get_context_info(context_id="<uuid>")
```

Report its `guardrails` key — its three states are the lane check:

| `guardrails` | Meaning |
|---|---|
| **absent** | this connection's URL carries `guardrails=off` — the hooks lane. In the digest lane it is a misconfiguration: nothing delivers guardrails |
| `null` | no context resolved, or the read failed — recheck the id from A2 |
| an object | the digest lane: report `total_available` (and `truncated`). In the hooks lane it means `guardrails=off` is not on the URL yet (A3) |

Then:

```
load_guardrails(context_id="<uuid>")
```

Report `context_name`, `tool_triggered_total_available` (the tool guardrails) and
`pinned_total_available`. That is the number the hooks should fetch; B5 compares its own against
it. `0` tool guardrails is a valid, connected answer: the context has no `details.tool_trigger`
memories yet.

If the Kagura MCP tools are not loaded in this session at all — the entry is new, unapproved or
disconnected — say `MCP not verified — the Kagura tools are not loaded in this session` and name the
restart or sign-in that loads them. Never fall back to calling the server with a credential.

### A5. Report

One block. The core contributes three rows; the harness adapter adds its own around them (Claude
Code: B7 has the full block).

```
  context      <name> (<uuid>)                      — or: no context yet
  Lane         hooks — guardrails=off on the MCP URL — yes
  MCP          get_context_info: guardrails absent (hooks lane) — load_guardrails: 7 tool guardrails, 2 pinned
```

Then the remaining manual steps, if any: the CLI install and login (A1), the context to create (A2),
the adapter's own steps. In check mode, end with the verdict only and state plainly that nothing was
changed.

---

<!-- BEGIN claude-code adapter -->

# Part B — Claude Code adapter

Everything below is specific to Claude Code: its MCP configuration, its plugin system and the
plugin's hook script. Another harness replaces this part and keeps Part A.

### B1. Detect the effective MCP entry

```bash
claude mcp list
# claude mcp get prints configured headers AND environment variables WITH their values, so
# redact them before reading the output: those lines, and nothing else, are indented four spaces.
claude mcp get kagura-memory | sed -E 's/^(    [A-Za-z0-9_-]+)([:=]).*/\1\2 <redacted>/'
```

`claude mcp list` prints every configured server with its URL (or command) and health, and — when
one name is defined in more than one scope — an `MCP config diagnostics` block with a
`[Conflicting scopes]` warning. `claude mcp get <name>` prints only the entry that **wins**:
`Scope`, `Status`, `Type`, then `URL` for an http entry or `Command` / `Args` for a stdio entry, plus
`Headers:`, `Environment:` and `OAuth:` blocks when the entry has them.

`claude mcp get` prints configured headers with their values — and environment variables with
theirs — so a Bearer entry puts its key on your screen. The `sed` blanks header and environment
*values* and leaves `Scope` / `Status` / `Type` / `URL` / `Command` / `Args` untouched; a redacted
`Authorization:` line is still the signal that the entry carries a Bearer key.
Never run `claude mcp get` unfiltered.

Precedence, strongest first. A stronger scope shadows a weaker one of the same name silently, which
is why editing the wrong file appears to do nothing:

| Scope | Where it lives |
|---|---|
| `local` | `~/.claude.json` → `projects["<absolute project path>"].mcpServers` |
| `project` | `.mcp.json` in the project root, and the user-level `~/.claude/.mcp.json` |
| `user` | `~/.claude.json` → `mcpServers` |

To read a `.mcp.json` without touching header or environment values:

```bash
python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for n,s in (d.get("mcpServers") or {}).items():
    hdr="bearer" if (s.get("headers") or {}).get("Authorization") else "no-header"
    if s.get("command")=="kagura-mcp":
        print(n, s.get("type"), "kagura-mcp", " ".join(s.get("args") or []), hdr)
    else:
        print(n, s.get("type"), s.get("url") or s.get("command"), hdr)' .mcp.json
```

**Entry forms.** A Kagura entry is one of three:

| Form | How to recognise it | Auth | Where its URL is |
|---|---|---|---|
| **OAuth (Claude Code)** | `Type: http` (or `url`), no `Authorization` header; `Needs authentication`, or `Connected` after `/mcp` sign-in | Claude Code's own OAuth token, stored per endpoint | the entry's `URL` |
| **Bearer key** | `Type: http` (or `url`), an `Authorization` header | a static API key in the entry | the entry's `URL` |
| **CLI profile** | `Type: stdio`, `Command: kagura-mcp` — what `kagura setup claude --profile <name>` writes | the CLI's refreshing OAuth profile; the entry holds **no secret** | **not in the MCP config** — see below |

**The upstream URL of a CLI-profile entry.** `kagura-mcp` forwards to, in order:

1. `--server <url>` in the entry's `Args`, when present — used verbatim, query included.
2. Otherwise the profile's MCP URL. The profile is `--profile <name>` in `Args`, or the CLI's
   default profile when there is none.

Read the profiles without opening the credential file:

```bash
kagura auth list --json | python3 -c 'import json,sys
for p in json.load(sys.stdin):
    print(p["profile"], "default" if p["default"] else "-", p["server"].rstrip("/") + "/mcp",
          "refreshable" if p["refreshable"] else "NOT refreshable")'
```

The profile's MCP URL is always its `server` + `/mcp`. An empty list, or no row for the profile
`Args` names, means the proxy cannot start: A1, step 2. `NOT refreshable` with an expired token needs
a new `kagura auth login`. A CLI-profile entry that `claude mcp list` shows as failing usually means
`kagura-mcp` is not on the `PATH` of the process that starts Claude Code, or the profile is gone. If
`kagura` is not on this shell's `PATH`, report `CLI profile — upstream URL unknown (kagura not on
PATH)` and ask the user to run `kagura auth list` where Claude Code starts; do not guess the URL.

Report, as a block:

- **Effective entry** — name, the file it comes from, the scope.
- **Form** — OAuth, Bearer key, or CLI profile (with the profile name).
- **URL** — the entry's URL, or for a CLI profile the upstream URL and where it came from
  (`--server`, or the profile). The path (`/mcp`, or `/mcp/w/<workspace-id>`) and each query
  parameter separately (`profile`, `guardrails`).
- **Shadowed entries** — every other scope defining the same name, with its file and URL. Fix:
  `claude mcp remove <name> -s <scope>` on the one that should not be there. OAuth tokens are stored
  per endpoint, so two OAuth entries with different URLs cannot share a sign-in.
- **Duplicate plugin installs** — `claude plugin list` and look for two `kagura-memory` rows, e.g. a
  claude.ai-synced install next to a marketplace install; Claude Code picks one and warns. Which one
  carries the hooks: `claude plugin details kagura-memory@<marketplace>` — the inventory line
  `Hooks (4)  SessionStart, PreToolUse, PostToolUse, PostToolUseFailure`. Uninstall the other.
- **CLI-written hooks** — `kagura setup claude` also adds its **own** `kagura recall` (SessionStart)
  and `kagura remember` (PostToolUse) hooks to the project's `.claude/settings.json`, and
  `/kagura-recall` / `/kagura-remember` commands under `.claude/commands/`. They sync memories; they
  are not this plugin's guardrail hooks and deliver no guardrail. Report them if present; leave them
  alone.

**No Kagura entry** (setup mode) — A1: install the CLI and sign in, then the user runs, in the
project directory:

```bash
kagura setup claude --profile default   # the profile A1 signed in: default unless --profile named another
```

It writes the CLI-profile entry into this project's `.mcp.json` (plus `.kagura.json` and the hooks
above), lets the user pick or create the context, and verifies the connection. Pass `--profile`:
without it the command takes the legacy path, which bakes an API key into `.mcp.json` and
`.kagura.json`. Claude Code asks to approve a new project `.mcp.json` server at the next start;
after that, run this skill again. The user may instead keep Claude Code's own OAuth:
`claude mcp add --transport http kagura-memory https://<host>/mcp -s user`, then `/mcp` → sign in.

In check mode, a missing entry is the finding: report it and name A1.

### B2. `userConfig` — derive `server_url`

**`server_url` is the MCP endpoint, not the site root.** The hook POSTs `tools/call load_guardrails`
to `server_url` verbatim, so anything that is not the endpoint answers `http 404` or `http 405` and
no guardrail is ever delivered.

Derive it from B1 — never invent it, never ask the user to:

- The source is the effective entry's `URL`, or for a CLI-profile entry the upstream URL B1 resolved
  (`--server`, else the profile's `server` + `/mcp`). A CLI-profile entry has no `url` field — never
  read one from it.
- Keep the scheme, host and **path** exactly as they are: `https://<host>/mcp` or
  `https://<host>/mcp/w/<workspace-id>`.
- Drop the query, except that `guardrails=off` may stay. A `guardrails=<context-id>` left in
  `server_url` makes the hook print a warning at every session start.

Then show the values to enter — three of them, and where the fourth comes from:

| Option | Value |
|---|---|
| `server_url` | the endpoint derived above |
| `context_id` | the UUID from A2 |
| `max_action` | `block` (default), or `inform` to never deny |
| `api_key` | **the user enters this** — Workspace → Integrations → API Keys, `kagura_…` |

The hooks authenticate with a Bearer key only, so an **OAuth** entry and a **CLI-profile** entry
still need an API key here; it is a separate credential from the MCP sign-in and from the CLI
profile. A user key can read and author guardrails; an agent-bound key can only read them.

Applying it:

- **Not installed yet** — give the user this line to run in their own terminal, so the key never
  passes through the conversation:

  ```
  claude plugin install kagura-memory@kagura-memory-cloud --config server_url=<endpoint> --config context_id=<uuid> --config max_action=block --config api_key=<your key>
  ```

- **Already installed** — `/plugin` → kagura-memory → Configure, and paste the four values. Claude
  Code keeps them in the user's settings and keychain; they never reach the repository, and a
  project's `.claude/settings.json`, `.mcp.json`, `~/.claude.json`, `.kagura.json` and any
  `KAGURA_*` variable are never read by the hooks.

### B3. Apply `?guardrails=off` (the hooks lane from A3)

This is a change to the MCP entry, not to `server_url`: `guardrails=off` (`&guardrails=off` when the
URL already has a query, such as `?profile=core`). How depends on the form.

**OAuth or Bearer-key entry** — warn before changing it, and get an explicit yes:

> Changing the URL of an MCP entry that is authenticated with **OAuth** disconnects that server.
> Claude Code stores OAuth tokens per endpoint, so the new URL starts with no token and every Kagura
> tool disappears until you re-run `/mcp` and sign in again. A Bearer-key entry is not affected.

- Entry in a `.mcp.json` file (project root or `~/.claude/.mcp.json`) → change **only** the `url`
  string in place. Do not touch `headers` and do not rewrite the file.
- Entry in `~/.claude.json` (`local` / `user` scope) → do not hand-edit that file; it holds the whole
  client state. For an OAuth entry, `claude mcp remove <name> -s <scope>` then
  `claude mcp add --transport http <name> "<new url>" -s <scope>`. For a Bearer entry, print both
  commands and let the **user** run the `add` with its `--header`, so the key stays out of this
  conversation.

Afterwards: `/mcp` → the entry → authenticate, then B1's redacted `claude mcp get` to confirm
`Connected`.

**CLI-profile entry** — the CLI profile **cannot carry the query**. `kagura auth login` stores the
profile's MCP URL as `<server>/mcp`, and no `kagura` command edits it; putting the query into
`--server` at login breaks the URL. Say so plainly, then offer the one place that can carry it: the
proxy's own `--server` flag, which overrides the profile's URL verbatim. Add two items to the
entry's `args`, after `--profile <name>`:

```json
"args": ["--profile", "default", "--server", "https://<host>/mcp?guardrails=off"]
```

- The `--server` value is the profile's own MCP URL from B1 plus the query — **never another host**:
  the proxy sends the profile's token to whatever `--server` names.
- Entry in a `.mcp.json` file → change **only** that entry's `args` array in place. Entry in
  `~/.claude.json` → `claude mcp remove kagura-memory -s <scope>`, then
  `claude mcp add kagura-memory -s <scope> -- kagura-mcp --profile <name> --server "<url>"`.
- No re-authentication: the proxy owns the token, so the entry reconnects on the next start (or
  `/mcp` → reconnect) with the same sign-in.
- The pin has two costs; name them. If the profile later points at another server, `--server` still
  wins — update or drop it. Re-running `kagura setup claude --profile …` rewrites the entry and
  drops `--server` — apply it again.
- Never edit `~/.kagura/credentials.json` to change the URL.

If the user declines, record `Lane  hooks — guardrails=off not set (digest also sent)`.

### B4. Restart the hooks

The hooks read their configuration at `SessionStart`, so a new configuration takes effect in a
**new** session — start one, or `/clear`. A4 and B5 do not wait for that: A4 asks the server, and B5
runs the hook itself.

### B5. Hook check

A4 already proved the connection and the context. This proves the **hook's own** path: its
`server_url` and its API key, which A4 never touches.

#### B5a. Endpoint probe — no credentials

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
| any other `4xx` | an MCP endpoint answered and rejected the probe on its merits (`400` a malformed session, `406` a stricter `Accept`) — the path is right; go to B5b |
| `5xx` | the endpoint is there but the server is failing — retry, then check the deployment |
| `000`, timeout | host unreachable: DNS, TLS, proxy, or the server is down |
| `200` | something that answers an unauthenticated `tools/call` — not the Kagura endpoint |

#### B5b. Run the hook once

Only with the user's explicit go-ahead, and only with a key already on the machine — an exported
variable, or a file the user names. Never paste a key into the command text. When the key lives only
in the Claude Code keychain, skip this and rely on A4: report
`hook fetch not verified — no API key in this shell`.

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
| `additionalContext`: `Kagura Memory: N tool guardrails active for context <uuid> (fetched)` | **verified** — report N and the context name; N should match A4's tool-guardrail count |
| `server_url must be the MCP endpoint, …` | `server_url` is not the endpoint (the `404`/`405` case) |
| `server unreachable and no usable cache` | the fetch failed for another reason — key, TLS, network |
| `<field> is missing or invalid` | that value did not parse: a `context_id` that is not a UUID, an empty key |
| `this URL also requests the server digest (guardrails=<context>)` | drop `guardrails=<id>` from `server_url`, or set it to `off` |
| nothing at all | none of the three values reached the hook — check the variable spelling |
| `N` is 0, no message | connected; the context simply has no `details.tool_trigger` memories yet |

A hook N lower than A4's usually means the hook's key is a different identity (for example an
agent-bound key) from the MCP connection's.

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

### B6. When nothing happens

Silence is this plugin's failure mode — a misconfiguration looks exactly like a context with no
guardrails. In order:

1. `claude plugin details kagura-memory@<marketplace>` — does the install that Claude Code uses list
   `Hooks (4)`?
2. `ls "$HOME/.claude/plugins/data/"kagura-memory-*/guardrails/` — a `<context_id>.json` means a
   session-start fetch has succeeded at least once.
3. A4 — does the server have tool guardrails for this context at all?
4. B5a — is `server_url` the endpoint at all?
5. B5b — what does the hook actually say?
6. `python3 --version` — 3.9+, on `PATH`, and not inside the project. Windows is unsupported for
   these hooks.

### B7. Report rows

The full Claude Code block — B1–B5 rows around A5's three:

```
Kagura Memory setup
  MCP entry    kagura-memory — project (.mcp.json) — CLI profile (kagura-mcp --profile default)
  Upstream     https://<host>/mcp?guardrails=off — from --server (profile default: https://<host>/mcp)
  Shadowed     user (~/.claude.json) — OAuth — https://<host>/mcp
  Plugin       kagura-memory@kagura-memory-cloud 0.74.0 — Hooks (4)
  server_url   https://<host>/mcp
  context      <name> (<uuid>)
  Lane         hooks — guardrails=off on the MCP URL — yes
  MCP          get_context_info: guardrails absent (hooks lane) — load_guardrails: 7 tool guardrails, 2 pinned
  Endpoint     401 — endpoint reachable, credentials required
  Hooks        fetched 7 tool guardrails for <name>
```

Its manual steps: the OAuth sign-in (`/mcp`), the API key to paste into `/plugin`, the shadowed entry
to remove, the duplicate install to uninstall. In check mode, when B5b had no key, write
`hook fetch not verified — no API key in this shell` next to the MCP row rather than asking for one.

<!-- END claude-code adapter -->
