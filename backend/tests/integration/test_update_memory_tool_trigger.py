"""Update/patch write paths × ``details.tool_trigger`` (tool guardrails).

The ``test_update_memory_geo.py`` pattern against the real DB + MemoryService:

- the tool_trigger contract fires on update/patch when details are supplied,
  and normalizes defaults back;
- the details replace-all contract drops the marking (round-trip pin);
- an explicit ``tool_trigger: null`` clears it (and the stored JSON has no key);
- an update that does NOT touch details never 422s on a legacy row whose
  stored ``tool_trigger`` shape predates the contract;
- the author gate: the workspace OWNER passes; a plain workspace MEMBER with
  no ContextMember editor row is denied (``AuthorizationError``) both when
  marking a row and when editing an existing guardrail's summary, and a
  ``forget`` by that member is the silent ``deleted_count=0``.

The context is **shared** (``is_private=False``) on purpose: a private
context is creator-only at the RBAC chokepoint (``can_access_memory``), so a
member would get the uniform 404 before the guardrail gate is ever reached
and the gate tests would prove nothing about it.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, User, Workspace, WorkspaceMember
from models.memory import SOURCE_TYPE_MANUAL, Memory
from models.schemas import ForgetRequest, PatchMemoryRequest, UpdateMemoryRequest
from services.memory_service import MemoryService
from utils.exceptions import AuthorizationError

TT = {"tool": "Bash", "match": "gh pr merge"}
TT_NORMALIZED = {"tool": "Bash", "on": "pre", "match": "gh pr merge", "action": "inform"}


@pytest_asyncio.fixture(loop_scope="session")
async def guard_env(db_session):
    owner = f"e2e-ttupd-{uuid.uuid4().hex[:8]}"
    member = f"e2e-ttmem-{uuid.uuid4().hex[:8]}"
    ws_id = uuid.uuid4()
    for uid, name in ((owner, "Guardrail Owner"), (member, "Guardrail Member")):
        db_session.add(
            User(
                email=f"{uid}@example.test",
                user_id=uid,
                name=name,
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
            )
        )
    await db_session.flush()
    db_session.add(
        Workspace(
            id=ws_id,
            name=f"ws-{uuid.uuid4().hex[:8]}",
            plan_name="free",
            owner_user_id=owner,
            daily_api_limit=500,
            weekly_api_limit=2500,
        )
    )
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws_id, user_id=owner, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws_id, user_id=member, role=WorkspaceRole.MEMBER),
        ]
    )
    await db_session.flush()
    ctx = Context(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        name="guard-upd",
        created_by=owner,
        # Shared: workspace members reach the owner's rows; the guardrail gate
        # (context EDITOR) is then the only thing standing between them.
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    def _mem(details):
        return Memory(
            id=uuid.uuid4(),
            user_id=owner,
            workspace_id=ws_id,
            context_id=ctx.id,
            summary="row under update",
            content="row under update",
            type="troubleshooting",
            client="test",
            tags=[],
            source_type=SOURCE_TYPE_MANUAL,
            details=details,
        )

    marked = _mem({"tool_trigger": dict(TT_NORMALIZED), "note": "keep"})
    plain = _mem({"note": "plain"})
    # Legacy shape: a non-object value the contract would reject today. The
    # SERVICE contract must not 422 an update that never touches details.
    legacy = _mem({"tool_trigger": "Bash"})
    db_session.add_all([marked, plain, legacy])
    await db_session.flush()
    return {
        "owner": owner,
        "member": member,
        "ws": ws_id,
        "ctx": ctx.id,
        "marked": marked.id,
        "plain": plain.id,
        "legacy": legacy.id,
    }


def _patched_side_effects():
    return (
        patch("services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()),
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()),
    )


async def _stored_details(db_session, mem_id):
    return (
        await db_session.execute(
            text("SELECT details::text FROM memories WHERE id = :id"), {"id": mem_id}
        )
    ).scalar_one()


@pytest.mark.asyncio(loop_scope="session")
async def test_update_with_invalid_tool_trigger_raises(guard_env, db_session):
    svc = MemoryService(db_session)
    with pytest.raises(ValueError, match="invalid details.tool_trigger: regex_nested_quantifier"):
        await svc.update_memory(
            UpdateMemoryRequest(
                memory_id=guard_env["plain"], details={"tool_trigger": {"tool": "(a+)+"}}
            ),
            user_id=guard_env["owner"],
            current_context_id=guard_env["ctx"],
        )


@pytest.mark.asyncio(loop_scope="session")
async def test_update_marks_a_row_and_normalizes_defaults(guard_env, db_session):
    svc = MemoryService(db_session)
    p1, p2, p3 = _patched_side_effects()
    with p1, p2, p3:
        await svc.update_memory(
            UpdateMemoryRequest(memory_id=guard_env["plain"], details={"tool_trigger": dict(TT)}),
            user_id=guard_env["owner"],
            current_context_id=guard_env["ctx"],
        )
    refreshed = await db_session.get(Memory, guard_env["plain"])
    assert refreshed.details["tool_trigger"] == TT_NORMALIZED
    assert refreshed.is_tool_triggered is True
    # Round-trip pin: the index predicate sees the row.
    seen = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM memories WHERE id = :id "
                "AND (details->'tool_trigger') IS NOT NULL AND deleted_at IS NULL"
            ),
            {"id": guard_env["plain"]},
        )
    ).scalar_one()
    assert seen == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_update_details_replace_all_drops_the_marking(guard_env, db_session):
    svc = MemoryService(db_session)
    p1, p2, p3 = _patched_side_effects()
    with p1, p2, p3:
        await svc.update_memory(
            UpdateMemoryRequest(memory_id=guard_env["marked"], details={"note": "only"}),
            user_id=guard_env["owner"],
            current_context_id=guard_env["ctx"],
        )
    refreshed = await db_session.get(Memory, guard_env["marked"])
    assert "tool_trigger" not in refreshed.details
    assert refreshed.is_tool_triggered is False


@pytest.mark.asyncio(loop_scope="session")
async def test_patch_explicit_null_clears_without_storing_json_null(guard_env, db_session):
    svc = MemoryService(db_session)
    p1, p2, p3 = _patched_side_effects()
    with p1, p2, p3:
        await svc.patch_memory(
            guard_env["marked"],
            PatchMemoryRequest(details={"tool_trigger": None, "note": "keep"}),
            guard_env["owner"],
        )
    stored = await _stored_details(db_session, guard_env["marked"])
    assert "tool_trigger" not in stored  # the key is gone, not a JSON null
    seen = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM memories WHERE id = :id "
                "AND (details->'tool_trigger') IS NOT NULL"
            ),
            {"id": guard_env["marked"]},
        )
    ).scalar_one()
    assert seen == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_update_without_details_skips_validation_on_a_legacy_row(guard_env, db_session):
    svc = MemoryService(db_session)
    p1, p2, p3 = _patched_side_effects()
    with p1, p2, p3:
        response = await svc.update_memory(
            UpdateMemoryRequest(memory_id=guard_env["legacy"], importance=0.9),
            user_id=guard_env["owner"],
            current_context_id=guard_env["ctx"],
        )
    assert response.memory_id == guard_env["legacy"]


@pytest.mark.asyncio(loop_scope="session")
async def test_patch_with_valid_tool_trigger_marks_the_row(guard_env, db_session):
    svc = MemoryService(db_session)
    p1, p2, p3 = _patched_side_effects()
    with p1, p2, p3:
        await svc.patch_memory(
            guard_env["legacy"],
            PatchMemoryRequest(details={"tool_trigger": {"tool": "Edit|Write", "on": "result"}}),
            guard_env["owner"],
        )
    refreshed = await db_session.get(Memory, guard_env["legacy"])
    assert refreshed.details["tool_trigger"] == {
        "tool": "Edit|Write",
        "on": "result",
        "action": "inform",
    }


# --------------------------------------------------------------------------- #
# author gate: a plain workspace MEMBER without a ContextMember editor row
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio(loop_scope="session")
async def test_member_cannot_mark_a_row(guard_env, db_session):
    svc = MemoryService(db_session)
    with pytest.raises(AuthorizationError):
        await svc.update_memory(
            UpdateMemoryRequest(memory_id=guard_env["plain"], details={"tool_trigger": dict(TT)}),
            user_id=guard_env["member"],
            current_context_id=guard_env["ctx"],
        )
    refreshed = await db_session.get(Memory, guard_env["plain"])
    assert "tool_trigger" not in (refreshed.details or {})


@pytest.mark.asyncio(loop_scope="session")
async def test_member_cannot_rewrite_a_guardrail_summary(guard_env, db_session):
    svc = MemoryService(db_session)
    with pytest.raises(AuthorizationError):
        await svc.patch_memory(
            guard_env["marked"],
            PatchMemoryRequest(summary="an instruction the member wants injected"),
            guard_env["member"],
        )
    refreshed = await db_session.get(Memory, guard_env["marked"])
    assert refreshed.summary == "row under update"


@pytest.mark.asyncio(loop_scope="session")
async def test_member_forget_of_a_guardrail_is_a_silent_zero(guard_env, db_session):
    svc = MemoryService(db_session)
    p1, p2, p3 = _patched_side_effects()
    with p1, p2, p3:
        result = await svc.forget(
            ForgetRequest(memory_id=guard_env["marked"]),
            user_id=guard_env["member"],
            current_context_id=guard_env["ctx"],
        )
    assert result.deleted_count == 0
    refreshed = await db_session.get(Memory, guard_env["marked"])
    assert refreshed.deleted_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_member_can_still_edit_a_plain_row(guard_env, db_session):
    """The gate is scoped to guardrails — ordinary member writes are unchanged."""
    svc = MemoryService(db_session)
    p1, p2, p3 = _patched_side_effects()
    with p1, p2, p3:
        response = await svc.update_memory(
            UpdateMemoryRequest(memory_id=guard_env["plain"], importance=0.7),
            user_id=guard_env["member"],
            current_context_id=guard_env["ctx"],
        )
    assert response.memory_id == guard_env["plain"]
