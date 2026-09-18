"""Tests for utils.plan_resolver (#674 sub-A, #675, #1550).

Pure unit tests — no DB, no live settings. The single ``execute()``
call inside ``get_user_workspace_cap_summary`` is mocked so each
test fully owns the ``WorkspaceCapSummary`` output.

The mock simulates ``result.one_or_none()`` on the JOIN query: it
returns a Row-shaped object whose attribute access via
``row.owned_count``, ``row.workspace_slot_bonus`` and
``row.owned_plan_names`` matches the SQLAlchemy result interface used
in the helper. The helper then computes
``cap = 1 + workspace_slot_bonus + owned_workspace_grant(highest owned
tier)`` internally (#1550).

The last section pins the call-site allow-list: the cap is consumed by
the creation gate and two read-only dashboards only — rename / delete /
list never consult it (block-new-creation-only, #1550).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.plan_resolver import (
    BASE_CAP,
    WorkspaceCapSummary,
    get_user_workspace_cap_summary,
    next_tier_with_more_workspaces,
    resolve_workspace_cap,
    tier_owned_workspace_cap,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def _mock_db(
    owned_count: int | None,
    slot_bonus: int = 0,
    owned_plan_names: list[str | None] | None = None,
):
    """Mock AsyncSession.execute returning a Row(owned_count, slot_bonus, plan_names).

    Set ``owned_count=None`` to simulate the missing-user case where
    ``one_or_none()`` returns ``None``. ``owned_plan_names`` defaults to
    the PostgreSQL shape of ``array_agg`` over a no-match LEFT JOIN
    (``[None]``) so the zero-workspace case mirrors production.
    """
    db = MagicMock()
    db.execute = AsyncMock()
    execute_result = MagicMock()
    if owned_count is None:
        execute_result.one_or_none = MagicMock(return_value=None)
    else:
        row = MagicMock()
        row.owned_count = owned_count
        row.workspace_slot_bonus = slot_bonus
        row.owned_plan_names = [None] if owned_plan_names is None else owned_plan_names
        execute_result.one_or_none = MagicMock(return_value=row)
    db.execute.return_value = execute_result
    return db


# ----------------------------------------------------------------------
# Pure formula (#1550): cap = BASE_CAP + slot_bonus + tier_grant
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "expected_cap"),
    [("free", 1), ("basic", 1), ("pro", 3), ("promax", 20)],
)
def test_four_tiers_with_zero_bonus_cap_1_1_3_20(tier: str, expected_cap: int) -> None:
    summary = resolve_workspace_cap(owned_count=1, slot_bonus=0, owned_plan_names=[tier])
    assert summary.cap == expected_cap
    assert summary.tier == tier
    assert summary.base == BASE_CAP == 1
    assert summary.slot_bonus == 0
    assert summary.tier_grant == expected_cap - 1
    assert summary.cap == summary.base + summary.slot_bonus + summary.tier_grant


def test_slot_bonus_stacks_on_tier_grant() -> None:
    """pro (grant 2) + admin/referral bonus 1 → 1 + 1 + 2 = 4."""
    summary = resolve_workspace_cap(owned_count=2, slot_bonus=1, owned_plan_names=["pro"])
    assert (summary.cap, summary.slot_bonus, summary.tier_grant) == (4, 1, 2)


def test_highest_owned_tier_wins() -> None:
    """Owns a free and a pro workspace → the pro grant applies (cap 3)."""
    summary = resolve_workspace_cap(
        owned_count=2, slot_bonus=0, owned_plan_names=["free", "pro", "basic"]
    )
    assert summary.tier == "pro"
    assert summary.cap == 3


def test_downgrade_drops_cap_but_owned_count_is_untouched() -> None:
    """Block-new-only: after the pro workspace is downgraded to basic the cap
    falls to 1 while the user still owns 3 — over cap, nothing is removed."""
    summary = resolve_workspace_cap(
        owned_count=3, slot_bonus=0, owned_plan_names=["basic", "free", "free"]
    )
    assert summary.tier == "basic"
    assert summary.cap == 1
    assert summary.owned_count == 3
    assert summary.owned_count >= summary.cap  # the create gate refuses; that is all


@pytest.mark.parametrize("names", [[], [None]])
def test_no_owned_workspaces_resolves_to_lowest_tier_base_cap(names: list) -> None:
    """Zero owned (Python empty list or PostgreSQL ``array_agg`` ``[NULL]``)
    → lowest tier, grant 0, cap = base."""
    summary = resolve_workspace_cap(owned_count=0, slot_bonus=0, owned_plan_names=names)
    assert summary.tier == "free"
    assert summary.tier_grant == 0
    assert summary.cap == 1


def test_unknown_plan_name_ranks_as_lowest_tier() -> None:
    summary = resolve_workspace_cap(owned_count=1, slot_bonus=0, owned_plan_names=["enterprise"])
    assert summary.tier == "free"
    assert summary.cap == 1


def test_tier_owned_workspace_cap_is_the_matrix_row() -> None:
    """``owned_workspaces`` served by the plan matrix = 1 + grant."""
    from config.plan_tiers import PLAN_ORDER, get_plan_tier

    assert [tier_owned_workspace_cap(get_plan_tier(p)) for p in PLAN_ORDER] == [1, 1, 3, 20]


@pytest.mark.parametrize(
    ("tier", "expected_next"),
    [("free", "pro"), ("basic", "pro"), ("pro", "promax"), ("promax", None)],
)
def test_next_tier_with_more_workspaces_skips_equal_grants(
    tier: str, expected_next: str | None
) -> None:
    """Upsell target = the lowest higher tier that actually grants MORE
    (basic grants the same as free, so free upsells straight to pro)."""
    assert next_tier_with_more_workspaces(tier) == expected_next


# ----------------------------------------------------------------------
# DB-backed helper (mocked SELECT) — one resolution site
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_workspaces_no_bonus_returns_base_cap():
    """Brand-new user: 0 owned, 0 bonus, no tier → cap = 1 (base)."""
    db = _mock_db(owned_count=0, slot_bonus=0)
    summary = await get_user_workspace_cap_summary(db, "user-1")
    assert isinstance(summary, WorkspaceCapSummary)
    assert summary.owned_count == 0
    assert summary.cap == 1
    assert summary.tier == "free"


@pytest.mark.asyncio
async def test_one_owned_no_bonus_at_cap():
    """Base case: 1 free workspace, 0 bonus → count == cap."""
    db = _mock_db(owned_count=1, slot_bonus=0, owned_plan_names=["free"])
    summary = await get_user_workspace_cap_summary(db, "user-1")
    assert (summary.owned_count, summary.cap) == (1, 1)


@pytest.mark.asyncio
async def test_pro_owner_gets_three():
    db = _mock_db(owned_count=1, slot_bonus=0, owned_plan_names=["pro"])
    summary = await get_user_workspace_cap_summary(db, "user-1")
    assert (summary.owned_count, summary.cap, summary.tier_grant) == (1, 3, 2)


@pytest.mark.asyncio
async def test_promax_owner_with_bonus_stacks():
    db = _mock_db(owned_count=5, slot_bonus=2, owned_plan_names=["promax", "free"])
    summary = await get_user_workspace_cap_summary(db, "user-1")
    assert summary.tier == "promax"
    assert summary.cap == 22  # 1 + 2 + 19


@pytest.mark.asyncio
async def test_grandfathered_five_owned_bonus_four_at_cap():
    """Grandfather case: 5 free workspaces, bonus=4 → cap = 5, at cap."""
    db = _mock_db(owned_count=5, slot_bonus=4, owned_plan_names=["free"] * 5)
    summary = await get_user_workspace_cap_summary(db, "user-1")
    assert (summary.owned_count, summary.cap) == (5, 5)


@pytest.mark.asyncio
async def test_admin_granted_bonus_no_workspaces_yet():
    """Phase 1 admin grant before user creates: 0 owned, 3 bonus → cap 4."""
    db = _mock_db(owned_count=0, slot_bonus=3)
    summary = await get_user_workspace_cap_summary(db, "user-1")
    assert (summary.owned_count, summary.cap) == (0, 4)


@pytest.mark.asyncio
async def test_single_select():
    """The gate and the dashboards rely on ONE round-trip for all numbers."""
    db = _mock_db(owned_count=1, slot_bonus=0, owned_plan_names=["pro"])
    await get_user_workspace_cap_summary(db, "user-1")
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_missing_user_returns_zero_owned_base_cap():
    """Defensive: helper returns ``(0, 1)`` if the User row is not found.

    Theoretically unreachable because the caller has already passed
    authentication, but a fail-safe default avoids crashing the gate
    or the dashboard if something upstream returns a stale user_id.
    """
    db = _mock_db(owned_count=None)
    summary = await get_user_workspace_cap_summary(db, "user-1")
    assert (summary.owned_count, summary.cap) == (0, 1)


# ----------------------------------------------------------------------
# Call-site allow-list (#1550): the cap gates CREATE only
# ----------------------------------------------------------------------

# Every production module allowed to resolve the owned-workspace cap. The
# gate is the only writer-side consumer; the other two are read-only
# dashboards (owner usage widget, admin user detail / slot-bonus PATCH /
# users list). Adding a file here needs a design reason — rename, delete,
# list, switch and membership paths must never consult the cap.
RESOLVER_CALL_SITE_ALLOW_LIST = {
    "services/quota_service.py",
    "api/routes/usage.py",
    "api/routes/admin.py",
}
_RESOLVER_CALL = re.compile(r"\b(get_user_workspace_cap_summary|resolve_workspace_cap)\(")


def _src_files_calling_resolver() -> set[str]:
    hits: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "__pycache__" in path.parts or path == SRC / "utils" / "plan_resolver.py":
            continue
        if _RESOLVER_CALL.search(path.read_text(encoding="utf-8")):
            hits.add(path.relative_to(SRC).as_posix())
    return hits


def test_resolver_call_sites_match_allow_list() -> None:
    assert _src_files_calling_resolver() == RESOLVER_CALL_SITE_ALLOW_LIST


def _functions_calling(module: Path, callee: str) -> set[str]:
    """Names of the top-level functions in ``module`` whose body calls ``callee``."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    callers: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == callee
            ):
                callers.add(node.name)
    return callers


def test_workspace_routes_gate_only_create() -> None:
    """``POST /workspaces`` is the sole route consulting the cap; rename
    (``update_workspace``), ``delete_workspace``, ``list_workspaces`` and the
    rest never do — an over-cap user keeps and manages every workspace."""
    routes = SRC / "api" / "routes" / "workspaces.py"
    assert _functions_calling(routes, "check_workspace_creation_allowed") == {"create_workspace"}
    assert "plan_resolver" not in routes.read_text(encoding="utf-8")


def test_create_gate_is_the_only_consumer_in_quota_service() -> None:
    quota = SRC / "services" / "quota_service.py"
    assert _functions_calling(quota, "get_user_workspace_cap_summary") == set()  # method, not fn
    tree = ast.parse(quota.read_text(encoding="utf-8"))
    methods = {
        fn.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for fn in cls.body
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
        and any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "get_user_workspace_cap_summary"
            for sub in ast.walk(fn)
        )
    }
    assert methods == {"check_workspace_creation_allowed"}
