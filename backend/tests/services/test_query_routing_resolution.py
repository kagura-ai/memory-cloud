"""Tests for MemoryService._resolve_search_mode (#1212 routing hook).

Pins the acceptance contract: log_only changes NOTHING about the effective
mode (zero ranking change), active applies the routed lane only when the
caller omitted search_mode, an explicit search_mode always wins in every
routing_mode, and the resolver is total (a concrete mode on every branch).

#1220 (advisor rec): the resolver no longer reads the DB — ``recall()``
prefetches the ContextSearchConfig row once and threads it in via the
``config`` parameter (None = row missing / prefetch failed = routing off).
These tests therefore pass the config directly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.memory_service import MemoryService

_CTX = uuid4()
# classify_query routes this to "keyword" (exact-id) — distinguishable from
# both the "hybrid" fallback and any explicit mode used in the tests.
_KEYWORD_QUERY = "HEALTH-001"


def _service() -> MemoryService:
    svc = MemoryService.__new__(MemoryService)
    svc.db = AsyncMock()
    return svc


def _request(query: str = _KEYWORD_QUERY, search_mode: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(query=query, search_mode=search_mode)


def _config(routing_mode: str | None) -> SimpleNamespace | None:
    """The prefetched config row (or None for a row-less context)."""
    return None if routing_mode is None else SimpleNamespace(routing_mode=routing_mode)


async def _resolve(
    routing_mode: str | None,
    *,
    search_mode: str | None = None,
    query: str = _KEYWORD_QUERY,
    cross_context: bool = False,
) -> str:
    return await _service()._resolve_search_mode(
        _request(query=query, search_mode=search_mode),
        _CTX,
        cross_context=cross_context,
        config=_config(routing_mode),
    )


class TestRoutingOff:
    @pytest.mark.asyncio
    async def test_no_config_row_defaults_hybrid(self) -> None:
        assert await _resolve(None) == "hybrid"

    @pytest.mark.asyncio
    async def test_off_defaults_hybrid(self) -> None:
        assert await _resolve("off") == "hybrid"

    @pytest.mark.asyncio
    async def test_off_explicit_mode_wins(self) -> None:
        assert await _resolve("off", search_mode="semantic") == "semantic"


class TestLogOnly:
    @pytest.mark.asyncio
    async def test_log_only_never_changes_the_mode(self) -> None:
        """The zero-ranking-change guarantee: the classifier says keyword,
        the effective mode stays the historical default."""
        assert await _resolve("log_only") == "hybrid"

    @pytest.mark.asyncio
    async def test_log_only_explicit_mode_wins(self) -> None:
        assert await _resolve("log_only", search_mode="keyword") == "keyword"

    @pytest.mark.asyncio
    async def test_log_only_stamps_telemetry(self) -> None:
        with patch("services.memory_service.logger") as log:
            await _service()._resolve_search_mode(
                _request(), _CTX, cross_context=False, config=_config("log_only")
            )
        events = [c for c in log.info.call_args_list if c.args[0] == "query_router_decision"]
        assert len(events) == 1
        kwargs = events[0].kwargs
        assert kwargs["routing_mode"] == "log_only"
        assert kwargs["decided_lane"] == "keyword"
        assert kwargs["applied"] is False
        assert kwargs["effective_mode"] == "hybrid"
        # Telemetry privacy: the query text itself is never logged.
        assert _KEYWORD_QUERY not in str(kwargs)


class TestActive:
    @pytest.mark.asyncio
    async def test_active_routes_when_mode_omitted(self) -> None:
        assert await _resolve("active") == "keyword"

    @pytest.mark.asyncio
    async def test_active_explicit_mode_always_wins(self) -> None:
        assert await _resolve("active", search_mode="hybrid") == "hybrid"

    @pytest.mark.asyncio
    async def test_active_semantic_query_routes_semantic(self) -> None:
        assert (
            await _resolve("active", query="what were the sleep maintenance design decisions")
            == "semantic"
        )


class TestNeverNone:
    """Gate2/QA: the resolver's return is a concrete mode string on every
    (requested x routing_mode x config-presence) branch — None can never
    leak into SearchService.hybrid_search's mode validation."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("requested", [None, "hybrid", "semantic", "keyword"])
    @pytest.mark.parametrize("routing_mode", [None, "off", "log_only", "active"])
    async def test_all_branches_return_concrete_mode(
        self, requested: str | None, routing_mode: str | None
    ) -> None:
        mode = await _resolve(routing_mode, search_mode=requested)
        assert mode in ("hybrid", "semantic", "keyword")
        if requested is not None:
            assert mode == requested


class TestNoDbReadAndScope:
    @pytest.mark.asyncio
    async def test_resolver_never_touches_the_db(self) -> None:
        """#1220: the config is prefetched by recall() — the resolver itself
        must issue no read, in any routing_mode."""
        svc = _service()
        for routing_mode in (None, "off", "log_only", "active"):
            await svc._resolve_search_mode(
                _request(), _CTX, cross_context=False, config=_config(routing_mode)
            )
        svc.db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cross_context_skips_routing(self) -> None:
        assert await _resolve("active", cross_context=True) == "hybrid"

    @pytest.mark.asyncio
    async def test_mock_like_config_without_valid_mode_is_off(self) -> None:
        """A config whose routing_mode is not a recognized value (e.g. a
        MagicMock attribute) must behave as 'off' — membership check, never
        truthiness (#1207 lesson)."""
        mode = await _service()._resolve_search_mode(
            _request(), _CTX, cross_context=False, config=MagicMock()
        )
        assert mode == "hybrid"


class TestReinforcePrefetchThreading:
    @pytest.mark.asyncio
    async def test_passed_config_skips_the_reinforce_config_read(self) -> None:
        """#1220: when recall() threads the prefetched row in, the reinforce
        re-rank must not issue its own get_by_context SELECT."""
        svc = _service()
        cfg = SimpleNamespace(reinforce_enabled=False)
        repo_cls = MagicMock()
        with patch("repositories.config_repository.ContextSearchConfigRepository", repo_cls):
            await svc._maybe_reinforce_rerank(
                [{"id": "a"}, {"id": "b"}], {}, _CTX, top_k=5, config=cfg
            )
        repo_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_prefetch_falls_back_to_local_read(self) -> None:
        """A fresh context's row is materialized by hybrid_search AFTER the
        prefetch — config=None must re-read so the first-ever recall keeps
        its re-rank behavior."""
        svc = _service()
        repo = MagicMock()
        repo.get_by_context = AsyncMock(return_value=SimpleNamespace(reinforce_enabled=False))
        with patch(
            "repositories.config_repository.ContextSearchConfigRepository", return_value=repo
        ):
            await svc._maybe_reinforce_rerank([{"id": "a"}, {"id": "b"}], {}, _CTX, top_k=5)
        repo.get_by_context.assert_awaited_once_with(_CTX)
