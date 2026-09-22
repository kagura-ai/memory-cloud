"""Guard tests for #1624: the Codex CLI ``config.toml`` snippets must load in Codex.

Codex validates every ``[mcp_servers.<name>]`` table when it deserialises
``~/.codex/config.toml``. An inline ``bearer_token`` on a streamable-HTTP
server is rejected (``bearer_token is not supported for streamable_http``)
and, because ``mcp_servers`` is one map of validated entries, the WHOLE file
then fails to load — the user loses every other server too. Unknown keys such
as ``type`` are ignored by default and become errors under ``--strict-config``.
A published snippet carrying either key therefore breaks a user's Codex the
moment they paste it, and nothing in Codex's error names the snippet.

``tomllib`` catches syntax only; the key allowlist below is what catches this
class of bug. The allowlist is the streamable-HTTP transport of
``codex-rs/config/src/mcp_types.rs`` at ``rust-v0.155.1`` plus the
transport-independent server fields of the same struct. Following
``tests/mcp_server/test_tool_usage_docs.py``, the tests parse the agent- and
user-facing Markdown itself, so the docs, the plugin skill and the web UI
(``MCPConfigBlock.test.tsx`` pins the TOML the UI renders) stay on one shape.
"""

from __future__ import annotations

import re
import textwrap
import tomllib
from pathlib import Path

import pytest

# backend/tests/test_codex_config_snippets.py -> tests -> backend -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Keys Codex accepts on a streamable-HTTP `[mcp_servers.<name>]` table.
STREAMABLE_HTTP_KEYS = frozenset(
    {
        "url",
        "bearer_token_env_var",
        "http_headers",
        "env_http_headers",
        "http_headers_helper",
    }
)
SHARED_SERVER_KEYS = frozenset(
    {
        "environment_id",
        "auth",
        "enabled",
        "required",
        "startup_timeout_sec",
        "startup_timeout_ms",
        "tool_timeout_sec",
        "supports_parallel_tool_calls",
        "omit_tools_from",
        "default_tools_approval_mode",
        "enabled_tools",
        "disabled_tools",
        "scopes",
        "oauth",
        "oauth_resource",
        "tools",
    }
)
ALLOWED_KEYS = STREAMABLE_HTTP_KEYS | SHARED_SERVER_KEYS

# The MCP server name every snippet registers, and the environment variable
# every snippet names for the API key (docs/getting-started.md exports it in
# its Quick API Test; the web UI's Codex tab emits the same name).
SERVER_NAME = "kagura-memory"
BEARER_TOKEN_ENV_VAR = "KAGURA_API_KEY"

# (relative path, heading substring). Fences are taken from the section whose
# heading contains the substring — up to the next heading of the same or a
# higher level — or from the whole file when the substring is None.
CODEX_SNIPPET_DOCS = [
    ("docs/getting-started.md", "Codex CLI"),
    ("docs/troubleshooting.md", "Codex CLI"),
    ("plugins/kagura-memory/skills/kagura-memory/SKILL.md", None),
]

# Every Markdown tree a Codex snippet could be published from.
MARKDOWN_TREES = ["docs", "plugins", "claude-skills"]
MARKDOWN_FILES = ["README.md"]

# A fenced ```toml block, possibly indented inside a list item.
_TOML_FENCE = re.compile(r"^[ \t]*```toml[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.MULTILINE | re.DOTALL)
_HEADING = re.compile(r"^(#+)[ \t]+(.*?)[ \t]*$", re.MULTILINE)


def _section(text: str, heading_substring: str | None) -> str:
    if heading_substring is None:
        return text
    headings = list(_HEADING.finditer(text))
    for index, match in enumerate(headings):
        if heading_substring not in match.group(2):
            continue
        level = len(match.group(1))
        end = next(
            (later.start() for later in headings[index + 1 :] if len(later.group(1)) <= level),
            len(text),
        )
        return text[match.start() : end]
    raise AssertionError(f"no heading containing {heading_substring!r}")


def _toml_fences(text: str) -> list[str]:
    return [textwrap.dedent(match.group(1)) for match in _TOML_FENCE.finditer(text)]


def _mcp_server_tables(fence: str) -> dict[str, dict]:
    return tomllib.loads(fence).get("mcp_servers", {})


def _markdown_files() -> list[Path]:
    files = [_REPO_ROOT / name for name in MARKDOWN_FILES]
    for tree in MARKDOWN_TREES:
        files.extend(sorted((_REPO_ROOT / tree).rglob("*.md")))
    return files


@pytest.mark.parametrize(("relative_path", "heading"), CODEX_SNIPPET_DOCS)
def test_codex_section_shows_a_config_entry_for_kagura_memory(relative_path, heading):
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    fences = _toml_fences(_section(text, heading))
    assert fences, f"{relative_path}: the Codex section shows no toml snippet"
    tables = [_mcp_server_tables(fence) for fence in fences]
    assert all(SERVER_NAME in servers for servers in tables), (
        f"{relative_path}: every toml snippet must register [mcp_servers.{SERVER_NAME}]"
    )


@pytest.mark.parametrize(("relative_path", "heading"), CODEX_SNIPPET_DOCS)
def test_codex_section_uses_one_auth_shape(relative_path, heading):
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for fence in _toml_fences(_section(text, heading)):
        server = _mcp_server_tables(fence)[SERVER_NAME]
        assert server.get("bearer_token_env_var") == BEARER_TOKEN_ENV_VAR, (
            f"{relative_path}: the snippet must name {BEARER_TOKEN_ENV_VAR} in bearer_token_env_var"
        )
        assert "/mcp/w/" in server["url"], f"{relative_path}: url is not a workspace endpoint"


def test_every_published_toml_snippet_uses_only_codex_streamable_http_keys():
    """Repo-wide sweep: no Markdown anywhere ships `bearer_token =` or `type =`."""
    offending: list[str] = []
    seen = 0
    for path in _markdown_files():
        for fence in _toml_fences(path.read_text(encoding="utf-8")):
            for name, table in _mcp_server_tables(fence).items():
                seen += 1
                for key in sorted(set(table) - ALLOWED_KEYS):
                    offending.append(f"{path.relative_to(_REPO_ROOT)}: [mcp_servers.{name}] {key}")
    assert seen, "no [mcp_servers.*] toml snippet found — the sweep matched nothing"
    assert offending == [], (
        "Codex rejects these keys on a streamable-HTTP server (an inline bearer_token "
        f"stops the whole config.toml from loading): {offending}"
    )
