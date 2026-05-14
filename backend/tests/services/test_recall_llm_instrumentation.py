"""#475 PR-3: recall path embedding cost instrumentation.

These tests assert the wiring between ``SearchService.hybrid_search`` and
``LLMCallLogWriter``: every embedding call on the recall path that
actually hits the provider (cache miss) emits exactly one
``llm_call_log`` row, while cache hits (tokens=0) and keyword-only
searches emit zero rows. Writer-level semantics (``fail_on_error=False``
swallowing flush / pricing exceptions, validation errors still raising)
are tested in ``test_llm_call_log_writer.py:TestFailOnErrorSemantics``
— here we only confirm the call site passes the right kwargs.

The 5th acceptance test for #475 PR-3 — the inventory regression guard
that asserts ``hybrid_search`` never calls ``.embed()`` directly again
— is ``test_hybrid_search_uses_embed_with_usage_not_embed``. Without
this guard a future refactor could silently re-introduce the cost gap
that PR-3 closes.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# Shared helpers ---------------------------------------------------------


def _fake_config() -> SimpleNamespace:
    """Minimal ContextSearchConfig-shaped object for hybrid_search.

    Carries only the attributes the function reads. ``context_id`` is
    present so ``hasattr(config, "context_id")`` resolves True and
    ``resolve_routing_from_config`` receives a routing-eligible config
    object — exercises the same code path production hits.
    """
    return SimpleNamespace(
        context_id=uuid4(),
        fetch_factor=2,
        use_rerank=False,
        semantic_weight=0.6,
        bm25_weight=0.4,
    )


def _fake_embed_svc(tokens_used: int) -> MagicMock:
    """An EmbeddingService-shaped mock for the recall-side embedding call.

    Provider/model are read from the *returned* ``embed_svc`` (not from
    ``SearchService.embedding_service`` directly) so a context-specific
    routing override is correctly priced — the writer call site copies
    these attributes off ``embed_svc``, so the test mock must carry them.
    """
    svc = MagicMock()
    svc.provider = "openai"
    svc.model = "text-embedding-3-small"
    svc.embed_with_usage = AsyncMock(return_value=([0.1] * 8, tokens_used))
    return svc


async def _run_hybrid_search(
    *,
    embed_tokens: int,
    search_mode: str = "semantic",
    writer_cls_target: str = "services.search_service.LLMCallLogWriter",
):
    """Invoke ``SearchService.hybrid_search`` with everything around the
    embedding call mocked out. Returns ``(mock_writer_cls, mock_writer_instance)``
    so assertions can target either the constructor call or the
    ``record()`` invocation.

    ``search_mode='semantic'`` exercises the embedding branch without
    paying the BM25 path's mocking cost. ``search_mode='keyword'``
    exercises the no-embed branch.
    """
    # Import inside the helper so test collection doesn't trigger module
    # import errors on environments where the path isn't wired up yet
    # (mirrors the pattern in test_llm_call_log_writer.py — imports stay
    # near the consumers).
    from services.search_service import SearchService

    user_id = "u-test"
    workspace_id = str(uuid4())
    context_id = str(uuid4())

    embed_svc = _fake_embed_svc(embed_tokens)
    cfg = _fake_config()

    mock_writer = MagicMock()
    mock_writer.record = AsyncMock(return_value=None)
    mock_writer_cls = MagicMock(return_value=mock_writer)

    # Patch every IO boundary hybrid_search crosses on the embedding path.
    # ``ContextService`` is imported inside the function body, so we patch
    # the source module rather than the search_service namespace.
    with (
        patch(writer_cls_target, mock_writer_cls),
        patch(
            "services.search_service.resolve_routing_from_config",
            return_value=("default", embed_svc),
        ),
        patch(
            "services.search_service.search_memories_qdrant",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "services.search_service.search_memories_fulltext",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "services.context_service.ContextService.is_context_shared",
            new=AsyncMock(return_value=False),
        ),
    ):
        service = SearchService(db=AsyncMock())
        # _get_search_config is async; replace with our pre-built config.
        service._get_search_config = AsyncMock(return_value=cfg)

        await service.hybrid_search(
            query="test query",
            user_id=user_id,
            workspace_id=workspace_id,
            context_id=context_id,
            k=5,
            use_rerank=False,
            filters=None,
            search_mode=search_mode,
        )

    return mock_writer_cls, mock_writer, embed_svc, user_id, workspace_id, context_id


# Tests ------------------------------------------------------------------


class TestRecallEmbeddingInstrumentation:
    """SearchService.hybrid_search → LLMCallLogWriter wiring (#475 PR-3)."""

    @pytest.mark.asyncio
    async def test_cache_hit_does_not_invoke_writer(self):
        """Cache hit returns tokens=0 → writer never constructed (B1 pin).

        The ``llm_call_log`` ledger is "API was called" event log, not
        cache analytics. Emitting a zero-token row on a cache hit would
        let the table be misread as a hit-rate metric (operator pin B1,
        2026-05-14). The gate is co-located with the embed call site so
        the writer never opens an unnecessary DB connection on the
        recall hot path.
        """
        writer_cls, writer, *_ = await _run_hybrid_search(embed_tokens=0)
        writer_cls.assert_not_called()
        writer.record.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_miss_records_one_row_with_recall_caller(self):
        """tokens > 0 → exactly one writer.record() call with the right kwargs.

        Verifies the full kwarg surface the writer's nullability matrix
        requires for ``caller='recall'`` (user_id + workspace_id +
        context_id all populated) and the operator-pinned A1 contract
        (``fail_on_error=False`` so a writer flake never breaks the
        user-facing recall response).
        """
        writer_cls, writer, embed_svc, uid, wid, cid = await _run_hybrid_search(embed_tokens=150)

        writer_cls.assert_called_once()
        writer.record.assert_awaited_once()
        kwargs = writer.record.await_args.kwargs

        assert kwargs["caller"] == "recall"
        assert kwargs["call_type"] == "embedding"
        assert kwargs["provider"] == embed_svc.provider
        assert kwargs["model"] == embed_svc.model
        assert kwargs["user_id"] == uid
        assert kwargs["workspace_id"] == wid
        assert kwargs["context_id"] == cid
        assert kwargs["embedding_tokens"] == 150
        assert kwargs["paid_by"] == "platform"
        assert kwargs["fail_on_error"] is False  # A1 pin

    @pytest.mark.asyncio
    async def test_keyword_only_search_skips_embedding_and_writer(self):
        """search_mode='keyword' never embeds → never logs.

        The current implementation gates the writer on ``embedding_tokens > 0``
        *inside* the ``("hybrid", "semantic")`` branch. A future refactor
        that lifts the writer call out of that branch would emit a
        spurious zero-token row for every keyword-only search; this test
        guards against that regression. Hiragana-heavy queries route
        through this mode at scale (#163 context), so the noise floor
        would be non-trivial without the gate.
        """
        writer_cls, writer, embed_svc, *_ = await _run_hybrid_search(
            embed_tokens=999,  # irrelevant — embed_with_usage shouldn't run
            search_mode="keyword",
        )
        embed_svc.embed_with_usage.assert_not_called()
        writer_cls.assert_not_called()
        writer.record.assert_not_called()


class TestRecallEmbeddingInventory:
    """Regression guard: ``hybrid_search`` must never re-introduce ``.embed()``.

    The cost-discarding ``EmbeddingService.embed()`` wrapper exists
    intentionally for callers that don't track cost (``resource_indexer``,
    etc.). On the recall path it is a silent cost leak — every call site
    must use ``embed_with_usage`` so the writer can attribute tokens via
    ``llm_call_log``. Pure-AST check rather than a behavior assertion so
    a refactor that *swaps* embed implementations (without breaking the
    behavior tests) still fails fast.
    """

    def test_hybrid_search_uses_embed_with_usage_not_embed(self):
        """No bare ``.embed(`` call inside ``hybrid_search``'s body.

        AST walk instead of substring grep so ``.embed_with_usage(`` /
        ``.embed_v2(`` don't false-positive against ``.embed(``.
        ``inspect.getsource`` instead of a path-based read survives test
        file moves and matches the single-function inspection convention
        at ``tests/cli/test_create_admin_mcp_json.py:138``.
        """
        from services.search_service import SearchService

        # ``inspect.getsource`` returns the method body with its class-
        # level indentation; ``ast.parse`` rejects that with
        # IndentationError, so dedent before parsing.
        src = textwrap.dedent(inspect.getsource(SearchService.hybrid_search))
        tree = ast.parse(src)

        bare_embed_calls: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            # Exact attribute-name match: ``embed`` not ``embed_with_usage``.
            if node.func.attr == "embed":
                bare_embed_calls.append(node)

        assert bare_embed_calls == [], (
            f"Found {len(bare_embed_calls)} bare .embed() call(s) inside "
            "SearchService.hybrid_search — recall path must use "
            ".embed_with_usage() so embedding tokens can be attributed to "
            "llm_call_log (#475 PR-3). Update the call site to "
            "``embed_with_usage`` and emit a writer row when tokens > 0."
        )
