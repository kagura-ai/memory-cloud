"""Tests for MemoryService.patch_memory (Issue #439).

Covers the four #439-specific behaviors that diverge from `_update_in_place`:
  - Soft-deleted memory raises MemoryGoneError (410), not NotFoundException (404)
  - Permission denial raises NotFoundException (no leak), not silent success
  - summary/content change invalidates neural edges (forget's pattern)
  - Qdrant payload-only failure logs error but does not raise (drift visibility)

Plus the explicit-null `details` clear semantic (`{"details": null}` clears the
column; omitting `details` preserves it).
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import PatchMemoryRequest
from services.memory_service import MemoryService
from utils.exceptions import MemoryGoneError, NotFoundException


def _make_memory(**overrides):
    """Mock Memory with sensible defaults and the columns patch_memory touches.

    Fields populated to valid types so the inline ReferenceResponse
    construction at the end of `patch_memory` passes Pydantic validation
    without needing extra wiring per test.
    """
    memory = MagicMock()
    memory.id = overrides.get("id", uuid4())
    # An API-written row: its point is its own id (#2100).
    memory.point_id = overrides.get("point_id", memory.id)
    memory.user_id = overrides.get("user_id", "test_user")
    memory.workspace_id = overrides.get("workspace_id", uuid4())
    memory.context_id = overrides.get("context_id", uuid4())
    memory.summary = overrides.get("summary", "Original summary for testing edits")
    memory.context_summary = overrides.get("context_summary", None)
    memory.content = overrides.get("content", "Original content body")
    memory.details = overrides.get("details", None)
    memory.type = overrides.get("type", "normal")
    memory.importance = overrides.get("importance", 0.5)
    memory.tags = overrides.get("tags", ["original"])
    memory.context = overrides.get("context", None)
    memory.scope = overrides.get("scope", "working")
    memory.client = overrides.get("client", "mcp")
    memory.created_at = overrides.get("created_at", datetime(2026, 4, 25, 10, 0, 0))
    memory.updated_at = overrides.get("updated_at", datetime(2026, 4, 25, 10, 0, 0))
    memory.deleted_at = overrides.get("deleted_at", None)
    memory.deleted_by = overrides.get("deleted_by", None)
    memory.embedding_status = overrides.get("embedding_status", "success")
    memory.source_uri = overrides.get("source_uri", None)
    memory.source_type = overrides.get("source_type", None)
    return memory


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def service(mock_db):
    return MemoryService(mock_db)


class TestPatchMemoryNotFound:
    """Memory does not exist or is unreachable."""

    @pytest.mark.asyncio
    async def test_missing_row_raises_not_found(self, service):
        service.memory_repo.get = AsyncMock(return_value=None)

        with pytest.raises(NotFoundException):
            await service.patch_memory(
                memory_id=uuid4(),
                request=PatchMemoryRequest(importance=0.9),
                user_id="test_user",
            )

    @pytest.mark.asyncio
    async def test_permission_denied_raises_not_found(self, service):
        """Access check failure must NOT silently succeed (forget's pattern is wrong here)."""
        memory = _make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        with patch("services.permission_service.PermissionService") as mock_perm_cls:
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=False)

            with pytest.raises(NotFoundException):
                await service.patch_memory(
                    memory_id=memory.id,
                    request=PatchMemoryRequest(importance=0.9),
                    user_id="other_user",
                )


class TestPatchMemorySoftDeleted:
    """Soft-deleted memories must surface as 410 Gone — but only to authorized callers.

    Permission denial fires BEFORE the soft-delete check (CSO security review):
    a non-member must not distinguish "soft-deleted" (would-be 410) from
    "never existed" (404), or they could confirm a memory was once real
    just by guessing a UUID.
    """

    @pytest.mark.asyncio
    async def test_soft_deleted_raises_memory_gone_when_authorized(self, service):
        memory = _make_memory(deleted_at=datetime(2026, 4, 20))
        service.memory_repo.get = AsyncMock(return_value=memory)

        with patch("services.permission_service.PermissionService") as mock_perm_cls:
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            with pytest.raises(MemoryGoneError) as excinfo:
                await service.patch_memory(
                    memory_id=memory.id,
                    request=PatchMemoryRequest(importance=0.9),
                    user_id="test_user",
                )
            assert excinfo.value.status_code == 410

        # #1316: repo.get() excludes tombstones by default — patch is the one
        # by-id caller that must opt in, or the 410 contract silently dies.
        service.memory_repo.get.assert_awaited_once_with(memory.id, include_deleted=True)

    @pytest.mark.asyncio
    async def test_soft_deleted_to_unauthorized_returns_not_found(self, service):
        """Existence-leak guard: 404 (not 410) for non-members on a deleted memory."""
        memory = _make_memory(deleted_at=datetime(2026, 4, 20))
        service.memory_repo.get = AsyncMock(return_value=memory)

        with patch("services.permission_service.PermissionService") as mock_perm_cls:
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=False)

            with pytest.raises(NotFoundException):
                await service.patch_memory(
                    memory_id=memory.id,
                    request=PatchMemoryRequest(importance=0.9),
                    user_id="other_user",
                )


class TestPatchMemoryEmbeddingRegen:
    """summary / content changes trigger re-embed + neural edge invalidation."""

    @pytest.mark.asyncio
    async def test_summary_change_invalidates_neural_edges_and_commits(self, service, mock_db):
        """Copilot loop 1: edge DELETE must be committed (repo doesn't commit internally)."""
        memory = _make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        edge_repo_instance = MagicMock()
        edge_repo_instance.delete_node_edges = AsyncMock(return_value=3)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=edge_repo_instance,
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(
                    summary="A completely new summary that triggers re-embedding",
                ),
                user_id="test_user",
            )

            edge_repo_instance.delete_node_edges.assert_awaited_once()
            # Two commits expected: (1) memory update, (2) edge invalidation.
            assert mock_db.commit.await_count >= 2, (
                "edge invalidation must commit; otherwise the DELETE is discarded"
            )
            assert memory.embedding_status == "pending"

    @pytest.mark.asyncio
    async def test_content_change_triggers_reembed(self, service):
        memory = _make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        edge_repo_instance = MagicMock()
        edge_repo_instance.delete_node_edges = AsyncMock(return_value=0)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
            patch(
                "repositories.neural_edge.NeuralEdgeRepository",
                return_value=edge_repo_instance,
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(content="Brand new content body"),
                user_id="test_user",
            )

            assert memory.embedding_status == "pending"
            edge_repo_instance.delete_node_edges.assert_awaited_once()


class TestPatchMemoryMetadataOnly:
    """tags / importance / type / details changes do NOT re-embed."""

    @pytest.mark.asyncio
    async def test_metadata_only_no_reembed(self, service, mock_db):
        memory = _make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.update_memory_payload_in_qdrant",
                new=AsyncMock(),
            ) as mock_payload_update,
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(
                    importance=0.9,
                    tags=["new", "tags"],
                ),
                user_id="test_user",
            )

            assert memory.embedding_status == "success"  # unchanged
            mock_payload_update.assert_awaited_once()
            mock_db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_quota_check_uses_post_patch_size_for_clear(self, service):
        """Copilot loop 2: clearing details on a near-quota memory must not 422.

        The pre-fix `len(str(request.details or memory.details or ""))` form
        kept the OLD value when `request.details` was `None` or `{}`, so a
        patch that intended to SHRINK the row was rejected against the
        pre-shrink size.
        """
        from config.constants import MAX_CONTENT_SIZE

        # Memory with details near the quota — clearing them should pass.
        big_details = {"k": "x" * (MAX_CONTENT_SIZE - 100)}
        memory = _make_memory(details=big_details, summary="ok summary baseline")
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.update_memory_payload_in_qdrant",
                new=AsyncMock(),
            ),
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            # Send `details: null` to clear — must NOT raise QuotaExceededError.
            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(details=None),
                user_id="test_user",
            )
            assert memory.details is None

    @pytest.mark.asyncio
    async def test_qdrant_payload_failure_does_not_raise(self, service):
        """Drift visibility: qdrant fail after PG commit logs error, no rollback."""
        memory = _make_memory()
        service.memory_repo.get = AsyncMock(return_value=memory)

        failing_qdrant = AsyncMock(side_effect=RuntimeError("qdrant down"))

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.update_memory_payload_in_qdrant",
                new=failing_qdrant,
            ),
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            # Must NOT raise even though qdrant errored.
            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(importance=0.9),
                user_id="test_user",
            )


class TestPatchMemoryDetailsClear:
    """Issue #439: explicitly setting `details: null` must clear the column."""

    @pytest.mark.asyncio
    async def test_details_explicit_null_clears_column(self, service):
        memory = _make_memory(details={"existing": "value"})
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.update_memory_payload_in_qdrant",
                new=AsyncMock(),
            ),
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(details=None),
                user_id="test_user",
            )

            # Explicit None must clear the JSON column.
            assert memory.details is None

    @pytest.mark.asyncio
    async def test_details_omitted_preserves_column(self, service):
        existing = {"keep_me": "yes"}
        memory = _make_memory(details=existing)
        service.memory_repo.get = AsyncMock(return_value=memory)

        with (
            patch("services.permission_service.PermissionService") as mock_perm_cls,
            patch(
                "services.memory_service.update_memory_payload_in_qdrant",
                new=AsyncMock(),
            ),
            patch(
                "services.memory_service.resolve_collection_name",
                new=AsyncMock(return_value="kagura_memories"),
            ),
            patch.object(
                service,
                "_fetch_declared_link_refs",
                new=AsyncMock(return_value=([], False, [], False)),
            ),
        ):
            mock_perm = mock_perm_cls.return_value
            mock_perm.can_access_memory = AsyncMock(return_value=True)

            # Patch only `importance`; omit `details`.
            await service.patch_memory(
                memory_id=memory.id,
                request=PatchMemoryRequest(importance=0.8),
                user_id="test_user",
            )

            assert memory.details == existing


class TestPatchMemoryRequestValidation:
    """Pydantic-level validation of the request body."""

    def test_empty_patch_rejected(self):
        with pytest.raises(ValueError, match="at least one field"):
            PatchMemoryRequest()

    def test_importance_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            PatchMemoryRequest(importance=1.5)

    def test_summary_too_short_rejected(self):
        with pytest.raises(ValueError):
            PatchMemoryRequest(summary="short")

    def test_type_too_long_rejected(self):
        with pytest.raises(ValueError):
            PatchMemoryRequest(type="x" * 51)

    def test_minimal_valid_patch(self):
        req = PatchMemoryRequest(importance=0.7)
        assert req.importance == 0.7
        assert req.summary is None

    def test_tags_array_max_length_enforced(self):
        """CSO #2: prevent unbounded PG ARRAY bloat from authenticated members."""
        with pytest.raises(ValueError):
            PatchMemoryRequest(tags=["x"] * 101)

    def test_tags_array_at_max_length_accepted(self):
        req = PatchMemoryRequest(tags=["x"] * 100)
        assert len(req.tags) == 100

    def test_explicit_null_rejected_for_non_clearable_fields(self):
        """Copilot loop 1: sending `{field: null}` for NOT NULL columns must 422, not 500."""
        for field in ("summary", "content", "type", "importance", "tags"):
            with pytest.raises(ValueError):
                PatchMemoryRequest(**{field: None})

    def test_explicit_null_accepted_only_for_details(self):
        req = PatchMemoryRequest(details=None)
        assert "details" in req.model_fields_set
        assert req.details is None

    def test_per_tag_length_cap_enforced(self):
        """Copilot loop 3: per-tag length cap (64 chars) — not just array length."""
        with pytest.raises(ValueError, match="exceeds 64 chars"):
            PatchMemoryRequest(tags=["x" * 65])

    def test_per_tag_length_at_cap_accepted(self):
        req = PatchMemoryRequest(tags=["x" * 64, "y" * 64])
        assert len(req.tags) == 2
        assert all(len(t) == 64 for t in req.tags)

    # ------------------------------------------------ #2585 partial edits

    @pytest.mark.parametrize("edit", [{"add_tags": ["a"]}, {"remove_tags": ["a"]}])
    def test_tags_with_a_tag_edit_is_refused(self, edit):
        with pytest.raises(ValueError, match="give one or the other"):
            PatchMemoryRequest(tags=["a"], **edit)

    @pytest.mark.parametrize("details", [{"a": 1}, None])
    def test_details_with_details_patch_is_refused(self, details):
        """Presence counts: ``{"details": null}`` replaces (clears) as well."""
        with pytest.raises(ValueError, match="details_patch merges"):
            PatchMemoryRequest(details=details, details_patch={"b": 2})

    def test_partial_edits_alone_are_a_patch(self):
        req = PatchMemoryRequest(add_tags=["b"], remove_tags=["a"], details_patch={"k": None})
        assert {"add_tags", "remove_tags", "details_patch"} <= req.model_fields_set

    @pytest.mark.parametrize("field", ["add_tags", "remove_tags", "details_patch"])
    def test_explicit_null_rejected_for_partial_edits(self, field):
        with pytest.raises(ValueError, match="must not be null"):
            PatchMemoryRequest(**{field: None})

    def test_add_tags_take_the_patch_caps(self):
        with pytest.raises(ValueError):
            PatchMemoryRequest(add_tags=["x"] * 101)
        with pytest.raises(ValueError, match="exceeds 64 chars"):
            PatchMemoryRequest(add_tags=["x" * 65])
        # A removal never lands on the row: a longer stored tag can be removed.
        assert PatchMemoryRequest(remove_tags=["x" * 100]).remove_tags == ["x" * 100]


# --------------------------------------------------------------------------
# #2585: add_tags / remove_tags / details_patch on PATCH
# --------------------------------------------------------------------------


def _patch_service(memory):
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()
    svc = MemoryService(db)
    svc._patch_load_authorized = AsyncMock(return_value=memory)
    svc._lock_for_edit = AsyncMock(return_value=memory)
    svc._ensure_capacity = AsyncMock()
    svc._require_guardrail_author = AsyncMock()
    svc._sync_metadata_payload = AsyncMock()
    svc._patch_build_response = AsyncMock(return_value=MagicMock())
    return svc


async def _patch_partial(svc, memory, **fields):
    with patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()):
        return await svc.patch_memory(memory.id, PatchMemoryRequest(**fields), "test_user")


def _patch_synced(svc):
    _memory, updates, delete_keys = svc._sync_metadata_payload.await_args.args
    return updates, delete_keys


@pytest.mark.asyncio
class TestPatchPartialEdits:
    async def test_add_and_remove_tags_edit_the_stored_list(self):
        memory = _make_memory(tags=["a", "status:open", "b"])
        svc = _patch_service(memory)
        await _patch_partial(
            svc, memory, remove_tags=["status:open", "nope"], add_tags=["status:done", "a"]
        )
        assert memory.tags == ["a", "b", "status:done"]
        updates, delete_keys = _patch_synced(svc)
        assert updates["tags"] == ["a", "b", "status:done"]
        assert delete_keys == []

    async def test_the_resulting_list_takes_the_patch_caps(self):
        stored = [f"t{i}" for i in range(100)]
        memory = _make_memory(tags=list(stored))
        svc = _patch_service(memory)
        with pytest.raises(ValueError, match=r"at most 100 tags \(got 101\)"):
            await _patch_partial(svc, memory, add_tags=["one-more"])
        assert memory.tags == stored
        svc.db.commit.assert_not_awaited()

    async def test_a_stored_tag_over_the_patch_length_fails_the_result(self):
        """A 100-character tag is legal through MCP, not through PATCH; the
        PATCH caps apply to the list the call produces."""
        memory = _make_memory(tags=["y" * 80])
        svc = _patch_service(memory)
        with pytest.raises(ValueError, match="exceeds 64 chars"):
            await _patch_partial(svc, memory, add_tags=["b"])
        await _patch_partial(svc, memory, remove_tags=["y" * 80], add_tags=["b"])
        assert memory.tags == ["b"]

    async def test_details_patch_on_a_null_column_merges_into_an_empty_object(self):
        memory = _make_memory(details=None)
        svc = _patch_service(memory)
        await _patch_partial(svc, memory, details_patch={"k": 1, "gone": None})
        assert memory.details == {"k": 1}

    async def test_details_patch_merges_and_null_deletes(self):
        memory = _make_memory(details={"observed_at": "2026-01-02", "x": 1, "y": 2})
        svc = _patch_service(memory)
        await _patch_partial(svc, memory, details_patch={"observed_at": None, "y": 3})
        assert memory.details == {"x": 1, "y": 3}
        _, delete_keys = _patch_synced(svc)
        assert "observed_at" in delete_keys and "location" in delete_keys

    async def test_an_invalid_trigger_fails_through_a_patch(self):
        trigger = MemoryService._apply_time_trigger("time", {"trigger": {"year": 2026, "month": 5}})
        memory = _make_memory(type="time", details=dict(trigger or {}))
        svc = _patch_service(memory)
        with pytest.raises(ValueError, match="invalid details.trigger"):
            await _patch_partial(
                svc, memory, details_patch={"trigger": {"year": 2026, "month": 13}}
            )
        assert memory.details == trigger

    async def test_a_patch_naming_tool_trigger_needs_a_guardrail_author(self):
        memory = _make_memory(details={"x": 1})
        svc = _patch_service(memory)
        await _patch_partial(svc, memory, details_patch={"tool_trigger": None})
        svc._require_guardrail_author.assert_awaited_once_with("test_user", memory.context_id)
        assert memory.details == {"x": 1}

    async def test_observed_at_folds_into_a_patch(self):
        memory = _make_memory(details={"x": 1})
        svc = _patch_service(memory)
        await _patch_partial(svc, memory, observed_at="2026-03-04", details_patch={"x": 2})
        assert memory.details == {"x": 2, "observed_at": "2026-03-04"}
