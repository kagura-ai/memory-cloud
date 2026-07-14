"""#1257: memory operations must touch ``Context.last_used_at`` (throttled).

The column feeds the ``list_contexts`` recency sort but was never written
after row creation, so it was effectively ``created_at``. The fix mirrors the
api_keys.last_used_at throttle (#947): remember/recall/reference mark the
already-resolved Context row as used, at most once per
``context_last_used_throttle_seconds`` window per context.

Follows the mock-db handler convention of tests/mcp_server/ (patch
db.base.get_db + the context resolver + the service). The throttle-window
arithmetic is pinned on the helper directly (where settings can be patched
without touching the handler path); the handler tests pin the WIRING — which
tools touch which contexts.
"""

import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import _touch_context_last_used
from mcp_server.tools.memory import handle_recall, handle_reference, handle_remember


def _make_context(last_used_at=None, workspace_id=None, is_private=False):
    return SimpleNamespace(
        last_used_at=last_used_at,
        workspace_id=workspace_id or uuid4(),
        is_private=is_private,
    )


# ============================================================================
# Helper: throttle arithmetic
# ============================================================================


class TestTouchContextLastUsed:
    def _settings(self, seconds=60):
        return patch(
            "config.settings.get_settings",
            return_value=SimpleNamespace(context_last_used_throttle_seconds=seconds),
        )

    def test_never_used_context_writes_aware_utc(self):
        ctx = _make_context(last_used_at=None)
        with self._settings():
            assert _touch_context_last_used(ctx) is True
        # Aware UTC — list_contexts sorts against an aware _UTC_MIN sentinel;
        # a naive write would TypeError that sort.
        assert ctx.last_used_at is not None
        assert ctx.last_used_at.utcoffset() == timedelta(0)

    def test_fresh_timestamp_is_throttled(self):
        stamp = datetime.now(UTC)
        ctx = _make_context(last_used_at=stamp)
        with self._settings():
            assert _touch_context_last_used(ctx) is False
        assert ctx.last_used_at is stamp

    def test_stale_timestamp_writes(self):
        stamp = datetime.now(UTC) - timedelta(seconds=61)
        ctx = _make_context(last_used_at=stamp)
        with self._settings(seconds=60):
            assert _touch_context_last_used(ctx) is True
        assert ctx.last_used_at > stamp

    def test_naive_stored_value_is_normalized_not_typeerror(self):
        # A direct-SQL backfill could leave a naive value in the aware column;
        # the hot path must normalize (naive == UTC by project convention),
        # not crash on aware-vs-naive subtraction.
        naive_stale = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        ctx = _make_context(last_used_at=naive_stale)
        with self._settings(seconds=60):
            assert _touch_context_last_used(ctx) is True
        assert ctx.last_used_at.utcoffset() == timedelta(0)

    def test_naive_fresh_value_is_throttled(self):
        naive_fresh = datetime.now(UTC).replace(tzinfo=None)
        ctx = _make_context(last_used_at=naive_fresh)
        with self._settings(seconds=60):
            assert _touch_context_last_used(ctx) is False
        assert ctx.last_used_at is naive_fresh


# ============================================================================
# Handler wiring: recall
# ============================================================================


def _recall_result():
    result = MagicMock()
    result.results = []
    result.related_tags = []
    result.explore_hints = None
    result.confidence = None
    return result


@contextlib.contextmanager
def _patched_recall(resolver: AsyncMock):
    async def mock_get_db():
        yield AsyncMock()

    service = MagicMock()
    service.recall = AsyncMock(return_value=_recall_result())

    config = MagicMock()
    config.embedding_model = "text-embedding-3-small"
    config_repo = MagicMock()
    config_repo.create_or_get = AsyncMock(return_value=config)

    with (
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
        yield


@pytest.mark.asyncio
async def test_recall_bumps_never_used_context():
    ctx = _make_context(last_used_at=None)

    with _patched_recall(AsyncMock(return_value=ctx)):
        result = await handle_recall(
            {"query": "q", "context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert ctx.last_used_at is not None
    assert ctx.last_used_at.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_recall_within_throttle_window_does_not_write():
    """The regression #1257 exists to prevent: a hot MCP client recalling in a
    loop must NOT trigger a contexts-row write per call. A just-written
    timestamp is inside any sane window (default 60s), so the second recall
    leaves the exact same object in place."""
    stamp = datetime.now(UTC)
    ctx = _make_context(last_used_at=stamp)

    with _patched_recall(AsyncMock(return_value=ctx)):
        result = await handle_recall(
            {"query": "q", "context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert ctx.last_used_at is stamp


@pytest.mark.asyncio
async def test_recall_bump_then_immediate_recall_is_suppressed():
    """End-to-end shape of the throttle: first recall writes (was never used),
    an immediate second recall is a no-op on the same row."""
    ctx = _make_context(last_used_at=None)

    with _patched_recall(AsyncMock(return_value=ctx)):
        await handle_recall(
            {"query": "q", "context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )
        first_stamp = ctx.last_used_at
        assert first_stamp is not None

        await handle_recall(
            {"query": "q", "context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )

    assert ctx.last_used_at is first_stamp


@pytest.mark.asyncio
async def test_cross_context_recall_touches_every_listed_context():
    """#1228 cross-context recall: the secondaries are read too — all listed
    contexts count as used, not just the billable primary."""
    ws = uuid4()
    primary = _make_context(last_used_at=None, workspace_id=ws)
    secondary = _make_context(last_used_at=None, workspace_id=ws)

    with _patched_recall(AsyncMock(side_effect=[primary, secondary])):
        result = await handle_recall(
            {"query": "q", "context_ids": [str(uuid4()), str(uuid4())]},
            user_id="u1",
            workspace_id=None,
        )

    assert json.loads(result[0].text)["status"] == "success"
    assert primary.last_used_at is not None
    assert secondary.last_used_at is not None


# ============================================================================
# Handler wiring: remember / reference
# ============================================================================


@pytest.mark.asyncio
async def test_remember_bumps_context():
    ctx = _make_context(last_used_at=None)

    async def mock_get_db():
        yield AsyncMock()

    service = MagicMock()
    service.remember = AsyncMock(return_value=SimpleNamespace(memory_id=uuid4(), scope="personal"))

    with (
        patch("db.base.get_db", new=mock_get_db),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=ctx)),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
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
    assert ctx.last_used_at is not None


@pytest.mark.asyncio
async def test_reference_bumps_context():
    ctx = _make_context(last_used_at=None)

    async def mock_get_db():
        yield AsyncMock()

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
    )
    service = MagicMock()
    service.reference = AsyncMock(return_value=reference_result)

    with (
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
    assert ctx.last_used_at is not None


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
async def test_list_contexts_sorts_by_recency_with_never_used_last():
    """The sort the touch exists to make meaningful: freshest first, and
    ``None`` (never used, pre-backfill rows) sorts last via the aware
    ``_UTC_MIN`` sentinel without tripping aware-vs-naive comparison."""
    from mcp_server.tools.context import handle_list_contexts

    now = datetime.now(UTC)
    never = _listable_context("never-used", None)
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
    assert [c["name"] for c in payload["contexts"]] == ["fresh", "old", "never-used"]
    assert payload["contexts"][0]["last_used_at"] == fresh.last_used_at.isoformat()
    assert payload["contexts"][2]["last_used_at"] is None
