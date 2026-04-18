"""Tests for the /admin/bm25-drift admin API (issue #343).

Mirrors test_sleep_reports_admin.py: dependency_overrides for require_admin
+ get_db, MagicMock AsyncSession with execute.side_effect chained per
endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db


@pytest.fixture
def admin_user() -> dict:
    return {"email": "admin@example.com", "role": "admin"}


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def client(admin_user, mock_db):
    """TestClient with require_admin / get_db overrides; clears on teardown."""

    async def _admin():
        return admin_user

    async def _db():
        yield mock_db

    app.dependency_overrides[require_admin] = _admin
    app.dependency_overrides[get_db] = _db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _make_drift_row(
    *,
    row_id: int = 1,
    psi: Decimal | None = Decimal("0.05"),
    psi_status: str = "stable",
    context_id=None,
) -> SimpleNamespace:
    """Build a minimal Bm25IdfDriftLog stand-in for from_attributes parsing."""
    return SimpleNamespace(
        id=row_id,
        context_id=context_id or uuid4(),
        measured_at=datetime(2026, 4, 19, 3, 0, 0, tzinfo=UTC),
        psi=psi,
        psi_status=psi_status,
        m_memory_points=200,
        r_resource_points=300,
        num_terms=80,
        top_divergent_terms=[
            {
                "index": 12345,
                "df_memory": 10,
                "df_global": 50,
                "idf_memory": 1.5,
                "idf_global": 0.3,
                "delta": 1.2,
            }
        ],
    )


class TestList:
    def test_returns_paginated_list(self, client: TestClient, mock_db: MagicMock) -> None:
        ctx_id = uuid4()
        row = _make_drift_row(context_id=ctx_id)
        # Three queries: count, rows, context-batch.
        count_result = MagicMock()
        count_result.scalar.return_value = 1
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = [row]
        ctx_result = MagicMock()
        ctx_result.all.return_value = [(ctx_id, "ctx-name", None, None)]
        mock_db.execute.side_effect = [count_result, rows_result, ctx_result]

        resp = client.get("/api/v1/admin/bm25-drift/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["rows"]) == 1
        assert body["rows"][0]["context_name"] == "ctx-name"
        assert body["rows"][0]["psi_status"] == "stable"

    def test_invalid_status_returns_400(self, client: TestClient) -> None:
        resp = client.get("/api/v1/admin/bm25-drift/?status=garbage")
        assert resp.status_code == 400

    def test_filter_by_context_id_skips_batch_lookup_when_no_rows(
        self, client: TestClient, mock_db: MagicMock
    ) -> None:
        count_result = MagicMock()
        count_result.scalar.return_value = 0
        rows_result = MagicMock()
        rows_result.scalars.return_value.all.return_value = []
        # Only two execute calls when the result list is empty (no
        # context-batch query needed).
        mock_db.execute.side_effect = [count_result, rows_result]
        ctx = uuid4()
        resp = client.get(f"/api/v1/admin/bm25-drift/?context_id={ctx}")
        assert resp.status_code == 200
        assert resp.json() == {"rows": [], "total": 0, "limit": 50, "offset": 0}


class TestDetail:
    def test_returns_detail_with_top_terms(self, client: TestClient, mock_db: MagicMock) -> None:
        ctx_id = uuid4()
        row = _make_drift_row(context_id=ctx_id)
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = row
        ctx_result = MagicMock()
        ctx_result.first.return_value = (ctx_id, "ctx-name", "Ctx Display", None)
        mock_db.execute.side_effect = [row_result, ctx_result]

        resp = client.get("/api/v1/admin/bm25-drift/1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["row"]["psi_status"] == "stable"
        assert body["row"]["context_name"] == "Ctx Display"
        assert body["row"]["top_divergent_terms"][0]["index"] == 12345

    def test_missing_returns_404(self, client: TestClient, mock_db: MagicMock) -> None:
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [row_result]

        resp = client.get("/api/v1/admin/bm25-drift/9999")
        assert resp.status_code == 404

    def test_deleted_context_marks_flag(self, client: TestClient, mock_db: MagicMock) -> None:
        ctx_id = uuid4()
        row = _make_drift_row(context_id=ctx_id)
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = row
        ctx_result = MagicMock()
        # deleted_at is non-null → context is treated as deleted.
        deleted_at = datetime(2026, 4, 1, tzinfo=UTC)
        ctx_result.first.return_value = (ctx_id, "ctx", "Ctx", deleted_at)
        mock_db.execute.side_effect = [row_result, ctx_result]

        resp = client.get("/api/v1/admin/bm25-drift/1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["row"]["context_deleted"] is True
        assert body["row"]["context_name"] is None


class TestRunTrigger:
    def test_run_with_explicit_context_returns_202(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch asyncio.create_task so the test does not actually spawn
        # background work against the mocked DB.
        from api.routes import bm25_drift as drift_route

        captured = []
        monkeypatch.setattr(
            drift_route.asyncio,
            "create_task",
            lambda coro: captured.append(coro) or coro.close() or MagicMock(),
        )

        ctx = uuid4()
        resp = client.post(
            "/api/v1/admin/bm25-drift/run",
            json={"context_id": str(ctx)},
        )
        assert resp.status_code == 202
        assert resp.json() == {"scheduled_context_count": 1}
        assert len(captured) == 1
