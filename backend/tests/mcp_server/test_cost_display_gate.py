"""#1571 — MCP analysis payloads under ``ENABLE_COST_DISPLAY=false``.

REST keeps its schema shape and nulls the cost fields (pinned in
``tests/api/test_cost_display_feature_gate.py``); the MCP dicts have no
schema to keep stable, so the keys are OMITTED — an agent reading the tool
output never sees a money key at all. Both lanes go through
``services.cost_visibility.strip_cost_fields`` so they cannot drift.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from auth.agent_scope import AgentScope, set_agent_scope
from mcp_server.tools.analysis import _serialize_run_row, handle_analyze_context


@pytest.fixture
def cost_display_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_COST_DISPLAY", "false")
    monkeypatch.setattr("config.settings._settings", None)


def _row() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=uuid4(),
        context_id=uuid4(),
        status="succeeded",
        triggered_by="user-1",
        started_at=datetime(2026, 7, 19, 0, 0, 0),
        finished_at=None,
        input_count=120,
        cost_estimated_cents=42,
        cost_actual_cents=40,
        error=None,
        cancellation_reason=None,
    )


class TestSerializeRunRowCostDisplay:
    def teardown_method(self) -> None:
        set_agent_scope(None)

    def test_disabled_omits_cost_keys(self, cost_display_disabled):
        out = _serialize_run_row(_row())
        assert "cost_estimated_cents" not in out
        assert "cost_actual_cents" not in out
        # Non-money aggregates and the rest of the row are untouched.
        assert out["input_count"] == 120
        assert out["status"] == "succeeded"

    def test_disabled_omits_cost_keys_even_under_enforce_redaction(self, cost_display_disabled):
        """#1366 nulls the aggregates for enforce agents; the deployment
        gate must still remove the money keys rather than leave ``null``
        placeholders behind."""
        set_agent_scope(
            AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=uuid4())
        )
        out = _serialize_run_row(_row())
        assert out["input_count"] is None
        assert "cost_estimated_cents" not in out
        assert "cost_actual_cents" not in out

    def test_enabled_keeps_cost_keys(self):
        out = _serialize_run_row(_row())
        assert out["cost_estimated_cents"] == 42
        assert out["cost_actual_cents"] == 40


# ---------------------------------------------------------------------------
# analyze_context dry_run
# ---------------------------------------------------------------------------


def _fake_get_db(db_mock):
    async def _gen():
        yield db_mock

    return _gen


async def _dry_run(pricing) -> dict:
    db_mock = MagicMock()
    db_mock.execute = AsyncMock()
    db_mock.commit = AsyncMock()
    with ExitStack() as stack:
        for patcher in (
            patch("db.base.get_db", _fake_get_db(db_mock)),
            patch(
                "mcp_server.tools.analysis._verify_context_in_workspace_mcp",
                AsyncMock(return_value=None),
            ),
            patch(
                "auth.analysis_gates.check_memory_analysis_access_mcp",
                AsyncMock(return_value="UTC"),
            ),
            patch(
                "services.analysis.query_service.count_context_memories",
                AsyncMock(return_value=100),
            ),
            patch("mcp_server.tools.analysis._log_tool_usage", AsyncMock()),
            patch(
                "services.analysis.orchestrator.try_resolve_pricing_row",
                AsyncMock(return_value=pricing),
            ),
        ):
            stack.enter_context(patcher)
        result = await handle_analyze_context(
            {"context_id": str(uuid4()), "dry_run": True}, "u1", uuid4()
        )
    assert result
    return json.loads(result[0].text)


_PRICED = (MagicMock(), {"rates": {"input_tokens": 0.2, "output_tokens": 1.25}})


class TestDryRunCostDisplay:
    @pytest.mark.asyncio
    async def test_disabled_omits_estimated_cost(self, cost_display_disabled):
        body = await _dry_run(pricing=_PRICED)
        assert body.get("dry_run") is True, body
        assert "estimated_cost_cents" not in body
        # The rest of the preview is still served.
        assert body["memory_count"] == 100
        assert body["cluster_count_estimate"] >= 1

    @pytest.mark.asyncio
    async def test_enabled_keeps_estimated_cost(self):
        body = await _dry_run(pricing=_PRICED)
        assert body["estimated_cost_cents"] >= 1
