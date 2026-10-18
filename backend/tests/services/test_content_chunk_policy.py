"""#2103 / #2080: whether a context's content chunks are active, and the enable gates."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services import content_chunk_policy as policy
from services.content_chunk_policy import ContentChunksStatus
from utils.exceptions import FeatureNotAvailableError, QuotaExceededError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def gate():
    """``gate(True)`` turns CONTENT_CHUNKS_REQUIRE_PLAN on for the test."""
    state = {"on": False}
    with patch.object(
        policy,
        "get_settings",
        lambda: SimpleNamespace(content_chunks_require_plan=state["on"]),
    ):
        yield lambda on: state.__setitem__("on", on)


@pytest.fixture(autouse=True)
def _qdrant():
    with patch.object(policy, "chunk_points_supported", return_value=True) as supported:
        yield supported


def _db(*results):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))

    @asynccontextmanager
    async def _nested():
        yield

    db.begin_nested = MagicMock(side_effect=_nested)
    return db


def _scalar(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _usage(plan, in_use, earlier, workspace_id=None):
    result = MagicMock()
    result.one_or_none.return_value = SimpleNamespace(
        workspace_id=workspace_id or uuid4(), plan_name=plan, in_use=in_use, earlier=earlier
    )
    return result


# ---------------------------------------------------------------------------
# The status truth table
# ---------------------------------------------------------------------------


def _gated(**kw):
    base = {"stored": True, "supported": True, "gated": True, "plan_name": "basic"}
    base.update(plan_has_feature=True, limit=1, in_use=1, earlier=0)
    base.update(kw)
    return ContentChunksStatus(**base)


@pytest.mark.parametrize(
    ("status", "effective", "can_enable", "reason"),
    [
        # Gate off: the flag and the store decide (#2080 exactly).
        (ContentChunksStatus(stored=True, supported=True, gated=False), True, True, None),
        (ContentChunksStatus(stored=False, supported=True, gated=False), False, True, None),
        (
            ContentChunksStatus(stored=True, supported=False, gated=False),
            False,
            False,
            "unsupported",
        ),
        (
            ContentChunksStatus(stored=False, supported=False, gated=False),
            False,
            False,
            "unsupported",
        ),
        # Gated, stored on: the earliest `limit` contexts are active.
        (_gated(earlier=0), True, False, None),
        (_gated(earlier=1, in_use=2), False, False, "cap"),
        (_gated(plan_has_feature=False, limit=0, plan_name="free"), False, False, "plan"),
        (_gated(supported=False), False, False, "unsupported"),
        # Gated, stored off: the toggle needs the feature and a free slot.
        (_gated(stored=False, in_use=0, earlier=None), False, True, None),
        (_gated(stored=False, in_use=1, earlier=None), False, False, "cap"),
        (
            _gated(stored=False, plan_has_feature=False, limit=0, in_use=0, earlier=None),
            False,
            False,
            "plan",
        ),
        # A feature on a tier whose cap is 0 (a PLAN_<KEY>_FEATURES override).
        (_gated(stored=False, limit=0, in_use=0, earlier=None), False, False, "cap"),
        (_gated(limit=0, earlier=0), False, False, "cap"),
    ],
)
def test_status_truth_table(status, effective, can_enable, reason):
    assert status.effective is effective
    assert status.can_enable is can_enable
    assert status.reason == reason


def test_an_uncounted_gated_status_never_claims_it_can_enable():
    assert ContentChunksStatus(stored=False, supported=True, gated=True).can_enable is False


@pytest.mark.parametrize(
    ("plan", "limit"),
    [("free", 0), ("basic", 1), ("pro", 5), ("promax", 20), (None, 0), ("nope", 0)],
)
def test_the_per_plan_limit(plan, limit):
    assert policy.content_chunk_context_limit(plan) == limit


def test_a_tier_without_the_feature_has_no_slots_whatever_its_number():
    with patch.object(policy, "has_feature", return_value=False):
        assert policy.content_chunk_context_limit("pro") == 0


# ---------------------------------------------------------------------------
# content_chunks_status / content_chunks_effective
# ---------------------------------------------------------------------------


async def test_gate_off_reads_only_the_flag(gate):
    db = _db(_scalar(True))
    status = await policy.content_chunks_status(db, uuid4())
    assert status.effective is True
    assert db.execute.await_count == 1


async def test_gate_off_with_a_known_flag_reads_nothing(gate):
    db = _db()
    assert (await policy.content_chunks_status(db, uuid4(), stored=True)).effective is True
    assert await policy.content_chunks_effective(db, uuid4(), stored=True) is True
    assert await policy.content_chunks_effective(db, uuid4(), stored=False) is False
    db.execute.assert_not_awaited()


async def test_gate_off_counts_only_when_asked(gate):
    db = _db(_usage(None, 3, 1))
    status = await policy.content_chunks_status(db, uuid4(), stored=False, include_usage=True)
    assert (status.in_use, status.limit, status.can_enable) == (3, None, True)


async def test_gated_stored_on_reads_plan_and_rank_in_one_query(gate):
    gate(True)
    db = _db(_usage("basic", 2, 1))
    status = await policy.content_chunks_status(db, uuid4(), stored=True)
    assert db.execute.await_count == 1
    assert (status.limit, status.earlier, status.effective, status.reason) == (1, 1, False, "cap")


async def test_gated_stored_off_reads_no_counts_for_effective(gate):
    gate(True)
    db = _db()
    assert await policy.content_chunks_effective(db, uuid4(), stored=False) is False
    db.execute.assert_not_awaited()


async def test_gated_on_a_missing_context_is_inactive(gate):
    gate(True)
    missing = MagicMock()
    missing.one_or_none.return_value = None
    status = await policy.content_chunks_status(_db(missing), uuid4(), stored=True)
    assert status.effective is False


async def test_effective_fails_closed_in_a_savepoint(gate):
    gate(True)
    db = _db(RuntimeError("db down"))
    assert await policy.content_chunks_effective(db, uuid4(), stored=True) is False
    db.begin_nested.assert_called_once()


async def test_the_lancedb_store_has_no_active_chunks(gate, _qdrant):
    _qdrant.return_value = False
    db = _db(_scalar(True))
    assert (await policy.content_chunks_status(db, uuid4())).effective is False
    assert await policy.content_chunks_stored(_db(_scalar(True)), uuid4()) is False


def test_the_usage_query_ranks_by_enable_then_context_id():
    sql = str(
        policy._usage_query(uuid4()).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "coalesce(other.content_chunks_enabled_at, 'infinity'::timestamptz)" in sql
    assert "FILTER (WHERE" in sql
    assert "other_ctx.deleted_at IS NULL" in sql


# ---------------------------------------------------------------------------
# Cross-context recall
# ---------------------------------------------------------------------------


async def test_effective_ids_gate_off_is_the_flag(gate):
    a, b = uuid4(), uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [a]
    db = _db(result)
    assert await policy.content_chunks_effective_ids(db, [a, b]) == {a}


async def test_effective_ids_keep_the_first_limit_per_plan(gate):
    gate(True)
    a, b, c = uuid4(), uuid4(), uuid4()
    result = MagicMock()
    result.all.return_value = [(a, "basic", 1), (b, "basic", 2), (c, "free", 1)]
    db = _db(result)
    assert await policy.content_chunks_effective_ids(db, [a, b, c]) == {a}


async def test_effective_ids_none_on_lancedb(gate, _qdrant):
    _qdrant.return_value = False
    db = _db()
    assert await policy.content_chunks_effective_ids(db, [uuid4()]) == set()
    db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# The enable gates
# ---------------------------------------------------------------------------


def _workspace(plan, workspace_id=None):
    result = MagicMock()
    result.one_or_none.return_value = SimpleNamespace(
        workspace_id=workspace_id or uuid4(), plan_name=plan
    )
    return result


async def test_the_gates_do_nothing_with_the_switch_off(gate):
    db = _db()
    await policy.ensure_content_chunks_plan(db, uuid4())
    await policy.ensure_content_chunk_capacity(db, uuid4())
    assert await policy.contexts_losing_content_chunks(db, uuid4(), "pro", "free") == []
    db.execute.assert_not_awaited()


async def test_a_plan_without_the_feature_is_refused_with_feat001(gate):
    gate(True)
    with pytest.raises(FeatureNotAvailableError) as exc:
        await policy.ensure_content_chunks_plan(_db(_workspace("free")), uuid4())
    assert exc.value.status_code == 403
    assert exc.value.details["gate"] == "plan"
    assert exc.value.details["feature"] == "content_chunks"
    assert exc.value.details["required_plan"] == "basic"
    assert exc.value.details["required_plan_display"] == "M"
    await policy.ensure_content_chunks_plan(_db(_workspace("basic")), uuid4())


async def test_the_cap_locks_then_counts(gate):
    gate(True)
    ws = uuid4()
    db = _db(_workspace("pro", ws), MagicMock(), _scalar(4))
    await policy.ensure_content_chunk_capacity(db, uuid4())
    lock = db.execute.await_args_list[1].args[0]
    assert "pg_advisory_xact_lock" in str(lock)
    assert lock.compile().params == {"key": f"content_chunk_contexts:{ws}"}


async def test_at_the_cap_the_enable_is_refused_with_quota001(gate):
    gate(True)
    db = _db(_workspace("basic"), MagicMock(), _scalar(1))
    with pytest.raises(QuotaExceededError) as exc:
        await policy.ensure_content_chunk_capacity(db, uuid4())
    assert exc.value.status_code == 429
    details = exc.value.details
    assert (details["quota_type"], details["current"], details["limit"]) == (
        "content_chunk_contexts",
        1,
        1,
    )
    assert (details["required_plan"], details["feature"]) == ("pro", "content_chunks")
    assert "1 of the 1 context(s) the M plan allows" in exc.value.message


async def test_the_top_tier_cap_offers_no_upgrade(gate):
    gate(True)
    db = _db(_workspace("promax"), MagicMock(), _scalar(20))
    with pytest.raises(QuotaExceededError) as exc:
        await policy.ensure_content_chunk_capacity(db, uuid4())
    assert exc.value.details["required_plan"] is None
    assert "upgrade" not in exc.value.message


async def test_a_zero_cap_is_refused_without_the_lock(gate):
    gate(True)
    with patch.object(policy, "has_feature", return_value=True):
        db = _db(_workspace("free"), _scalar(0))
        with pytest.raises(QuotaExceededError):
            await policy.ensure_content_chunk_capacity(db, uuid4())
    assert all("pg_advisory" not in str(c.args[0]) for c in db.execute.await_args_list)


def test_a_zero_cap_refusal_says_the_plan_allows_none():
    with pytest.raises(QuotaExceededError) as exc:
        policy._raise_cap("free", 0, 0)
    assert exc.value.message == (
        "The S plan allows no contexts with content chunk vectors. Upgrade to turn them on."
    )
    assert exc.value.details["required_plan"] == "basic"


def test_the_upgrade_names_a_tier_that_has_the_feature():
    """A tier above the limit but without the feature (an override) is skipped."""
    real = policy.has_feature
    with patch.object(policy, "has_feature", lambda plan, f: plan != "pro" and real(plan, f)):
        assert policy._lowest_plan_above(1) == "promax"


@pytest.mark.parametrize(
    ("old", "new", "offset", "limit"),
    [("pro", "basic", 1, 4), ("promax", "free", 0, 20), ("pro", "free", 0, 5)],
)
async def test_a_downgrade_reports_the_ranks_between_the_two_limits(gate, old, new, offset, limit):
    gate(True)
    ids = [uuid4()]
    result = MagicMock()
    result.scalars.return_value.all.return_value = ids
    db = _db(result)
    assert await policy.contexts_losing_content_chunks(db, uuid4(), old, new) == ids
    stmt = db.execute.await_args.args[0]
    assert (stmt._offset_clause.value, stmt._limit_clause.value) == (offset, limit)


@pytest.mark.parametrize(("old", "new"), [("basic", "pro"), ("free", "basic"), ("pro", "pro")])
async def test_an_upgrade_or_no_change_reports_nothing(gate, old, new):
    gate(True)
    db = _db()
    assert await policy.contexts_losing_content_chunks(db, uuid4(), old, new) == []
    db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# v0.113.0 review F1: a new context's default under the plan gate
# ---------------------------------------------------------------------------


async def test_new_context_default_off_or_ungated_reads_nothing(gate):
    db = _db()
    assert await policy.new_context_content_chunks(db, uuid4(), default_on=False) is False
    gate(False)
    assert await policy.new_context_content_chunks(db, uuid4(), default_on=True) is True
    db.execute.assert_not_awaited()


async def test_new_context_default_on_without_the_feature_is_stored_off(gate):
    gate(True)
    db = _db(_scalar("free"))
    assert await policy.new_context_content_chunks(db, uuid4(), default_on=True) is False
    assert db.execute.await_count == 1  # the plan read; no lock, no count


@pytest.mark.parametrize(("in_use", "expected"), [(0, True), (1, False), (3, False)])
async def test_new_context_default_on_takes_a_free_slot_under_the_lock(gate, in_use, expected):
    """M (``basic``) allows one context: the default is stored on only while
    the workspace is below its cap, counted under the cap's advisory lock."""
    gate(True)
    ws = uuid4()
    db = _db(_scalar("basic"), MagicMock(), _scalar(in_use))
    assert await policy.new_context_content_chunks(db, ws, default_on=True) is expected
    lock_stmt = db.execute.await_args_list[1].args[0]
    assert "pg_advisory_xact_lock" in str(lock_stmt)
    assert lock_stmt.compile().params["key"] == f"content_chunk_contexts:{ws}"


async def test_create_context_stores_the_gated_default(monkeypatch):
    """create_context writes what the gate allows, not the raw setting."""
    from config.settings import Settings
    from models.config import ContextSearchConfig
    from services.context_service import ContextService

    for key in ("DEFAULT_CONTENT_CHUNKS_ENABLED", "KAGURA_VECTOR_BACKEND", "VECTOR_BACKEND"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None, default_content_chunks_enabled=True)
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    service = ContextService(db=db)
    gated = AsyncMock(return_value=False)
    with (
        patch("services.context_service.get_settings", return_value=settings),
        patch.object(policy, "new_context_content_chunks", gated),
        patch.object(type(service), "validate_context_name", MagicMock(return_value=None)),
        patch.object(service, "get_context_by_name_for_workspace", AsyncMock(return_value=None)),
    ):
        ws = uuid4()
        await service.create_context(
            workspace_id=ws, name="fresh", is_private=True, create_collection=False
        )
    (config,) = [
        c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], ContextSearchConfig)
    ]
    assert config.content_chunks_enabled is False and config.content_chunks_enabled_at is None
    assert gated.await_args.args[1] == ws
    assert gated.await_args.kwargs == {"default_on": True}


# ---------------------------------------------------------------------------
# v0.113.0 review F2: a same-context double enable
# ---------------------------------------------------------------------------


async def test_a_second_enable_of_the_same_context_keeps_the_first_one(monkeypatch):
    """The row was loaded (off) before the cap's lock; the first enable
    committed meanwhile. The update re-reads the flag under the row lock and
    sees it on: no new stamp, no second purge, no second backfill."""
    from models.schemas import ContextSearchConfigUpdate
    from repositories.config_repository import ContextSearchConfigRepository

    stamp = object()
    stale = SimpleNamespace(
        content_chunks_enabled=False,
        content_chunks_enabled_at=None,
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
    )
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    refreshed: list[tuple] = []

    async def refresh(obj, attribute_names=None, with_for_update=None):
        refreshed.append((tuple(attribute_names or ()), with_for_update))
        if attribute_names:  # the committed values
            obj.content_chunks_enabled, obj.content_chunks_enabled_at = True, stamp

    db.refresh = refresh
    repo = ContextSearchConfigRepository(db)
    monkeypatch.setattr(repo, "get_by_context", AsyncMock(return_value=stale))
    purge, backfill = AsyncMock(), AsyncMock(return_value=0)
    monkeypatch.setattr(repo, "_purge_chunk_points", purge)
    monkeypatch.setattr(repo, "_queue_content_chunk_backfill", backfill)

    enable = ContextSearchConfigUpdate.model_construct(
        _fields_set={"content_chunks_enabled"}, content_chunks_enabled=True
    )
    await repo.update(uuid4(), enable)

    assert refreshed[0] == (("content_chunks_enabled", "content_chunks_enabled_at"), True)
    assert stale.content_chunks_enabled_at is stamp
    purge.assert_not_awaited()
    backfill.assert_not_awaited()
