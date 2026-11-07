"""Tests for update_memory (Issue #80)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import UpdateMemoryRequest
from services.memory_service import MemoryService


class TestUpdateMemoryInPlace:
    """Test in-place update by memory_id."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        db.rollback = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return MemoryService(mock_db)

    def _make_memory(self, **overrides):
        """Create a mock Memory object with sensible defaults."""
        memory = MagicMock()
        memory.id = overrides.get("id", uuid4())
        # An API-written row: its point is its own id (#2100).
        memory.point_id = overrides.get("point_id", memory.id)
        memory.user_id = overrides.get("user_id", "test_user")
        memory.workspace_id = overrides.get("workspace_id", uuid4())
        memory.context_id = overrides.get("context_id", uuid4())
        memory.summary = overrides.get("summary", "Original summary for testing")
        memory.context_summary = overrides.get("context_summary", None)
        memory.content = overrides.get("content", "Original content")
        memory.details = overrides.get("details", None)
        memory.type = overrides.get("type", "note")
        memory.importance = overrides.get("importance", 0.5)
        memory.tags = overrides.get("tags", ["original"])
        memory.context = overrides.get("context", None)
        memory.scope = overrides.get("scope", "working")
        memory.client = overrides.get("client", "mcp")
        memory.created_at = None
        memory.updated_at = None
        memory.deleted_at = None
        memory.embedding_status = "success"
        return memory

    @pytest.mark.asyncio
    async def test_metadata_only_update_no_reembed(self, service):
        """Tags/importance change should not trigger re-embedding."""
        memory = self._make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.update_memory_payload_in_qdrant", new=AsyncMock()
            ) as mock_payload_update,
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            request = UpdateMemoryRequest(
                memory_id=memory.id,
                tags=["updated", "tags"],
                importance=0.9,
            )

            result = await service._update_in_place(request, user_id="test_user")

            assert result.operation == "updated"
            assert result.re_embedded is False
            assert memory.embedding_status == "success"  # unchanged
            mock_payload_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_summary_change_triggers_reembed(self, service, mock_db):
        """Summary change should set embedding_status=pending and fire create_task."""
        memory = self._make_memory(summary="Original summary for testing")
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            request = UpdateMemoryRequest(
                memory_id=memory.id,
                summary="Completely different summary that triggers re-embedding",
            )

            result = await service._update_in_place(request, user_id="test_user")

            assert result.operation == "updated"
            assert result.re_embedded is True
            mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_context_summary_change_triggers_reembed(self, service, mock_db):
        """context_summary change should trigger re-embedding (BM25 tokens)."""
        memory = self._make_memory(context_summary="Old context summary")
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            request = UpdateMemoryRequest(
                memory_id=memory.id,
                context_summary="Updated context summary with new info",
            )

            result = await service._update_in_place(request, user_id="test_user")

            assert result.re_embedded is True


class TestUpsertByExternalId:
    """Test upsert by external_id."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        db.rollback = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return MemoryService(mock_db)

    @pytest.mark.asyncio
    async def test_upsert_creates_when_not_exists(self, service):
        """When external_id not found, should create new memory."""
        service.memory_repo.list_by_resource_id = AsyncMock(return_value=[])

        new_memory_id = uuid4()
        mock_remember_response = MagicMock()
        mock_remember_response.memory_id = new_memory_id
        mock_remember_response.scope = "working"
        # #1519: RememberResponse always carries `persistence`; the upsert reuses it.
        mock_remember_response.persistence = None
        mock_remember_response.observed_at = None  # #2689: carried as well
        service.remember = AsyncMock(return_value=mock_remember_response)

        ctx_id = uuid4()
        ws_id = uuid4()
        request = UpdateMemoryRequest(
            external_id="new-resource",
            summary="Brand new memory for upsert test",
            content="Content for new memory",
            type="note",
        )

        result = await service._upsert_by_external_id(
            request,
            user_id="test_user",
            client="mcp",
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )

        assert result.operation == "created"
        assert result.memory_id == new_memory_id
        service.remember.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_replaces_when_exists(self, service):
        """When external_id found, should remember new then forget old."""
        existing = MagicMock()
        existing.id = uuid4()
        # #1519: a replacement inherits the existing row's delivery_mode when the
        # request omits it, so the stub must carry a real value.
        existing.delivery_mode = "on_recall"
        service.memory_repo.list_by_resource_id = AsyncMock(return_value=[existing])

        new_memory_id = uuid4()
        mock_remember_response = MagicMock()
        mock_remember_response.memory_id = new_memory_id
        mock_remember_response.scope = "working"
        # #1519: RememberResponse always carries `persistence`; the upsert reuses it.
        mock_remember_response.persistence = None
        mock_remember_response.observed_at = None  # #2689: carried as well
        service.remember = AsyncMock(return_value=mock_remember_response)

        mock_forget_response = MagicMock()
        mock_forget_response.deleted_count = 1
        service.forget = AsyncMock(return_value=mock_forget_response)

        ctx_id = uuid4()
        ws_id = uuid4()
        request = UpdateMemoryRequest(
            external_id="existing-resource",
            summary="Replacement memory for upsert test",
            content="New replacement content",
            type="note",
        )

        result = await service._upsert_by_external_id(
            request,
            user_id="test_user",
            client="mcp",
            current_context_id=ctx_id,
            current_workspace_id=ws_id,
        )

        assert result.operation == "replaced"
        assert result.memory_id == new_memory_id
        # remember should be called before forget (data safety)
        service.remember.assert_called_once()
        service.forget.assert_called_once()


class TestUpdateMemoryRequestValidation:
    """Test schema validation for UpdateMemoryRequest."""

    def test_requires_identifier(self):
        """Must provide either memory_id or external_id."""
        with pytest.raises(ValueError, match="Either memory_id or external_id"):
            UpdateMemoryRequest()

    def test_rejects_both_identifiers(self):
        """Cannot provide both memory_id and external_id."""
        with pytest.raises(ValueError, match="either memory_id or external_id"):
            UpdateMemoryRequest(
                memory_id=uuid4(),
                external_id="some-id",
                summary="Test summary for validation",
                content="Test content",
                type="note",
            )

    def test_upsert_requires_summary_content_type(self):
        """external_id mode requires summary, content, type."""
        with pytest.raises(ValueError, match="summary, content, and type are required"):
            UpdateMemoryRequest(
                external_id="some-id",
                summary="Has summary for test",
                # missing content and type
            )

    def test_upsert_rejects_empty_strings(self):
        """external_id mode rejects empty string values."""
        with pytest.raises(ValueError, match="summary, content, and type are required"):
            UpdateMemoryRequest(
                external_id="some-id",
                summary="Has summary for test",
                content="   ",  # whitespace only
                type="note",
            )

    def test_memory_id_mode_accepts_partial(self):
        """memory_id mode allows partial updates."""
        req = UpdateMemoryRequest(
            memory_id=uuid4(),
            tags=["new-tag"],
        )
        assert req.tags == ["new-tag"]
        assert req.summary is None

    # ------------------------------------------------ #2585 partial edits

    @pytest.mark.parametrize("edit", [{"add_tags": ["a"]}, {"remove_tags": ["a"]}])
    def test_tags_with_a_tag_edit_is_refused(self, edit):
        with pytest.raises(ValueError, match="give one or the other"):
            UpdateMemoryRequest(memory_id=uuid4(), tags=["a"], **edit)

    def test_details_with_details_patch_is_refused(self):
        with pytest.raises(ValueError, match="details_patch merges"):
            UpdateMemoryRequest(memory_id=uuid4(), details={"a": 1}, details_patch={"b": 2})

    def test_add_and_remove_tags_and_details_patch_go_together(self):
        req = UpdateMemoryRequest(
            memory_id=uuid4(),
            add_tags=["status:done"],
            remove_tags=["status:open"],
            details_patch={"a": None},
        )
        assert req.add_tags == ["status:done"]
        assert req.remove_tags == ["status:open"]
        assert req.details_patch == {"a": None}
        assert req.tags is None and req.details is None

    @pytest.mark.parametrize(
        "edit", [{"add_tags": ["a"]}, {"remove_tags": ["a"]}, {"details_patch": {"a": 1}}]
    )
    def test_partial_edits_are_refused_in_upsert_mode(self, edit):
        with pytest.raises(ValueError, match="use tags / details with external_id"):
            UpdateMemoryRequest(
                external_id="ext-1",
                summary="Has summary for test",
                content="c",
                type="note",
                **edit,
            )

    @pytest.mark.parametrize(
        "edit", [{"add_tags": ["a"]}, {"remove_tags": ["a"]}, {"details_patch": {"a": 1}}]
    )
    def test_partial_edits_count_as_field_edits_for_an_acceptance(self, edit):
        with pytest.raises(ValueError, match="cannot be combined with field edits"):
            UpdateMemoryRequest(memory_id=uuid4(), accept_supersede_candidate=True, **edit)

    def test_edit_fields_name_the_partial_edits(self):
        from models.schemas import UPDATE_MEMORY_EDIT_FIELDS

        assert {"add_tags", "remove_tags", "details_patch"} <= set(UPDATE_MEMORY_EDIT_FIELDS)


# --------------------------------------------------------------------------
# #2585: add_tags / remove_tags / details_patch on the in-place path
# --------------------------------------------------------------------------


def _stored(**overrides):
    """A locked row with real values on every field the edit path reads."""
    memory = MagicMock()
    memory.id = uuid4()
    memory.point_id = memory.id
    memory.user_id = "test_user"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "stored summary text"
    memory.context_summary = None
    memory.content = "stored content"
    memory.details = None
    memory.type = "note"
    memory.importance = 0.5
    memory.tags = ["original"]
    memory.context = None
    memory.scope = "working"
    memory.delivery_mode = "on_recall"
    memory.is_pinned = False
    memory.updated_at = None
    memory.deleted_at = None
    memory.supersede_candidate = None
    memory.version = 3
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


def _partial_service(memory):
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    svc = MemoryService(db)
    svc._update_load_authorized = AsyncMock(return_value=memory)
    svc._lock_for_edit = AsyncMock(return_value=memory)
    svc._ensure_capacity = AsyncMock()
    svc._require_guardrail_author = AsyncMock()
    svc._sync_metadata_payload = AsyncMock()
    svc._lint_write = AsyncMock(return_value=[])
    return svc


async def _edit(svc, **fields):
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await svc._update_in_place(UpdateMemoryRequest(**fields), user_id="test_user")


def _synced(svc):
    """(payload_updates, delete_keys) of the metadata-only payload sync."""
    _memory, updates, delete_keys = svc._sync_metadata_payload.await_args.args
    return updates, delete_keys


@pytest.mark.asyncio
class TestPartialTagEdits:
    async def test_removals_apply_first_then_new_tags_are_appended_once(self):
        memory = _stored(tags=["a", "status:open", "b"])
        svc = _partial_service(memory)
        await _edit(
            svc,
            memory_id=memory.id,
            remove_tags=["status:open", "not-there"],
            add_tags=["status:done", "a", "status:done"],
        )
        assert memory.tags == ["a", "b", "status:done"]
        updates, delete_keys = _synced(svc)
        assert updates["tags"] == ["a", "b", "status:done"]
        assert delete_keys == []

    async def test_a_tag_removed_and_added_in_one_call_moves_to_the_end(self):
        memory = _stored(tags=["a", "b"])
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, remove_tags=["a"], add_tags=["a"])
        assert memory.tags == ["b", "a"]

    async def test_add_tags_on_a_row_without_tags(self):
        memory = _stored(tags=None)
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, add_tags=["x"])
        assert memory.tags == ["x"]

    async def test_a_status_flip_touches_nothing_else(self):
        details = {"k": "v", "location": {"lat": 35.0, "lon": 139.0}}
        memory = _stored(tags=["memo", "status:open", "file:a.py"], details=details)
        svc = _partial_service(memory)
        result = await _edit(
            svc, memory_id=memory.id, remove_tags=["status:open"], add_tags=["status:done"]
        )
        assert memory.tags == ["memo", "file:a.py", "status:done"]
        assert memory.details is details and memory.summary == "stored summary text"
        assert result.re_embedded is False
        updates, delete_keys = _synced(svc)
        assert set(updates) == {"tags", "updated_at"}
        assert delete_keys == []
        svc._require_guardrail_author.assert_not_awaited()

    async def test_the_resulting_list_takes_the_caps(self):
        stored = [f"t{i}" for i in range(50)]
        memory = _stored(tags=list(stored))
        svc = _partial_service(memory)
        with pytest.raises(ValueError, match=r"at most 50 tags \(got 51\)"):
            await _edit(svc, memory_id=memory.id, add_tags=["one-more"])
        assert memory.tags == stored
        svc.db.commit.assert_not_awaited()
        # Room made in the same call: removals come first.
        await _edit(svc, memory_id=memory.id, remove_tags=["t0"], add_tags=["one-more"])
        assert len(memory.tags) == 50 and memory.tags[-1] == "one-more"

    async def test_a_stored_over_long_tag_survives_a_tag_edit(self):
        """The length cap applies to the tags a call adds; a stored tag over it
        (written before the #1743 caps) does not block a flip that leaves it alone."""
        long_tag = "x" * 101
        memory = _stored(tags=["a", long_tag, "status:open"])
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, remove_tags=["status:open"], add_tags=["status:done"])
        assert memory.tags == ["a", long_tag, "status:done"]
        await _edit(svc, memory_id=memory.id, remove_tags=[long_tag])
        assert memory.tags == ["a", "status:done"]

    async def test_lint_checks_only_the_added_tags(self):
        memory = _stored(tags=["a", "status:open"])
        svc = _partial_service(memory)
        await _edit(
            svc, memory_id=memory.id, remove_tags=["status:open"], add_tags=["a", "status:done"]
        )
        kwargs = svc._lint_write.await_args.kwargs
        assert kwargs["tags"] == ["a", "status:done"]
        assert kwargs["checked_tags"] == ["status:done"]

    async def test_a_wholesale_tags_write_lints_every_tag(self):
        memory = _stored(tags=["a"])
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, tags=["a", "b"])
        assert svc._lint_write.await_args.kwargs["checked_tags"] is None


@pytest.mark.asyncio
class TestDetailsPatch:
    async def test_a_patch_merges_and_a_null_deletes_the_key(self):
        stored = {"keep": 1, "drop": 2, "change": 3}
        memory = _stored(details=stored)
        svc = _partial_service(memory)
        await _edit(
            svc,
            memory_id=memory.id,
            details_patch={"drop": None, "change": 4, "new": {"n": 5}, "absent": None},
        )
        assert memory.details == {"keep": 1, "change": 4, "new": {"n": 5}}
        assert stored == {"keep": 1, "drop": 2, "change": 3}  # a new dict, not a mutation
        assert memory.tags == ["original"]

    async def test_a_patch_on_a_row_without_details_merges_into_an_empty_object(self):
        memory = _stored(details=None)
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, details_patch={"new": 1})
        assert memory.details == {"new": 1}

    async def test_an_invalid_trigger_fails_through_a_patch_as_it_does_wholesale(self):
        trigger = MemoryService._apply_time_trigger("time", {"trigger": {"year": 2026, "month": 5}})
        bad = {"trigger": {"year": 2026, "month": 13}}

        memory = _stored(type="time", details=dict(trigger or {}))
        with pytest.raises(ValueError, match="invalid details.trigger") as wholesale:
            await _edit(_partial_service(memory), memory_id=memory.id, details=bad)
        with pytest.raises(ValueError, match="invalid details.trigger") as patched:
            await _edit(_partial_service(memory), memory_id=memory.id, details_patch=bad)
        assert str(patched.value) == str(wholesale.value)
        assert memory.details == trigger

    async def test_a_patch_keeps_a_time_memory_window_normalized(self):
        trigger = MemoryService._apply_time_trigger("time", {"trigger": {"year": 2026, "month": 5}})
        memory = _stored(type="time", details=dict(trigger or {}))
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, details_patch={"trigger": {"year": 2027, "month": 1}})
        assert memory.details["trigger"]["from"] == "2027-01-01T00:00:00"

    async def test_an_invalid_location_fails_through_a_patch(self):
        memory = _stored(details={"k": "v"})
        with pytest.raises(ValueError, match="invalid details.location"):
            await _edit(
                _partial_service(memory),
                memory_id=memory.id,
                details_patch={"location": {"lat": 999, "lon": 0}},
            )
        assert memory.details == {"k": "v"}

    async def test_a_location_patch_syncs_the_payload(self):
        memory = _stored(details={"k": "v"})
        svc = _partial_service(memory)
        await _edit(
            svc, memory_id=memory.id, details_patch={"location": {"lat": 35.0, "lon": 139.0}}
        )
        updates, _ = _synced(svc)
        assert updates["location"] == {"lat": 35.0, "lon": 139.0}

    async def test_observed_at_null_through_a_patch_removes_it(self):
        memory = _stored(details={"observed_at": "2026-01-02", "x": 1})
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, details_patch={"observed_at": None})
        assert memory.details == {"x": 1}
        _, delete_keys = _synced(svc)
        assert "observed_at" in delete_keys

    async def test_observed_at_and_a_patch_that_deletes_it_disagree(self):
        memory = _stored(details={"observed_at": "2026-01-02"})
        with pytest.raises(ValueError, match="disagree"):
            await _edit(
                _partial_service(memory),
                memory_id=memory.id,
                observed_at="2026-03-04",
                details_patch={"observed_at": None},
            )

    async def test_observed_at_folds_into_a_patch(self):
        memory = _stored(details={"x": 1})
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, observed_at="2026-03-04", details_patch={"x": 2})
        assert memory.details == {"x": 2, "observed_at": "2026-03-04"}
        updates, _ = _synced(svc)
        assert updates["observed_at"]

    async def test_the_size_guard_measures_the_merged_details(self):
        from config.constants import MAX_CONTENT_SIZE
        from utils.exceptions import ContentSizeExceededError

        memory = _stored(details={"big": "x" * (MAX_CONTENT_SIZE - 200)})
        with pytest.raises(ContentSizeExceededError):
            await _edit(
                _partial_service(memory), memory_id=memory.id, details_patch={"more": "y" * 400}
            )

    async def test_a_plain_patch_needs_no_guardrail_author(self):
        memory = _stored(details={"x": 1})
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, details_patch={"x": 2})
        svc._require_guardrail_author.assert_not_awaited()

    @pytest.mark.parametrize("value", [None, {"tool": "Bash", "match": "rm", "action": "inform"}])
    async def test_a_patch_naming_tool_trigger_needs_a_guardrail_author(self, value):
        """A null counts too, although there is nothing to delete on this row."""
        memory = _stored(details={"x": 1})
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, details_patch={"tool_trigger": value})
        svc._require_guardrail_author.assert_awaited_once_with("test_user", memory.context_id)

    async def test_a_patch_of_a_guardrail_row_needs_a_guardrail_author(self):
        trigger = MemoryService._apply_tool_trigger(
            {"tool_trigger": {"tool": "Bash", "match": "rm", "action": "inform"}}
        )
        memory = _stored(details=trigger)
        svc = _partial_service(memory)
        await _edit(svc, memory_id=memory.id, details_patch={"note": "kept"})
        svc._require_guardrail_author.assert_awaited_once()
        assert memory.details["tool_trigger"] == (trigger or {})["tool_trigger"]
        assert memory.details["note"] == "kept"

    async def test_a_denied_guardrail_author_writes_nothing(self):
        from utils.exceptions import AuthorizationError

        memory = _stored(details={"x": 1})
        svc = _partial_service(memory)
        svc._require_guardrail_author = AsyncMock(side_effect=AuthorizationError("denied"))
        with pytest.raises(AuthorizationError):
            await _edit(
                svc,
                memory_id=memory.id,
                details_patch={"tool_trigger": {"tool": "Bash", "match": "rm", "action": "block"}},
            )
        assert memory.details == {"x": 1}
        svc.db.commit.assert_not_awaited()

    async def test_an_invalid_tool_trigger_fails_through_a_patch(self):
        memory = _stored(details={"x": 1})
        with pytest.raises(ValueError, match="invalid details.tool_trigger"):
            await _edit(
                _partial_service(memory),
                memory_id=memory.id,
                details_patch={"tool_trigger": "nope"},
            )
