"""Whether the caller may accept a supersede suggestion (#2427).

The pure half of ``services.supersede_acceptance``: the reason precedence over
a pair's ownership and order, the edge_exists predicate and the order
``evaluate`` reports in (#2518), and the wire shape. The database half (the
ownership query, the write probe, reference/PATCH/MCP) is in
``tests/integration/test_2427_supersede_acceptance_db.py``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import SupersedeAcceptance, SupersedeCandidate
from services.supersede_acceptance import (
    ACCEPT_CONFIDENCE,
    ACCEPT_WEIGHT,
    AcceptanceReason,
    PairOwnership,
    acceptance_reason,
    conflicting_declared_edge,
    evaluate,
)

_OLD = datetime(2026, 1, 1, tzinfo=UTC)
_NEW = _OLD + timedelta(hours=1)


def _pair(
    *,
    carrier_own: bool = True,
    target_own: bool = True,
    carrier_created_at: datetime | None = _NEW,
    target_created_at: datetime | None = _OLD,
    target_exists: bool = True,
) -> PairOwnership:
    return PairOwnership(
        carrier_own=carrier_own,
        target_own=target_own,
        carrier_created_at=carrier_created_at,
        target_created_at=target_created_at,
        target_exists=target_exists,
    )


class TestAcceptanceReason:
    def test_own_pair_with_an_older_candidate_is_allowed(self):
        assert acceptance_reason(_pair()) is None

    @pytest.mark.parametrize(
        ("carrier_own", "target_own"),
        [(False, True), (True, False), (False, False)],
        ids=["foreign_carrier", "foreign_target", "foreign_both"],
    )
    def test_a_foreign_end_is_one_uniform_reason(self, carrier_own, target_own):
        """Never which end: the reason is the same for either or both."""
        pair = _pair(carrier_own=carrier_own, target_own=target_own)
        assert acceptance_reason(pair) == "not_owner"

    def test_a_missing_target_is_not_owner(self):
        """The edge writer refuses a missing endpoint with the same uniform denial."""
        pair = _pair(target_exists=False, target_own=False, target_created_at=None)
        assert acceptance_reason(pair) == "not_owner"

    def test_a_candidate_written_after_the_carrier_is_newer(self):
        pair = _pair(carrier_created_at=_OLD, target_created_at=_NEW)
        assert acceptance_reason(pair) == "newer_candidate"

    def test_unknown_timestamps_do_not_refuse(self):
        """The accept path skips the direction rule when a timestamp is missing."""
        assert acceptance_reason(_pair(carrier_created_at=None)) is None
        assert acceptance_reason(_pair(target_created_at=None)) is None

    def test_ownership_wins_over_direction(self):
        pair = _pair(target_own=False, carrier_created_at=_OLD, target_created_at=_NEW)
        assert acceptance_reason(pair) == "not_owner"


class TestWireShape:
    def test_allowed_dumps_without_a_reason(self):
        dumped = SupersedeAcceptance(allowed=True).model_dump(exclude_none=True)
        assert dumped == {"allowed": True}

    def test_refused_dumps_its_reason(self):
        dumped = SupersedeAcceptance(allowed=False, reason="not_owner").model_dump(
            exclude_none=True
        )
        assert dumped == {"allowed": False, "reason": "not_owner"}

    @pytest.mark.parametrize(
        "reason", ["not_writable", "not_owner", "newer_candidate", "edge_exists"]
    )
    def test_every_reason_of_the_rule_is_on_the_wire(self, reason):
        """``AcceptanceReason`` and the wire Literal name the same set (#2518)."""
        assert reason in get_args(AcceptanceReason)
        assert SupersedeAcceptance(allowed=False, reason=reason).reason == reason

    def test_an_unknown_reason_is_rejected(self):
        with pytest.raises(ValueError):
            SupersedeAcceptance(allowed=False, reason="target_not_owner")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            SupersedeAcceptance(allowed=False, reason="conflict")  # type: ignore[arg-type]


def _edge(**overrides: Any) -> SimpleNamespace:
    values = {
        "origin": "declared",
        "edge_type": "supersedes",
        "weight": ACCEPT_WEIGHT,
        "confidence": ACCEPT_CONFIDENCE,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestConflictingDeclaredEdge:
    """#2518: the edge writer's duplicate contract, read before the click."""

    async def _conflicts(self, existing: SimpleNamespace | None) -> bool:
        with patch(
            "repositories.neural_edge.NeuralEdgeRepository.get_edge",
            AsyncMock(return_value=existing),
        ):
            return await conflicting_declared_edge(
                MagicMock(),
                owner_id="owner",
                workspace_id=uuid4(),
                context_id=uuid4(),
                carrier_id=uuid4(),
                target_id=uuid4(),
            )

    @pytest.mark.asyncio
    async def test_no_edge_does_not_conflict(self):
        assert await self._conflicts(None) is False

    @pytest.mark.asyncio
    async def test_the_accepts_own_edge_is_unchanged(self):
        assert await self._conflicts(_edge()) is False

    @pytest.mark.asyncio
    async def test_an_automatic_edge_is_overwritten_not_refused(self):
        assert await self._conflicts(_edge(origin="hebbian", edge_type="related_to")) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [{"edge_type": "related_to"}, {"weight": 0.5}, {"confidence": 0.8}],
        ids=["type", "weight", "confidence"],
    )
    async def test_a_declared_edge_with_other_values_conflicts(self, overrides):
        assert await self._conflicts(_edge(**overrides)) is True


class TestEvaluatePrecedence:
    """``not_writable`` > ``not_owner`` > ``newer_candidate`` > ``edge_exists``;
    a later check runs only when the earlier ones pass."""

    async def _evaluate(self, *, writable: bool, pair: PairOwnership, edge: bool):
        memory = SimpleNamespace(
            id=uuid4(), user_id="owner", workspace_id=uuid4(), context_id=uuid4()
        )
        probe_pair = AsyncMock(return_value=pair)
        probe_edge = AsyncMock(return_value=edge)
        with (
            patch(
                "services.supersede_acceptance.can_write_edges", AsyncMock(return_value=writable)
            ),
            patch("services.supersede_acceptance.pair_ownership", probe_pair),
            patch("services.supersede_acceptance.conflicting_declared_edge", probe_edge),
        ):
            result = await evaluate(MagicMock(), user_id="owner", memory=memory, target_id=uuid4())
        return result, probe_pair, probe_edge

    @pytest.mark.asyncio
    async def test_not_writable_wins_and_reads_nothing_else(self):
        result, probe_pair, probe_edge = await self._evaluate(
            writable=False, pair=_pair(carrier_own=False), edge=True
        )
        assert result.reason == "not_writable"
        assert probe_pair.await_count == 0 and probe_edge.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pair", "reason"),
        [
            (_pair(target_own=False), "not_owner"),
            (_pair(carrier_created_at=_OLD, target_created_at=_NEW), "newer_candidate"),
        ],
    )
    async def test_the_pair_wins_over_edge_exists(self, pair, reason):
        result, _, probe_edge = await self._evaluate(writable=True, pair=pair, edge=True)
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": reason}
        assert probe_edge.await_count == 0

    @pytest.mark.asyncio
    async def test_edge_exists_when_everything_else_passes(self):
        result, _, probe_edge = await self._evaluate(writable=True, pair=_pair(), edge=True)
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
        assert probe_edge.await_args.kwargs["owner_id"] == "owner"

    @pytest.mark.asyncio
    async def test_allowed_when_nothing_refuses(self):
        result, _, _ = await self._evaluate(writable=True, pair=_pair(), edge=False)
        assert result.model_dump(exclude_none=True) == {"allowed": True}

    def test_a_candidate_without_acceptance_keeps_the_recall_shape(self):
        """Recall never sets it, so exclude_none keeps the key off the wire."""
        candidate = SupersedeCandidate(memory_id=uuid4(), summary="s", similarity=0.9)
        assert "acceptance" not in candidate.model_dump(exclude_none=True)
