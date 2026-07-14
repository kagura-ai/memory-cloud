"""HTTP/serialization tests for ``api/routes/analyses.py`` (Issue #496).

The full 4-stage gate behavior is covered by
``tests/api/test_analyses_quota_precedence.py``; this suite uses
``app.dependency_overrides`` to bypass the gate and focuses on:

- Route → service wiring (POST /preview returns the cost shape, GET /list
  paginates, GET /{run_id} 404s on unknown ids, DELETE soft-cancels).
- Pydantic response shape (``run_id``, ``status``, ``analysis_runs_today``).
- Context-boundary 404 on a context belonging to another workspace.
- Idempotent DELETE when the run is already terminal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.analysis_gates import (
    require_memory_analysis_access,
    require_memory_analysis_read,
)
from db.base import get_db

# ============================================================================
# Test fixtures
# ============================================================================


_TEST_USER_ID = "test_user_analyses"
_TEST_WORKSPACE_ID = uuid4()
_TEST_CONTEXT_ID = uuid4()
_TEST_TZ = "Asia/Tokyo"


def _gate_payload() -> tuple[str, UUID, str]:
    return (_TEST_USER_ID, _TEST_WORKSPACE_ID, _TEST_TZ)


@pytest.fixture
def db_mock():
    """A MagicMock standing in for AsyncSession.

    Per-test ``execute`` side effects are configured inside each test;
    this fixture just supplies the bare object so the route handler's
    ``db.execute(...)`` calls don't AttributeError.
    """
    m = MagicMock()
    m.execute = AsyncMock()
    m.commit = AsyncMock()
    m.rollback = AsyncMock()
    m.refresh = AsyncMock()
    return m


@pytest.fixture
def client(db_mock):
    async def _override_write_gate():
        return _gate_payload()

    async def _override_read_gate():
        return _gate_payload()

    async def _override_db():
        yield db_mock

    app.dependency_overrides[require_memory_analysis_access] = _override_write_gate
    app.dependency_overrides[require_memory_analysis_read] = _override_read_gate
    app.dependency_overrides[get_db] = _override_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _scalar_one(value: Any) -> MagicMock:
    """Mock ``await db.execute(...)``'s ``.scalar_one_or_none()`` return."""
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=value)
    res.scalar = MagicMock(return_value=value)
    return res


def _scalars_all(values: list) -> MagicMock:
    """Mock ``await db.execute(...).scalars().all()``."""
    res = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=values)
    res.scalars = MagicMock(return_value=scalars)
    return res


# ============================================================================
# POST /preview
# ============================================================================


class TestPreview:
    def test_returns_cost_estimate_shape(self, client, db_mock):
        # Boundary check passes (Context exists in workspace) → memory count = 100.
        db_mock.execute.side_effect = [
            _scalar_one(_TEST_CONTEXT_ID),  # Context boundary
            _scalar_one(100),  # _count_filtered_memories
        ]
        response = client.post(
            f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/preview",
            json={},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["memory_count"] == 100
        assert body["model_id"] == "gpt-5-nano"
        assert body["cluster_count_estimate"] >= 1
        assert body["estimated_cost_cents"] >= 1
        assert "input_tokens" in body["breakdown"]


# ============================================================================
# POST / (start)
# ============================================================================


class TestStartRun:
    def test_returns_202_with_run_id_on_success(self, client, db_mock):
        run_id = uuid4()
        started_at = datetime(2026, 5, 2, 0, 0, 0)
        # Boundary check passes; orchestrator stub returns a fake row.
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]

        fake_analysis = MagicMock(id=run_id, status="running", started_at=started_at)
        with (
            patch("api.routes.analyses.AnalysisOrchestrator") as orch_cls,
            patch(
                "api.routes.analyses.run_analysis_task",
                AsyncMock(),
            ),
        ):
            orch_cls.return_value.start = AsyncMock(return_value=fake_analysis)
            response = client.post(
                f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses",
                json={},
            )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["run_id"] == str(run_id)
        assert body["status"] == "running"

    def test_404_on_foreign_context(self, client, db_mock):
        # Boundary check returns None → context_not_found.
        db_mock.execute.side_effect = [_scalar_one(None)]
        response = client.post(
            f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses",
            json={},
        )
        assert response.status_code == 404


# ============================================================================
# GET / (list)
# ============================================================================


class TestListRuns:
    def test_returns_items_and_next_cursor(self, client, db_mock):
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]  # boundary

        run_id = uuid4()
        fake_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="succeeded",
            triggered_by=_TEST_USER_ID,
            started_at=datetime(2026, 5, 2),
            finished_at=datetime(2026, 5, 2),
            input_count=10,
            cost_estimated_cents=5,
            cost_actual_cents=4,
            error=None,
            cancellation_reason=None,
        )
        with patch(
            "services.analysis.query_service.list_analyses",
            AsyncMock(return_value=([fake_run], "2026-05-01T00:00:00")),
        ):
            response = client.get(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses?limit=20")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["run_id"] == str(run_id)
        assert body["next_cursor"] == "2026-05-01T00:00:00"


# ============================================================================
# GET /{run_id}
# ============================================================================


class TestGetRun:
    def test_returns_404_for_unknown_run(self, client, db_mock):
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        with patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=None),
        ):
            response = client.get(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{uuid4()}")
        assert response.status_code == 404

    def test_returns_404_when_run_belongs_to_different_context(self, client, db_mock):
        """Run exists in workspace but is bound to ANOTHER context."""
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        run_id = uuid4()
        fake_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=uuid4(),  # different context
            status="succeeded",
            triggered_by=_TEST_USER_ID,
            started_at=datetime(2026, 5, 2),
            finished_at=datetime(2026, 5, 2),
            input_count=10,
            cost_estimated_cents=5,
            cost_actual_cents=4,
            error=None,
            cancellation_reason=None,
        )
        with patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=fake_run),
        ):
            response = client.get(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}")
        assert response.status_code == 404


# ============================================================================
# GET /{run_id}/clusters (#497 list_run_clusters)
# ============================================================================


class TestListRunClusters:
    def test_returns_clusters_ordered_by_index(self, client, db_mock):
        """Happy path: existing run, non-empty cluster list."""
        run_id = uuid4()
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        fake_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="succeeded",
        )
        fake_clusters = [
            MagicMock(
                cluster_index=0,
                label="Cluster A",
                description="first cluster",
                count=42,
                centroid_2d=[0.1, 0.2],
                representative_memory_ids=[uuid4(), uuid4()],
                property_stats={"top_tags": []},
                label_confidence=0.91,
            ),
            MagicMock(
                cluster_index=1,
                label="Cluster B",
                description=None,
                count=15,
                centroid_2d=[1.5, -0.4],
                representative_memory_ids=[],
                property_stats={},
                label_confidence=0.78,
            ),
        ]
        with (
            patch(
                "services.analysis.query_service.get_analysis",
                AsyncMock(return_value=fake_run),
            ),
            patch(
                "services.analysis.query_service.list_clusters",
                AsyncMock(return_value=fake_clusters),
            ),
        ):
            response = client.get(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}/clusters")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 2
        assert body["items"][0]["cluster_index"] == 0
        assert body["items"][0]["label"] == "Cluster A"
        assert body["items"][0]["centroid_2d"] == [0.1, 0.2]
        assert body["items"][1]["description"] is None

    def test_returns_empty_items_for_running_run(self, client, db_mock):
        """No 404 when the labeler has not produced clusters yet."""
        run_id = uuid4()
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        fake_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="running",
        )
        with (
            patch(
                "services.analysis.query_service.get_analysis",
                AsyncMock(return_value=fake_run),
            ),
            patch(
                "services.analysis.query_service.list_clusters",
                AsyncMock(return_value=[]),
            ),
        ):
            response = client.get(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}/clusters")
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []

    def test_404_when_run_belongs_to_another_context(self, client, db_mock):
        """Cross-context run lookup returns 404, not the foreign clusters."""
        run_id = uuid4()
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        foreign_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=uuid4(),  # different context
            status="succeeded",
        )
        with patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=foreign_run),
        ):
            response = client.get(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}/clusters")
        assert response.status_code == 404


# ============================================================================
# GET /{run_id}/positions (#497 list_run_positions)
# ============================================================================


class TestListRunPositions:
    def test_returns_position_rows(self, client, db_mock):
        run_id = uuid4()
        memory_a, memory_b = uuid4(), uuid4()
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        fake_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="succeeded",
        )
        fake_positions = [
            {
                "memory_id": str(memory_a),
                "x": 1.23,
                "y": -0.45,
                "cluster_index": 0,
            },
            {
                "memory_id": str(memory_b),
                "x": 0.0,
                "y": 0.0,
                "cluster_index": 1,
            },
        ]
        with (
            patch(
                "services.analysis.query_service.get_analysis",
                AsyncMock(return_value=fake_run),
            ),
            patch(
                "services.analysis.query_service.list_positions",
                AsyncMock(return_value=fake_positions),
            ),
        ):
            response = client.get(
                f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}/positions"
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 2
        assert body["items"][0]["memory_id"] == str(memory_a)
        assert body["items"][0]["x"] == 1.23
        assert body["items"][1]["cluster_index"] == 1

    def test_404_when_run_unknown(self, client, db_mock):
        run_id = uuid4()
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        with patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=None),
        ):
            response = client.get(
                f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}/positions"
            )
        assert response.status_code == 404


# ============================================================================
# DELETE /{run_id} (soft cancel)
# ============================================================================


class TestCancelRun:
    def test_soft_cancels_running_run(self, client, db_mock):
        run_id = uuid4()
        fake_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="running",
            triggered_by=_TEST_USER_ID,
            started_at=datetime(2026, 5, 2),
            finished_at=None,
            input_count=10,
            cost_estimated_cents=5,
            cost_actual_cents=None,
            error=None,
            cancellation_reason=None,
        )
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        with (
            patch(
                "services.analysis.query_service.get_analysis",
                AsyncMock(return_value=fake_run),
            ),
            patch(
                "api.routes.analyses.cancel_run_task",
                return_value=True,
            ) as mock_cancel,
        ):
            response = client.delete(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "cancelled"
        assert body["cancellation_reason"] == "user"
        assert fake_run.status == "cancelled"
        assert fake_run.cancellation_reason == "user"
        # #1241: the locked re-check MUST be refresh(with_for_update=True) —
        # a plain locked SELECT would return the identity-mapped instance
        # with STALE attributes (see services/workspace_locks.py) and the
        # lock-loser would clobber the winner's committed terminal state.
        db_mock.refresh.assert_awaited_once_with(fake_run, with_for_update=True)
        # #1241: a confirmed cancel also stops the in-process compute.
        mock_cancel.assert_called_once_with(run_id)

    def test_cancel_lost_race_returns_actual_terminal_state(self, client, db_mock):
        """#1241: if persist_results wins the row lock and commits
        'succeeded' first, the locked re-check must NOT flip the run to
        cancelled (and must not cancel the finished task) — the response
        reports the actual terminal state.
        """
        run_id = uuid4()
        stale_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="running",  # stale pre-lock read
            triggered_by=_TEST_USER_ID,
            started_at=datetime(2026, 5, 2),
            finished_at=None,
            input_count=10,
            cost_estimated_cents=5,
            cost_actual_cents=None,
            error=None,
            cancellation_reason=None,
        )
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]

        async def _refresh_reveals_success(instance, **kwargs):
            # Simulates the locked repopulating read: persist_results won
            # the lock and committed 'succeeded' — refresh(with_for_update)
            # overwrites the stale in-memory 'running'.
            instance.status = "succeeded"
            instance.finished_at = datetime(2026, 5, 2)
            instance.cost_actual_cents = 4

        db_mock.refresh = AsyncMock(side_effect=_refresh_reveals_success)
        with (
            patch(
                "services.analysis.query_service.get_analysis",
                AsyncMock(return_value=stale_run),
            ),
            patch(
                "api.routes.analyses.cancel_run_task",
                return_value=True,
            ) as mock_cancel,
        ):
            response = client.delete(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "succeeded"
        assert stale_run.status == "succeeded"
        assert stale_run.cancellation_reason is None  # never clobbered
        mock_cancel.assert_not_called()

    def test_cancel_hard_deleted_run_returns_404(self, client, db_mock):
        """#1241: the run row vanished (context/workspace CASCADE) between
        the initial read and the locked refresh — 404, not a 200 with a
        stale 'running' body implying the cancel is still possible."""
        from sqlalchemy.orm.exc import ObjectDeletedError

        run_id = uuid4()
        stale_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="running",
            triggered_by=_TEST_USER_ID,
            started_at=datetime(2026, 5, 2),
            finished_at=None,
            input_count=10,
            cost_estimated_cents=5,
            cost_actual_cents=None,
            error=None,
            cancellation_reason=None,
        )
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        db_mock.refresh = AsyncMock(
            side_effect=ObjectDeletedError(MagicMock(), "row deleted by CASCADE")
        )
        with patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=stale_run),
        ):
            response = client.delete(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}")
        assert response.status_code == 404, response.text

    def test_idempotent_when_already_terminal(self, client, db_mock):
        """Already-succeeded runs return 200 with current state, no flip."""
        db_mock.execute.side_effect = [_scalar_one(_TEST_CONTEXT_ID)]
        run_id = uuid4()
        fake_run = MagicMock(
            id=run_id,
            workspace_id=_TEST_WORKSPACE_ID,
            context_id=_TEST_CONTEXT_ID,
            status="succeeded",
            triggered_by=_TEST_USER_ID,
            started_at=datetime(2026, 5, 2),
            finished_at=datetime(2026, 5, 2),
            input_count=10,
            cost_estimated_cents=5,
            cost_actual_cents=4,
            error=None,
            cancellation_reason=None,
        )
        with patch(
            "services.analysis.query_service.get_analysis",
            AsyncMock(return_value=fake_run),
        ):
            response = client.delete(f"/api/v1/contexts/{_TEST_CONTEXT_ID}/analyses/{run_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"  # NOT flipped to cancelled
        assert fake_run.status == "succeeded"
