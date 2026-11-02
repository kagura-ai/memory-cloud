"""Whether the caller may accept a supersede suggestion (#2427).

The pure half of ``services.supersede_acceptance``: the reason precedence over
a pair's ownership and order, and the wire shape. The database half (the
ownership query, the write probe, reference/PATCH/MCP) is in
``tests/integration/test_2427_supersede_acceptance_db.py``.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from models.schemas import SupersedeAcceptance, SupersedeCandidate
from services.supersede_acceptance import PairOwnership, acceptance_reason

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

    def test_an_unknown_reason_is_rejected(self):
        with pytest.raises(ValueError):
            SupersedeAcceptance(allowed=False, reason="target_not_owner")  # type: ignore[arg-type]

    def test_a_candidate_without_acceptance_keeps_the_recall_shape(self):
        """Recall never sets it, so exclude_none keeps the key off the wire."""
        candidate = SupersedeCandidate(memory_id=uuid4(), summary="s", similarity=0.9)
        assert "acceptance" not in candidate.model_dump(exclude_none=True)
