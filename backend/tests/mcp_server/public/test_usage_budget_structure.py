"""Structural guards of the public usage budget (#2805).

Source / AST checks that pin where the budget is charged and written:

1. ``PUBLIC_HANDLERS`` is used by the dispatcher only, and in ``_run`` the
   charge comes before the handler lookup;
2. ``charge_public_usage`` is called from exactly the public MCP dispatcher
   and the REST public search, neither with ``now=`` (the test clock);
3. the table and the model are named only by the model, its registrations,
   the budget service and migrations: no installation, entitlement or tool
   module writes or reads it;
4. the budget service lowers ``calls_used`` only in the operator refill and
   imports nothing from plan tiers, add-ons, Redis or the authenticated surface.
"""

from __future__ import annotations

import ast
import re
from datetime import timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "src"
DISPATCH = SRC / "mcp_server" / "public" / "dispatch.py"
TOOLS = SRC / "mcp_server" / "public" / "tools.py"
SERVICE = SRC / "services" / "public_usage_budget.py"


def _python_sources() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(name)


def test_handlers_are_reached_only_through_the_dispatcher() -> None:
    users = {
        _rel(path)
        for path in _python_sources()
        if "PUBLIC_HANDLERS" in path.read_text(encoding="utf-8")
    }
    assert users == {"mcp_server/public/dispatch.py", "mcp_server/public/tools.py"}


def test_the_charge_precedes_the_handler_lookup_in_run() -> None:
    run = _function(ast.parse(DISPATCH.read_text(encoding="utf-8")), "_run")
    charge_lines = [
        node.lineno
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "charge_public_usage"
    ]
    lookup_lines = [
        node.lineno
        for node in ast.walk(run)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "PUBLIC_HANDLERS"
    ]
    bucket_lines = [
        node.lineno
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "check_public_rate_limit"
    ]
    assert len(charge_lines) == 1
    assert len(lookup_lines) == 1
    assert len(bucket_lines) == 1
    assert bucket_lines[0] < charge_lines[0] < lookup_lines[0]


def test_charge_callers_are_the_two_entry_points_without_a_clock() -> None:
    callers: dict[str, list[ast.Call]] = {}
    for path in _python_sources():
        if path == SERVICE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == "charge_public_usage":
                callers.setdefault(_rel(path), []).append(node)
    assert set(callers) == {"mcp_server/public/dispatch.py", "api/routes/public_search.py"}
    for rel, calls in callers.items():
        for call in calls:
            assert "now" not in {kw.arg for kw in call.keywords}, rel


def test_the_table_is_named_only_by_its_owners() -> None:
    allowed = {
        "models/public_usage_budget.py",
        "models/__init__.py",
        "models/data_boundary.py",
        "services/public_usage_budget.py",
    }
    pattern = re.compile(r"\bPublicUsageBudget\b|public_usage_budgets")
    named = {
        _rel(path) for path in _python_sources() if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert named <= allowed, named - allowed
    for rel in (
        "services/sync_installations.py",
        "services/sync_provisioning.py",
        "services/integration_entitlements.py",
        "mcp_server/public/tools.py",
    ):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "public_usage_budget" not in text, rel
        assert "PublicUsageBudget" not in text, rel


def test_no_module_imports_the_model_class_outside_its_owners() -> None:
    for path in _python_sources():
        rel = _rel(path)
        if rel in ("models/public_usage_budget.py", "services/public_usage_budget.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "models.public_usage_budget":
                names = {alias.name for alias in node.names}
                # The owner route and the CLI may use the status schema only.
                assert "PublicUsageBudget" not in names, rel


def test_the_service_lowers_the_count_only_in_the_refill() -> None:
    text = SERVICE.read_text(encoding="utf-8")
    lowered = [
        line.strip()
        for line in text.splitlines()
        if "calls_used = 0" in line or "calls_used - " in line or "calls_used -" in line
    ]
    assert lowered == [
        "\"SET calls_used = 0, updated_at = timezone('UTC', statement_timestamp()) \""
    ]
    for word in ("refund", "uncharge", "decrement", "dedup", "idempot"):
        assert word not in text.lower().replace("never refunds", "").replace(
            "nothing here refunds, decrements or deduplicates", ""
        ), word


def test_the_service_imports_nothing_it_must_not() -> None:
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    allowed_prefixes = (
        "__future__",
        "asyncio",
        "math",
        "dataclasses",
        "datetime",
        "typing",
        "uuid",
        "sqlalchemy",
        "config.settings",
        "db.base",
        "models.public_usage_budget",
        "utils.logger",
    )
    assert all(m.startswith(allowed_prefixes) for m in modules), modules
    text = SERVICE.read_text(encoding="utf-8")
    for banned in (
        "services.usage_stats",
        "mcp_server.auth",
        "integration_entitlements",
        "sync_",
        "plan_tiers",
        "redis",
        "addon",
        "public_calls_per",
    ):
        assert banned not in text, banned


def test_the_charge_is_one_statement_on_the_database_clock() -> None:
    from services.public_usage_budget import CHARGE_SQL, PUBLIC_USAGE_BUDGET_PERIOD

    sql = " ".join(CHARGE_SQL.split())
    assert sql.count(";") == 0
    assert sql.startswith("INSERT INTO public_usage_budgets AS b")
    assert "ON CONFLICT (workspace_id) DO UPDATE SET" in sql
    assert sql.endswith("RETURNING calls_used, period_started_at, period_seq")
    assert f"interval '{PUBLIC_USAGE_BUDGET_PERIOD.days} days'" in sql
    assert PUBLIC_USAGE_BUDGET_PERIOD == timedelta(days=PUBLIC_USAGE_BUDGET_PERIOD.days)
    assert "statement_timestamp()" in sql
    for clock in ("now()", "clock_timestamp", "current_timestamp", "localtimestamp"):
        assert clock not in sql.lower(), clock
    assert "b.calls_used < :limit" in sql
