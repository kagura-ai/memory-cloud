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
4. #3019: recency is the later of ``created_at`` and the last text edit the
   revision log records; ``updated_at`` is never read, so a row that Sleep (or
   any other bookkeeping writer) stamped without an edit does not outrank a
   newer one.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from services.memory_versions import (
    TextEditEvent,
    TextEditTimes,
    last_text_edit_at,
    text_edit_times,
)
from services.sleep.dedup_merge import (
    AUTO_MERGE_THRESHOLD,
    DedupMergePhase,
    _fmt_minute,
    _MergeTally,
)
from services.sleep.reporter import SleepBudget


def _phase() -> DedupMergePhase:
    return DedupMergePhase(MagicMock(), MagicMock())


def _mem(
    *,
    created: datetime,
    updated: datetime | None = None,
    source_type: str = "api",
    importance: float = 0.5,
    version: Any = 1,
    summary: str = "a memory",
) -> MagicMock:
    m = MagicMock()
    m.id = uuid4()
    m.created_at = created
    m.updated_at = updated
    m.source_type = source_type
    m.importance = importance
    m.version = version
    m.summary = summary
    m.type = "note"
    m.tags = []
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


# ---------------------------------------------------------------------------
# #3019: recency is the creation or the last text edit, never updated_at
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 11, 27, 12, 0)


def _rev(
    memory_id: UUID,
    version: int,
    *,
    at: datetime,
    writer: str = "api",
    text: bool = True,
    restored_from: str | None = None,
) -> SimpleNamespace:
    """One row of the revision read, as Postgres returns it (``restored_from``
    is the ``->>`` text of the JSON number)."""
    return SimpleNamespace(
        memory_id=memory_id,
        version=version,
        writer=writer,
        at=at,
        text=text,
        restored_from=restored_from,
    )


_UNREAD: Any = object()


def _with_revisions(
    phase: DedupMergePhase,
    rows=(),
    error: Exception | None = None,
    *,
    log_began: Any = _UNREAD,
    log_row: datetime | None = None,
) -> MagicMock:
    """Give ``phase`` a session whose revision read returns ``rows``.

    ``log_began``: when the revision log began, as if already read this run
    (no query is made for it); left out, the log start query is answered by
    ``log_row`` (None: the log has no row).
    """
    db = MagicMock()
    savepoint = MagicMock()
    savepoint.__aenter__ = AsyncMock(return_value=None)
    savepoint.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=savepoint)
    result = MagicMock()
    result.all.return_value = list(rows)
    result.scalar.return_value = log_row
    db.execute = AsyncMock(side_effect=error) if error else AsyncMock(return_value=result)
    phase.db = db
    if log_began is not _UNREAD:
        phase._log_began_at = log_began
        phase._log_began_read = True
    return db


def _config() -> MagicMock:
    config = MagicMock()
    config.sleep_llm_provider = "openai"
    config.sleep_llm_model = "m"
    return config


def _judge_picks(phase: DedupMergePhase, winner_label: str) -> None:
    _judge_answers(phase, [{"pair": ["A", "B"], "verdict": "merge", "winner": winner_label}])


def _judge_answers(phase: DedupMergePhase, judgments: list[dict[str, Any]]) -> None:
    response = MagicMock()
    response.parsed = {"judgments": judgments}
    response.total_tokens = 10
    response.input_tokens = 6
    response.output_tokens = 4
    response.cached_input_tokens = 0
    response.cache_write_tokens = 0
    response.provider = "openai"
    response.model = "m"
    response.tokenizer_version = "v1"
    phase.llm_service.complete_json = AsyncMock(return_value=response)


def _key(a: MagicMock, b: MagicMock) -> tuple[UUID, UUID]:
    x, y = sorted([a.id, b.id], key=str)
    return x, y


async def _judge(phase: DedupMergePhase, a: MagicMock, b: MagicMock, winner_label: str):
    """Load the edit times, then judge the cluster ``[a, b]`` (labels A, B)."""
    _judge_picks(phase, winner_label)
    await phase._load_text_edit_times([a, b])
    return await phase._judge_cluster(
        [a, b], {_key(a, b): 0.96}, True, "u", "ctx", "ws", SleepBudget(), _config()
    )


def _prompt(phase: DedupMergePhase) -> str:
    return phase.llm_service.complete_json.call_args.kwargs["prompt"]


def _log_began_before_b() -> datetime:
    """The revision log began three months ago, before B was written."""
    return _now() - timedelta(days=90)


class TestRecencyIgnoresUpdatedAt:
    """The scenario of the owner decision (2026-11-27): A is six months old and
    was stamped last week without an edit; B was written two months ago and
    never stamped (``updated_at`` NULL). The revision log began before B was
    written, so an edit of A after B would be in it: none is, A's stamp is no
    edit, and B holds the newer fact."""

    def _pair(self) -> tuple[MagicMock, MagicMock]:
        now = _now()
        a = _mem(created=now - timedelta(days=180), updated=now - timedelta(days=7))
        b = _mem(created=now - timedelta(days=60), updated=None)
        return a, b

    async def test_judge_picking_the_stamped_old_row_is_flipped(self) -> None:
        phase = _phase()
        db = _with_revisions(phase, log_began=_log_began_before_b())
        a, b = self._pair()

        decisions = await _judge(phase, a, b, "A")

        assert decisions == [(b.id, a.id)]
        assert phase._override_reasons == {"recency": 1}
        db.execute.assert_not_called()

    async def test_judge_picking_the_newer_row_is_kept(self) -> None:
        phase = _phase()
        _with_revisions(phase, log_began=_log_began_before_b())
        a, b = self._pair()

        decisions = await _judge(phase, a, b, "B")

        assert decisions == [(b.id, a.id)]
        assert phase._winner_overrides == 0

    async def test_rule_path_at_equal_importance_keeps_the_newer_row(self) -> None:
        phase = _phase()
        db = _with_revisions(phase, log_began=_log_began_before_b())
        a, b = self._pair()

        await phase._load_text_edit_times([a, b])
        decisions = phase._rule_based_judge([a, b], {_key(a, b): 0.99})

        assert decisions == [(b.id, a.id)]
        db.execute.assert_not_called()

    async def test_prompt_shows_the_creation_minute_of_the_stamped_row(self) -> None:
        phase = _phase()
        _with_revisions(phase, log_began=_log_began_before_b())
        a, b = self._pair()

        await _judge(phase, a, b, "B")

        prompt = _prompt(phase)
        assert f"last_updated={_fmt_minute(a.created_at)}," in prompt
        assert f"last_updated={_fmt_minute(a.updated_at)}," not in prompt
        assert f"last_updated={_fmt_minute(b.created_at)}," in prompt

    async def test_an_earlier_merge_winner_loses_to_a_later_created_row(self) -> None:
        """A merge winner's tag union stamps ``updated_at`` without a version."""
        now = _now()
        phase = _phase()
        _with_revisions(phase, log_began=_log_began_before_b())
        winner_before = _mem(created=now - timedelta(days=100), updated=now - timedelta(days=1))
        later = _mem(created=now - timedelta(days=50))

        decisions = await _judge(phase, winner_before, later, "A")

        assert decisions == [(later.id, winner_before.id)]
        assert phase._override_reasons == {"recency": 1}

    async def test_audit_recency_equals_what_the_judge_was_shown(self) -> None:
        phase = _phase()
        now = _now()
        a = _mem(created=now - timedelta(days=180), updated=now - timedelta(days=7), version=2)
        b = _mem(created=now - timedelta(days=60))
        _with_revisions(phase, [_rev(a.id, 2, at=now - timedelta(days=30, minutes=7))])

        decisions = await _judge(phase, a, b, "A")
        jobs = phase._plan_cluster_merges(
            decisions,
            {a.id: a, b.id: b},
            {_key(a, b): 0.96},
            set(),
            0.92,
            _MergeTally(),
            snapshot_audit=True,
            exempt_tags=frozenset(),
        )

        audit = jobs[0][4]
        assert audit is not None
        prompt = _prompt(phase)
        assert f"last_updated={audit['winner_recency']}," in prompt
        assert f"last_updated={audit['loser_recency']}," in prompt
        assert audit["winner_recency"] == _fmt_minute(now - timedelta(days=30, minutes=7))
        assert audit["loser_recency"] == _fmt_minute(b.created_at)


class TestTextEditTimes:
    """Which revisions make a memory newer than its creation."""

    def _b(self) -> MagicMock:
        return _mem(created=_now() - timedelta(days=60))

    async def test_a_text_edit_after_the_duplicate_was_written_wins(self) -> None:
        """#1198 kept: a real in-place edit is the memory's recency."""
        now = _now()
        phase = _phase()
        a = _mem(created=now - timedelta(days=180), updated=now - timedelta(days=7), version=3)
        b = self._b()
        _with_revisions(
            phase,
            [
                _rev(a.id, 2, at=now - timedelta(days=30)),
                _rev(a.id, 3, at=now - timedelta(days=7), text=False),
            ],
        )

        decisions = await _judge(phase, a, b, "B")

        assert decisions == [(a.id, b.id)]
        assert phase._override_reasons == {"recency": 1}
        assert phase._recency_key(a) == now - timedelta(days=30)

    async def test_an_edit_of_metadata_only_is_not_recency(self) -> None:
        now = _now()
        phase = _phase()
        a = _mem(created=now - timedelta(days=180), updated=now - timedelta(days=7), version=2)
        b = self._b()
        _with_revisions(
            phase,
            [_rev(a.id, 2, at=now - timedelta(days=7), text=False)],
            log_began=_log_began_before_b(),
        )

        decisions = await _judge(phase, a, b, "A")

        assert decisions == [(b.id, a.id)]
        assert phase._recency_key(a) == a.created_at

    async def test_an_old_text_edit_counts_at_its_own_time(self) -> None:
        now = _now()
        phase = _phase()
        a = _mem(created=now - timedelta(days=180), updated=now - timedelta(days=7), version=3)
        b = self._b()
        _with_revisions(
            phase,
            [
                _rev(a.id, 2, at=now - timedelta(days=150)),
                _rev(a.id, 3, at=now - timedelta(days=7), text=False),
            ],
        )

        decisions = await _judge(phase, a, b, "A")

        assert phase._recency_key(a) == now - timedelta(days=150)
        assert decisions == [(b.id, a.id)]

    @pytest.mark.parametrize(
        ("revisions", "expected_days_ago"),
        [
            # the rollback reverted the text edit: the text is the creation's
            ([(2, "api", 30, True, None), (3, "rollback", 20, True, "2")], 180),
            # an edit after the rollback counts
            (
                [
                    (2, "api", 30, True, None),
                    (3, "rollback", 20, True, "2"),
                    (4, "api", 10, True, None),
                ],
                10,
            ),
            # the rollback reverted only what came after the text edit
            (
                [
                    (2, "api", 30, True, None),
                    (3, "api", 25, False, None),
                    (4, "rollback", 20, False, "3"),
                ],
                30,
            ),
            # a rollback that names no block reverts every earlier edit
            ([(2, "api", 30, True, None), (3, "rollback", 20, True, None)], 180),
            # one that names no block and changed no text left the text as it was
            ([(2, "api", 30, True, None), (3, "rollback", 20, False, None)], 30),
            ([(2, "api", 30, True, None), (3, "rollback", 20, False, "x")], 30),
            # an unreadable block reads as no block
            ([(2, "api", 30, True, None), (3, "rollback", 20, True, "x")], 180),
            # an erasure's details revert is never an edit of the text
            ([(2, "erasure", 5, True, "1")], 180),
            ([(2, "api", 30, True, None), (3, "erasure", 5, False, "2")], 30),
            # a later version but no revision row at all (a legacy row)
            ([], 180),
        ],
    )
    async def test_the_walk_over_the_log(self, revisions, expected_days_ago) -> None:
        now = _now()
        phase = _phase()
        top = max([v for v, *_ in revisions], default=2)
        a = _mem(created=now - timedelta(days=180), updated=now - timedelta(days=1), version=top)
        _with_revisions(
            phase,
            [
                _rev(a.id, v, writer=w, at=now - timedelta(days=d), text=t, restored_from=r)
                for v, w, d, t, r in revisions
            ],
        )

        await phase._load_text_edit_times([a, self._b()])

        assert phase._recency_key(a) == now - timedelta(days=expected_days_ago)

    @pytest.mark.parametrize(
        ("revisions", "last_days_ago", "unsure_days_ago"),
        [
            # a block-less rollback that changed the text: which edit's text it
            # put back is unknown, the latest it may be is the last one dropped
            ([(2, "api", 30, True, None), (3, "rollback", 20, True, None)], None, 30),
            (
                [
                    (2, "api", 40, True, None),
                    (3, "api", 30, True, None),
                    (4, "rollback", 20, True, None),
                ],
                None,
                30,
            ),
            # an edit after it writes the text again: no doubt left
            (
                [
                    (2, "api", 30, True, None),
                    (3, "rollback", 20, True, None),
                    (4, "api", 10, True, None),
                ],
                10,
                None,
            ),
            # it changed no text: the edit stays, no doubt
            ([(2, "api", 30, True, None), (3, "rollback", 20, False, None)], 30, None),
            # a rollback that names its block leaves no doubt
            ([(2, "api", 30, True, None), (3, "rollback", 20, True, 2)], None, None),
            ([], None, None),
        ],
    )
    def test_the_doubt_a_rollback_without_a_block_leaves(
        self, revisions, last_days_ago, unsure_days_ago
    ) -> None:
        now = _now()
        times = text_edit_times(
            TextEditEvent(version=v, writer=w, at=now - timedelta(days=d), text=t, restored_from=r)
            for v, w, d, t, r in revisions
        )

        def ago(days: int | None) -> datetime | None:
            return None if days is None else now - timedelta(days=days)

        assert times == TextEditTimes(last=ago(last_days_ago), unsure_until=ago(unsure_days_ago))
        assert last_text_edit_at(
            TextEditEvent(version=v, writer=w, at=now - timedelta(days=d), text=t, restored_from=r)
            for v, w, d, t, r in revisions
        ) == ago(last_days_ago)

    @pytest.mark.parametrize("version", [MagicMock(), True, None, "3"])
    async def test_a_version_that_is_not_an_int_reads_nothing(self, version) -> None:
        now = _now()
        phase = _phase()
        a = _mem(created=now - timedelta(days=180), updated=now, version=version)
        db = _with_revisions(phase, [_rev(a.id, 2, at=now)])

        await phase._load_text_edit_times([a, self._b()])

        db.execute.assert_not_called()
        assert phase._recency_key(a) == a.created_at

    async def test_one_read_per_cluster_and_each_id_once_a_run(self) -> None:
        now = _now()
        phase = _phase()
        members = [_mem(created=now - timedelta(days=100 + i), version=2) for i in range(3)]
        db = _with_revisions(
            phase, [_rev(m.id, 2, at=now - timedelta(days=i)) for i, m in enumerate(members)]
        )

        await phase._load_text_edit_times(members)
        assert db.execute.await_count == 1
        assert [phase._recency_key(m) for m in members] == [
            now - timedelta(days=i) for i in range(3)
        ]

        await phase._load_text_edit_times(members)
        assert db.execute.await_count == 1

        newcomer = _mem(created=now - timedelta(days=90), version=2)
        await phase._load_text_edit_times([members[0], newcomer])
        assert db.execute.await_count == 2

    @pytest.mark.parametrize(
        ("stamp_days_ago", "kept"),
        [(0, True), (100, False)],
        ids=["stamped_after_b", "stamped_before_b"],
    )
    async def test_a_read_that_fails_leaves_the_creation_time(self, stamp_days_ago, kept) -> None:
        """The member is compared by its creation time. Its logged edits are
        unknown, so when its stamp is later than B the log cannot tell whether
        it is an edit: both are kept, however early the log began."""
        now = _now()
        phase = _phase()
        a = _mem(
            created=now - timedelta(days=180),
            updated=now - timedelta(days=stamp_days_ago),
            version=2,
        )
        b = self._b()
        db = _with_revisions(
            phase, error=RuntimeError("statement timeout"), log_began=_log_began_before_b()
        )

        decisions = await _judge(phase, a, b, "A")

        assert db.execute.await_count == 1
        assert phase._recency_key(a) == a.created_at
        assert decisions == ([] if kept else [(b.id, a.id)])
        assert phase._ambiguous_pairs == (1 if kept else 0)

    async def test_the_cache_is_per_run(self) -> None:
        """``execute`` starts every run with no edit time known."""
        phase = _phase()
        phase._text_edited_at = {uuid4(): _now()}
        phase.llm_service = MagicMock()
        config = MagicMock()
        config.sleep_dedup_enabled = False

        await phase.execute(config, "u", "ws", "ctx", SleepBudget())

        assert phase._text_edited_at == {}

    def test_a_phase_built_without_init_reads_creation_time(self) -> None:
        """Several tests build the phase with ``__new__``."""
        phase = DedupMergePhase.__new__(DedupMergePhase)
        a = _mem(created=datetime(2026, 1, 1), updated=datetime(2026, 6, 1), version=2)

        assert phase._recency_key(a) == datetime(2026, 1, 1)


# ---------------------------------------------------------------------------
# #3019, owner decision 2026-11-27: keep both when the log cannot tell
# ---------------------------------------------------------------------------


def _days_ago(days: float) -> datetime:
    return _now() - timedelta(days=days)


class TestKeepBothWhenTheLogCannotTell:
    """A: six months old, version 1, stamped last week. B: two months old,
    never stamped. A's stamp is a text edit or Sleep bookkeeping; when the
    revision log cannot tell which, and an edit then would make A the newer
    row, the pair is not merged in either direction and counted apart."""

    def _pair(self, **a_fields: Any) -> tuple[MagicMock, MagicMock]:
        fields: dict[str, Any] = {"created": _days_ago(180), "updated": _days_ago(7)}
        fields.update(a_fields)
        return _mem(**fields), _mem(created=_days_ago(60))

    @pytest.mark.parametrize("log_began", [None, 30], ids=["no_log", "log_began_after_b"])
    @pytest.mark.parametrize("pick", ["A", "B"])
    async def test_the_judge_path_keeps_both(self, log_began, pick) -> None:
        phase = _phase()
        _with_revisions(phase, log_began=None if log_began is None else _days_ago(log_began))
        a, b = self._pair()

        decisions = await _judge(phase, a, b, pick)

        assert decisions == []
        assert phase._ambiguous_pairs == 1
        assert phase._decision_meta == {}

    async def test_the_rule_path_keeps_both_at_equal_importance(self) -> None:
        phase = _phase()
        _with_revisions(phase, log_began=None)
        a, b = self._pair()

        await phase._load_text_edit_times([a, b])
        decisions = await phase._judge_cluster(
            [a, b], {_key(a, b): 0.99}, False, "u", "ctx", "ws", SleepBudget(), _config()
        )

        assert decisions == []
        assert phase._ambiguous_pairs == 1

    async def test_the_rule_path_merges_when_importance_decides(self) -> None:
        """An edit of A would not change a decision made on importance."""
        phase = _phase()
        _with_revisions(phase, log_began=None)
        a, b = self._pair(importance=0.3)
        b.importance = 0.9

        await phase._load_text_edit_times([a, b])
        decisions = await phase._judge_cluster(
            [a, b], {_key(a, b): 0.99}, False, "u", "ctx", "ws", SleepBudget(), _config()
        )

        assert decisions == [(b.id, a.id)]
        assert phase._ambiguous_pairs == 0

    async def test_a_log_that_began_before_the_duplicate_tells(self) -> None:
        phase = _phase()
        _with_revisions(phase, log_began=_days_ago(90))
        a, b = self._pair()

        assert await _judge(phase, a, b, "A") == [(b.id, a.id)]
        assert phase._ambiguous_pairs == 0

    async def test_a_logged_text_edit_leaves_no_doubt(self) -> None:
        """The log shows the edit that wrote A's text; anything it does not
        show is older than that edit."""
        phase = _phase()
        a, b = self._pair(version=2)
        _with_revisions(phase, [_rev(a.id, 2, at=_days_ago(100))], log_began=None)

        assert await _judge(phase, a, b, "A") == [(b.id, a.id)]
        assert phase._ambiguous_pairs == 0

    @pytest.mark.parametrize(("log_began", "kept"), [(None, True), (30, True), (90, False)])
    async def test_a_metadata_edit_leaves_the_doubt(self, log_began, kept) -> None:
        phase = _phase()
        a, b = self._pair(version=2)
        _with_revisions(
            phase,
            [_rev(a.id, 2, at=_days_ago(7), text=False)],
            log_began=None if log_began is None else _days_ago(log_began),
        )

        decisions = await _judge(phase, a, b, "A")

        assert decisions == ([] if kept else [(b.id, a.id)])

    @pytest.mark.parametrize(("b_days_ago", "kept"), [(60, True), (10, False)])
    async def test_a_rollback_without_a_block_leaves_the_doubt(self, b_days_ago, kept) -> None:
        """It put back the text of some earlier version, which may be the edit
        it dropped (30 days ago): later than B written 60 days ago, earlier
        than B written 10 days ago. The log began a year ago."""
        phase = _phase()
        a = _mem(created=_days_ago(180), updated=_days_ago(20), version=3)
        b = _mem(created=_days_ago(b_days_ago))
        _with_revisions(
            phase,
            [
                _rev(a.id, 2, at=_days_ago(30)),
                _rev(a.id, 3, at=_days_ago(20), writer="rollback"),
            ],
            log_began=_days_ago(365),
        )

        decisions = await _judge(phase, a, b, "A")

        assert phase._recency_key(a) == a.created_at
        assert decisions == ([] if kept else [(b.id, a.id)])

    @pytest.mark.parametrize(
        "stamp",
        [_days_ago(70), _days_ago(60) + timedelta(seconds=30)],
        ids=["before_b", "in_b_minute"],
    )
    async def test_a_stamp_not_later_than_the_winner_needs_no_log(self, stamp) -> None:
        phase = _phase()
        db = _with_revisions(phase)
        a, b = self._pair(updated=stamp)

        assert await _judge(phase, a, b, "A") == [(b.id, a.id)]
        db.execute.assert_not_called()

    async def test_the_log_start_is_read_once_a_run_and_only_when_needed(self) -> None:
        phase = _phase()
        db = _with_revisions(phase, log_row=_days_ago(90))
        a1, b1 = self._pair()
        a2, b2 = self._pair()

        assert await _judge(phase, a1, b1, "A") == [(b1.id, a1.id)]
        assert await _judge(phase, a2, b2, "B") == [(b2.id, a2.id)]

        assert db.execute.await_count == 1
        assert phase._log_began_at == _days_ago(90)

    @pytest.mark.parametrize("failure", [True, False], ids=["read_failed", "no_row"])
    async def test_no_log_start_keeps_both(self, failure) -> None:
        phase = _phase()
        db = _with_revisions(
            phase, error=RuntimeError("statement timeout") if failure else None, log_row=None
        )
        a, b = self._pair()

        assert await _judge(phase, a, b, "A") == []
        assert db.execute.await_count == 1
        assert phase._log_began_read is True and phase._log_began_at is None

    async def test_a_kept_row_is_never_absorbed_through_a_third(self) -> None:
        """W (100 days) cannot take L (180 days, stamped last week): kept. C
        (5 days) is newer than any edit L can have had, so L may go to C, and
        W goes to C too; nothing leaves L under W."""
        phase = _phase()
        _with_revisions(phase, log_began=None)
        w = _mem(created=_days_ago(100))
        loser = _mem(created=_days_ago(180), updated=_days_ago(7))
        c = _mem(created=_days_ago(5))
        _judge_answers(
            phase,
            [
                {"pair": ["A", "B"], "verdict": "merge", "winner": "A"},
                {"pair": ["A", "C"], "verdict": "merge", "winner": "A"},
                {"pair": ["B", "C"], "verdict": "merge", "winner": "B"},
            ],
        )
        scores = {_key(w, loser): 0.96, _key(w, c): 0.96, _key(loser, c): 0.96}
        await phase._load_text_edit_times([w, loser, c])

        decisions = await phase._judge_cluster(
            [w, loser, c], scores, True, "u", "ctx", "ws", SleepBudget(), _config()
        )

        assert decisions == [(c.id, w.id), (c.id, loser.id)]
        assert phase._ambiguous_pairs == 1

    async def test_the_rest_of_a_cluster_still_merges(self) -> None:
        phase = _phase()
        _with_revisions(phase, log_began=None)
        w = _mem(created=_days_ago(100))
        loser = _mem(created=_days_ago(180), updated=_days_ago(7))
        older = _mem(created=_days_ago(150))
        _judge_answers(
            phase,
            [
                {"pair": ["A", "B"], "verdict": "merge", "winner": "A"},
                {"pair": ["A", "C"], "verdict": "merge", "winner": "A"},
            ],
        )
        scores = {_key(w, loser): 0.96, _key(w, older): 0.96, _key(loser, older): 0.5}
        await phase._load_text_edit_times([w, loser, older])

        decisions = await phase._judge_cluster(
            [w, loser, older], scores, True, "u", "ctx", "ws", SleepBudget(), _config()
        )

        assert decisions == [(w.id, older.id)]
        assert phase._ambiguous_pairs == 1


def _run_config(**overrides: Any) -> Any:
    from neural.config import NeuralMemoryConfig

    values: dict[str, Any] = {
        "sleep_llm_provider": "openai",
        "sleep_llm_model": "m",
        "sleep_dedup_enabled": True,
        "sleep_max_memories_per_run": 50,
        "sleep_dedup_similarity_threshold": 0.92,
        "sleep_dedup_supersede_enabled": False,
    }
    values.update(overrides)
    return NeuralMemoryConfig(**values)


class TestKeepBothInARun:
    """``execute``: a cluster in which no judge answer could merge anything is
    not sent to the judge; the pair is counted under its own reason."""

    def _phase(self, memories, pairs, *, log_row: datetime | None) -> DedupMergePhase:
        phase = _phase()
        _with_revisions(phase, log_row=log_row)
        phase._fetch_active_memories = AsyncMock(return_value=memories)
        phase._find_similar_pairs = AsyncMock(return_value=pairs)
        phase._count_pinned = AsyncMock(return_value=0)
        phase._execute_merge = AsyncMock(return_value=True)
        phase._execute_shadow_merge = AsyncMock(return_value=(True, None))
        _judge_picks(phase, "A")
        return phase

    @pytest.mark.parametrize("shadow", [False, True], ids=["remove", "shadow"])
    async def test_a_cluster_nothing_can_merge_spends_no_call(self, shadow) -> None:
        a = _mem(created=_days_ago(180), updated=_days_ago(7))
        b = _mem(created=_days_ago(60))
        phase = self._phase([a, b], [(a.id, b.id, 0.96)], log_row=None)

        result = await phase.execute(
            _run_config(sleep_dedup_supersede_enabled=shadow), "u", "ws", "ctx", SleepBudget()
        )

        phase.llm_service.complete_json.assert_not_awaited()
        phase._execute_merge.assert_not_awaited()
        phase._execute_shadow_merge.assert_not_awaited()
        assert result.llm_calls_used == 0
        assert result.details["merged"] == 0
        assert result.details["no_action_reasons"] == {"ambiguous_recency": 1}
        assert result.details["budget_cut"] is False

    async def test_the_same_pair_merges_once_the_log_tells(self) -> None:
        a = _mem(created=_days_ago(180), updated=_days_ago(7))
        b = _mem(created=_days_ago(60))
        phase = self._phase([a, b], [(a.id, b.id, 0.96)], log_row=_days_ago(90))

        result = await phase.execute(_run_config(), "u", "ws", "ctx", SleepBudget())

        phase.llm_service.complete_json.assert_awaited_once()
        phase._execute_merge.assert_awaited_once()
        assert phase._execute_merge.await_args.args[:2] == (b, a)
        assert result.details["merged"] == 1
        assert "ambiguous_recency" not in result.details["no_action_reasons"]

    async def test_a_cluster_with_a_pair_that_can_merge_is_judged(self) -> None:
        """The judge picks the kept row's pair and a mergeable one; only the
        mergeable one runs, and the cluster is not counted as kept-both."""
        w = _mem(created=_days_ago(100))
        loser = _mem(created=_days_ago(180), updated=_days_ago(7))
        older = _mem(created=_days_ago(150))
        phase = self._phase(
            [w, loser, older],
            [(w.id, loser.id, 0.96), (w.id, older.id, 0.96)],
            log_row=None,
        )
        _judge_answers(
            phase,
            [
                {"pair": ["A", "B"], "verdict": "merge", "winner": "A"},
                {"pair": ["A", "C"], "verdict": "merge", "winner": "A"},
            ],
        )

        result = await phase.execute(_run_config(), "u", "ws", "ctx", SleepBudget())

        phase.llm_service.complete_json.assert_awaited_once()
        phase._execute_merge.assert_awaited_once()
        assert phase._execute_merge.await_args.args[:2] == (w, older)
        assert result.details["no_action_reasons"] == {"ambiguous_recency": 1}

    async def test_the_rule_path_counts_the_kept_pair(self) -> None:
        a = _mem(created=_days_ago(180), updated=_days_ago(7))
        b = _mem(created=_days_ago(60))
        phase = self._phase([a, b], [(a.id, b.id, 0.99)], log_row=None)

        result = await phase.execute(
            _run_config(sleep_llm_provider=""), "u", "ws", "ctx", SleepBudget()
        )

        phase._execute_merge.assert_not_awaited()
        assert result.details["no_action_reasons"] == {"ambiguous_recency": 1}
