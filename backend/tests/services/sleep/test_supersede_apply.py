"""#3020: the Sleep phase that applies judge-verified supersede suggestions.

A fake session answers the phase's three column selects (carriers, targets,
the pair's edges) from lists, records every statement and each pair's
savepoint; the locked recheck and the edge writer are patched per test. The
DB-backed counterpart is tests/integration/test_3020_sleep_supersede_apply_db.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services.sleep.coverage import FAILURE, PASSED, SETTLED, LaneEntry
from services.sleep.reporter import SleepBudget
from services.sleep.supersede_apply import SupersedeApplyPhase
from services.supersede_verify import (
    CONTENT_HEAD_CHARS,
    SUMMARY_CHARS,
    pair_fingerprint,
    text_fingerprint,
    verification_record,
)

USER = "apply-user"
WS = "6d0c9a52-63cf-4c0e-9a3b-3f0b8f9a0001"
CTX = "6d0c9a52-63cf-4c0e-9a3b-3f0b8f9a0002"
T0 = datetime(2026, 1, 1)
REPORT_ID = UUID("6d0c9a52-63cf-4c0e-9a3b-3f0b8f9a0003")


def _record(older, new_summary, new_head, *, verdict="supersedes", confidence=0.9):
    return verification_record(
        verdict,
        confidence,
        "the newer states the current value",
        model="m",
        min_confidence=0.7,
        fingerprint=pair_fingerprint(
            SimpleNamespace(summary=older.summary, content=older.content_head),
            SimpleNamespace(summary=new_summary, content=new_head),
        ),
        old_fingerprint=text_fingerprint(older.summary, older.content_head),
        at="2026-01-01T00:00:00Z",
    )


def _pair(i: int, *, verdict="supersedes", confidence=0.9, older_after=False, tags=None):
    created = T0 + timedelta(hours=i)
    older = SimpleNamespace(
        id=uuid4(),
        user_id=USER,
        context_id=UUID(CTX),
        created_at=created + timedelta(minutes=1) if older_after else created - timedelta(days=1),
        updated_at=None,
        version=1,
        summary=f"old value {i}",
        content_head="old body",
        content_chars=len("old body"),
        tags=list(tags or []),
    )
    new_summary, new_head = f"new value {i}", "new body"
    candidate = {
        "memory_id": str(older.id),
        "similarity": 0.91,
        "detected_at": "2026-01-01T00:00:00Z",
        "verification": _record(
            older, new_summary, new_head, verdict=verdict, confidence=confidence
        ),
    }
    newer = SimpleNamespace(
        id=uuid4(),
        user_id=USER,
        workspace_id=UUID(WS),
        context_id=UUID(CTX),
        created_at=created,
        version=1,
        summary=new_summary,
        content_head=new_head,
        supersede_candidate=candidate,
    )
    return newer, older


def _edge(src, dst, edge_type="supersedes", *, origin="semantic", user_id=USER):
    return SimpleNamespace(
        user_id=user_id, src_id=src.id, dst_id=dst.id, edge_type=edge_type, origin=origin
    )


class _Savepoint:
    def __init__(self, db: _Db) -> None:
        self.db = db

    async def __aenter__(self) -> None:
        self.db.savepoints.append("begin")

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.db.savepoints.append("rollback" if exc_type else "release")
        return False


class _Db:
    """Answers the three column selects from lists and records the rest."""

    def __init__(
        self,
        carriers,
        targets=(),
        edges=(),
        locked_edges=None,
        *,
        revisions=(),
        revision_error: Exception | None = None,
        log_began=None,
    ) -> None:
        self.carriers = list(carriers)
        self.targets = list(targets)
        self.edges = list(edges)
        self.locked_edges = locked_edges
        # The revision log: ``updated`` events (#3019's read) and its start.
        self.revisions = list(revisions)
        self.revision_error = revision_error
        self.log_began = log_began
        self.revision_reads: list[Any] = []
        self.log_reads = 0
        self.statements: list[Any] = []
        self.updates: list[Any] = []
        self.savepoints: list[str] = []
        self._edge_reads = 0

    def begin_nested(self) -> _Savepoint:
        return _Savepoint(self)

    def expire(self, obj) -> None:
        return None

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        result = MagicMock()
        if stmt.is_dml:
            self.updates.append(stmt)
            return result
        names = {column.name for column in stmt.selected_columns}
        if "writer" in names:
            self.revision_reads.append(stmt)
            if self.revision_error is not None:
                raise self.revision_error
            result.all.return_value = list(self.revisions)
            return result
        if names == {"at"}:
            self.log_reads += 1
            result.scalar.return_value = self.log_began
            return result
        if "supersede_candidate" in names:
            rows = self.carriers
        elif "tags" in names:
            rows = self.targets
        elif "edge_type" in names:
            self._edge_reads += 1
            rows = self.edges
            if self._edge_reads > 1 and self.locked_edges is not None:
                rows = self.locked_edges
        else:  # pragma: no cover - a statement the phase must not run
            raise AssertionError(f"unexpected statement: {stmt}")
        result.all.return_value = list(rows)
        return result


def _locked_as_read(pairs):
    """``locked_refusals`` answering with the rows as they were read."""
    by_id = {}
    for newer, older in pairs:
        by_id[newer.id] = SimpleNamespace(
            tags=[],
            supersede_candidate=newer.supersede_candidate,
            summary=newer.summary,
            content_head=newer.content_head,
            content_chars=len(newer.content_head),
        )
        by_id[older.id] = SimpleNamespace(
            tags=list(older.tags),
            supersede_candidate=None,
            summary=older.summary,
            content_head=older.content_head,
            content_chars=older.content_chars,
        )

    async def locked(db, rows):
        return [], {obj.id: by_id[obj.id] for _label, obj in rows}

    return locked


def _written(edge_type="supersedes", origin="semantic"):
    return AsyncMock(return_value=(None, SimpleNamespace(edge_type=edge_type, origin=origin)))


async def _run(
    db: _Db,
    pairs,
    *,
    max_memories=200,
    writable=True,
    locked=None,
    writer=None,
    exempt=frozenset({"status:open"}),
    coverage=None,
):
    reporter = MagicMock()
    reporter.add_action = AsyncMock()
    writer = writer or _written()
    can_write = writable if isinstance(writable, AsyncMock) else AsyncMock(return_value=writable)
    phase = SupersedeApplyPhase(db, coverage=coverage or LaneEntry())
    with (
        patch("services.supersede_acceptance.can_write_edges", can_write),
        patch(
            "services.sleep.supersede_apply.locked_refusals",
            AsyncMock(side_effect=locked or _locked_as_read(pairs)),
        ),
        patch("services.sleep.supersede_apply.write_sleep_supersedes_edge", writer),
        patch("services.sleep.supersede_apply.exempt_tags", return_value=exempt),
    ):
        result = await phase.execute(
            MagicMock(),
            USER,
            WS,
            CTX,
            SleepBudget(max_memories=max_memories),
            reporter=reporter,
            report_id=REPORT_ID,
        )
    return SimpleNamespace(
        result=result, phase=phase, reporter=reporter, writer=writer, can_write=can_write
    )


def _db_for(pairs, *, targets=None, **kw) -> _Db:
    return _Db(
        [newer for newer, _ in pairs],
        [older for _, older in pairs] if targets is None else targets,
        **kw,
    )


# ---------------------------------------------------------------- write access


class TestWriteAccessFirst:
    @pytest.mark.asyncio
    async def test_not_writable_is_skipped_before_any_read(self) -> None:
        pairs = [_pair(0)]
        db = _db_for(pairs)
        run = await _run(db, pairs, writable=False)
        assert run.result.skipped is True
        assert run.result.skip_reason == "not_writable"
        assert run.result.details is None
        assert db.statements == []
        assert run.phase.coverage_outcome is None
        run.can_write.assert_awaited_once()
        kwargs = run.can_write.await_args.kwargs
        assert kwargs == {"user_id": USER, "workspace_id": UUID(WS), "context_id": CTX}

    @pytest.mark.asyncio
    async def test_a_suspended_workspace_is_not_writable(self) -> None:
        from utils.exceptions import WorkspaceSlotSuspendedError

        pairs = [_pair(0)]
        db = _db_for(pairs)
        refusal = AsyncMock(side_effect=WorkspaceSlotSuspendedError.for_outsider())
        run = await _run(db, pairs, writable=refusal)
        assert run.result.skip_reason == "not_writable"
        assert db.statements == []
        assert run.phase.coverage_outcome is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("ws", "ctx"), [(None, CTX), (WS, None)])
    async def test_no_context_is_skipped(self, ws, ctx) -> None:
        db = _Db([])
        phase = SupersedeApplyPhase(db, coverage=LaneEntry())
        result = await phase.execute(
            MagicMock(), USER, ws, ctx, SleepBudget(), reporter=MagicMock(), report_id=REPORT_ID
        )
        assert (result.skipped, result.skip_reason) == (True, "no_context")
        assert db.statements == []


# ------------------------------------------------------------------ selection


class TestCarrierStatement:
    @staticmethod
    def _compiled(db: _Db):
        stmt = db.statements[0]
        compiled = stmt.compile(dialect=postgresql.dialect())
        return str(compiled), compiled.params

    @pytest.mark.asyncio
    async def test_the_shape_without_a_position(self) -> None:
        db = _Db([])
        await _run(db, [], max_memories=7)
        sql, params = self._compiled(db)
        assert len(db.statements) == 1
        # A stored verdict "supersedes", bound, and no rolled-back marker.
        assert "memories.supersede_candidate #>> " in sql
        assert "supersedes" in params.values()
        assert "memories.supersede_candidate ->> " in sql
        assert "sleep_apply_reverted" in params.values()
        assert "IS NULL" in sql
        # dedup_eligible: the run user's own live, unpinned rows in the scope.
        assert "memories.user_id = " in sql
        assert USER in params.values()
        assert "memories.deleted_at IS NULL" in sql
        assert "memories.type != " in sql
        assert UUID(WS) in params.values() and UUID(CTX) in params.values()
        # Coverage order, one row more than the allowance.
        assert "ORDER BY memories.created_at ASC, memories.id ASC" in sql
        assert 8 in params.values()
        # The content head is bound, never a literal.
        assert "substr(memories.content" in sql
        assert str(CONTENT_HEAD_CHARS) not in sql
        assert CONTENT_HEAD_CHARS in params.values()
        assert "(memories.created_at, memories.id) >" not in sql

    @pytest.mark.asyncio
    async def test_a_stored_position_is_a_bound_row_value(self) -> None:
        at, row_id = T0 + timedelta(days=3), uuid4()
        db = _Db([])
        await _run(db, [], coverage=LaneEntry(at=at, id=row_id))
        sql, params = self._compiled(db)
        assert "(memories.created_at, memories.id) > (" in sql
        assert at in params.values() and row_id in params.values()

    @pytest.mark.asyncio
    async def test_nothing_waiting_reads_nothing_more(self) -> None:
        db = _Db([])
        run = await _run(db, [])
        assert len(db.statements) == 1
        assert run.result.success is True
        assert run.result.details == {
            "applied": 0,
            "floor": 0.7,
            "candidates_considered": 0,
            "no_action_reasons": {},
            "budget_cut": False,
        }
        assert run.phase.coverage_outcome.keys == []
        assert run.phase.coverage_outcome.exhausted is True

    @pytest.mark.asyncio
    async def test_targets_are_read_through_dedup_eligible_and_the_exempt_tags(self) -> None:
        pairs = [_pair(0)]
        db = _db_for(pairs)
        await _run(db, pairs)
        target_sql = str(db.statements[1].compile(dialect=postgresql.dialect()))
        params = db.statements[1].compile(dialect=postgresql.dialect()).params
        assert "memories.id IN" in target_sql
        assert "memories.deleted_at IS NULL" in target_sql
        assert "memories.tags &&" in target_sql
        assert CONTENT_HEAD_CHARS in params.values()
        edge_sql = str(db.statements[2].compile(dialect=postgresql.dialect()))
        assert "neural_memory_edges.src_id IN" in edge_sql
        assert "neural_memory_edges.dst_id IN" in edge_sql
        assert "neural_memory_edges.workspace_id = " in edge_sql
        assert "neural_memory_edges.context_id = " in edge_sql
        # Any author: no user filter on the pair's edges.
        assert "neural_memory_edges.user_id =" not in edge_sql


class TestBound:
    @pytest.mark.asyncio
    async def test_one_row_more_than_the_allowance_is_a_cut(self) -> None:
        pairs = [_pair(i) for i in range(3)]
        db = _db_for(pairs)
        run = await _run(db, pairs, max_memories=2)
        outcome = run.phase.coverage_outcome
        assert outcome.exhausted is False
        assert [key[1] for key in outcome.keys] == [pairs[0][0].id, pairs[1][0].id]
        assert run.result.details["budget_cut"] is True
        assert run.result.details["candidates_considered"] == 2
        assert run.result.memories_processed == 2
        assert run.writer.await_count == 2

    @pytest.mark.asyncio
    async def test_at_most_the_allowance_is_exhausted(self) -> None:
        pairs = [_pair(i) for i in range(2)]
        run = await _run(_db_for(pairs), pairs, max_memories=2)
        assert run.phase.coverage_outcome.exhausted is True
        assert run.result.details["budget_cut"] is False


# ------------------------------------------------------------------- reasons


def _reasons(run) -> dict[str, int]:
    return run.result.details["no_action_reasons"]


class TestReasons:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "malform",
        [
            lambda c: {k: v for k, v in c.items() if k != "memory_id"},
            lambda c: {**c, "memory_id": "not-a-uuid"},
            lambda c: {**c, "memory_id": None},
            lambda c: {**c, "similarity": "high"},
            lambda c: {**c, "similarity": True},
            lambda c: {k: v for k, v in c.items() if k != "similarity"},
            lambda c: "a string",
        ],
        ids=["no-id", "bad-id", "null-id", "text-sim", "bool-sim", "no-sim", "not-a-dict"],
    )
    async def test_malformed_candidate(self, malform) -> None:
        newer, older = _pair(0)
        newer.supersede_candidate = malform(newer.supersede_candidate)
        run = await _run(_db_for([(newer, older)]), [(newer, older)])
        assert _reasons(run) == {"malformed_candidate": 1}
        run.writer.assert_not_awaited()
        assert run.phase.coverage_outcome.outcomes[newer.id] == SETTLED

    @pytest.mark.asyncio
    async def test_a_candidate_naming_its_own_carrier_is_malformed(self) -> None:
        newer, older = _pair(0)
        newer.supersede_candidate = {**newer.supersede_candidate, "memory_id": str(newer.id)}
        run = await _run(_db_for([(newer, older)]), [(newer, older)])
        assert _reasons(run) == {"malformed_candidate": 1}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("verdict", "confidence"), [("supersedes", 0.5), ("parallel", 0.9), ("unclear", 0.9)]
    )
    async def test_below_floor(self, verdict, confidence) -> None:
        pair = _pair(0, verdict=verdict, confidence=confidence)
        run = await _run(_db_for([pair]), [pair])
        assert _reasons(run) == {"below_floor": 1}

    @pytest.mark.asyncio
    async def test_the_floor_is_the_read_paths(self, monkeypatch) -> None:
        monkeypatch.setenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", "0.9")
        pair = _pair(0, confidence=0.8)
        run = await _run(_db_for([pair]), [pair])
        assert _reasons(run) == {"below_floor": 1}
        assert run.result.details["floor"] == 0.9

    @pytest.mark.asyncio
    async def test_below_floor_wins_over_an_ineligible_target(self) -> None:
        pair = _pair(0, confidence=0.5)
        run = await _run(_db_for([pair], targets=[]), [pair])
        assert _reasons(run) == {"below_floor": 1}

    @pytest.mark.asyncio
    async def test_target_ineligible(self) -> None:
        pair = _pair(0)
        run = await _run(_db_for([pair], targets=[]), [pair])
        assert _reasons(run) == {"target_ineligible": 1}

    @pytest.mark.asyncio
    async def test_newer_target_wins_over_a_stale_verdict(self) -> None:
        newer, older = _pair(0, older_after=True)
        older.summary = "edited since the verdict"
        run = await _run(_db_for([(newer, older)]), [(newer, older)])
        assert _reasons(run) == {"newer_target": 1}

    @pytest.mark.asyncio
    async def test_equal_creation_times_pass_the_direction_rule(self) -> None:
        newer, older = _pair(0)
        older.created_at = newer.created_at
        run = await _run(_db_for([(newer, older)]), [(newer, older)])
        assert run.result.details["applied"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("side", ["older", "newer"])
    async def test_verdict_stale_wins_over_an_existing_link(self, side) -> None:
        newer, older = _pair(0)
        if side == "older":
            older.content_head = "edited older body"
        else:
            newer.summary = "edited newer summary"
        db = _db_for([(newer, older)], edges=[_edge(newer, older)])
        run = await _run(db, [(newer, older)])
        assert _reasons(run) == {"verdict_stale": 1}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("edges", "reason"),
        [
            (lambda n, o: [_edge(n, o, origin="declared")], "already_linked"),
            (lambda n, o: [_edge(o, n)], "already_linked"),
            (lambda n, o: [_edge(n, o, user_id="another-member")], "already_linked"),
            (
                lambda n, o: [_edge(n, o, "contradicts"), _edge(o, n)],
                "already_linked",
            ),
            (lambda n, o: [_edge(o, n, "contradicts", user_id="x")], "contradicts"),
            (
                lambda n, o: [
                    _edge(n, o, "related_to", origin="declared"),
                    _edge(n, o, "contradicts", user_id="x"),
                ],
                "contradicts",
            ),
            (
                lambda n, o: [_edge(n, o, "related_to", origin="declared")],
                "declared_edge_conflict",
            ),
        ],
        ids=[
            "declared-supersedes",
            "reverse-supersedes",
            "other-member",
            "supersedes-and-contradicts",
            "contradicts",
            "contradicts-and-declared",
            "declared-related",
        ],
    )
    async def test_edge_rules_in_order(self, edges, reason) -> None:
        newer, older = _pair(0)
        db = _db_for([(newer, older)], edges=edges(newer, older))
        run = await _run(db, [(newer, older)])
        assert _reasons(run) == {reason: 1}
        run.writer.assert_not_awaited()
        assert run.phase.coverage_outcome.outcomes[newer.id] == SETTLED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "edge",
        [
            lambda n, o: _edge(n, o, "related_to", origin="declared", user_id="another-member"),
            lambda n, o: _edge(n, o, "neural_association", origin="hebbian"),
            lambda n, o: _edge(n, o, "related_to", origin="semantic"),
            lambda n, o: _edge(o, n, "related_to", origin="declared"),
        ],
        ids=["another-members-declared", "hebbian", "discovered", "declared-reverse"],
    )
    async def test_other_rows_on_the_pair_do_not_refuse(self, edge) -> None:
        newer, older = _pair(0)
        db = _db_for([(newer, older)], edges=[edge(newer, older)])
        run = await _run(db, [(newer, older)])
        assert run.result.details["applied"] == 1
        assert _reasons(run) == {}


# ------------------------------------------------------------ text recency


def _text_edit(memory, at, *, version=2, text=True):
    """One logged ``updated`` event of ``memory`` (an API edit), as the read returns it."""
    memory.version = max(memory.version, version)
    return SimpleNamespace(
        memory_id=memory.id, version=version, writer="api", at=at, text=text, restored_from=None
    )


class TestTextRecency:
    """#3020: the direction rule is dedup's recency (#3019): the later of a
    memory's creation and its last logged text edit; a pair the revision log
    cannot order is left alone, under dedup's keep-both conditions."""

    @pytest.mark.asyncio
    async def test_an_older_memory_edited_after_the_newer_was_written_is_refused(self) -> None:
        """The verdict is fresh for both current texts: only the edit time tells."""
        newer, older = _pair(0)
        older.updated_at = newer.created_at + timedelta(hours=1)
        edits = [_text_edit(older, newer.created_at + timedelta(hours=1))]
        db = _db_for([(newer, older)], revisions=edits)
        run = await _run(db, [(newer, older)])
        assert _reasons(run) == {"newer_target": 1}
        run.writer.assert_not_awaited()
        assert run.phase.coverage_outcome.outcomes[newer.id] == SETTLED

    @pytest.mark.asyncio
    async def test_an_edit_seconds_after_the_newer_refuses_too(self) -> None:
        """Compared to the microsecond: no tie rule decides an equal minute here."""
        newer, older = _pair(0)
        older.updated_at = newer.created_at + timedelta(seconds=5)
        edits = [_text_edit(older, newer.created_at + timedelta(seconds=5))]
        run = await _run(_db_for([(newer, older)], revisions=edits), [(newer, older)])
        assert _reasons(run) == {"newer_target": 1}

    @pytest.mark.asyncio
    async def test_an_older_memory_edited_before_the_newer_was_written_is_applied(self) -> None:
        newer, older = _pair(0)
        older.updated_at = newer.created_at - timedelta(hours=1)
        edits = [_text_edit(older, newer.created_at - timedelta(hours=1))]
        db = _db_for([(newer, older)], revisions=edits)
        run = await _run(db, [(newer, older)])
        assert run.result.details["applied"] == 1
        # One read for the edited rows only; the log start is not needed.
        assert len(db.revision_reads) == 1
        params = db.revision_reads[0].compile(dialect=postgresql.dialect()).params
        assert [v for v in params.values() if isinstance(v, list)] == [[older.id]]
        assert db.log_reads == 0

    @pytest.mark.asyncio
    async def test_a_newer_memory_edited_later_still_wins(self) -> None:
        newer, older = _pair(0)
        older.updated_at = newer.created_at + timedelta(hours=1)
        edits = [
            _text_edit(older, newer.created_at + timedelta(hours=1)),
            _text_edit(newer, newer.created_at + timedelta(hours=2)),
        ]
        run = await _run(_db_for([(newer, older)], revisions=edits), [(newer, older)])
        assert run.result.details["applied"] == 1

    @pytest.mark.asyncio
    async def test_an_edit_of_other_fields_does_not_count(self) -> None:
        newer, older = _pair(0)
        older.updated_at = newer.created_at + timedelta(hours=1)
        edits = [_text_edit(older, newer.created_at + timedelta(hours=1), text=False)]
        db = _db_for([(newer, older)], revisions=edits, log_began=T0 - timedelta(days=90))
        run = await _run(db, [(newer, older)])
        assert run.result.details["applied"] == 1
        assert db.log_reads == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("began", [None, "after"], ids=["no-log", "log-began-after"])
    async def test_a_later_stamp_the_log_does_not_cover_is_unknowable(self, began) -> None:
        newer, older = _pair(0)
        older.updated_at = newer.created_at + timedelta(hours=1)
        log_began = None if began is None else newer.created_at + timedelta(minutes=30)
        db = _db_for([(newer, older)], log_began=log_began)
        run = await _run(db, [(newer, older)])
        assert _reasons(run) == {"ambiguous_recency": 1}
        run.writer.assert_not_awaited()
        assert db.revision_reads == []

    @pytest.mark.asyncio
    async def test_a_later_stamp_the_log_covers_is_known(self) -> None:
        newer, older = _pair(0)
        older.updated_at = newer.created_at + timedelta(hours=1)
        db = _db_for([(newer, older)], log_began=T0 - timedelta(days=90))
        run = await _run(db, [(newer, older)])
        assert run.result.details["applied"] == 1

    @pytest.mark.asyncio
    async def test_a_failed_revision_read_leaves_a_later_stamp_unknowable(self) -> None:
        newer, older = _pair(0)
        older.version = 2
        older.updated_at = newer.created_at + timedelta(hours=1)
        db = _db_for(
            [(newer, older)],
            revision_error=RuntimeError("revision read failed"),
            log_began=T0 - timedelta(days=90),
        )
        run = await _run(db, [(newer, older)])
        assert _reasons(run) == {"ambiguous_recency": 1}
        # The failed read was rolled back to its own savepoint.
        assert db.savepoints[:2] == ["begin", "rollback"]

    @pytest.mark.asyncio
    async def test_the_log_start_is_read_once_a_run(self) -> None:
        pairs = [_pair(i) for i in range(2)]
        for newer, older in pairs:
            older.updated_at = newer.created_at + timedelta(hours=1)
        db = _db_for(pairs, log_began=None)
        run = await _run(db, pairs)
        assert _reasons(run) == {"ambiguous_recency": 2}
        assert db.log_reads == 1

    @pytest.mark.asyncio
    async def test_newer_target_wins_over_ambiguous_recency(self) -> None:
        newer, older = _pair(0, older_after=True)
        older.updated_at = newer.created_at + timedelta(hours=1)
        run = await _run(_db_for([(newer, older)], log_began=None), [(newer, older)])
        assert _reasons(run) == {"newer_target": 1}


# ---------------------------------------------------------- judged heads


class TestJudgedHeads:
    """#3020: the verdict covers the heads the judge saw (the summary's first
    ``SUMMARY_CHARS`` and the content's first ``CONTENT_HEAD_CHARS``). An older
    memory longer than that is not linked: a correction past the head is
    invisible to the fingerprint."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("summary_chars", "content_chars", "applied"),
        [
            (SUMMARY_CHARS, CONTENT_HEAD_CHARS, True),
            (SUMMARY_CHARS + 1, 10, False),
            (10, CONTENT_HEAD_CHARS + 1, False),
        ],
        ids=["at-the-heads", "summary-past", "content-past"],
    )
    async def test_an_older_memory_past_the_judged_heads_is_refused(
        self, summary_chars, content_chars, applied
    ) -> None:
        newer, older = _pair(0)
        older.summary = "s" * summary_chars
        older.content_head = "c" * min(content_chars, CONTENT_HEAD_CHARS)
        older.content_chars = content_chars
        # The verdict is current: it was made on the heads the memory still has.
        newer.supersede_candidate["verification"] = _record(
            older, newer.summary, newer.content_head
        )
        run = await _run(_db_for([(newer, older)]), [(newer, older)])
        if applied:
            assert run.result.details["applied"] == 1
        else:
            assert _reasons(run) == {"older_past_judged_head": 1}
            run.writer.assert_not_awaited()
            assert run.phase.coverage_outcome.outcomes[newer.id] == SETTLED

    @pytest.mark.asyncio
    async def test_a_long_newer_memory_is_applied(self) -> None:
        """Only the older memory's text is hidden on the verdict."""
        newer, older = _pair(0)
        newer.summary = "n" * (SUMMARY_CHARS + 50)
        newer.supersede_candidate["verification"] = _record(
            older, newer.summary, newer.content_head
        )
        run = await _run(_db_for([(newer, older)]), [(newer, older)])
        assert run.result.details["applied"] == 1

    @pytest.mark.asyncio
    async def test_a_stale_verdict_wins_over_a_long_older_memory(self) -> None:
        newer, older = _pair(0)
        older.content_chars = CONTENT_HEAD_CHARS + 1
        older.content_head = "edited older body"
        run = await _run(_db_for([(newer, older)]), [(newer, older)])
        assert _reasons(run) == {"verdict_stale": 1}

    @pytest.mark.asyncio
    async def test_the_length_is_read_with_the_target_and_under_the_lock(self) -> None:
        pair = _pair(0)
        db = _db_for([pair])
        await _run(db, [pair])
        target_sql = str(db.statements[1].compile(dialect=postgresql.dialect()))
        assert "char_length(memories.content)" in target_sql

    @pytest.mark.asyncio
    async def test_content_appended_past_the_head_after_the_read_refuses(self) -> None:
        newer, older = pair = _pair(0)
        as_read = _locked_as_read([pair])

        async def locked(db, rows):
            reasons, state = await as_read(db, rows)
            state[older.id].content_chars = CONTENT_HEAD_CHARS + 40
            return reasons, state

        db = _db_for([pair])
        run = await _run(db, [pair], locked=locked)
        assert _reasons(run) == {"changed_since_read": 1}
        run.writer.assert_not_awaited()
        assert db.savepoints == ["begin", "rollback"]


# ------------------------------------------------------------- under the lock


class TestLockedRecheck:
    @pytest.mark.asyncio
    async def test_a_lock_reason_refuses(self) -> None:
        pair = _pair(0)

        async def locked(db, rows):
            return ["older_edited"], {}

        run = await _run(_db_for([pair]), [pair], locked=locked)
        assert _reasons(run) == {"changed_since_read": 1}
        run.writer.assert_not_awaited()

    @staticmethod
    def _locked_with(pair, **changes):
        newer, older = pair
        as_read = _locked_as_read([pair])

        async def locked(db, rows):
            reasons, state = await as_read(db, rows)
            for side, values in changes.items():
                row = state[newer.id if side == "newer" else older.id]
                for key, value in values.items():
                    setattr(row, key, value)
            return reasons, state

        return locked

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "changes",
        [
            {"newer": {"supersede_candidate": {"dismissed": {"memory_id": "x"}}}},
            {"newer": {"supersede_candidate": None}},
            {"older": {"tags": ["status:open"]}},
            {"older": {"summary": "edited without a version move"}},
            {"newer": {"content_head": "edited without a version move"}},
        ],
        ids=["dismissed", "cleared", "exempt-tag", "older-text", "newer-text"],
    )
    async def test_a_change_seen_only_under_the_lock_refuses(self, changes) -> None:
        pair = _pair(0)
        db = _db_for([pair])
        run = await _run(db, [pair], locked=self._locked_with(pair, **changes))
        assert _reasons(run) == {"changed_since_read": 1}
        run.writer.assert_not_awaited()
        assert db.savepoints == ["begin", "rollback"]
        assert run.phase.coverage_outcome.outcomes[pair[0].id] == SETTLED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("edge", "reason"),
        [
            (lambda n, o: _edge(o, n), "already_linked"),
            (lambda n, o: _edge(n, o, "contradicts", user_id="x"), "contradicts"),
            (lambda n, o: _edge(n, o, "related_to", origin="declared"), "declared_edge_conflict"),
        ],
        ids=["supersedes", "contradicts", "declared"],
    )
    async def test_the_edges_are_read_again_under_the_lock(self, edge, reason) -> None:
        newer, older = _pair(0)
        db = _db_for([(newer, older)], edges=[], locked_edges=[edge(newer, older)])
        run = await _run(db, [(newer, older)])
        assert _reasons(run) == {reason: 1}
        run.writer.assert_not_awaited()


# --------------------------------------------------------------------- write


class TestWrite:
    @pytest.mark.asyncio
    async def test_an_applied_pair(self) -> None:
        newer, older = _pair(0)
        db = _db_for([(newer, older)])
        run = await _run(db, [(newer, older)])

        assert run.result.success is True
        assert run.result.details["applied"] == 1
        assert run.result.changed_memory_ids == set()
        kwargs = run.writer.await_args.kwargs
        assert kwargs == {
            "user_id": USER,
            "newer_id": newer.id,
            "older_id": older.id,
            "workspace_id": WS,
            "context_id": CTX,
            "edge_metadata": {"source": "sleep_supersede_apply", "report_id": str(REPORT_ID)},
            "protect_declared_link": True,
        }
        # The suggestion is cleared to SQL NULL; neither updated_at nor version.
        assert len(db.updates) == 1
        update_sql = str(db.updates[0].compile(dialect=postgresql.dialect()))
        assert update_sql.startswith("UPDATE memories SET supersede_candidate=NULL WHERE")
        assert "updated_at" not in update_sql and "version" not in update_sql
        run.reporter.add_action.assert_awaited_once_with(
            report_id=REPORT_ID,
            phase="supersede_apply",
            action_type="apply_supersede",
            memory_id=newer.id,
            target_id=older.id,
            details={
                "prior_candidate": newer.supersede_candidate,
                "prior_edge": None,
                "verdict_confidence": 0.9,
                "similarity": 0.91,
            },
        )
        assert db.savepoints == ["begin", "release"]
        assert run.phase.coverage_outcome.outcomes[newer.id] == SETTLED

    @pytest.mark.asyncio
    async def test_a_retyped_edge_is_recorded(self) -> None:
        newer, older = _pair(0)
        prior = {
            "edge_type": "neural_association",
            "origin": "hebbian",
            "weight": 0.3,
            "confidence": 1.0,
            "edge_metadata": None,
        }
        writer = AsyncMock(
            return_value=(prior, SimpleNamespace(edge_type="supersedes", origin="semantic"))
        )
        run = await _run(_db_for([(newer, older)]), [(newer, older)], writer=writer)
        details = run.reporter.add_action.await_args.kwargs["details"]
        assert details["prior_edge"] == prior

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("edge_type", "reason"),
        [("supersedes", "already_linked"), ("related_to", "declared_edge_conflict")],
    )
    async def test_a_declared_row_kept_by_the_writer_discards_the_write(
        self, edge_type, reason
    ) -> None:
        newer, older = _pair(0)
        db = _db_for([(newer, older)])
        run = await _run(
            db, [(newer, older)], writer=_written(edge_type=edge_type, origin="declared")
        )
        assert _reasons(run) == {reason: 1}
        assert run.result.details["applied"] == 0
        run.reporter.add_action.assert_not_awaited()
        assert db.updates == []
        assert db.savepoints == ["begin", "rollback"]
        assert run.phase.coverage_outcome.outcomes[newer.id] == SETTLED

    @pytest.mark.asyncio
    async def test_a_failing_pair_among_applied_ones_is_passed(self) -> None:
        pairs = [_pair(i) for i in range(3)]
        failing = pairs[1][0].id

        async def writer(edge_repo, **kwargs):
            if kwargs["newer_id"] == failing:
                raise RuntimeError("edge write failed")
            return None, SimpleNamespace(edge_type="supersedes", origin="semantic")

        db = _db_for(pairs)
        with patch("services.sleep.supersede_apply.logger") as log:
            run = await _run(db, pairs, writer=AsyncMock(side_effect=writer))
        assert run.result.success is True
        assert run.result.operation_failures == 1
        assert run.result.details["applied"] == 2
        assert _reasons(run) == {"write_failed": 1}
        outcomes = run.phase.coverage_outcome.outcomes
        assert outcomes == {pairs[0][0].id: SETTLED, failing: PASSED, pairs[2][0].id: SETTLED}
        assert db.savepoints == ["begin", "release", "begin", "rollback", "begin", "release"]
        log.warning.assert_called_once()
        assert log.warning.call_args.args[0] == "sleep_supersede_apply_write_failed"
        assert log.warning.call_args.kwargs == {
            "newer_id": str(failing),
            "older_id": str(pairs[1][1].id),
            "error_type": "RuntimeError",
        }

    @pytest.mark.asyncio
    async def test_a_failing_pair_among_refusals_only_holds_the_position(self) -> None:
        refused = _pair(0, confidence=0.5)
        failing = _pair(1)
        writer = AsyncMock(side_effect=RuntimeError("edge write failed"))
        run = await _run(_db_for([refused, failing]), [refused, failing], writer=writer)
        assert run.result.success is True
        assert run.result.operation_failures == 1
        assert run.phase.coverage_outcome.outcomes == {
            refused[0].id: SETTLED,
            failing[0].id: FAILURE,
        }

    @pytest.mark.asyncio
    async def test_every_pair_failing_fails_the_phase(self) -> None:
        pairs = [_pair(i) for i in range(2)]
        writer = AsyncMock(side_effect=RuntimeError("edge write failed"))
        run = await _run(_db_for(pairs), pairs, writer=writer)
        assert run.result.success is False
        assert run.result.error == "every supersede write failed"
        assert run.result.operation_failures == 2
        assert _reasons(run) == {"write_failed": 2}
        assert set(run.phase.coverage_outcome.outcomes.values()) == {FAILURE}
        assert run.phase.coverage_outcome.rows(returned=False) == [
            (key, FAILURE) for key in run.phase.coverage_outcome.keys
        ]


# ------------------------------------------------------------------ rollback


class TestUndoApplySupersede:
    """``undo_apply_supersede`` touches the edge only while it carries this
    report's marker, and restores the suggestion only after it reversed the
    link, only into an empty column, marked so Sleep does not apply it again."""

    _CANDIDATE = {"memory_id": "x", "similarity": 0.91, "verification": {"verdict": "supersedes"}}

    @staticmethod
    def _action(prior_edge=None):
        return SimpleNamespace(
            memory_id=uuid4(),
            target_id=uuid4(),
            report_id=REPORT_ID,
            details={
                "prior_candidate": dict(TestUndoApplySupersede._CANDIDATE),
                "prior_edge": prior_edge,
                "verdict_confidence": 0.9,
                "similarity": 0.91,
            },
        )

    @staticmethod
    def _db(*rowcounts) -> AsyncMock:
        results = []
        for count in rowcounts:
            result = MagicMock()
            result.rowcount = count
            results.append(result)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=results)
        return db

    @staticmethod
    def _sql(db, index):
        stmt = db.execute.await_args_list[index].args[0]
        compiled = stmt.compile(dialect=postgresql.dialect())
        return str(compiled), compiled.params

    async def _undo(self, db, action, summary=None):
        from services.sleep.supersede_apply import undo_apply_supersede

        summary = {} if summary is None else summary
        await undo_apply_supersede(db, action, SimpleNamespace(user_id=USER), summary)
        return summary

    @pytest.mark.asyncio
    async def test_a_link_without_a_prior_edge_is_deleted_by_its_marker(self) -> None:
        action = self._action()
        db = self._db(1, 1)
        summary = await self._undo(db, action)
        assert summary == {"supersede_links_reversed": 1, "supersede_candidates_restored": 1}
        sql, params = self._sql(db, 0)
        assert sql.startswith("DELETE FROM neural_memory_edges WHERE")
        for column in ("user_id", "src_id", "dst_id", "edge_type", "origin"):
            assert f"neural_memory_edges.{column} = " in sql
        assert "CAST(neural_memory_edges.metadata AS JSONB) ->> " in sql
        values = list(params.values())
        assert USER in values and action.memory_id in values and action.target_id in values
        assert "sleep_supersede_apply" in values and str(REPORT_ID) in values
        assert "supersedes" in values and "semantic" in values

    @pytest.mark.asyncio
    async def test_a_retyped_edge_is_put_back(self) -> None:
        prior = {
            "edge_type": "neural_association",
            "origin": "hebbian",
            "weight": 0.3,
            "confidence": 0.8,
            "edge_metadata": {"source": "tag_cooccurrence"},
        }
        db = self._db(1, 1)
        summary = await self._undo(db, self._action(prior_edge=prior))
        assert summary["supersede_links_reversed"] == 1
        sql, params = self._sql(db, 0)
        assert sql.startswith("UPDATE neural_memory_edges SET")
        values = list(params.values())
        assert "neural_association" in values and "hebbian" in values
        assert 0.3 in values and 0.8 in values
        assert "CAST(neural_memory_edges.metadata AS JSONB) ->> " in sql

    @pytest.mark.asyncio
    async def test_the_restored_suggestion_carries_the_rolled_back_run(self) -> None:
        action = self._action()
        db = self._db(1, 1)
        await self._undo(db, action)
        sql, params = self._sql(db, 1)
        assert sql.startswith("UPDATE memories SET supersede_candidate=")
        assert "memories.supersede_candidate IS NULL" in sql
        assert "CAST(memories.supersede_candidate AS TEXT) = " in sql
        assert "memories.deleted_at IS NULL" in sql
        assert "memories.user_id = " in sql
        assert "updated_at" not in sql and "version" not in sql
        restored = next(v for v in params.values() if isinstance(v, dict))
        assert restored == {**self._CANDIDATE, "sleep_apply_reverted": str(REPORT_ID)}

    @pytest.mark.asyncio
    async def test_a_changed_link_is_kept_and_nothing_is_restored(self) -> None:
        db = self._db(0)
        summary = await self._undo(db, self._action())
        assert summary == {"supersede_links_kept": 1}
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_a_suggestion_landed_since_is_kept(self) -> None:
        db = self._db(1, 0)
        summary = await self._undo(db, self._action())
        assert summary == {"supersede_links_reversed": 1}

    @pytest.mark.asyncio
    async def test_counters_add_to_a_summary_and_touch_nothing_else(self) -> None:
        summary = {"edges_deleted": 2, "errors": [], "supersede_links_reversed": 1}
        await self._undo(self._db(1, 1), self._action(), summary)
        assert summary == {
            "edges_deleted": 2,
            "errors": [],
            "supersede_links_reversed": 2,
            "supersede_candidates_restored": 1,
        }
