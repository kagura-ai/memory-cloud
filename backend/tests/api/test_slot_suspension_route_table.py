"""Every REST route is declared against the workspace slot gate (#2918).

A suspended workspace (its owner owns more workspaces than their allowance)
serves nobody: every route that reads or writes a workspace's data goes
through a gated primitive (``services.workspace_slot_gate``, the permission
primitives that call it). This table lists only the EXCEPTIONS; every other
route is GATED by default:

* ``OWNER_MANAGEMENT`` — the owner's way out (export, delete, the billing
  handoff, revoking credentials): the owner passes, members are refused.
* ``ACCOUNT`` — the caller's own account, sign-in, deployment metadata: no
  workspace's data.
* ``INTERNAL`` — service-to-service (``/internal``, the worker service token):
  billing and sync control must keep working on a suspended workspace.
* ``SYSTEM_ADMIN`` — the operator surface.
* ``PUBLIC_OUTSIDER`` — public readers and share keys: refused with the
  redacted form (``outsider=True``).

Router-module defaults exist only for ACCOUNT, INTERNAL and SYSTEM_ADMIN.
Every GATED route must name a gated primitive in its handler or anywhere in
its FastAPI dependency tree, or sit in ``SLOT_SERVICE_GATED`` with the function
that gates it. Every ``SlotPolicy.OWNER_MANAGEMENT`` and every ``slot=`` that is
not ``SlotPolicy.BLOCKED`` under ``backend/src`` is pinned to an exact set of
call sites. A new route or a new opt-out therefore fails here until somebody
decides.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

from tests.api._route_inventory import (
    _iter_routes,
    call_name,
    function_ast,
    route_dependency_calls,
)

OWNER_MANAGEMENT = "owner_management"
ACCOUNT = "account"
INTERNAL = "internal"
SYSTEM_ADMIN = "system_admin"
PUBLIC_OUTSIDER = "public_outsider"

_DEFAULTABLE = {ACCOUNT, INTERNAL, SYSTEM_ADMIN}

# Router modules whose whole surface is one exception category.
ROUTER_DEFAULTS: dict[str, str] = {
    # ---- the operator surface ---------------------------------------------
    "admin": SYSTEM_ADMIN,
    "admin_audit_logs": SYSTEM_ADMIN,
    "admin_environment": SYSTEM_ADMIN,
    "admin_erasure_requests": SYSTEM_ADMIN,
    "admin_mcp_notices": SYSTEM_ADMIN,
    "admin_memory_health": SYSTEM_ADMIN,
    "admin_neural": SYSTEM_ADMIN,
    "admin_operations": SYSTEM_ADMIN,
    "admin_plans": SYSTEM_ADMIN,
    "admin_referrals": SYSTEM_ADMIN,
    "admin_signup_gate": SYSTEM_ADMIN,
    "admin_sleep": SYSTEM_ADMIN,
    "bm25_drift": SYSTEM_ADMIN,
    "config": SYSTEM_ADMIN,  # deployment configuration
    "neural_config": SYSTEM_ADMIN,
    "system_admins": SYSTEM_ADMIN,
    "worker_apps": SYSTEM_ADMIN,
    # ---- service to service -----------------------------------------------
    "internal_billing": INTERNAL,
    "internal_integrations": INTERNAL,
    "internal_public_mcp_domains": INTERNAL,
    # validate / activate / observe / reconnect reach ``sync_authority`` with
    # no principal, which skips the slot gate (``slot=None``).
    "internal_sync_installations": INTERNAL,
    # ---- the caller's own account, sign-in, deployment metadata -------------
    "announcement_emails": ACCOUNT,
    # Global and workspace-scoped key management; every use of a key is
    # refused per call, and revoking is never blocked.
    "api_keys": ACCOUNT,
    "auth": ACCOUNT,
    "beta_invites": ACCOUNT,
    "email_signup": ACCOUNT,
    "fastapi.applications": ACCOUNT,
    "mcp": ACCOUNT,  # tool catalogue and server status
    "mcp_notices": ACCOUNT,
    "me_account": ACCOUNT,
    "me_oauth": ACCOUNT,
    "me_oauth_grants": ACCOUNT,  # grant settings; the context is resolved per call
    "me_terms": ACCOUNT,
    "memory_health": ACCOUNT,  # the caller's own health signals, counts only
    "mfa_settings": ACCOUNT,
    "oauth": ACCOUNT,  # clients and the authorization flow; tokens are refused per call
    "oidc": ACCOUNT,
    "passkeys": ACCOUNT,
    "password": ACCOUNT,
    "provider_link_confirm": ACCOUNT,
    "referrals": ACCOUNT,
    "system": ACCOUNT,
    "users": ACCOUNT,
    "well_known": ACCOUNT,
}

# Path prefixes of a whole account surface, allowed before its router is
# mounted (the account API of #2918 lands in another lane).
PREFIX_DEFAULTS: dict[str, str] = {
    "/api/v1/me/workspace-slots": ACCOUNT,
}

# One row per exception route of a router without a default.
EXCEPTIONS: dict[tuple[str, str], str] = {
    # ---- owner management (the complete list, design 12.4) ----------------
    ("DELETE", "/api/v1/workspaces/{workspace_id}"): OWNER_MANAGEMENT,
    ("POST", "/api/v1/billing/handoff"): OWNER_MANAGEMENT,
    ("GET", "/api/v1/contexts/{context_id}/export"): OWNER_MANAGEMENT,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-key",
    ): OWNER_MANAGEMENT,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys/{key_id}",
    ): OWNER_MANAGEMENT,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/oauth",
    ): OWNER_MANAGEMENT,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/sync-authorizations/{authorization_id}",
    ): OWNER_MANAGEMENT,
    # ---- account ------------------------------------------------------------
    ("GET", "/"): ACCOUNT,
    ("GET", "/health"): ACCOUNT,
    ("GET", "/readiness"): ACCOUNT,
    ("GET", "/static/api/openapi.json"): ACCOUNT,
    # Removed endpoints that answer 410 Gone.
    ("GET", "/api/v1/contexts/current"): ACCOUNT,
    ("DELETE", "/api/v1/attachments/{attachment_id}"): ACCOUNT,
    ("GET", "/api/v1/attachments/memories/{memory_id}"): ACCOUNT,
    ("GET", "/api/v1/attachments/{attachment_id}"): ACCOUNT,
    ("POST", "/api/v1/attachments/memories/{memory_id}"): ACCOUNT,
    # The caller's own invitations; joining a suspended workspace gives
    # nothing until it is resumed.
    ("GET", "/api/v1/invitations/pending"): ACCOUNT,
    ("GET", "/api/v1/invitations/{token}"): ACCOUNT,
    ("POST", "/api/v1/invitations/accept"): ACCOUNT,
    # The workspace list (rows carry ``slot_suspended``) and creation, which
    # the #1550 cap governs.
    ("GET", "/api/v1/workspaces"): ACCOUNT,
    ("POST", "/api/v1/workspaces"): ACCOUNT,
    ("GET", "/api/v1/me/workspaces"): ACCOUNT,
    ("GET", "/api/v1/workspaces/plans/available"): ACCOUNT,
    ("GET", "/api/v1/workspaces/plans/tiers"): ACCOUNT,
    # The caller's own share keys; revoking is never blocked, and a share key
    # of a suspended workspace is refused per call.
    ("GET", "/api/v1/config/share-keys"): ACCOUNT,
    ("POST", "/api/v1/config/share-keys/{key_id}/revoke"): ACCOUNT,
    # ---- service to service ---------------------------------------------------
    ("GET", "/api/v1/workers/active-color"): INTERNAL,
    ("GET", "/api/v1/workers/apps"): INTERNAL,
    # ---- the operator surface in mixed routers ---------------------------------
    ("GET", "/api/v1/admin/cost-aggregation"): SYSTEM_ADMIN,
    ("GET", "/api/v1/admin/sleep-reports"): SYSTEM_ADMIN,
    ("GET", "/api/v1/admin/sleep-reports/{report_id}"): SYSTEM_ADMIN,
    ("GET", "/api/v1/admin/sleep-reports/{report_id}/actions"): SYSTEM_ADMIN,
    # ---- public readers and share keys ------------------------------------------
    ("GET", "/api/v1/public/{context_id}/info"): PUBLIC_OUTSIDER,
    ("POST", "/api/v1/public/{context_id}/search"): PUBLIC_OUTSIDER,
    ("POST", "/api/v1/share/recall"): PUBLIC_OUTSIDER,
    ("GET", "/api/v1/share/sessions"): PUBLIC_OUTSIDER,
}

# The calls that gate, by name, with the function each name stands for
# (``None``: the gate module itself). A call with ``slot=`` other than
# ``SlotPolicy.BLOCKED`` does not count. Every definition must itself reach the
# gate (``test_every_primitive_reaches_the_gate``), so a name cannot vouch for a
# route while the function behind it is ungated.
_PS = "services.permission_service:PermissionService"
GATED_PRIMITIVES: dict[str, str | None] = {
    "ensure_slot_active": None,
    "slot_suspended": None,
    "active_workspace_predicate": None,
    "check_workspace_access": f"{_PS}.check_workspace_access",
    "check_workspace_owner": f"{_PS}.check_workspace_owner",
    "check_workspace_admin": f"{_PS}.check_workspace_admin",
    "require_workspace_owner": "auth.dependencies:require_workspace_owner",
    "require_workspace_admin": "auth.dependencies:require_workspace_admin",
    "require_workspace_admin_session": "auth.dependencies:require_workspace_admin_session",
    "require_workspace_member": "auth.dependencies:require_workspace_member",
    "authorize_workspace_management": (
        "auth.programmatic_workspace_auth:authorize_workspace_management"
    ),
    "resolve_context_for_workspace_read": f"{_PS}.resolve_context_for_workspace_read",
    "check_context_access": f"{_PS}.check_context_access",
    "check_context_write": f"{_PS}.check_context_write",
    "check_context_owner": f"{_PS}.check_context_owner",
    "can_access_memory": f"{_PS}.can_access_memory",
    "get_accessible_contexts": f"{_PS}._get_accessible_contexts_rbac",
    "resolve_resource_by_slug": f"{_PS}.resolve_resource_by_slug",
    "reachable_memory_filter": "services.permission_service:reachable_memory_filter",
    "get_context": "services.context_service:ContextService.get_context",
    "list_contexts": "services.context_service:ContextService.list_contexts",
    "create_context": "services.context_service:ContextService.create_context",
    # The sync boundaries: every human sync route enters one of them.
    "sync_authority": "services.sync_authority:sync_authority",
    "lock_membership": "services.sync_feed_authority:lock_membership",
    "human_feed_boundary": "services.sync_feed_authority:human_feed_boundary",
    "worker_target": "services.sync_feed_authority:worker_target",
    "activity_boundary": "services.sync_activity:activity_boundary",
    "management_boundary": "services.sync_management:management_boundary",
    "read_mode_access": "services.sync_management:read_mode_access",
    # The credential principals.
    "get_observation_principal": "auth.observation_credentials:get_observation_principal",
    "current_binding": "auth.management_grants:current_binding",
    "get_share_key_principal": "auth.dependencies:get_share_key_principal",
    # The MCP dispatcher's target-less check.
    "slot_suspension_refusal": "mcp_server.tools._slot_gate:slot_suspension_refusal",
    # MemoryService's context chokepoint (ContextService.get_context).
    "_get_context_isolation_params": (
        "services.memory_service:MemoryService._get_context_isolation_params"
    ),
}

_MS = "services.memory_service:MemoryService"

# GATED routes whose gate is a function the handler reaches indirectly:
# ``"module:qualname"`` of the function that names a gated primitive.
_WS_SYNC = "/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}"
SLOT_SERVICE_GATED: dict[tuple[str, str], str] = {
    # The MCP transport: the dispatcher refuses a target-less call on a
    # suspended session workspace; named targets are gated by the resolvers.
    ("GET", "/mcp{path:path}"): "mcp_server.tools:_execute_tool_call",
    ("POST", "/mcp{path:path}"): "mcp_server.tools:_execute_tool_call",
    ("PUT", "/mcp{path:path}"): "mcp_server.tools:_execute_tool_call",
    ("PATCH", "/mcp{path:path}"): "mcp_server.tools:_execute_tool_call",
    ("DELETE", "/mcp{path:path}"): "mcp_server.tools:_execute_tool_call",
    # MemoryService: the context resolvers and can_access_memory.
    ("POST", "/api/v1/memory/remember"): f"{_MS}.remember",
    ("POST", "/api/v1/memory/reference"): f"{_MS}.reference",
    ("POST", "/api/v1/memory/explore"): f"{_MS}.explore",
    ("POST", "/api/v1/memory/pinned"): f"{_MS}.load_pinned",
    ("POST", "/api/v1/memory/guardrails"): f"{_MS}.load_guardrails",
    ("PATCH", "/api/v1/memory/{memory_id}"): f"{_MS}.patch_memory",
    ("GET", "/api/v1/memory/{memory_id}/revisions"): f"{_MS}.list_revisions",
    ("POST", "/api/v1/memory/forget"): f"{_MS}.forget",
    ("POST", "/api/v1/memory/forget-bulk"): f"{_MS}.forget_bulk",
    ("GET", "/api/v1/memory/guardrails/digest"): "services.guardrail_digest:fetch_entries",
    # ContextService reads through resolve_context_for_workspace_read.
    ("GET", "/api/v1/contexts/{context_id}/stats"): (
        "services.context_service:ContextService.get_context_stats"
    ),
    ("GET", "/api/v1/contexts/{context_id}/tags"): (
        "services.context_service:ContextService.aggregate_tags"
    ),
    ("POST", "/api/v1/agents/{agent_id}/bootstrap"): (
        "services.agent_bootstrap_service:AgentBootstrapService.resolve_context"
    ),
    # Human sync routes: the service enters a sync boundary.
    ("POST", "/api/v1/workspaces/{workspace_id}/sync-billing-handoffs"): (
        "services.sync_setup_handoff:create_setup_handoff"
    ),
    ("POST", "/api/v1/workspaces/{workspace_id}/sync-authorizations"): (
        "services.sync_installations:create_authorization"
    ),
    ("POST", "/api/v1/workspaces/{workspace_id}/sync-installations"): (
        "services.sync_installations:activate_installation"
    ),
    ("GET", _WS_SYNC): "services.sync_installations:inspect_installation",
    ("DELETE", _WS_SYNC): "services.sync_installations:unlink_installation",
    ("GET", f"{_WS_SYNC}/management"): "services.sync_management:control_mode",
    ("POST", f"{_WS_SYNC}/management/stop"): "services.sync_management:control_mode",
    ("POST", f"{_WS_SYNC}/management/finalize"): "services.sync_management:control_mode",
    ("GET", f"{_WS_SYNC}/management/access"): "services.sync_management:read_mode_access",
    ("POST", f"{_WS_SYNC}/management/reconnect"): "services.sync_resume:reconnect",
    ("POST", f"{_WS_SYNC}/management/resume/preview"): "services.sync_resume:resume_preview",
    ("GET", f"{_WS_SYNC}/management/resume/diff"): "services.sync_resume:resume_diff",
    ("POST", f"{_WS_SYNC}/management/resume/confirm"): "services.sync_resume:confirm_resume",
    ("POST", f"{_WS_SYNC}/management/resume/receipt"): (
        "services.sync_resume_receipt:read_resume_receipt"
    ),
    ("GET", f"{_WS_SYNC}/activity"): "services.sync_activity:overview",
    ("GET", f"{_WS_SYNC}/activity/feeds"): "services.sync_activity:feed_configuration",
    ("GET", f"{_WS_SYNC}/activity/feeds/{{feed_id}}"): "services.sync_activity:feed_configuration",
    ("GET", f"{_WS_SYNC}/activity/runs"): "services.sync_activity:runs_page",
    ("GET", f"{_WS_SYNC}/activity/runs/{{run_id}}"): "services.sync_activity:run_detail",
    ("GET", f"{_WS_SYNC}/activity/events"): "services.sync_activity:activity_events",
    ("POST", f"{_WS_SYNC}/activity/requests"): "services.sync_requests:create_request",
    ("PUT", f"{_WS_SYNC}/feeds/{{feed_id}}"): "services.sync_feed_human:configure_feed",
    ("GET", f"{_WS_SYNC}/records"): "services.sync_feed_human:project_records",
    ("GET", f"{_WS_SYNC}/runs/{{run_id}}/diff"): "services.sync_feed_human:human_diff",
    ("POST", f"{_WS_SYNC}/runs/{{run_id}}/approvals"): "services.sync_feed_human:approve_diff",
}

# Where an exception route's own gate lives (OWNER_MANAGEMENT and
# PUBLIC_OUTSIDER rows whose handler does not name it).
EXCEPTION_GATED_IN: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/contexts/{context_id}/export"): (
        "services.context_service:ContextService.export_context"
    ),
    ("DELETE", "/api/v1/workspaces/{workspace_id}/sync-authorizations/{authorization_id}"): (
        "services.sync_installations:revoke_authorization"
    ),
}

# Every ``SlotPolicy.OWNER_MANAGEMENT`` under backend/src: (module, function).
OWNER_MANAGEMENT_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("api.routes.workspaces", "delete_workspace"),
        ("api.routes.billing_handoff", "mint_billing_handoff"),
        ("services.context_service", "ContextService.export_context"),
        ("api.routes.member_credentials", "delete_api_key"),
        ("api.routes.member_credentials", "delete_api_key_by_id"),
        ("api.routes.member_credentials", "delete_oauth_app"),
        ("services.sync_installations", "revoke_authorization"),
    }
)

# Every ``slot=`` keyword that is not ``SlotPolicy.BLOCKED`` (``None`` skips
# the gate) under backend/src: (module, function).
SLOT_NONE_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        # The context resolver runs the gate itself, after the reach check.
        ("services.permission_service", "PermissionService.resolve_context_for_workspace_read"),
        # The internal billing service (no principal) reaches sync control.
        ("services.sync_authority", "sync_authority"),
        # The OIDC inspection answers read=false instead of refusing.
        ("api.routes.me_workspaces", "workspace_access"),
    }
)

_SRC = Path(__file__).resolve().parents[2] / "src"


def _category(key: tuple[str, str], module: str) -> str | None:
    """The exception category of a route, or ``None`` for GATED."""
    if key in EXCEPTIONS:
        return EXCEPTIONS[key]
    for prefix, category in PREFIX_DEFAULTS.items():
        if key[1] == prefix or key[1].startswith(prefix + "/"):
            return category
    return ROUTER_DEFAULTS.get(module)


def _is_gating_call(node: ast.Call) -> bool:
    if call_name(node) not in GATED_PRIMITIVES:
        return False
    for kw in node.keywords:
        if kw.arg == "slot" and not (_is_blocked_policy(kw.value) or _is_passthrough(kw.value)):
            return False
    return True


def _is_blocked_policy(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "BLOCKED"
        and isinstance(value.value, ast.Name)
        and value.value.id == "SlotPolicy"
    )


def _is_owner_management(value: ast.expr) -> bool:
    return (
        isinstance(value, ast.Attribute)
        and value.attr == "OWNER_MANAGEMENT"
        and isinstance(value.value, ast.Name)
        and value.value.id == "SlotPolicy"
    )


def _is_passthrough(value: ast.expr) -> bool:
    # ``slot=slot``: a wrapper handing its own caller's policy on.
    return isinstance(value, ast.Name) and value.id == "slot"


def _owner_class(func) -> type | None:
    """The class ``func`` is defined in (from its ``__qualname__``), if any."""
    raw = inspect.unwrap(func)
    parts = getattr(raw, "__qualname__", "").split(".")
    if len(parts) < 2 or "<locals>" in parts:
        return None
    owner = getattr(raw, "__globals__", {}).get(parts[0])
    for part in parts[1:-1]:
        owner = getattr(owner, part, None)
    return owner if isinstance(owner, type) else None


def _names_a_gate(func, *, exclude: str | None = None, _seen: set[int] | None = None) -> bool:
    """Whether ``func`` names a gated primitive, directly or through a helper of
    its own module (``_enforce_workspace_membership`` …) or its own class
    (``self._load_readable_memory`` …)."""
    seen = _seen if _seen is not None else set()
    if id(func) in seen:
        return False
    seen.add(id(func))
    name = getattr(func, "__name__", "")
    if name in GATED_PRIMITIVES and name != exclude:
        return True
    tree = function_ast(func)
    if tree is None:
        return False
    raw = inspect.unwrap(func)
    module = getattr(raw, "__module__", None)
    namespace = getattr(raw, "__globals__", {})
    owner = _owner_class(func)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if call_name(node) != exclude and _is_gating_call(node):
            return True
        helper = None
        if isinstance(node.func, ast.Name):
            helper = namespace.get(node.func.id)
            if getattr(helper, "__module__", None) != module:
                helper = None
        elif (
            owner is not None
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ("self", "cls")
        ):
            helper = getattr(owner, node.func.attr, None)
        if callable(helper) and _names_a_gate(helper, exclude=exclude, _seen=seen):
            return True
    return False


def _resolve(target: str):
    module_name, qualname = target.split(":")
    obj = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _routes() -> dict[tuple[str, str], tuple[str, object, tuple]]:
    return {
        (method, path): (route.endpoint.__module__.removeprefix("api.routes."), route, extra)
        for method, path, route, extra in _iter_routes()
    }


def test_every_route_is_gated_or_declared_once() -> None:
    routes = _routes()
    assert routes, "no routes found: the enumeration broke"
    modules = {module for module, _route, _extra in routes.values()}

    bad_defaults = sorted(m for m, c in ROUTER_DEFAULTS.items() if c not in _DEFAULTABLE)
    bad_prefixes = sorted(p for p, c in PREFIX_DEFAULTS.items() if c not in _DEFAULTABLE)
    stale_defaults = sorted(m for m in ROUTER_DEFAULTS if m not in modules)
    stale_rows = sorted(key for key in EXCEPTIONS if key not in routes)
    double = sorted(
        key
        for key, (module, _route, _extra) in routes.items()
        if key in EXCEPTIONS and module in ROUTER_DEFAULTS
    )
    assert not bad_defaults, f"router defaults allowed only for {_DEFAULTABLE}: {bad_defaults}"
    assert not bad_prefixes, f"prefix defaults allowed only for {_DEFAULTABLE}: {bad_prefixes}"
    assert not stale_defaults, f"router defaults for modules with no route: {stale_defaults}"
    assert not stale_rows, f"stale rows (route no longer exists): {stale_rows}"
    assert not double, f"routes declared both by row and by router default: {double}"


def test_every_gated_route_reaches_a_gated_primitive() -> None:
    """A route that is not an exception names a gate in its handler, in its
    dependency tree, or in the function ``SLOT_SERVICE_GATED`` names."""
    ungated = []
    for key, (module, route, extra) in sorted(_routes().items()):
        if _category(key, module) is not None:
            continue
        if key in SLOT_SERVICE_GATED:
            if not _names_a_gate(_resolve(SLOT_SERVICE_GATED[key])):
                ungated.append((key, SLOT_SERVICE_GATED[key]))
            continue
        callables = [route.endpoint, *route_dependency_calls(route, extra)]
        if not any(_names_a_gate(c) for c in callables):
            ungated.append(key)
    assert not ungated, f"GATED routes that reach no slot gate: {ungated}"


def test_service_gated_rows_are_gated_routes() -> None:
    routes = _routes()
    stale = sorted(key for key in SLOT_SERVICE_GATED if key not in routes)
    excepted = sorted(
        key for key in SLOT_SERVICE_GATED if key in routes and _category(key, routes[key][0])
    )
    assert not stale, f"stale SLOT_SERVICE_GATED rows: {stale}"
    assert not excepted, f"SLOT_SERVICE_GATED rows that are exceptions: {excepted}"


def _mentions_owner_management(func) -> bool:
    tree = function_ast(func)
    return tree is not None and any(
        isinstance(n, ast.Attribute) and _is_owner_management(n) for n in ast.walk(tree)
    )


def test_owner_management_routes_reach_the_opt_out() -> None:
    """An OWNER_MANAGEMENT row is gated with the owner's opt-out, not ungated."""
    routes = _routes()
    missing = []
    for key, category in EXCEPTIONS.items():
        if category != OWNER_MANAGEMENT:
            continue
        _module, route, extra = routes[key]
        callables = [route.endpoint, *route_dependency_calls(route, extra)]
        if key in EXCEPTION_GATED_IN:
            callables.append(_resolve(EXCEPTION_GATED_IN[key]))
        if not any(_mentions_owner_management(c) for c in callables):
            missing.append(key)
    assert not missing, f"OWNER_MANAGEMENT rows without SlotPolicy.OWNER_MANAGEMENT: {missing}"


def test_public_outsider_routes_gate_with_the_outsider_form() -> None:
    routes = _routes()
    missing = []
    for key, category in EXCEPTIONS.items():
        if category != PUBLIC_OUTSIDER:
            continue
        _module, route, extra = routes[key]
        callables = [route.endpoint, *route_dependency_calls(route, extra)]
        if key in EXCEPTION_GATED_IN:
            callables.append(_resolve(EXCEPTION_GATED_IN[key]))
        if not any(_names_a_gate(c) for c in callables):
            missing.append(key)
    assert not missing, f"PUBLIC_OUTSIDER rows that reach no slot gate: {missing}"


def _visit_slot_sites(
    module: str,
    node: ast.AST,
    scope: tuple[str, ...],
    owner_sites: set[tuple[str, str]],
    none_sites: set[tuple[str, str]],
) -> None:
    for child in ast.iter_child_nodes(node):
        inner = scope
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = (*scope, child.name)
        site = (module, ".".join(scope) or "<module>")
        if isinstance(child, ast.Attribute) and _is_owner_management(child):
            owner_sites.add(site)
        if (
            isinstance(child, ast.keyword)
            and child.arg == "slot"
            and not _is_blocked_policy(child.value)
            and not _is_owner_management(child.value)
            and not _is_passthrough(child.value)
        ):
            none_sites.add(site)
        _visit_slot_sites(module, child, inner, owner_sites, none_sites)


def _slot_sites() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """``(OWNER_MANAGEMENT sites, non-BLOCKED slot= sites)`` under backend/src."""
    owner_sites: set[tuple[str, str]] = set()
    none_sites: set[tuple[str, str]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        module = ".".join(path.relative_to(_SRC).with_suffix("").parts)
        if module == "services.workspace_slot_gate":
            continue  # the definition and the policy check themselves
        _visit_slot_sites(module, ast.parse(path.read_text()), (), owner_sites, none_sites)
    return owner_sites, none_sites


def test_owner_management_and_slot_none_sites_are_pinned() -> None:
    """Every opt-out of the gate is a reviewed decision (design 12.4)."""
    owner_sites, none_sites = _slot_sites()
    added, removed = owner_sites - OWNER_MANAGEMENT_SITES, OWNER_MANAGEMENT_SITES - owner_sites
    assert owner_sites == OWNER_MANAGEMENT_SITES, (
        f"SlotPolicy.OWNER_MANAGEMENT sites changed: added {sorted(added)}, removed {sorted(removed)}"
    )
    assert none_sites == SLOT_NONE_SITES, (
        f"slot=None sites changed: added {sorted(none_sites - SLOT_NONE_SITES)}, "
        f"removed {sorted(SLOT_NONE_SITES - none_sites)}"
    )


def test_every_primitive_reaches_the_gate() -> None:
    """Each name the table trusts stands for a function that gates."""
    from services import workspace_slot_gate

    hollow = []
    for name, target in sorted(GATED_PRIMITIVES.items()):
        if target is None:
            assert callable(getattr(workspace_slot_gate, name)), name
            continue
        if not _names_a_gate(_resolve(target), exclude=name):
            hollow.append((name, target))
    assert not hollow, f"primitives whose definition reaches no slot gate: {hollow}"
