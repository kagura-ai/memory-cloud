"""The write paths resolve ``details.event_phrase`` and echo the event dates.

``remember`` stores the phrase verbatim and, when it resolves against the
stored ``observed_at`` day, the ``event_start`` / ``event_end`` pair; a phrase
without an ``observed_at`` or outside the rule set leaves the pair absent (the
server never guesses and never counts from the write time). ``update_memory``
resolves again on every details write, the ``observed_at``-only edit included.
These drive the real service methods, as tests/services/test_observed_at_write_response.py
does, whose fixtures they reuse.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.memory_service import MemoryService
from tests.services.test_observed_at_write_response import (
    _remember,
    _stored_memory,
    _update_in_place,
)


@pytest.fixture
def service():
    """The service of tests/services/test_observed_at_write_response.py, same mocks."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.execute = AsyncMock()

    svc = MemoryService(db)
    context = MagicMock()
    context.id = uuid4()
    context.workspace_id = uuid4()
    svc._get_context_isolation_params = AsyncMock(
        return_value=(context, str(context.workspace_id), str(context.id))
    )
    svc.memory_repo = MagicMock()
    svc.memory_repo.create = AsyncMock()
    svc._create_declared_links = AsyncMock()
    svc._mock_context = context
    return svc


def _written(service):
    """The row ``remember`` handed to the repository."""
    return service.memory_repo.create.await_args.args[0]


class TestRemember:
    @pytest.mark.asyncio
    async def test_the_phrase_is_stored_verbatim_and_resolved(self, service):
        result = await _remember(
            service, details={"observed_at": "2024-03-11", "event_phrase": "Last weekend"}
        )
        assert (result.event_start, result.event_end) == ("2024-03-09", "2024-03-10")
        assert result.event_phrase == "Last weekend"
        assert _written(service).details == {
            "observed_at": "2024-03-11",
            "event_phrase": "Last weekend",
            "event_start": "2024-03-09",
            "event_end": "2024-03-10",
        }

    @pytest.mark.asyncio
    async def test_the_top_level_observed_at_is_the_anchor(self, service):
        # Folded into details and normalised to UTC first: the stored day is 2024-03-11.
        result = await _remember(
            service, observed_at="2024-03-11T23:30:00+09:00", details={"event_phrase": "yesterday"}
        )
        assert result.observed_at == "2024-03-11T14:30:00Z"
        assert (result.event_start, result.event_end) == ("2024-03-10", "2024-03-10")

    @pytest.mark.asyncio
    async def test_an_unknown_phrase_leaves_the_dates_absent(self, service):
        result = await _remember(
            service, details={"observed_at": "2024-03-11", "event_phrase": "a few days ago"}
        )
        assert result.event_start is None and result.event_end is None
        assert result.event_phrase == "a few days ago"
        details = _written(service).details
        assert details["event_phrase"] == "a few days ago"
        assert "event_start" not in details and "event_end" not in details

    @pytest.mark.asyncio
    async def test_no_observed_at_leaves_the_phrase_unresolved(self, service):
        """Nothing to count from: the dates are not taken from the write time."""
        result = await _remember(service, details={"event_phrase": "yesterday"})
        assert result.observed_at is None
        assert result.event_start is None and result.event_end is None
        assert result.event_phrase == "yesterday"
        assert "event_start" not in _written(service).details

    @pytest.mark.asyncio
    async def test_an_explicit_pair_is_kept(self, service):
        result = await _remember(
            service, details={"event_start": "2024-03-09", "event_end": "2024-03-10"}
        )
        assert (result.event_start, result.event_end, result.event_phrase) == (
            "2024-03-09",
            "2024-03-10",
            None,
        )

    @pytest.mark.asyncio
    async def test_none_without_one(self, service):
        result = await _remember(service, details={"observed_at": "2024-03-11"})
        assert (result.event_start, result.event_end, result.event_phrase) == (None, None, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "details",
        [
            {"observed_at": "2024-03-11", "event_phrase": ""},
            {"event_start": "2024-3-9"},
            {"event_end": "2024-03-10"},
        ],
    )
    async def test_a_bad_value_is_the_established_value_error(self, service, details):
        with pytest.raises(ValueError, match="invalid details.event_phrase"):
            await _remember(service, details=details)
        service.memory_repo.create.assert_not_awaited()


_STORED = {
    "observed_at": "2024-03-11",
    "event_phrase": "last weekend",
    "event_start": "2024-03-09",
    "event_end": "2024-03-10",
}


class TestUpdateInPlace:
    @pytest.mark.asyncio
    async def test_a_details_write_resolves_again(self, service):
        memory = _stored_memory(dict(_STORED))
        result = await _update_in_place(
            service, memory, details={"observed_at": "2024-03-18", "event_phrase": "last weekend"}
        )
        assert (result.event_start, result.event_end) == ("2024-03-16", "2024-03-17")
        assert memory.details["event_start"] == "2024-03-16"

    @pytest.mark.asyncio
    async def test_observed_at_alone_moves_the_event(self, service):
        """The fold makes it a details write, so the carried phrase is counted again."""
        memory = _stored_memory(dict(_STORED))
        result = await _update_in_place(service, memory, observed_at="2024-03-18")
        assert result.observed_at == "2024-03-18"
        assert (result.event_start, result.event_end) == ("2024-03-16", "2024-03-17")
        assert result.event_phrase == "last weekend"

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_carries_the_stored_dates(self, service):
        memory = _stored_memory(dict(_STORED))
        result = await _update_in_place(service, memory, importance=0.9)
        assert (result.event_start, result.event_end, result.event_phrase) == (
            "2024-03-09",
            "2024-03-10",
            "last weekend",
        )

    @pytest.mark.asyncio
    async def test_a_details_write_without_the_phrase_drops_the_dates(self, service):
        memory = _stored_memory(dict(_STORED))
        result = await _update_in_place(service, memory, details={"observed_at": "2024-03-11"})
        assert (result.event_start, result.event_end, result.event_phrase) == (None, None, None)


def test_every_write_response_construction_carries_the_event_dates():
    """A construction site added later without them would drop the echo silently.

    Each ``RememberResponse`` / ``UpdateMemoryResponse`` call either splats
    ``event_response_kwargs(...)`` or names ``event_start`` itself.
    """
    import services.memory_service as memory_service

    tree = ast.parse(Path(memory_service.__file__).read_text(encoding="utf-8"))
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"RememberResponse", "UpdateMemoryResponse"}
    ]
    assert len(sites) >= 4, f"expected the known write-response sites, found {len(sites)}"
    for call in sites:
        carries = any(
            kw.arg == "event_start"
            or (
                kw.arg is None
                and isinstance(kw.value, ast.Call)
                and isinstance(kw.value.func, ast.Name)
                and kw.value.func.id == "event_response_kwargs"
            )
            for kw in call.keywords
        )
        assert carries, f"{call.func.id} at line {call.lineno} is built without the event dates"
