"""Whether the caller may accept a supersede suggestion (#2427).

The pure half of ``services.supersede_acceptance``: the reason precedence over
a pair's ownership and order, the edge_exists predicate and the order
``evaluate`` reports in (#2518), the guardrail-carrier gate (#2614), and the
wire shape. The database half (the ownership query, the write probe,
reference/PATCH/MCP) is in
``tests/integration/test_2427_supersede_acceptance_db.py``.
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
    PairOwnership,
    acceptance_reason,
    conflicting_declared_edge,
    evaluate,
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


class TestConflictingDeclaredEdge:
    """#2518: the edge writer's duplicate contract, read before the click."""

    async def _conflicts(
        self, *rows: SimpleNamespace, owner: str = "owner"
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
    async def test_the_rows_are_read_under_the_one_owner(self):
        """#2614: the account the accept writes under, and no other."""
        _, listed = await self._conflicts(owner="author")
        assert listed.await_args.args[0] == "author"

    @pytest.mark.asyncio
    async def test_any_conflicting_row_refuses_not_only_the_first(self):
        clean = _edge()
        weaker = _edge(edge_type="related_to", weight=0.5)
        assert (await self._conflicts(clean, weaker))[0] is True

    @pytest.mark.asyncio
    async def test_clean_and_automatic_rows_together_do_not_conflict(self):
        rows = (_edge(), _edge(origin="semantic", edge_type="related_to", weight=0.3))
        assert (await self._conflicts(*rows))[0] is False


class TestEdgeOwner:
    """#2614 (the #2549 owner question): the probe reads the carrier author's
    row alone. Both transports write the accept under that author since #2600,
    so a row kept by the caller, linked or not, is never one the accept meets."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caller_id", ["author", "linked_caller"])
    async def test_the_edge_is_read_under_the_carrier_author_alone(self, caller_id):
        memory = SimpleNamespace(
            id=uuid4(), user_id="author", workspace_id=uuid4(), context_id=uuid4(), details=None
        )
        probe_edge = AsyncMock(return_value=False)
        with (
            patch("services.supersede_acceptance.can_write_edges", AsyncMock(return_value=True)),
            patch("services.supersede_acceptance.pair_ownership", AsyncMock(return_value=_pair())),
            patch("services.supersede_acceptance.conflicting_declared_edge", probe_edge),
        ):
            await evaluate(MagicMock(), user_id=caller_id, memory=memory, target_id=uuid4())
        assert probe_edge.await_args.kwargs["owner"] == "author"


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


class TestEvaluatePrecedence:
    """``not_writable`` > ``not_owner`` > ``newer_candidate`` > ``edge_exists``;
    a later check runs only when the earlier ones pass. A guardrail carrier's
    author gate (#2614) is part of ``not_writable``."""

    async def _evaluate(
        self,
        *,
        writable: bool,
        pair: PairOwnership,
        edge: bool,
        details: Any = None,
        may_author: bool = True,
    ):
        memory = SimpleNamespace(
            id=uuid4(), user_id="owner", workspace_id=uuid4(), context_id=uuid4(), details=details
        )
        probe_pair = AsyncMock(return_value=pair)
        probe_edge = AsyncMock(return_value=edge)
        self.probe_author = AsyncMock(return_value=may_author)
        with (
            patch(
                "services.supersede_acceptance.can_write_edges", AsyncMock(return_value=writable)
            ),
            patch("services.supersede_acceptance.may_author_guardrail", self.probe_author),
            patch("services.supersede_acceptance.pair_ownership", probe_pair),
            patch("services.supersede_acceptance.conflicting_declared_edge", probe_edge),
        ):
            result = await evaluate(MagicMock(), user_id="caller", memory=memory, target_id=uuid4())
        self.memory = memory
        return result, probe_pair, probe_edge

    @pytest.mark.asyncio
    async def test_not_writable_wins_and_reads_nothing_else(self):
        result, probe_pair, probe_edge = await self._evaluate(
            writable=False, pair=_pair(carrier_own=False), edge=True, details=_GUARDRAIL
        )
        assert result.reason == "not_writable"
        assert probe_pair.await_count == 0 and probe_edge.await_count == 0
        assert self.probe_author.await_count == 0

    @pytest.mark.asyncio
    async def test_a_guardrail_carrier_below_editor_is_not_writable_before_the_pair(self):
        """#2614: the gate both accept transports run before the accept."""
        result, probe_pair, probe_edge = await self._evaluate(
            writable=True,
            pair=_pair(carrier_own=False),
            edge=True,
            details=_GUARDRAIL,
            may_author=False,
        )
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": "not_writable"}
        assert probe_pair.await_count == 0 and probe_edge.await_count == 0
        assert self.probe_author.await_args.kwargs == {
            "user_id": "caller",
            "context_id": self.memory.context_id,
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
        result, _, _ = await self._evaluate(
            writable=True, pair=pair, edge=edge, details=_GUARDRAIL, may_author=True
        )
        assert result.model_dump(exclude_none=True) == expected
        assert self.probe_author.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "details",
        [None, {"note": "x"}, {"tool_trigger": None}],
        ids=["none", "plain", "null_marking"],
    )
    async def test_a_plain_carrier_is_not_probed_for_the_guardrail_gate(self, details):
        result, _, _ = await self._evaluate(
            writable=True, pair=_pair(), edge=False, details=details, may_author=False
        )
        assert result.model_dump(exclude_none=True) == {"allowed": True}
        assert self.probe_author.await_count == 0

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
        """#2614: the edge is looked up under the carrier's author alone."""
        result, _, probe_edge = await self._evaluate(writable=True, pair=_pair(), edge=True)
        assert result.model_dump(exclude_none=True) == {"allowed": False, "reason": "edge_exists"}
        assert probe_edge.await_args.kwargs["owner"] == "owner"

    @pytest.mark.asyncio
    async def test_allowed_when_nothing_refuses(self):
        result, _, _ = await self._evaluate(writable=True, pair=_pair(), edge=False)
        assert result.model_dump(exclude_none=True) == {"allowed": True}

    def test_a_candidate_without_acceptance_keeps_the_recall_shape(self):
        """Recall never sets it, so exclude_none keeps the key off the wire."""
        candidate = SupersedeCandidate(memory_id=uuid4(), summary="s", similarity=0.9)
        assert "acceptance" not in candidate.model_dump(exclude_none=True)
