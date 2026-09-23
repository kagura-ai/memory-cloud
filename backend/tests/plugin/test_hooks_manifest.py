"""Manifest and static pins for the Claude Code guardrail hooks (#1619).

Pins ``claude-hooks/hooks.json``, the ``hooks`` / ``userConfig`` fields of
``.claude-plugin/plugin.json``, the shape of the hook script (AST), the
environment-variable allowlist, the forbidden output tokens and the lint
configuration that reaches the script from outside ``backend/``.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.plugin.conftest import (
    CLAUDE_HOOKS_JSON,
    CLAUDE_PATH_VARIABLES,
    CLAUDE_PLUGIN_JSON,
    HOOK_SCRIPT,
    REPO_ROOT,
    hook_commands,
)

# The sh guard: absolute interpreter outside the project and ``$PWD``, ``-I -S``, the
# script under ``$CLAUDE_PLUGIN_ROOT`` — the variable Claude Code exports to the hook
# process, expanded by the shell inside double quotes. Never the braced
# ``${CLAUDE_PLUGIN_ROOT}``: Claude Code substitutes that placeholder textually into a
# shell-form hook command when it loads the plugin (plugins reference, "Environment
# variables": hook commands resolve it "anywhere the placeholder appears"), so a plugin
# path with ``$(``, backticks or ``"`` would become shell syntax before the guard runs.
# ``${CLAUDE_PROJECT_DIR:-/nonexistent}`` is a parameter expansion, not the placeholder.
GUARD_COMMAND = (
    'p=$(command -v python3) || exit 0; case "$p" in /*) ;; *) exit 0;; esac; '
    'case "$p" in "${CLAUDE_PROJECT_DIR:-/nonexistent}"/*|"$PWD"/*) exit 0;; esac; '
    'exec "$p" -I -S "$CLAUDE_PLUGIN_ROOT/plugins/kagura-memory/hooks/kagura_guardrails.py" '
    "--client claude"
)
SCRIPT_ARG = '"$CLAUDE_PLUGIN_ROOT/plugins/kagura-memory/hooks/kagura_guardrails.py"'
PATH_PLACEHOLDERS = ["${" + key + "}" for key in CLAUDE_PATH_VARIABLES]
REFRESH_MATCHER = "^mcp__.*__(remember|update_memory|forget)$"
# Claude Code hooks reference, "Matcher patterns" table: `"*"`, `""`, or omitted is
# evaluated as "Match all - fires on every occurrence of the event". It is a documented
# literal, not a regular expression, so `*` is the form the reference itself uses.
MATCH_ALL = "*"

ENV_ALLOWLIST = {
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_OPTION_SERVER_URL",
    "CLAUDE_PLUGIN_OPTION_API_KEY",
    "CLAUDE_PLUGIN_OPTION_CONTEXT_ID",
    "CLAUDE_PLUGIN_OPTION_MAX_ACTION",
}

# Assembled at runtime so this file never contains the tokens it forbids.
FORBIDDEN_TOKENS = [
    '"' + "allow" + '"',
    '"' + "ask" + '"',
    '"' + "defer" + '"',
    "updated" + "Input",
    "updated" + "ToolOutput",
    "updated" + "MCPToolOutput",
    '"' + "continue" + '"',
    "stop" + "Reason",
    "suppress" + "Output",
    '"' + "decision" + '"',
    "sys.exit(" + "2)",
]

# Docs and skills this PR touches; a hosted host name must not appear in them.
DOCS_TOUCHED = [
    "claude-skills/guide.md",
    "claude-skills/session-start.md",
    "claude-skills/remember.md",
    "claude-skills/session-summary.md",
    "claude-skills/setup.md",
    "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
    "docs/mcp-clients.md",
    "docs/mcp-tools.md",
    "docs/getting-started.md",
    "docs/troubleshooting.md",
    "README.md",
]


def _hooks_json() -> dict:
    return json.loads(CLAUDE_HOOKS_JSON.read_text(encoding="utf-8"))


def _plugin_json() -> dict:
    return json.loads(CLAUDE_PLUGIN_JSON.read_text(encoding="utf-8"))


def _source() -> str:
    return HOOK_SCRIPT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# plugin.json
# ---------------------------------------------------------------------------


def test_plugin_hooks_path_resolves_inside_plugin_root() -> None:
    manifest = _plugin_json()
    hooks = manifest["hooks"]
    assert hooks == "./claude-hooks/hooks.json"
    resolved = (REPO_ROOT / hooks).resolve()
    assert resolved.is_file()
    assert REPO_ROOT.resolve() in resolved.parents


def test_plugin_description_mentions_hooks() -> None:
    assert "tool-guardrail hooks" in _plugin_json()["description"]


def test_user_config_shape() -> None:
    cfg = _plugin_json()["userConfig"]
    assert list(cfg) == ["server_url", "api_key", "context_id", "max_action"]
    for key, field in cfg.items():
        assert field["type"] == "string", key
        assert field["title"] and field["description"], key
        assert "required" not in field, key
    assert cfg["api_key"]["sensitive"] is True
    assert cfg["max_action"]["default"] == "block"
    assert cfg["max_action"]["options"] == ["block", "inform"]
    assert "inject_pinned" not in cfg
    assert (
        "Add ?guardrails=off to the .mcp.json URL itself (&guardrails=off when the URL "
        "already has a query, such as ?profile=core)" in cfg["server_url"]["description"]
    )


def test_user_config_descriptions_carry_the_traps() -> None:
    """The two values a user can only get wrong silently (#1649)."""
    cfg = _plugin_json()["userConfig"]
    server_url = cfg["server_url"]["description"]
    assert "https://<host>/mcp" in server_url, "an example endpoint, never a real host"
    assert "/mcp/w/<workspace-id>" in server_url
    assert "never the site root" in server_url
    context_id = cfg["context_id"]["description"]
    assert "UUID" in context_id
    assert "list_contexts" in context_id


# ---------------------------------------------------------------------------
# claude-hooks/hooks.json
# ---------------------------------------------------------------------------


def test_hooks_file_top_level_keys() -> None:
    data = _hooks_json()
    assert set(data) == {"hooks"}, "no top-level description on the Claude hooks file"
    assert set(data["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse", "PostToolUseFailure"}


def test_every_command_is_the_guarded_string() -> None:
    for event, handlers in hook_commands().items():
        for handler in handlers:
            assert handler["type"] == "command", event
            expected = GUARD_COMMAND + (" --refresh" if handler.get("async") else "")
            assert handler["command"] == expected, event
            assert SCRIPT_ARG in handler["command"], event
            for placeholder in PATH_PLACEHOLDERS:
                assert placeholder not in handler["command"], (
                    f"{event}: Claude Code would substitute {placeholder} textually"
                )
            assert "${user_config" not in handler["command"]
            assert "args" not in handler


def test_timeouts_and_matchers() -> None:
    handlers = hook_commands()
    assert [h["timeout"] for h in handlers["SessionStart"]] == [5]
    assert handlers["SessionStart"][0]["matcher"] is None
    for event in ("PreToolUse", "PostToolUseFailure"):
        assert len(handlers[event]) == 1
        assert handlers[event][0]["timeout"] == 2
        assert handlers[event][0]["matcher"] == MATCH_ALL
    post = handlers["PostToolUse"]
    assert len(post) == 2
    sync = [h for h in post if not h.get("async")]
    assert sync[0]["timeout"] == 2 and sync[0]["matcher"] == MATCH_ALL
    for event, entries in handlers.items():
        for handler in entries:
            if "timeout" in handler:
                assert isinstance(handler["timeout"], int), event
            if "async" in handler:
                assert isinstance(handler["async"], bool), event


def test_exactly_one_async_handler_and_it_is_the_refresh() -> None:
    async_handlers = [
        (event, h) for event, hs in hook_commands().items() for h in hs if h.get("async") is True
    ]
    assert len(async_handlers) == 1
    event, handler = async_handlers[0]
    assert event == "PostToolUse"
    assert handler["matcher"] == REFRESH_MATCHER
    assert "timeout" not in handler, "Claude Code does not enforce timeout on async hooks"
    assert handler["command"].endswith(" --refresh")


def test_refresh_matcher_is_a_regex_and_compiles() -> None:
    matcher = REFRESH_MATCHER
    assert re.search(r"[^A-Za-z0-9_|]", matcher), "a plain name list would not be a regex"
    assert re.compile(matcher).search("mcp__kagura-memory__remember")
    assert not re.compile(matcher).search("mcp__kagura-memory__recall")


# ---------------------------------------------------------------------------
# Script shape (AST pins)
# ---------------------------------------------------------------------------


def test_script_parses_on_the_3_8_grammar() -> None:
    ast.parse(_source(), filename=str(HOOK_SCRIPT), feature_version=(3, 8))


def test_future_import_then_sys_then_version_guard() -> None:
    tree = ast.parse(_source())
    body = [node for node in tree.body if not isinstance(node, ast.Expr)]  # skip docstring
    first, second, third = body[0], body[1], body[2]
    assert isinstance(first, ast.ImportFrom) and first.module == "__future__"
    assert [a.name for a in first.names] == ["annotations"]
    assert isinstance(second, ast.Import) and [a.name for a in second.names] == ["sys"]
    assert isinstance(third, ast.If)
    src = ast.get_source_segment(_source(), third.test)
    assert src == "sys.version_info < (3, 9)", src
    assert "sys.exit(0)" in (ast.get_source_segment(_source(), third.body[0]) or "")


def _walk_with_parents(tree: ast.AST):
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent  # type: ignore[attr-defined]
    return ast.walk(tree)


def _enclosing_function(node: ast.AST) -> str | None:
    current = getattr(node, "_parent", None)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = getattr(current, "_parent", None)
    return None


def test_network_imports_only_inside_fetch() -> None:
    tree = ast.parse(_source())
    nodes = list(_walk_with_parents(tree))
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                top = name.split(".")[0]
                assert top != "subprocess", "the hook never spawns processes"
                assert top != "urllib", "no urllib: redirects are refused by construction"
                if top in ("http", "socket", "ssl", "tomllib"):
                    assert _enclosing_function(node) == "fetch_guardrails", (name, node.lineno)


def test_codex_adapter_is_loaded_by_path_not_by_name() -> None:
    source = _source()
    assert "import _codex_adapter" not in source
    assert "from _codex_adapter" not in source
    assert "import_module(" not in source
    assert "spec_from_file_location" in source


def test_no_match_statement_or_union_isinstance() -> None:
    tree = ast.parse(_source())
    match_type = getattr(ast, "Match", None)
    for node in ast.walk(tree):
        if match_type is not None:
            assert not isinstance(node, match_type)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "isinstance" and len(node.args) == 2:
                assert not isinstance(node.args[1], ast.BinOp), ast.unparse(node)


def test_environment_reads_are_allowlisted() -> None:
    """Every environment read names an allowlisted key; no dynamic read in the entry file."""
    tree = ast.parse(_source())
    reads: list[tuple[int, str | None]] = []

    def env_target(node: ast.AST) -> bool:
        if isinstance(node, ast.Name) and node.id in ("env", "environ"):
            return True
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and env_target(node.value):
            key = node.slice
            reads.append((node.lineno, key.value if isinstance(key, ast.Constant) else None))
        elif isinstance(node, ast.Call):
            func = node.func
            is_get = (
                isinstance(func, ast.Attribute) and func.attr == "get" and env_target(func.value)
            )
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            )
            if is_get or is_getenv:
                first = node.args[0] if node.args else None
                reads.append(
                    (node.lineno, first.value if isinstance(first, ast.Constant) else None)
                )
    assert reads, "expected at least one environment read"
    for lineno, key in reads:
        assert key is not None, f"dynamic environment read at line {lineno}"
        assert key in ENV_ALLOWLIST, f"line {lineno} reads {key!r}"


def test_forbidden_output_tokens_absent_from_source() -> None:
    source = _source()
    for token in FORBIDDEN_TOKENS:
        assert token not in source, token


def test_only_deny_decision_in_source() -> None:
    source = _source()
    assert '"deny"' in source
    assert source.count("permissionDecision") >= 2


# ---------------------------------------------------------------------------
# Lint configuration and docs hygiene
# ---------------------------------------------------------------------------


def _ruff_binary() -> str | None:
    found = shutil.which("ruff")
    if found:
        return found
    sibling = Path(sys.executable).with_name("ruff")
    return str(sibling) if sibling.exists() else None


@pytest.mark.skipif(_ruff_binary() is None, reason="ruff not available")
def test_ruff_settings_for_the_hook_dir() -> None:
    """The dir's ruff.toml is picked up: py39 target, pyupgrade rules enabled."""
    proc = subprocess.run(
        [str(_ruff_binary()), "check", "--show-settings", str(HOOK_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert re.search(r"target_version = (3\.9|py39)", proc.stdout), "target version not py39"
    assert re.search(r"\(UP\d{3}\)", proc.stdout), "pyupgrade rules must reach the hook script"


def test_makefile_lints_the_hook_dir() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "ruff check plugins/kagura-memory/hooks/" in makefile
    assert "ruff format --check plugins/kagura-memory/hooks/" in makefile
    assert "plugins/kagura-memory/hooks/" in makefile.split("type-check:")[1].split("\n\n")[0]


def test_ruff_toml_extends_backend_config() -> None:
    text = (REPO_ROOT / "plugins" / "kagura-memory" / "hooks" / "ruff.toml").read_text(
        encoding="utf-8"
    )
    assert 'extend = "../../../backend/pyproject.toml"' in text
    assert 'target-version = "py39"' in text


@pytest.mark.parametrize("relpath", DOCS_TOUCHED)
def test_touched_docs_carry_no_hosted_host(relpath: str) -> None:
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    # The public project site (www.) is the only host allowed under that domain;
    # the hosted service host never appears in public docs.
    pattern = re.compile(r"://([a-z0-9.-]*" + "kagura" + "-ai" + r"\.com)")
    hosts = {m.group(1) for m in pattern.finditer(text)}
    assert hosts <= {"www." + "kagura" + "-ai.com"}, f"{relpath} names a hosted host: {hosts}"


def test_hook_script_exists_and_is_not_executable_dependent() -> None:
    """The sh guard execs python3 on the file, so the file needs no exec bit."""
    assert HOOK_SCRIPT.is_file()
    assert Path(HOOK_SCRIPT).suffix == ".py"


# ---------------------------------------------------------------------------
# /kagura-memory:setup (#1649)
# ---------------------------------------------------------------------------

SETUP_SKILL = REPO_ROOT / "claude-skills" / "setup.md"


def _setup_skill() -> str:
    return SETUP_SKILL.read_text(encoding="utf-8")


def test_setup_skill_ships_as_a_plugin_command() -> None:
    assert _plugin_json()["commands"] == ["./claude-skills/"]
    assert SETUP_SKILL.is_file()
    text = _setup_skill()
    assert text.startswith("---\ndescription: "), "front matter must match the sibling skills"
    first = text.split("---", 2)[1].strip()
    assert first.startswith("description:") and "\n" not in first, "one front-matter key only"


@pytest.mark.parametrize(
    "needle",
    [
        # 1. detect the EFFECTIVE entry, not just some entry
        "claude mcp get",
        "shadow",
        "claude plugin list",
        # 2. the context comes from the tool, not from a pasted uuid
        "list_contexts()",
        # 3. server_url is derived from the active MCP URL
        "https://<host>/mcp",
        "/mcp/w/<workspace-id>",
        # 4. the OAuth trap, before the change
        "re-run `/mcp`",
        # 5. verification through MCP, then the hook itself
        'get_context_info(context_id="<uuid>")',
        'load_guardrails(context_id="<uuid>")',
        "kagura_guardrails.py",
        "CLAUDE_PLUGIN_OPTION_SERVER_URL",
        # 6. doctor mode
        "--check",
    ],
)
def test_setup_skill_covers_every_load_bearing_step(needle: str) -> None:
    assert needle in _setup_skill(), f"/kagura-memory:setup must cover {needle!r}"


def test_setup_skill_never_echoes_a_credential() -> None:
    text = _setup_skill()
    assert "Never print an API key" in text
    assert "never read a key out of a config file" in text.lower()
    # The key reaches the hook by expansion only, so no value enters the transcript.
    assert 'CLAUDE_PLUGIN_OPTION_API_KEY="$' in text
    # Nothing key-shaped in the file; the hook script's own name is the only kagura_ token.
    assert not re.search(r"kagura_(?!guardrails)[A-Za-z0-9]", text), "no key-shaped literal"


def test_setup_skill_redacts_what_claude_mcp_get_echoes() -> None:
    """`claude mcp get` prints configured headers with their values, key included (#1649)."""
    text = _setup_skill()
    assert "claude mcp get kagura-memory | sed" in text, "the detection step must pipe through sed"
    assert "<redacted>" in text
    assert "Never run `claude mcp get` unfiltered" in text
    assert "prints configured headers with their values" in text


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.DOTALL)


def _block_containing(text: str, needle: str) -> str:
    blocks = [b for b in _fenced_blocks(text) if needle in b]
    assert len(blocks) == 1, f"expected one code block containing {needle!r}, got {len(blocks)}"
    return blocks[0]


def _setup_core() -> str:
    """Part A — the harness-neutral core, up to the fenced Claude Code adapter."""
    text = _setup_skill()
    return text.split("# Part A — Core (any harness)", 1)[1].split(
        "<!-- BEGIN claude-code adapter -->"
    )[0]


def test_setup_skill_fences_everything_claude_specific() -> None:
    """The core must lift into a shared skill for other harnesses unchanged (#1649)."""
    text = _setup_skill()
    assert text.count("<!-- BEGIN claude-code adapter -->") == 1
    assert text.count("<!-- END claude-code adapter -->") == 1
    assert text.index("<!-- BEGIN claude-code adapter -->") < text.index(
        "<!-- END claude-code adapter -->"
    )
    core = _setup_core()
    for claude_only in (
        "claude mcp",
        "claude plugin",
        "CLAUDE_PLUGIN_",
        "kagura_guardrails.py",
        ".claude.json",
        "`/plugin`",
        "$ARGUMENTS",
        "setup claude",
        ".mcp.json",
    ):
        assert claude_only not in core, f"{claude_only!r} belongs in the Claude Code adapter"


def test_setup_skill_check_mode_triggers_on_natural_language() -> None:
    mode = _setup_skill().split("## Mode", 1)[1].split("\n## ")[0]
    assert "--check" in mode
    for word in ("*check*", "*diagnose*", "*doctor*"):
        assert word in mode, f"check mode must trigger on the user asking to {word}"
    assert "create no context" in mode, "check mode must not create a context"


def test_setup_skill_verifies_through_mcp_before_the_hook() -> None:
    """get_context_info + load_guardrails prove the connection in any harness (#1649)."""
    text = _setup_skill()
    core = _setup_core()
    assert 'get_context_info(context_id="<uuid>")' in core
    assert 'load_guardrails(context_id="<uuid>")' in core
    assert text.index("### A4. Verify through MCP") < text.index("### B5. Hook check")
    # Both run orders pass through the MCP check before the hook check.
    for order in text.split("## Run order", 1)[1].split("## Rules")[0].split("|"):
        if "B5" in order:
            assert order.index("A4") < order.index("B5")
    # The guardrails block's three states are what the report reads.
    a4 = core.split("### A4.", 1)[1].split("### A5.")[0]
    for state in ("**absent**", "`null`", "`total_available`", "`tool_triggered_total_available`"):
        assert state in a4
    # Works when the hooks' key lives only in the keychain.
    assert "needs no key in this shell" in a4
    assert "hook fetch not verified — no API key in this shell" in text


def test_setup_skill_offers_create_context_for_an_empty_workspace() -> None:
    a2 = _setup_core().split("### A2.", 1)[1].split("### A3.")[0]
    assert "no context" in a2.lower()
    assert 'create_context(name="<name>")' in a2
    assert "Never create a context silently" in a2
    assert "explicit yes" in a2
    assert "do not offer to create one" in a2, "check mode reports, never creates"


def test_setup_skill_hands_login_and_connection_to_the_cli() -> None:
    """Account creation and the MCP entry are the SDK CLI's job, not the skill's (#1649)."""
    text = _setup_skill()
    a1 = _setup_core().split("### A1.", 1)[1].split("### A2.")[0]
    # Real package names with the minimum version whose CLI has the commands cited.
    assert 'pip install -U "kagura-memory>=0.31.0"' in a1
    assert 'uvx --from "kagura-memory>=0.31.0" kagura' in a1
    assert "npx kagura-memory" in a1 and "0.8.0 or later" in a1
    assert "kagura auth login --server https://<host>" in a1
    assert "invite link" in a1, "closed sign-up needs the invite link first"
    # An older kagura on PATH has no `auth list --json`; the version is checked first.
    assert "`kagura --version`" in a1 and "0.31.0 or later" in a1
    assert "**stop**" in a1, "without the new entry the MCP tools the next steps call are absent"
    assert "kagura setup claude --profile default" in text
    # No re-implemented device flow.
    for reimplemented in ("device_code", "/oauth/", "grant_type"):
        assert reimplemented not in text


def test_setup_skill_recognises_the_stdio_proxy_entry() -> None:
    """A kagura-mcp entry has no url; its upstream lives in the CLI profile (#1649)."""
    text = _setup_skill()
    assert "Command: kagura-mcp" in text
    assert "kagura auth list --json" in text
    assert "A CLI-profile entry has no `url` field" in text
    # ?guardrails=off: the profile cannot carry it, the proxy's --server can.
    assert "the CLI profile **cannot carry the query**" in text
    assert '"--server", "https://<host>/mcp?guardrails=off"' in text
    assert "**never another host**" in text
    assert "git ls-files --error-unmatch .mcp.json" in text, "a tracked --server reaches teammates"
    # The credential file is the CLI's; the skill never opens it.
    assert "Never open or edit `~/.kagura/credentials.json`" in text
    for secret_printer in ("kagura auth status", "kagura auth token", "kagura doctor"):
        assert secret_printer in text.split("## Rules", 1)[1].split("\n---\n")[0]


def test_setup_skill_mcp_json_reader_handles_the_stdio_form(tmp_path: Path) -> None:
    """The .mcp.json projection runs on a stdio entry without a url and leaks no value."""
    block = _block_containing(_setup_skill(), 'd.get("mcpServers")')
    secret = "sentinel-value-" + "0123456789"
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kagura-memory": {
                        "type": "stdio",
                        "command": "kagura-mcp",
                        "args": [
                            "--profile",
                            "default",
                            "--server",
                            "https://<host>/mcp?guardrails=off",
                        ],
                        "env": {"SOME_TOKEN": secret},
                    },
                    "kagura-bearer": {
                        "type": "http",
                        "url": "https://<host>/mcp",
                        "headers": {"Authorization": "Bearer " + secret},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    out = subprocess.run(
        ["bash", "-c", block], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True
    ).stdout
    assert (
        "kagura-memory stdio kagura-mcp --profile default --server https://<host>/mcp?guardrails=off"
        in out
    )
    assert "kagura-bearer http https://<host>/mcp bearer" in out
    assert secret not in out


def test_setup_skill_mcp_json_reader_finds_a_proxy_by_path_or_launcher(tmp_path: Path) -> None:
    """kagura-mcp by absolute path or behind uvx is still the CLI-profile form."""
    block = _block_containing(_setup_skill(), 'd.get("mcpServers")')
    secret = "sentinel-value-" + "0123456789"
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "by-path": {
                        "type": "stdio",
                        "command": "/opt/tools/bin/kagura-mcp",
                        "args": ["--profile", "work"],
                    },
                    "by-uvx": {
                        "type": "stdio",
                        "command": "uvx",
                        "args": ["--from", "kagura-memory", "kagura-mcp"],
                    },
                    "lower-header": {
                        "type": "http",
                        "url": "https://<host>/mcp",
                        "headers": {"authorization": "Bearer " + secret},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    out = subprocess.run(
        ["bash", "-c", block], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True
    ).stdout
    assert "by-path stdio /opt/tools/bin/kagura-mcp --profile work no-header" in out
    assert "by-uvx stdio uvx --from kagura-memory kagura-mcp no-header" in out
    assert "lower-header http https://<host>/mcp bearer" in out
    assert secret not in out


def test_setup_skill_profile_reader_derives_the_mcp_url(tmp_path: Path) -> None:
    block = _block_containing(_setup_skill(), "kagura auth list --json |")
    sample = tmp_path / "profiles.json"
    sample.write_text(
        json.dumps(
            [
                {
                    "profile": "default",
                    "default": True,
                    "user_email": "someone@example.invalid",
                    "server": "https://<host>",
                    "refreshable": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    script = block.replace("kagura auth list --json", f"cat '{sample}'")
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    assert "default default https://<host>/mcp refreshable" in out
    assert "example.invalid" not in out


def test_setup_skill_redaction_covers_headers_and_environment(tmp_path: Path) -> None:
    """`claude mcp get` echoes header AND env values; the sed must blank both."""
    line = next(
        ln
        for ln in _block_containing(_setup_skill(), "claude mcp get kagura-memory |").splitlines()
        if ln.startswith("claude mcp get kagura-memory |")
    )
    secret = "sentinel-value-" + "0123456789"
    sample = tmp_path / "get.txt"
    sample.write_text(
        "kagura-memory:\n"
        "  Scope: Project config (shared via .mcp.json)\n"
        "  Type: stdio\n"
        "  Command: kagura-mcp\n"
        "  Args: --profile default\n"
        "  Headers:\n"
        f"    Authorization: Bearer {secret}\n"
        "  Environment:\n"
        f"    SOME_TOKEN={secret}\n",
        encoding="utf-8",
    )
    script = line.replace("claude mcp get kagura-memory", f"cat '{sample}'", 1)
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, check=True
    ).stdout
    assert secret not in out
    assert "    Authorization: <redacted>" in out
    assert "    SOME_TOKEN= <redacted>" in out
    assert "  Command: kagura-mcp" in out and "  Args: --profile default" in out


def test_setup_skill_cleans_up_after_verifying() -> None:
    text = _setup_skill()
    assert 'KAGURA_SETUP_DATA="$(mktemp -d)"' in text
    assert 'rm -rf "$KAGURA_SETUP_DATA"' in text


# Values the skill reads from configuration (.mcp.json is repository-controlled) must never be
# spliced into command text: they pass B0's check and are read back from its files (#1649).
PROJECT_PLACEHOLDERS = (
    "<server_url>",
    "<mcp_url>",
    "<context_id>",
    "<context uuid>",
    "<uuid>",
    "<profile>",
    "<url>",
    "<new url>",
    "<endpoint>",
    "<name>",
)
# Placeholders a shell command may still carry, with where each value comes from: the machine's own
# mktemp path, the user's own plugin install record, a file the user names - and the literal
# ``<redacted>`` B1's sed writes in place of a header value.
QUOTABLE_PLACEHOLDERS = ("<values dir>", "<plugin root>", "<the file the user named>", "<redacted>")
# Unquoted words chosen from a fixed set or typed by the user in their own terminal.
BARE_PLACEHOLDERS = ("<scope>", "<host>", "<your key>", "<marketplace>")
SHELL_WORDS = (
    "claude",
    "curl",
    "kagura",
    "python3",
    "printf",
    "uvx",
    "pip",
    "git",
    "rm",
    "ls",
    "mktemp",
    "|",
    "CLAUDE_PLUGIN_",
    "KAGURA_SETUP_DATA=",
)


def _shell_lines(text: str) -> list[str]:
    """Every line of shell the skill runs or hands the user: bash blocks, shell-looking lines of
    an unlabelled block, and inline code spans that start with a command."""
    lines: list[str] = []
    for label, body in re.findall(r"```([a-z]*)\n(.*?)```", text, flags=re.DOTALL):
        if label == "bash":
            lines.extend(body.splitlines())
        elif label == "":
            lines.extend(ln for ln in body.splitlines() if ln.strip().startswith(SHELL_WORDS))
    for span in re.findall(r"`([^`\n]+)`", text):
        if span.strip().startswith(SHELL_WORDS):
            lines.append(span)
    return lines


def _quoted_segments(line: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"""(["'])(.*?)\1""", line)]


def test_setup_skill_never_splices_a_project_value_into_a_command() -> None:
    lines = _shell_lines(_setup_skill())
    assert any("CLAUDE_PLUGIN_OPTION_SERVER_URL" in ln for ln in lines), "the scan saw B5b"
    assert any(ln.lstrip().startswith("curl") for ln in lines), "the scan saw B5a"
    for line in lines:
        for placeholder in re.findall(r"<[a-z][a-z_ ]*>", line):
            assert placeholder not in PROJECT_PLACEHOLDERS, (
                f"{placeholder} spliced into a command: {line.strip()!r}"
            )
            assert placeholder in QUOTABLE_PLACEHOLDERS + BARE_PLACEHOLDERS, (
                f"unknown placeholder {placeholder} in a command: {line.strip()!r}"
            )
        for segment in _quoted_segments(line):
            for placeholder in re.findall(r"<[a-z][a-z_ ]*>", segment):
                assert placeholder in QUOTABLE_PLACEHOLDERS, (
                    f"{placeholder} inside quotes in a command: {line.strip()!r}"
                )


def test_setup_skill_checks_values_before_the_first_command_that_uses_them() -> None:
    text = _setup_skill()
    check = _block_containing(text, "KAGURA_VALUE_CHECK")
    b0 = text.index("### B0. Check every value before a command uses it")
    first_use = text.index('"$(cat "<values dir>/')
    assert b0 < text.index(check) < first_use
    assert text.index("### B0.") < text.index("### B1.")
    # B1 routes the URL, profile and entry name through the check before its first command
    # that uses one (the shadowed-entry removal).
    b1 = text.split("### B1.", 1)[1].split("### B2.")[0]
    assert b1.index("run B0's check") < b1.index('claude mcp remove "$(cat "<values dir>/')
    # The stop rule: say it is malformed, never echo it into a command.
    b0_text = text.split("### B0.", 1)[1].split("### B1.")[0]
    assert "stop" in b0_text and "looks malformed" in b0_text
    assert "without printing the value, putting it into any command" in b0_text
    assert "file-writing tool" in b0_text, "values reach the files without a shell"
    # Every field a command reads has a pattern in the check.
    used = set(re.findall(r'"\$\(cat "<values dir>/([a-z_]+)"\)"', text))
    assert used == {"server_url", "context_id", "entry_name", "new_mcp_url", "profile"}
    for field in used:
        assert f'"{field}":' in check, f"no pattern for {field}"
    # The rule is stated for every harness, above Part A.
    rules = text.split("## Rules", 1)[1].split("\n---\n")[0]
    assert "never command text" in rules


def _run_value_check(tmp_path: Path, values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    block = _block_containing(_setup_skill(), "KAGURA_VALUE_CHECK")
    # Undo the list-item indentation of the fenced block.
    script = "\n".join(ln[3:] if ln.startswith("   ") else ln for ln in block.splitlines())
    values_dir = tmp_path / "values"
    values_dir.mkdir()
    for field, value in values.items():
        (values_dir / field).write_text(value, encoding="utf-8")
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    script = script.replace('"<values dir>"', f"'{values_dir}'")
    return subprocess.run(
        ["bash", "-c", script], cwd=workdir, capture_output=True, text=True, timeout=30
    )


HOSTILE_URLS = [
    "https://mcp.example.com/mcp'",
    "https://mcp.example.com/mcp'; touch x; '",
    "https://mcp.example.com/mcp$(touch x)",
    "https://mcp.example.com/mcp`touch x`",
    "https://mcp.example.com/mcp;touch x",
    "https://mcp.example.com/mcp|touch x",
    "https://mcp.example.com/mcp&",
    "https://mcp.example.com/mcp&touch=x",
    "https://mcp.example.com/mcp?guardrails=off&",
    "https://mcp.example.com/mcp?guardrails=off&&touch=x",
    "https://mcp.example.com/mcp\ntouch x",
    "https://mcp.example.com/mcp\n\n",
    "https://mcp.example.com/m cp",
    "https://mcp.example.com /mcp",
    'https://mcp.example.com/mcp"',
    "https://mcp.example.com/mcp#frag",
    "https://user@mcp.example.com/mcp",
    "ftp://mcp.example.com/mcp",
    "https:///mcp",
    "",
]


@pytest.mark.parametrize("value", HOSTILE_URLS)
@pytest.mark.parametrize("field", ["mcp_url", "server_url", "new_mcp_url"])
def test_setup_skill_value_check_rejects_shell_syntax(
    tmp_path: Path, field: str, value: str
) -> None:
    result = _run_value_check(tmp_path, {field: value})
    assert result.returncode == 1
    assert result.stdout.strip() == f"malformed: {field}"
    assert not (tmp_path / "cwd" / "x").exists()
    assert "touch" not in result.stdout + result.stderr, "the value is never echoed"


@pytest.mark.parametrize(
    "value",
    [
        "https://mcp.example.com/mcp",
        "https://mcp.example.com/mcp/w/00000000-0000-0000-0000-000000000000",
        "http://localhost:8080/mcp/w/00000000-0000-0000-0000-000000000000?guardrails=off",
        "http://[::1]:8080/mcp",
        "http://127.0.0.1:8080/mcp?profile=core&guardrails=off",
        "https://mcp.example.com/mcp\n",
    ],
)
def test_setup_skill_value_check_accepts_mcp_urls(tmp_path: Path, value: str) -> None:
    result = _run_value_check(
        tmp_path, {"mcp_url": value, "server_url": value, "new_mcp_url": value}
    )
    assert (result.returncode, result.stdout.strip()) == (0, "ok"), result.stderr


@pytest.mark.parametrize(
    ("field", "value", "ok"),
    [
        ("context_id", "00000000-0000-0000-0000-000000000000", True),
        ("context_id", "00000000-0000-0000-0000-000000000000;touch x", False),
        ("context_id", "not-a-uuid", False),
        ("profile", "default", True),
        ("profile", "work_2", True),
        ("profile", "default $(touch x)", False),
        ("profile", "default'", False),
        ("entry_name", "kagura-memory", True),
        ("entry_name", "kagura-memory;touch x", False),
    ],
)
def test_setup_skill_value_check_covers_ids_and_names(
    tmp_path: Path, field: str, value: str, ok: bool
) -> None:
    result = _run_value_check(tmp_path, {field: value})
    expected = (0, "ok") if ok else (1, f"malformed: {field}")
    assert (result.returncode, result.stdout.strip()) == expected
    assert not (tmp_path / "cwd" / "x").exists()


def test_setup_skill_check_mode_skips_context_checks_without_a_context() -> None:
    """list_contexts empty in check mode: A4 and the hook run need an id (#1649)."""
    text = _setup_skill()
    a2 = _setup_core().split("### A2.", 1)[1].split("### A3.")[0]
    check_mode = a2.split("In check mode", 1)[1]
    assert "verification not possible until a context exists" in check_mode
    assert "skip every check that" in check_mode and "needs a context id" in check_mode
    for skipped in ("A4", "`get_context_info`", "`load_guardrails`", "B5b"):
        assert skipped in check_mode
    assert "setup mode" in check_mode and "`create_context`" in check_mode, "the next step"
    assert "go on" not in check_mode
    a4 = _setup_core().split("### A4.", 1)[1].split("### A5.")[0]
    assert "with no context yet, skip it" in a4
    run_order = text.split("## Run order", 1)[1].split("## Rules")[0]
    assert "no context: A4 and B5b are skipped" in run_order


def test_setup_skill_is_listed_where_the_other_skills_are() -> None:
    guide = (REPO_ROOT / "claude-skills" / "guide.md").read_text(encoding="utf-8")
    assert "| `setup` |" in guide, "guide.md's skill table must list setup"
    clients = (REPO_ROOT / "docs" / "mcp-clients.md").read_text(encoding="utf-8")
    assert "| `/kagura-memory:setup` |" in clients


def test_docs_warn_that_changing_an_oauth_url_needs_reauthentication() -> None:
    """The Migration note users follow when they add ?guardrails=off (#1649)."""
    text = (REPO_ROOT / "docs" / "mcp-clients.md").read_text(encoding="utf-8")
    assert "requires re-authentication" in text
    assert "OAuth tokens per endpoint" in text
    assert "?guardrails=off" in text
    assert "the CLI profile cannot carry the query" in text


def test_troubleshooting_covers_the_claude_hooks_silence() -> None:
    text = (REPO_ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    section = text.split("## Claude Code — kagura-memory hooks never run")[1].split("\n## ")[0]
    assert "server_url must be the MCP endpoint" in section
    assert "claude mcp get kagura-memory" in section
    assert "/kagura-memory:setup --check" in section
    # The pasted sed must blank environment values as well as headers, like the skill's.
    assert "[A-Za-z0-9_-]+)([:=])" in section
    assert "kagura-mcp" in section, "the CLI-profile entry has no URL of its own"


def test_hook_names_the_endpoint_on_404_and_405() -> None:
    """The stage -> message wiring; the behaviour is in test_guardrail_fetch.py."""
    source = _source()
    assert 'ENDPOINT_STAGES = ("http 404", "http 405")' in source
    assert "server_url must be the MCP endpoint" in source
    assert "_fallback_cache(config, old, messages, stage)" in source
