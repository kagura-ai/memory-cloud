"""Dual-collection embedding migration (#1525).

Everything downstream of the service — the embedding provider, Qdrant, the
DB — is mocked at the seam the service actually calls, so these tests pin the
*orchestration*: what is refused, which collection each write goes to, that
routing is not touched until ``switch``, and that ``switch`` re-queues exactly
the rows the bulk pass could not have seen.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from services import embedding_migration_service as svc
from services.embedding_migration_service import MigrationPlan
from utils.exceptions import NotFoundException, ValidationError

SMALL = "text-embedding-3-small"
QWEN = "qwen3-embedding:4b"


def _result(*, scalar=None, scalars=None, rowcount=0):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar)
    result.scalar_one = MagicMock(return_value=scalar)
    result.scalars = MagicMock(
        return_value=MagicMock(all=MagicMock(return_value=list(scalars or [])))
    )
    result.rowcount = rowcount
    return result


def _db(results):
    """A session whose successive ``execute`` calls return ``results`` in order."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.commit = AsyncMock()
    db.add = MagicMock()
    db.expunge_all = MagicMock()
    return db


def _context(workspace_id: UUID | None = None):
    context = MagicMock()
    context.workspace_id = workspace_id or uuid4()
    return context


def _config(model: str, dims: int):
    config = MagicMock()
    config.embedding_model = model
    config.embedding_dimensions = dims
    return config


def _memory(user_id: str = "u1", memory_id: UUID | None = None, summary: str = "s"):
    memory = MagicMock()
    memory.id = memory_id or uuid4()
    memory.user_id = user_id
    memory.summary = summary
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    return memory


def _plan(**overrides) -> MigrationPlan:
    base = {
        "context_id": uuid4(),
        "workspace_id": uuid4(),
        "source_model": SMALL,
        "source_dimensions": 512,
        "source_collection": "kagura_memories",
        "target_model": QWEN,
        "target_dimensions": 2560,
        "target_collection": "kagura_memories_qwen3_embedding_4b_2560",
        "memory_count": 3,
    }
    base.update(overrides)
    return MigrationPlan(**base)


# --------------------------------------------------------------------------- plan


class TestPlan:
    @pytest.mark.asyncio
    async def test_unknown_target_is_refused_before_touching_the_db(self):
        db = _db([])
        with pytest.raises(ValidationError):
            await svc.plan_context_migration(db, uuid4(), "not-a-model")
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_outside_the_deployment_allowlist_is_refused(self):
        db = _db([])
        with pytest.raises(ValidationError):
            await svc.plan_context_migration(db, uuid4(), QWEN, allowlist_setting=SMALL)
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_context(self):
        db = _db([_result(scalar=None)])
        with pytest.raises(NotFoundException):
            await svc.plan_context_migration(db, uuid4(), QWEN)

    @pytest.mark.asyncio
    async def test_same_model_is_a_no_op_refusal(self):
        db = _db([_result(scalar=_context()), _result(scalar=_config(QWEN, 2560))])
        with pytest.raises(ValidationError):
            await svc.plan_context_migration(db, uuid4(), QWEN)

    @pytest.mark.asyncio
    async def test_legacy_context_without_config_row_plans_from_the_fallback(self):
        workspace_id = uuid4()
        context_id = uuid4()
        db = _db(
            [_result(scalar=_context(workspace_id)), _result(scalar=None), _result(scalar=8261)]
        )
        plan = await svc.plan_context_migration(db, context_id, QWEN)
        assert plan.workspace_id == workspace_id
        assert (plan.source_model, plan.source_dimensions) == (SMALL, 512)
        assert plan.source_collection == "kagura_memories"
        assert (plan.target_model, plan.target_dimensions) == (QWEN, 2560)
        assert plan.target_collection == "kagura_memories_qwen3_embedding_4b_2560"
        assert plan.memory_count == 8261

    @pytest.mark.asyncio
    async def test_explicit_source_equal_to_the_active_model_is_refused(self):
        # After A -> B switched the context routes to QWEN; naming QWEN as the
        # source would purge the collection it serves from.
        db = _db([_result(scalar=_context()), _result(scalar=_config(QWEN, 2560))])
        with pytest.raises(ValidationError, match="still routes"):
            await svc.plan_context_migration(db, uuid4(), QWEN, source_model=QWEN)

    @pytest.mark.asyncio
    async def test_explicit_source_must_pair_with_the_active_model_as_target(self):
        db = _db([_result(scalar=_context()), _result(scalar=_config(QWEN, 2560))])
        with pytest.raises(ValidationError, match="active"):
            await svc.plan_context_migration(db, uuid4(), "qwen3-embedding:8b", source_model=SMALL)

    @pytest.mark.asyncio
    async def test_unknown_explicit_source_is_refused_before_touching_the_db(self):
        db = _db([])
        with pytest.raises(ValidationError):
            await svc.plan_context_migration(db, uuid4(), QWEN, source_model="not-a-model")
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_source_rebuilds_the_old_plan_after_a_switch(self):
        # Context now routes to QWEN; the operator names SMALL as the source.
        db = _db(
            [_result(scalar=_context()), _result(scalar=_config(QWEN, 2560)), _result(scalar=5)]
        )
        plan = await svc.plan_context_migration(db, uuid4(), QWEN, source_model=SMALL)
        assert (plan.source_model, plan.source_dimensions) == (SMALL, 512)
        assert plan.source_collection == "kagura_memories"
        assert (plan.target_model, plan.target_collection) == (
            QWEN,
            "kagura_memories_qwen3_embedding_4b_2560",
        )

    @pytest.mark.asyncio
    async def test_configured_context_plans_from_its_row(self):
        db = _db(
            [_result(scalar=_context()), _result(scalar=_config(QWEN, 2560)), _result(scalar=1)]
        )
        plan = await svc.plan_context_migration(db, uuid4(), "qwen3-embedding:8b")
        assert plan.source_collection == "kagura_memories_qwen3_embedding_4b_2560"
        assert plan.target_dimensions == 4096


# ------------------------------------------------------------------------ reembed


class TestReembed:
    @pytest.mark.asyncio
    async def test_writes_every_memory_to_the_target_collection_grouped_by_user(self):
        plan = _plan()
        m1, m2, m3 = _memory("u1"), _memory("u1"), _memory("u2")
        db = _db([_result(scalars=[m1, m2, m3]), _result(scalars=[])])
        service = MagicMock()
        service.embed_batch = AsyncMock(
            side_effect=lambda texts, *a, **k: [[0.1] * 2560 for _ in texts]
        )

        with (
            patch.object(svc, "ensure_kagura_memories_collection", AsyncMock()) as ensure,
            patch.object(svc, "add_memory_to_qdrant", AsyncMock()) as add,
            patch.object(svc, "build_memory_point", return_value=({"summary": "s"}, [1], [1.0])),
        ):
            result = await svc.reembed_context(db, plan, batch_size=10, embedding_service=service)

        ensure.assert_awaited_once_with(2560, plan.target_collection)
        # One embed_batch per consecutive user run: [u1, u1] then [u2].
        assert [call.args[1] for call in service.embed_batch.await_args_list] == ["u1", "u2"]
        assert [len(call.args[0]) for call in service.embed_batch.await_args_list] == [2, 1]
        # Every point lands in the TARGET collection under its own id.
        assert add.await_count == 3
        assert {call.kwargs["collection_name"] for call in add.await_args_list} == {
            plan.target_collection
        }
        assert {call.kwargs["memory_id"] for call in add.await_args_list} == {m1.id, m2.id, m3.id}
        assert result.embedded == 3 and result.batches == 1
        assert isinstance(result.started_at, datetime)
        # Routing was never touched: no config write, and every statement was
        # a SELECT — the commits only close each page's read transaction.
        db.add.assert_not_called()
        for call in db.execute.await_args_list:
            assert str(call.args[0]).lstrip().upper().startswith("SELECT")

    @pytest.mark.asyncio
    async def test_pages_with_keyset_and_reports_progress(self):
        plan = _plan(memory_count=3)
        rows = [_memory() for _ in range(3)]
        db = _db([_result(scalars=rows[:2]), _result(scalars=rows[2:]), _result(scalars=[])])
        service = MagicMock()
        service.embed_batch = AsyncMock(side_effect=lambda texts, *a, **k: [[0.0] for _ in texts])
        seen: list[tuple[int, int]] = []

        with (
            patch.object(svc, "ensure_kagura_memories_collection", AsyncMock()),
            patch.object(svc, "add_memory_to_qdrant", AsyncMock()),
            patch.object(svc, "build_memory_point", return_value=({}, [], [])),
        ):
            result = await svc.reembed_context(
                db,
                plan,
                batch_size=2,
                embedding_service=service,
                progress=lambda d, t: seen.append((d, t)),
            )

        assert result.batches == 2 and result.embedded == 3
        assert seen == [(2, 3), (3, 3)]
        # Every page (including the terminating empty one) releases its read
        # transaction before any embedding / Qdrant call.
        assert db.expunge_all.call_count == 3
        assert db.commit.await_count == 3

    @pytest.mark.asyncio
    async def test_read_transaction_ends_before_the_page_is_embedded(self):
        plan = _plan()
        db = _db([_result(scalars=[_memory()]), _result(scalars=[])])
        order: list[str] = []
        db.commit = AsyncMock(side_effect=lambda: order.append("commit"))
        service = MagicMock()

        async def _embed(texts, *a, **k):
            order.append("embed")
            return [[0.0] for _ in texts]

        service.embed_batch = AsyncMock(side_effect=_embed)
        with (
            patch.object(svc, "ensure_kagura_memories_collection", AsyncMock()),
            patch.object(svc, "add_memory_to_qdrant", AsyncMock()),
            patch.object(svc, "build_memory_point", return_value=({}, [], [])),
        ):
            await svc.reembed_context(db, plan, embedding_service=service)
        assert order[:2] == ["commit", "embed"]

    @pytest.mark.asyncio
    async def test_a_short_embedding_batch_is_an_error_not_a_silent_gap(self):
        plan = _plan()
        db = _db([_result(scalars=[_memory(), _memory()]), _result(scalars=[])])
        service = MagicMock()
        service.embed_batch = AsyncMock(return_value=[[0.0]])  # one vector for two texts
        with (
            patch.object(svc, "ensure_kagura_memories_collection", AsyncMock()),
            patch.object(svc, "add_memory_to_qdrant", AsyncMock()) as add,
            pytest.raises(RuntimeError),
        ):
            await svc.reembed_context(db, plan, embedding_service=service)
        add.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_a_nonpositive_batch_size(self):
        with pytest.raises(ValueError):
            await svc.reembed_context(_db([]), _plan(), batch_size=0)


# ------------------------------------------------------------------------- verify


class TestVerify:
    @pytest.mark.asyncio
    async def test_reports_the_missing_ids_not_just_a_count(self):
        plan = _plan()
        ids = [uuid4() for _ in range(3)]
        db = _db([_result(scalars=ids)])
        client = MagicMock()
        client.retrieve = AsyncMock(
            return_value=[MagicMock(id=str(ids[0])), MagicMock(id=str(ids[2]))]
        )

        with (
            patch.object(svc, "get_qdrant_client", return_value=client),
            patch.object(svc, "list_context_point_ids", AsyncMock(return_value=[])),
            patch.object(svc, "delete_points_from_qdrant", AsyncMock()) as delete,
        ):
            result = await svc.verify_context_migration(db, plan)

        assert (result.expected, result.present) == (3, 2)
        assert result.missing == [ids[1]]
        assert result.ok is False
        assert result.stale_removed == 0
        delete.assert_not_called()
        client.retrieve.assert_awaited_once()
        assert client.retrieve.await_args.kwargs["collection_name"] == plan.target_collection

    @pytest.mark.asyncio
    async def test_target_points_without_a_live_row_are_removed(self):
        # A memory forgotten after the re-embed copied it: live in the target
        # collection, tombstoned in Postgres. forget() only touched the source
        # collection, so verify is what deletes the copy.
        plan = _plan()
        live = [uuid4(), uuid4()]
        forgotten = uuid4()
        db = _db([_result(scalars=live)])
        client = MagicMock()
        client.retrieve = AsyncMock(return_value=[MagicMock(id=str(i)) for i in live])

        with (
            patch.object(svc, "get_qdrant_client", return_value=client),
            patch.object(
                svc,
                "list_context_point_ids",
                AsyncMock(return_value=[str(live[0]), str(forgotten), str(live[1])]),
            ) as listed,
            patch.object(svc, "delete_points_from_qdrant", AsyncMock()) as delete,
        ):
            result = await svc.verify_context_migration(db, plan)

        listed.assert_awaited_once_with(
            str(plan.workspace_id), str(plan.context_id), plan.target_collection
        )
        delete.assert_awaited_once_with([str(forgotten)], plan.target_collection)
        assert result.ok and result.stale_removed == 1
        assert (result.expected, result.present) == (2, 2)

    @pytest.mark.asyncio
    async def test_empty_context_verifies_trivially(self):
        client = MagicMock()
        client.retrieve = AsyncMock()
        with (
            patch.object(svc, "get_qdrant_client", return_value=client),
            patch.object(svc, "list_context_point_ids", AsyncMock(return_value=[])),
        ):
            result = await svc.verify_context_migration(_db([_result(scalars=[])]), _plan())
        assert result.ok and result.expected == 0
        client.retrieve.assert_not_called()


# ------------------------------------------------------------------------- switch


class TestSwitch:
    @pytest.mark.asyncio
    async def test_dimension_mismatch_is_refused(self):
        with pytest.raises(ValidationError):
            await svc.switch_context_embedding(_db([]), uuid4(), QWEN, 512)

    @pytest.mark.asyncio
    async def test_legacy_context_gets_a_config_row_and_delta_is_requeued(self):
        context_id = uuid4()
        since = datetime(2026, 9, 10, 12, 0, 0)
        # resolve (no row) -> select config (no row) -> requeue UPDATE
        # -> SELECT ids forgotten since (none)
        db = _db(
            [
                _result(scalar=None),
                _result(scalar=None),
                _result(rowcount=4),
                _result(scalars=[]),
            ]
        )

        with patch.object(svc, "delete_points_from_qdrant", AsyncMock()) as delete:
            result = await svc.switch_context_embedding(
                db, context_id, QWEN, 2560, requeue_since=since
            )

        added = db.add.call_args.args[0]
        assert added.context_id == context_id
        assert (added.embedding_model, added.embedding_dimensions) == (QWEN, 2560)
        assert (result.previous_model, result.previous_dimensions) == (SMALL, 512)
        assert result.requeued == 4
        assert result.stale_removed == 0
        delete.assert_not_called()
        db.commit.assert_awaited_once()
        requeue_stmt = db.execute.await_args_list[2].args[0]
        compiled = str(requeue_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "embedding_status != 'success'" in compiled
        assert "created_at >=" in compiled and "updated_at >=" in compiled
        assert "embedding_status='pending'" in compiled.replace(" ", "")
        # The requeue UPDATE is in the routing transaction; the stale-point
        # SELECT runs after it committed (routing already flipped).
        assert db.execute.await_count == 4

    @pytest.mark.asyncio
    async def test_memories_forgotten_since_the_reembed_are_dropped_from_the_new_collection(
        self,
    ):
        config = _config(SMALL, 512)
        since = datetime(2026, 9, 10, 12, 0, 0)
        forgotten = [uuid4(), uuid4()]
        db = _db(
            [
                _result(scalar=config),
                _result(scalar=config),
                _result(rowcount=1),
                _result(scalars=forgotten),
            ]
        )

        with patch.object(svc, "delete_points_from_qdrant", AsyncMock()) as delete:
            result = await svc.switch_context_embedding(
                db, uuid4(), QWEN, 2560, requeue_since=since
            )

        delete.assert_awaited_once_with(
            [str(i) for i in forgotten], "kagura_memories_qwen3_embedding_4b_2560"
        )
        assert result.stale_removed == 2
        forgotten_stmt = db.execute.await_args_list[3].args[0]
        compiled = str(forgotten_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "deleted_at >=" in compiled

    @pytest.mark.asyncio
    async def test_existing_row_is_updated_in_place_without_requeue(self):
        config = _config(SMALL, 512)
        db = _db([_result(scalar=config), _result(scalar=config)])

        result = await svc.switch_context_embedding(db, uuid4(), QWEN, 2560)

        assert (config.embedding_model, config.embedding_dimensions) == (QWEN, 2560)
        db.add.assert_not_called()
        assert result.requeued == 0
        assert db.execute.await_count == 2  # no UPDATE without requeue_since
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pure_flip_without_requeue_since_reconciles_nothing(self):
        config = _config(QWEN, 2560)
        db = _db([_result(scalar=config), _result(scalar=config)])
        with patch.object(svc, "delete_points_from_qdrant", AsyncMock()) as delete:
            result = await svc.switch_context_embedding(db, uuid4(), SMALL, 512)
        assert (result.previous_model, result.model) == (QWEN, SMALL)
        assert (config.embedding_model, config.embedding_dimensions) == (SMALL, 512)
        delete.assert_not_called()


# ----------------------------------------------------------------------- rollback


class TestRollback:
    """A rollback is a switch back PLUS the delta written since the switch;
    a bare flip would strand memories that only exist in the new collection."""

    @pytest.mark.asyncio
    async def test_refuses_the_model_the_context_already_routes_to(self):
        db = _db([_result(scalar=_config(SMALL, 512))])
        with (
            patch.object(svc, "switch_context_embedding", AsyncMock()) as switch,
            pytest.raises(ValidationError, match="already routes"),
        ):
            await svc.rollback_context_embedding(db, uuid4(), SMALL)
        switch.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_model_is_refused_before_touching_the_db(self):
        db = _db([])
        with pytest.raises(ValidationError):
            await svc.rollback_context_embedding(db, uuid4(), "not-a-model")
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_when_the_collection_to_route_back_to_is_gone(self):
        switched_at = datetime(2026, 9, 10, 12, 0, 0)
        db = _db([_result(scalar=_config(QWEN, 2560)), _result(scalar=switched_at)])
        client = MagicMock()
        client.collection_exists = AsyncMock(return_value=False)
        with (
            patch.object(svc, "get_qdrant_client", return_value=client),
            patch.object(svc, "switch_context_embedding", AsyncMock()) as switch,
            pytest.raises(ValidationError, match="does not exist"),
        ):
            await svc.rollback_context_embedding(db, uuid4(), SMALL)
        client.collection_exists.assert_awaited_once_with("kagura_memories")
        switch.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_without_a_recorded_switch_time(self):
        # Legacy context with no config row: nothing stamped the switch, so
        # the operator must say what to re-queue from.
        db = _db([_result(scalar=None), _result(scalar=None)])
        with (
            patch.object(svc, "switch_context_embedding", AsyncMock()) as switch,
            pytest.raises(ValidationError, match="requeue_since"),
        ):
            await svc.rollback_context_embedding(db, uuid4(), QWEN)
        switch.assert_not_called()

    @pytest.mark.asyncio
    async def test_requeues_from_the_switch_time_the_config_row_recorded(self):
        context_id = uuid4()
        switched_at = datetime(2026, 9, 10, 12, 0, 0)
        db = _db([_result(scalar=_config(QWEN, 2560)), _result(scalar=switched_at)])
        client = MagicMock()
        client.collection_exists = AsyncMock(return_value=True)
        expected = svc.SwitchResult(QWEN, 2560, SMALL, 512, requeued=3)
        with (
            patch.object(svc, "get_qdrant_client", return_value=client),
            patch.object(
                svc, "switch_context_embedding", AsyncMock(return_value=expected)
            ) as switch,
        ):
            result = await svc.rollback_context_embedding(db, context_id, SMALL)
        switch.assert_awaited_once_with(db, context_id, SMALL, 512, requeue_since=switched_at)
        assert result is expected

    @pytest.mark.asyncio
    async def test_an_explicit_requeue_since_skips_the_config_lookup(self):
        context_id = uuid4()
        since = datetime(2026, 9, 9, 0, 0, 0)
        db = _db([_result(scalar=_config(QWEN, 2560))])
        client = MagicMock()
        client.collection_exists = AsyncMock(return_value=True)
        with (
            patch.object(svc, "get_qdrant_client", return_value=client),
            patch.object(svc, "switch_context_embedding", AsyncMock()) as switch,
        ):
            await svc.rollback_context_embedding(db, context_id, SMALL, requeue_since=since)
        assert db.execute.await_count == 1  # only the routing resolve
        assert switch.await_args.kwargs["requeue_since"] == since


# -------------------------------------------------------------------------- purge


class TestPurge:
    @pytest.mark.asyncio
    async def test_refuses_while_the_context_still_serves_from_the_source(self):
        plan = _plan()
        db = _db([_result(scalar=_config(SMALL, 512))])
        with (
            patch.object(svc, "delete_context_points", AsyncMock()) as delete,
            pytest.raises(ValidationError),
        ):
            await svc.purge_source_points(db, plan)
        delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_only_from_the_source_collection_after_the_switch(self):
        plan = _plan()
        db = _db([_result(scalar=_config(QWEN, 2560))])
        with patch.object(svc, "delete_context_points", AsyncMock(return_value=8261)) as delete:
            assert await svc.purge_source_points(db, plan) == 8261
        delete.assert_awaited_once_with(
            str(plan.workspace_id), str(plan.context_id), plan.source_collection
        )


# -------------------------------------------------------------------- listing


class TestListing:
    @pytest.mark.asyncio
    async def test_lists_live_contexts(self):
        ids = [uuid4(), uuid4()]
        db = _db([_result(scalars=ids)])
        assert await svc.list_migratable_context_ids(db) == ids
        compiled = str(db.execute.await_args.args[0].compile())
        assert "deleted_at IS NULL" in compiled
