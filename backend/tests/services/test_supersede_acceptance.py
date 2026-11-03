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
    edge_owner,
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

    async def _conflicts(
        self, *rows: SimpleNamespace, owner: str | frozenset[str] = "owner"
    ) -> tuple[bool, AsyncMock]:
        listed = AsyncMock(return_value=list(rows))
        with patch("repositories.neural_edge.NeuralEdgeRepository.list_pair_edges", listed):
            conflicts = await conflicting_declared_edge(
                MagicMock(),
                owner=owner,
                workspace_id=uuid4(),
                context_id=uuid4(),
                carrier_id=uuid4(),
                target_id=uuid4(),
            )
        return conflicts, listed

    @pytest.mark.asyncio
    async def test_no_edge_does_not_conflict(self):
        assert (await self._conflicts())[0] is False

    @pytest.mark.asyncio
    async def test_the_accepts_own_edge_is_unchanged(self):
        assert (await self._conflicts(_edge()))[0] is False

    @pytest.mark.asyncio
    async def test_an_automatic_edge_is_overwritten_not_refused(self):
        assert (await self._conflicts(_edge(origin="hebbian", edge_type="related_to")))[0] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [{"edge_type": "related_to"}, {"weight": 0.5}, {"confidence": 0.8}],
        ids=["type", "weight", "confidence"],
    )
    async def test_a_declared_edge_with_other_values_conflicts(self, overrides):
        assert (await self._conflicts(_edge(**overrides)))[0] is True

    @pytest.mark.asyncio
    async def test_every_row_of_the_owner_filter_is_read(self):
        """#2549: the carrier author's row and the caller's row are each read."""
        owners = frozenset({"author", "caller"})
        _, listed = await self._conflicts(owner=owners)
        assert listed.await_args.args[0] == owners

    @pytest.mark.asyncio
    async def test_a_conflict_behind_a_clean_stronger_row_is_seen(self):
        """#2549: the strongest parallel row is the accept's own edge, the
        caller's weaker row differs — the web dialog's click still meets it."""
        clean = _edge()
        weaker = _edge(edge_type="related_to", weight=0.5, user_id="caller")
        assert (await self._conflicts(clean, weaker))[0] is True

    @pytest.mark.asyncio
    async def test_clean_and_automatic_rows_together_do_not_conflict(self):
        rows = (_edge(), _edge(origin="semantic", edge_type="related_to", weight=0.3))
        assert (await self._conflicts(*rows))[0] is False


class TestEdgeOwner:
    """#2549: whose declared edges on the pair the probe reads — the carrier
    author's row and the caller's row, the two accounts an accept writes under."""

    async def _owner(
        self, *, private: bool, caller_id: str = "caller"
    ) -> tuple[str | frozenset[str], AsyncMock]:
        probe_private = AsyncMock(return_value=private)
        with patch("services.supersede_acceptance.is_private_context", probe_private):
            owner = await edge_owner(
                MagicMock(), author_id="author", caller_id=caller_id, context_id=uuid4()
            )
        return owner, probe_private

    @pytest.mark.asyncio
    async def test_a_linked_caller_in_a_private_context_reads_both_rows(self):
        owner, _ = await self._owner(private=True)
        assert owner == frozenset({"author", "caller"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("private", [True, False], ids=["private", "shared"])
    async def test_the_author_as_caller_reads_the_author_alone(self, private):
        owner, probe_private = await self._owner(private=private, caller_id="author")
        assert owner == "author"
        assert probe_private.await_count == 0

    @pytest.mark.asyncio
    async def test_a_shared_context_reads_the_author_alone(self):
        owner, _ = await self._owner(private=False)
        assert owner == "author"


class TestEvaluatePrecedence:
    """``not_writable`` > ``not_owner`` > ``newer_candidate`` > ``edge_exists``;
    a later check runs only when the earlier ones pass."""

    _OWNERS = frozenset({"owner", "caller"})

    async def _evaluate(self, *, writable: bool, pair: PairOwnership, edge: bool):
        memory = SimpleNamespace(
            id=uuid4(), user_id="owner", workspace_id=uuid4(), context_id=uuid4()
        )
        probe_pair = AsyncMock(return_value=pair)
        probe_edge = AsyncMock(return_value=edge)
        self.probe_owner = AsyncMock(return_value=self._OWNERS)
        with (
            patch(
                "services.supersede_acceptance.can_write_edges", AsyncMock(return_value=writable)
            ),
            patch("services.supersede_acceptance.pair_ownership", probe_pair),
            patch("services.supersede_acceptance.edge_owner", self.probe_owner),
            patch("services.supersede_acceptance.conflicting_declared_edge", probe_edge),
        ):
            result = await evaluate(MagicMock(), user_id="caller", memory=memory, target_id=uuid4())
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
        assert probe_edge.await_count == 0 and self.probe_owner.await_count == 0

    @pytest.mark.asyncio
    async def test_edge_exists_when_everything_else_passes(self):
        """#2549: the edge is looked up under the carrier author and the caller."""
        result, _, probe_edge = await self._evaluate(writable=True, pair=_pair(), edge=True)
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
        assert self.probe_owner.await_args.kwargs["author_id"] == "owner"
        assert self.probe_owner.await_args.kwargs["caller_id"] == "caller"
        assert probe_edge.await_args.kwargs["owner"] == self._OWNERS

    @pytest.mark.asyncio
    async def test_allowed_when_nothing_refuses(self):
        result, _, _ = await self._evaluate(writable=True, pair=_pair(), edge=False)
        assert result.model_dump(exclude_none=True) == {"allowed": True}

    def test_a_candidate_without_acceptance_keeps_the_recall_shape(self):
        """Recall never sets it, so exclude_none keeps the key off the wire."""
        candidate = SupersedeCandidate(memory_id=uuid4(), summary="s", similarity=0.9)
        assert "acceptance" not in candidate.model_dump(exclude_none=True)
