"""#1209: deterministic winner rules + merge-audit metadata on dedup merges.

Pins, on top of the #1195/#1198 newer-wins veto:

1. The equal-recency source tie-break — a non-manual (ingested) winner never
   deletes a manual (human-authored, #887 provenance) loser when the recency
   keys are EQUAL. Recency stays primary: the source rule never fires when
   one side is strictly newer.
2. Per-reason override counters ("recency" | "source") that feed the sleep
   report's ``winner_override_reasons`` breakdown.
3. Decision audit metadata: the judge's ``reason`` (already in the JSON
   schema, previously discarded) is captured leniently; the rule-based path
   records a rule id; overrides re-key the metadata to the final decision.
"""

from datetime import datetime
from unittest.mock import MagicMock
from uuid import uuid4

from services.sleep.dedup_merge import AUTO_MERGE_THRESHOLD, DedupMergePhase


def _phase() -> DedupMergePhase:
    return DedupMergePhase(MagicMock(), MagicMock())


def _mem(
    *,
    created: datetime,
    updated: datetime | None = None,
    source_type: str = "api",
    importance: float = 0.5,
) -> MagicMock:
    m = MagicMock()
    m.id = uuid4()
    m.created_at = created
    m.updated_at = updated
    m.source_type = source_type
    m.importance = importance
    return m


class TestWinnerRules:
    def test_recency_flip_counts_reason(self) -> None:
        phase = _phase()
        older = _mem(created=datetime(2026, 1, 1))
        newer = _mem(created=datetime(2026, 6, 1))
        corrected = phase._enforce_winner_rules([(older.id, newer.id)], [older, newer])
        assert corrected == [(newer.id, older.id)]
        assert phase._winner_overrides == 1
        assert phase._override_reasons == {"recency": 1}
        assert phase._decision_meta[(newer.id, older.id)]["override_reason"] == "recency"

    def test_equal_recency_manual_loser_flips(self) -> None:
        """The trusted-over-ingested tie-break: at equal recency, an api
        winner never deletes a manual loser."""
        phase = _phase()
        ts = datetime(2026, 6, 1, 12, 0)
        ingested_winner = _mem(created=ts, source_type="api")
        manual_loser = _mem(created=ts, source_type="manual")
        corrected = phase._enforce_winner_rules(
            [(ingested_winner.id, manual_loser.id)], [ingested_winner, manual_loser]
        )
        assert corrected == [(manual_loser.id, ingested_winner.id)]
        assert phase._override_reasons == {"source": 1}
        meta = phase._decision_meta[(manual_loser.id, ingested_winner.id)]
        assert meta["override_reason"] == "source"

    def test_strictly_newer_ingested_winner_beats_manual_loser(self) -> None:
        """Recency stays primary — the source rule never overrides a
        strictly newer ingested version."""
        phase = _phase()
        manual_old = _mem(created=datetime(2026, 1, 1), source_type="manual")
        ingested_new = _mem(created=datetime(2026, 6, 1), source_type="url")
        corrected = phase._enforce_winner_rules(
            [(ingested_new.id, manual_old.id)], [ingested_new, manual_old]
        )
        assert corrected == [(ingested_new.id, manual_old.id)]
        assert phase._winner_overrides == 0
        assert phase._override_reasons == {}

    def test_equal_recency_manual_winner_untouched(self) -> None:
        phase = _phase()
        ts = datetime(2026, 6, 1)
        manual_winner = _mem(created=ts, source_type="manual")
        ingested_loser = _mem(created=ts, source_type="vault")
        corrected = phase._enforce_winner_rules(
            [(manual_winner.id, ingested_loser.id)], [manual_winner, ingested_loser]
        )
        assert corrected == [(manual_winner.id, ingested_loser.id)]
        assert phase._winner_overrides == 0

    def test_equal_recency_both_manual_untouched(self) -> None:
        phase = _phase()
        ts = datetime(2026, 6, 1)
        a = _mem(created=ts, source_type="manual")
        b = _mem(created=ts, source_type="manual")
        corrected = phase._enforce_winner_rules([(a.id, b.id)], [a, b])
        assert corrected == [(a.id, b.id)]
        assert phase._winner_overrides == 0

    def test_same_minute_seconds_apart_counts_as_tie(self) -> None:
        """Rules compare at MINUTE precision — the precision the judge saw.

        Two writes seconds apart look identical in the prompt's
        ``last_updated=``; sub-minute deltas are write-ordering noise, so
        they neither trigger the recency flip nor defeat the source
        tie-break (a manual loser 20s OLDER than an ingested winner still
        wins the tie).
        """
        phase = _phase()
        ingested_winner = _mem(created=datetime(2026, 6, 1, 12, 0, 30), source_type="api")
        manual_loser = _mem(created=datetime(2026, 6, 1, 12, 0, 10), source_type="manual")
        corrected = phase._enforce_winner_rules(
            [(ingested_winner.id, manual_loser.id)], [ingested_winner, manual_loser]
        )
        assert corrected == [(manual_loser.id, ingested_winner.id)]
        assert phase._override_reasons == {"source": 1}

    def test_cross_minute_delta_still_flips_on_recency(self) -> None:
        phase = _phase()
        older_winner = _mem(created=datetime(2026, 6, 1, 12, 0, 59))
        newer_loser = _mem(created=datetime(2026, 6, 1, 12, 1, 0))
        corrected = phase._enforce_winner_rules(
            [(older_winner.id, newer_loser.id)], [older_winner, newer_loser]
        )
        assert corrected == [(newer_loser.id, older_winner.id)]
        assert phase._override_reasons == {"recency": 1}


class TestDecisionMeta:
    def test_parse_captures_reason_and_confidence(self) -> None:
        phase = _phase()
        id_a, id_b = uuid4(), uuid4()
        label_to_id = {"A": id_a, "B": id_b}
        response = {
            "judgments": [
                {
                    "pair": ["A", "B"],
                    "verdict": "merge",
                    "winner": "A",
                    "confidence": 0.9,
                    "reason": "B is a subset of A",
                }
            ]
        }
        decisions = phase._parse_dedup_response(response, label_to_id)
        assert decisions == [(id_a, id_b)]
        meta = phase._decision_meta[(id_a, id_b)]
        assert meta["merge_reason"] == "B is a subset of A"
        assert meta["judge_confidence"] == 0.9

    def test_parse_missing_reason_is_lenient(self) -> None:
        """A judgment without a reason stays valid — reason parse is
        best-effort, never a validity gate."""
        phase = _phase()
        id_a, id_b = uuid4(), uuid4()
        label_to_id = {"A": id_a, "B": id_b}
        response = {"judgments": [{"pair": ["A", "B"], "verdict": "merge", "winner": "B"}]}
        decisions = phase._parse_dedup_response(response, label_to_id)
        assert decisions == [(id_b, id_a)]
        meta = phase._decision_meta[(id_b, id_a)]
        assert meta["merge_reason"] == "unspecified"
        assert meta["judge_confidence"] is None

    def test_rule_based_records_rule_reason(self) -> None:
        phase = _phase()
        ts = datetime(2026, 6, 1)
        a = _mem(created=ts, importance=0.9)
        b = _mem(created=ts, importance=0.5)
        key = tuple(sorted([a.id, b.id], key=str))
        decisions = phase._rule_based_judge([a, b], {key: 0.99})
        assert decisions == [(a.id, b.id)]
        meta = phase._decision_meta[(a.id, b.id)]
        assert meta["merge_reason"] == f"rule:cosine>={AUTO_MERGE_THRESHOLD}"
