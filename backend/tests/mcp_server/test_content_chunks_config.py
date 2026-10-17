"""#2015: ContextSearchConfig.content_chunks_enabled — the per-context opt-in."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from models.config import ContextSearchConfig
from models.schemas import (
    ContextSearchConfigResponse,
    ContextSearchConfigUpdate,
    ExportedSearchConfig,
)
from repositories.config_repository import ContextSearchConfigRepository

_BASE = {
    "semantic_weight": 0.6,
    "bm25_weight": 0.4,
    "fetch_factor": 3,
    "use_rerank": False,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2",
}


def test_column_defaults_off():
    col = ContextSearchConfig.__table__.c.content_chunks_enabled
    assert col.nullable is False
    assert str(col.server_default.arg) == "false"
    assert col.default.arg is False


def test_update_default_off_and_unset():
    config = ContextSearchConfigUpdate(**_BASE)
    assert config.content_chunks_enabled is False
    assert "content_chunks_enabled" not in config.model_dump(exclude_unset=True)


def test_response_and_export_carry_the_flag():
    assert ContextSearchConfigResponse.model_fields["content_chunks_enabled"].default is False
    assert ExportedSearchConfig.model_fields["content_chunks_enabled"].default is False


@pytest.mark.asyncio
async def test_partial_update_keeps_enabled():
    config = SimpleNamespace(content_chunks_enabled=True, semantic_weight=0.6)
    repo = ContextSearchConfigRepository(AsyncMock())
    repo.get_by_context = AsyncMock(return_value=config)
    await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE))
    assert config.content_chunks_enabled is True


@pytest.mark.asyncio
async def test_explicit_flag_is_applied():
    from unittest.mock import patch

    config = SimpleNamespace(content_chunks_enabled=False)
    repo = ContextSearchConfigRepository(AsyncMock())
    repo.get_by_context = AsyncMock(return_value=config)
    with patch.object(repo, "_purge_chunk_points", AsyncMock()):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True))
    assert config.content_chunks_enabled is True


def test_mcp_tool_schema_exposes_the_flag():
    from mcp_server.tools._definitions import get_tool_definitions

    tool = next(t for t in get_tool_definitions() if t["name"] == "update_search_config")
    prop = tool["inputSchema"]["properties"]["content_chunks_enabled"]
    assert prop["type"] == "boolean"
    assert "content_chunks_enabled" in tool["description"]


def _flagged_config(enabled: bool):
    return SimpleNamespace(
        content_chunks_enabled=enabled,
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        embedding_model="text-embedding-3-small",
        embedding_dimensions=512,
    )


def _repo_with(config, workspace_id):
    from unittest.mock import MagicMock

    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = workspace_id
    db.execute = AsyncMock(return_value=result)
    repo = ContextSearchConfigRepository(db)
    repo.get_by_context = AsyncMock(return_value=config)
    return repo, db


@pytest.mark.asyncio
async def test_turning_the_flag_off_purges_the_contexts_chunk_points():
    """#2015 invariant: a context with the flag off has no chunk points."""
    from unittest.mock import patch

    from db.qdrant import get_collection_name

    ws, ctx = uuid4(), uuid4()
    config = _flagged_config(True)
    repo, db = _repo_with(config, ws)
    order = []
    db.commit.side_effect = lambda: order.append("commit")
    with patch(
        "db.qdrant.purge_context_chunk_points",
        AsyncMock(side_effect=lambda *a: order.append("purge")),
    ) as purge:
        await repo.update(ctx, ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=False))
    # Before the commit (a failure keeps the flag on) and again after it, for
    # a writer that read "on" in between (self-repairing invariant).
    assert order == ["purge", "commit", "purge"]
    assert purge.await_args.args == (
        str(ws),
        str(ctx),
        get_collection_name("text-embedding-3-small", 512),
    )


@pytest.mark.asyncio
async def test_turning_the_flag_on_purges_leftovers_before_the_commit():
    """#2015: chunks a racing writer left while the flag was off are removed
    before the flag makes chunk points visible to recall again."""
    from unittest.mock import patch

    repo, db = _repo_with(_flagged_config(False), uuid4())
    order = []
    db.commit.side_effect = lambda: order.append("commit")
    with patch(
        "db.qdrant.purge_context_chunk_points",
        AsyncMock(side_effect=lambda *a: order.append("purge")),
    ):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True))
    assert order == ["purge", "commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "after"), [(True, True), (False, False)])
async def test_no_purge_without_a_flag_change(before, after):
    from unittest.mock import patch

    repo, _ = _repo_with(_flagged_config(before), uuid4())
    with patch("db.qdrant.purge_context_chunk_points", AsyncMock()) as purge:
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=after))
    purge.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_purge_keeps_the_flag_on():
    """Purge before commit: if the chunks cannot be removed, the flag stays on
    (rolled back), so the invariant still holds."""
    from unittest.mock import patch

    config = _flagged_config(True)
    repo, db = _repo_with(config, uuid4())
    with (
        patch("db.qdrant.purge_context_chunk_points", AsyncMock(side_effect=RuntimeError("x"))),
        pytest.raises(RuntimeError),
    ):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=False))
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_a_failed_post_commit_purge_does_not_fail_the_update():
    """The flag is committed off; leftovers are removed by the purge when it is
    turned on again, and recall never reads chunks while it is off."""
    from unittest.mock import patch

    repo, db = _repo_with(_flagged_config(True), uuid4())
    with patch(
        "db.qdrant.purge_context_chunk_points",
        AsyncMock(side_effect=[None, RuntimeError("down")]),
    ):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=False))
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_reset_to_default_turns_chunks_off_and_purges():
    """#2015: a reset means the documented defaults — chunks off."""
    from unittest.mock import patch

    repo, _ = _repo_with(_flagged_config(True), uuid4())
    with patch("db.qdrant.purge_context_chunk_points", AsyncMock()) as purge:
        config = await repo.reset_to_default(uuid4())
    assert config.content_chunks_enabled is False
    assert purge.await_count == 2
