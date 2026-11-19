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
import re
from pathlib import Path

from tests.api._route_inventory import (
    _iter_routes,
    call_name,
    function_ast,
    route_dependency_calls,
)

OWNER_MANAGEMENT = "owner_management"
REVOCATION = "revocation"
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
    # The owner's slot overview, selection and exportable contexts (#2918):
    # account-scoped, and usable while the owner's workspaces are suspended.
    "me_workspace_slots": ACCOUNT,
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

# One row per exception route of a router without a default.
EXCEPTIONS: dict[tuple[str, str], str] = {
    # ---- owner management: the owner passes, nobody else ------------------
    ("DELETE", "/api/v1/workspaces/{workspace_id}"): OWNER_MANAGEMENT,
    ("POST", "/api/v1/billing/handoff"): OWNER_MANAGEMENT,
    ("GET", "/api/v1/contexts/{context_id}/export"): OWNER_MANAGEMENT,
    # ---- revocation: never blocked; the route's own authorization decides --
    ("DELETE", "/api/v1/resource-tokens/{token_id}"): REVOCATION,
    ("DELETE", "/api/v1/observation-credentials/{credential_id}"): REVOCATION,
    ("DELETE", "/api/v1/resource-management-grants/{grant_id}"): REVOCATION,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-key",
    ): REVOCATION,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys/{key_id}",
    ): REVOCATION,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/oauth",
    ): REVOCATION,
    (
        "DELETE",
        "/api/v1/workspaces/{workspace_id}/sync-authorizations/{authorization_id}",
    ): REVOCATION,
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
    # Deployment settings any signed-in user reads (admin-only categories are
    # left out for others); the writes are the operator's.
    ("GET", "/api/v1/config"): ACCOUNT,
    ("GET", "/api/v1/config/categories"): ACCOUNT,
    ("GET", "/api/v1/config/schema"): ACCOUNT,
    ("POST", "/api/v1/config/validate"): ACCOUNT,
    ("PUT", "/api/v1/config/{key}"): SYSTEM_ADMIN,
    ("POST", "/api/v1/config/batch"): SYSTEM_ADMIN,
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
    ("GET", "/mcp/public/{public_id}{path:path}"): PUBLIC_OUTSIDER,
    ("POST", "/mcp/public/{public_id}{path:path}"): PUBLIC_OUTSIDER,
    ("PUT", "/mcp/public/{public_id}{path:path}"): PUBLIC_OUTSIDER,
    ("PATCH", "/mcp/public/{public_id}{path:path}"): PUBLIC_OUTSIDER,
    ("DELETE", "/mcp/public/{public_id}{path:path}"): PUBLIC_OUTSIDER,
    ("GET", "/api/v1/share/sessions"): PUBLIC_OUTSIDER,
}

# ---------------------------------------------------------------------------
# What gates
# ---------------------------------------------------------------------------

_PS = "services.permission_service:PermissionService"
# Module-level gates, matched by the object a call resolves to (a ``Name`` call
# through the caller's globals) or by identity in a route's dependency tree.
GATE_FUNCTIONS: dict[str, str | None] = {
    "ensure_slot_active": None,  # the gate itself
    "active_workspace_predicate": None,  # its SQL twin
    "reachable_memory_filter": "services.permission_service:reachable_memory_filter",
    "require_workspace_owner": "auth.dependencies:require_workspace_owner",
    "require_workspace_admin": "auth.dependencies:require_workspace_admin",
    "require_workspace_admin_session": "auth.dependencies:require_workspace_admin_session",
    "require_workspace_member": "auth.dependencies:require_workspace_member",
    "authorize_workspace_management": (
        "auth.programmatic_workspace_auth:authorize_workspace_management"
    ),
    "sync_authority": "services.sync_authority:sync_authority",
    "lock_membership": "services.sync_feed_authority:lock_membership",
    "human_feed_boundary": "services.sync_feed_authority:human_feed_boundary",
    "worker_target": "services.sync_feed_authority:worker_target",
    "activity_boundary": "services.sync_activity:activity_boundary",
    "management_boundary": "services.sync_management:management_boundary",
    "read_mode_access": "services.sync_management:read_mode_access",
    "get_observation_principal": "auth.observation_credentials:get_observation_principal",
    "current_binding": "auth.management_grants:current_binding",
    "get_share_key_principal": "auth.dependencies:get_share_key_principal",
    "serving_paused": "mcp_server.public.publication:serving_paused",
    "slot_suspension_refusal": "mcp_server.tools._slot_gate:slot_suspension_refusal",
}
# PermissionService methods, matched by name at an attribute call
# (``perm.check_workspace_access(...)``): each name has exactly one definition
# under backend/src (``test_method_gates_have_one_definition``), so the name
# is the function.
GATE_METHODS: dict[str, str] = {
    name: f"{_PS}.{target}"
    for name, target in {
        "check_workspace_access": "check_workspace_access",
        "check_workspace_owner": "check_workspace_owner",
        "check_workspace_admin": "check_workspace_admin",
        "resolve_context_for_workspace_read": "resolve_context_for_workspace_read",
        "check_context_access": "check_context_access",
        "check_context_write": "check_context_write",
        "check_context_owner": "check_context_owner",
        "can_access_memory": "can_access_memory",
        "get_accessible_contexts": "_get_accessible_contexts_rbac",
        "resolve_resource_by_slug": "resolve_resource_by_slug",
    }.items()
}

# ContextService methods, matched at an attribute call whose receiver is
# visibly a ContextService (``self.context_service`` / ``context_service`` /
# ``ContextService(db)``); other receivers go through SLOT_SERVICE_GATED.
_CS = "services.context_service:ContextService"
CONTEXT_SERVICE_GATES: dict[str, str] = {
    name: f"{_CS}.{name}" for name in ("get_context", "list_contexts", "create_context")
}
_CONTEXT_SERVICE_RECEIVER = re.compile(r"(^|\.)context_service$|^ContextService\(.*\)$")

# Definitions whose gate is an answer, not a refusal: they call
# ``slot_suspended`` (the batch-safe False of can_access_memory).
SUSPENSION_ANSWER_GATES = frozenset({"can_access_memory"})

_MS = "services.memory_service:MemoryService"
_WS_SYNC = "/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}"
# GATED routes whose gate sits behind a service method or a transport: the
# route -> the function that gates (checked to reach a gate). The handler must
# call it by name (the MCP mounts dispatch through the transport instead).
SLOT_SERVICE_GATED: dict[tuple[str, str], str | tuple[str, ...]] = {
    **{
        (m, "/mcp{path:path}"): "mcp_server.tools:_execute_tool_call"
        for m in ("GET", "POST", "PUT", "PATCH", "DELETE")
    },
    **{
        (m, "/mcp/w/{workspace_id}{path:path}"): "mcp_server.tools:_execute_tool_call"
        for m in ("GET", "POST", "PUT", "PATCH", "DELETE")
    },
    ("GET", "/api/v1/contexts"): f"{_CS}.list_contexts",
    ("GET", "/api/v1/contexts/{context_id}"): f"{_CS}.get_context",
    ("POST", "/api/v1/contexts"): f"{_CS}.create_context",
    ("POST", "/api/v1/invitations/accept"): (
        "services.invitation_service:InvitationService.accept_invitation"
    ),
    ("GET", "/api/v1/memory-health"): (
        "services.memory_health_service:MemoryHealthService.build_breakdown",
        "services.memory_health_service:MemoryHealthService.build_context_report",
    ),
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
    ("GET", "/api/v1/contexts/{context_id}/stats"): f"{_CS}.get_context_stats",
    ("GET", "/api/v1/contexts/{context_id}/tags"): f"{_CS}.aggregate_tags",
    ("POST", "/api/v1/agents/{agent_id}/bootstrap"): (
        "services.agent_bootstrap_service:AgentBootstrapService.resolve_context"
    ),
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
_MCP_TRANSPORT = "mcp_server.tools:_execute_tool_call"

# GATED routes that answer a fixed "not usable" state for a suspended
# workspace instead of refusing: they must call ``slot_suspended`` itself.
SLOT_STATE_ROUTES: dict[tuple[str, str], str] = {
    # The connector worker skips a team that is not ready (before the ETag).
    ("GET", "/api/v1/workers/config"): "not ready (404)",
    # OIDC inspection: 200 with read, sync.manage and usage.read false.
    ("GET", "/api/v1/me/workspaces/{workspace_id}/access"): "read=false",
}

# Where an exception route's own gate (or opt-out) lives when the handler does
# not name it.
EXCEPTION_GATED_IN: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/contexts/{context_id}/export"): f"{_CS}.export_context",
    ("DELETE", "/api/v1/workspaces/{workspace_id}/sync-authorizations/{authorization_id}"): (
        "services.sync_installations:revoke_authorization"
    ),
    **{
        (m, "/mcp/public/{public_id}{path:path}"): "mcp_server.public.publication:serving_paused"
        for m in ("GET", "POST", "PUT", "PATCH", "DELETE")
    },
}

# The operator's identity checks; every SYSTEM_ADMIN route has one in its
# dependency tree.
ADMIN_DEPENDENCIES = (
    "auth.dependencies:require_admin",
    "auth.dependencies:require_admin_account",
    "auth.dependencies:_require_service_operator",
)

# ---------------------------------------------------------------------------
# The pins: every opt-out, by (module, function) -> number of occurrences
# ---------------------------------------------------------------------------

OWNER_MANAGEMENT_SITES: dict[tuple[str, str], int] = {
    ("api.routes.workspaces", "delete_workspace"): 1,
    ("api.routes.billing_handoff", "mint_billing_handoff"): 1,
    ("services.context_service", "ContextService.export_context"): 1,
}
REVOCATION_SITES: dict[tuple[str, str], int] = {
    ("api.routes.member_credentials", "delete_api_key"): 1,
    # The owner-key branch (authorize_workspace_management) and the session branch.
    ("api.routes.member_credentials", "delete_api_key_by_id"): 2,
    ("api.routes.member_credentials", "delete_oauth_app"): 1,
    ("services.sync_installations", "revoke_authorization"): 1,
    # WorkspaceOwnerRevocation: the resource-token, observation-credential and
    # management-grant revoke routes.
    ("auth.dependencies", "require_workspace_owner_for_revocation"): 1,
}
# Every ``slot=`` keyword (or ``slot`` parameter default, or ``"slot"`` dict
# key) that is not ``SlotPolicy.BLOCKED``: ``None``, a passthrough, a policy.
SLOT_KEYWORD_SITES: dict[tuple[str, str], int] = {
    # slot=SlotPolicy.OWNER_MANAGEMENT / REVOCATION (also counted above).
    ("api.routes.workspaces", "delete_workspace"): 1,
    ("api.routes.billing_handoff", "mint_billing_handoff"): 1,
    ("services.context_service", "ContextService.export_context"): 1,
    ("api.routes.member_credentials", "delete_api_key_by_id"): 1,
    ("services.sync_installations", "revoke_authorization"): 1,
    ("auth.dependencies", "require_workspace_owner_for_revocation"): 1,
    # slot=None: the context resolver gates itself after the reach check; the
    # internal billing service (no principal); the OIDC inspection answers
    # read=false; resource ingest refuses the token holder in the redacted form.
    ("services.permission_service", "PermissionService.resolve_context_for_workspace_read"): 1,
    # The write resolvers gate after the key's workspace and the agent binding.
    ("mcp_server.tools._helpers", "_resolve_context"): 1,
    ("services.memory_service", "MemoryService._get_context_isolation_params"): 1,
    ("services.sync_authority", "sync_authority"): 2,  # None for no principal, else slot=slot
    ("api.routes.me_workspaces", "workspace_access"): 1,
    ("api.routes.resource_ingest", "_enforce_workspace_membership"): 1,
    # slot=slot: wrappers handing their caller's policy on.
    ("services.permission_service", "PermissionService.check_workspace_owner"): 1,
    ("services.permission_service", "PermissionService.check_workspace_admin"): 1,
    ("auth.programmatic_workspace_auth", "authorize_workspace_management"): 2,
    ("auth.dependencies", "_require_workspace_owner"): 1,
    # A log field of the service-token key rotation, not the gate.
    ("auth.dependencies", "_require_service_operator"): 1,
}
# ``export_context`` hard-codes the owner's opt-out: its callers are pinned.
EXPORT_CALLERS: dict[tuple[str, str], int] = {
    ("api.routes.contexts", "export_context"): 1,
}

_SRC = Path(__file__).resolve().parents[2] / "src"


def _resolve(target: str):
    module_name, qualname = target.split(":")
    obj = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _gate_objects() -> dict[int, str]:
    from services import workspace_slot_gate

    found: dict[int, str] = {}
    for name, target in GATE_FUNCTIONS.items():
        obj = getattr(workspace_slot_gate, name) if target is None else _resolve(target)
        found[id(inspect.unwrap(obj))] = name
    return found


def _answer_gates() -> dict[int, str]:
    """The gates plus ``slot_suspended``, for SUSPENSION_ANSWER_GATES only."""
    from services.workspace_slot_gate import slot_suspended

    return {**_gate_objects(), id(inspect.unwrap(slot_suspended)): "slot_suspended"}


def _category(key: tuple[str, str], module: str) -> str | None:
    """The exception category of a route, or ``None`` for GATED."""
    if key in EXCEPTIONS:
        return EXCEPTIONS[key]
    return ROUTER_DEFAULTS.get(module)


def _is_policy(value: ast.expr, member: str) -> bool:
    return (
        isinstance(value, ast.Attribute)
        and value.attr == member
        and isinstance(value.value, ast.Name)
        and value.value.id == "SlotPolicy"
    )


def _own_body(func) -> list[ast.stmt]:
    """The statements of ``func``'s own body (decorators unwrapped)."""
    tree = function_ast(func)
    if tree is None:
        return []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.body
    return []


def _live_calls(nodes: list) -> list[ast.Call]:
    """The calls a body makes itself: not inside a nested function, lambda or
    class, nor in an ``if`` whose test is a false constant."""
    calls: list[ast.Call] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return
        if isinstance(node, ast.If) and isinstance(node.test, ast.Constant):
            for child in node.body if node.test.value else node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Call):
            calls.append(node)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for node in nodes:
        visit(node)
    return calls


def _gating_call(call: ast.Call, namespace: dict, gates: dict[int, str]) -> bool:
    """A call to a gate with no opt-out (``slot=`` other than BLOCKED or a passthrough)."""
    for kw in call.keywords:
        if kw.arg == "slot" and not (_is_policy(kw.value, "BLOCKED") or _is_passthrough(kw.value)):
            return False
        if kw.arg == "policy" and not (
            _is_policy(kw.value, "BLOCKED") or _is_passthrough(kw.value)
        ):
            return False
        if kw.arg is None:
            return False  # **kwargs could carry an opt-out
    if isinstance(call.func, ast.Name):
        target = namespace.get(call.func.id)
        return target is not None and id(inspect.unwrap(target)) in gates
    if isinstance(call.func, ast.Attribute):
        if call.func.attr in GATE_METHODS:
            return True
        if call.func.attr in CONTEXT_SERVICE_GATES and _CONTEXT_SERVICE_RECEIVER.search(
            ast.unparse(call.func.value)
        ):
            return True
        # ``module.function(...)``: resolve the module attribute.
        if isinstance(call.func.value, ast.Name):
            holder = namespace.get(call.func.value.id)
            target = getattr(holder, call.func.attr, None) if holder is not None else None
            return target is not None and id(inspect.unwrap(target)) in gates
    return False


def _is_passthrough(value: ast.expr) -> bool:
    # ``slot=slot`` / ``policy=slot``: a wrapper handing its own caller's
    # policy on (every such site is pinned below).
    return isinstance(value, ast.Name) and value.id in ("slot", "policy")


def _namespace(func) -> dict:
    """``func``'s globals plus the names its body imports locally."""
    raw = inspect.unwrap(func)
    names = dict(getattr(raw, "__globals__", {}))
    for stmt in _own_body(func):
        for node in ast.walk(stmt):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                try:
                    module = importlib.import_module(node.module)
                except ImportError:
                    continue
                for alias in node.names:
                    if hasattr(module, alias.name):
                        names[alias.asname or alias.name] = getattr(module, alias.name)
    return names


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


def _reaches_gate(func, gates: dict[int, str], *, _seen: set[int] | None = None) -> bool:
    """Whether ``func``'s own body calls a gate, directly or through a helper
    of its own module (a ``Name`` call) or its own class (``self.x(...)``).
    The function's name plays no part."""
    seen = _seen if _seen is not None else set()
    raw = inspect.unwrap(func)
    if id(raw) in seen:
        return False
    seen.add(id(raw))
    namespace = _namespace(func)
    module = getattr(raw, "__module__", None)
    owner = _owner_class(func)
    for call in _live_calls(_own_body(func)):
        if _gating_call(call, namespace, gates):
            return True
        helper = None
        if isinstance(call.func, ast.Name):
            helper = namespace.get(call.func.id)
            if getattr(inspect.unwrap(helper), "__module__", None) != module:
                helper = None
        elif (
            owner is not None
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in ("self", "cls")
        ):
            helper = getattr(owner, call.func.attr, None)
        if callable(helper) and _reaches_gate(helper, gates, _seen=seen):
            return True
    return False


def _route_gated(route, extra, gates: dict[int, str]) -> bool:
    for dep in route_dependency_calls(route, extra):
        if id(inspect.unwrap(dep)) in gates or _reaches_gate(dep, gates):
            return True
    return _reaches_gate(route.endpoint, gates)


def _calls_by_name(func, name: str) -> bool:
    """Whether ``func`` (or a helper of its module / class) calls ``name``."""
    seen: set[int] = set()

    def walk(f) -> bool:
        raw = inspect.unwrap(f)
        if id(raw) in seen:
            return False
        seen.add(id(raw))
        namespace = getattr(raw, "__globals__", {})
        module = getattr(raw, "__module__", None)
        for call in _live_calls(_own_body(f)):
            if call_name(call) == name:
                return True
            if isinstance(call.func, ast.Name):
                helper = namespace.get(call.func.id)
                if (
                    callable(helper)
                    and getattr(inspect.unwrap(helper), "__module__", None) == module
                    and walk(helper)
                ):
                    return True
        return False

    return walk(func)


def _routes(*, include_mcp: bool = True) -> dict[tuple[str, str], tuple[str, object, tuple]]:
    return {
        (method, path): (route.endpoint.__module__.removeprefix("api.routes."), route, extra)
        for method, path, route, extra in _iter_routes(include_mcp=include_mcp)
    }


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


def test_every_route_is_gated_or_declared_once() -> None:
    routes = _routes()
    assert routes, "no routes found: the enumeration broke"
    modules = {module for module, _route, _extra in routes.values()}

    bad_defaults = sorted(m for m, c in ROUTER_DEFAULTS.items() if c not in _DEFAULTABLE)
    stale_defaults = sorted(m for m in ROUTER_DEFAULTS if m not in modules)
    stale_rows = sorted(key for key in EXCEPTIONS if key not in routes)
    double = sorted(
        key
        for key, (module, _route, _extra) in routes.items()
        if key in EXCEPTIONS and module in ROUTER_DEFAULTS
    )
    assert not bad_defaults, f"router defaults allowed only for {_DEFAULTABLE}: {bad_defaults}"
    assert not stale_defaults, f"router defaults for modules with no route: {stale_defaults}"
    assert not stale_rows, f"stale rows (route no longer exists): {stale_rows}"
    assert not double, f"routes declared both by row and by router default: {double}"


def test_the_workspace_slot_account_routes_are_account_routes() -> None:
    """The four routes of ``/api/v1/me/workspace-slots`` (#2918) are ACCOUNT.

    The owner reads and changes the selection of a suspended workspace and
    lists its exportable contexts there, so none of them may reach the gate.
    """
    routes = _routes()
    slot_routes = {
        key: module
        for key, (module, _route, _extra) in routes.items()
        if key[1].startswith("/api/v1/me/workspace-slots")
    }
    assert sorted(slot_routes) == [
        ("GET", "/api/v1/me/workspace-slots"),
        ("GET", "/api/v1/me/workspace-slots/workspaces/{workspace_id}/contexts"),
        ("POST", "/api/v1/me/workspace-slots/preview"),
        ("PUT", "/api/v1/me/workspace-slots/selection"),
    ]
    assert {key: _category(key, module) for key, module in slot_routes.items()} == dict.fromkeys(
        slot_routes, ACCOUNT
    )


def test_every_gated_route_reaches_a_gate() -> None:
    """A route that is not an exception calls a gate (resolved, not by its own
    name) in its handler or its dependency tree, or is declared in
    ``SLOT_SERVICE_GATED`` / ``SLOT_STATE_ROUTES``."""
    gates = _gate_objects()
    ungated = []
    for key, (module, route, extra) in sorted(_routes().items()):
        if _category(key, module) is not None:
            continue
        if key in SLOT_SERVICE_GATED:
            targets = SLOT_SERVICE_GATED[key]
            for target in (targets,) if isinstance(targets, str) else targets:
                name = target.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
                if not _reaches_gate(_resolve(target), gates):
                    ungated.append((key, target, "target reaches no gate"))
                elif target != _MCP_TRANSPORT and not _calls_by_name(route.endpoint, name):
                    ungated.append((key, target, "handler does not call it"))
            continue
        if key in SLOT_STATE_ROUTES:
            if not _calls_by_name(route.endpoint, "slot_suspended"):
                ungated.append((key, "slot_suspended not called"))
            continue
        if not _route_gated(route, extra, gates):
            ungated.append(key)
    assert not ungated, f"GATED routes that reach no slot gate: {ungated}"


def test_declared_sets_name_gated_routes() -> None:
    routes = _routes()
    for table in (SLOT_SERVICE_GATED, SLOT_STATE_ROUTES):
        stale = sorted(key for key in table if key not in routes)
        excepted = sorted(key for key in table if key in routes and _category(key, routes[key][0]))
        assert not stale, f"stale rows: {stale}"
        assert not excepted, f"rows that are exceptions: {excepted}"
    assert not set(SLOT_SERVICE_GATED) & set(SLOT_STATE_ROUTES)


def test_every_gate_definition_reaches_the_gate() -> None:
    """Each gate the table trusts is a function that gates."""
    hollow = []
    every = {**GATE_FUNCTIONS, **GATE_METHODS, **CONTEXT_SERVICE_GATES}
    for name, target in sorted(every.items()):
        if target is None:
            continue
        gates = _answer_gates() if name in SUSPENSION_ANSWER_GATES else _gate_objects()
        own = {k: v for k, v in gates.items() if v != name}
        if not _reaches_gate(_resolve(target), own):
            hollow.append((name, target))
    assert not hollow, f"gates whose definition reaches no slot gate: {hollow}"


def test_method_gates_have_one_definition() -> None:
    """A method gate is matched by name: the name must mean one function."""
    counts: dict[str, int] = dict.fromkeys(GATE_METHODS, 0)
    for path in _SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in counts:
                counts[node.name] += 1
    assert all(n == 1 for n in counts.values()), counts


# A handler that only shares a gate's name, one whose gate call is nested or
# dead, and one that opts out, must not pass.
async def check_workspace_access() -> None:  # the name of a gate, no call
    return None


async def _nested_gate(perm) -> None:
    async def inner() -> None:
        await perm.check_workspace_access("u", None)

    del inner


async def _dead_gate(perm) -> None:
    if False:
        await perm.check_workspace_access("u", None)


async def _opted_out(perm) -> None:
    await perm.check_workspace_access("u", None, slot=None)


async def _gated(perm) -> None:
    await perm.check_workspace_access("u", None)


def test_the_matcher_is_exact() -> None:
    gates = _gate_objects()
    for fake in (check_workspace_access, _nested_gate, _dead_gate, _opted_out):
        assert not _reaches_gate(fake, gates), fake.__name__
    assert _reaches_gate(_gated, gates)


def _mentions(func, member: str) -> bool:
    return any(
        _is_policy(node, member)
        for stmt in _own_body(func)
        for node in ast.walk(stmt)
        if isinstance(node, ast.Attribute)
    )


def _dependency_names(route, extra) -> set[str]:
    return {
        f"{getattr(inspect.unwrap(d), '__module__', '')}:{getattr(d, '__name__', '')}"
        for d in route_dependency_calls(route, extra)
    }


def test_owner_management_and_revocation_routes_reach_their_opt_out() -> None:
    routes = _routes()
    missing = []
    for key, category in EXCEPTIONS.items():
        if category not in (OWNER_MANAGEMENT, REVOCATION):
            continue
        member = "OWNER_MANAGEMENT" if category == OWNER_MANAGEMENT else "REVOCATION"
        _module, route, extra = routes[key]
        callables = [route.endpoint, *route_dependency_calls(route, extra)]
        if key in EXCEPTION_GATED_IN:
            callables.append(_resolve(EXCEPTION_GATED_IN[key]))
        if not any(_mentions(c, member) for c in callables):
            missing.append(key)
    assert not missing, f"opt-out rows without their SlotPolicy: {missing}"


def test_public_outsider_routes_reach_a_gate() -> None:
    gates = _gate_objects()
    routes = _routes()
    missing = []
    for key, category in EXCEPTIONS.items():
        if category != PUBLIC_OUTSIDER:
            continue
        _module, route, extra = routes[key]
        if key in EXCEPTION_GATED_IN:
            target = _resolve(EXCEPTION_GATED_IN[key])
            if id(inspect.unwrap(target)) in gates:
                continue
        if not _route_gated(route, extra, gates):
            missing.append(key)
    assert not missing, f"PUBLIC_OUTSIDER rows that reach no slot gate: {missing}"


def test_system_admin_routes_require_the_operator() -> None:
    routes = _routes()
    admin = set(ADMIN_DEPENDENCIES)
    missing = sorted(
        key
        for key, (module, route, extra) in routes.items()
        if _category(key, module) == SYSTEM_ADMIN and not _dependency_names(route, extra) & admin
    )
    assert not missing, f"SYSTEM_ADMIN routes without an operator dependency: {missing}"


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


def _scan() -> dict[str, dict[tuple[str, str], int]]:
    found: dict[str, dict[tuple[str, str], int]] = {
        "owner": {},
        "revocation": {},
        "dynamic": {},
        "slot": {},
        "export": {},
    }

    def bump(kind: str, site: tuple[str, str]) -> None:
        found[kind][site] = found[kind].get(site, 0) + 1

    for path in sorted(_SRC.rglob("*.py")):
        module = ".".join(path.relative_to(_SRC).with_suffix("").parts)
        if module == "services.workspace_slot_gate":
            continue  # the definitions themselves
        _visit(module, ast.parse(path.read_text()), (), bump)
    return found


def _visit(module: str, node: ast.AST, scope: tuple[str, ...], bump) -> None:
    for child in ast.iter_child_nodes(node):
        inner = scope
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner = (*scope, child.name)
        site = (module, ".".join(scope) or "<module>")
        if _is_policy(child, "OWNER_MANAGEMENT"):
            bump("owner", site)
        if _is_policy(child, "REVOCATION"):
            bump("revocation", site)
        # SlotPolicy("…"), SlotPolicy[…], getattr(SlotPolicy, …): not allowed.
        if isinstance(child, ast.Call) and (
            (isinstance(child.func, ast.Name) and child.func.id == "SlotPolicy")
            or (
                call_name(child) == "getattr"
                and child.args
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id == "SlotPolicy"
            )
        ):
            bump("dynamic", site)
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.value, ast.Name)
            and child.value.id == "SlotPolicy"
        ):
            bump("dynamic", site)
        if (
            isinstance(child, ast.keyword)
            and child.arg == "slot"
            and not _is_policy(child.value, "BLOCKED")
        ):
            bump("slot", site)
        if isinstance(child, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "slot" for k in child.keys
        ):
            bump("slot", site)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = child.args
            params = [*args.posonlyargs, *args.args]
            defaults = [None] * (len(params) - len(args.defaults)) + list(args.defaults)
            pairs = list(zip(params, defaults, strict=True)) + list(
                zip(args.kwonlyargs, args.kw_defaults, strict=True)
            )
            for arg, default in pairs:
                if arg.arg == "slot" and default is not None and not _is_policy(default, "BLOCKED"):
                    bump("slot", (module, ".".join(inner)))
        if isinstance(child, ast.Call) and call_name(child) == "export_context":
            bump("export", site)
        _visit(module, child, inner, bump)


def test_every_opt_out_is_pinned() -> None:
    """Every reference to a non-default policy and every ``slot=`` that is not
    ``SlotPolicy.BLOCKED`` is a reviewed decision (design 12.4, owner 2026-11-19)."""
    found = _scan()
    assert found["dynamic"] == {}, f"SlotPolicy built dynamically: {found['dynamic']}"
    assert found["owner"] == OWNER_MANAGEMENT_SITES, found["owner"]
    assert found["revocation"] == REVOCATION_SITES, found["revocation"]
    assert found["slot"] == SLOT_KEYWORD_SITES, found["slot"]
    assert found["export"] == EXPORT_CALLERS, found["export"]
