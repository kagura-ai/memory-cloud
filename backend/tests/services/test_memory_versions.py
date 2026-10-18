"""#2043: memory versions, the revision log and ``expected_version``.

The pure rules (which edit is a new version, what ``prior`` holds) and their
wiring into every write path, on a mocked session: the in-place update and
REST PATCH lock, compare and bump; remember / remember_batch log ``created``;
forget logs ``forgotten`` without a bump. The concurrent-edit and migration
behaviour against a real Postgres is in
``tests/integration/test_memory_versions_db.py``, the REST 409 in
``tests/api/test_memory_patch_version_conflict.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.memory import VERSIONED_FIELDS, MemoryRevision
from models.schemas import PatchMemoryRequest, RememberRequest, UpdateMemoryRequest
from services.memory_service import MemoryService
from services.memory_versions import (
    changed_prior,
    check_expected_version,
    record_edit,
    record_revision,
    versioned_snapshot,
)
from utils.exceptions import MemoryGoneError, VersionConflictError

_STAMP = datetime(2026, 10, 18, 12, 0, 0)


def _row(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid4(),
        "context_id": uuid4(),
        "version": 3,
        "summary": "stored summary text",
        "context_summary": None,
        "content": "stored content",
        "details": {"k": "v"},
        "type": "note",
        "importance": 0.5,
        "tags": ["a"],
        "updated_at": _STAMP,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _added_revisions(db: MagicMock) -> list[MemoryRevision]:
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], MemoryRevision)]


# ---------------------------------------------------------------- the rules


class TestChangedPrior:
    def test_only_changed_keys_with_their_old_values(self) -> None:
        row = _row()
        before = versioned_snapshot(row)
        row.importance = 0.9
        row.tags = ["a", "b"]
        assert changed_prior(before, row) == {"importance": 0.5, "tags": ["a"]}

    def test_a_null_that_was_overwritten_is_kept_as_null(self) -> None:
        row = _row()
        before = versioned_snapshot(row)
        row.context_summary = "now set"
        prior = changed_prior(before, row)
        assert prior == {"context_summary": None}
        assert "context_summary" in prior  # "was null", not "unchanged"

    def test_setting_a_field_to_null_records_the_old_value(self) -> None:
        row = _row()
        before = versioned_snapshot(row)
        row.details = None
        assert changed_prior(before, row) == {"details": {"k": "v"}}

    def test_null_tags_and_empty_tags_are_the_same_version(self) -> None:
        row = _row(tags=None)
        before = versioned_snapshot(row)
        row.tags = []
        assert changed_prior(before, row) == {}

    def test_an_in_place_mutation_does_not_alias_the_snapshot(self) -> None:
        row = _row()
        before = versioned_snapshot(row)
        row.details["k"] = "changed in place"
        assert changed_prior(before, row) == {"details": {"k": "v"}}

    def test_the_versioned_fields_are_the_content_bearing_ones(self) -> None:
        assert VERSIONED_FIELDS == (
            "summary",
            "context_summary",
            "content",
            "details",
            "type",
            "importance",
            "tags",
        )


class TestRecordEdit:
    def test_no_versioned_change_is_no_bump_and_no_row(self) -> None:
        db = MagicMock()
        row = _row()
        assert record_edit(db, row, versioned_snapshot(row), actor_user_id="u1") is False
        assert row.version == 3
        db.add.assert_not_called()

    def test_a_change_bumps_once_and_logs_the_overwritten_values(self) -> None:
        db = MagicMock()
        row = _row()
        before = versioned_snapshot(row)
        row.summary = "a new summary text"
        assert record_edit(db, row, before, actor_user_id="u1") is True
        assert row.version == 4
        (revision,) = _added_revisions(db)
        assert revision.memory_id == row.id
        assert revision.context_id == row.context_id
        assert revision.version == 4
        assert revision.kind == "updated"
        assert revision.writer == "api"
        assert revision.actor_user_id == "u1"
        assert revision.prior == {"summary": "stored summary text"}
        assert revision.at == _STAMP  # the edit's own updated_at
        assert revision.agent_id is None and revision.session_id is None  # #2044 fills these

    def test_a_row_without_a_context_bumps_but_logs_nothing(self) -> None:
        db = MagicMock()
        row = _row(context_id=None)
        before = versioned_snapshot(row)
        row.type = "decision"
        assert record_edit(db, row, before, actor_user_id="u1") is True
        db.add.assert_not_called()

    def test_record_revision_without_at_leaves_the_column_default(self) -> None:
        db = MagicMock()
        record_revision(db, _row(version=1), kind="created", actor_user_id="u1")
        (revision,) = _added_revisions(db)
        assert revision.at is None  # unset: the INSERT applies now(), the row's created_at
        assert revision.prior == {}


class TestCheckExpectedVersion:
    def test_omitted_is_last_writer_wins(self) -> None:
        check_expected_version(_row(version=7), None)

    def test_matching_version_passes(self) -> None:
        check_expected_version(_row(version=7), 7)

    def test_a_stale_version_is_refused_with_the_current_one(self) -> None:
        row = _row(version=7)
        with pytest.raises(VersionConflictError) as excinfo:
            check_expected_version(row, 6)
        err = excinfo.value
        assert (err.expected_version, err.current_version, err.memory_id) == (6, 7, row.id)
        assert err.status_code == 409
        assert err.details == {
            "reason": "version_conflict",
            "expected_version": 6,
            "current_version": 7,
        }


# ------------------------------------------------------------- request shapes


class TestRequestSchemas:
    def test_update_accepts_a_positive_version(self) -> None:
        req = UpdateMemoryRequest(memory_id=uuid4(), importance=0.4, expected_version=2)
        assert req.expected_version == 2

    @pytest.mark.parametrize("bad", [0, -1, True, "two"])
    def test_update_rejects_a_non_version(self, bad: Any) -> None:
        with pytest.raises(ValidationError):
            UpdateMemoryRequest(memory_id=uuid4(), importance=0.4, expected_version=bad)

    def test_update_refuses_a_version_on_the_external_id_upsert(self) -> None:
        with pytest.raises(ValidationError, match="expected_version requires memory_id"):
            UpdateMemoryRequest(
                external_id="doc-1",
                summary="a summary long enough",
                content="body",
                type="note",
                expected_version=1,
            )

    def test_patch_expected_version_alone_is_not_a_patch(self) -> None:
        with pytest.raises(ValidationError, match="at least one field"):
            PatchMemoryRequest(expected_version=1)

    def test_patch_takes_a_version_next_to_a_field(self) -> None:
        req = PatchMemoryRequest(importance=0.4, expected_version=5)
        assert req.expected_version == 5
        with pytest.raises(ValidationError):
            PatchMemoryRequest(importance=0.4, expected_version=True)


# ----------------------------------------------------- in-place update wiring


def _memory(**overrides: Any) -> MagicMock:
    m = MagicMock()
    m.id = uuid4()
    m.point_id = m.id
    m.user_id = "test_user"
    m.workspace_id = uuid4()
    m.context_id = uuid4()
    m.summary = "stored summary"
    m.context_summary = None
    m.content = "stored content"
    m.details = None
    m.type = "note"
    m.importance = 0.5
    m.tags = ["a"]
    m.context = None
    m.scope = "working"
    m.delivery_mode = "on_recall"
    m.is_pinned = False
    m.updated_at = None
    m.deleted_at = None
    m.supersede_candidate = None
    m.source_type = "manual"
    m.version = 3
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def service(mock_db: MagicMock) -> MemoryService:
    svc = MemoryService(mock_db)
    svc._ensure_capacity = AsyncMock()  # type: ignore[method-assign]
    svc._lint_write = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc._update_sync_qdrant_payload = AsyncMock()  # type: ignore[method-assign]
    svc._patch_sync_qdrant_payload = AsyncMock()  # type: ignore[method-assign]
    svc._patch_build_response = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    return svc


def _prime(service: MemoryService, read: Any, locked: Any | None = None) -> None:
    """``read`` is what authorization saw; ``locked`` what the FOR UPDATE re-read sees."""
    service._update_load_authorized = AsyncMock(return_value=read)  # type: ignore[method-assign]
    service._patch_load_authorized = AsyncMock(return_value=read)  # type: ignore[method-assign]
    service._lock_for_edit = AsyncMock(return_value=locked or read)  # type: ignore[method-assign]


async def _update(service: MemoryService, **fields: Any) -> Any:
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await service._update_in_place(UpdateMemoryRequest(**fields), user_id="u1")


async def _patch(service: MemoryService, memory: Any, **fields: Any) -> Any:
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await service.patch_memory(memory.id, PatchMemoryRequest(**fields), "u1")


@pytest.mark.asyncio
class TestUpdateInPlace:
    async def test_a_versioned_edit_bumps_and_logs(self, service, mock_db) -> None:
        memory = _memory()
        _prime(service, memory)
        result = await _update(service, memory_id=memory.id, importance=0.9)
        assert result.version == 4 and memory.version == 4
        (revision,) = _added_revisions(mock_db)
        assert revision.prior == {"importance": 0.5}
        assert revision.version == 4 and revision.actor_user_id == "u1"
        assert revision.at == memory.updated_at
        mock_db.commit.assert_awaited()

    @pytest.mark.parametrize(
        "fields",
        [
            {"delivery_mode": "always"},
            {"context": {"file": "x.py"}},
            {"importance": 0.5, "tags": ["a"]},  # resending the stored values
            {"summary": "stored summary"},
        ],
        ids=["delivery_mode", "context", "same-values", "same-summary"],
    )
    async def test_an_edit_of_no_versioned_field_keeps_the_version(
        self, service, mock_db, fields
    ) -> None:
        memory = _memory()
        _prime(service, memory)
        result = await _update(service, memory_id=memory.id, **fields)
        assert result.version == 3 and memory.version == 3
        assert _added_revisions(mock_db) == []

    async def test_a_dismissal_only_call_keeps_the_version(self, service, mock_db) -> None:
        memory = _memory()
        _prime(service, memory)
        result = await _update(service, memory_id=memory.id, dismiss_supersede_candidate=True)
        assert result.version == 3
        assert _added_revisions(mock_db) == []

    async def test_a_matching_expected_version_applies(self, service, mock_db) -> None:
        memory = _memory()
        _prime(service, memory)
        result = await _update(service, memory_id=memory.id, type="decision", expected_version=3)
        assert result.version == 4 and memory.type == "decision"

    async def test_a_stale_expected_version_writes_nothing(self, service, mock_db) -> None:
        memory = _memory()
        _prime(service, memory)
        with pytest.raises(VersionConflictError) as excinfo:
            await _update(
                service, memory_id=memory.id, summary="a different summary", expected_version=2
            )
        assert excinfo.value.current_version == 3
        assert memory.summary == "stored summary" and memory.version == 3
        assert memory.updated_at is None  # no field was applied
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_awaited()

    async def test_the_check_reads_the_locked_row(self, service, mock_db) -> None:
        """Another edit committed between the authorized read and the lock:
        the locked row's version decides, and the edit lands on that row."""
        read, locked = _memory(), _memory(version=4)
        locked.id = read.id
        _prime(service, read, locked)
        with pytest.raises(VersionConflictError) as excinfo:
            await _update(service, memory_id=read.id, importance=0.9, expected_version=3)
        assert excinfo.value.current_version == 4
        service._lock_for_edit.assert_awaited_once_with(read.id)

        result = await _update(service, memory_id=read.id, importance=0.9, expected_version=4)
        assert result.version == 5 and locked.importance == 0.9
        assert read.importance == 0.5  # the unlocked copy is not what was edited


@pytest.mark.asyncio
class TestPatch:
    async def test_a_patch_bumps_and_logs(self, service, mock_db) -> None:
        memory = _memory()
        _prime(service, memory)
        await _patch(service, memory, tags=["a", "b"])
        assert memory.version == 4
        (revision,) = _added_revisions(mock_db)
        assert revision.prior == {"tags": ["a"]}
        service._lock_for_edit.assert_awaited_once_with(memory.id, include_deleted=True)

    async def test_a_stale_expected_version_writes_nothing(self, service, mock_db) -> None:
        memory = _memory()
        _prime(service, memory)
        with pytest.raises(VersionConflictError):
            await _patch(service, memory, content="new body", expected_version=1)
        assert memory.content == "stored content" and memory.version == 3
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_awaited()

    async def test_a_memory_forgotten_before_the_lock_is_gone(self, service, mock_db) -> None:
        read, locked = _memory(), _memory(deleted_at=_STAMP)
        _prime(service, read, locked)
        with pytest.raises(MemoryGoneError):
            await _patch(service, read, importance=0.9, expected_version=3)
        mock_db.add.assert_not_called()


@pytest.mark.asyncio
async def test_lock_for_edit_reads_for_update_and_404s_a_vanished_row(mock_db) -> None:
    from utils.exceptions import NotFoundException

    svc = MemoryService(mock_db)
    row = _memory()
    svc.memory_repo.get = AsyncMock(return_value=row)  # type: ignore[method-assign]
    assert await svc._lock_for_edit(row.id) is row
    svc.memory_repo.get.assert_awaited_once_with(row.id, include_deleted=False, for_update=True)

    svc.memory_repo.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    with pytest.raises(NotFoundException):
        await svc._lock_for_edit(row.id, include_deleted=True)


@pytest.mark.asyncio
async def test_the_repository_read_for_update_locks_and_refreshes() -> None:
    from sqlalchemy.dialects import postgresql

    from repositories.memory import MemoryRepository

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
    await MemoryRepository(db).get(uuid4(), for_update=True)
    stmt = db.execute.await_args.args[0]
    assert str(stmt.compile(dialect=postgresql.dialect())).rstrip().endswith("FOR UPDATE")
    assert stmt.get_execution_options().get("populate_existing") is True


# ----------------------------------------------------------- remember / forget


def _quota() -> MagicMock:
    return MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=(True, None)),
        reserve_memories_per_day=AsyncMock(return_value=None),
        release_memories_per_day=AsyncMock(),
    )


@pytest.fixture
def remember_service(mock_db: MagicMock) -> MemoryService:
    svc = MemoryService(mock_db)
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(ctx, str(ctx.workspace_id), str(ctx.id))
    )
    svc._ensure_capacity = AsyncMock()  # type: ignore[method-assign]
    svc._create_declared_links = AsyncMock()  # type: ignore[method-assign]
    svc._lint_write = AsyncMock(return_value=[])  # type: ignore[method-assign]
    svc.memory_repo = MagicMock()
    svc.memory_repo.create = AsyncMock()
    svc._ctx = ctx  # type: ignore[attr-defined]
    return svc


def _remember_request(n: int = 0) -> RememberRequest:
    return RememberRequest(summary=f"remembered fact number {n}", content="body", type="note")


@pytest.mark.asyncio
async def test_remember_logs_created_at_version_one(remember_service, mock_db) -> None:
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=_quota()),
        patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
    ):
        result = await remember_service.remember(
            _remember_request(),
            user_id="u1",
            current_context_id=remember_service._ctx.id,
        )
    assert result.version == 1
    (revision,) = _added_revisions(mock_db)
    created = remember_service.memory_repo.create.await_args.args[0]
    assert created.version == 1
    assert (revision.memory_id, revision.kind, revision.version) == (created.id, "created", 1)
    assert revision.context_id == remember_service._ctx.id
    assert revision.actor_user_id == "u1"
    assert revision.at is None  # the INSERT's now(): the row's created_at


@pytest.mark.asyncio
async def test_remember_many_logs_one_created_row_per_item(remember_service, mock_db) -> None:
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=_quota()),
        patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
    ):
        results = await remember_service.remember_many(
            [_remember_request(1), _remember_request(2)],
            user_id="u1",
            current_context_id=remember_service._ctx.id,
        )
    assert [r.version for r in results] == [1, 1]
    revisions = _added_revisions(mock_db)
    assert [r.kind for r in revisions] == ["created", "created"]
    assert [r.memory_id for r in revisions] == [r.memory_id for r in results]


@pytest.mark.asyncio
async def test_forget_logs_forgotten_without_a_bump(mock_db) -> None:
    svc = MemoryService(mock_db)
    svc.memory_repo.update = AsyncMock()  # type: ignore[method-assign]
    memory = _memory(version=5)
    with (
        patch("services.memory_service.resolve_collection_name", new=AsyncMock(return_value="c")),
        patch("services.memory_service.delete_memory_from_qdrant", new=AsyncMock()),
        patch("repositories.neural_edge.NeuralEdgeRepository") as edges,
        patch("services.resource_indexer.unshared_point_id", new=AsyncMock(return_value=None)),
    ):
        edges.return_value.delete_node_edges = AsyncMock(return_value=0)
        await svc._soft_delete_memory(
            "u1", memory, str(memory.workspace_id), str(memory.context_id)
        )
    assert memory.version == 5
    (revision,) = _added_revisions(mock_db)
    assert (revision.kind, revision.version, revision.actor_user_id) == ("forgotten", 5, "u1")
    assert revision.at == memory.deleted_at  # the tombstone's own stamp
    assert revision.prior == {}


# ------------------------------------------------------------ the surfaces


@pytest.mark.asyncio
async def test_mcp_update_memory_answers_version_conflict() -> None:
    import json
    from contextlib import ExitStack

    from mcp_server.tools.memory import handle_update_memory

    memory_id = uuid4()
    service = MagicMock()
    service.update_memory = AsyncMock(
        side_effect=VersionConflictError(memory_id=memory_id, expected_version=2, current_version=5)
    )
    db = AsyncMock()

    async def get_db():
        yield db

    with ExitStack() as stack:
        for target, value in (
            ("db.base.get_db", get_db),
            ("mcp_server.tools.memory._check_viewer_permission", AsyncMock(return_value=None)),
            ("mcp_server.tools.memory._resolve_context", AsyncMock(return_value=MagicMock())),
            ("mcp_server.tools.memory._log_tool_usage", AsyncMock()),
            ("services.memory_service.MemoryService", MagicMock(return_value=service)),
        ):
            stack.enter_context(patch(target, new=value))
        result = await handle_update_memory(
            {
                "memory_id": str(memory_id),
                "importance": 0.9,
                "expected_version": 2,
                "context_id": str(uuid4()),
            },
            user_id="u1",
            workspace_id=None,
        )

    assert service.update_memory.await_args.args[0].expected_version == 2
    payload = json.loads(result[0].text)
    assert payload["status"] == "error"
    assert payload["error"] == "version_conflict"
    assert payload["memory_id"] == str(memory_id)
    assert (payload["expected_version"], payload["current_version"]) == (2, 5)
    assert payload["retryable"] is False
    assert "reference()" in payload["help"]
    db.rollback.assert_awaited()
    db.commit.assert_not_awaited()


def test_mcp_projections_carry_the_version() -> None:
    from mcp_server.tools.listing import _memory_item
    from models.schemas import MemoryResponse
    from utils.recall_projection import recall_result_item

    hit = MemoryResponse(
        memory_id=uuid4(),
        summary="a recalled memory",
        context_summary=None,
        type="note",
        importance=0.5,
        scope="working",
        created_at=_STAMP,
        client="mcp",
        tags=[],
        context=None,
        score=0.5,
        version=6,
    )
    assert recall_result_item(hit, "full")["version"] == 6
    # compact is the judge-and-fetch pass: reference() carries the version
    assert "version" not in recall_result_item(hit, "compact")
    assert "version" not in recall_result_item(hit.model_copy(update={"version": None}), "full")

    row = SimpleNamespace(
        id=uuid4(),
        summary="listed",
        type="note",
        importance=0.5,
        scope="working",
        tags=None,
        delivery_mode="on_recall",
        source_type="manual",
        created_at=_STAMP,
        updated_at=None,
        version=2,
        details=None,
    )
    assert _memory_item(row, include_details=False)["version"] == 2
