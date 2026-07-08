"""Tests for MemoryService._resolve_search_mode (#1212 routing hook).

Pins the acceptance contract: log_only changes NOTHING about the effective
mode (zero ranking change), active applies the routed lane only when the
caller omitted search_mode, an explicit search_mode always wins in every
routing_mode, and the hook is fail-open (config read failure or missing
config row never breaks recall).
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


def _patch_config(routing_mode: str | None):
    """Patch the repository to return a config row (or None)."""
    config = None if routing_mode is None else SimpleNamespace(routing_mode=routing_mode)
    repo = MagicMock()
    repo.get_by_context = AsyncMock(return_value=config)
    return patch(
        "repositories.config_repository.ContextSearchConfigRepository",
        return_value=repo,
    )


class TestRoutingOff:
    @pytest.mark.asyncio
    async def test_no_config_row_defaults_hybrid(self) -> None:
        with _patch_config(None):
            assert (
                await _service()._resolve_search_mode(_request(), _CTX, cross_context=False)
                == "hybrid"
            )

    @pytest.mark.asyncio
    async def test_off_defaults_hybrid(self) -> None:
        with _patch_config("off"):
            assert (
                await _service()._resolve_search_mode(_request(), _CTX, cross_context=False)
                == "hybrid"
            )

    @pytest.mark.asyncio
    async def test_off_explicit_mode_wins(self) -> None:
        with _patch_config("off"):
            assert (
                await _service()._resolve_search_mode(
                    _request(search_mode="semantic"), _CTX, cross_context=False
                )
                == "semantic"
            )


class TestLogOnly:
    @pytest.mark.asyncio
    async def test_log_only_never_changes_the_mode(self) -> None:
        """The zero-ranking-change guarantee: the classifier says keyword,
        the effective mode stays the historical default."""
        with _patch_config("log_only"):
            assert (
                await _service()._resolve_search_mode(_request(), _CTX, cross_context=False)
                == "hybrid"
            )

    @pytest.mark.asyncio
    async def test_log_only_explicit_mode_wins(self) -> None:
        with _patch_config("log_only"):
            assert (
                await _service()._resolve_search_mode(
                    _request(search_mode="keyword"), _CTX, cross_context=False
                )
                == "keyword"
            )

    @pytest.mark.asyncio
    async def test_log_only_stamps_telemetry(self) -> None:
        with (
            _patch_config("log_only"),
            patch("services.memory_service.logger") as log,
        ):
            await _service()._resolve_search_mode(_request(), _CTX, cross_context=False)
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
        with _patch_config("active"):
            assert (
                await _service()._resolve_search_mode(_request(), _CTX, cross_context=False)
                == "keyword"
            )

    @pytest.mark.asyncio
    async def test_active_explicit_mode_always_wins(self) -> None:
        with _patch_config("active"):
            assert (
                await _service()._resolve_search_mode(
                    _request(search_mode="hybrid"), _CTX, cross_context=False
                )
                == "hybrid"
            )

    @pytest.mark.asyncio
    async def test_active_semantic_query_routes_semantic(self) -> None:
        with _patch_config("active"):
            assert (
                await _service()._resolve_search_mode(
                    _request(query="what were the sleep maintenance design decisions"),
                    _CTX,
                    cross_context=False,
                )
                == "semantic"
            )


class TestFailOpenAndScope:
    @pytest.mark.asyncio
    async def test_cross_context_skips_routing_and_config_read(self) -> None:
        repo_cls = MagicMock()
        with patch("repositories.config_repository.ContextSearchConfigRepository", repo_cls):
            mode = await _service()._resolve_search_mode(_request(), _CTX, cross_context=True)
        assert mode == "hybrid"
        repo_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_config_read_failure_fails_open(self) -> None:
        repo = MagicMock()
        repo.get_by_context = AsyncMock(side_effect=RuntimeError("db down"))
        with patch(
            "repositories.config_repository.ContextSearchConfigRepository",
            return_value=repo,
        ):
            mode = await _service()._resolve_search_mode(
                _request(search_mode="semantic"), _CTX, cross_context=False
            )
        assert mode == "semantic"

    @pytest.mark.asyncio
    async def test_mock_like_config_without_valid_mode_is_off(self) -> None:
        """A config whose routing_mode is not a recognized value (e.g. a
        MagicMock attribute) must behave as 'off' — membership check, never
        truthiness (#1207 lesson)."""
        with _patch_config(""):
            assert (
                await _service()._resolve_search_mode(_request(), _CTX, cross_context=False)
                == "hybrid"
            )
