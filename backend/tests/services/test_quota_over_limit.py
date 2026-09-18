"""Over-limit workspaces: nothing breaks, only creating MORE is refused (#1552).

Hosted deployments lower the S/M/L limits via ``PLAN_*`` env overrides, so some
workspaces wake up with usage ABOVE their limit. The intended behaviour is that
nothing is deleted and nothing breaks — reads / recall / update / delete / Sleep
keep working — and only creation is refused until the workspace is back under
the limit. The ``QuotaService`` gates are "count vs limit at creation", so they
already behave this way, but nothing pinned it and the gates were never
exercised with usage strictly above (not just at) the limit.

Mock style mirrors ``test_quota_service.py`` (MagicMock/AsyncMock db, no
Postgres): each helper arms ``db.execute`` with the exact SELECT sequence the
gate issues. The limits below are deliberately NOT the tier defaults — they
stand in for an env-lowered limit.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.quota_service import QuotaService
from utils.exceptions import QuotaExceededError

LIMIT = 500  # the env-lowered memory limit
OVER, AT, UNDER = LIMIT + 250, LIMIT, LIMIT - 1


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def service(mock_db):
    return QuotaService(mock_db)


@pytest.fixture
def workspace_id():
    return uuid4()


def _make_workspace(workspace_id, plan_name="basic", **effective):
    """A workspace mock whose ``effective_*`` limits are set explicitly.

    The gates read the model's ``effective_*`` properties (directly or via
    ``EffectiveQuotaService``), so the "env-lowered" limit is injected there
    rather than through ``PLAN_TIERS``.
    """
    workspace = MagicMock()
    workspace.id = workspace_id
    workspace.plan_name = plan_name
    for name, value in effective.items():
        setattr(workspace, name, value)
    return workspace


def _returning(workspace):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=workspace)
    return result


def _counting(n):
    result = MagicMock()
    result.scalar = MagicMock(return_value=n)
    return result


# --------------------------------------------------------------------------- #
# check_memory_quota
# --------------------------------------------------------------------------- #


def _arm_memory(mock_db, workspace, count):
    """Call order: select(Workspace) FOR UPDATE → count → EffectiveQuotaService select(Workspace)."""
    mock_db.execute.side_effect = [_returning(workspace), _counting(count), _returning(workspace)]


class TestMemoryQuotaOverLimit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("count", "can_create"),
        [
            pytest.param(OVER, False, id="over-limit-refused"),
            pytest.param(AT, False, id="at-limit-refused"),
            pytest.param(UNDER, True, id="back-under-limit-allowed"),
        ],
    )
    async def test_creation_is_gated_by_count_vs_limit(
        self, service, mock_db, workspace_id, count, can_create
    ):
        ws = _make_workspace(workspace_id, effective_memory_limit=LIMIT)
        _arm_memory(mock_db, ws, count)

        ok, error = await service.check_memory_quota(workspace_id)

        assert ok is can_create
        if can_create:
            assert error is None
        else:
            assert f"Current: {count}, Limit: {LIMIT}" in error

    @pytest.mark.asyncio
    async def test_over_limit_raises_when_asked(self, service, mock_db, workspace_id):
        ws = _make_workspace(workspace_id, effective_memory_limit=LIMIT)
        _arm_memory(mock_db, ws, OVER)

        with pytest.raises(QuotaExceededError, match=f"Current: {OVER}, Limit: {LIMIT}"):
            await service.check_memory_quota(workspace_id, raise_on_exceeded=True)


# --------------------------------------------------------------------------- #
# check_context_creation_allowed
# --------------------------------------------------------------------------- #

CONTEXT_LIMIT = 3


def _arm_contexts(mock_db, workspace, count):
    """Call order: select(Workspace) → count(Context)."""
    mock_db.execute.side_effect = [_returning(workspace), _counting(count)]


class TestContextGateOverLimit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("count", "can_create"),
        [
            pytest.param(CONTEXT_LIMIT + 2, False, id="over-limit-refused"),
            pytest.param(CONTEXT_LIMIT, False, id="at-limit-refused"),
            pytest.param(CONTEXT_LIMIT - 1, True, id="back-under-limit-allowed"),
        ],
    )
    async def test_creation_is_gated_by_count_vs_limit(
        self, service, mock_db, workspace_id, count, can_create
    ):
        ws = _make_workspace(workspace_id, effective_max_contexts=CONTEXT_LIMIT)
        _arm_contexts(mock_db, ws, count)

        ok, error = await service.check_context_creation_allowed(workspace_id)

        assert ok is can_create
        if can_create:
            assert error is None
        else:
            assert f"allows {CONTEXT_LIMIT} context(s)" in error

    @pytest.mark.asyncio
    async def test_over_limit_raises_when_asked(self, service, mock_db, workspace_id):
        ws = _make_workspace(workspace_id, effective_max_contexts=CONTEXT_LIMIT)
        _arm_contexts(mock_db, ws, CONTEXT_LIMIT + 2)

        with pytest.raises(QuotaExceededError, match="Context limit reached"):
            await service.check_context_creation_allowed(workspace_id, raise_on_denied=True)


# --------------------------------------------------------------------------- #
# check_member_quota
# --------------------------------------------------------------------------- #

MEMBER_LIMIT = 10


def _arm_members(mock_db, workspace, count):
    """Call order: select(Workspace) → count(members) → count(pending) → EffectiveQuotaService."""
    mock_db.execute.side_effect = [
        _returning(workspace),
        _counting(count),
        _counting(0),
        _returning(workspace),
    ]


class TestMemberQuotaOverLimit:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("count", "can_invite"),
        [
            pytest.param(MEMBER_LIMIT + 5, False, id="over-limit-refused"),
            pytest.param(MEMBER_LIMIT, False, id="at-limit-refused"),
            pytest.param(MEMBER_LIMIT - 1, True, id="back-under-limit-allowed"),
        ],
    )
    async def test_invites_are_gated_by_count_vs_limit(
        self, service, mock_db, workspace_id, count, can_invite
    ):
        ws = _make_workspace(workspace_id, "pro", effective_max_members=MEMBER_LIMIT)
        _arm_members(mock_db, ws, count)

        ok, error = await service.check_member_quota(workspace_id)

        assert ok is can_invite
        if can_invite:
            assert error is None
        else:
            assert f"Member limit reached ({MEMBER_LIMIT} seats)" in error


# --------------------------------------------------------------------------- #
# get_quota_status at >100 %
# --------------------------------------------------------------------------- #


def _numeric_leaves(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _numeric_leaves(value)
    elif isinstance(obj, int | float) and not isinstance(obj, bool):
        yield obj


class TestQuotaStatusOverLimit:
    @pytest.mark.asyncio
    async def test_status_reports_raw_numbers_and_an_unclamped_percentage(
        self, service, mock_db, workspace_id
    ):
        """The dashboard must be able to say "750 / 500 (150 %)", so the status
        carries the raw numbers and a >100 percentage — nothing clamped, nothing
        negative (there is no "remaining" key to go below zero)."""
        # #1549 added the daily block to the status payload; give the mock a
        # daily limit too so the raw-number assertions cover it.
        ws = _make_workspace(
            workspace_id, effective_memory_limit=LIMIT, effective_memories_per_day=50
        )
        members_result = MagicMock()
        members_result.all = MagicMock(return_value=[("user-1",)])
        # Call order: select(Workspace) → select(member user_ids) → count(Memory).
        mock_db.execute.side_effect = [_returning(ws), members_result, _counting(OVER)]

        status = await service.get_quota_status(workspace_id)

        memory = status["memory"]
        assert (memory["current"], memory["limit"]) == (OVER, LIMIT)
        assert memory["percentage"] == 150.0
        assert memory["exceeded"] is True
        assert memory["warning"] is True
        assert all(value >= 0 for value in _numeric_leaves(status))


# --------------------------------------------------------------------------- #
# Call-site pin — the "reads / updates / deletes are not gated" proof
# --------------------------------------------------------------------------- #

SRC = Path(__file__).resolve().parents[2] / "src"

_GATE_CALL = re.compile(r"\.check_(memory_quota|context_creation_allowed|member_quota)\(")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")

# Every quota-gate call in backend/src, by file → enclosing function. Each one
# is a CREATE path (a new memory, context, or seat); none is a read, update or
# delete. The function name is pinned too, because a file-level allow-list
# would not notice a gate added to, say, ``MemoryService.delete_memory`` inside
# an already-allowed file.
CREATE_PATH_GATES = {
    # MemoryService.remember — the single memory-write gate; recall / update /
    # forget in the same file never call it.
    "services/memory_service.py": {"remember"},
    # POST /contexts
    "api/routes/contexts.py": {"create_context"},
    # MCP create_context tool
    "mcp_server/tools/context.py": {"handle_create_context"},
    # MCP setup_resource preflight — setting up a resource creates a context
    "mcp_server/tools/resource.py": {"_setup_resource_preflight"},
    # POST /workspaces/{id}/invitations — a pending invite reserves a seat
    "api/routes/invitations.py": {"create_invitation"},
    # InvitationService.accept_invitation — the seat is taken on accept
    "services/invitation_service.py": {"accept_invitation"},
}


def _gate_call_sites() -> dict[str, set[str]]:
    sites: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        enclosing = "<module>"
        for line in path.read_text(encoding="utf-8").splitlines():
            if m := _DEF.match(line):
                enclosing = m.group(1)
            if _GATE_CALL.search(line):
                sites.setdefault(path.relative_to(SRC).as_posix(), set()).add(enclosing)
    return sites


def test_quota_gates_are_only_called_from_create_paths():
    assert _gate_call_sites() == CREATE_PATH_GATES, (
        "a quota gate on a read/update/delete path breaks over-limit workspaces (#1552)"
    )
