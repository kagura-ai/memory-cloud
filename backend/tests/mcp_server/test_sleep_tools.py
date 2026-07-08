"""Tests for Sleep Maintenance MCP tool handlers.

Issue #164: get_sleep_history, get_sleep_report, rollback_sleep_run.
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.sleep import (
    handle_get_sleep_history,
    handle_get_sleep_report,
    handle_rollback_sleep_run,
)
from services.sleep.prompts import EDGE_DISCOVERY_PROMPT_REVISION


class TestGetSleepHistory:
    """Test get_sleep_history MCP tool handler."""

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    def _mock_report(self, context_id, status="completed"):
        r = MagicMock()
        r.id = uuid4()
        r.context_id = context_id
        r.status = status
        r.started_at = datetime(2026, 4, 5, 2, 0, 0, tzinfo=UTC)
        r.completed_at = datetime(2026, 4, 5, 2, 3, 0, tzinfo=UTC)
        r.memories_processed = 45
        r.edges_created = 3
        r.memories_merged = 2
        r.memories_promoted = 5
        r.llm_calls_made = 4
        r.llm_tokens_used = 1200
        r.llm_call_failures = 0  # #1183
        return r

    def _setup_history_mocks(self, reports):
        """Set up mock DB for get_sleep_history (reports query + log)."""
        mock_db = AsyncMock()
        mock_reports_result = MagicMock()
        mock_reports_result.scalars.return_value.all.return_value = reports
        mock_log_result = MagicMock()
        mock_db.execute.side_effect = [mock_reports_result, mock_log_result]
        mock_db.commit = AsyncMock()
        return mock_db

    @pytest.mark.asyncio
    async def test_returns_history(self, user_id, workspace_id, context_id):
        report = self._mock_report(context_id)
        mock_db = self._setup_history_mocks([report])

        async def mock_get_db():
            yield mock_db

        mock_ctx = MagicMock()
        mock_ctx.id = context_id

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.sleep._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
        ):
            result = await handle_get_sleep_history(
                {"context_id": str(context_id)}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["count"] == 1
        assert data["reports"][0]["status"] == "completed"
        assert data["reports"][0]["memories_processed"] == 45

    @pytest.mark.asyncio
    async def test_empty_history(self, user_id, workspace_id, context_id):
        mock_db = self._setup_history_mocks([])

        async def mock_get_db():
            yield mock_db

        mock_ctx = MagicMock()
        mock_ctx.id = context_id

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.sleep._resolve_context",
                new_callable=AsyncMock,
                return_value=mock_ctx,
            ),
        ):
            result = await handle_get_sleep_history(
                {"context_id": str(context_id)}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["count"] == 0
        assert data["reports"] == []


class TestGetSleepReport:
    """Test get_sleep_report MCP tool handler."""

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    def _mock_report(self, report_id, user_id):
        r = MagicMock()
        r.id = report_id
        r.user_id = user_id
        r.context_id = uuid4()
        r.workspace_id = uuid4()
        r.status = "completed"
        r.started_at = datetime(2026, 4, 5, 2, 0, 0, tzinfo=UTC)
        r.completed_at = datetime(2026, 4, 5, 2, 3, 0, tzinfo=UTC)
        r.memories_processed = 10
        r.edges_created = 2
        r.memories_merged = 1
        r.memories_promoted = 3
        r.memories_flagged = 0
        r.llm_calls_made = 2
        r.llm_tokens_used = 500
        r.llm_call_failures = 0  # #1183
        r.embedding_calls_made = 0
        r.error_message = None
        r.edge_discovery_result = {"success": True}
        r.dedup_result = {"success": True}
        r.importance_result = None
        r.consolidation_result = None
        r.reindex_result = None
        r.merge_retention_result = None  # #1209
        return r

    def _mock_action(self, phase="edge_discovery", action_type="create_edge"):
        a = MagicMock()
        a.id = 1
        a.phase = phase
        a.action_type = action_type
        a.memory_id = uuid4()
        a.target_id = uuid4()
        a.details = {"edge_type": "related_to"}
        a.created_at = datetime(2026, 4, 5, 2, 1, 0, tzinfo=UTC)
        return a

    @pytest.mark.asyncio
    async def test_returns_report_with_actions(self, user_id, workspace_id):
        report_id = uuid4()
        report = self._mock_report(report_id, user_id)
        action = self._mock_action()

        mock_db = AsyncMock()
        # Query 1: SELECT sleep_reports
        mock_report_result = MagicMock()
        mock_report_result.scalar_one_or_none.return_value = report
        # Query 2: SELECT sleep_actions
        mock_actions_result = MagicMock()
        mock_actions_result.scalars.return_value.all.return_value = [action]
        # Query 3: _log_tool_usage
        mock_log_result = MagicMock()

        mock_db.execute.side_effect = [mock_report_result, mock_actions_result, mock_log_result]
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_sleep_report(
                {"report_id": str(report_id)}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        assert data["action_count"] == 1
        assert data["report"]["status"] == "completed"
        assert data["actions"][0]["action_type"] == "create_edge"

    @pytest.mark.asyncio
    async def test_report_not_found(self, user_id, workspace_id):
        mock_db = AsyncMock()
        mock_report_result = MagicMock()
        mock_report_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_report_result
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_sleep_report(
                {"report_id": str(uuid4())}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "report_not_found"

    @pytest.mark.asyncio
    async def test_missing_report_id(self, user_id, workspace_id):
        result = await handle_get_sleep_report({}, user_id, workspace_id)
        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "missing_required_fields"

    @pytest.mark.asyncio
    async def test_invalid_report_id(self, user_id, workspace_id):
        result = await handle_get_sleep_report({"report_id": "not-a-uuid"}, user_id, workspace_id)
        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "invalid_report_id"

    @pytest.mark.asyncio
    async def test_get_sleep_report_includes_edge_discovery_metrics(self, user_id, workspace_id):
        """Issues #306 / #372: edge_discovery_result JSON column passes new
        metric keys through `_report_to_detail` to the MCP response untouched.

        Verifies that the JSON column contract — sleep_reports.details written
        by `execute()` → SQLAlchemy JSON serialize → `_report_to_detail` read —
        round-trips every documented metric key without filtering or reshaping.
        Covers the accept-side (#306) keys and the parallel reject-side
        (#372) `_rejected` suffix keys.
        """
        report_id = uuid4()
        report = self._mock_report(report_id, user_id)
        report.edge_discovery_result = {
            "success": True,
            "skipped": False,
            "skip_reason": None,
            "error": None,
            "llm_calls": 2,
            "memories_processed": 10,
            "details": {
                "sampled": 10,
                "candidates": 6,
                "filtered": 5,
                "edges_created": 3,
                "llm_accepted": 3,
                "llm_rejected": 2,
                "llm_call_failures": 0,
                "auto_accepted": 0,
                "edge_type_dist": {"related_to": 2, "depends_on": 1},
                "avg_confidence": 0.78,
                # PhD-review additions (#306 follow-up): 5-number summary +
                # sample size + imputation counter must also round-trip.
                "median_confidence": 0.80,
                "p25_confidence": 0.65,
                "p75_confidence": 0.92,
                "confidence_n": 3,
                "confidence_imputed": 0,
                "confidence_histogram": {
                    "0.0-0.5": 0,
                    "0.5-0.7": 1,
                    "0.7-0.85": 1,
                    "0.85-1.0": 1,
                },
                # #372: reject-side parallel metrics must round-trip identically.
                "avg_confidence_rejected": 0.42,
                "median_confidence_rejected": 0.40,
                "p25_confidence_rejected": 0.35,
                "p75_confidence_rejected": 0.50,
                "confidence_n_rejected": 2,
                "confidence_imputed_rejected": 0,
                "confidence_histogram_rejected": {
                    "0.0-0.5": 1,
                    "0.5-0.7": 1,
                    "0.7-0.85": 0,
                    "0.85-1.0": 0,
                },
                "llm_model": "gpt-5-nano",
                "prompt_revision": EDGE_DISCOVERY_PROMPT_REVISION,
            },
        }

        mock_db = AsyncMock()
        mock_report_result = MagicMock()
        mock_report_result.scalar_one_or_none.return_value = report
        mock_actions_result = MagicMock()
        mock_actions_result.scalars.return_value.all.return_value = []
        mock_log_result = MagicMock()
        mock_db.execute.side_effect = [mock_report_result, mock_actions_result, mock_log_result]
        mock_db.commit = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_get_sleep_report(
                {"report_id": str(report_id)}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "success"
        ed = data["report"]["edge_discovery_result"]["details"]
        # Every #306 metric key must reach the MCP response untouched.
        assert ed["llm_accepted"] == 3
        assert ed["llm_rejected"] == 2
        assert ed["llm_call_failures"] == 0
        assert ed["auto_accepted"] == 0
        assert ed["edge_type_dist"] == {"related_to": 2, "depends_on": 1}
        assert ed["avg_confidence"] == 0.78
        assert ed["confidence_histogram"] == {
            "0.0-0.5": 0,
            "0.5-0.7": 1,
            "0.7-0.85": 1,
            "0.85-1.0": 1,
        }
        assert ed["llm_model"] == "gpt-5-nano"
        # Track the constant rather than hardcoding "v1" so this test stays
        # green when EDGE_DISCOVERY_PROMPT_REVISION is bumped on prompt edits
        # (addresses Copilot review #371 finding, loop 5).
        assert ed["prompt_revision"] == EDGE_DISCOVERY_PROMPT_REVISION
        # PhD-review additions (#306 follow-up FB loop) — without these
        # assertions, a future filter/reshape regression in _report_to_detail
        # could silently drop the new keys.
        assert ed["median_confidence"] == 0.80
        assert ed["p25_confidence"] == 0.65
        assert ed["p75_confidence"] == 0.92
        assert ed["confidence_n"] == 3
        assert ed["confidence_imputed"] == 0
        # #372 reject-side parallel keys — dropping any of these from
        # `_report_to_detail` would silently hide half of the decision-
        # boundary signal that #372 was specifically filed to restore.
        assert ed["avg_confidence_rejected"] == 0.42
        assert ed["median_confidence_rejected"] == 0.40
        assert ed["p25_confidence_rejected"] == 0.35
        assert ed["p75_confidence_rejected"] == 0.50
        assert ed["confidence_n_rejected"] == 2
        assert ed["confidence_imputed_rejected"] == 0
        assert ed["confidence_histogram_rejected"] == {
            "0.0-0.5": 1,
            "0.5-0.7": 1,
            "0.7-0.85": 0,
            "0.85-1.0": 0,
        }


class TestRollbackSleepRun:
    """Test rollback_sleep_run MCP tool handler."""

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_missing_report_id(self, user_id, workspace_id):
        result = await handle_rollback_sleep_run({}, user_id, workspace_id)
        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "missing_required_fields"

    @pytest.mark.asyncio
    async def test_invalid_report_id(self, user_id, workspace_id):
        result = await handle_rollback_sleep_run({"report_id": "bad-uuid"}, user_id, workspace_id)
        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "invalid_report_id"

    @pytest.mark.asyncio
    async def test_report_not_found(self, user_id, workspace_id):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_rollback_sleep_run(
                {"report_id": str(uuid4())}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "report_not_found"

    @pytest.mark.asyncio
    async def test_cannot_rollback_non_completed(self, user_id, workspace_id):
        report_id = uuid4()
        report = MagicMock()
        report.id = report_id
        report.user_id = user_id
        report.status = "running"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = report
        mock_db.execute.return_value = mock_result
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_rollback_sleep_run(
                {"report_id": str(report_id)}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "invalid_status"
        assert "running" in data["message"]

    @pytest.mark.asyncio
    async def test_cannot_rollback_already_rolled_back(self, user_id, workspace_id):
        report_id = uuid4()
        report = MagicMock()
        report.id = report_id
        report.user_id = user_id
        report.status = "rolled_back"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = report
        mock_db.execute.return_value = mock_result
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with patch("db.base.get_db", new=mock_get_db):
            result = await handle_rollback_sleep_run(
                {"report_id": str(report_id)}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "invalid_status"
        assert "rolled_back" in data["message"]

    @pytest.mark.asyncio
    async def test_no_actions_to_rollback(self, user_id, workspace_id):
        report_id = uuid4()
        report = MagicMock()
        report.id = report_id
        report.user_id = user_id
        report.status = "completed"
        report.context_id = uuid4()

        mock_db = AsyncMock()
        # Query 1: SELECT report
        mock_report_result = MagicMock()
        mock_report_result.scalar_one_or_none.return_value = report
        # Query 2: SELECT actions (empty)
        mock_actions_result = MagicMock()
        mock_actions_result.scalars.return_value.all.return_value = []

        mock_db.execute.side_effect = [mock_report_result, mock_actions_result]
        mock_db.rollback = AsyncMock()

        async def mock_get_db():
            yield mock_db

        with (
            patch("db.base.get_db", new=mock_get_db),
            patch(
                "mcp_server.tools.sleep._check_viewer_permission",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await handle_rollback_sleep_run(
                {"report_id": str(report_id)}, user_id, workspace_id
            )

        data = json.loads(result[0].text)
        assert data["status"] == "error"
        assert data["error"] == "no_actions"
