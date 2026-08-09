"""#1257: memory operations must touch ``Context.last_used_at`` (throttled).

The column feeds the ``list_contexts`` recency sort but was never written
after row creation, so it was effectively ``created_at``. The fix mirrors the
api_keys.last_used_at throttle (#947) with a free in-memory precheck, but
writes via a single guarded Core UPDATE at commit time (not a dirty ORM
attribute) so that:

- the contexts row lock spans one round-trip, not the recall pipeline;
- error paths / mid-request commits by collaborators never persist a touch;
- ``Context.updated_at``'s ``onupdate=func.now()`` does NOT fire (a recall
  must not rewrite the "last modified" timestamp shown in the REST API).

Throttle arithmetic and SQL shape are pinned on the helper directly; the
handler tests pin the WIRING — which tools touch which contexts. Settings are
always patched (mcp_server.tools._helpers.get_settings): the ambient
CONTEXT_LAST_USED_THROTTLE_SECONDS env var must not leak into assertions.

Production rows normally carry a creation-time value (server_default), so
"an immediate repeat call is suppressed" is modeled by a fresh stored stamp:
a second request re-reads the row and sees the just-written timestamp. The
``None`` branch is defensive coverage for the nullable column, not a state
the ORM/SQL defaults can produce.
"""

import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import _touch_context_last_used
from mcp_server.tools.memory import (
    handle_forget,
    handle_recall,
    handle_reference,
    handle_remember,
    handle_update_memory,
)


def _make_context(last_used_at=None, workspace_id=None):
    return SimpleNamespace(
        id=uuid4(),
        name="ctx",
        last_used_at=last_used_at,
        workspace_id=workspace_id or uuid4(),
        is_private=False,
    )


def _patched_settings(seconds=3600):
    """Pin the throttle window — never read ambient env in tests."""
    return patch(
        "mcp_server.tools._helpers.get_settings",
        return_value=SimpleNamespace(context_last_used_throttle_seconds=seconds),
    )


def _touch_statements(db: AsyncMock):
    """The contexts-touch UPDATE statements issued on a mock session."""
    return [
        call.args[0]
        for call in db.execute.await_args_list
        if "UPDATE contexts" in str(call.args[0])
    ]


# ============================================================================
# Helper: throttle arithmetic + statement shape
# ============================================================================


class TestTouchContextLastUsed:
    @pytest.mark.asyncio
    async def test_never_used_context_issues_guarded_update(self):
        db = AsyncMock()
        ctx = _make_context(last_used_at=None)
        with _patched_settings():
            assert await _touch_context_last_used(db, ctx) is True
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fresh_timestamp_is_throttled_with_zero_queries(self):
        db = AsyncMock()
        ctx = _make_context(last_used_at=datetime.now(UTC))
        with _patched_settings(seconds=3600):
            assert await _touch_context_last_used(db, ctx) is False
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_timestamp_writes(self):
        db = AsyncMock()
        stamp = datetime.now(UTC) - timedelta(seconds=61)
        ctx = _make_context(last_used_at=stamp)
        with _patched_settings(seconds=60):
            assert await _touch_context_last_used(db, ctx) is True
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_naive_stored_value_is_normalized_not_typeerror(self):
        # The column is nullable and legacy/direct-SQL writes could be naive;
        # the precheck must normalize (naive == UTC by project convention),
        # not crash the hot recall path on aware-vs-naive subtraction.
        db = AsyncMock()
        naive_stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        ctx = _make_context(last_used_at=naive_stale)
        with _patched_settings(seconds=60):
            assert await _touch_context_last_used(db, ctx) is True

    @pytest.mark.asyncio
    async def test_naive_fresh_value_is_throttled(self):
        db = AsyncMock()
        ctx = _make_context(last_used_at=datetime.now(UTC).replace(tzinfo=None))
        with _patched_settings(seconds=3600):
            assert await _touch_context_last_used(db, ctx) is False
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_pins_updated_at_and_reguards_in_where(self):
        """The two load-bearing properties of the statement itself:

        1. ``updated_at`` is explicitly SET to itself so the column-level
           ``onupdate=func.now()`` does not fire — a pure read (recall) must
           not rewrite the context's "last modified" timestamp.
        2. The WHERE clause re-checks the throttle so two concurrent requests
           that both passed the in-memory precheck race safely.
        """
        db = AsyncMock()
        ctx = _make_context(last_used_at=None)
        with _patched_settings():
            await _touch_context_last_used(db, ctx)

        sql = str(db.execute.await_args.args[0]).replace("\n", " ")
        assert "UPDATE contexts" in sql
        assert "updated_at=contexts.updated_at" in sql.replace(" ", "")
        assert "contexts.last_used_at IS NULL" in sql
        assert "contexts.last_used_at <=" in sql


# ============================================================================
# Handler wiring: recall
# ============================================================================


def _recall_result():
    result = MagicMock()
    result.results = []
    result.related_tags = []
    result.explore_hints = None
    result.confidence = None
    result.tag_suggestions = None  # #1503 hint, not under test here
    return result


@contextlib.contextmanager
def _patched_recall(resolver: AsyncMock):
    db = AsyncMock()

    async def mock_get_db():
        yield db

    service = MagicMock()
    service.recall = AsyncMock(return_value=_recall_result())

    config = MagicMock()
    config.embedding_model = "text-embedding-3-small"
    config_repo = MagicMock()
    config_repo.create_or_get = AsyncMock(return_value=config)

    with (
        _patched_settings(),
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.memory._resolve_context_for_read", new=resolver),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch(
            "repositories.config_repository.ContextSearchConfigRepository",
            new=MagicMock(return_value=config_repo),
        ),
    ):
        yield db


@pytest.mark.asyncio
async def test_recall_bumps_never_used_context():
    ctx = _make_context(last_used_at=None)

    with _patched_recall(AsyncMock(return_value=ctx)) as db:
        result = await handle_recall(
            {"query": "q", "context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert len(_touch_statements(db)) == 1


@pytest.mark.asyncio
async def test_recall_within_throttle_window_does_not_write():
    """The regression #1257's throttle exists to prevent: a hot MCP client
    recalling in a loop must NOT trigger a contexts-row write per call. An
    immediate repeat request re-reads the row and sees the just-written
    timestamp, which is exactly this fresh-stamp case."""
    ctx = _make_context(last_used_at=datetime.now(UTC))

    with _patched_recall(AsyncMock(return_value=ctx)) as db:
        result = await handle_recall(
            {"query": "q", "context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert len(_touch_statements(db)) == 0


@pytest.mark.asyncio
async def test_cross_context_recall_touches_every_listed_context_in_id_order():
    """#1228 cross-context recall: all listed contexts count as used, not just
    the billable primary — and the touches are issued in ascending id order so
    concurrent overlapping recalls cannot deadlock on opposite lock order."""
    ws = uuid4()
    primary = _make_context(last_used_at=None, workspace_id=ws)
    secondary = _make_context(last_used_at=None, workspace_id=ws)

    with _patched_recall(AsyncMock(side_effect=[primary, secondary])) as db:
        result = await handle_recall(
            {"query": "q", "context_ids": [str(uuid4()), str(uuid4())]},
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["status"] == "success"
    stmts = _touch_statements(db)
    assert len(stmts) == 2
    touched_ids = [
        next(v for v in stmt.compile().params.values() if isinstance(v, type(primary.id)))
        for stmt in stmts
    ]
    assert touched_ids == sorted([primary.id, secondary.id], key=str)


@pytest.mark.asyncio
async def test_recall_validation_error_touches_nothing():
    """Error paths must not mark contexts as used — the guarded UPDATE is only
    issued on the success path, immediately before commit, so a mid-request
    commit by a collaborator (e.g. create_or_get) cannot persist a touch for a
    recall that then fails validation."""
    ws_a, ws_b = uuid4(), uuid4()
    primary = _make_context(last_used_at=None, workspace_id=ws_a)
    foreign = _make_context(last_used_at=None, workspace_id=ws_b)

    with _patched_recall(AsyncMock(side_effect=[primary, foreign])) as db:
        result = await handle_recall(
            {"query": "q", "context_ids": [str(uuid4()), str(uuid4())]},
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["error"] == "workspace_mismatch"
    assert len(_touch_statements(db)) == 0


# ============================================================================
# Handler wiring: remember / update_memory / forget / reference
# ============================================================================


@contextlib.contextmanager
def _patched_write_handler(context, service):
    db = AsyncMock()

    async def mock_get_db():
        yield db

    with (
        _patched_settings(),
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=context)),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield db


@pytest.mark.asyncio
async def test_remember_bumps_context():
    ctx = _make_context(last_used_at=None)
    service = MagicMock()
    # The optional write-response blocks the handler forwards (#1502/#1505);
    # this test is about the context touch, not their contents.
    service.remember = AsyncMock(
        return_value=SimpleNamespace(memory_id=uuid4(), scope="personal", persistence=None, lint=[])
    )

    with _patched_write_handler(ctx, service) as db:
        result = await handle_remember(
            {
                "summary": "touch test: remember marks the context used",
                "content": "remember must bump Context.last_used_at (#1257)",
                "type": "fact",
                "context_id": str(uuid4()),
            },
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert len(_touch_statements(db)) == 1


@pytest.mark.asyncio
async def test_update_memory_bumps_context():
    """#1257 review: a context maintained purely via external_id upserts must
    not sort as never-used."""
    ctx = _make_context(last_used_at=None)
    service = MagicMock()
    service.update_memory = AsyncMock(
        return_value=SimpleNamespace(
            memory_id=uuid4(),
            operation="update",
            re_embedded=False,
            scope="personal",
            persistence=None,  # #1505 block, not under test here
            supersede_candidate_dismissed=None,  # #1504, not under test here
            lint=[],  # #1502 hints, not under test here
        )
    )

    with _patched_write_handler(ctx, service) as db:
        result = await handle_update_memory(
            {
                "memory_id": str(uuid4()),
                "summary": "touch test: update_memory marks the context used",
                "context_id": str(uuid4()),
            },
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert len(_touch_statements(db)) == 1


@pytest.mark.asyncio
async def test_forget_bumps_context():
    ctx = _make_context(last_used_at=None)
    service = MagicMock()
    service.forget = AsyncMock(return_value=SimpleNamespace(deleted_count=1, memory_ids=[uuid4()]))

    with _patched_write_handler(ctx, service) as db:
        result = await handle_forget(
            {"memory_id": str(uuid4()), "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert len(_touch_statements(db)) == 1


@pytest.mark.asyncio
async def test_reference_bumps_context_but_not_on_memory_miss():
    ctx = _make_context(last_used_at=None)

    db = AsyncMock()

    async def mock_get_db():
        yield db

    reference_result = SimpleNamespace(
        memory_id=uuid4(),
        summary="s",
        context_summary=None,
        content="c",
        details=None,
        type="fact",
        scope="personal",
        importance=0.5,
        tags=[],
        context=None,
        created_at=datetime.now(UTC),
        updated_at=None,
        client="mcp",
        source_uri=None,
        source_type=None,
        outgoing_links=[],
        outgoing_has_more=False,
        incoming_links=[],
        incoming_has_more=False,
        supersede_candidate=None,  # #1403
    )
    service = MagicMock()
    service.reference = AsyncMock(return_value=reference_result)

    with (
        _patched_settings(),
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.memory._resolve_context_for_read", new=AsyncMock(return_value=ctx)),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        result = await handle_reference(
            {"memory_id": str(uuid4()), "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )
        assert json.loads(result[0].text)["status"] == "success"
        assert len(_touch_statements(db)) == 1

        # A miss returns memory_not_found BEFORE the touch — failed lookups
        # must not mark the context as used (and must not leave a flushed
        # UPDATE holding the contexts row lock on a rollback-free return).
        db.execute.reset_mock()
        from utils.exceptions import NotFoundException

        service.reference = AsyncMock(side_effect=NotFoundException("Memory not found"))
        result = await handle_reference(
            {"memory_id": str(uuid4()), "context_id": str(uuid4())},
            user_id="u1",
            workspace_id=None,
        )
        assert json.loads(result[0].text)["error"] == "memory_not_found"
        assert len(_touch_statements(db)) == 0


# ============================================================================
# list_contexts recency sort (consumes the touched values)
# ============================================================================


def _listable_context(name, last_used_at):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        summary=None,
        is_private=True,
        is_locked=False,
        last_used_at=last_used_at,
    )


@pytest.mark.asyncio
async def test_list_contexts_sorts_by_recency_with_null_last():
    """The sort the touch exists to make meaningful: freshest first. ``None``
    (nullable column; not produced by the ORM/SQL defaults, which stamp
    creation time) sorts last via the aware ``_UTC_MIN`` sentinel without
    tripping aware-vs-naive comparison, and the wire shape is the project-wide
    Z-suffix (to_utc_iso), not raw ``.isoformat()`` (+00:00)."""
    from mcp_server.tools.context import handle_list_contexts
    from utils.datetime import to_utc_iso

    now = datetime.now(UTC)
    never = _listable_context("null-last-used", None)
    old = _listable_context("old", now - timedelta(days=1))
    fresh = _listable_context("fresh", now)

    db = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalars.return_value.all.return_value = []  # no per-context search configs
    db.execute = AsyncMock(return_value=exec_result)

    async def mock_get_db():
        yield db

    context_service = MagicMock()
    context_service.list_contexts = AsyncMock(return_value=[never, old, fresh])

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "services.context_service.ContextService",
            new=MagicMock(return_value=context_service),
        ),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
    ):
        result = await handle_list_contexts({}, user_id="u1", workspace_id=None)

    payload = json.loads(result[0].text)
    assert payload["status"] == "success"
    assert [c["name"] for c in payload["contexts"]] == ["fresh", "old", "null-last-used"]
    assert payload["contexts"][0]["last_used_at"] == to_utc_iso(fresh.last_used_at)
    assert payload["contexts"][0]["last_used_at"].endswith("Z")
    assert payload["contexts"][2]["last_used_at"] is None
