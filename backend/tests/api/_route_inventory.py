"""The app's REST route inventory, shared by the route-table tests.

``test_capacity_lock_route_table.py`` (#1948) and
``test_slot_suspension_route_table.py`` (#2918) classify every route of the
app; both enumerate the routes here, without application startup.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable, Iterator
from typing import Any

from api.main import app

# The route objects the walker knows. Anything else fails ``_iter_routes``: a
# new kind of route must be enumerated on purpose, not skipped in silence.
_KNOWN_ROUTE_TYPES = ("APIRoute", "Route")

# Routes outside the REST tables: the MCP transport mounts (classified by the
# slot table only) and FastAPI's own documentation helpers.
MCP_MOUNTS = frozenset({"/mcp/w/{workspace_id}{path:path}", "/mcp/public/{public_id}{path:path}"})
_DOC_HELPERS = frozenset({"/openapi.json", "/docs/oauth2-redirect"})


def _iter_routes(*, include_mcp: bool = False) -> Iterator[tuple[str, str, Any, tuple[Any, ...]]]:
    """Yield ``(method, path, route, include-time dependencies)`` per REST route.

    FastAPI registers included routers lazily (``_IncludedRouter``); the original
    router and its include prefix are read back, recursively (auth includes the
    google/github routers). The documentation helpers are left out, and so are
    the MCP mounts unless ``include_mcp`` (the capacity table classifies the
    REST surface only). Any other route without methods, or of a kind not in
    ``_KNOWN_ROUTE_TYPES`` (subclasses included), raises.
    """

    def inner(routes, prefix: str, extra: tuple[Any, ...]):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                context = route.include_context
                yield from inner(
                    original.routes,
                    prefix + (getattr(context, "prefix", "") or ""),
                    extra + tuple(getattr(context, "dependencies", None) or ()),
                )
                continue
            kinds = {cls.__name__ for cls in type(route).__mro__}
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None)
            if not kinds & set(_KNOWN_ROUTE_TYPES) or not methods or path is None:
                raise AssertionError(f"unknown route object {type(route)!r} at {prefix}{path}")
            full = prefix + path
            if full in _DOC_HELPERS:
                continue
            if full == "/mcp" or full.startswith("/mcp/"):
                if full not in MCP_MOUNTS:
                    raise AssertionError(f"unpinned MCP mount: {full}")
                if not include_mcp:
                    continue
            for method in methods - {"HEAD", "OPTIONS"}:
                yield method, full, route, extra

    yield from inner(app.routes, "", ())


def _walk():
    """Yield ``(method, path, endpoint)`` for every REST route of the app."""
    for method, path, route, _extra in _iter_routes():
        yield method, path, route.endpoint


def _registered_routes() -> dict[tuple[str, str], str]:
    """``{(method, path): router module}`` for every REST route of the app."""
    return {
        (method, path): endpoint.__module__.removeprefix("api.routes.")
        for method, path, endpoint in _walk()
    }


def route_dependency_calls(route: Any, extra: tuple[Any, ...] = ()) -> list[Callable[..., Any]]:
    """Every callable in the route's FastAPI dependency tree, recursively.

    Reads ``route.dependant`` (router-level ``dependencies=`` included, and the
    ``Annotated[..., Depends(...)]`` aliases such as ``WorkspaceOwner``) plus the
    include-time dependencies, so a gate that runs as a dependency is seen.
    """
    from fastapi.dependencies.utils import get_parameterless_sub_dependant

    found: list[Callable[..., Any]] = []
    seen: set[int] = set()

    def visit(dependant: Any) -> None:
        for sub in getattr(dependant, "dependencies", None) or ():
            call = getattr(sub, "call", None)
            if call is not None and id(call) not in seen:
                seen.add(id(call))
                found.append(call)
            visit(sub)

    visit(getattr(route, "dependant", None))
    for depends in extra:
        visit(get_parameterless_sub_dependant(depends=depends, path=getattr(route, "path", "")))
    return found


def call_name(node: ast.Call) -> str:
    """The called name of an AST call: ``f(...)`` -> ``f``, ``a.b.f(...)`` -> ``f``."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def function_ast(func: Callable[..., Any]) -> ast.AST | None:
    """The parsed source of ``func`` (decorators unwrapped), or ``None`` without source."""
    try:
        source = inspect.getsource(inspect.unwrap(func))
    except (OSError, TypeError):
        return None
    return ast.parse(textwrap.dedent(source))
