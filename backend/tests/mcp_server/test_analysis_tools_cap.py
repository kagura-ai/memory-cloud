"""#1244: MCP ``analyze_context`` run-size cap enforcement tests.

The REST enforcement points are covered in
``tests/api/test_analyses_routes.py``; these tests pin the two MCP
call sites (dry_run preview and the real start) — without them a
mutation deleting either check passes the whole suite while MCP
quotes prices for (or starts) runs that REST would refuse.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.analysis import handle_analyze_context


def _fake_get_db(db_mock):
    """Async-generator factory standing in for ``db.base.get_db``."""

    async def _gen():
        yield db_mock

    return _gen


def _envelope(result) -> dict:
    assert result, "handler returned an empty response list"
    return json.loads(result[0].text)


@pytest.fixture
def db_mock():
    m = MagicMock()
    m.execute = AsyncMock()
    m.commit = AsyncMock()
    m.rollback = AsyncMock()
    return m


@pytest.fixture
def small_cap(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "analysis_max_memory_count", 50)


class TestAnalyzeContextRunSizeCap:
    @pytest.mark.asyncio
    async def test_dry_run_rejects_over_cap_context(self, db_mock, small_cap):
        """dry_run must return the same validation_error the real start
        does — never quote a price for a run start would refuse."""
        with (
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
                AsyncMock(return_value=51),
            ),
        ):
            result = await handle_analyze_context(
                {"context_id": str(uuid4()), "dry_run": True},
                "u1",
                uuid4(),
            )
        body = _envelope(result)
        assert body.get("error") == "validation_error", body
        assert "50" in body.get("message", "")

    @pytest.mark.asyncio
    async def test_real_start_rejects_over_cap_before_creating_run(self, db_mock, small_cap):
        """The real start must 422 BEFORE orchestrator.start() creates
        any row — otherwise the run dies later in vector_pull having
        consumed a daily-quota slot."""
        with (
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
                AsyncMock(return_value=51),
            ),
            patch("services.analysis.orchestrator.AnalysisOrchestrator") as orch_cls,
        ):
            result = await handle_analyze_context(
                {"context_id": str(uuid4())},
                "u1",
                uuid4(),
            )
        body = _envelope(result)
        assert body.get("error") == "validation_error", body
        orch_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_cap_dry_run_returns_estimate(self, db_mock, small_cap):
        """Control: an under-cap context still gets the cost preview."""
        with (
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
                AsyncMock(return_value=49),
            ),
            patch(
                "mcp_server.tools.analysis._log_tool_usage",
                AsyncMock(),
            ),
            # #1570: the estimate now reads the pricing row from the DB;
            # unpriced here (None) — the pricing surface has its own tests below.
            patch(
                "services.analysis.orchestrator.try_resolve_pricing_row",
                AsyncMock(return_value=None),
            ),
        ):
            result = await handle_analyze_context(
                {"context_id": str(uuid4()), "dry_run": True},
                "u1",
                uuid4(),
            )
        body = _envelope(result)
        assert body.get("dry_run") is True, body
        assert body.get("memory_count") == 49


class TestDryRunEstimatePricing:
    """#1570: the dry_run estimate reads the run's pricing row; unpriced → null."""

    @staticmethod
    async def _run(db_mock, pricing):
        """dry_run analyze_context with the gates stubbed and ``pricing`` resolved."""
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
            return await handle_analyze_context(
                {"context_id": str(uuid4()), "dry_run": True}, "u1", uuid4()
            )

    @pytest.mark.asyncio
    async def test_unpriced_model_surfaces_null_estimate(self, db_mock):
        body = _envelope(await self._run(db_mock, pricing=None))
        assert body.get("dry_run") is True, body
        assert "estimated_cost_cents" in body
        assert body["estimated_cost_cents"] is None
        assert body["memory_count"] == 100

    @pytest.mark.asyncio
    async def test_priced_model_surfaces_a_positive_estimate(self, db_mock):
        snapshot = {"rates": {"input_tokens": 0.2, "output_tokens": 1.25}}
        body = _envelope(await self._run(db_mock, pricing=(MagicMock(), snapshot)))
        assert body["estimated_cost_cents"] >= 1
