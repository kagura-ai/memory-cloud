"""The public MCP package stays away from the authenticated surface (#2675).

Structural guard over the source of ``mcp_server/public/``: no module there
imports or names the authenticated dispatcher, its registry, a registry
handler, ``MemoryService``, the usage / last-used writers, the authentication
entry point, or the ``WWW-Authenticate`` challenge header. A public tool is a
handler of its own over ``scope.public_rows``; reusing an authenticated one
(with the owner's identity, say) is the leak this test exists to stop.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "mcp_server" / "public"

BANNED_NAMES = frozenset(
    {
        "authenticate_mcp_request",
        "execute_tool_call",
        "_TOOL_REGISTRY",
        "MemoryService",
        "_log_tool_usage",
        "_touch_context_last_used",
        "log_usage",
        "GraphService",
        "ActivationSpreader",
    }
)
# Modules whose import alone would put an authenticated path in reach.
BANNED_MODULES = frozenset(
    {
        "mcp_server.auth",
        "services.memory_service",
        "services.usage_stats",
        "services.graph_service",
        # #2762: service notices are a bootstrap-only lane of the
        # authenticated surface; the anonymous endpoint never carries them.
        "services.mcp_notice_service",
        "services.mcp_notice_text",
        "mcp_server.tools._notices",
    }
)
# Handlers of the authenticated tools live in these modules; only the named
# helpers may be imported from them.
TOOLS_MODULE_ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    "mcp_server.tools._helpers": frozenset(
        {"_error_response", "_dumps", "execute_with_timeout", "ToolErrorContent"}
    ),
    "mcp_server.tools._errors": frozenset({"_tool_exception_response"}),
    "mcp_server.tools._annotations": frozenset({"_read"}),
    "mcp_server.tools._profiles": frozenset({"ToolProfileError"}),
    "mcp_server.tools.memory": frozenset({"_reference_page", "_ReferenceOffsetError"}),
}


def _sources() -> list[Path]:
    files = sorted(PACKAGE.glob("*.py"))
    assert files, PACKAGE
    return files


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_banned_name_or_header(path: Path) -> None:
    text = path.read_text()
    assert "WWW-Authenticate" not in text
    assert "www-authenticate" not in text.lower()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in BANNED_NAMES, (path.name, node.id)
        elif isinstance(node, ast.Attribute):
            assert node.attr not in BANNED_NAMES, (path.name, node.attr)
            assert not node.attr.startswith("handle_"), (path.name, node.attr)
        elif isinstance(node, ast.alias):
            assert node.name not in BANNED_NAMES, (path.name, node.name)
            assert not node.name.startswith("handle_"), (path.name, node.name)


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_imports_from_the_authenticated_package_are_the_allowed_helpers(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in BANNED_MODULES, (path.name, alias.name)
                assert not alias.name.startswith("mcp_server.tools"), (path.name, alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module not in BANNED_MODULES, (path.name, module)
            if module == "mcp_server.tools" or module.startswith("mcp_server.tools."):
                allowed = TOOLS_MODULE_ALLOWED_IMPORTS.get(module)
                assert allowed is not None, (path.name, module)
                names = {alias.name for alias in node.names}
                assert names <= allowed, (path.name, module, names - allowed)


def test_the_guard_reads_every_module_of_the_package() -> None:
    names = {p.name for p in _sources()}
    assert {
        "__init__.py",
        "publication.py",
        "scope.py",
        "limits.py",
        "telemetry.py",
        "definitions.py",
        "tools.py",
        "dispatch.py",
        "transport.py",
    } <= names
