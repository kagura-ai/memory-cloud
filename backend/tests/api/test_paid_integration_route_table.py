"""Coverage table for paid-integration authority boundaries (#2801)."""

from __future__ import annotations

import ast
from pathlib import Path

from api.main import app

NONE = "none"
GUARDED = "guarded"

ROUTER_MODULES = frozenset(
    {
        "admin",
        "admin_mcp_notices",
        "admin_memory_health",
        "admin_neural",
        "admin_operations",
        "admin_plans",
        "admin_referrals",
        "admin_signup_gate",
        "admin_sleep",
        "agent_state",
        "agents",
        "analyses",
        "announcement_emails",
        "api_keys",
        "attachments",
        "auth",
        "beta_invites",
        "billing_handoff",
        "bm25_drift",
        "config",
        "connectors_slack",
        "context_search_config",
        "contexts",
        "cost_aggregation",
        "email_signup",
        "external_keys",
        "feedback",
        "files",
        "graph",
        "internal_billing",
        "internal_integrations",
        "invitations",
        "management_grants",
        "mcp",
        "mcp_notices",
        "me_account",
        "me_oauth",
        "me_oauth_grants",
        "me_terms",
        "me_workspaces",
        "member_credentials",
        "memory",
        "memory_health",
        "mfa_settings",
        "neural_config",
        "oauth",
        "observability",
        "observation_credentials",
        "oidc",
        "operation_history",
        "passkeys",
        "password",
        "provider_link_confirm",
        "public_mcp_endpoints",
        "public_mcp_usage",
        "public_search",
        "referrals",
        "resource_indexer",
        "resource_ingest",
        "resource_management",
        "resource_schema",
        "resource_tokens",
        "resources",
        "secrets",
        "share_keys",
        "sleep_reports",
        "system",
        "system_admins",
        "users",
        "well_known",
        "worker_apps",
        "workers",
        "workspace",
        "workspace_connectors",
        "workspace_plan",
        "workspaces",
    }
)

ROUTER_CLASSIFICATION = dict.fromkeys(ROUTER_MODULES, NONE)

SOURCE_ROOT = Path(__file__).parents[2] / "src"
SYMBOL_OWNERS = {
    "models/__init__.py": "model-registration",
    "models/integration_entitlement.py": "model-definitions",
    "services/integration_entitlements.py": "authority-service",
}


def _walk_routes(routes, prefix: str = ""):
    for route in routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _walk_routes(
                original.routes, prefix + (getattr(route.include_context, "prefix", "") or "")
            )
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        yield endpoint


def _registered_router_modules() -> set[str]:
    return {
        endpoint.__module__.removeprefix("api.routes.")
        for endpoint in _walk_routes(app.routes)
        if endpoint.__module__.startswith("api.routes.")
    }


def _authority_references(path: Path, tree: ast.AST) -> bool:
    source = path.read_text(encoding="utf-8")
    if any(
        token in source
        for token in (
            "paid_integration_write",
            "require_paid_integration",
            "inspect_paid_integration",
            "WorkspaceIntegrationState",
            "WorkspaceIntegrationLicense",
            "WorkspaceIntegrationEvent",
        )
    ):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "models.integration_entitlement" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "models.integration_entitlement":
                return True
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "models.integration_entitlement"
        ):
            return True
    return False


def test_every_registered_router_has_an_explicit_paid_integration_decision():
    registered = _registered_router_modules()
    assert registered == set(ROUTER_CLASSIFICATION), (
        "paid-integration route table must classify every registered router: "
        f"missing={sorted(registered - set(ROUTER_CLASSIFICATION))}, "
        f"stale={sorted(set(ROUTER_CLASSIFICATION) - registered)}"
    )
    assert set(ROUTER_CLASSIFICATION.values()) <= {NONE, GUARDED}


def test_authority_symbols_have_an_explicit_owner_and_guarded_routes():
    found: set[str] = set()
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _authority_references(path, tree):
            found.add(path.relative_to(SOURCE_ROOT).as_posix())

    assert found == set(SYMBOL_OWNERS), (
        "paid-integration symbols must have an explicit owner: "
        f"missing={sorted(found - set(SYMBOL_OWNERS))}, "
        f"stale={sorted(set(SYMBOL_OWNERS) - found)}"
    )

    guarded = {module for module, role in ROUTER_CLASSIFICATION.items() if role == GUARDED}
    for module in guarded:
        path = SOURCE_ROOT / "api" / "routes" / f"{module}.py"
        source = path.read_text(encoding="utf-8")
        assert any(
            token in source for token in ("paid_integration_write", "require_paid_integration")
        ), f"guarded router {module} does not reach the paid integration guard"
