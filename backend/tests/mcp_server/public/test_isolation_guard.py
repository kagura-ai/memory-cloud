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


# The package the guarded sources live in, for resolving relative imports.
PACKAGE_NAME = "mcp_server.public"


def _resolve(module: str | None, level: int) -> str:
    """The absolute module of an ``ImportFrom`` (``from .. import x`` included)."""
    if not level:
        return module or ""
    parts = PACKAGE_NAME.split(".")
    base = parts[: len(parts) - (level - 1)]
    return ".".join([*base, module] if module else base)


def _dotted(node: ast.AST) -> str | None:
    """``a.b.c`` for an attribute chain over a name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _is_banned_module(name: str) -> bool:
    return any(name == banned or name.startswith(banned + ".") for banned in BANNED_MODULES)


def _import_violations(source: str) -> list[str]:
    """Every banned import in ``source``, as ``module`` or ``module:name``.

    Catches ``import a.b``, ``from a.b import x``, ``from a import b`` (the
    module named as the imported name), relative forms, attribute chains such
    as ``services.mcp_notice_service.x``, and any import from
    ``mcp_server.tools`` beyond the allowed helpers.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_banned_module(alias.name) or alias.name.startswith("mcp_server.tools"):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(node.module, node.level)
            if _is_banned_module(module):
                found.append(module)
                continue
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                if _is_banned_module(full):
                    found.append(full)
            if module == "mcp_server.tools" or module.startswith("mcp_server.tools."):
                allowed = TOOLS_MODULE_ALLOWED_IMPORTS.get(module, frozenset())
                found.extend(
                    f"{module}:{alias.name}" for alias in node.names if alias.name not in allowed
                )
        elif isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if dotted and _is_banned_module(dotted):
                found.append(dotted)
    return found


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_imports_from_the_authenticated_package_are_the_allowed_helpers(path: Path) -> None:
    assert _import_violations(path.read_text()) == [], path.name


@pytest.mark.parametrize(
    "source",
    [
        "import services.mcp_notice_service",
        "import services.mcp_notice_text as text",
        "from services.mcp_notice_service import read_notice_candidates",
        "from services import mcp_notice_service",
        "from services import mcp_notice_text",
        "from services import usage_stats, mcp_notice_text",
        "from mcp_server.tools import _notices",
        "from mcp_server.tools._notices import fit_notices",
        "import mcp_server.tools._notices",
        "from mcp_server import auth",
        "from .. import auth",
        "from ..tools import _notices",
        "from ..tools._notices import place_notices",
        "import services\nservices.mcp_notice_service.read_notice_candidates",
    ],
)
def test_the_guard_catches_every_import_form(source: str) -> None:
    assert _import_violations(source), source


@pytest.mark.parametrize(
    "source",
    [
        "from mcp_server.tools._helpers import _dumps",
        "from . import scope",
        "from .scope import public_rows",
        "from services import public_publication_service",
        "import services.public_publication_service",
        "from utils.logger import get_logger",
    ],
)
def test_the_guard_lets_allowed_imports_through(source: str) -> None:
    assert _import_violations(source) == [], source


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
