"""Whether the caller may accept a supersede suggestion (#2427).

The pure half of ``services.supersede_acceptance``: the reason precedence over
a pair's ownership and order, the edge_exists predicate and the order
``evaluate`` reports in (#2518), the guardrail-carrier gate (#2614), the wire
shape, and how ``evaluate_many`` batches a recall page's probes (#2508). The database half (the ownership
query, the write probe, reference/PATCH/MCP, recall pages) is in
``tests/integration/test_2427_supersede_acceptance_db.py`` and
``tests/integration/test_2508_recall_supersede_acceptance_db.py``.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

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
    evaluate,
    evaluate_many,
    may_author_guardrail,
)
from utils.tool_trigger import carries_tool_trigger

# A stored guardrail marking, as normalize_tool_trigger writes it.
_GUARDRAIL = {"tool_trigger": {"tool": "Bash", "on": "pre", "match": "gh", "action": "inform"}}

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
        self, *rows: SimpleNamespace, owner: str = "owner"
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
    async def test_the_rows_are_read_under_the_one_owner(self):
        """#2614: the account the accept writes under, and no other."""
        _, listed = await self._conflicts(owner="author")
        assert listed.await_args.args[0] == [
            ("author", self._CARRIER, self._TARGET, self._WS, self._CTX)
        ]

    @pytest.mark.asyncio
    async def test_any_conflicting_row_refuses_not_only_the_first(self):
        clean = _edge()
        weaker = _edge(edge_type="related_to", weight=0.5)
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
    """#2614 (the #2549 owner question): the probe reads the carrier author's
    row alone. Both transports write the accept under that author since #2600,
    so a row kept by the caller, linked or not, is never one the accept meets."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("private", [True, False], ids=["private", "shared"])
    @pytest.mark.parametrize("caller_id", ["author", "linked_caller"])
    async def test_the_edge_is_read_under_the_carrier_author_alone(self, caller_id, private):
        memory = _carrier(user_id="author")
        probes = _Probes(private=private)

        await probes.run({memory.id: memory}, {memory.id: uuid4()}, user_id=caller_id)

        assert probes.edges.await_args.args[1][memory.id].owner == "author"


class TestMayAuthorGuardrail:
    """#2614: the probe twin of ``MemoryService._require_guardrail_author``."""

    async def _probe(
        self, *, agent: bool, can_write: bool, context_id: Any = "ctx"
    ) -> tuple[bool, AsyncMock]:
        if context_id == "ctx":
            context_id = uuid4()
        write = AsyncMock(return_value=can_write)
        scope = SimpleNamespace(agent_id=uuid4()) if agent else None
        with (
            patch("auth.agent_scope.get_agent_scope", return_value=scope),
            patch("services.permission_service.PermissionService.can_write_context", write),
        ):
            allowed = await may_author_guardrail(MagicMock(), user_id="u", context_id=context_id)
        return allowed, write

    @pytest.mark.asyncio
    @pytest.mark.parametrize("can_write", [True, False], ids=["editor", "below_editor"])
    async def test_a_user_credential_is_decided_by_the_context_write_probe(self, can_write):
        context_id = uuid4()
        allowed, write = await self._probe(agent=False, can_write=can_write, context_id=context_id)
        assert allowed is can_write
        assert write.await_args.args == ("u", context_id)

    @pytest.mark.asyncio
    async def test_a_string_context_id_is_probed_as_its_uuid(self):
        context_id = uuid4()
        _, write = await self._probe(agent=False, can_write=True, context_id=str(context_id))
        assert write.await_args.args[1] == context_id
        assert isinstance(write.await_args.args[1], UUID)

    @pytest.mark.asyncio
    async def test_an_agent_credential_never_authors_and_is_not_probed(self):
        """Answered before the context check, whose binding filter would write
        an agent's deny-audit row: a probe is not a denied request."""
        allowed, write = await self._probe(agent=True, can_write=True)
        assert allowed is False
        assert write.await_count == 0

    @pytest.mark.asyncio
    async def test_no_context_is_refused(self):
        allowed, write = await self._probe(agent=False, can_write=True, context_id=None)
        assert allowed is False and write.await_count == 0


class TestCarriesToolTrigger:
    """The one raw-``details`` spelling of "tool guardrail" (``Memory.is_tool_triggered``)."""

    @pytest.mark.parametrize(
        ("details", "carries"),
        [
            (_GUARDRAIL, True),
            (None, False),
            ({}, False),
            ({"note": "x"}, False),
            ({"tool_trigger": None}, False),
            ("tool_trigger", False),
        ],
        ids=["marked", "none", "empty", "plain", "null_marking", "not_a_dict"],
    )
    def test_only_a_stored_marking_counts(self, details, carries):
        assert carries_tool_trigger(details) is carries

    def test_memory_service_reads_the_same_predicate(self):
        from services.memory_service import MemoryService

        assert MemoryService._carries_tool_trigger(_GUARDRAIL) is True
        assert MemoryService._carries_tool_trigger({"tool_trigger": None}) is False


class _Probes:
    """The batch's probes as mocks, answering per carrier.

    ``writable`` maps a context id to the write probe's answer (default True),
    ``may_author`` a context id to the guardrail gate's (default True),
    ``pairs`` a carrier id to its :class:`PairOwnership` (default own and
    older), ``edges`` the carriers whose pair meets a different declared edge.
    """

    def __init__(
        self,
        *,
        writable: dict[Any, bool] | None = None,
        may_author: dict[Any, bool] | None = None,
        pairs: dict[Any, PairOwnership] | None = None,
        edges: set[Any] | None = None,
        private: bool = False,
    ) -> None:
        writable = writable or {}
        may_author = may_author or {}
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
        self.author = AsyncMock(
            side_effect=lambda _db, **kw: may_author.get(kw["context_id"], True)
        )

    def patched(self):
        """The four probes and the privacy read, patched in the module."""
        from contextlib import ExitStack

        stack = ExitStack()
        for name, mock in (
            ("can_write_edges", self.can_write),
            ("may_author_guardrail", self.author),
            ("pair_ownerships", self.pairs),
            ("conflicting_declared_edges", self.edges),
            ("is_private_context", self.private),
        ):
            stack.enter_context(patch(f"services.supersede_acceptance.{name}", mock))
        return stack

    async def run(self, memories, targets, *, user_id: str = "caller"):
        with self.patched():
            return await evaluate_many(
                MagicMock(), user_id=user_id, memories=memories, targets=targets
            )


def _carrier(
    *, context_id=None, workspace_id=None, user_id: str = "caller", details: Any = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id or uuid4(),
        context_id=context_id or uuid4(),
        details=details,
    )


class TestEvaluatePrecedence:
    """``not_writable`` > ``not_owner`` > ``newer_candidate`` > ``edge_exists``;
    a later check runs only when the earlier ones pass. A guardrail carrier's
    author gate (#2614) is part of ``not_writable``. ``evaluate`` is
    ``evaluate_many`` over one memory (#2508)."""

    async def _evaluate(
        self,
        *,
        writable: bool,
        pair: PairOwnership,
        edge: bool,
        details: Any = None,
        may_author: bool = True,
    ):
        memory = _carrier(user_id="owner", details=details)
        probes = _Probes(
            writable={memory.context_id: writable},
            may_author={memory.context_id: may_author},
            pairs={memory.id: pair},
            edges={memory.id} if edge else set(),
            private=True,
        )
        with probes.patched():
            result = await evaluate(MagicMock(), user_id="caller", memory=memory, target_id=uuid4())
        return result, probes, memory

    @pytest.mark.asyncio
    async def test_not_writable_wins_and_reads_nothing_else(self):
        result, probes, _ = await self._evaluate(
            writable=False, pair=_pair(carrier_own=False), edge=True, details=_GUARDRAIL
        )
        assert result.reason == "not_writable"
        assert probes.pairs.await_count == 0 and probes.edges.await_count == 0
        assert probes.author.await_count == 0

    @pytest.mark.asyncio
    async def test_a_guardrail_carrier_below_editor_is_not_writable_before_the_pair(self):
        """#2614: the gate both accept transports run before the accept."""
        result, probes, memory = await self._evaluate(
            writable=True,
            pair=_pair(carrier_own=False),
            edge=True,
            details=_GUARDRAIL,
            may_author=False,
        )
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": "not_writable"}
        assert probes.pairs.await_count == 0 and probes.edges.await_count == 0
        assert probes.author.await_args.kwargs == {
            "user_id": "caller",
            "context_id": memory.context_id,
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pair", "edge", "expected"),
        [
            (_pair(), False, {"allowed": True}),
            (_pair(target_own=False), True, {"allowed": False, "reason": "not_owner"}),
            (_pair(), True, {"allowed": False, "reason": "edge_exists"}),
        ],
        ids=["allowed", "not_owner", "edge_exists"],
    )
    async def test_a_guardrail_author_goes_on_to_the_pair(self, pair, edge, expected):
        result, probes, _ = await self._evaluate(
            writable=True, pair=pair, edge=edge, details=_GUARDRAIL, may_author=True
        )
        assert result.model_dump(exclude_none=True) == expected
        assert probes.author.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "details",
        [None, {"note": "x"}, {"tool_trigger": None}],
        ids=["none", "plain", "null_marking"],
    )
    async def test_a_plain_carrier_is_not_probed_for_the_guardrail_gate(self, details):
        result, probes, _ = await self._evaluate(
            writable=True, pair=_pair(), edge=False, details=details, may_author=False
        )
        assert result.model_dump(exclude_none=True) == {"allowed": True}
        assert probes.author.await_count == 0

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
        """#2614: the edge is looked up under the carrier's author alone."""
        result, probes, memory = await self._evaluate(writable=True, pair=_pair(), edge=True)
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
        probe = probes.edges.await_args.args[1][memory.id]
        assert probe.owner == "owner"
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
    @pytest.mark.parametrize("private", [True, False], ids=["private", "shared"])
    async def test_privacy_is_read_once_per_context_for_ownership(self, private):
        """Linked accounts count as the caller's own only in a private context."""
        ctx = uuid4()
        page = [_carrier(context_id=ctx, user_id="author") for _ in range(3)]
        probes = _Probes(private=private)

        await probes.run({m.id: m for m in page}, {m.id: uuid4() for m in page})

        assert probes.private.await_count == 1
        assert probes.pairs.await_args.kwargs["private"] is private
        assert {p.owner for p in probes.edges.await_args.args[1].values()} == {"author"}

    @pytest.mark.asyncio
    async def test_the_guardrail_gate_is_one_probe_per_context_for_its_carriers_only(self):
        """#2614: a guardrail carrier below EDITOR is ``not_writable``; a plain
        carrier beside it is not held to the gate."""
        ws, ctx = uuid4(), uuid4()
        guarded = [_carrier(workspace_id=ws, context_id=ctx, details=_GUARDRAIL) for _ in range(3)]
        plain = _carrier(workspace_id=ws, context_id=ctx)
        memories = {m.id: m for m in (*guarded, plain)}
        probes = _Probes(may_author={ctx: False})

        result = await probes.run(memories, {m: uuid4() for m in memories})

        assert {cid: a.model_dump(exclude_none=True) for cid, a in result.items()} == {
            **{m.id: {"allowed": False, "reason": "not_writable"} for m in guarded},
            plain.id: {"allowed": True},
        }
        assert probes.author.await_count == 1
        assert probes.pairs.await_args.kwargs["pairs"].keys() == {plain.id}

    @pytest.mark.asyncio
    async def test_no_guardrail_probe_without_a_writable_guardrail_carrier(self):
        ws, closed, open_ = uuid4(), uuid4(), uuid4()
        unwritable = _carrier(workspace_id=ws, context_id=closed, details=_GUARDRAIL)
        plain = _carrier(workspace_id=ws, context_id=open_)
        probes = _Probes(writable={closed: False})

        await probes.run(
            {unwritable.id: unwritable, plain.id: plain},
            {unwritable.id: uuid4(), plain.id: uuid4()},
        )

        assert probes.author.await_count == 0
