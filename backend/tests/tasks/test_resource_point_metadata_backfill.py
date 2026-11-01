"""``backfill_resource_point_metadata`` (#2254).

The walk sets ``type`` / ``tags`` / ``context_summary`` from the row on the
resource point the row owns (``names_own_resource_point``), skips a shared
point, resolves each context's collection once and isolates a failing row.
The query itself runs against Postgres in
``tests/integration/test_resource_point_metadata_backfill_qdrant.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from services.resource_indexer import resource_point_id
from tasks.resource_point_metadata_backfill import backfill_resource_point_metadata


def _db(rows):
    db = MagicMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


def _row(**overrides):
    resource_id = overrides.get("resource_id", "crm")
    doc_id = overrides.get("doc_id", f"doc-{uuid4().hex[:6]}")
    version = overrides.get("version", 1)
    return SimpleNamespace(
        id=overrides.get("id", uuid4()),
        summary_embedding_id=overrides.get(
            "summary_embedding_id", resource_point_id(resource_id, doc_id, version)
        ),
        context_id=overrides.get("context_id", uuid4()),
        resource_id=resource_id,
        resource_doc_id=doc_id,
        resource_version=version,
        type=overrides.get("type", "resource_data"),
        tags=overrides.get("tags", []),
        context_summary=overrides.get("context_summary", "summary"),
    )


async def _unshared(db, memory_id, point_id):
    return point_id


def _apply(rows, *, update=None, unshared=_unshared, resolve=None, current=None):
    update = update or AsyncMock()
    resolve = resolve or AsyncMock(return_value="kagura_memories")
    by_id = {row.id: row for row in rows}

    async def _current(db, memory_id, point_id):
        # The fresh per-row read; by default the row as the snapshot saw it.
        return by_id[memory_id]

    with (
        patch("db.qdrant.update_memory_payload_in_qdrant", update),
        patch("services.resource_indexer.unshared_point_id", side_effect=unshared),
        patch("services.context_routing.resolve_collection_name", resolve),
        patch(
            "tasks.resource_point_metadata_backfill._current_values",
            side_effect=current or _current,
        ),
    ):
        result = asyncio.run(backfill_resource_point_metadata(_db(rows), dry_run=False))
    return result, update, resolve


class TestPlan:
    def test_dry_run_counts_owning_rows_and_writes_nothing(self):
        context = uuid4()
        rows = [_row(context_id=context), _row(context_id=context), _row()]
        update = AsyncMock()
        with patch("db.qdrant.update_memory_payload_in_qdrant", update):
            result = asyncio.run(backfill_resource_point_metadata(_db(rows), dry_run=True))
        assert (result.rows, result.contexts, result.written) == (3, 2, 0)
        update.assert_not_awaited()

    def test_a_row_naming_another_point_is_not_a_candidate(self):
        # remember(external_id) rows carry the natural key in details but name
        # their own id (or any other point): not an indexer point to backfill.
        stray = _row(summary_embedding_id=uuid4())
        result, update, _ = _apply([stray, _row()])
        assert (result.rows, result.written) == (1, 1)
        assert update.await_count == 1


class TestApply:
    def test_sets_the_three_keys_from_the_row_on_its_point(self):
        row = _row(tags=["a", "b"], context_summary="ｶﾀｶﾅ ㈱")
        result, update, _ = _apply([row])
        assert result.written == 1
        call = update.await_args.kwargs
        assert call["memory_id"] == row.summary_embedding_id
        assert call["collection_name"] == "kagura_memories"
        assert call["payload_updates"] == {
            "type": "resource_data",
            "tags": ["a", "b"],
            # normalize_for_search, like build_resource_point.
            "context_summary": "カタカナ (株)",
        }
        # An indexer point has no chunk points of its own.
        assert call["cascade_chunks"] is False
        assert "delete_keys" not in call

    def test_null_tags_become_empty_and_a_null_summary_is_omitted(self):
        result, update, _ = _apply([_row(tags=None, context_summary=None, type="note")])
        assert result.written == 1
        assert update.await_args.kwargs["payload_updates"] == {"type": "note", "tags": []}

    def test_a_shared_point_is_left_as_it_is(self):
        shared = _row()

        async def unshared(db, memory_id, point_id):
            return None if memory_id == shared.id else point_id

        result, update, _ = _apply([shared, _row()], unshared=unshared)
        assert (result.written, result.shared_skipped, result.failed) == (1, 1, [])
        assert update.await_args.kwargs["memory_id"] != shared.summary_embedding_id

    def test_the_values_are_read_again_right_before_the_write(self):
        # Edited during the run: its own metadata patch already wrote the new
        # tags to the point; the snapshot's old ones must not overwrite them.
        edited = _row(tags=["old"])

        async def current(db, memory_id, point_id):
            assert point_id == edited.summary_embedding_id
            return SimpleNamespace(type="resource_data", tags=["new"], context_summary=None)

        result, update, _ = _apply([edited], current=current)
        assert result.written == 1
        assert update.await_args.kwargs["payload_updates"]["tags"] == ["new"]

    def test_a_row_gone_since_the_snapshot_is_skipped(self):
        gone, kept = _row(), _row()

        async def current(db, memory_id, point_id):
            return None if memory_id == gone.id else kept

        result, update, _ = _apply([gone, kept], current=current)
        assert (result.written, result.gone_skipped, result.failed) == (1, 1, [])
        assert update.await_args.kwargs["memory_id"] == kept.summary_embedding_id

    def test_each_context_collection_is_resolved_once(self):
        a, b = uuid4(), uuid4()
        rows = [_row(context_id=a), _row(context_id=b), _row(context_id=a)]
        resolve = AsyncMock(side_effect=lambda db, context_id: f"col_{context_id}")
        result, update, resolve = _apply(rows, resolve=resolve)
        assert result.written == 3
        assert [c.args[1] for c in resolve.await_args_list] == [a, b]
        assert [c.kwargs["collection_name"] for c in update.await_args_list] == [
            f"col_{a}",
            f"col_{b}",
            f"col_{a}",
        ]

    def test_a_failing_row_is_recorded_and_the_walk_goes_on(self):
        down, gone, good = _row(), _row(), _row()

        async def unshared(db, memory_id, point_id):
            if memory_id == gone.id:
                raise RuntimeError("lookup failed")
            return point_id

        update = AsyncMock(side_effect=[RuntimeError("point not found"), None])
        result, update, _ = _apply([down, gone, good], update=update, unshared=unshared)
        assert (result.written, result.failed) == (1, [down.id, gone.id])
        assert update.await_args_list[-1].kwargs["memory_id"] == good.summary_embedding_id

    def test_an_oversized_summary_fails_only_its_row(self):
        huge = _row()
        good = _row()
        with patch(
            "tasks.resource_point_metadata_backfill.normalize_for_search",
            side_effect=[ValueError("too large"), "summary"],
        ):
            result, update, _ = _apply([huge, good])
        assert (result.written, result.failed) == (1, [huge.id])
