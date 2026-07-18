"""Unit tests for the HOW-MUCH measurement-lane MCP handlers (Issue #1333).

Pins the access-control composition (IDOR guard + write gate + #1275 binding
gate) and the arg/dispatch contract of ``handle_record_measurement`` /
``handle_recall_series`` without a database — the service is mocked. Mirrors
tests/mcp_server/test_state_tools.py. DB-backed behaviour is covered in
tests/services/test_measurement_service.py and the migration test.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import _ContextNotFoundError, _error_response
from mcp_server.tools.measurement import handle_recall_series, handle_record_measurement

CTX = str(uuid4())


def _payload(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def _enter(stack, *, service, resolve_raises=None, viewer_error=None, binding_permits=True):
    """Enter the standard patch set and return the mocked service."""

    async def gen():
        yield AsyncMock()

    resolve = AsyncMock(side_effect=resolve_raises) if resolve_raises else AsyncMock()
    stack.enter_context(patch("db.base.get_db", new=gen))
    stack.enter_context(
        patch("mcp_server.tools.measurement._resolve_context_for_read", new=resolve)
    )
    stack.enter_context(
        patch(
            "mcp_server.tools.measurement._check_viewer_permission",
            new=AsyncMock(return_value=viewer_error),
        )
    )
    # #1275: record_measurement applies the WRITE binding gate. Default
    # permits (no agent scope); tests pass binding_permits=False to simulate
    # a write-denied agent binding.
    stack.enter_context(
        patch(
            "services.agent_binding_service.agent_binding_permits",
            new=AsyncMock(return_value=binding_permits),
        )
    )
    stack.enter_context(
        patch("services.measurement_service.MeasurementService", return_value=service)
    )
    return service


def _row(**overrides):
    row = MagicMock()
    row.id = overrides.get("id", uuid4())
    row.metric = overrides.get("metric", "weight_kg")
    row.measured_at = overrides.get("measured_at", datetime(2026, 7, 17, 8, 0))
    row.value = overrides.get("value", Decimal("72.5"))
    row.unit = overrides.get("unit", "kg")
    return row


class TestRecordMeasurement:
    @pytest.mark.asyncio
    async def test_missing_fields_returns_error(self):
        for args in (
            {"context_id": CTX, "metric": "weight_kg"},
            {"context_id": CTX, "value": 1.0},
        ):
            result = await handle_record_measurement(args=args, user_id="u", workspace_id=uuid4())
            assert _payload(result)["error"] == "missing_fields"

    @pytest.mark.asyncio
    async def test_missing_context_id_returns_error(self):
        svc = MagicMock(record=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_record_measurement(
                args={"metric": "weight_kg", "value": 1.0}, user_id="u", workspace_id=uuid4()
            )
        assert _payload(result)["error"] == "missing_fields"
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_context_id_returns_error(self):
        svc = MagicMock(record=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_record_measurement(
                args={"context_id": "not-a-uuid", "metric": "weight_kg", "value": 1.0},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "invalid_context_id_format"
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_not_found_short_circuits(self):
        svc = MagicMock(record=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc, resolve_raises=_ContextNotFoundError(uuid4(), "nope"))
            result = await handle_record_measurement(
                args={"context_id": CTX, "metric": "weight_kg", "value": 1.0},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "context_not_found"
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_bad_metric_is_rejected(self):
        svc = MagicMock(record=AsyncMock())
        for bad in ("", "m" * 65, 42):
            with ExitStack() as stack:
                _enter(stack, service=svc)
                result = await handle_record_measurement(
                    args={"context_id": CTX, "metric": bad, "value": 1.0},
                    user_id="u",
                    workspace_id=uuid4(),
                )
            assert _payload(result)["error"] == "validation_error", bad
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_number_value_is_rejected(self):
        # "72.5" is NOT here: numeric strings coerce (#1322 parity — some
        # MCP clients stringify scalars and this handler has no pydantic
        # model to do the coercion).
        svc = MagicMock(record=AsyncMock())
        for bad in (True, "abc", [1], {"v": 1}):
            with ExitStack() as stack:
                _enter(stack, service=svc)
                result = await handle_record_measurement(
                    args={"context_id": CTX, "metric": "weight_kg", "value": bad},
                    user_id="u",
                    workspace_id=uuid4(),
                )
            assert _payload(result)["error"] == "validation_error", bad
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_unparseable_measured_at_is_rejected(self):
        svc = MagicMock(record=AsyncMock())
        for bad in ("yesterday", 1234, ""):
            with ExitStack() as stack:
                _enter(stack, service=svc)
                result = await handle_record_measurement(
                    args={
                        "context_id": CTX,
                        "metric": "weight_kg",
                        "value": 1.0,
                        "measured_at": bad,
                    },
                    user_id="u",
                    workspace_id=uuid4(),
                )
            assert _payload(result)["error"] == "validation_error", bad
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_viewer_is_blocked_from_writing(self):
        svc = MagicMock(record=AsyncMock())
        blocked = _error_response("permission_denied", "viewers cannot write")
        with ExitStack() as stack:
            _enter(stack, service=svc, viewer_error=blocked)
            result = await handle_record_measurement(
                args={"context_id": CTX, "metric": "weight_kg", "value": 1.0},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "permission_denied"
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_viewer_gate_uses_context_workspace_when_workspace_id_none(self):
        # Regression precedent (#889): with workspace_id=None (OAuth2/session
        # MCP auth), the write gate must still fire against the resolved
        # context's workspace.
        svc = MagicMock(record=AsyncMock())
        ctx_ws = uuid4()
        blocked = _error_response("permission_denied", "viewers cannot write")
        viewer = AsyncMock(return_value=blocked)

        async def gen():
            yield AsyncMock()

        with ExitStack() as stack:
            resolved = AsyncMock(return_value=MagicMock(workspace_id=ctx_ws))
            stack.enter_context(patch("db.base.get_db", new=gen))
            stack.enter_context(
                patch("mcp_server.tools.measurement._resolve_context_for_read", new=resolved)
            )
            stack.enter_context(
                patch("mcp_server.tools.measurement._check_viewer_permission", new=viewer)
            )
            stack.enter_context(
                patch("services.measurement_service.MeasurementService", return_value=svc)
            )
            result = await handle_record_measurement(
                args={"context_id": CTX, "metric": "weight_kg", "value": 1.0},
                user_id="u",
                workspace_id=None,
            )
        assert _payload(result)["error"] == "permission_denied"
        assert viewer.await_args.args[2] == ctx_ws
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_denied_agent_binding_blocks_record(self):
        """#1275: record_measurement is a WRITE — a read-only-bound agent must
        be blocked with the uniform context_not_found."""
        svc = MagicMock(record=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc, binding_permits=False)
            result = await handle_record_measurement(
                args={"context_id": CTX, "metric": "weight_kg", "value": 1.0},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "context_not_found"
        svc.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_service_validation_error_maps_to_validation_error(self):
        svc = MagicMock(record=AsyncMock(side_effect=ValueError("value must be finite")))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_record_measurement(
                args={"context_id": CTX, "metric": "weight_kg", "value": 1.0},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "validation_error"

    @pytest.mark.asyncio
    async def test_happy_path_returns_recorded_measurement(self):
        row = _row()
        svc = MagicMock(record=AsyncMock(return_value=row))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_record_measurement(
                args={
                    "context_id": CTX,
                    "metric": "weight_kg",
                    "value": 72.5,
                    "unit": "kg",
                    "measured_at": "2026-07-17T08:00:00Z",
                    "details": {"device": "scale"},
                },
                user_id="u",
                workspace_id=uuid4(),
            )
        body = _payload(result)
        assert body["status"] == "success"
        assert body["measurement_id"] == str(row.id)
        assert body["metric"] == "weight_kg"
        assert body["value"] == 72.5
        assert body["unit"] == "kg"
        assert body["measured_at"].endswith("Z")
        svc.record.assert_awaited_once()
        kwargs = svc.record.call_args.kwargs
        assert kwargs["unit"] == "kg"
        assert kwargs["details"] == {"device": "scale"}
        assert kwargs["measured_at"] is not None


class TestRecallSeries:
    @pytest.mark.asyncio
    async def test_missing_metric_returns_error(self):
        result = await handle_recall_series(
            args={"context_id": CTX}, user_id="u", workspace_id=uuid4()
        )
        assert _payload(result)["error"] == "missing_fields"

    @pytest.mark.asyncio
    async def test_missing_context_id_returns_error(self):
        svc = MagicMock(recall_series=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_recall_series(
                args={"metric": "weight_kg"}, user_id="u", workspace_id=uuid4()
            )
        assert _payload(result)["error"] == "missing_fields"
        svc.recall_series.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_not_found_short_circuits(self):
        svc = MagicMock(recall_series=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc, resolve_raises=_ContextNotFoundError(uuid4(), "nope"))
            result = await handle_recall_series(
                args={"context_id": CTX, "metric": "weight_kg"},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "context_not_found"
        svc.recall_series.assert_not_called()

    @pytest.mark.asyncio
    async def test_service_value_error_maps_to_validation_error(self):
        svc = MagicMock(recall_series=AsyncMock(side_effect=ValueError("Invalid period")))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_recall_series(
                args={"context_id": CTX, "metric": "weight_kg", "period": "hour"},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "validation_error"

    @pytest.mark.asyncio
    async def test_unparseable_start_is_rejected(self):
        svc = MagicMock(recall_series=AsyncMock())
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_recall_series(
                args={"context_id": CTX, "metric": "weight_kg", "start": "last tuesday"},
                user_id="u",
                workspace_id=uuid4(),
            )
        assert _payload(result)["error"] == "validation_error"
        svc.recall_series.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_serializes_buckets_as_utc_iso(self):
        svc = MagicMock(
            recall_series=AsyncMock(
                return_value=[
                    {"bucket": datetime(2026, 7, 1), "value": 71.2, "count": 3},
                    {"bucket": datetime(2026, 7, 2), "value": 70.8, "count": 1},
                ]
            )
        )
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_recall_series(
                args={"context_id": CTX, "metric": "weight_kg", "period": "day", "agg": "avg"},
                user_id="u",
                workspace_id=uuid4(),
            )
        body = _payload(result)
        assert body["status"] == "success"
        assert body["metric"] == "weight_kg"
        assert body["period"] == "day"
        assert body["agg"] == "avg"
        assert body["count"] == 2
        assert body["series"] == [
            {"bucket": "2026-07-01T00:00:00Z", "value": 71.2, "count": 3},
            {"bucket": "2026-07-02T00:00:00Z", "value": 70.8, "count": 1},
        ]
        kwargs = svc.recall_series.call_args.kwargs
        assert kwargs["period"] == "day"
        assert kwargs["agg"] == "avg"

    @pytest.mark.asyncio
    async def test_defaults_period_day_agg_avg(self):
        svc = MagicMock(recall_series=AsyncMock(return_value=[]))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_recall_series(
                args={"context_id": CTX, "metric": "weight_kg"},
                user_id="u",
                workspace_id=uuid4(),
            )
        body = _payload(result)
        assert body["period"] == "day"
        assert body["agg"] == "avg"
        kwargs = svc.recall_series.call_args.kwargs
        assert kwargs["period"] == "day"
        assert kwargs["agg"] == "avg"
        assert kwargs["start"] is None
        assert kwargs["end"] is None


class TestRegistration:
    def test_tools_are_defined_and_dispatchable(self):
        from mcp_server.tools import _build_registry
        from mcp_server.tools._definitions import get_tool_definitions

        names = {d["name"] for d in get_tool_definitions()}
        assert {"record_measurement", "recall_series"} <= names
        registry = _build_registry()
        assert registry["record_measurement"] is handle_record_measurement
        assert registry["recall_series"] is handle_recall_series

    def test_recall_series_is_read_only(self):
        from mcp_server.tools._definitions import get_tool_definitions

        by_name = {d["name"]: d for d in get_tool_definitions()}
        assert by_name["recall_series"].get("readOnly") is True
        assert by_name["record_measurement"].get("readOnly") is not True

    def test_tools_require_context_id_and_are_rate_limited(self):
        from mcp_server.tools import _RATE_LIMIT_EXEMPT_TOOLS, _TOOLS_WITHOUT_CONTEXT_ID

        for name in ("record_measurement", "recall_series"):
            assert name not in _TOOLS_WITHOUT_CONTEXT_ID
            assert name not in _RATE_LIMIT_EXEMPT_TOOLS


@pytest.mark.asyncio
async def test_numeric_string_value_coerces_like_pydantic_tools():
    """#1322 parity: clients that JSON-stringify scalars get the same
    acceptance a pydantic-backed tool would give."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    svc = MagicMock(record=AsyncMock(return_value=_row()))
    with ExitStack() as stack:
        _enter(stack, service=svc)
        result = await handle_record_measurement(
            args={"context_id": CTX, "metric": "weight_kg", "value": "72.5"},
            user_id="u",
            workspace_id=uuid4(),
        )
    assert _payload(result)["status"] == "success"
    assert svc.record.await_args.kwargs.get("value") == pytest.approx(72.5) or (
        len(svc.record.await_args.args) > 2 and svc.record.await_args.args[2] == pytest.approx(72.5)
    )
