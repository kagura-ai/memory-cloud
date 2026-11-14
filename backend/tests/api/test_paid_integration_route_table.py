"""Every route and tool decides whether it consumes paid-integration rights (#2801).

``docs/ops/paid-integration-entitlements.md``, "Mandatory consumer boundary":
installation, token and sync handlers must commit through
``paid_integration_write`` (read-only admissions through
``require_paid_integration``), and ``inspect_paid_integration`` is for display
only. This test makes that boundary a table change, in three parts:

* ``PAID_INTEGRATION_ROUTERS``: every router module that serves a REST route in
  ``api.main.app`` (found as in ``test_capacity_lock_route_table.py``) is
  ``NONE`` (grants no paid-integration authority) or ``Guarded(services=...)``,
  naming the service module(s) whose code calls the guard. A new router module
  without a row, or a stale row, fails.
* ``PAID_INTEGRATION_MCP_TOOLS``: the same decision for every MCP tool (the
  REST discovery skips ``/mcp``).
* ``PAID_INTEGRATION_SYMBOLS``: an AST scan of ``backend/src`` lists every
  module that touches the service, its models, the three entry functions or
  the ``workspace_integration_*`` tables; each such module has a role, and a
  role allows only its kinds of use. Background tasks and other non-route code
  are covered by this scan only.

The checks are pure functions over names and sources, so the stub tests below
prove each failure without changing the app.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.api.test_capacity_lock_route_table import _registered_routes

DOC = 'docs/ops/paid-integration-entitlements.md, "Mandatory consumer boundary"'
SRC = Path(__file__).resolve().parents[2] / "src"

SERVICE_MODULE = "services.integration_entitlements"
MODELS_MODULE = "models.integration_entitlement"
WRITE = "paid_integration_write"
REQUIRE = "require_paid_integration"
INSPECT = "inspect_paid_integration"
GUARD_CALLS = frozenset({WRITE, REQUIRE})
ENTRY_NAMES = frozenset({WRITE, REQUIRE, INSPECT})
TABLE_PREFIX = "workspace_integration_"
CLASS_PREFIX = "WorkspaceIntegration"

NONE = "none"


@dataclass(frozen=True)
class Guarded:
    """A router whose paid writes go through the guard in ``services``."""

    services: tuple[str, ...]


# --- Router decisions --------------------------------------------------------
# Every served router module, explicitly. Never derive this from the capacity
# table: each new router needs its own paid-integration decision.
PAID_INTEGRATION_ROUTERS: dict[str, str | Guarded] = {
    "admin": NONE,
    "admin_mcp_notices": NONE,
    "admin_memory_health": NONE,
    "admin_neural": NONE,
    "admin_operations": NONE,
    "admin_plans": NONE,
    "admin_referrals": NONE,
    "admin_signup_gate": NONE,
    "admin_sleep": NONE,
    "agent_state": NONE,
    "agents": NONE,
    "analyses": NONE,
    "announcement_emails": NONE,
    "api.main": NONE,  # app-level routes
    "api_keys": NONE,
    "attachments": NONE,
    "auth": NONE,
    "beta_invites": NONE,
    "billing_handoff": NONE,
    "bm25_drift": NONE,
    "config": NONE,
    "connectors_slack": NONE,
    "context_search_config": NONE,
    "contexts": NONE,
    "cost_aggregation": NONE,
    "email_signup": NONE,
    "external_keys": NONE,
    "fastapi.applications": NONE,  # /docs, /redoc
    "feedback": NONE,
    "files": NONE,
    "graph": NONE,
    "internal_billing": NONE,
    "internal_integrations": NONE,  # publisher, consumes no rights
    "invitations": NONE,
    "management_grants": NONE,
    "mcp": NONE,
    "mcp_notices": NONE,
    "me_account": NONE,
    "me_oauth": NONE,
    "me_oauth_grants": NONE,
    "me_terms": NONE,
    "me_workspaces": NONE,
    "member_credentials": NONE,
    "memory": NONE,
    "memory_health": NONE,
    "mfa_settings": NONE,
    "neural_config": NONE,
    "oauth": NONE,
    "observability": NONE,
    "observation_credentials": NONE,
    "oidc": NONE,
    "operation_history": NONE,
    "passkeys": NONE,
    "password": NONE,
    "provider_link_confirm": NONE,
    "public_mcp_endpoints": NONE,
    "public_mcp_usage": NONE,
    "public_search": NONE,
    "referrals": NONE,
    "resource_indexer": NONE,
    "resource_ingest": NONE,
    "resource_management": NONE,
    "resource_schema": NONE,
    "resource_tokens": NONE,
    "resources": NONE,
    "secrets": NONE,
    "share_keys": NONE,
    "sleep_reports": NONE,
    "system": NONE,
    "system_admins": NONE,
    "users": NONE,
    "well_known": NONE,
    "worker_apps": NONE,
    "workers": NONE,
    "workspace": NONE,
    "workspace_connectors": NONE,
    "workspace_plan": NONE,
    "workspaces": NONE,
}

# --- MCP tool decisions ------------------------------------------------------
PAID_INTEGRATION_MCP_TOOLS: dict[str, str] = {
    "analyze_context": NONE,
    "bind_agent_context": NONE,
    "bootstrap": NONE,
    "changes_since": NONE,
    "complete_file_upload": NONE,
    "create_context": NONE,
    "create_edge": NONE,
    "delete_agent": NONE,
    "delete_context": NONE,
    "delete_edge": NONE,
    "delete_file": NONE,
    "describe_binding": NONE,
    "describe_tools": NONE,
    "explore": NONE,
    "feedback": NONE,
    "forget": NONE,
    "get_active_analysis": NONE,
    "get_agent": NONE,
    "get_agent_bootstrap": NONE,
    "get_analysis": NONE,
    "get_cluster": NONE,
    "get_context_info": NONE,
    "get_file_download_url": NONE,
    "get_resource_impact": NONE,
    "get_resource_schema": NONE,
    "get_sleep_history": NONE,
    "get_sleep_report": NONE,
    "get_state": NONE,
    "get_usage": NONE,
    "guide": NONE,
    "ingest_events": NONE,
    "init_file_upload": NONE,
    "list": NONE,
    "list_agent_bindings": NONE,
    "list_agents": NONE,
    "list_analyses": NONE,
    "list_contexts": NONE,
    "list_edges": NONE,
    "list_files": NONE,
    "list_my_bindings": NONE,
    "list_resource_tokens": NONE,
    "list_revisions": NONE,
    "list_tags": NONE,
    "load_guardrails": NONE,
    "load_pinned": NONE,
    "merge_contexts": NONE,
    "recall": NONE,
    "recall_nearby": NONE,
    "recall_series": NONE,
    "recall_upcoming": NONE,
    "record_measurement": NONE,
    "reference": NONE,
    "register_agent": NONE,
    "remember": NONE,
    "remember_batch": NONE,
    "rollback_session": NONE,
    "rollback_sleep_run": NONE,
    "secret_get": NONE,
    "secret_list": NONE,
    "secret_put": NONE,
    "secret_register_pubkey": NONE,
    "secret_revoke_grant": NONE,
    "set_state": NONE,
    "setup_connector": NONE,
    "setup_resource": NONE,
    "unbind_agent_context": NONE,
    "update_agent": NONE,
    "update_agent_binding": NONE,
    "update_context": NONE,
    "update_edge": NONE,
    "update_memory": NONE,
    "update_search_config": NONE,
}

# --- Symbol roles ------------------------------------------------------------
SERVICE = "service"  # defines the entry functions; may use everything
MODELS = "models"  # defines the mapped classes (and their table names)
PUBLISHER = "publisher"  # imports only the apply_/read_ publisher functions
REGISTRY = "registry"  # names the models module in a string
DATA_BOUNDARY = "data_boundary"  # names the tables in strings
DISPLAY_ONLY = "display_only"  # inspect_paid_integration, for display only
GUARDED = "guarded"  # paid_integration_write / require_paid_integration + models

PAID_INTEGRATION_SYMBOLS: dict[str, str] = {
    "services.integration_entitlements": SERVICE,
    "models.integration_entitlement": MODELS,
    "api.routes.internal_integrations": PUBLISHER,
    "models": REGISTRY,  # importlib.import_module("models.integration_entitlement")
    "models.data_boundary": DATA_BOUNDARY,
}

# Kinds of use the scan reports.
SERVICE_MODULE_IMPORT = "service module import"
SERVICE_INTERNAL = "service name other than apply_/read_ or the entries"
PUBLISH_API = "apply_/read_ publisher import"
MODELS_MODULE_IMPORT = "models module import"
MODEL_CLASS = "mapped class reference"
MODEL_CLASS_DEF = "mapped class definition"
STRING = "module path or table name string"

ALL_KINDS = frozenset(
    {
        SERVICE_MODULE_IMPORT,
        SERVICE_INTERNAL,
        PUBLISH_API,
        MODELS_MODULE_IMPORT,
        MODEL_CLASS,
        MODEL_CLASS_DEF,
        STRING,
        *ENTRY_NAMES,
    }
)
ROLE_KINDS: dict[str, frozenset[str]] = {
    SERVICE: ALL_KINDS,
    MODELS: frozenset({MODEL_CLASS_DEF, STRING}),
    PUBLISHER: frozenset({PUBLISH_API}),
    REGISTRY: frozenset({STRING}),
    DATA_BOUNDARY: frozenset({STRING}),
    DISPLAY_ONLY: frozenset({INSPECT}),
    GUARDED: frozenset({WRITE, REQUIRE, SERVICE_MODULE_IMPORT, MODELS_MODULE_IMPORT, MODEL_CLASS}),
}


def _see(table: str) -> str:
    return f" Decide it in {table}; see {DOC}."


# --- Pure checkers -----------------------------------------------------------


def router_problems(modules: Iterable[str], table: Mapping[str, str | Guarded]) -> list[str]:
    """Unclassified served router modules and stale rows."""
    served = set(modules)
    see = _see("PAID_INTEGRATION_ROUTERS")
    problems = [
        f"PAID_INTEGRATION_ROUTERS: router module {name!r} serves a route but has no "
        f"paid-integration decision (NONE or Guarded).{see}"
        for name in sorted(served - set(table))
    ]
    problems += [
        f"PAID_INTEGRATION_ROUTERS: stale row {name!r}, the module serves no route.{see}"
        for name in sorted(set(table) - served)
    ]
    problems += [
        f"PAID_INTEGRATION_ROUTERS: {name!r} has the decision {decision!r}; use NONE or "
        f"Guarded(services=(...)).{see}"
        for name, decision in sorted(table.items(), key=lambda item: item[0])
        if decision != NONE and not (isinstance(decision, Guarded) and decision.services)
    ]
    return problems


def _calls_guard(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in GUARD_CALLS:
                return True
    return False


def _imports_module(source: str, module: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
            alias.name == module or alias.name.startswith(module + ".") for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == module or any(
                f"{node.module}.{alias.name}" == module for alias in node.names
            ):
                return True
    return False


def guarded_router_problems(
    table: Mapping[str, str | Guarded],
    sources: Mapping[str, str],
    symbols: Mapping[str, str],
) -> list[str]:
    """A ``Guarded`` router imports its service, which calls the guard and is ``guarded``."""
    see = _see("PAID_INTEGRATION_ROUTERS")
    problems: list[str] = []
    for name, decision in sorted(table.items(), key=lambda item: item[0]):
        if not isinstance(decision, Guarded):
            continue
        module = name if "." in name else "api.routes." + name
        router_source = sources.get(module)
        if router_source is None:
            problems.append(f"PAID_INTEGRATION_ROUTERS: no source for {module!r}.{see}")
            continue
        for service in decision.services:
            service_source = sources.get(service)
            if service_source is None:
                problems.append(
                    f"PAID_INTEGRATION_ROUTERS: {name!r} names {service!r}, which has no "
                    f"source.{see}"
                )
                continue
            if not _calls_guard(service_source):
                problems.append(
                    f"PAID_INTEGRATION_ROUTERS: {name!r} names {service!r}, which never calls "
                    f"{WRITE} or {REQUIRE} (an import is not enough).{see}"
                )
            if service != module and not _imports_module(router_source, service):
                problems.append(
                    f"PAID_INTEGRATION_ROUTERS: {name!r} does not import its guarded service "
                    f"{service!r}.{see}"
                )
            if symbols.get(service) != GUARDED:
                problems.append(
                    f"PAID_INTEGRATION_ROUTERS: {name!r} names {service!r}, which is not "
                    f"{GUARDED!r} in PAID_INTEGRATION_SYMBOLS.{see}"
                )
    return problems


def _mapped_class_names() -> frozenset[str]:
    import models.integration_entitlement as models_module

    return frozenset(
        name
        for name, value in vars(models_module).items()
        if isinstance(value, type)
        and value.__module__ == models_module.__name__
        and hasattr(value, "__mapper__")
    )


def symbol_kinds(source: str, mapped: frozenset[str]) -> set[str]:
    """The kinds of paid-integration use in one module's source."""

    def is_mapped(name: str) -> bool:
        return name in mapped or name.startswith(CLASS_PREFIX)

    def name_kind(name: str) -> str | None:
        if name in ENTRY_NAMES:
            return name
        if is_mapped(name):
            return MODEL_CLASS
        return None

    kinds: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == SERVICE_MODULE:
                    kinds.add(SERVICE_MODULE_IMPORT)
                elif alias.name == MODELS_MODULE:
                    kinds.add(MODELS_MODULE_IMPORT)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                full = f"{module}.{alias.name}"
                if full == SERVICE_MODULE:
                    kinds.add(SERVICE_MODULE_IMPORT)
                elif full == MODELS_MODULE:
                    kinds.add(MODELS_MODULE_IMPORT)
                elif (kind := name_kind(alias.name)) is not None:
                    kinds.add(kind)
                elif module == SERVICE_MODULE:
                    kinds.add(
                        PUBLISH_API
                        if alias.name.startswith(("apply_", "read_"))
                        else SERVICE_INTERNAL
                    )
                elif module == MODELS_MODULE:
                    kinds.add(MODELS_MODULE_IMPORT)
        elif isinstance(node, ast.Name):
            if (kind := name_kind(node.id)) is not None:
                kinds.add(kind)
        elif isinstance(node, ast.Attribute):
            if (kind := name_kind(node.attr)) is not None:
                kinds.add(kind)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in ENTRY_NAMES:
                kinds.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if is_mapped(node.name):
                kinds.add(MODEL_CLASS_DEF)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in (SERVICE_MODULE, MODELS_MODULE) or TABLE_PREFIX in node.value:
                kinds.add(STRING)
    return kinds


def symbol_problems(
    sources: Mapping[str, str],
    table: Mapping[str, str],
    mapped: frozenset[str] | None = None,
) -> list[str]:
    """Unlisted modules, stale rows and uses a module's role does not allow."""
    mapped = _mapped_class_names() if mapped is None else mapped
    see = _see("PAID_INTEGRATION_SYMBOLS")
    found = {
        module: kinds
        for module, source in sources.items()
        if (kinds := symbol_kinds(source, mapped))
    }
    problems = [
        f"PAID_INTEGRATION_SYMBOLS: module {module!r} uses the paid-integration surface "
        f"({', '.join(sorted(found[module]))}) but has no row.{see}"
        for module in sorted(set(found) - set(table))
    ]
    problems += [
        f"PAID_INTEGRATION_SYMBOLS: stale row {module!r}, the scan finds no use.{see}"
        for module in sorted(set(table) - set(found))
    ]
    for module in sorted(set(found) & set(table)):
        role = table[module]
        allowed = ROLE_KINDS.get(role)
        if allowed is None:
            problems.append(f"PAID_INTEGRATION_SYMBOLS: {module!r} has unknown role {role!r}.{see}")
            continue
        refused = found[module] - allowed
        if INSPECT in refused:
            problems.append(
                f"PAID_INTEGRATION_SYMBOLS: {module!r} ({role}) references {INSPECT}; outside "
                f"the service it must be {DISPLAY_ONLY!r}, and a write never relies on it.{see}"
            )
            refused = refused - {INSPECT}
        if refused:
            problems.append(
                f"PAID_INTEGRATION_SYMBOLS: {module!r} ({role}) uses "
                f"{', '.join(sorted(refused))}, which its role does not allow.{see}"
            )
    return problems


def mcp_problems(tools: Iterable[str], table: Mapping[str, str]) -> list[str]:
    """Unclassified and stale MCP tools; only NONE exists for tools today."""
    registered = set(tools)
    see = _see("PAID_INTEGRATION_MCP_TOOLS")
    problems = [
        f"PAID_INTEGRATION_MCP_TOOLS: tool {name!r} has no paid-integration decision.{see}"
        for name in sorted(registered - set(table))
    ]
    problems += [
        f"PAID_INTEGRATION_MCP_TOOLS: stale row {name!r}, no such tool.{see}"
        for name in sorted(set(table) - registered)
    ]
    problems += [
        f"PAID_INTEGRATION_MCP_TOOLS: {name!r} is {decision!r}; a tool that consumes paid "
        f"rights needs a guarded check added here first.{see}"
        for name, decision in sorted(table.items())
        if decision != NONE
    ]
    return problems


def src_sources() -> dict[str, str]:
    """``{dotted module: source}`` for every ``.py`` under ``backend/src``."""
    sources: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            sources[".".join(parts)] = path.read_text(encoding="utf-8")
    return sources


# --- The real app ------------------------------------------------------------


def test_every_served_router_module_has_a_paid_integration_decision():
    assert router_problems(set(_registered_routes().values()), PAID_INTEGRATION_ROUTERS) == []


def test_guarded_routers_call_the_guard_through_their_service():
    problems = guarded_router_problems(
        PAID_INTEGRATION_ROUTERS, src_sources(), PAID_INTEGRATION_SYMBOLS
    )
    assert problems == []


def test_every_mcp_tool_has_a_paid_integration_decision():
    from mcp_server.tools import _build_registry

    assert mcp_problems(set(_build_registry()), PAID_INTEGRATION_MCP_TOOLS) == []


def test_the_symbol_scan_matches_the_table():
    assert symbol_problems(src_sources(), PAID_INTEGRATION_SYMBOLS) == []


def test_the_mapped_classes_are_derived_from_the_models_module():
    assert _mapped_class_names() == {
        "WorkspaceIntegrationState",
        "WorkspaceIntegrationLicense",
        "WorkspaceIntegrationEvent",
    }


def test_the_wire_schemas_module_is_not_a_consumer():
    # Pydantic wire types: a module path that merely starts like the models
    # module is not an import of it.
    source = "from models.integration_entitlement_schemas import IntegrationEntitlementView\n"
    assert symbol_kinds(source, _mapped_class_names()) == set()


# --- Stubs: each failure names its table and the doc section ----------------


def _assert_problem(problems: list[str], table: str, *needles: str) -> None:
    assert problems, "expected a problem"
    for problem in problems:
        assert table in problem and DOC in problem, problem
    assert any(all(needle in problem for needle in needles) for problem in problems), problems


def test_stub_unclassified_router_module_fails():
    served = set(PAID_INTEGRATION_ROUTERS) | {"integration_installations"}
    _assert_problem(
        router_problems(served, PAID_INTEGRATION_ROUTERS),
        "PAID_INTEGRATION_ROUTERS",
        "'integration_installations'",
        "no paid-integration decision",
    )


def test_stub_stale_router_row_fails():
    served = set(PAID_INTEGRATION_ROUTERS) - {"internal_integrations"}
    _assert_problem(
        router_problems(served, PAID_INTEGRATION_ROUTERS),
        "PAID_INTEGRATION_ROUTERS",
        "stale row 'internal_integrations'",
    )


def test_stub_unknown_router_decision_fails():
    table = {**PAID_INTEGRATION_ROUTERS, "workers": "guarded"}
    _assert_problem(
        router_problems(set(PAID_INTEGRATION_ROUTERS), table),
        "PAID_INTEGRATION_ROUTERS",
        "'workers'",
        "decision",
    )


def test_stub_unclassified_mcp_tool_fails():
    from mcp_server.tools import _build_registry

    tools = set(_build_registry()) | {"install_integration"}
    _assert_problem(
        mcp_problems(tools, PAID_INTEGRATION_MCP_TOOLS),
        "PAID_INTEGRATION_MCP_TOOLS",
        "'install_integration'",
    )


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (f"from {SERVICE_MODULE} import {WRITE}\n", WRITE),
        (f"from {SERVICE_MODULE} import {REQUIRE} as admit\n", REQUIRE),
        ("from services import integration_entitlements\n", SERVICE_MODULE_IMPORT),
        (f"from {MODELS_MODULE} import WorkspaceIntegrationState\n", MODEL_CLASS),
        ("from models import WorkspaceIntegrationLicense\n", MODEL_CLASS),
        (f"import {MODELS_MODULE}\n", MODELS_MODULE_IMPORT),
        ("TABLE = 'workspace_integration_states'\n", STRING),
    ],
    ids=["write", "require-alias", "service-module", "model", "model-reexport", "models", "table"],
)
def test_stub_unlisted_module_fails(source, kind):
    sources = {"services.integration_installations": source}
    problems = symbol_problems(sources, {})
    _assert_problem(
        problems, "PAID_INTEGRATION_SYMBOLS", "'services.integration_installations'", kind
    )


@pytest.mark.parametrize("role", [PUBLISHER, GUARDED])
def test_stub_publisher_or_guarded_module_calling_inspect_fails(role):
    source = (
        f"from {SERVICE_MODULE} import {INSPECT}, {WRITE}\n"
        "async def handler(db, workspace_id):\n"
        f"    if (await {INSPECT}(db, workspace_id, 'sync')).allowed:\n"
        "        pass\n"
    )
    problems = symbol_problems(
        {"api.routes.integration_sync": source}, {"api.routes.integration_sync": role}
    )
    _assert_problem(problems, "PAID_INTEGRATION_SYMBOLS", "'api.routes.integration_sync'", INSPECT)


def test_stub_display_only_module_may_inspect_but_not_write():
    display = f"from {SERVICE_MODULE} import {INSPECT}\n"
    assert symbol_problems({"api.routes.view": display}, {"api.routes.view": DISPLAY_ONLY}) == []
    writes = f"from {SERVICE_MODULE} import {INSPECT}, {WRITE}\n"
    _assert_problem(
        symbol_problems({"api.routes.view": writes}, {"api.routes.view": DISPLAY_ONLY}),
        "PAID_INTEGRATION_SYMBOLS",
        WRITE,
    )


def test_stub_publisher_importing_the_guard_or_models_fails():
    source = (
        f"from {SERVICE_MODULE} import apply_integration_entitlements, {WRITE}\n"
        f"from {MODELS_MODULE} import WorkspaceIntegrationState\n"
    )
    module = "api.routes.internal_integrations"
    _assert_problem(
        symbol_problems({module: source}, {module: PUBLISHER}),
        "PAID_INTEGRATION_SYMBOLS",
        WRITE,
        MODEL_CLASS,
    )


ROUTER_STUB = (
    "from fastapi import APIRouter\n"
    "from services.integration_installations import create_installation\n"
    "router = APIRouter()\n"
)
GUARDED_SERVICE_STUB = (
    f"from {SERVICE_MODULE} import {WRITE}\n"
    "async def create_installation(db, workspace_id):\n"
    f"    async with {WRITE}(db, workspace_id, 'sync'):\n"
    "        pass\n"
)


def test_stub_guarded_router_passes_when_its_service_calls_the_guard():
    table = {"integration_installations": Guarded(("services.integration_installations",))}
    sources = {
        "api.routes.integration_installations": ROUTER_STUB,
        "services.integration_installations": GUARDED_SERVICE_STUB,
    }
    symbols = {"services.integration_installations": GUARDED}
    assert guarded_router_problems(table, sources, symbols) == []
    assert symbol_problems(sources, symbols) == []


@pytest.mark.parametrize("defect", ["import_only", "router_skips_service", "not_guarded_role"])
def test_stub_guarded_router_fails_without_a_guard_call(defect):
    table = {"integration_installations": Guarded(("services.integration_installations",))}
    service = GUARDED_SERVICE_STUB
    router = ROUTER_STUB
    symbols = {"services.integration_installations": GUARDED}
    if defect == "import_only":
        service = (
            f"from {SERVICE_MODULE} import {WRITE}\nasync def create_installation(db):\n    pass\n"
        )
    elif defect == "router_skips_service":
        router = "from fastapi import APIRouter\nrouter = APIRouter()\n"
    else:
        symbols = {"services.integration_installations": PUBLISHER}
    sources = {
        "api.routes.integration_installations": router,
        "services.integration_installations": service,
    }
    needle = {
        "import_only": "never calls",
        "router_skips_service": "does not import",
        "not_guarded_role": "is not 'guarded'",
    }[defect]
    _assert_problem(
        guarded_router_problems(table, sources, symbols), "PAID_INTEGRATION_ROUTERS", needle
    )
