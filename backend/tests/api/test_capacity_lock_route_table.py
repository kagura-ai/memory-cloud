"""Every REST route is classified against the capacity-over lock (#1948).

The MCP dispatcher keeps a table of every tool (``_capacity_gate``) and a test
that the table covers the registry exactly. This is the REST twin, in two
layers so it stays maintainable at ~300 routes:

* a **router-module default** for routers that never touch memory content
  (auth, admin, billing, OAuth, …) — ``NOT_APPLICABLE`` — or whose whole
  surface is metadata (``ALLOWED``);
* an explicit **(method, path) row** for every route of a *content* router,
  with the policy the route must carry: ``BLOCKED`` (refused with
  ``CAPACITY-001`` once authorized) or ``ALLOWED`` (the cleanup / inspection
  surface that keeps working under the lock).

The test fails when a route is unclassified (a new route was added to a
content router without a decision), or when a row is stale (the route is gone),
or when a content router falls back to a default (every row must be explicit).
Behaviour per route group is pinned by ``test_capacity_lock_routes.py``; the
resolver gate itself by ``tests/services/test_permission_capacity_policy.py``.
"""

from __future__ import annotations

from api.main import app

BLOCKED = "blocked"
ALLOWED = "allowed"
NOT_APPLICABLE = "n/a"


# Layer 1 — routers whose whole surface shares one policy.
ROUTER_DEFAULTS: dict[str, str] = {
    "api.main": NOT_APPLICABLE,
    "fastapi.applications": NOT_APPLICABLE,  # /docs, /redoc
    "admin": NOT_APPLICABLE,
    "admin_memory_health": NOT_APPLICABLE,
    "admin_neural": NOT_APPLICABLE,
    "admin_plans": NOT_APPLICABLE,
    "admin_referrals": NOT_APPLICABLE,
    "admin_signup_gate": NOT_APPLICABLE,
    "admin_sleep": NOT_APPLICABLE,
    "api_keys": NOT_APPLICABLE,
    "auth": NOT_APPLICABLE,
    "beta_invites": NOT_APPLICABLE,
    "billing_handoff": NOT_APPLICABLE,
    "bm25_drift": NOT_APPLICABLE,
    "config": NOT_APPLICABLE,
    "connectors_slack": NOT_APPLICABLE,
    "cost_aggregation": NOT_APPLICABLE,
    "external_keys": NOT_APPLICABLE,
    "internal_billing": NOT_APPLICABLE,
    "invitations": NOT_APPLICABLE,
    "mcp": NOT_APPLICABLE,
    "me_account": NOT_APPLICABLE,
    "me_oauth": NOT_APPLICABLE,
    "me_terms": NOT_APPLICABLE,
    "member_credentials": NOT_APPLICABLE,
    "neural_config": NOT_APPLICABLE,
    "oauth": NOT_APPLICABLE,
    "password": NOT_APPLICABLE,
    "referrals": NOT_APPLICABLE,
    "secrets": NOT_APPLICABLE,
    "system": NOT_APPLICABLE,
    "system_admins": NOT_APPLICABLE,
    "users": NOT_APPLICABLE,
    "well_known": NOT_APPLICABLE,
    "worker_apps": NOT_APPLICABLE,
    "workers": NOT_APPLICABLE,
    "workspace_connectors": NOT_APPLICABLE,
    "workspace_plan": NOT_APPLICABLE,
    # Metadata-only surfaces: counts, usage, members, tokens, schemas. They
    # are what an owner uses to find their way back under the cap.
    "workspace": ALLOWED,
    "workspaces": ALLOWED,
    "resources": ALLOWED,
    "resource_indexer": ALLOWED,
    "resource_schema": ALLOWED,
    "resource_tokens": ALLOWED,
    # Attachments were not gated by #1941 (bytes count toward the storage
    # limit when confirmed); kept as they are.
    "attachments": ALLOWED,
}

# Layer 2 — content routers: every route named, no default.
ROUTES: dict[tuple[str, str], str] = {
    # memory — the MCP recall/remember family
    ("POST", "/api/v1/memory/recall"): BLOCKED,
    ("POST", "/api/v1/memory/reference"): BLOCKED,
    ("POST", "/api/v1/memory/explore"): BLOCKED,
    ("POST", "/api/v1/memory/pinned"): BLOCKED,
    ("POST", "/api/v1/memory/remember"): BLOCKED,
    ("PATCH", "/api/v1/memory/{memory_id}"): BLOCKED,
    ("POST", "/api/v1/memory/guardrails"): BLOCKED,
    ("GET", "/api/v1/memory/guardrails/digest"): ALLOWED,  # MCP load_guardrails
    ("POST", "/api/v1/memory/forget"): ALLOWED,
    ("POST", "/api/v1/memory/forget-bulk"): ALLOWED,
    ("GET", "/api/v1/memory/list"): ALLOWED,
    ("GET", "/api/v1/memory/stats"): ALLOWED,
    ("GET", "/api/v1/memory/access-patterns"): ALLOWED,
    # contexts — creation is blocked (MCP create_context); the rest is the
    # cleanup / inspection surface
    ("POST", "/api/v1/contexts"): BLOCKED,
    ("GET", "/api/v1/contexts"): ALLOWED,
    ("GET", "/api/v1/contexts/current"): ALLOWED,
    ("GET", "/api/v1/contexts/{context_id}"): ALLOWED,
    ("PUT", "/api/v1/contexts/{context_id}"): ALLOWED,
    ("DELETE", "/api/v1/contexts/{context_id}"): ALLOWED,
    ("GET", "/api/v1/contexts/{context_id}/duplicates"): ALLOWED,
    ("GET", "/api/v1/contexts/{context_id}/export"): ALLOWED,
    ("GET", "/api/v1/contexts/{context_id}/members"): ALLOWED,
    ("POST", "/api/v1/contexts/{context_id}/members"): ALLOWED,
    ("PUT", "/api/v1/contexts/{context_id}/members/{user_id}"): ALLOWED,
    ("DELETE", "/api/v1/contexts/{context_id}/members/{user_id}"): ALLOWED,
    ("GET", "/api/v1/contexts/{context_id}/memory-stats"): ALLOWED,
    ("GET", "/api/v1/contexts/{context_id}/stats"): ALLOWED,
    ("GET", "/api/v1/contexts/{context_id}/tags"): ALLOWED,
    # graph — memory content (MCP list_edges / create_edge)
    ("GET", "/api/v1/graph/data"): BLOCKED,
    ("GET", "/api/v1/graph/stats"): BLOCKED,
    ("POST", "/api/v1/graph/edges"): BLOCKED,
    # agent state — MCP set_state / get_state; delete is cleanup
    ("PUT", "/api/v1/contexts/{context_id}/state/{key}"): BLOCKED,
    ("GET", "/api/v1/contexts/{context_id}/state"): BLOCKED,
    ("GET", "/api/v1/contexts/{context_id}/state/{key}"): BLOCKED,
    ("DELETE", "/api/v1/contexts/{context_id}/state/{key}"): ALLOWED,
    # feedback — MCP feedback
    ("POST", "/api/v1/contexts/{context_id}/feedback"): BLOCKED,
    ("POST", "/api/v1/contexts/{context_id}/host-feedback"): BLOCKED,
    # search settings — MCP update_search_config; reading them is metadata
    ("GET", "/api/v1/contexts/{context_id}/search-config"): ALLOWED,
    ("PUT", "/api/v1/contexts/{context_id}/search-config"): BLOCKED,
    ("POST", "/api/v1/contexts/{context_id}/search-config/reset"): BLOCKED,
    # analyses — derive from memory content; cancelling only stops work
    ("POST", "/api/v1/contexts/{context_id}/analyses"): BLOCKED,
    ("POST", "/api/v1/contexts/{context_id}/analyses/preview"): BLOCKED,
    ("GET", "/api/v1/contexts/{context_id}/analyses"): BLOCKED,
    ("GET", "/api/v1/contexts/{context_id}/analyses/active"): BLOCKED,
    ("GET", "/api/v1/contexts/{context_id}/analyses/{run_id}"): BLOCKED,
    ("GET", "/api/v1/contexts/{context_id}/analyses/{run_id}/clusters"): BLOCKED,
    ("GET", "/api/v1/contexts/{context_id}/analyses/{run_id}/positions"): BLOCKED,
    ("DELETE", "/api/v1/contexts/{context_id}/analyses/{run_id}"): ALLOWED,
    # sleep reports — a workspace's reports are memory-derived (MCP
    # get_sleep_history / get_sleep_report); the admin surface is cross-workspace
    ("GET", "/api/v1/workspaces/{workspace_id}/sleep-reports"): BLOCKED,
    ("GET", "/api/v1/workspaces/{workspace_id}/sleep-reports/{report_id}"): BLOCKED,
    ("GET", "/api/v1/admin/sleep-reports"): ALLOWED,
    ("GET", "/api/v1/admin/sleep-reports/{report_id}"): ALLOWED,
    # share keys — outsiders read content (redacted refusal); key management is metadata
    ("POST", "/api/v1/share/recall"): BLOCKED,
    ("GET", "/api/v1/share/sessions"): BLOCKED,
    ("GET", "/api/v1/config/share-keys"): ALLOWED,
    ("POST", "/api/v1/config/share-keys"): ALLOWED,
    ("POST", "/api/v1/config/share-keys/{key_id}/revoke"): ALLOWED,
    # public search — anonymous readers are outsiders
    ("POST", "/api/v1/public/{context_id}/search"): BLOCKED,
    ("GET", "/api/v1/public/{context_id}/info"): BLOCKED,
    # agents — bootstrap rehydrates memory content; the registry is metadata
    ("POST", "/api/v1/agents/{agent_id}/bootstrap"): BLOCKED,
    ("GET", "/api/v1/agents"): ALLOWED,
    ("POST", "/api/v1/agents"): ALLOWED,
    ("GET", "/api/v1/agents/{agent_id}"): ALLOWED,
    ("PATCH", "/api/v1/agents/{agent_id}"): ALLOWED,
    ("DELETE", "/api/v1/agents/{agent_id}"): ALLOWED,
    ("GET", "/api/v1/agents/{agent_id}/bindings"): ALLOWED,
    ("POST", "/api/v1/agents/{agent_id}/bindings"): ALLOWED,
    ("PATCH", "/api/v1/agents/{agent_id}/bindings/{binding_id}"): ALLOWED,
    ("DELETE", "/api/v1/agents/{agent_id}/bindings/{binding_id}"): ALLOWED,
    # files — uploads are new bytes; download / list / delete are the way out
    ("POST", "/api/v1/files/reserve"): BLOCKED,
    ("POST", "/api/v1/files/{file_id}/confirm"): BLOCKED,
    ("GET", "/api/v1/files"): ALLOWED,
    ("GET", "/api/v1/files/{file_id}/download-url"): ALLOWED,
    ("DELETE", "/api/v1/files/{file_id}"): ALLOWED,
    # resource ingest — MCP ingest_events
    ("POST", "/api/v1/resources/{resource_id}/events"): BLOCKED,
    ("POST", "/api/v1/resources/{resource_id}/events/batch"): BLOCKED,
}

CONTENT_ROUTERS = {
    "memory",
    "contexts",
    "graph",
    "agent_state",
    "feedback",
    "context_search_config",
    "analyses",
    "sleep_reports",
    "share_keys",
    "public_search",
    "agents",
    "files",
    "resource_ingest",
}


def _registered_routes() -> dict[tuple[str, str], str]:
    """``{(method, path): router module}`` for every REST route of the app.

    FastAPI registers included routers lazily (``_IncludedRouter``); the
    original router and its include prefix are read back, so this needs no
    application startup (no Redis, no database).
    """
    rows: dict[tuple[str, str], str] = {}

    def add(route, prefix: str) -> None:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or path is None:
            return
        full = prefix + path
        if "/mcp" in full or full in ("/openapi.json", "/docs/oauth2-redirect"):
            return
        module = route.endpoint.__module__.removeprefix("api.routes.")
        for method in methods - {"HEAD", "OPTIONS"}:
            rows[(method, full)] = module

    for route in app.routes:
        original = getattr(route, "original_router", None)
        if original is None:
            add(route, "")
            continue
        prefix = getattr(route.include_context, "prefix", "") or ""
        for sub in original.routes:
            add(sub, prefix)
    return rows


def test_every_route_is_classified_exactly_once() -> None:
    registered = _registered_routes()
    assert registered, "no routes found — the enumeration broke"

    unclassified = sorted(
        key
        for key, module in registered.items()
        if key not in ROUTES and module not in ROUTER_DEFAULTS
    )
    stale = sorted(key for key in ROUTES if key not in registered)
    # A content router must not lean on a default: every route is a decision.
    leaking = sorted(
        key for key, module in registered.items() if module in CONTENT_ROUTERS and key not in ROUTES
    )
    # And a default must not also carry explicit rows (one layer per router).
    double = sorted(
        key for key, module in registered.items() if key in ROUTES and module in ROUTER_DEFAULTS
    )
    assert not unclassified, f"unclassified routes (add a row or a router default): {unclassified}"
    assert not stale, f"stale rows (route no longer exists): {stale}"
    assert not leaking, f"content-router routes without an explicit row: {leaking}"
    assert not double, f"routes classified in both layers: {double}"
    assert CONTENT_ROUTERS == {registered[key] for key in ROUTES}


def test_the_blocked_set_matches_the_mcp_decisions() -> None:
    """The REST rows and the MCP table say the same thing about shared surfaces."""
    from mcp_server.tools._capacity_gate import (
        CAPACITY_LOCK_ALLOWED_TOOLS,
        CAPACITY_LOCK_BLOCKED_TOOLS,
    )

    twins = {
        ("POST", "/api/v1/memory/recall"): "recall",
        ("POST", "/api/v1/memory/reference"): "reference",
        ("POST", "/api/v1/memory/explore"): "explore",
        ("POST", "/api/v1/memory/pinned"): "load_pinned",
        ("POST", "/api/v1/memory/remember"): "remember",
        ("PATCH", "/api/v1/memory/{memory_id}"): "update_memory",
        ("GET", "/api/v1/memory/guardrails/digest"): "load_guardrails",
        ("POST", "/api/v1/memory/forget"): "forget",
        ("GET", "/api/v1/memory/list"): "list",
        ("POST", "/api/v1/contexts"): "create_context",
        ("DELETE", "/api/v1/contexts/{context_id}"): "delete_context",
        ("PUT", "/api/v1/contexts/{context_id}"): "update_context",
        ("GET", "/api/v1/contexts/{context_id}"): "get_context_info",
        ("GET", "/api/v1/graph/data"): "list_edges",
        ("POST", "/api/v1/graph/edges"): "create_edge",
        ("PUT", "/api/v1/contexts/{context_id}/state/{key}"): "set_state",
        ("GET", "/api/v1/contexts/{context_id}/state/{key}"): "get_state",
        ("POST", "/api/v1/contexts/{context_id}/feedback"): "feedback",
        ("PUT", "/api/v1/contexts/{context_id}/search-config"): "update_search_config",
        ("POST", "/api/v1/contexts/{context_id}/analyses"): "analyze_context",
        ("GET", "/api/v1/contexts/{context_id}/analyses/{run_id}"): "get_analysis",
        ("GET", "/api/v1/contexts/{context_id}/analyses"): "list_analyses",
        ("GET", "/api/v1/workspaces/{workspace_id}/sleep-reports"): "get_sleep_history",
        ("POST", "/api/v1/agents/{agent_id}/bootstrap"): "get_agent_bootstrap",
        ("POST", "/api/v1/files/reserve"): "init_file_upload",
        ("POST", "/api/v1/files/{file_id}/confirm"): "complete_file_upload",
        ("GET", "/api/v1/files/{file_id}/download-url"): "get_file_download_url",
        ("DELETE", "/api/v1/files/{file_id}"): "delete_file",
        ("POST", "/api/v1/resources/{resource_id}/events"): "ingest_events",
        ("GET", "/api/v1/agents"): "list_agents",
        ("DELETE", "/api/v1/agents/{agent_id}"): "delete_agent",
    }
    for route, tool in twins.items():
        expected = BLOCKED if tool in CAPACITY_LOCK_BLOCKED_TOOLS else ALLOWED
        assert tool in CAPACITY_LOCK_BLOCKED_TOOLS | CAPACITY_LOCK_ALLOWED_TOOLS, tool
        assert ROUTES[route] == expected, f"{route} disagrees with MCP {tool}"
