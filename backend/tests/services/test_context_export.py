"""Tests for ContextService.export_context (Issue #950, GDPR Art.20 portability).

The export's authorization + visibility mirror GET /memory/list exactly:
resolve via PermissionService.resolve_context_for_workspace_read (uniform 404),
then a private context exports only the caller's own memories while a shared
context exports every member's. These tests pin that contract, the serialized
shape, the regenerable-field exclusion, the null search-config path, and the
single-export size cap (413).
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.context_service import ContextService
from utils.exceptions import ExportTooLargeError, NotFoundException

USER = "user-1"


def _ctx(*, is_private: bool):
    c = MagicMock()
    c.id = uuid4()
    c.name = "proj"
    c.display_name = "Proj"
    c.description = "desc"
    c.summary = "sum"
    c.usage_guide = None
    c.is_private = is_private
    c.is_public = False
    c.created_at = datetime(2026, 1, 1)
    c.updated_at = None
    return c


def _mem(user_id: str = USER):
    m = MagicMock()
    m.id = uuid4()
    m.summary = "s"
    m.context_summary = None
    m.content = "c"
    m.details = {"k": "v"}
    m.type = "note"
    m.importance = 0.8
    m.confidence = 0.9
    m.tags = ["a"]
    m.context = None
    m.scope = "persistent"
    m.delivery_mode = "on_recall"
    m.created_at = datetime(2026, 1, 2)
    m.updated_at = None
    m.source_uri = None
    m.source_type = None
    m.user_id = user_id
    return m


def _cfg():
    c = MagicMock()
    c.semantic_weight = Decimal("0.60")
    c.bm25_weight = Decimal("0.40")
    c.fetch_factor = 3
    c.use_rerank = False
    c.reranker_provider = "voyage"
    c.reranker_model = "rerank-2"
    c.embedding_model = "text-embedding-3-small"
    c.embedding_dimensions = 512
    # #1207: real attribute values (bare MagicMock attrs would be silently
    # coerced by pydantic to True/1.0, making assertions vacuous). False
    # mirrors an explicit opt-out that must survive the portability boundary.
    c.reinforce_enabled = False
    c.reinforce_max_boost = Decimal("0.15")
    c.reinforce_require_host_arbitration = False
    # #1212: pin so the MagicMock attribute doesn't fail Pydantic validation.
    c.routing_mode = "off"
    return c


def _db_with(mem_rows, cfg):
    """AsyncMock db: 1st execute -> memories, 2nd -> search config row."""
    db = AsyncMock()
    mem_res = MagicMock()
    mem_res.scalars.return_value.all.return_value = mem_rows
    cfg_res = MagicMock()
    cfg_res.scalar_one_or_none.return_value = cfg
    db.execute.side_effect = [mem_res, cfg_res]
    return db


def _patch_perm(ctx=None, *, raises=None):
    perm = MagicMock()
    perm.resolve_context_for_workspace_read = AsyncMock(return_value=ctx, side_effect=raises)
    return patch("services.permission_service.PermissionService", return_value=perm), perm


def _mem_where_sql(db, call_index: int = 0) -> str:
    stmt = db.execute.call_args_list[call_index].args[0]
    return str(stmt.whereclause.compile(compile_kwargs={"literal_binds": False}))


@pytest.mark.asyncio
async def test_export_private_context_is_creator_scoped_and_serializes():
    ctx = _ctx(is_private=True)
    db = _db_with([_mem(), _mem()], _cfg())
    perm_patch, _ = _patch_perm(ctx)

    with perm_patch:
        out = await ContextService(db).export_context(USER, ctx.id, key_workspace_id=None)

    assert out.format_version == "1.0"
    assert out.memory_count == 2
    assert len(out.memories) == 2
    assert out.context.name == "proj"
    assert out.context.is_private is True
    assert out.memories[0].type == "note"
    assert out.memories[0].tags == ["a"]
    assert out.search_config is not None
    assert out.search_config.semantic_weight == 0.6
    assert out.search_config.embedding_dimensions == 512
    # #1207: an explicit opt-out must survive export — without these fields a
    # re-created context would silently pick up the new default-on.
    assert out.search_config.reinforce_enabled is False
    assert out.search_config.reinforce_max_boost == 0.15
    assert out.search_config.reinforce_require_host_arbitration is False

    # Private context => the memory query MUST carry a user_id (creator) filter
    # alongside context_id + the soft-delete guard.
    sql = _mem_where_sql(db, 0)
    assert "user_id" in sql
    assert "context_id" in sql
    assert "deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_export_shared_context_drops_user_filter():
    ctx = _ctx(is_private=False)
    db = _db_with([_mem("other-user")], _cfg())
    perm_patch, _ = _patch_perm(ctx)

    with perm_patch:
        out = await ContextService(db).export_context(USER, ctx.id, key_workspace_id=None)

    assert out.memory_count == 1
    # Shared context => every member's memories; NO user_id predicate, but
    # context_id + deleted_at guards remain.
    sql = _mem_where_sql(db, 0)
    assert "user_id" not in sql
    assert "context_id" in sql
    assert "deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_export_forwards_key_workspace_id():
    ctx = _ctx(is_private=False)
    db = _db_with([], None)
    perm_patch, perm = _patch_perm(ctx)
    key_ws = uuid4()

    with perm_patch:
        await ContextService(db).export_context(USER, ctx.id, key_workspace_id=key_ws)

    perm.resolve_context_for_workspace_read.assert_awaited_once_with(
        user_id=USER, context_id=ctx.id, key_workspace_id=key_ws
    )


@pytest.mark.asyncio
async def test_export_no_search_config_yields_null():
    ctx = _ctx(is_private=False)
    db = _db_with([_mem()], None)  # no ContextSearchConfig row
    perm_patch, _ = _patch_perm(ctx)

    with perm_patch:
        out = await ContextService(db).export_context(USER, ctx.id)

    assert out.search_config is None
    assert out.memory_count == 1


@pytest.mark.asyncio
async def test_export_missing_or_forbidden_context_propagates_404():
    cid = uuid4()
    db = AsyncMock()
    perm_patch, _ = _patch_perm(raises=NotFoundException("Context", str(cid)))

    with perm_patch:
        with pytest.raises(NotFoundException) as exc:
            await ContextService(db).export_context(USER, cid)

    assert exc.value.status_code == 404
    db.execute.assert_not_called()  # never reaches the memory query


@pytest.mark.asyncio
async def test_export_too_large_raises_413():
    ctx = _ctx(is_private=False)
    # Patch the cap low so we don't have to fabricate 50k rows; the service
    # fetches cap+1 and raises when the result exceeds the cap.
    with patch("services.context_service.EXPORT_MAX_MEMORIES", 2):
        db = AsyncMock()
        mem_res = MagicMock()
        mem_res.scalars.return_value.all.return_value = [_mem(), _mem(), _mem()]  # 3 > 2
        db.execute.side_effect = [mem_res]
        perm_patch, _ = _patch_perm(ctx)
        with perm_patch:
            with pytest.raises(ExportTooLargeError) as exc:
                await ContextService(db).export_context(USER, ctx.id)

    assert exc.value.status_code == 413
    assert exc.value.error_code == "EXPORT-001"
    assert exc.value.details["memory_count"] == 3
    assert exc.value.details["limit"] == 2


# --- Route-level delegation + identity guard (api/routes/contexts.py) ---------


@pytest.mark.asyncio
async def test_route_delegates_and_forwards_identity():
    from api.routes.contexts import export_context as route_export

    svc = MagicMock()
    svc.export_context = AsyncMock(return_value="SENTINEL")
    cid = uuid4()
    key = uuid4()

    out = await route_export(
        context_id=cid,
        user={"user_id": USER, "api_key_workspace_id": key},
        service=svc,
    )

    assert out == "SENTINEL"
    svc.export_context.assert_awaited_once_with(user_id=USER, context_id=cid, key_workspace_id=key)


@pytest.mark.asyncio
async def test_route_missing_identity_returns_401():
    from fastapi import HTTPException

    from api.routes.contexts import export_context as route_export

    svc = MagicMock()
    svc.export_context = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await route_export(context_id=uuid4(), user={}, service=svc)

    assert exc.value.status_code == 401
    svc.export_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_omits_regenerable_and_internal_fields():
    """Regression guard (#950): the serialized memory must NOT carry vector /
    embedding fields, internal owner/workspace identifiers, or soft-delete
    state. Pins the exclusion so a future field added to ExportedMemory can't
    silently leak these into a portability export."""
    ctx = _ctx(is_private=False)
    db = _db_with([_mem()], _cfg())
    perm_patch, _ = _patch_perm(ctx)

    with perm_patch:
        out = await ContextService(db).export_context(USER, ctx.id)

    dumped = out.memories[0].model_dump()
    for forbidden in (
        "summary_embedding_id",
        "embedding_status",
        "embedding_error",
        "user_id",
        "workspace_id",
        "context_id",
        "deleted_at",
        "deleted_by",
    ):
        assert forbidden not in dumped, f"{forbidden} leaked into export"
