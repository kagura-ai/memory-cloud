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
    return {
        "email": "admin@example.com",
        "role": "admin",
        "user_id": "admin-user-id",
        "sub": "admin-sub",
    }


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
        ctx_result.first.return_value = ("ctx-name", "Ctx Display", None)
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
        ctx_result.first.return_value = ("ctx", "Ctx", deleted_at)
        mock_db.execute.side_effect = [row_result, ctx_result]

        resp = client.get("/api/v1/admin/bm25-drift/1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["row"]["context_deleted"] is True
        assert body["row"]["context_name"] is None


class TestRevealTerms:
    """Tests for POST /admin/bm25-drift/{row_id}/reveal-terms (#377)."""

    @pytest.fixture
    def patch_reveal_deps(self, monkeypatch: pytest.MonkeyPatch):
        """Patch Qdrant scroll + Redis counter at the route module."""
        from api.routes import bm25_drift as drift_route

        scroll_calls: list[str] = []
        counter_calls: list[tuple[str, int | None]] = []

        async def _scroll(context_id: str, **_: object):
            scroll_calls.append(context_id)
            point = SimpleNamespace(
                payload={
                    "summary_tokens": "alpha beta gamma",
                    "context_summary_tokens": "",
                    "content_tokens": "",
                    "summary_reading": "",
                }
            )
            yield [point]

        async def _increment(key: str, ttl: int | None = None) -> int:
            counter_calls.append((key, ttl))
            return len(counter_calls)

        monkeypatch.setattr(drift_route, "scroll_context_points", _scroll)
        monkeypatch.setattr(drift_route, "increment_counter", _increment)
        return scroll_calls, counter_calls

    def test_returns_resolved_terms_on_success(
        self,
        client: TestClient,
        mock_db: MagicMock,
        patch_reveal_deps,
    ) -> None:
        import mmh3

        ctx_id = uuid4()
        alpha_hash = mmh3.hash("alpha", signed=False)
        row = _make_drift_row(context_id=ctx_id)
        row.top_divergent_terms = [
            {
                "index": alpha_hash,
                "df_memory": 10,
                "df_global": 50,
                "idf_memory": 1.5,
                "idf_global": 0.3,
                "delta": 1.2,
            },
            {
                "index": 99999999,  # unresolvable
                "df_memory": 5,
                "df_global": 25,
                "idf_memory": 1.1,
                "idf_global": 0.2,
                "delta": 0.9,
            },
        ]
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = row
        ctx_result = MagicMock()
        ctx_result.first.return_value = ("ctx", "Ctx", None)
        mock_db.execute.side_effect = [row_result, ctx_result]
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        resp = client.post(
            f"/api/v1/admin/bm25-drift/{row.id}/reveal-terms",
            json={"reason": "Investigating drift alert PSI 0.31"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        terms = body["resolved_terms"]
        assert len(terms) == 2
        assert terms[0]["token"] == "alpha"
        assert terms[1]["token"] is None
        # Audit log was written and committed.
        assert mock_db.add.call_count == 1
        assert mock_db.commit.await_count == 1

    def test_missing_row_returns_404(
        self,
        client: TestClient,
        mock_db: MagicMock,
        patch_reveal_deps,
    ) -> None:
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [row_result]
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        resp = client.post(
            "/api/v1/admin/bm25-drift/9999/reveal-terms",
            json={"reason": "Investigating drift alert PSI 0.31"},
        )
        assert resp.status_code == 404
        # 404 branch now writes a denied-attempt audit row.
        assert mock_db.add.call_count == 1
        assert mock_db.commit.await_count == 1

    def test_short_reason_returns_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/admin/bm25-drift/1/reveal-terms",
            json={"reason": "too short"},  # 9 chars, min is 10
        )
        assert resp.status_code == 422

    def test_missing_reason_returns_422(self, client: TestClient) -> None:
        resp = client.post("/api/v1/admin/bm25-drift/1/reveal-terms", json={})
        assert resp.status_code == 422

    def test_rate_limit_returns_429(
        self,
        client: TestClient,
        mock_db: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from api.routes import bm25_drift as drift_route

        ctx_id = uuid4()
        row = _make_drift_row(context_id=ctx_id)
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = row
        mock_db.execute.side_effect = [row_result]
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        async def _scroll(*_a, **_kw):
            if False:
                yield []  # pragma: no cover - never reached, still an async generator

        async def _over_limit(*_a, **_kw) -> int:
            # Default settings.bm25_reveal_rate_limit_per_hour = 10.
            return 11

        monkeypatch.setattr(drift_route, "scroll_context_points", _scroll)
        monkeypatch.setattr(drift_route, "increment_counter", _over_limit)

        resp = client.post(
            "/api/v1/admin/bm25-drift/1/reveal-terms",
            json={"reason": "Investigating drift alert PSI 0.31"},
        )
        assert resp.status_code == 429
        # 429 branch now writes a denied-attempt audit row.
        assert mock_db.add.call_count == 1
        assert mock_db.commit.await_count == 1

    def test_redis_error_returns_503_with_audit(
        self,
        client: TestClient,
        mock_db: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Redis incident → fail-closed 503 + audit row with rate_limit_unavailable."""
        from api.routes import bm25_drift as drift_route
        from utils.exceptions import RedisError

        ctx_id = uuid4()
        row = _make_drift_row(context_id=ctx_id)
        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = row
        mock_db.execute.side_effect = [row_result]
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        async def _redis_down(*_a, **_kw) -> int:
            raise RedisError("connection refused")

        monkeypatch.setattr(drift_route, "increment_counter", _redis_down)

        resp = client.post(
            "/api/v1/admin/bm25-drift/1/reveal-terms",
            json={"reason": "Investigating drift alert PSI 0.31"},
        )
        assert resp.status_code == 503
        assert mock_db.add.call_count == 1
        assert mock_db.commit.await_count == 1

    def test_non_admin_returns_403(self, mock_db: MagicMock) -> None:
        """Non-admin role is rejected by require_admin (no override here)."""
        from auth.dependencies import get_current_user

        async def _non_admin():
            return {"email": "user@example.com", "role": "user"}

        async def _db():
            yield mock_db

        app.dependency_overrides[get_current_user] = _non_admin
        app.dependency_overrides[get_db] = _db
        try:
            tc = TestClient(app, raise_server_exceptions=False)
            resp = tc.post(
                "/api/v1/admin/bm25-drift/1/reveal-terms",
                json={"reason": "Investigating drift alert PSI 0.31"},
            )
            assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()


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
