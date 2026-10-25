"""``observed_at`` on the write paths (#2330): payload sync and the D7 fold.

The metadata payload patch sets or clears ``observed_at`` like ``location``;
a top-level ``observed_at`` on update / PATCH is a details write that keeps
the stored details (D7); a conflicting or malformed value is refused.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.schemas import PatchMemoryRequest, RememberRequest, UpdateMemoryRequest
from services.memory_service import (
    MemoryService,
    metadata_payload_patch,
    restore_payload_request,
)


def _request(details):
    return SimpleNamespace(tags=None, importance=None, type=None, details=details)


class TestMetadataPayloadPatch:
    def test_a_date_is_sent_in_the_column_form(self):
        updates, delete_keys = metadata_payload_patch(
            _request({"observed_at": "2024-03-07"}), None, {"observed_at": "2024-03-07"}
        )
        assert updates["observed_at"] == "2024-03-07T00:00:00Z"
        assert "observed_at" not in delete_keys

    def test_details_without_it_clear_it(self):
        updates, delete_keys = metadata_payload_patch(_request({"k": 1}), None, {"k": 1})
        assert "observed_at" not in updates
        assert "observed_at" in delete_keys

    def test_no_details_write_leaves_it(self):
        updates, delete_keys = metadata_payload_patch(_request(None), None, None)
        assert "observed_at" not in updates
        assert "observed_at" not in delete_keys


class TestRestorePayload:
    """Session rollback and the erasure details revert build their patch with
    ``restore_payload_request`` + ``metadata_payload_patch``."""

    def test_restored_details_carry_their_observed_at(self):
        memory = SimpleNamespace(
            tags=[], importance=0.5, type="note", details={"observed_at": "2024-03-07"}
        )
        request = restore_payload_request(memory, {"details"})
        updates, delete_keys = metadata_payload_patch(request, None, memory.details)
        assert updates["observed_at"] == "2024-03-07T00:00:00Z"
        assert "observed_at" not in delete_keys

    def test_details_restored_to_null_clear_it(self):
        memory = SimpleNamespace(tags=[], importance=0.5, type="note", details=None)
        request = restore_payload_request(memory, {"details"})
        _, delete_keys = metadata_payload_patch(request, None, request.details)
        assert "observed_at" in delete_keys


class TestFold:
    def test_none_leaves_the_request(self):
        assert MemoryService._fold_observed_at(None, {"a": 1}, None) is None
        assert MemoryService._fold_observed_at({"b": 2}, {"a": 1}, None) == {"b": 2}

    def test_alone_keeps_the_stored_details(self):
        stored = {"a": 1, "observed_at": "2020-01-01"}
        folded = MemoryService._fold_observed_at(None, stored, "2024-03-07")
        assert folded == {"a": 1, "observed_at": "2024-03-07"}
        assert stored["observed_at"] == "2020-01-01"  # not mutated

    def test_alone_on_a_row_without_details(self):
        assert MemoryService._fold_observed_at(None, None, "2024-03-07") == {
            "observed_at": "2024-03-07"
        }

    def test_with_details_it_is_folded_in(self):
        assert MemoryService._fold_observed_at({"b": 2}, {"a": 1}, "2024-03-07") == {
            "b": 2,
            "observed_at": "2024-03-07",
        }

    def test_disagreeing_values_are_refused(self):
        with pytest.raises(ValueError, match="disagree"):
            MemoryService._fold_observed_at({"observed_at": "2024-03-08"}, None, "2024-03-07")


class TestSchemas:
    def test_remember_keeps_the_argument_for_the_service(self):
        """The schema does not fold (#2330 review): the service does, so a
        conflict is the same validation_error on every write tool."""
        request = RememberRequest(
            summary="Fact with an event time",
            content="c",
            type="fact",
            details={"observed_at": "2024-03-08"},
            observed_at="2024-03-07",
        )
        assert request.details == {"observed_at": "2024-03-08"}
        assert request.observed_at == "2024-03-07"


async def _prepare(request: RememberRequest) -> None:
    await MemoryService(MagicMock())._prepare_remember(
        request,
        "u",
        client="test",
        current_context_id=None,
        current_workspace_id=None,
        key_workspace_id=None,
        _skip_daily_quota=True,
        tags_normalize=False,
        dedupe="suggest",
    )


class TestRememberFold:
    @pytest.mark.asyncio
    async def test_a_conflict_is_a_value_error_before_anything_else(self):
        request = RememberRequest(
            summary="Fact with an event time",
            content="c",
            type="fact",
            details={"observed_at": "2024-03-08"},
            observed_at="2024-03-07",
        )
        with pytest.raises(ValueError, match="invalid details.observed_at: .*disagree"):
            await _prepare(request)

    @pytest.mark.asyncio
    async def test_the_argument_lands_in_details(self):
        request = RememberRequest(
            summary="Fact with an event time",
            content="c",
            type="fact",
            details={"k": 1},
            observed_at="2024-03-07",
        )
        # Stop right after the fold: context resolution is the next step.
        with patch.object(
            MemoryService,
            "_get_context_isolation_params",
            AsyncMock(side_effect=RuntimeError("stop")),
        ):
            with pytest.raises(RuntimeError, match="stop"):
                await _prepare(request)
        assert request.details == {"k": 1, "observed_at": "2024-03-07"}


class TestSchemasMore:
    def test_patch_with_only_a_null_observed_at_is_refused(self):
        with pytest.raises(ValidationError, match="at least one field"):
            PatchMemoryRequest(observed_at=None)

    def test_accept_supersede_refuses_an_observed_at_edit(self):
        with pytest.raises(ValidationError, match="observed_at"):
            UpdateMemoryRequest(
                memory_id=uuid4(), accept_supersede_candidate=True, observed_at="2024-03-07"
            )


def _make_memory(details):
    memory = MagicMock()
    memory.id = uuid4()
    memory.point_id = memory.id
    memory.user_id = "test_user"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "Original summary for testing"
    memory.context_summary = None
    memory.content = "Original content"
    memory.details = details
    memory.type = "note"
    memory.importance = 0.5
    memory.tags = ["original"]
    memory.context = None
    memory.scope = "working"
    memory.client = "mcp"
    memory.created_at = None
    memory.updated_at = None
    memory.deleted_at = None
    memory.embedding_status = "success"
    return memory


@pytest.fixture
def service():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return MemoryService(db)


async def _update(service, memory, request):
    service.memory_repo.get = AsyncMock(return_value=memory)
    with (
        patch("services.permission_service.PermissionService") as mock_perm_cls,
        patch(
            "services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()
        ) as payload,
        patch(
            "services.memory_service.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
    ):
        mock_perm_cls.return_value.can_access_memory = AsyncMock(return_value=True)
        await service._update_in_place(request, user_id="test_user")
    return payload


class TestUpdateInPlace:
    @pytest.mark.asyncio
    async def test_observed_at_alone_edits_only_the_event_time(self, service):
        memory = _make_memory({"note": "keep me", "observed_at": "2020-01-01"})
        payload = await _update(
            service,
            memory,
            UpdateMemoryRequest(memory_id=memory.id, observed_at="2024-03-07T10:00:00+09:00"),
        )
        assert memory.details == {"note": "keep me", "observed_at": "2024-03-07T01:00:00Z"}
        assert payload.await_args.kwargs["payload_updates"]["observed_at"] == (
            "2024-03-07T01:00:00Z"
        )

    @pytest.mark.asyncio
    async def test_a_details_write_without_it_clears_the_payload(self, service):
        memory = _make_memory({"observed_at": "2020-01-01"})
        payload = await _update(
            service, memory, UpdateMemoryRequest(memory_id=memory.id, details={"k": 1})
        )
        assert memory.details == {"k": 1}
        assert "observed_at" in payload.await_args.kwargs["delete_keys"]

    @pytest.mark.asyncio
    async def test_a_malformed_value_is_refused_before_anything_is_written(self, service):
        memory = _make_memory({"note": "keep me"})
        with pytest.raises(ValueError, match="invalid details.observed_at"):
            await _update(
                service, memory, UpdateMemoryRequest(memory_id=memory.id, observed_at="soon")
            )

    @pytest.mark.asyncio
    async def test_an_untouched_legacy_value_does_not_refuse_other_edits(self, service):
        memory = _make_memory({"observed_at": "March 7"})
        await _update(service, memory, UpdateMemoryRequest(memory_id=memory.id, importance=0.9))
        assert memory.details == {"observed_at": "March 7"}


async def _patch(service, memory, request):
    with (
        patch.object(service, "_patch_load_authorized", AsyncMock(return_value=memory)),
        patch.object(service, "_ensure_capacity", AsyncMock()),
        patch.object(service, "_lock_for_edit", AsyncMock(return_value=memory)),
        patch.object(service, "_sync_metadata_payload", AsyncMock()) as sync,
        patch.object(service, "_patch_build_response", AsyncMock(return_value=None)),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
        patch("services.memory_service.record_edit"),
    ):
        await service.patch_memory(memory.id, request, user_id="test_user")
    return sync


class TestPatch:
    @pytest.mark.asyncio
    async def test_observed_at_alone_keeps_the_stored_details(self, service):
        memory = _make_memory({"note": "keep me"})
        sync = await _patch(service, memory, PatchMemoryRequest(observed_at="2024-03-07"))
        assert memory.details == {"note": "keep me", "observed_at": "2024-03-07"}
        updates, delete_keys = sync.await_args.args[1], sync.await_args.args[2]
        assert updates["observed_at"] == "2024-03-07T00:00:00Z"
        assert "observed_at" not in delete_keys

    @pytest.mark.asyncio
    async def test_with_null_details_only_the_event_time_is_kept(self, service):
        memory = _make_memory({"note": "dropped"})
        await _patch(service, memory, PatchMemoryRequest(details=None, observed_at="2024-03-07"))
        assert memory.details == {"observed_at": "2024-03-07"}
