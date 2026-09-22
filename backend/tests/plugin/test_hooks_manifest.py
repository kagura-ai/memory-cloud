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
from pathlib import Path

import pytest

from tests.plugin.conftest import (
    CLAUDE_HOOKS_JSON,
    CLAUDE_PLUGIN_JSON,
    HOOK_SCRIPT,
    REPO_ROOT,
    hook_commands,
)

GUARD_COMMAND = (
    'p=$(command -v python3) || exit 0; case "$p" in /*) ;; *) exit 0;; esac; '
    'case "$p" in "${CLAUDE_PROJECT_DIR:-/nonexistent}"/*|"$PWD"/*) exit 0;; esac; '
    'exec "$p" -I -S "${CLAUDE_PLUGIN_ROOT}/plugins/kagura-memory/hooks/kagura_guardrails.py" '
    "--client claude"
)
REFRESH_MATCHER = "^mcp__.*__(remember|update_memory|forget)$"

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
    "plugins/kagura-memory/skills/kagura-memory/SKILL.md",
    "docs/mcp-clients.md",
    "docs/mcp-tools.md",
    "docs/getting-started.md",
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
    assert "?guardrails=off" in cfg["server_url"]["description"]


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
            assert "${user_config" not in handler["command"]
            assert "args" not in handler


def test_timeouts_and_matchers() -> None:
    handlers = hook_commands()
    assert [h["timeout"] for h in handlers["SessionStart"]] == [5]
    assert handlers["SessionStart"][0]["matcher"] is None
    for event in ("PreToolUse", "PostToolUseFailure"):
        assert len(handlers[event]) == 1
        assert handlers[event][0]["timeout"] == 2
        assert handlers[event][0]["matcher"] == "*"
    post = handlers["PostToolUse"]
    assert len(post) == 2
    sync = [h for h in post if not h.get("async")]
    assert sync[0]["timeout"] == 2 and sync[0]["matcher"] == "*"
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


def test_codex_hooks_file_does_not_exist_yet() -> None:
    """PR-B (#1620) adds ``plugins/kagura-memory/hooks/hooks.json`` and replaces this test."""
    assert not (REPO_ROOT / "plugins" / "kagura-memory" / "hooks" / "hooks.json").exists()
    assert not (REPO_ROOT / "plugins" / "kagura-memory" / "hooks" / "_codex_adapter.py").exists()


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
    assert "sys.exit(0)" in ast.get_source_segment(_source(), third.body[0])


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


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_ruff_settings_for_the_hook_dir() -> None:
    proc = subprocess.run(
        ["ruff", "check", "--show-settings", str(HOOK_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "target_version = py39" in proc.stdout.replace("-", "_") or "py39" in proc.stdout
    assert "UP" in proc.stdout, "pyupgrade rules must reach the hook script"


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
    # Only github.com/kagura-ai is a permitted host under that organisation name.
    pattern = re.compile(r"://[a-z0-9.-]*" + "kagura" + "-ai" + r"\.com")
    assert not pattern.search(text), f"{relpath} names a hosted host"


def test_hook_script_exists_and_is_not_executable_dependent() -> None:
    """The sh guard execs python3 on the file, so the file needs no exec bit."""
    assert HOOK_SCRIPT.is_file()
    assert Path(HOOK_SCRIPT).suffix == ".py"
