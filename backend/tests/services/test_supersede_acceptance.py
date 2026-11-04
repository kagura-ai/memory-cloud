"""Whether the caller may accept a supersede suggestion (#2427).

The pure half of ``services.supersede_acceptance``: the reason precedence over
a pair's ownership and order, the edge_exists predicate and the order
``evaluate`` reports in (#2518), the wire shape, and how ``evaluate_many``
batches a recall page's probes (#2508). The database half (the ownership
query, the write probe, reference/PATCH/MCP, recall pages) is in
``tests/integration/test_2427_supersede_acceptance_db.py`` and
``tests/integration/test_2508_recall_supersede_acceptance_db.py``.
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
    EdgeProbe,
    PairOwnership,
    acceptance_reason,
    conflicting_declared_edges,
    edge_owner,
    evaluate,
    evaluate_many,
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


class TestConflictingDeclaredEdges:
    """#2518: the edge writer's duplicate contract, read before the click;
    #2508: for a page of pairs in one query."""

    _CARRIER = uuid4()
    _TARGET = uuid4()
    _WS = uuid4()
    _CTX = uuid4()

    async def _conflicts(
        self, *rows: SimpleNamespace, owner: str | frozenset[str] = "owner"
    ) -> tuple[bool, AsyncMock]:
        listed = AsyncMock(
            return_value=[
                SimpleNamespace(src_id=self._CARRIER, dst_id=self._TARGET, **vars(row))
                for row in rows
            ]
        )
        with patch("repositories.neural_edge.NeuralEdgeRepository.list_pairs_edges", listed):
            conflicts = await conflicting_declared_edges(
                MagicMock(),
                {
                    self._CARRIER: EdgeProbe(
                        owner=owner,
                        target_id=self._TARGET,
                        workspace_id=self._WS,
                        context_id=self._CTX,
                    )
                },
            )
        return self._CARRIER in conflicts, listed

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
        assert sorted(listed.await_args.args[0]) == sorted(
            (account, self._CARRIER, self._TARGET, self._WS, self._CTX) for account in owners
        )

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

    @pytest.mark.asyncio
    async def test_a_page_of_pairs_is_one_lookup_and_each_carrier_its_own_answer(self):
        """#2508: one repository call for every pair; a conflict is the carrier's own."""
        clean, conflicted, bare = uuid4(), uuid4(), uuid4()
        probes = {
            carrier: EdgeProbe(
                owner="owner", target_id=uuid4(), workspace_id=self._WS, context_id=self._CTX
            )
            for carrier in (clean, conflicted, bare)
        }
        rows = [
            SimpleNamespace(src_id=clean, dst_id=probes[clean].target_id, **vars(_edge())),
            SimpleNamespace(
                src_id=conflicted,
                dst_id=probes[conflicted].target_id,
                **vars(_edge(edge_type="related_to")),
            ),
        ]
        listed = AsyncMock(return_value=rows)
        with patch("repositories.neural_edge.NeuralEdgeRepository.list_pairs_edges", listed):
            conflicts = await conflicting_declared_edges(MagicMock(), probes)

        assert conflicts == {conflicted}
        assert listed.await_count == 1
        assert len(listed.await_args.args[0]) == 3

    @pytest.mark.asyncio
    async def test_no_pairs_read_nothing(self):
        listed = AsyncMock(return_value=[])
        with patch("repositories.neural_edge.NeuralEdgeRepository.list_pairs_edges", listed):
            assert await conflicting_declared_edges(MagicMock(), {}) == set()
        assert listed.await_count == 0


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


class _Probes:
    """The batch's probes as mocks, answering per carrier.

    ``writable`` maps a context id to the write probe's answer (default True),
    ``pairs`` a carrier id to its :class:`PairOwnership` (default own and
    older), ``edges`` the carriers whose pair meets a different declared edge.
    """

    def __init__(
        self,
        *,
        writable: dict[Any, bool] | None = None,
        pairs: dict[Any, PairOwnership] | None = None,
        edges: set[Any] | None = None,
        private: bool = False,
    ) -> None:
        writable = writable or {}
        pairs = pairs or {}
        edges = edges or set()
        self.can_write = AsyncMock(
            side_effect=lambda _db, **kw: writable.get(kw["context_id"], True)
        )
        self.pairs = AsyncMock(
            side_effect=lambda _db, **kw: {cid: pairs.get(cid, _pair()) for cid in kw["pairs"]}
        )
        self.edges = AsyncMock(side_effect=lambda _db, probes: edges & set(probes))
        self.private = AsyncMock(return_value=private)

    async def run(self, memories, targets, *, user_id: str = "caller"):
        with (
            patch("services.supersede_acceptance.can_write_edges", self.can_write),
            patch("services.supersede_acceptance.pair_ownerships", self.pairs),
            patch("services.supersede_acceptance.conflicting_declared_edges", self.edges),
            patch("services.supersede_acceptance.is_private_context", self.private),
        ):
            return await evaluate_many(
                MagicMock(), user_id=user_id, memories=memories, targets=targets
            )


def _carrier(*, context_id=None, workspace_id=None, user_id: str = "caller") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id or uuid4(),
        context_id=context_id or uuid4(),
    )


class TestEvaluatePrecedence:
    """``not_writable`` > ``not_owner`` > ``newer_candidate`` > ``edge_exists``;
    a later check runs only when the earlier ones pass. ``evaluate`` is
    ``evaluate_many`` over one memory (#2508)."""

    async def _evaluate(self, *, writable: bool, pair: PairOwnership, edge: bool):
        memory = _carrier(user_id="owner")
        probes = _Probes(
            writable={memory.context_id: writable},
            pairs={memory.id: pair},
            edges={memory.id} if edge else set(),
            private=True,
        )
        with (
            patch("services.supersede_acceptance.can_write_edges", probes.can_write),
            patch("services.supersede_acceptance.pair_ownerships", probes.pairs),
            patch("services.supersede_acceptance.conflicting_declared_edges", probes.edges),
            patch("services.supersede_acceptance.is_private_context", probes.private),
        ):
            result = await evaluate(MagicMock(), user_id="caller", memory=memory, target_id=uuid4())
        return result, probes, memory

    @pytest.mark.asyncio
    async def test_not_writable_wins_and_reads_nothing_else(self):
        result, probes, _ = await self._evaluate(
            writable=False, pair=_pair(carrier_own=False), edge=True
        )
        assert result.reason == "not_writable"
        assert probes.pairs.await_count == 0 and probes.edges.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pair", "reason"),
        [
            (_pair(target_own=False), "not_owner"),
            (_pair(carrier_created_at=_OLD, target_created_at=_NEW), "newer_candidate"),
        ],
    )
    async def test_the_pair_wins_over_edge_exists(self, pair, reason):
        result, probes, _ = await self._evaluate(writable=True, pair=pair, edge=True)
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": reason}
        assert probes.edges.await_count == 0

    @pytest.mark.asyncio
    async def test_edge_exists_when_everything_else_passes(self):
        """#2549: the edge is looked up under the carrier author and the caller."""
        result, probes, memory = await self._evaluate(writable=True, pair=_pair(), edge=True)
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
        probe = probes.edges.await_args.args[1][memory.id]
        assert probe.owner == frozenset({"owner", "caller"})
        assert (probe.workspace_id, probe.context_id) == (memory.workspace_id, memory.context_id)

    @pytest.mark.asyncio
    async def test_allowed_when_nothing_refuses(self):
        result, _, _ = await self._evaluate(writable=True, pair=_pair(), edge=False)
        assert result.model_dump(exclude_none=True) == {"allowed": True}

    @pytest.mark.asyncio
    async def test_evaluate_is_the_batch_over_one_memory(self):
        memory = _carrier()
        target = uuid4()
        answer = SupersedeAcceptance(allowed=False, reason="edge_exists")
        batch = AsyncMock(return_value={memory.id: answer})
        with patch("services.supersede_acceptance.evaluate_many", batch):
            result = await evaluate(MagicMock(), user_id="caller", memory=memory, target_id=target)
        assert result is answer
        assert batch.await_args.kwargs["memories"] == {memory.id: memory}
        assert batch.await_args.kwargs["targets"] == {memory.id: target}

    def test_a_candidate_without_acceptance_keeps_the_old_shape(self):
        """Unset (a failed evaluation, an older backend), exclude_none keeps
        the key off the wire."""
        candidate = SupersedeCandidate(memory_id=uuid4(), summary="s", similarity=0.9)
        assert "acceptance" not in candidate.model_dump(exclude_none=True)


class TestEvaluateManyPrecedence:
    """#2508: a recall page's carriers, each through ``evaluate``'s precedence,
    with each probe batched: write access once per context, ownership once per
    context, the declared-edge lookup once for the page."""

    @pytest.mark.asyncio
    async def test_rows_of_one_page_get_their_own_reasons(self):
        ws, ctx, closed = uuid4(), uuid4(), uuid4()
        allowed, foreign, newer, linked_edge = (
            _carrier(workspace_id=ws, context_id=ctx) for _ in range(4)
        )
        unwritable = _carrier(workspace_id=ws, context_id=closed)
        memories = {m.id: m for m in (allowed, foreign, newer, linked_edge, unwritable)}
        probes = _Probes(
            writable={closed: False},
            pairs={
                foreign.id: _pair(carrier_own=False),
                newer.id: _pair(carrier_created_at=_OLD, target_created_at=_NEW),
            },
            # A not_owner pair with a conflicting edge stays not_owner.
            edges={linked_edge.id, foreign.id},
        )

        result = await probes.run(memories, {m: uuid4() for m in memories})

        assert {cid: a.model_dump(exclude_none=True) for cid, a in result.items()} == {
            allowed.id: {"allowed": True},
            foreign.id: {"allowed": False, "reason": "not_owner"},
            newer.id: {"allowed": False, "reason": "newer_candidate"},
            linked_edge.id: {"allowed": False, "reason": "edge_exists"},
            unwritable.id: {"allowed": False, "reason": "not_writable"},
        }

    @pytest.mark.asyncio
    async def test_each_probe_is_once_per_context_or_page(self):
        ws = uuid4()
        first, second = uuid4(), uuid4()
        page = [_carrier(workspace_id=ws, context_id=first) for _ in range(3)] + [
            _carrier(workspace_id=ws, context_id=second) for _ in range(2)
        ]
        memories = {m.id: m for m in page}
        probes = _Probes()

        result = await probes.run(memories, {m: uuid4() for m in memories})

        assert all(a.allowed for a in result.values()) and len(result) == 5
        assert sorted(c.kwargs["context_id"] for c in probes.can_write.await_args_list) == sorted(
            [first, second]
        )
        assert probes.private.await_count == 2
        assert sorted(len(c.kwargs["pairs"]) for c in probes.pairs.await_args_list) == [2, 3]
        assert probes.edges.await_count == 1
        assert set(probes.edges.await_args.args[1]) == set(memories)

    @pytest.mark.asyncio
    async def test_a_context_without_write_access_is_not_read_further(self):
        ws, closed = uuid4(), uuid4()
        page = [_carrier(workspace_id=ws, context_id=closed) for _ in range(3)]
        probes = _Probes(writable={closed: False})

        result = await probes.run({m.id: m for m in page}, {m.id: uuid4() for m in page})

        assert {a.reason for a in result.values()} == {"not_writable"}
        assert probes.can_write.await_count == 1
        assert probes.pairs.await_count == 0 and probes.private.await_count == 0
        assert probes.edges.await_count == 0

    @pytest.mark.asyncio
    async def test_only_pairs_that_pass_ownership_reach_the_edge_lookup(self):
        ctx = uuid4()
        own, foreign = _carrier(context_id=ctx), _carrier(context_id=ctx)
        probes = _Probes(pairs={foreign.id: _pair(target_own=False)})

        await probes.run({own.id: own, foreign.id: foreign}, {own.id: uuid4(), foreign.id: uuid4()})

        assert set(probes.edges.await_args.args[1]) == {own.id}

    @pytest.mark.asyncio
    async def test_the_pairs_carry_their_targets(self):
        memory = _carrier()
        target = uuid4()
        probes = _Probes()

        await probes.run({memory.id: memory}, {memory.id: target})

        assert probes.pairs.await_args.kwargs["pairs"] == {memory.id: target}
        assert probes.edges.await_args.args[1][memory.id].target_id == target

    @pytest.mark.asyncio
    async def test_only_carriers_with_a_target_are_answered(self):
        with_target, without = _carrier(), _carrier()
        probes = _Probes()

        result = await probes.run(
            {with_target.id: with_target, without.id: without},
            {with_target.id: uuid4(), uuid4(): uuid4()},
        )

        assert set(result) == {with_target.id}

    @pytest.mark.asyncio
    async def test_nothing_to_evaluate_reads_nothing(self):
        probes = _Probes()

        assert await probes.run({}, {}) == {}
        assert probes.can_write.await_count == 0 and probes.edges.await_count == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("private", "author", "owner"),
        [
            (True, "author", frozenset({"author", "caller"})),
            (False, "author", "author"),
            (True, "caller", "caller"),
        ],
        ids=["linked_private", "shared", "own"],
    )
    async def test_the_edge_owner_follows_the_contexts_privacy(self, private, author, owner):
        """#2549: the author's row and a linked caller's row in a private
        context, the author's elsewhere; privacy is read once per context."""
        ctx = uuid4()
        page = [_carrier(context_id=ctx, user_id=author) for _ in range(3)]
        probes = _Probes(private=private)

        await probes.run({m.id: m for m in page}, {m.id: uuid4() for m in page})

        assert {p.owner for p in probes.edges.await_args.args[1].values()} == {owner}
        assert probes.private.await_count == 1
        assert probes.pairs.await_args.kwargs["private"] is private
