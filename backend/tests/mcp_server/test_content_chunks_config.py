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
        "db.qdrant.purge_context_chunk_points_everywhere",
        AsyncMock(side_effect=lambda *a, **k: order.append("purge") or []),
    ) as purge:
        await repo.update(ctx, ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=False))
    # Before the commit (a failure keeps the flag on) and again after it, for
    # a writer that read "on" in between (self-repairing invariant).
    assert order == ["purge", "commit", "purge"]
    # Every collection, not only the routed one (PR 2a): a migration keeps
    # the context's points in its source and its target collection. The
    # routed one is the one whose failure blocks the change.
    assert purge.await_args.args == (str(ws), str(ctx))
    assert purge.await_args.kwargs == {
        "routed_collection": get_collection_name("text-embedding-3-small", 512)
    }


@pytest.mark.asyncio
async def test_turning_the_flag_on_purges_leftovers_before_the_commit():
    """#2015: chunks a racing writer left while the flag was off are removed
    before the flag makes chunk points visible to recall again."""
    from unittest.mock import patch

    repo, db = _repo_with(_flagged_config(False), uuid4())
    order = []
    db.commit.side_effect = lambda: order.append("commit")
    with patch(
        "db.qdrant.purge_context_chunk_points_everywhere",
        AsyncMock(side_effect=lambda *a, **k: order.append("purge") or []),
    ):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True))
    assert order == ["purge", "commit"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "after"), [(True, True), (False, False)])
async def test_no_purge_without_a_flag_change(before, after):
    from unittest.mock import patch

    repo, _ = _repo_with(_flagged_config(before), uuid4())
    with patch("db.qdrant.purge_context_chunk_points_everywhere", AsyncMock()) as purge:
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
        patch(
            "db.qdrant.purge_context_chunk_points_everywhere",
            AsyncMock(side_effect=RuntimeError("x")),
        ),
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
        "db.qdrant.purge_context_chunk_points_everywhere",
        AsyncMock(side_effect=[[], RuntimeError("down")]),
    ):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=False))
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_reset_to_default_turns_chunks_off_and_purges():
    """#2015: a reset means the documented defaults — chunks off."""
    from unittest.mock import patch

    repo, _ = _repo_with(_flagged_config(True), uuid4())
    with patch("db.qdrant.purge_context_chunk_points_everywhere", AsyncMock()) as purge:
        config = await repo.reset_to_default(uuid4())
    assert config.content_chunks_enabled is False
    assert purge.await_count == 2


# ---------------------------------------------------------------------------
# #2015: changing the flag takes the context owner (MCP twin of the REST gate)
# ---------------------------------------------------------------------------


def _mcp_config(enabled: bool):
    return SimpleNamespace(
        content_chunks_enabled=enabled,
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
        reinforce_enabled=True,
        reinforce_max_boost=0.15,
        reinforce_require_host_arbitration=False,
        routing_mode="off",
    )


async def _call_update(args: dict, *, stored: bool, owner: bool):
    import json
    from unittest.mock import MagicMock, patch

    from mcp_server.tools.search_config import handle_update_search_config
    from utils.exceptions import AuthorizationError

    db = MagicMock()
    db.rollback = AsyncMock()

    async def fake_get_db():
        yield db

    perms = MagicMock()
    perms.check_context_write = AsyncMock()
    perms.check_context_owner = AsyncMock(side_effect=None if owner else AuthorizationError())
    config = _mcp_config(stored)
    repo = MagicMock()
    repo.get_by_context = AsyncMock(return_value=config)
    repo.update = AsyncMock(return_value=config)
    with (
        patch("db.base.get_db", new=fake_get_db),
        patch("services.permission_service.PermissionService", return_value=perms),
        patch("services.plan_suspension.ensure_context_writable", AsyncMock()),
        patch("repositories.config_repository.ContextSearchConfigRepository", return_value=repo),
        patch("mcp_server.tools.search_config._log_tool_usage", AsyncMock()),
    ):
        result = await handle_update_search_config(
            {"context_id": str(uuid4()), **args}, "user-1", uuid4()
        )
    return json.loads(result[0].text), perms, repo


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "requested"), [(False, True), (True, False)])
async def test_mcp_an_editor_cannot_change_the_flag(stored, requested):
    payload, _, repo = await _call_update(
        {"content_chunks_enabled": requested}, stored=stored, owner=False
    )
    assert payload["error"] == "permission_denied"
    assert "content_chunks_enabled" in payload["message"]
    assert payload["required_role"] == "owner"
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_an_editor_can_change_other_fields():
    payload, perms, repo = await _call_update({"use_rerank": True}, stored=True, owner=False)
    assert payload["status"] == "success"
    perms.check_context_owner.assert_not_awaited()
    applied = repo.update.await_args.args[1]
    assert applied.use_rerank is True
    # Omitted: not round-tripped, so a concurrent owner change is never undone.
    assert "content_chunks_enabled" not in applied.model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_mcp_an_editor_sending_the_stored_value_is_a_no_op(stored):
    payload, perms, repo = await _call_update(
        {"content_chunks_enabled": stored}, stored=stored, owner=False
    )
    assert payload["status"] == "success"
    perms.check_context_owner.assert_not_awaited()
    assert "content_chunks_enabled" not in repo.update.await_args.args[1].model_fields_set


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "requested"), [(False, True), (True, False)])
async def test_mcp_an_owner_or_workspace_admin_can_change_the_flag(stored, requested):
    payload, perms, repo = await _call_update(
        {"content_chunks_enabled": requested}, stored=stored, owner=True
    )
    assert payload["status"] == "success"
    perms.check_context_owner.assert_awaited_once()
    assert repo.update.await_args.args[1].content_chunks_enabled is requested


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_mcp_turning_the_flag_on_is_refused_without_chunk_point_support(owner):
    """#2072: the LanceDB store holds no chunk points."""
    from unittest.mock import patch

    with patch("services.permission_service.chunk_points_supported", return_value=False):
        payload, _, repo = await _call_update(
            {"content_chunks_enabled": True}, stored=False, owner=owner
        )
    assert payload["error"] == "invalid_search_config"
    assert "content_chunks_enabled" in payload["message"]
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_turning_the_flag_off_is_allowed_without_chunk_point_support():
    from unittest.mock import patch

    with patch("services.permission_service.chunk_points_supported", return_value=False):
        payload, _, repo = await _call_update(
            {"content_chunks_enabled": False}, stored=True, owner=True
        )
    assert payload["status"] == "success"
    assert repo.update.await_args.args[1].content_chunks_enabled is False


# ---------------------------------------------------------------------------
# #2015 PR 2b (D4): turning the flag on queues the existing long memories.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turning_the_flag_on_queues_the_backfill_inside_the_transaction():
    from unittest.mock import patch

    import structlog

    repo, db = _repo_with(_flagged_config(False), uuid4())
    order = []
    db.commit.side_effect = lambda: order.append("commit")
    ctx = uuid4()
    with (
        structlog.testing.capture_logs() as logs,
        patch(
            "db.qdrant.purge_context_chunk_points_everywhere",
            AsyncMock(side_effect=lambda *a, **k: order.append("purge") or []),
        ),
        patch.object(
            repo,
            "_queue_content_chunk_backfill",
            AsyncMock(side_effect=lambda _ctx: order.append("backfill") or 7),
        ) as backfill,
    ):
        await repo.update(ctx, ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True))
    assert order == ["purge", "backfill", "commit"]
    backfill.assert_awaited_once_with(ctx)
    event = next(e for e in logs if e["event"] == "content_chunks_backfill_queued")
    assert (event["context_id"], event["count"]) == (str(ctx), 7)


@pytest.mark.asyncio
@pytest.mark.parametrize(("before", "after"), [(True, False), (True, True), (False, False)])
async def test_no_backfill_unless_the_flag_turns_on(before, after):
    from unittest.mock import patch

    repo, _ = _repo_with(_flagged_config(before), uuid4())
    with (
        patch("db.qdrant.purge_context_chunk_points_everywhere", AsyncMock(return_value=[])),
        patch.object(repo, "_queue_content_chunk_backfill", AsyncMock()) as backfill,
    ):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=after))
    backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_backfill_keeps_the_flag_off():
    from unittest.mock import patch

    repo, db = _repo_with(_flagged_config(False), uuid4())
    with (
        patch("db.qdrant.purge_context_chunk_points_everywhere", AsyncMock(return_value=[])),
        patch.object(
            repo, "_queue_content_chunk_backfill", AsyncMock(side_effect=RuntimeError("db"))
        ),
        pytest.raises(RuntimeError),
    ):
        await repo.update(uuid4(), ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True))
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited()


@pytest.mark.asyncio
async def test_the_backfill_statement_requeues_only_long_live_success_rows():
    """The predicate (the DB test runs it on real rows): never processing rows,
    never the pipeline clock or updated_at, never resource rows."""
    from unittest.mock import MagicMock, patch

    from sqlalchemy.dialects import postgresql

    from utils.content_chunks import CONTENT_CHUNK_MIN_CONTENT_CHARS

    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=3))
    repo = ContextSearchConfigRepository(db)
    with patch("db.qdrant.chunk_points_supported", return_value=True):
        assert await repo._queue_content_chunk_backfill(uuid4()) == 3
    stmt = db.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert sql.startswith("UPDATE memories SET embedding_status='pending'")
    set_clause = sql.split(" WHERE ")[0]
    assert "embedding_attempted_at" not in set_clause and "updated_at" not in set_clause
    where = sql.split(" WHERE ")[1]
    assert "memories.embedding_status = 'success'" in where
    assert "memories.deleted_at IS NULL" in where
    assert f"char_length(memories.content) >= {CONTENT_CHUNK_MIN_CONTENT_CHARS}" in where
    assert "memories.resource_id IS NULL" in where


@pytest.mark.asyncio
async def test_no_backfill_on_a_store_without_chunk_points():
    from unittest.mock import patch

    db = AsyncMock()
    repo = ContextSearchConfigRepository(db)
    with patch("db.qdrant.chunk_points_supported", return_value=False):
        assert await repo._queue_content_chunk_backfill(uuid4()) == 0
    db.execute.assert_not_awaited()
