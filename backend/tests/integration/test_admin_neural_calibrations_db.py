"""``GET /api/v1/admin/neural/calibrations`` on real Postgres (#2845).

Over HTTP against the real route, as a session admin and as a service
operator (``require_admin`` is overridden with each principal it builds):

- every model-global row is answered, ``knn_seed`` and ``edge_gate`` alike,
  ordered by ``model_name``, ``dimensions``, ``kind``;
- ``expired`` equals ``is_expired()`` and ``threshold`` equals what
  ``resolve_knn_threshold`` / ``resolve_edge_threshold`` return for the pair;
  ``knn_seed_override`` holds ``knn_seed_min_similarity`` when it is set;
- the read queues no recalibration, also for an expired row;
- a per-context row is never answered, and no row id or context id is in
  the raw body;
- after ``compute_calibration`` writes a pair the read shows the new values,
  and after a run that writes nothing the read is unchanged.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import neural.calibration as calibration_module
import tasks.neural_calibration as calibration_task
from api.routes import admin_neural
from auth.dependencies import require_admin
from auth.system_admin_mode import build_operator_principal
from db.base import get_db
from models.auth import Context, User, Workspace
from models.neural import (
    CALIBRATION_KIND_EDGE_GATE,
    CALIBRATION_KIND_KNN_SEED,
    EmbeddingCalibration,
)
from neural.calibration import resolve_edge_threshold, resolve_knn_threshold
from neural.config import NeuralMemoryConfig

pytestmark = pytest.mark.asyncio(loop_scope="session")

URL = "/api/v1/admin/neural/calibrations"
PRINCIPALS = {
    "session": {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"},
    "service": build_operator_principal("ops@example.com"),
}
MODES = sorted(PRINCIPALS)
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
KNN_PERCENTILES = {"p25": 0.41, "p50": 0.48, "p75": 0.55, "p90": 0.63, "p95": 0.67, "p99": 0.74}
EDGE_PERCENTILES = {"p25": 0.05, "p50": 0.11, "p75": 0.19, "p90": 0.27, "p95": 0.33, "p99": 0.45}


@pytest_asyncio.fixture(loop_scope="session")
async def maker(async_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


def _client(maker, principal: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(admin_neural.router, prefix="/api/v1")

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_admin] = lambda: principal
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def config(monkeypatch) -> NeuralMemoryConfig:
    """The answering process's config: non-default percentiles, no override."""
    cfg = NeuralMemoryConfig()
    cfg.knn_seed_min_similarity = None
    cfg.knn_seed_min_percentile = 82.5
    cfg.knn_seed_min_similarity_floor = 0.3
    cfg.min_similarity_for_edge_percentile = 97.0
    cfg.min_similarity_for_edge_floor = 0.2
    monkeypatch.setattr(NeuralMemoryConfig, "from_db", AsyncMock(return_value=cfg))
    return cfg


@pytest.fixture
def no_enqueue(monkeypatch) -> SimpleNamespace:
    """Fail-safe stand-ins for every way a recalibration can be queued."""
    enqueue = AsyncMock(return_value=True)
    lazy = AsyncMock()
    monkeypatch.setattr(admin_neural, "enqueue_recalibration_dedup", enqueue)
    monkeypatch.setattr(calibration_task, "enqueue_recalibration_dedup", enqueue)
    monkeypatch.setattr(calibration_module, "_enqueue_lazy_recalibration", lazy)
    return SimpleNamespace(enqueue=enqueue, lazy=lazy)


def _calibration(model: str, dims: int, kind: str, **cols) -> EmbeddingCalibration:
    now = datetime.now(UTC)
    percentiles = KNN_PERCENTILES if kind == CALIBRATION_KIND_KNN_SEED else EDGE_PERCENTILES
    values = {
        "sample_size": 10000,
        "sampled_at": now - timedelta(days=2),
        "valid_until": now + timedelta(days=28),
        **percentiles,
    }
    values.update(cols)
    return EmbeddingCalibration(model_name=model, dimensions=dims, kind=kind, **values)


@pytest_asyncio.fixture(loop_scope="session")
async def world(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """Four model-global rows of two models, and one per-context row."""
    tag = uuid4().hex[:8]
    model_a, model_b = f"cal-{tag}-a", f"cal-{tag}-b"
    per_context_model = f"percontext-{secrets.token_hex(8)}"
    owner = f"cal-owner-{tag}"
    db_session.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db_session.flush()
    workspace = Workspace(
        id=uuid4(),
        name=f"cal-ws-{tag}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(workspace)
    await db_session.flush()
    context = Context(id=uuid4(), workspace_id=workspace.id, name=f"cal-{tag}", created_by=owner)
    db_session.add(context)
    await db_session.flush()
    now = datetime.now(UTC)
    db_session.add_all(
        [
            _calibration(model_b, 512, CALIBRATION_KIND_KNN_SEED),
            _calibration(model_b, 512, CALIBRATION_KIND_EDGE_GATE),
            # Expired: still served by the resolvers.
            _calibration(
                model_a,
                1024,
                CALIBRATION_KIND_KNN_SEED,
                sampled_at=now - timedelta(days=40),
                valid_until=now - timedelta(days=10),
            ),
            _calibration(
                model_a,
                256,
                CALIBRATION_KIND_EDGE_GATE,
                sampled_at=now - timedelta(days=45),
                valid_until=now - timedelta(days=15),
                sample_size=4321,
            ),
            _calibration(per_context_model, 512, CALIBRATION_KIND_KNN_SEED, context_id=context.id),
            # The same pair as a model-global row, per context: only the
            # model-global one may be answered.
            _calibration(
                model_b, 512, CALIBRATION_KIND_KNN_SEED, context_id=context.id, sample_size=77
            ),
        ]
    )
    await db_session.commit()
    ids = SimpleNamespace(
        tag=tag,
        model_a=model_a,
        model_b=model_b,
        per_context_model=per_context_model,
        context_id=context.id,
        workspace_id=workspace.id,
        owner=owner,
    )
    try:
        yield ids
    finally:
        await db_session.rollback()
        await db_session.execute(
            delete(EmbeddingCalibration).where(
                EmbeddingCalibration.model_name.in_([model_a, model_b, per_context_model])
            )
        )
        await db_session.execute(delete(Workspace).where(Workspace.id == ids.workspace_id))
        await db_session.execute(delete(User).where(User.user_id == owner))
        await db_session.commit()


async def _read(maker, mode: str) -> httpx.Response:
    async with _client(maker, PRINCIPALS[mode]) as client:
        return await client.get(URL)


def _mine(response: httpx.Response, world) -> list[dict]:
    return [
        item
        for item in response.json()["calibrations"]
        if item["model_name"] in {world.model_a, world.model_b}
    ]


@pytest.mark.parametrize("mode", MODES)
async def test_every_model_global_row_is_answered_in_order(maker, world, config, mode):
    response = await _read(maker, mode)

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"knn_seed_override", "calibrations"}
    mine = _mine(response, world)
    assert [(item["model_name"], item["dimensions"], item["kind"]) for item in mine] == [
        (world.model_a, 256, "edge_gate"),
        (world.model_a, 1024, "knn_seed"),
        (world.model_b, 512, "edge_gate"),
        (world.model_b, 512, "knn_seed"),
    ]
    for item in mine:
        assert set(item) == ITEM_KEYS
        expected = KNN_PERCENTILES if item["kind"] == "knn_seed" else EDGE_PERCENTILES
        assert {key: item[key] for key in expected} == expected
    assert [item["sample_size"] for item in mine] == [4321, 10000, 10000, 10000]


@pytest.mark.parametrize("mode", MODES)
async def test_expired_and_threshold_match_the_model_and_the_resolvers(
    maker, world, config, no_enqueue, db_session: AsyncSession, mode
):
    response = await _read(maker, mode)

    assert response.status_code == 200, response.text
    assert response.json()["knn_seed_override"] is None
    stored = {
        (row.model_name, row.dimensions, row.kind): row
        for row in (
            await db_session.execute(
                select(EmbeddingCalibration).where(
                    EmbeddingCalibration.context_id.is_(None),
                    EmbeddingCalibration.model_name.in_([world.model_a, world.model_b]),
                )
            )
        ).scalars()
    }
    mine = _mine(response, world)
    assert len(mine) == len(stored) == 4
    for item in mine:
        row = stored[(item["model_name"], item["dimensions"], item["kind"])]
        assert item["expired"] is row.is_expired()
        resolver = resolve_knn_threshold if item["kind"] == "knn_seed" else resolve_edge_threshold
        resolved = await resolver(db_session, config, item["model_name"], item["dimensions"])
        assert item["threshold"] == pytest.approx(resolved)
    await db_session.rollback()
    assert {(item["model_name"], item["expired"]) for item in mine} == {
        (world.model_a, True),
        (world.model_b, False),
    }
    # The two formulas differ: the knn percentile sits between p75 and p90,
    # the edge one between p95 and p99.
    by_kind = {item["kind"]: item["threshold"] for item in mine if item["dimensions"] == 512}
    assert by_kind["knn_seed"] == pytest.approx(0.59)
    assert by_kind["edge_gate"] == pytest.approx(0.39)


async def test_the_override_is_answered_when_it_is_set(maker, world, config):
    config.knn_seed_min_similarity = 0.42

    response = await _read(maker, "service")

    assert response.status_code == 200, response.text
    assert response.json()["knn_seed_override"] == 0.42
    # The rows are still answered with what they give.
    assert len(_mine(response, world)) == 4


@pytest.mark.parametrize("mode", MODES)
async def test_the_read_queues_no_recalibration_for_an_expired_row(
    maker, world, config, no_enqueue, mode
):
    before = set(calibration_module._LAZY_TTL_TASKS)

    response = await _read(maker, mode)

    assert response.status_code == 200, response.text
    assert any(item["expired"] for item in _mine(response, world))
    no_enqueue.enqueue.assert_not_awaited()
    no_enqueue.lazy.assert_not_called()
    assert set(calibration_module._LAZY_TTL_TASKS) == before


@pytest.mark.parametrize("mode", MODES)
async def test_a_per_context_row_and_every_identifier_stay_out_of_the_body(
    maker, world, config, db_session: AsyncSession, mode
):
    row_ids = list((await db_session.execute(select(EmbeddingCalibration.id))).scalars())
    await db_session.rollback()
    assert len(row_ids) >= 6

    response = await _read(maker, mode)

    assert response.status_code == 200, response.text
    body = response.text
    assert world.per_context_model not in body
    assert str(world.context_id) not in body
    assert str(world.workspace_id) not in body
    assert world.owner not in body
    for row_id in row_ids:
        assert str(row_id) not in body
    for item in response.json()["calibrations"]:
        assert set(item) == ITEM_KEYS
    # The per-context twin of a model-global pair is not answered in its place.
    knn_b = [
        item
        for item in _mine(response, world)
        if (item["model_name"], item["kind"]) == (world.model_b, "knn_seed")
    ]
    assert [item["sample_size"] for item in knn_b] == [10000]


async def test_the_read_follows_what_compute_calibration_wrote(
    maker, world, config, db_session: AsyncSession, monkeypatch
):
    """A run that writes shows in the read; a run that writes nothing leaves
    the read as it was."""
    model, dims = world.model_b, 512
    new_knn = {"p25": 0.30, "p50": 0.36, "p75": 0.44, "p90": 0.52, "p95": 0.58, "p99": 0.66}
    state = {
        "context": (world.context_id, 500),
        "observations": calibration_task.BOOTSTRAP_MIN_OBSERVATIONS,
    }
    sampled = [SimpleNamespace(id=uuid4()) for _ in range(3)]

    async def sample_memories(_db, _context_id, _n):
        return sampled

    async def fetch_vectors(_qdrant, _collection, _ids):
        return {str(m.id): [0.1, 0.2] for m in sampled}

    async def measure_top_k(_mems, _vectors, _collection, _k):
        return [0.5] * state["observations"], len(sampled)

    script = SimpleNamespace(
        compute_percentiles=lambda _scores: dict(new_knn),
        fetch_vectors=fetch_vectors,
        measure_top_k=measure_top_k,
        measure_random_pair=lambda _vectors, _n: (
            [0.2] * calibration_task.EDGE_GATE_MIN_PAIR_OBSERVATIONS
        ),
        sample_memories=sample_memories,
    )

    async def pick(_db, _model, _dims):
        return state["context"]

    monkeypatch.setattr(calibration_task, "_load_measure_script", lambda: script)
    monkeypatch.setattr(calibration_task, "get_qdrant_client", lambda: MagicMock())
    monkeypatch.setattr(calibration_task, "_pick_largest_context_for_model", pick)

    def pair(response: httpx.Response) -> dict[str, dict]:
        return {
            item["kind"]: item
            for item in response.json()["calibrations"]
            if (item["model_name"], item["dimensions"]) == (model, dims)
        }

    before = pair(await _read(maker, "service"))
    assert {key: before["knn_seed"][key] for key in KNN_PERCENTILES} == KNN_PERCENTILES

    written = await calibration_task.compute_calibration(db_session, model, dims, None)
    assert written is not None

    after = pair(await _read(maker, "service"))
    assert {key: after["knn_seed"][key] for key in new_knn} == new_knn
    assert after["knn_seed"]["sampled_at"] != before["knn_seed"]["sampled_at"]
    assert after["knn_seed"]["sampled_at"] > before["knn_seed"]["sampled_at"]
    assert after["knn_seed"]["sample_size"] == calibration_task.BOOTSTRAP_MIN_OBSERVATIONS
    assert after["knn_seed"]["expired"] is False
    assert after["edge_gate"]["sampled_at"] == after["knn_seed"]["sampled_at"]

    # No context uses the pair: nothing is written.
    state["context"] = None
    assert await calibration_task.compute_calibration(db_session, model, dims, None) is None
    await db_session.rollback()
    assert pair(await _read(maker, "service")) == after

    # The sample is below the gate: nothing is written.
    state["context"] = (world.context_id, 500)
    state["observations"] = 10
    assert await calibration_task.compute_calibration(db_session, model, dims, None) is None
    await db_session.rollback()
    assert pair(await _read(maker, "service")) == after
