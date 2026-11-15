"""``GET /api/v1/admin/neural/calibrations``: the model-global calibration rows (#2845).

The route answers named fields only (``model_name``, ``dimensions``, ``kind``,
the six percentiles, ``sample_size``, ``sampled_at``, ``valid_until``,
``expired``, ``threshold``) and ``knn_seed_override`` at the top level. It
never answers a row id, a ``context_id`` or a per-context row, and it starts
no recalibration. The database is mocked here; the rows on real Postgres are
in ``tests/integration/test_admin_neural_calibrations_db.py``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from structlog.testing import capture_logs

from api.main import app
from auth.dependencies import get_current_user_optional, require_admin
from config.settings import get_settings
from db.base import get_db
from models.data_boundary import EXPORT_SURFACE_SCHEMA_NAMES
from neural.config import NeuralMemoryConfig

URL = "/api/v1/admin/neural/calibrations"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
OPERATOR = "ops@example.com"
ITEM_KEYS = {
    "model_name",
    "dimensions",
    "kind",
    "p25",
    "p50",
    "p75",
    "p90",
    "p95",
    "p99",
    "sample_size",
    "sampled_at",
    "valid_until",
    "expired",
    "threshold",
}
NOW = datetime.now(UTC)


def _row(kind: str = "knn_seed", *, model: str = "embed-small", dims: int = 512, **cols):
    """One selected ``embedding_calibrations`` row (named columns)."""
    values = {
        "model_name": model,
        "dimensions": dims,
        "kind": kind,
        "p25": 0.40,
        "p50": 0.50,
        "p75": 0.60,
        "p90": 0.70,
        "p95": 0.80,
        "p99": 0.90,
        "sample_size": 10000,
        "sampled_at": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=29),
    }
    values.update(cols)
    return SimpleNamespace(**values)


def _db(rows) -> AsyncMock:
    mock_db = AsyncMock()
    result = MagicMock()
    result.all.return_value = list(rows)
    mock_db.execute.return_value = result
    mock_db.add = MagicMock()
    return mock_db


def _sql(mock_db) -> str:
    stmt = mock_db.execute.await_args_list[0].args[0]
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.fixture
def config(monkeypatch) -> NeuralMemoryConfig:
    """The answering process's config, without a ``neural_config`` read."""
    cfg = NeuralMemoryConfig()
    monkeypatch.setattr(NeuralMemoryConfig, "from_db", AsyncMock(return_value=cfg))
    return cfg


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _install(mock_db, *, admin: bool = True) -> None:
    async def _get_db():
        yield mock_db

    app.dependency_overrides[get_db] = _get_db
    if admin:
        app.dependency_overrides[require_admin] = lambda: dict(ADMIN_SESSION)


@pytest.fixture
def gate(monkeypatch):
    def _apply(mode: str, token: str = "") -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "system_admin_mode", mode)
        monkeypatch.setattr(settings, "system_admin_service_token", token)
        monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
        monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")

    return _apply


class TestAnswer:
    def test_answers_each_row_with_the_named_fields(self, client, config):
        _install(_db([_row("edge_gate"), _row("knn_seed")]))

        response = client.get(URL)

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"knn_seed_override", "calibrations"}
        assert body["knn_seed_override"] is None
        assert [item["kind"] for item in body["calibrations"]] == ["edge_gate", "knn_seed"]
        for item in body["calibrations"]:
            assert set(item) == ITEM_KEYS
            assert item["model_name"] == "embed-small"
            assert item["dimensions"] == 512
            assert (item["p25"], item["p50"], item["p75"]) == (0.40, 0.50, 0.60)
            assert (item["p90"], item["p95"], item["p99"]) == (0.70, 0.80, 0.90)
            assert item["sample_size"] == 10000
            assert item["expired"] is False
            # Timezone-aware on the wire.
            assert item["sampled_at"].endswith(("Z", "+00:00"))
            assert item["valid_until"].endswith(("Z", "+00:00"))

    def test_no_row_answers_an_empty_list(self, client, config):
        _install(_db([]))

        response = client.get(URL)

        assert response.status_code == 200, response.text
        assert response.json() == {"knn_seed_override": None, "calibrations": []}

    def test_takes_no_query_parameter_into_account(self, client, config):
        mock_db = _db([_row()])
        _install(mock_db)

        plain = client.get(URL)
        with_params = client.get(URL, params={"context_id": "x", "limit": "1", "kind": "nope"})

        assert with_params.status_code == 200
        assert with_params.json() == plain.json()


class TestThreshold:
    """``threshold`` is what the row gives with the answering process's config."""

    def test_knn_seed_threshold_is_the_configured_percentile(self, client, config):
        config.knn_seed_min_percentile = 90.0
        config.knn_seed_min_similarity_floor = 0.3
        _install(_db([_row("knn_seed")]))

        item = client.get(URL).json()["calibrations"][0]

        assert item["threshold"] == pytest.approx(0.70)

    def test_knn_seed_threshold_interpolates_between_stored_percentiles(self, client, config):
        config.knn_seed_min_percentile = 82.5  # halfway between p75 and p90
        _install(_db([_row("knn_seed")]))

        item = client.get(URL).json()["calibrations"][0]

        assert item["threshold"] == pytest.approx(0.65)

    def test_knn_seed_floor_wins_over_a_low_percentile(self, client, config):
        config.knn_seed_min_percentile = 90.0
        config.knn_seed_min_similarity_floor = 0.75
        _install(_db([_row("knn_seed")]))

        item = client.get(URL).json()["calibrations"][0]

        assert item["threshold"] == pytest.approx(0.75)

    def test_edge_gate_threshold_uses_the_edge_settings(self, client, config):
        config.min_similarity_for_edge_percentile = 95.0
        config.min_similarity_for_edge_floor = 0.3
        # The knn settings differ, so a mix-up of the two formulas shows.
        config.knn_seed_min_percentile = 50.0
        config.knn_seed_min_similarity_floor = 0.99
        _install(_db([_row("edge_gate")]))

        item = client.get(URL).json()["calibrations"][0]

        assert item["threshold"] == pytest.approx(0.80)

    def test_edge_gate_floor_wins_over_a_low_percentile(self, client, config):
        config.min_similarity_for_edge_percentile = 95.0
        config.min_similarity_for_edge_floor = 0.85
        _install(_db([_row("edge_gate")]))

        item = client.get(URL).json()["calibrations"][0]

        assert item["threshold"] == pytest.approx(0.85)

    def test_override_is_answered_and_the_rows_threshold_stays_the_rows(self, client, config):
        config.knn_seed_min_similarity = 0.42
        _install(_db([_row("knn_seed"), _row("edge_gate")]))

        body = client.get(URL).json()

        assert body["knn_seed_override"] == 0.42
        knn = next(item for item in body["calibrations"] if item["kind"] == "knn_seed")
        assert knn["threshold"] == pytest.approx(0.70)


class TestExpired:
    def test_expired_follows_valid_until(self, client, config):
        _install(
            _db(
                [
                    _row("knn_seed", model="a", valid_until=NOW - timedelta(seconds=5)),
                    _row("knn_seed", model="b", valid_until=NOW + timedelta(days=1)),
                    _row("edge_gate", model="c", valid_until=NOW - timedelta(days=40)),
                ]
            )
        )

        items = client.get(URL).json()["calibrations"]

        assert [item["expired"] for item in items] == [True, False, True]

    def test_an_expired_row_queues_no_recalibration(self, client, config, monkeypatch):
        import neural.calibration as calibration_module

        enqueue = AsyncMock(return_value=True)
        monkeypatch.setattr("api.routes.admin_neural.enqueue_recalibration_dedup", enqueue)
        monkeypatch.setattr("tasks.neural_calibration.enqueue_recalibration_dedup", enqueue)
        lazy = AsyncMock()
        monkeypatch.setattr(calibration_module, "_enqueue_lazy_recalibration", lazy)
        before = set(calibration_module._LAZY_TTL_TASKS)
        mock_db = _db([_row("knn_seed", valid_until=NOW - timedelta(days=3))])
        _install(mock_db)

        response = client.get(URL)

        assert response.status_code == 200, response.text
        assert response.json()["calibrations"][0]["expired"] is True
        enqueue.assert_not_awaited()
        lazy.assert_not_called()
        assert set(calibration_module._LAZY_TTL_TASKS) == before
        # A read: nothing added, flushed or committed.
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_awaited()
        mock_db.flush.assert_not_awaited()


class TestDisclosure:
    def test_the_statement_reads_model_global_rows_by_named_columns(self, client, config):
        mock_db = _db([])
        _install(mock_db)

        assert client.get(URL).status_code == 200

        assert mock_db.execute.await_count == 1
        sql = _sql(mock_db)
        select_list = sql.split("FROM")[0]
        assert "embedding_calibrations.context_id IS NULL" in sql
        assert "context_id" not in select_list
        assert "embedding_calibrations.id" not in select_list
        assert (
            "ORDER BY embedding_calibrations.model_name, embedding_calibrations.dimensions, "
            "embedding_calibrations.kind"
        ) in " ".join(sql.split())
        assert "LIMIT" not in sql

    def test_the_statement_is_limited_to_the_two_kinds(self, client, config):
        mock_db = _db([])
        _install(mock_db)

        assert client.get(URL).status_code == 200

        sql = _sql(mock_db)
        assert "embedding_calibrations.kind IN ('knn_seed', 'edge_gate')" in sql

    def test_an_attribute_the_model_does_not_name_is_never_answered(self, client, config):
        """Even if a row object carried an id or a context id, the answer is
        built field by field."""
        sentinel_id = f"row-id-{secrets.token_hex(8)}"
        sentinel_context = f"context-{secrets.token_hex(8)}"
        _install(_db([_row("knn_seed", id=sentinel_id, context_id=sentinel_context)]))

        response = client.get(URL)

        assert response.status_code == 200, response.text
        assert sentinel_id not in response.text
        assert sentinel_context not in response.text
        assert set(response.json()["calibrations"][0]) == ITEM_KEYS

    def test_the_response_models_are_not_on_the_export_surface(self):
        from api.routes import admin_neural

        for model in (
            admin_neural.EmbeddingCalibrationItem,
            admin_neural.EmbeddingCalibrationListResponse,
        ):
            assert model.__name__ not in EXPORT_SURFACE_SCHEMA_NAMES
            assert model.model_config.get("extra") == "forbid"
        assert set(admin_neural.EmbeddingCalibrationItem.model_fields) == ITEM_KEYS
        assert set(admin_neural.EmbeddingCalibrationListResponse.model_fields) == {
            "knn_seed_override",
            "calibrations",
        }

    def test_the_log_line_names_the_caller_and_the_count_only(self, client, config):
        model = f"model-{secrets.token_hex(6)}"
        _install(_db([_row("knn_seed", model=model)]))

        with capture_logs() as logs:
            response = client.get(URL)

        assert response.status_code == 200, response.text
        [entry] = [e for e in logs if e["event"] == "neural_calibrations_listed"]
        assert entry["count"] == 1
        assert entry["admin"] == ADMIN_SESSION["user_id"]
        assert model not in repr(entry)


class TestGate:
    def test_session_mode_answers_a_system_admin(self, client, config, gate):
        gate("session")
        _install(_db([_row()]), admin=False)
        app.dependency_overrides[get_current_user_optional] = lambda: dict(ADMIN_SESSION)

        response = client.get(URL)

        assert response.status_code == 200, response.text
        assert len(response.json()["calibrations"]) == 1

    def test_session_mode_refuses_a_non_admin(self, client, config, gate):
        gate("session")
        mock_db = _db([_row()])
        _install(mock_db, admin=False)
        app.dependency_overrides[get_current_user_optional] = lambda: {
            **ADMIN_SESSION,
            "role": "user",
        }

        response = client.get(URL)

        assert response.status_code == 403
        mock_db.execute.assert_not_awaited()

    def test_service_mode_answers_an_operator(self, client, config, gate):
        token = secrets.token_hex(32)
        gate("service", token)
        _install(_db([_row()]), admin=False)

        with capture_logs() as logs:
            response = client.get(
                URL, headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
            )

        assert response.status_code == 200, response.text
        assert len(response.json()["calibrations"]) == 1
        [entry] = [e for e in logs if e["event"] == "neural_calibrations_listed"]
        assert entry["admin"] == f"operator:{OPERATOR}"

    def test_off_mode_is_an_unknown_path(self, client, config, gate):
        gate("off")
        mock_db = _db([_row()])
        _install(mock_db, admin=False)
        app.dependency_overrides[get_current_user_optional] = lambda: dict(ADMIN_SESSION)

        response = client.get(URL)

        assert response.status_code == 404
        assert response.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}
        mock_db.execute.assert_not_awaited()

    def test_the_route_is_on_the_admin_surface(self):
        from tests.api.test_system_admin_surface import ADMIN_SURFACE, OWN_ACCOUNT_ROUTES

        assert ("GET", URL) in ADMIN_SURFACE
        assert ("GET", URL) not in OWN_ACCOUNT_ROUTES
