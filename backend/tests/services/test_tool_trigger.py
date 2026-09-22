"""Write-path tests for tool guardrails: ``MemoryService._apply_tool_trigger``
and the author gate (``_require_guardrail_author``).

Mirrors ``test_remember_geo_memory.py``'s mocked-DB pattern — the gate is pure
validation plus one role check that run before any DB write. Pinned here:

* the orthogonal gate fires on ``details.tool_trigger`` presence (any type),
  normalizes defaults back, and maps every violation to the established
  ``ValueError("invalid details.tool_trigger: <code>: ...")`` signal;
* caller-supplied details only — an update that does not touch ``details`` on
  a plain row never validates (legacy rows cannot 422);
* the author gate: adding, changing or REMOVING ``tool_trigger``, and any edit
  or forget of a row that already carries one, requires context EDITOR or
  above (``PermissionService.check_context_write``); an agent credential can
  never write a guardrail (``tool_trigger_requires_user_credential``).

The grammar itself is pinned in ``tests/utils/test_tool_trigger_regex.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from auth.agent_scope import AgentScope, set_agent_scope
from models.schemas import (
    ForgetRequest,
    PatchMemoryRequest,
    RememberRequest,
    UpdateMemoryRequest,
)
from services.memory_service import MemoryService
from utils.exceptions import AuthorizationError

TT = {"tool": "Bash|PowerShell", "match": r"gh pr merge\b.*--delete-branch", "action": "block"}


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def service(mock_db):
    svc = MemoryService(mock_db)
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(
        return_value=(ctx, str(ctx.workspace_id), str(ctx.id))
    )
    svc.memory_repo = MagicMock()
    svc.memory_repo.create = AsyncMock()
    svc.memory_repo.update = AsyncMock()
    svc._mock_context = ctx
    return svc


@pytest.fixture
def perm():
    """``PermissionService`` double: membership passes, the role gate is a spy."""
    ps = MagicMock()
    ps.can_access_memory = AsyncMock(return_value=True)
    ps.check_context_write = AsyncMock(return_value=MagicMock())
    with patch("services.permission_service.PermissionService", return_value=ps):
        yield ps


@pytest.fixture(autouse=True)
def _no_agent_scope():
    set_agent_scope(None)
    yield
    set_agent_scope(None)


def _req(details, mtype="troubleshooting"):
    return RememberRequest(
        summary="Use --ff-only after fetch; the worktree must be removed first",
        content="body",
        details=details,
        type=mtype,
    )


async def _remember(service, req):
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch(
            "services.quota_service.QuotaService",
            return_value=MagicMock(
                check_memory_quota=AsyncMock(return_value=(True, None)),
                check_memories_per_day=AsyncMock(return_value=(True, None)),
            ),
        ),
    ):
        return await service.remember(
            req,
            user_id="test_user",
            client="test",
            current_context_id=service._mock_context.id,
            current_workspace_id=None,
        )


# ------------------------------------------------------------ _apply_tool_trigger


def test_apply_tool_trigger_passthrough_without_key():
    details = {"foo": "bar"}
    assert MemoryService._apply_tool_trigger(details) is details
    assert MemoryService._apply_tool_trigger(None) is None


def test_apply_tool_trigger_normalizes_defaults_in_order():
    out = MemoryService._apply_tool_trigger({"tool_trigger": {"tool": "Bash"}})
    assert out == {"tool_trigger": {"tool": "Bash", "on": "pre", "action": "inform"}}
    assert list(out["tool_trigger"]) == ["tool", "on", "action"]


def test_apply_tool_trigger_explicit_null_removes_key():
    assert MemoryService._apply_tool_trigger({"tool_trigger": None, "k": 1}) == {"k": 1}


def test_apply_tool_trigger_maps_to_value_error_with_stable_prefix_and_code():
    with pytest.raises(
        ValueError, match=r"^invalid details\.tool_trigger: regex_nested_quantifier: "
    ):
        MemoryService._apply_tool_trigger({"tool_trigger": {"tool": "(a+)+"}})
    with pytest.raises(ValueError, match="block_requires_match"):
        MemoryService._apply_tool_trigger({"tool_trigger": {"tool": "Bash", "action": "block"}})


# ------------------------------------------------------------ _touches_tool_trigger


@pytest.mark.parametrize(
    ("existing", "new", "supplied", "expected"),
    [
        (None, None, False, False),
        ({"x": 1}, None, False, False),
        ({"x": 1}, {"x": 2}, True, False),  # plain row, details without the key
        ({"x": 1}, {"tool_trigger": TT}, True, True),  # adding
        ({"x": 1}, {"tool_trigger": None}, True, True),  # explicit null on a plain row still audits
        ({"tool_trigger": TT}, None, False, True),  # any edit of a guardrail row
        ({"tool_trigger": TT}, {"x": 1}, True, True),  # wholesale replace drops it
        ({"tool_trigger": None}, {"x": 1}, True, False),  # legacy null is "not a guardrail"
        ("legacy-string", {"x": 1}, True, False),
    ],
)
def test_touches_tool_trigger(existing, new, supplied, expected):
    assert MemoryService._touches_tool_trigger(existing, new, details_supplied=supplied) is expected


# -------------------------------------------------------------------- remember


@pytest.mark.asyncio
async def test_remember_normalizes_tool_trigger_and_requires_editor(service, perm):
    req = _req({"tool_trigger": {"tool": "Bash", "match": "gh pr merge"}})
    result = await _remember(service, req)
    assert result.memory_id is not None
    assert req.details["tool_trigger"] == {
        "tool": "Bash",
        "on": "pre",
        "match": "gh pr merge",
        "action": "inform",
    }
    perm.check_context_write.assert_awaited_once_with("test_user", service._mock_context.id)


@pytest.mark.asyncio
async def test_remember_tool_trigger_is_type_agnostic(service, perm):
    req = _req({"tool_trigger": TT}, mtype="note")
    assert (await _remember(service, req)).memory_id is not None


@pytest.mark.asyncio
async def test_remember_without_tool_trigger_skips_the_role_gate(service, perm):
    req = _req({"other": 1})
    await _remember(service, req)
    perm.check_context_write.assert_not_awaited()
    assert "tool_trigger" not in req.details


@pytest.mark.asyncio
async def test_remember_explicit_null_is_not_a_guardrail_write(service, perm):
    req = _req({"tool_trigger": None, "keep": 1})
    await _remember(service, req)
    perm.check_context_write.assert_not_awaited()
    assert req.details == {"keep": 1}


@pytest.mark.asyncio
async def test_remember_invalid_tool_trigger_raises_before_any_write(service, perm):
    req = _req({"tool_trigger": {"tool": "Bash", "match": "(a+)+"}})
    with pytest.raises(ValueError, match="invalid details.tool_trigger: regex_nested_quantifier"):
        await _remember(service, req)
    service.memory_repo.create.assert_not_awaited()
    perm.check_context_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_denies_non_editor_with_authorization_error(service, perm):
    perm.check_context_write.side_effect = AuthorizationError()
    req = _req({"tool_trigger": TT})
    with pytest.raises(AuthorizationError):
        await _remember(service, req)
    service.memory_repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_remember_rejects_tool_trigger_from_an_agent_credential(service, perm):
    set_agent_scope(AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=uuid4()))
    req = _req({"tool_trigger": TT})
    with pytest.raises(ValueError, match="tool_trigger_requires_user_credential"):
        await _remember(service, req)
    perm.check_context_write.assert_not_awaited()
    service.memory_repo.create.assert_not_awaited()


# ------------------------------------------------------------- _update_in_place


def _memory(details, **overrides):
    m = MagicMock()
    m.id = uuid4()
    m.user_id = "test_user"
    m.workspace_id = uuid4()
    m.context_id = uuid4()
    m.summary = "stored summary"
    m.context_summary = None
    m.content = "stored content"
    m.details = details
    m.type = "note"
    m.importance = 0.5
    m.tags = []
    m.context = None
    m.scope = "persistent"
    m.delivery_mode = "on_recall"
    m.is_pinned = False
    m.is_tool_triggered = isinstance(details, dict) and details.get("tool_trigger") is not None
    m.updated_at = None
    m.supersede_candidate = None
    m.source_type = "manual"
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


def _prime_update(service, memory):
    service._update_load_authorized = AsyncMock(return_value=memory)
    service._update_sync_qdrant_payload = AsyncMock()
    service._lint_write = AsyncMock(return_value=[])


async def _update(service, request):
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await service._update_in_place(request, user_id="test_user")


@pytest.mark.asyncio
async def test_update_with_tool_trigger_normalizes_and_requires_editor(service, perm):
    memory = _memory({"x": 1})
    _prime_update(service, memory)
    await _update(service, UpdateMemoryRequest(memory_id=memory.id, details={"tool_trigger": TT}))
    assert memory.details["tool_trigger"]["on"] == "pre"
    perm.check_context_write.assert_awaited_once_with("test_user", memory.context_id)


@pytest.mark.asyncio
async def test_update_of_a_guardrail_row_requires_editor_even_without_details(service, perm):
    memory = _memory({"tool_trigger": TT})
    _prime_update(service, memory)
    await _update(service, UpdateMemoryRequest(memory_id=memory.id, importance=0.9))
    perm.check_context_write.assert_awaited_once_with("test_user", memory.context_id)


@pytest.mark.asyncio
async def test_update_that_drops_tool_trigger_requires_editor(service, perm):
    memory = _memory({"tool_trigger": TT})
    _prime_update(service, memory)
    await _update(service, UpdateMemoryRequest(memory_id=memory.id, details={"plain": True}))
    perm.check_context_write.assert_awaited_once()
    assert "tool_trigger" not in memory.details


@pytest.mark.asyncio
async def test_update_of_a_plain_row_without_details_skips_validation_and_gate(service, perm):
    memory = _memory({"tool_trigger": "legacy garbage"})  # a string, not an object
    memory.is_tool_triggered = True  # what the model property would say for a non-None value
    memory.details = {"location": "Tokyo"}  # untouched legacy details of any shape
    memory.is_tool_triggered = False
    _prime_update(service, memory)
    await _update(service, UpdateMemoryRequest(memory_id=memory.id, importance=0.9))
    perm.check_context_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_invalid_tool_trigger_raises(service, perm):
    memory = _memory({"x": 1})
    _prime_update(service, memory)
    with pytest.raises(ValueError, match="invalid details.tool_trigger: on_invalid"):
        await _update(
            service,
            UpdateMemoryRequest(
                memory_id=memory.id, details={"tool_trigger": {"tool": "B", "on": "x"}}
            ),
        )
    service.memory_repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_non_editor_is_denied_before_fields_are_applied(service, perm):
    perm.check_context_write.side_effect = AuthorizationError()
    memory = _memory({"tool_trigger": TT})
    _prime_update(service, memory)
    with pytest.raises(AuthorizationError):
        await _update(service, UpdateMemoryRequest(memory_id=memory.id, summary="rewritten text"))
    assert memory.summary == "stored summary"
    service.db.commit.assert_not_awaited()


# ---------------------------------------------------------------- patch_memory


def _prime_patch(service, memory):
    service._patch_load_authorized = AsyncMock(return_value=memory)
    service._patch_sync_qdrant_payload = AsyncMock()
    service._patch_build_response = AsyncMock(return_value=MagicMock())


async def _patch(service, memory, request):
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await service.patch_memory(memory.id, request, "test_user")


@pytest.mark.asyncio
async def test_patch_with_tool_trigger_requires_editor(service, perm):
    memory = _memory({"x": 1})
    _prime_patch(service, memory)
    await _patch(service, memory, PatchMemoryRequest(details={"tool_trigger": TT}))
    perm.check_context_write.assert_awaited_once_with("test_user", memory.context_id)
    assert memory.details["tool_trigger"]["action"] == "block"


@pytest.mark.asyncio
async def test_patch_summary_of_a_guardrail_row_requires_editor(service, perm):
    memory = _memory({"tool_trigger": TT})
    _prime_patch(service, memory)
    await _patch(service, memory, PatchMemoryRequest(summary="a rewritten guardrail text"))
    perm.check_context_write.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_of_a_plain_row_skips_the_gate(service, perm):
    memory = _memory({"x": 1})
    _prime_patch(service, memory)
    await _patch(service, memory, PatchMemoryRequest(importance=0.7))
    perm.check_context_write.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_explicit_null_details_on_a_guardrail_row_requires_editor(service, perm):
    memory = _memory({"tool_trigger": TT})
    _prime_patch(service, memory)
    await _patch(service, memory, PatchMemoryRequest(details=None))
    perm.check_context_write.assert_awaited_once()


# ---------------------------------------------------------------------- forget


async def _forget(service, memory):
    with (
        patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()),
        patch("services.memory_service.resolve_collection_name", new=AsyncMock(return_value="c")),
        patch("repositories.neural_edge.NeuralEdgeRepository") as edge_repo,
        patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
    ):
        edge_repo.return_value.delete_node_edges = AsyncMock(return_value=0)
        return await service.forget(
            ForgetRequest(memory_id=memory.id),
            user_id="test_user",
            current_context_id=service._mock_context.id,
        )


@pytest.mark.asyncio
async def test_forget_of_a_guardrail_row_by_non_editor_is_a_silent_zero(service, perm):
    """forget keeps its contract: a target the caller may not delete is skipped
    (deleted_count=0), never a 403 — so the deny leaks nothing."""
    perm.check_context_write.side_effect = AuthorizationError()
    memory = _memory({"tool_trigger": TT})
    service.memory_repo.get = AsyncMock(return_value=memory)
    result = await _forget(service, memory)
    assert result.deleted_count == 0
    assert result.memory_ids == []
    service.memory_repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_forget_of_a_guardrail_row_by_editor_proceeds(service, perm):
    memory = _memory({"tool_trigger": TT})
    service.memory_repo.get = AsyncMock(return_value=memory)
    result = await _forget(service, memory)
    assert result.deleted_count == 1
    perm.check_context_write.assert_awaited_once_with("test_user", memory.context_id)


@pytest.mark.asyncio
async def test_forget_of_a_plain_row_skips_the_gate(service, perm):
    memory = _memory({"x": 1})
    service.memory_repo.get = AsyncMock(return_value=memory)
    result = await _forget(service, memory)
    assert result.deleted_count == 1
    perm.check_context_write.assert_not_awaited()


# ------------------------------------------------ forget(query=...) sweep


async def _forget_by_query(service, memories):
    """Run the by-query branch over ``memories`` as the recall hits."""
    by_id = {m.id: m for m in memories}
    service.memory_repo.get = AsyncMock(side_effect=lambda mid: by_id.get(mid))
    hits = []
    for m in memories:
        hit = MagicMock()
        hit.memory_id = m.id
        hits.append(hit)
    search = MagicMock()
    search.results = hits
    search.degraded = False
    service.recall = AsyncMock(return_value=search)
    with (
        patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()),
        patch("services.memory_service.resolve_collection_name", new=AsyncMock(return_value="c")),
        patch("repositories.neural_edge.NeuralEdgeRepository") as edge_repo,
        patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
    ):
        edge_repo.return_value.delete_node_edges = AsyncMock(return_value=0)
        return await service.forget(
            ForgetRequest(query="gh pr merge", k=10),
            user_id="test_user",
            current_context_id=service._mock_context.id,
        )


@pytest.mark.asyncio
async def test_forget_by_query_skips_guardrail_rows_for_non_editor(service, perm):
    """A query sweep is held to the same gate as a by-id forget: a member who
    cannot delete a guardrail one by one cannot sweep it away either. The
    plain rows in the same sweep are still deleted, and nothing is a 403."""
    perm.check_context_write.side_effect = AuthorizationError()
    guardrail = _memory({"tool_trigger": TT})
    plain = _memory({"x": 1})
    result = await _forget_by_query(service, [guardrail, plain])
    assert result.deleted_count == 1
    assert result.memory_ids == [plain.id]
    updated = [call.args[0] for call in service.memory_repo.update.await_args_list]
    assert updated == [plain.id]
    perm.check_context_write.assert_awaited_once_with("test_user", guardrail.context_id)


@pytest.mark.asyncio
async def test_forget_by_query_deletes_guardrail_rows_for_editor(service, perm):
    guardrail = _memory({"tool_trigger": TT})
    plain = _memory({"x": 1})
    result = await _forget_by_query(service, [guardrail, plain])
    assert result.deleted_count == 2
    assert result.memory_ids == [guardrail.id, plain.id]
    # One role check per guardrail row; plain rows never touch the gate.
    perm.check_context_write.assert_awaited_once_with("test_user", guardrail.context_id)


@pytest.mark.asyncio
async def test_forget_by_query_rejects_guardrail_rows_for_an_agent_credential(service, perm):
    set_agent_scope(
        AgentScope(
            agent_id=uuid4(),
            enforcement_mode="enforce",
            workspace_id=service._mock_context.workspace_id,
        )
    )
    guardrail = _memory({"tool_trigger": TT})
    result = await _forget_by_query(service, [guardrail])
    assert result.deleted_count == 0
    perm.check_context_write.assert_not_awaited()
