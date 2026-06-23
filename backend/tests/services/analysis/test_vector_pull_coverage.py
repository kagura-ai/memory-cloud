"""Coverage tests for ``services/analysis/vector_pull`` (Stage [C], Issue #495).

Stage [C] pulls memory metadata rows from Postgres (filter-authoritative) and
their already-computed dense embeddings from Qdrant (vector-authoritative), then
aligns the two into a ``VectorPullResult``.

What these tests target deliberately:

- Pure dataclasses ``MemoryRecord`` / ``VectorPullResult`` — frozen semantics,
  construction, equality.
- ``EmbeddingMismatchError`` — the 422 ValidationError subclass carrying the
  offending model list (currently unreachable in v1, but constructible).
- ``pull_memories_with_vectors`` end to end with a real ``db_session`` and a
  fully faked Qdrant client (no network): the SQL filters
  (``from``/``to``/``types``/``tags``/``min_importance``/``embedding_status``),
  the ``ContextSearchConfig`` collection resolution vs. the default fallback,
  vector alignment by id, the missing-vector drop path, and both
  ``ValueError`` raise paths (empty SQL result, all vectors missing).

Qdrant is mocked by monkeypatching ``vector_pull.get_qdrant_client`` to return a
fake async client whose ``retrieve`` yields ``_FakePoint`` objects. No real
network or Qdrant container is touched.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import numpy as np
import pytest
import pytest_asyncio

from db.qdrant import KAGURA_MEMORIES_COLLECTION, KAGURA_MEMORIES_VECTOR_NAME
from models.memory import Memory
from services.analysis import vector_pull
from services.analysis.vector_pull import (
    EmbeddingMismatchError,
    MemoryRecord,
    VectorPullResult,
    pull_memories_with_vectors,
)
from utils.datetime import utcnow
from utils.exceptions import ValidationError

# ---------------------------------------------------------------------------
# Fakes for Qdrant
# ---------------------------------------------------------------------------


class _FakePoint:
    """Minimal stand-in for a qdrant_client Record/ScoredPoint.

    The source reads ``p.id`` and ``p.vector`` (a dict keyed by vector name).
    """

    def __init__(self, point_id: str, vector):
        self.id = point_id
        self.vector = vector


class _FakeQdrantClient:
    """Async Qdrant client double.

    ``vectors_by_id`` maps point-id string -> dense vector list (or None to
    simulate a point with no dense vector). ``retrieve`` returns only the
    points among ``ids`` that exist in the map, wrapping each dense vector in
    the ``{KAGURA_MEMORIES_VECTOR_NAME: vec}`` named-vector dict the source
    expects.
    """

    def __init__(self, vectors_by_id: dict[str, object]):
        self._vectors_by_id = vectors_by_id
        self.retrieve_calls: list[dict] = []

    async def retrieve(self, *, collection_name, ids, with_vectors, with_payload):
        self.retrieve_calls.append(
            {
                "collection_name": collection_name,
                "ids": list(ids),
                "with_vectors": with_vectors,
                "with_payload": with_payload,
            }
        )
        points = []
        for pid in ids:
            if pid in self._vectors_by_id:
                raw = self._vectors_by_id[pid]
                # raw is either a list (the dense vector), a malformed
                # non-dict vector, or None.
                if raw is None:
                    points.append(_FakePoint(pid, None))
                elif isinstance(raw, dict):
                    points.append(_FakePoint(pid, raw))
                else:
                    points.append(_FakePoint(pid, {KAGURA_MEMORIES_VECTOR_NAME: raw}))
        return points


@pytest.fixture
def patch_qdrant(monkeypatch):
    """Return a factory that installs a ``_FakeQdrantClient`` and hands it back.

    Usage::

        client = patch_qdrant({str(mem.id): [0.1, 0.2, ...]})
    """

    def _install(vectors_by_id: dict[str, object]) -> _FakeQdrantClient:
        fake = _FakeQdrantClient(vectors_by_id)
        monkeypatch.setattr(vector_pull, "get_qdrant_client", lambda: fake)
        return fake

    return _install


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def workspace_id(db_session) -> UUID:
    from models.auth import Workspace

    ws = Workspace(id=uuid4(), name="VP Test", owner_user_id="vp_owner")
    db_session.add(ws)
    await db_session.flush()
    return ws.id


@pytest_asyncio.fixture
async def context_id(db_session, workspace_id) -> UUID:
    from models.auth import Context

    ctx = Context(
        id=uuid4(),
        workspace_id=workspace_id,
        name="vp_ctx",
        display_name="VP Context",
        created_by="vp_user",
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()
    return ctx.id


async def _make_memory(
    db_session,
    *,
    workspace_id: UUID,
    context_id: UUID,
    summary: str = "vp memory",
    mem_type: str = "note",
    importance: float = 0.5,
    tags: list[str] | None = None,
    created_at: datetime | None = None,
    embedding_status: str = "success",
    deleted_at: datetime | None = None,
) -> Memory:
    mem = Memory(
        id=uuid4(),
        workspace_id=workspace_id,
        context_id=context_id,
        user_id="vp_user",
        summary=summary,
        content="vp content",
        type=mem_type,
        importance=importance,
        tags=tags if tags is not None else ["t1"],
        client="test",
        embedding_status=embedding_status,
        deleted_at=deleted_at,
    )
    if created_at is not None:
        mem.created_at = created_at
    db_session.add(mem)
    await db_session.flush()
    return mem


# ===========================================================================
# Pure dataclasses + error type
# ===========================================================================


class TestPureTypes:
    """``MemoryRecord``, ``VectorPullResult``, ``EmbeddingMismatchError``."""

    def test_memory_record_fields_and_equality(self):
        """Frozen dataclass stores all fields; equal records compare equal."""
        mid = uuid4()
        ts = datetime(2026, 1, 2, 3, 4, 5)
        rec = MemoryRecord(
            id=mid,
            type="decision",
            summary="hi",
            tags=["a", "b"],
            importance=0.9,
            created_at=ts,
        )
        assert rec.id == mid
        assert rec.type == "decision"
        assert rec.summary == "hi"
        assert rec.tags == ["a", "b"]
        assert rec.importance == 0.9
        assert rec.created_at == ts

        twin = MemoryRecord(
            id=mid,
            type="decision",
            summary="hi",
            tags=["a", "b"],
            importance=0.9,
            created_at=ts,
        )
        assert rec == twin

    def test_memory_record_is_frozen(self):
        """``frozen=True`` blocks attribute mutation."""
        rec = MemoryRecord(
            id=uuid4(),
            type="note",
            summary="x",
            tags=[],
            importance=0.1,
            created_at=utcnow(),
        )
        with pytest.raises(FrozenInstanceError):
            rec.summary = "mutated"  # type: ignore[misc]

    def test_vector_pull_result_holds_aligned_outputs(self):
        """``VectorPullResult`` carries memories, embeddings, model, and dim."""
        rec = MemoryRecord(
            id=uuid4(),
            type="note",
            summary="x",
            tags=["t"],
            importance=0.5,
            created_at=utcnow(),
        )
        emb = np.zeros((1, 4), dtype=np.float32)
        result = VectorPullResult(
            memories=[rec],
            embeddings=emb,
            embedding_model="text-embedding-3-small",
            embedding_dim=4,
        )
        assert result.memories == [rec]
        assert result.embeddings.shape == (1, 4)
        assert result.embedding_model == "text-embedding-3-small"
        assert result.embedding_dim == 4

    def test_embedding_mismatch_error_is_validation_error(self):
        """Subclass of ValidationError → 422 + VAL-001 + offending model list."""
        models = ["text-embedding-3-small", "voyage-2"]
        err = EmbeddingMismatchError(models)
        assert isinstance(err, ValidationError)
        assert err.status_code == 422
        assert err.error_code == "VAL-001"
        assert err.details["field"] == "embedding_models"
        assert err.details["embedding_models"] == models
        # The offending models are surfaced in the human-readable message.
        assert "text-embedding-3-small" in err.message
        assert "voyage-2" in err.message


# ===========================================================================
# pull_memories_with_vectors — happy path + collection resolution
# ===========================================================================


class TestPullHappyPath:
    """End-to-end happy paths with a real db_session + faked Qdrant."""

    async def test_returns_aligned_result_default_collection(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """No ContextSearchConfig row → default collection + 'unknown' model."""
        base = utcnow()
        m1 = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="first",
            created_at=base - timedelta(minutes=10),
        )
        m2 = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="second",
            created_at=base - timedelta(minutes=5),
        )
        fake = patch_qdrant(
            {
                str(m1.id): [1.0, 0.0, 0.0],
                str(m2.id): [0.0, 1.0, 0.0],
            }
        )

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )

        # Ordered by created_at ascending → m1 then m2.
        assert [r.summary for r in result.memories] == ["first", "second"]
        assert result.embeddings.shape == (2, 3)
        assert result.embeddings.dtype == np.float32
        # Row i lines up with memories[i].
        np.testing.assert_array_equal(result.embeddings[0], [1.0, 0.0, 0.0])
        np.testing.assert_array_equal(result.embeddings[1], [0.0, 1.0, 0.0])
        assert result.embedding_dim == 3
        # No config row → fallback model label.
        assert result.embedding_model == "unknown"
        # Default collection used (no declared model/dim).
        assert fake.retrieve_calls[0]["collection_name"] == KAGURA_MEMORIES_COLLECTION
        # The source fetches vectors but not payload.
        assert fake.retrieve_calls[0]["with_payload"] is False
        assert fake.retrieve_calls[0]["with_vectors"] == [KAGURA_MEMORIES_VECTOR_NAME]

    async def test_uses_context_search_config_collection_and_model(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """A non-default ContextSearchConfig resolves a per-model collection."""
        from db.qdrant import get_collection_name
        from models.config import ContextSearchConfig

        cfg = ContextSearchConfig(
            context_id=context_id,
            semantic_weight=0.6,
            bm25_weight=0.4,
            embedding_model="voyage-3",
            embedding_dimensions=1024,
        )
        db_session.add(cfg)
        await db_session.flush()

        mem = await _make_memory(db_session, workspace_id=workspace_id, context_id=context_id)
        fake = patch_qdrant({str(mem.id): [0.5] * 1024})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )

        expected_collection = get_collection_name("voyage-3", 1024)
        assert expected_collection != KAGURA_MEMORIES_COLLECTION
        assert fake.retrieve_calls[0]["collection_name"] == expected_collection
        # Declared model is surfaced (not the 'unknown' fallback).
        assert result.embedding_model == "voyage-3"
        assert result.embedding_dim == 1024

    async def test_default_model_config_resolves_to_default_collection(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """ContextSearchConfig with the default (model, dim) → legacy collection."""
        from models.config import ContextSearchConfig

        cfg = ContextSearchConfig(
            context_id=context_id,
            semantic_weight=0.6,
            bm25_weight=0.4,
            embedding_model="text-embedding-3-small",
            embedding_dimensions=512,
        )
        db_session.add(cfg)
        await db_session.flush()

        mem = await _make_memory(db_session, workspace_id=workspace_id, context_id=context_id)
        fake = patch_qdrant({str(mem.id): [0.1] * 512})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )
        assert fake.retrieve_calls[0]["collection_name"] == KAGURA_MEMORIES_COLLECTION
        # declared_model is set even though collection is default.
        assert result.embedding_model == "text-embedding-3-small"

    async def test_record_defaults_for_null_summary_tags_importance(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """NULL summary/tags/importance coalesce to ''/[]/0.0 in MemoryRecord."""
        mem = Memory(
            id=uuid4(),
            workspace_id=workspace_id,
            context_id=context_id,
            user_id="vp_user",
            summary="",  # NOT NULL column; empty string exercises ``or ''``
            content="c",
            type="note",
            importance=0.0,
            tags=None,
            client="test",
            embedding_status="success",
        )
        db_session.add(mem)
        await db_session.flush()
        patch_qdrant({str(mem.id): [0.0, 1.0]})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )
        rec = result.memories[0]
        assert rec.summary == ""
        assert rec.tags == []
        assert rec.importance == 0.0
        assert isinstance(rec.tags, list)


# ===========================================================================
# pull_memories_with_vectors — SQL filter branches
# ===========================================================================


class TestPullFilters:
    """Each optional filter narrows the SQL result set."""

    async def test_embedding_status_non_success_excluded(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """Only embedding_status == 'success' rows are pulled."""
        good = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="good",
            embedding_status="success",
        )
        await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="pending",
            embedding_status="pending",
        )
        await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="failed",
            embedding_status="failed",
        )
        patch_qdrant({str(good.id): [1.0, 2.0]})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )
        assert [r.summary for r in result.memories] == ["good"]

    async def test_soft_deleted_excluded(self, db_session, workspace_id, context_id, patch_qdrant):
        """deleted_at IS NOT NULL rows are excluded."""
        live = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="live",
        )
        await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="dead",
            deleted_at=utcnow(),
        )
        patch_qdrant({str(live.id): [1.0]})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )
        assert [r.summary for r in result.memories] == ["live"]

    async def test_from_dt_and_to_dt_window(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """from_dt is inclusive lower bound; to_dt is exclusive upper bound."""
        base = datetime(2026, 3, 1, 12, 0, 0)
        before = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="before",
            created_at=base - timedelta(hours=1),
        )
        at_lower = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="at_lower",
            created_at=base,
        )
        inside = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="inside",
            created_at=base + timedelta(hours=1),
        )
        at_upper = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="at_upper",
            created_at=base + timedelta(hours=2),
        )
        patch_qdrant(
            {
                str(before.id): [1.0],
                str(at_lower.id): [1.0],
                str(inside.id): [1.0],
                str(at_upper.id): [1.0],
            }
        )

        result = await pull_memories_with_vectors(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            from_dt=base,
            to_dt=base + timedelta(hours=2),
        )
        # at_lower included (>= from), at_upper excluded (< to), before excluded.
        assert [r.summary for r in result.memories] == ["at_lower", "inside"]

    async def test_types_allow_list(self, db_session, workspace_id, context_id, patch_qdrant):
        """Only memories whose type is in the allow-list are returned."""
        note = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="note",
            mem_type="note",
        )
        decision = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="decision",
            mem_type="decision",
        )
        await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="code",
            mem_type="code",
        )
        patch_qdrant({str(note.id): [1.0], str(decision.id): [1.0]})

        result = await pull_memories_with_vectors(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            types=["note", "decision"],
        )
        assert {r.type for r in result.memories} == {"note", "decision"}

    async def test_min_importance_threshold(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """importance >= min_importance (boundary inclusive)."""
        low = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="low",
            importance=0.2,
        )
        boundary = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="boundary",
            importance=0.5,
        )
        high = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="high",
            importance=0.9,
        )
        patch_qdrant({str(low.id): [1.0], str(boundary.id): [1.0], str(high.id): [1.0]})

        result = await pull_memories_with_vectors(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            min_importance=0.5,
        )
        assert {r.summary for r in result.memories} == {"boundary", "high"}

    async def test_tags_any_match(self, db_session, workspace_id, context_id, patch_qdrant):
        """tags filter is ANY-match via the Postgres && overlap operator."""
        has_x = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="has_x",
            tags=["x", "z"],
        )
        has_y = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="has_y",
            tags=["y"],
        )
        await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="has_none",
            tags=["w"],
        )
        patch_qdrant({str(has_x.id): [1.0], str(has_y.id): [1.0]})

        result = await pull_memories_with_vectors(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            tags=["x", "y"],
        )
        assert {r.summary for r in result.memories} == {"has_x", "has_y"}

    async def test_workspace_and_context_isolation(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """Rows from a different workspace/context are not pulled."""
        from models.auth import Context, Workspace

        other_ws = Workspace(id=uuid4(), name="Other", owner_user_id="o")
        db_session.add(other_ws)
        await db_session.flush()
        other_ctx = Context(
            id=uuid4(),
            workspace_id=other_ws.id,
            name="other_ctx",
            display_name="Other",
            created_by="o",
            is_private=False,
        )
        db_session.add(other_ctx)
        await db_session.flush()

        mine = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="mine",
        )
        # Foreign rows that must be excluded.
        await _make_memory(
            db_session,
            workspace_id=other_ws.id,
            context_id=other_ctx.id,
            summary="foreign",
        )
        patch_qdrant({str(mine.id): [1.0]})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )
        assert [r.summary for r in result.memories] == ["mine"]


# ===========================================================================
# pull_memories_with_vectors — error / edge branches
# ===========================================================================


class TestPullEdges:
    """Empty result, missing-vector drop, and all-missing raise paths."""

    async def test_empty_sql_result_raises_value_error(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """No matching rows → ValueError before Qdrant is even consulted."""
        patch_qdrant({})  # nothing retrievable
        with pytest.raises(ValueError, match="No memories matched the analysis filters"):
            await pull_memories_with_vectors(
                db_session, workspace_id=workspace_id, context_id=context_id
            )

    async def test_missing_vector_dropped_but_others_kept(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """A row with no Qdrant vector is dropped; the rest still align."""
        base = utcnow()
        keep = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="keep",
            created_at=base - timedelta(minutes=5),
        )
        drop = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="drop",
            created_at=base,
        )
        # ``drop`` has no entry in the Qdrant map → no vector returned.
        patch_qdrant({str(keep.id): [1.0, 2.0, 3.0]})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )
        assert [r.summary for r in result.memories] == ["keep"]
        # The vector-less row is explicitly excluded from the aligned output.
        assert drop.id not in {r.id for r in result.memories}
        assert result.embeddings.shape == (1, 3)

    async def test_point_with_none_vector_is_dropped(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """A retrieved point whose dense vector is None is treated as missing."""
        keep = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="keep",
            created_at=utcnow() - timedelta(minutes=1),
        )
        none_vec = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="none_vec",
            created_at=utcnow(),
        )
        # ``none_vec`` returns a point but with vector=None (not a dict).
        patch_qdrant({str(keep.id): [1.0], str(none_vec.id): None})

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )
        assert [r.summary for r in result.memories] == ["keep"]

    async def test_point_with_non_dict_vector_is_dropped(
        self, db_session, workspace_id, context_id, monkeypatch
    ):
        """A point whose .vector is not a dict yields no named dense vector.

        A bare list (anonymous vector) is not the ``{name: vec}`` dict the
        source requires, so it falls into the ``vec_dict = {}`` branch and the
        row is dropped at alignment time.
        """
        keep = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="keep",
            created_at=utcnow() - timedelta(minutes=1),
        )
        bad = await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="bad",
            created_at=utcnow(),
        )

        class _RawVectorClient:
            def __init__(self):
                self.retrieve_calls = []

            async def retrieve(self, *, collection_name, ids, with_vectors, with_payload):
                self.retrieve_calls.append(list(ids))
                out = []
                for pid in ids:
                    if pid == str(keep.id):
                        out.append(_FakePoint(pid, {KAGURA_MEMORIES_VECTOR_NAME: [1.0]}))
                    elif pid == str(bad.id):
                        out.append(_FakePoint(pid, [9.9, 9.9]))  # non-dict .vector
                return out

        raw_client = _RawVectorClient()
        monkeypatch.setattr(vector_pull, "get_qdrant_client", lambda: raw_client)

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )

        # ``bad`` dropped (non-dict vector); only ``keep`` survives.
        assert [r.summary for r in result.memories] == ["keep"]

    async def test_all_vectors_missing_raises_value_error(
        self, db_session, workspace_id, context_id, patch_qdrant
    ):
        """SQL rows exist but none have a Qdrant vector → ValueError."""
        await _make_memory(
            db_session,
            workspace_id=workspace_id,
            context_id=context_id,
            summary="orphan",
        )
        patch_qdrant({})  # no vectors for any id

        with pytest.raises(ValueError, match="No memories had matching Qdrant vectors"):
            await pull_memories_with_vectors(
                db_session, workspace_id=workspace_id, context_id=context_id
            )

    async def test_multiple_batches_are_all_retrieved(
        self, db_session, workspace_id, context_id, monkeypatch
    ):
        """More than _QDRANT_SCROLL_BATCH ids → multiple retrieve batches.

        Shrink the batch size so a handful of rows forces >1 batch without
        creating thousands of DB rows.
        """
        monkeypatch.setattr(vector_pull, "_QDRANT_SCROLL_BATCH", 2)

        base = utcnow()
        mems = []
        for i in range(5):
            m = await _make_memory(
                db_session,
                workspace_id=workspace_id,
                context_id=context_id,
                summary=f"m{i}",
                created_at=base + timedelta(minutes=i),
            )
            mems.append(m)

        vectors = {str(m.id): [float(i), 0.0] for i, m in enumerate(mems)}
        fake = _FakeQdrantClient(vectors)
        monkeypatch.setattr(vector_pull, "get_qdrant_client", lambda: fake)

        result = await pull_memories_with_vectors(
            db_session, workspace_id=workspace_id, context_id=context_id
        )

        # 5 ids at batch size 2 → 3 retrieve calls (2 + 2 + 1).
        assert len(fake.retrieve_calls) == 3
        assert [r.summary for r in result.memories] == ["m0", "m1", "m2", "m3", "m4"]
        assert result.embeddings.shape == (5, 2)
        # Alignment preserved across batches.
        np.testing.assert_array_equal(result.embeddings[3], [3.0, 0.0])
