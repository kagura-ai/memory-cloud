"""Codex adapter for the Kagura Memory tool-guardrail hook.

Loaded by file path from ``kagura_guardrails.py`` (its ``--client codex``
dispatch) and never imported by name: under ``python3 -I -S`` the script
directory is not on ``sys.path``. The entry script calls ``make_adapter(core)``
and drives the adapter through the duck-typed interface described in its
docstring; the core owns the cache, the matcher, the markers, the rendering
and the fetch. This module supplies only what is Codex-specific:

* **Settings** — ``${PLUGIN_DATA}/config.json`` (fallback ``${CLAUDE_PLUGIN_DATA}``;
  neither → every hook is a silent no-op): ``context_id`` (UUID, required),
  ``max_action`` (``block`` default | ``inform``) and ``mcp_server`` (the
  ``[mcp_servers.<name>]`` table name, default ``kagura-memory``). Unknown keys
  are ignored.
* **Credentials** — the user-level ``$CODEX_HOME/config.toml`` (``CODEX_HOME``,
  else ``$HOME/.codex``), table ``mcp_servers.<mcp_server>``: ``url`` plus
  exactly one of ``bearer_token_env_var``, ``env_http_headers.Authorization``
  or ``http_headers.Authorization``. Project ``.codex/config.toml`` layers,
  profiles, ``-c`` overrides, ``hooks.state`` and every fixed variable name are
  never read. ``tomllib`` (3.11+) is imported only here; without it SessionStart
  and ``--refresh`` skip the fetch (SessionStart prints one notice saying so) and
  tool events keep running from an existing cache.
* **Events** — Codex stdin fields ``session_id`` / ``turn_id`` / ``agent_id`` /
  ``tool_name`` / ``tool_input`` / ``tool_response`` / ``source``; the subject
  rules of the contract's Codex rows (``Bash`` → the command; ``apply_patch`` →
  aliases ``Edit``, ``Write`` and one subject per patch header path; anything
  else → compact JSON of the arguments); ``PostToolUse`` → ``result_subject``.
* **Budget** — every model-visible string ≤ 2,000 tokens by Codex's own
  ``(utf8_bytes + 3) // 4`` estimate, under its 2,500-token spill threshold.

Credentials are resolved lazily. The core calls ``adapter.resolve(env)`` for
every event without saying which one; only ``SessionStart`` (which reads
``Resolution.warnings`` / ``bad_fields``) and ``--refresh`` (which reads
``config.url`` / ``config.authorization``) ever open ``config.toml`` or a
socket. Tool events read ``config.json``, the cache and the state directory
only, so the hot path never imports ``tomllib``.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

DEFAULT_MCP_SERVER = "kagura-memory"
MCP_SERVER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
CONFIG_JSON_CAP_BYTES = 64 * 1024
CONFIG_TOML_CAP_BYTES = 4 * 1024 * 1024
CODEX_RENDER_BUDGET_TOKENS = 2000

APPLY_PATCH_ALIASES = ("Edit", "Write")
APPLY_PATCH_HEADERS = (
    "*** Add File: ",
    "*** Update File: ",
    "*** Delete File: ",
    "*** Move to: ",
)
AUTH_SOURCES = (
    "bearer_token_env_var",
    "env_http_headers.Authorization",
    "http_headers.Authorization",
)
UNSUPPORTED_AUTH = ("oauth", "chatgpt")
TOMLLIB_MESSAGE = (
    "Codex credentials need Python 3.11+; no fetch at session start or refresh, "
    "tool events use the existing cache only"
)


def make_adapter(core: Any) -> CodexAdapter:
    """Entry point called by ``kagura_guardrails._load_codex_adapter``."""
    return CodexAdapter(core)


class CodexCredentialsUnavailable(Exception):
    """A lazily resolved credential was read but the Codex table is unusable.

    Only the ``--refresh`` path can reach this (SessionStart reports the
    problem through ``Resolution.bad_fields`` instead); the core's outermost
    handler turns it into a silent exit 0.
    """


# ---------------------------------------------------------------------------
# Settings: ${PLUGIN_DATA}/config.json
# ---------------------------------------------------------------------------


class Settings:
    __slots__ = ("context_raw", "max_action_raw", "mcp_server")

    def __init__(self, context_raw: Any, max_action_raw: Any, mcp_server: str) -> None:
        self.context_raw = context_raw
        self.max_action_raw = max_action_raw
        self.mcp_server = mcp_server


def data_dir_from_env(env: Any) -> str | None:
    value = env.get("PLUGIN_DATA") or env.get("CLAUDE_PLUGIN_DATA")
    return str(value) if value else None


def load_settings(data_dir: str) -> tuple[Settings | None, str | None]:
    """``(settings, problem)``: ``(None, None)`` = file absent or treated as absent.

    A file that exists but cannot be parsed yields ``(None, "config.json (unreadable)")``
    so SessionStart can name it once; an invalid ``mcp_server`` makes the whole file
    absent (never a guessed table name).
    """
    path = os.path.join(data_dir, "config.json")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(CONFIG_JSON_CAP_BYTES + 1)
    except OSError:
        return None, None
    if len(raw) > CONFIG_JSON_CAP_BYTES:
        return None, "config.json (unreadable)"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "config.json (unreadable)"
    if not isinstance(data, dict):
        return None, "config.json (unreadable)"
    mcp_server = data.get("mcp_server", DEFAULT_MCP_SERVER)
    if mcp_server is None:
        mcp_server = DEFAULT_MCP_SERVER
    if not isinstance(mcp_server, str) or not MCP_SERVER_RE.match(mcp_server):
        return None, None
    return Settings(data.get("context_id"), data.get("max_action"), mcp_server), None


# ---------------------------------------------------------------------------
# Credentials: $CODEX_HOME/config.toml, table mcp_servers.<name>
# ---------------------------------------------------------------------------


class Credentials:
    __slots__ = ("server_url", "url", "authorization", "bad_fields", "warnings", "tomllib_missing")

    def __init__(self) -> None:
        self.server_url: str | None = None
        self.url: Any = None
        self.authorization: str | None = None
        self.bad_fields: list[str] = []
        self.warnings: list[str] = []
        self.tomllib_missing = False

    @property
    def usable(self) -> bool:
        return (
            not self.tomllib_missing
            and not self.bad_fields
            and self.url is not None
            and self.authorization is not None
        )


def _authorization_header_entry(headers: Any) -> tuple[bool, Any]:
    """``(present, value)`` for the ``Authorization`` key of a header table (any case)."""
    if not isinstance(headers, dict):
        return False, None
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == "authorization":
            return True, value
    return False, None


def resolve_codex_credentials(core: Any, env: Any, mcp_server: str) -> Credentials:
    """Read ``mcp_servers.<mcp_server>`` from the user-level ``config.toml`` once.

    Every problem is a field name in ``bad_fields`` (the table and the key, never a
    value, never the URL). The only dynamic environment read of the whole hook is the
    variable named by ``bearer_token_env_var`` / ``env_http_headers.Authorization``.
    """
    creds = Credentials()
    try:
        import tomllib
    except ImportError:
        creds.tomllib_missing = True
        return creds

    table_name = f"mcp_servers.{mcp_server}"
    codex_home = env.get("CODEX_HOME")
    if not codex_home:
        home = env.get("HOME")
        if not home:
            creds.bad_fields.append("config.toml (CODEX_HOME and HOME unset)")
            return creds
        codex_home = os.path.join(str(home), ".codex")
    path = os.path.join(str(codex_home), "config.toml")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(CONFIG_TOML_CAP_BYTES + 1)
    except OSError:
        creds.bad_fields.append("config.toml (not found)")
        return creds
    if len(raw) > CONFIG_TOML_CAP_BYTES:
        creds.bad_fields.append("config.toml (too large)")
        return creds
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        creds.bad_fields.append("config.toml (parse error)")
        return creds
    servers = data.get("mcp_servers")
    table = servers.get(mcp_server) if isinstance(servers, dict) else None
    if not isinstance(table, dict):
        creds.bad_fields.append(f"{table_name} (table missing)")
        return creds

    server_url = table.get("url")
    url = core.parse_server_url(server_url)
    if url is None:
        creds.bad_fields.append(f"{table_name}.url")
    else:
        creds.server_url = str(server_url).strip()
        creds.url = url
        if core.url_requests_server_digest(url.query):
            creds.warnings.append(core.GUARDRAILS_PARAM_WARNING)
    if table.get("http_headers_helper") is not None:
        creds.bad_fields.append(f"{table_name}.http_headers_helper (unsupported)")

    # Exactly one Authorization source; the hook never chooses between credentials.
    sources: list[tuple[str, Any, bool]] = []  # (field, spec, spec names a variable)
    if table.get("bearer_token_env_var") is not None:
        sources.append(("bearer_token_env_var", table.get("bearer_token_env_var"), True))
    present, spec = _authorization_header_entry(table.get("env_http_headers"))
    if present:
        sources.append(("env_http_headers.Authorization", spec, True))
    present, spec = _authorization_header_entry(table.get("http_headers"))
    if present:
        sources.append(("http_headers.Authorization", spec, False))

    if len(sources) > 1:
        names = ", ".join(field for field, _spec, _dyn in sources)
        creds.bad_fields.append(
            f"{table_name} Authorization ({len(sources)} sources: {names}; keep exactly one)"
        )
        return creds
    if not sources:
        auth = table.get("auth")
        if isinstance(auth, str) and auth.lower() in UNSUPPORTED_AUTH:
            creds.bad_fields.append(f"{table_name}.auth (oauth/chatgpt unsupported)")
        else:
            creds.bad_fields.append(
                f"{table_name} Authorization (none of {', '.join(AUTH_SOURCES)})"
            )
        return creds

    field, spec, names_variable = sources[0]
    if not isinstance(spec, str) or not spec.strip():
        creds.bad_fields.append(f"{table_name}.{field}")
        return creds
    if names_variable:
        variable_name = spec.strip()
        value = env.get(variable_name)  # the one dynamic environment read
        if not isinstance(value, str) or not value.strip():
            creds.bad_fields.append(f"{table_name}.{field} (variable unset or empty)")
            return creds
    else:
        value = spec
    if "\r" in value or "\n" in value:
        creds.bad_fields.append(f"{table_name}.{field} (line break in value)")
        return creds
    if field == "bearer_token_env_var":
        creds.authorization = "Bearer " + value.strip()
    else:
        creds.authorization = value
    return creds


# ---------------------------------------------------------------------------
# Resolution and config objects (duck-typed against the core's Resolution / Config)
# ---------------------------------------------------------------------------


class CodexConfig:
    """What the core reads from a config; ``url`` / ``authorization`` resolve on first use."""

    __slots__ = ("context_id", "max_action", "data_dir", "_resolution")

    def __init__(
        self, context_id: str, max_action: str, data_dir: str, resolution: CodexResolution
    ) -> None:
        self.context_id = context_id
        self.max_action = max_action
        self.data_dir = data_dir
        self._resolution = resolution

    @property
    def guardrails_dir(self) -> str:
        return os.path.join(self.data_dir, "guardrails")

    @property
    def cache_path(self) -> str:
        return os.path.join(self.guardrails_dir, self.context_id + ".json")

    @property
    def server_url(self) -> str:
        return str(self._resolution.credentials_or_raise().server_url)

    @property
    def url(self) -> Any:
        return self._resolution.credentials_or_raise().url

    @property
    def authorization(self) -> str:
        return str(self._resolution.credentials_or_raise().authorization)


class CodexResolution:
    """Outcome of ``CodexAdapter.resolve``.

    ``configured`` and ``config`` are cheap (``config.json`` only). ``warnings`` and
    ``bad_fields`` — read only by the core's SessionStart handler — force the
    ``config.toml`` read, after which ``config`` is ``None`` when the credentials are
    unusable, so the misconfiguration is named once and no request is made.
    """

    def __init__(
        self,
        core: Any,
        env: Any,
        data_dir: str | None,
        settings: Settings | None,
        settings_problem: str | None,
    ) -> None:
        self._core = core
        self._env = env
        self._settings = settings
        self._settings_bad: list[str] = [settings_problem] if settings_problem else []
        self._settings_warnings: list[str] = []
        self._credentials: Credentials | None = None
        self.configured = data_dir is not None and (settings is not None or bool(settings_problem))
        self._config: CodexConfig | None = None
        if data_dir is not None and settings is not None:
            context_raw = settings.context_raw
            context_id = core.canonical_uuid(
                context_raw.strip() if isinstance(context_raw, str) else None
            )
            if context_id is None:
                self._settings_bad.append("config.json context_id")
            max_action, valid = core.normalize_max_action(settings.max_action_raw)
            if not valid:
                self._settings_warnings.append("max_action is not block or inform; using inform")
            if context_id is not None:
                self._config = CodexConfig(context_id, max_action, data_dir, self)

    def _ensure_credentials(self) -> Credentials:
        if self._credentials is None:
            if self._settings is None:
                self._credentials = Credentials()
            else:
                self._credentials = resolve_codex_credentials(
                    self._core, self._env, self._settings.mcp_server
                )
        return self._credentials

    def credentials_or_raise(self) -> Credentials:
        creds = self._ensure_credentials()
        if not creds.usable:
            raise CodexCredentialsUnavailable("codex credentials")
        return creds

    @property
    def warnings(self) -> list[str]:
        creds = self._ensure_credentials()
        out = list(self._settings_warnings) + list(creds.warnings)
        if creds.tomllib_missing:
            out.append(TOMLLIB_MESSAGE)
        return out

    @property
    def bad_fields(self) -> list[str]:
        creds = self._ensure_credentials()
        return list(self._settings_bad) + list(creds.bad_fields)

    @property
    def config(self) -> CodexConfig | None:
        if not self.configured or self._config is None:
            return None
        if self._credentials is not None and not self._credentials.usable:
            return None
        return self._config


# ---------------------------------------------------------------------------
# Event mapping
# ---------------------------------------------------------------------------


def apply_patch_paths(patch: Any) -> list[str]:
    """One subject per ``*** Add/Update/Delete File:`` / ``*** Move to:`` header path."""
    if not isinstance(patch, str):
        return []
    out: list[str] = []
    for line in patch.replace("\r\n", "\n").split("\n"):
        for header in APPLY_PATCH_HEADERS:
            if line.startswith(header):
                path = line[len(header) :].strip()
                if path:
                    out.append(path.replace("\\", "/"))
                break
    return out


def codex_pre_subjects(core: Any, tool_name: str, tool_input: Any) -> list[str]:
    inputs = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash":
        command = inputs.get("command")
        return [command] if isinstance(command, str) else []
    if tool_name == "apply_patch":
        return apply_patch_paths(inputs.get("command"))
    return [core.compact_json(tool_input)]


def codex_aliases(tool_name: str) -> list[str]:
    return list(APPLY_PATCH_ALIASES) if tool_name == "apply_patch" else []


class CodexAdapter:
    client = "codex"
    render_budget_chars: int | None = None
    render_budget_tokens: int | None = CODEX_RENDER_BUDGET_TOKENS

    def __init__(self, core: Any) -> None:
        self.core = core

    def resolve(self, env: Any) -> CodexResolution:
        data_dir = data_dir_from_env(env)
        if data_dir is None:
            return CodexResolution(self.core, env, None, None, None)
        settings, problem = load_settings(data_dir)
        return CodexResolution(self.core, env, data_dir, settings, problem)

    def plugin_version(self, env: Any) -> str:
        root = env.get("PLUGIN_ROOT")
        if not root:
            return "unknown"
        try:
            with open(
                os.path.join(str(root), ".codex-plugin", "plugin.json"), encoding="utf-8"
            ) as fh:
                version = json.load(fh).get("version")
        except (OSError, ValueError, AttributeError):
            return "unknown"
        return version if isinstance(version, str) and version else "unknown"

    def session_message(self, kind: str, fields: list[str]) -> str:
        if kind == "misconfigured":
            return f"{', '.join(fields)} is missing or invalid; hooks stay idle"
        return kind

    def subjects(self, event: dict[str, Any]) -> tuple[str | None, list[str], list[str]]:
        name = event.get("tool_name")
        if not isinstance(name, str):
            return None, [], []
        aliases = codex_aliases(name)
        hook_event = event.get("hook_event_name")
        if hook_event == "PreToolUse":
            return name, aliases, codex_pre_subjects(self.core, name, event.get("tool_input"))
        if hook_event == "PostToolUse":
            return name, aliases, [self.core.result_subject(event.get("tool_response"))]
        return name, aliases, []
