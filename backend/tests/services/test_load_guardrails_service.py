"""Tests for MemoryService.load_guardrails — the deterministic guardrail-set read.

Mirrors ``test_load_pinned_service.py``: the two repo queries are mocked, so
this pins the SERVICE contract — two independently capped lanes (pinned never
crowds out tool-triggered), the pinned half == trusted ``list_pinned``, item
shape (L1 only on the tool-triggered lane, never L3), per-lane and top-level
truncation reporting, the per-credential ``version`` hash, the binding filter
and the audit emission. The SQL (trust gate, order, index predicate) is pinned
against a real DB in ``tests/integration/test_load_guardrails_repo.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import GuardrailItem, LoadGuardrailsResponse
from services.memory_service import MemoryService
from utils.datetime import utcnow
from utils.tool_trigger import GUARDRAIL_FORMAT

TT = {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge", "action": "inform"}


@pytest.fixture
def service():
    svc = MemoryService(MagicMock())
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(
        return_value=(ctx, str(ctx.workspace_id), str(ctx.id))
    )
    svc.memory_repo = MagicMock()
    svc.memory_repo.list_pinned = AsyncMock(return_value=([], 0))
    svc.memory_repo.list_tool_triggered = AsyncMock(return_value=([], 0))
    svc._mock_context = ctx
    return svc


@pytest.fixture
def emit():
    with patch(
        "services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()
    ) as m:
        yield m


def _pinned_row(**o):
    m = MagicMock()
    m.id = o.get("id", uuid4())
    m.summary = o.get("summary", "agent goal")
    m.context_summary = o.get("context_summary", "why it matters")
    m.content = "FULL CONTENT — must never appear"
    m.type = "note"
    m.importance = o.get("importance", 0.9)
    m.delivery_mode = "always"
    m.created_at = utcnow()
    m.updated_at = o.get("updated_at", None)
    m.source_type = "manual"
    m.user_id = o.get("user_id", "u1")
    return m


def _tool_row(**o):
    m = MagicMock(spec=[])  # no context_summary / content attributes at all
    m.id = o.get("id", uuid4())
    m.summary = o.get("summary", "use --ff-only after fetch")
    m.type = "troubleshooting"
    m.importance = o.get("importance", 0.8)
    m.delivery_mode = o.get("delivery_mode", "on_recall")
    m.created_at = utcnow()
    m.updated_at = o.get("updated_at", None)
    m.source_type = "manual"
    m.user_id = o.get("user_id", "u1")
    m.tool_trigger = o.get("tool_trigger", dict(TT))
    return m


async def _call(service, cap=None, user_id="u1"):
    return await service.load_guardrails(
        user_id=user_id,
        current_context_id=service._mock_context.id,
        current_workspace_id=service._mock_context.workspace_id,
        cap=cap,
    )


# ------------------------------------------------------------------- two lanes


@pytest.mark.asyncio
async def test_pinned_lane_is_trusted_list_pinned_with_pinned_cap(service, emit):
    service.memory_repo.list_pinned = AsyncMock(return_value=([_pinned_row()], 1))
    await _call(service)
    service.memory_repo.list_pinned.assert_awaited_once_with(
        service._mock_context.workspace_id, service._mock_context.id, 100, trusted_only=True
    )
    # No trusted_only flag exists on the tool lane — the gate is hard-wired.
    service.memory_repo.list_tool_triggered.assert_awaited_once_with(
        service._mock_context.workspace_id, service._mock_context.id, 50
    )


@pytest.mark.asyncio
async def test_pinned_never_crowds_out_tool_triggered(service, emit):
    """60 pinned rows at importance 1.0 and 3 guardrails at 0.5 with cap=50:
    all 3 guardrails are served — the lanes do not share a budget."""
    pinned = [_pinned_row(importance=1.0) for _ in range(60)]
    tools = [_tool_row(importance=0.5) for _ in range(3)]
    service.memory_repo.list_pinned = AsyncMock(return_value=(pinned, 60))
    service.memory_repo.list_tool_triggered = AsyncMock(return_value=(tools, 3))
    result = await _call(service, cap=50)
    assert len(result.tool_triggered) == 3
    assert result.tool_triggered_truncated is False
    assert len(result.pinned) == 60


@pytest.mark.asyncio
async def test_request_cap_applies_to_the_tool_lane_only(service, emit):
    await _call(service, cap=7)
    assert service.memory_repo.list_tool_triggered.await_args.args[2] == 7
    assert service.memory_repo.list_pinned.await_args.args[2] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [(0, 1), (-5, 1), (5000, 1000), ("50", 50)],
)
async def test_tool_cap_is_coerced_and_clamped(service, emit, raw, expected):
    result = await _call(service, cap=raw)
    assert service.memory_repo.list_tool_triggered.await_args.args[2] == expected
    assert result.cap == expected


@pytest.mark.asyncio
async def test_non_integer_cap_is_rejected(service, emit):
    with pytest.raises(ValueError):
        await _call(service, cap="abc")


@pytest.mark.asyncio
async def test_a_row_in_both_lanes_appears_in_both_lists(service, emit):
    shared = uuid4()
    service.memory_repo.list_pinned = AsyncMock(return_value=([_pinned_row(id=shared)], 1))
    service.memory_repo.list_tool_triggered = AsyncMock(
        return_value=([_tool_row(id=shared, delivery_mode="always")], 1)
    )
    result = await _call(service)
    assert [i.memory_id for i in result.pinned] == [shared]
    assert [i.memory_id for i in result.tool_triggered] == [shared]
    # The pinned entry never carries the trigger; the tool entry does.
    assert result.pinned[0].tool_trigger is None
    assert result.tool_triggered[0].tool_trigger == TT
    assert result.total_available == 2  # sum of the two lane counts, by contract


# ------------------------------------------------------------------ item shape


@pytest.mark.asyncio
async def test_items_carry_provenance_and_never_l3(service, emit):
    now = utcnow()
    service.memory_repo.list_pinned = AsyncMock(
        return_value=([_pinned_row(user_id="u1", updated_at=now)], 1)
    )
    service.memory_repo.list_tool_triggered = AsyncMock(
        return_value=([_tool_row(user_id="someone-else")], 1)
    )
    result = await _call(service, user_id="u1")
    assert isinstance(result, LoadGuardrailsResponse)
    assert result.format == GUARDRAIL_FORMAT == 1
    p, t = result.pinned[0], result.tool_triggered[0]
    assert isinstance(p, GuardrailItem) and isinstance(t, GuardrailItem)
    assert not hasattr(p, "content") and not hasattr(t, "content")
    assert not hasattr(p, "details") and not hasattr(t, "details")
    # Pinned keeps L2; tool-triggered is L1 only.
    assert p.context_summary == "why it matters"
    assert t.context_summary is None
    assert p.authored_by_caller is True
    assert t.authored_by_caller is False
    assert p.source_type == t.source_type == "manual"
    assert p.updated_at == now
    assert t.updated_at == t.created_at  # nullable column falls back


@pytest.mark.asyncio
async def test_tool_trigger_json_text_from_the_driver_is_decoded(service, emit):
    service.memory_repo.list_tool_triggered = AsyncMock(
        return_value=(
            [_tool_row(tool_trigger='{"tool": "Bash", "on": "pre", "action": "inform"}')],
            1,
        )
    )
    result = await _call(service)
    assert result.tool_triggered[0].tool_trigger == {
        "tool": "Bash",
        "on": "pre",
        "action": "inform",
    }


@pytest.mark.asyncio
async def test_lists_keep_repo_order(service, emit):
    ids = [uuid4() for _ in range(3)]
    service.memory_repo.list_tool_triggered = AsyncMock(
        return_value=([_tool_row(id=i) for i in ids], 3)
    )
    result = await _call(service)
    assert [i.memory_id for i in result.tool_triggered] == ids


# ------------------------------------------------------------- truncation flags


@pytest.mark.asyncio
async def test_per_lane_and_top_level_truncation(service, emit):
    service.memory_repo.list_pinned = AsyncMock(return_value=([_pinned_row()], 1))
    service.memory_repo.list_tool_triggered = AsyncMock(
        return_value=([_tool_row() for _ in range(2)], 9)
    )
    result = await _call(service, cap=2)
    assert result.pinned_truncated is False
    assert result.pinned_total_available == 1
    assert result.pinned_cap == 100
    assert result.tool_triggered_truncated is True
    assert result.tool_triggered_total_available == 9
    assert result.cap == 2
    assert result.truncated is True
    assert result.total_available == 10


@pytest.mark.asyncio
async def test_not_truncated_when_both_lanes_fit(service, emit):
    service.memory_repo.list_pinned = AsyncMock(return_value=([_pinned_row()], 1))
    service.memory_repo.list_tool_triggered = AsyncMock(return_value=([_tool_row()], 1))
    result = await _call(service)
    assert result.truncated is False
    assert result.total_available == 2


# ------------------------------------------------------------------- version


@pytest.mark.asyncio
async def test_version_is_16_hex_stable_and_changes_on_summary_edit(service, emit):
    row = _tool_row(summary="before")
    service.memory_repo.list_tool_triggered = AsyncMock(return_value=([row], 1))
    v1 = (await _call(service)).version
    v1_again = (await _call(service)).version
    assert len(v1) == 16 and int(v1, 16) >= 0
    assert v1 == v1_again
    row.summary = "after"
    assert (await _call(service)).version != v1


@pytest.mark.asyncio
async def test_version_is_computed_after_the_binding_filter(service, emit):
    """Per-credential: a row the binding filter drops does not enter the hash."""
    a, b = _tool_row(summary="a"), _tool_row(summary="b")
    service.memory_repo.list_tool_triggered = AsyncMock(return_value=([a, b], 2))
    full = (await _call(service)).version

    async def drop_b(db, rows, *, operation, user_id):
        return [r for r in rows if r.summary != "b"], 1

    with patch("services.agent_binding_service.filter_memory_rows_by_binding", new=drop_b):
        filtered = await _call(service)
    assert filtered.version != full
    assert [i.summary for i in filtered.tool_triggered] == ["a"]
    assert filtered.tool_triggered_total_available == 2  # repo count, pre-binding


# ------------------------------------------------------------ guards / audit


@pytest.mark.asyncio
async def test_isolation_gate_threads_the_load_guardrails_operation(service, emit):
    await _call(service)
    kwargs = service._get_context_isolation_params.await_args.kwargs
    assert kwargs["operation"] == "load_guardrails"


@pytest.mark.asyncio
async def test_binding_filter_is_called_per_lane_with_operation(service, emit):
    service.memory_repo.list_pinned = AsyncMock(return_value=([_pinned_row()], 1))
    service.memory_repo.list_tool_triggered = AsyncMock(return_value=([_tool_row()], 1))
    with patch(
        "services.agent_binding_service.filter_memory_rows_by_binding",
        new=AsyncMock(side_effect=lambda db, rows, *, operation, user_id: (rows, 0)),
    ) as flt:
        await _call(service)
    assert flt.await_count == 2
    assert {c.kwargs["operation"] for c in flt.await_args_list} == {"load_guardrails"}


@pytest.mark.asyncio
async def test_audit_event_is_emitted_with_operation_and_count(service, emit):
    service.memory_repo.list_pinned = AsyncMock(return_value=([_pinned_row()], 1))
    service.memory_repo.list_tool_triggered = AsyncMock(
        return_value=([_tool_row(), _tool_row()], 2)
    )
    await _call(service)
    emit.assert_awaited_once()
    kw = emit.await_args.kwargs
    assert kw["operation"] == "load_guardrails"
    assert kw["outcome"] == "success"
    assert kw["result_count"] == 3
    assert kw["extra_metadata"] is None


@pytest.mark.asyncio
async def test_requires_a_context(service, emit):
    service._get_context_isolation_params = AsyncMock(return_value=(None, None, None))
    with pytest.raises(ValueError, match="requires current_context_id"):
        await _call(service)
