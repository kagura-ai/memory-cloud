"""#3020 on Postgres: Sleep applies judge-verified supersede suggestions.

Flush only: the phase, the orchestrator and the undo handler never commit, and
``db_session`` rolls back. The one test that goes through
``handle_rollback_sleep_run`` commits and cleans up through
``committed_cleanup`` (see ``_supersede_apply_support``). Every report has an
explicit, increasing ``started_at``; "now" is taken inside each test.

Not executed in the local unit run — needs the DB container.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import DateTime, literal, select, text, update

from models.auth import WorkspaceMember
from models.memory import Memory, MemoryRevision
from services.sleep.coverage import LaneEntry, parse_state
from services.supersede_dismissal import build_tombstone
from tests.integration._supersede_apply_support import (
    add_edge,
    add_user,
    apply_actions,
    candidate_column,
    committed_cleanup,
    edge_row,
    new_report,
    run_phase,
    run_sleep,
    scope,
    verified_candidate,
    verified_pair,
)
from utils.datetime import utcnow


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    monkeypatch.delenv("SLEEP_ARCHIVE_EXEMPT_TAGS", raising=False)
    monkeypatch.delenv("SUPERSEDE_VERIFY_MIN_CONFIDENCE", raising=False)


def _base():
    return utcnow() - timedelta(days=10)


@pytest_asyncio.fixture(loop_scope="session")
async def committed(db_session):
    """For the one test that commits: its rows are deleted and checked after it."""
    async with committed_cleanup(db_session) as ids:
        yield ids


# ------------------------------------------------------------------- applied


@pytest.mark.asyncio
async def test_a_verified_pair_becomes_the_marked_link(db_session):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    stored = dict(newer.supersede_candidate)
    report = await new_report(db, s, utcnow() - timedelta(hours=1))

    _, result = await run_phase(db, s, report)

    assert result.details["applied"] == 1
    assert result.details["no_action_reasons"] == {}
    edge = await edge_row(db, s.owner, newer.id, older.id)
    assert (edge.edge_type, edge.origin, edge.weight, edge.confidence) == (
        "supersedes",
        "semantic",
        1.0,
        1.0,
    )
    assert edge.edge_metadata == {"source": "sleep_supersede_apply", "report_id": str(report.id)}
    is_null, raw, _ = await candidate_column(db, newer.id)
    assert is_null is True and raw is None
    actions = await apply_actions(db, report.id)
    assert len(actions) == 1
    assert actions[0].phase == "supersede_apply"
    assert (actions[0].memory_id, actions[0].target_id) == (newer.id, older.id)
    assert actions[0].details == {
        "prior_candidate": stored,
        "prior_edge": None,
        "verdict_confidence": 0.9,
        "similarity": 0.91,
    }
    row = (
        await db.execute(
            text("SELECT updated_at, version FROM memories WHERE id = :id"), {"id": newer.id}
        )
    ).one()
    assert (row.updated_at, row.version) == (None, 1)


@pytest.mark.asyncio
async def test_a_hebbian_association_on_the_pair_is_retyped_and_recorded(db_session):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    await add_edge(db, s, newer, older, "neural_association", origin="hebbian", weight=0.3)
    report = await new_report(db, s, utcnow() - timedelta(hours=1))

    _, result = await run_phase(db, s, report)

    assert result.details["applied"] == 1
    edge = await edge_row(db, s.owner, newer.id, older.id)
    assert (edge.edge_type, edge.origin, edge.weight) == ("supersedes", "semantic", 1.0)
    (action,) = await apply_actions(db, report.id)
    assert action.details["prior_edge"] == {
        "edge_type": "neural_association",
        "origin": "hebbian",
        "weight": 0.3,
        "confidence": 1.0,
        "edge_metadata": None,
    }


# --------------------------------------------------------------- left alone


async def _json_null(db, newer, older):
    await db.execute(
        text("UPDATE memories SET supersede_candidate = 'null'::json WHERE id = :id"),
        {"id": newer.id},
    )


async def _stored(db, newer, value):
    await db.execute(update(Memory).where(Memory.id == newer.id).values(supersede_candidate=value))


_LEFT_ALONE = {
    "parallel": {"verdict": "parallel"},
    "unclear": {"verdict": "unclear"},
    "below-floor": {"confidence": 0.5},
    "older-pinned": {"older_fields": {"delivery_mode": "always"}},
    "older-tool-guardrail": {
        "older_fields": {
            "details": {
                "tool_trigger": {"tool": "Bash", "on": "pre", "match": "x", "action": "inform"}
            }
        }
    },
    "older-time": {
        "older_fields": {
            "type": "time",
            "details": {
                "trigger": {
                    "year": 2026,
                    "month": 9,
                    "day": 9,
                    "from": "2026-09-09T00:00:00",
                    "until": "2026-09-09T23:59:59",
                }
            },
        }
    },
    "older-resource-owned": {
        "older_fields": {
            "details": {"resource_id": "catalog", "doc_id": "item-1", "version": 1},
            "summary_embedding_id": uuid4(),
        }
    },
    "older-exempt-tagged": {"older_fields": {"tags": ["status:open"]}},
    "carrier-written-before-its-target": {"older_after": True},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_LEFT_ALONE))
async def test_left_alone_by_the_pair(db_session, case):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base(), **_LEFT_ALONE[case])
    before = await candidate_column(db, newer.id)
    report = await new_report(db, s, utcnow() - timedelta(hours=1))

    _, result = await run_phase(db, s, report)

    assert result.details["applied"] == 0
    assert await apply_actions(db, report.id) == []
    assert await candidate_column(db, newer.id) == before
    assert await edge_row(db, s.owner, newer.id, older.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unverified",
        "dismissal-tombstone",
        "json-null",
        "rolled-back",
        "older-edited-after-the-verdict",
        "older-another-users",
        "declared-related-to",
        "contradicts-newer-to-older",
        "contradicts-older-to-newer",
        "reverse-supersedes",
        "another-members-supersedes",
    ],
)
async def test_left_alone_by_state_or_edges(db_session, case):
    db = db_session
    s = await scope(db)
    older_user = await add_user(db, "sa_other") if case == "older-another-users" else None
    newer, older = await verified_pair(db, s, created_at=_base(), older_user=older_user)
    candidate = dict(newer.supersede_candidate)
    if case == "unverified":
        await _stored(db, newer, {k: v for k, v in candidate.items() if k != "verification"})
    elif case == "dismissal-tombstone":
        await _stored(db, newer, build_tombstone(candidate))
    elif case == "json-null":
        await _json_null(db, newer, older)
    elif case == "rolled-back":
        await _stored(db, newer, {**candidate, "sleep_apply_reverted": str(uuid4())})
    elif case == "older-edited-after-the-verdict":
        await db.execute(update(Memory).where(Memory.id == older.id).values(summary="edited since"))
    elif case == "declared-related-to":
        await add_edge(db, s, newer, older, "related_to", origin="declared")
    elif case == "contradicts-newer-to-older":
        await add_edge(db, s, newer, older, "contradicts", origin="declared")
    elif case == "contradicts-older-to-newer":
        other = await add_user(db, "sa_member")
        await add_edge(db, s, older, newer, "contradicts", origin="declared", user_id=other)
    elif case == "reverse-supersedes":
        await add_edge(db, s, older, newer, "supersedes", origin="declared")
    elif case == "another-members-supersedes":
        other = await add_user(db, "sa_member")
        await add_edge(db, s, newer, older, "supersedes", origin="declared", user_id=other)
    before = await candidate_column(db, newer.id)
    edge_before = await edge_row(db, s.owner, newer.id, older.id)
    report = await new_report(db, s, utcnow() - timedelta(hours=1))

    _, result = await run_phase(db, s, report)

    assert result.details["applied"] == 0
    assert await apply_actions(db, report.id) == []
    assert await candidate_column(db, newer.id) == before
    assert await edge_row(db, s.owner, newer.id, older.id) == edge_before


# ------------------------------------------------------------ text recency


async def _log_text_edit(db, memory: Memory, at, **fields) -> None:
    """An API edit of ``memory``'s text at ``at``, logged as ``record_edit`` logs it."""
    prior = {name: getattr(memory, name) for name in fields}
    for name, value in fields.items():
        setattr(memory, name, value)
    memory.version += 1
    memory.updated_at = at
    db.add(
        MemoryRevision(
            memory_id=memory.id,
            context_id=memory.context_id,
            version=memory.version,
            kind="updated",
            writer="api",
            actor_user_id=memory.user_id,
            prior=prior,
            at=at,
        )
    )
    await db.flush()


def _log_start(at):
    """A stand-in for ``revision_log_began_query`` answering ``at`` on Postgres."""
    return lambda: select(literal(at, DateTime()))


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", ["after", "before"])
async def test_an_older_memory_edited_after_the_newer_was_written_is_refused(db_session, edited):
    """The verdict is fresh for both current texts (the pair was judged after
    the edit): only the edit time says the older memory holds the later text."""
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    shift = timedelta(hours=2) if edited == "after" else -timedelta(hours=2)
    await _log_text_edit(db, older, newer.created_at + shift, summary="the office is on floor 4")
    newer_summary, newer_content = newer.summary, newer.content
    await _stored(
        db,
        newer,
        verified_candidate(older, newer_summary=newer_summary, newer_content=newer_content),
    )
    before = await candidate_column(db, newer.id)
    report = await new_report(db, s, utcnow() - timedelta(hours=1))

    _, result = await run_phase(db, s, report)

    if edited == "after":
        assert result.details["no_action_reasons"] == {"newer_target": 1}
        assert await apply_actions(db, report.id) == []
        assert await candidate_column(db, newer.id) == before
        assert await edge_row(db, s.owner, newer.id, older.id) is None
    else:
        assert result.details["applied"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("log", ["none", "began-after", "began-before"])
async def test_an_older_memory_whose_recency_cannot_be_known_is_refused(db_session, log):
    """No logged text edit, a stamp later than the newer memory, and a log that
    does not cover that time: dedup would keep both, so Sleep links neither."""
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    await db.execute(
        update(Memory)
        .where(Memory.id == older.id)
        .values(updated_at=newer.created_at + timedelta(hours=2))
    )
    began = {
        "none": None,
        "began-after": newer.created_at + timedelta(hours=1),
        "began-before": newer.created_at - timedelta(days=30),
    }[log]
    report = await new_report(db, s, utcnow() - timedelta(hours=1))

    with patch("services.sleep.supersede_apply.revision_log_began_query", _log_start(began)):
        _, result = await run_phase(db, s, report)

    if log == "began-before":
        assert result.details["applied"] == 1
    else:
        assert result.details["no_action_reasons"] == {"ambiguous_recency": 1}
        assert await apply_actions(db, report.id) == []
        assert await edge_row(db, s.owner, newer.id, older.id) is None


# --------------------------------------------------------------- write access


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["viewer", "removed"])
async def test_a_run_user_who_may_not_write_edges_is_skipped(db_session, change):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    # Through the session's own instance: the probe reads the membership back.
    member = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == s.ws.id, WorkspaceMember.user_id == s.owner
            )
        )
    ).scalar_one()
    if change == "viewer":
        member.role = "viewer"
    else:
        await db.delete(member)
    await db.flush()
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    entry = LaneEntry(at=_base(), id=uuid4(), passes=2)

    phase, result = await run_phase(db, s, report, coverage=entry)

    assert (result.skipped, result.skip_reason) == (True, "not_writable")
    assert phase.coverage_outcome is None
    assert await apply_actions(db, report.id) == []
    assert await edge_row(db, s.owner, newer.id, older.id) is None
    assert (await candidate_column(db, newer.id))[0] is False


# ----------------------------------------------------------- under the lock


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["dismissed", "older-text"])
async def test_a_change_committed_after_the_read_refuses_under_the_lock(db_session, change):
    from services.sleep import supersede_apply

    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    # The pair's savepoint rolls back and expires what changed in it: ids only.
    newer_id, older_id = newer.id, older.id
    candidate = dict(newer.supersede_candidate)
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    real = supersede_apply.locked_refusals

    async def changed_then_locked(session, rows):
        if change == "dismissed":
            await session.execute(
                update(Memory)
                .where(Memory.id == newer_id)
                .values(supersede_candidate=build_tombstone(candidate))
            )
        else:
            # An edit of the text that does not move the version.
            await session.execute(
                text("UPDATE memories SET summary = 'rewritten' WHERE id = :id"),
                {"id": older_id},
            )
        return await real(session, rows)

    with patch.object(supersede_apply, "locked_refusals", changed_then_locked):
        _, result = await run_phase(db, s, report)

    assert result.details["no_action_reasons"] == {"changed_since_read": 1}
    assert await apply_actions(db, report.id) == []
    assert await edge_row(db, s.owner, newer_id, older_id) is None


# ------------------------------------------------------------ over the runs


@pytest.mark.asyncio
async def test_coverage_over_runs(db_session):
    """Five verified pairs and one refused (the newest carrier), allowance 2:
    the runs apply 2, 2 and 1; the third completes the pass; the fourth reads
    the refused row again."""
    db = db_session
    s = await scope(db)
    base = _base()
    pairs = [await verified_pair(db, s, created_at=base + timedelta(hours=i)) for i in range(5)]
    refused, _ = await verified_pair(db, s, created_at=base + timedelta(hours=9), confidence=0.5)
    start = utcnow() - timedelta(hours=2)

    reports = [
        await run_sleep(db, s, started_at=start + timedelta(minutes=i), max_memories=2)
        for i in range(4)
    ]

    applied = [len(await apply_actions(db, r.id)) for r in reports]
    assert applied == [2, 2, 1, 0]
    blobs = [r.dedup_result["supersede_apply"] for r in reports]
    assert [b["details"]["budget_cut"] for b in blobs] == [True, True, False, False]
    assert blobs[2]["details"]["no_action_reasons"] == {"below_floor": 1}
    assert blobs[3]["details"]["candidates_considered"] == 1
    assert blobs[3]["details"]["no_action_reasons"] == {"below_floor": 1}
    states = [parse_state(r.coverage_state) for r in reports]
    assert states[0].apply.position == (pairs[1][0].created_at, pairs[1][0].id)
    assert states[1].apply.position == (pairs[3][0].created_at, pairs[3][0].id)
    assert states[2].apply == LaneEntry(passes=1)
    assert states[3].apply == LaneEntry(passes=2)
    assert blobs[2]["details"]["coverage"]["sweep"]["pass_completed"] is True
    assert reports[0].coverage_state["supersede_apply"]["sweep"]["passes"] == 0
    assert [r.status for r in reports] == ["completed"] * 4
    assert (await candidate_column(db, refused.id))[0] is False


@pytest.mark.asyncio
async def test_switched_off_the_run_leaves_no_trace(db_session):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())

    report = await run_sleep(db, s, started_at=utcnow() - timedelta(hours=1), enabled=False)

    assert await apply_actions(db, report.id) == []
    assert "supersede_apply" not in (report.dedup_result or {})
    assert report.coverage_state is None
    assert await edge_row(db, s.owner, newer.id, older.id) is None
    assert (await candidate_column(db, newer.id))[0] is False


@pytest.mark.asyncio
async def test_changes_since_lists_the_applied_link(db_session):
    from services.memory_listing import changes_since

    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    since = utcnow() - timedelta(minutes=1)
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    await run_phase(db, s, report)
    edge = await edge_row(db, s.owner, newer.id, older.id)

    page = await changes_since(db, context_id=s.ctx.id, owner_user_id=None, since=since, until=None)

    superseded = [c for c in page.changes if c.kind == "superseded"]
    assert [(c.memory_id, c.superseded_by, c.at) for c in superseded] == [
        (older.id, newer.id, edge.created_at)
    ]


# ------------------------------------------------- shadow-merge undo guard


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [False, True], ids=["delete", "restore"])
async def test_a_shadow_merge_undo_leaves_an_applied_link_standing(db_session, prior):
    """An older dedup shadow merge recorded the pair; its edge was deleted and
    the pair applied since. Undoing that shadow merge must leave the applied
    link as it is; a NULL-metadata shadow edge is still reverted."""
    from services.sleep.undo import ShadowEdgeRevert, revert_shadow_merge_edge

    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    await run_phase(db, s, report)
    applied = await edge_row(db, s.owner, newer.id, older.id)
    prior_edge = (
        {
            "edge_type": "neural_association",
            "origin": "hebbian",
            "weight": 0.2,
            "confidence": 1.0,
            "edge_metadata": None,
        }
        if prior
        else None
    )

    outcome = await revert_shadow_merge_edge(
        db, user_id=s.owner, winner_id=newer.id, loser_id=older.id, prior_edge=prior_edge
    )

    assert outcome is ShadowEdgeRevert.BLOCKED_BY_NEWER_STATE
    assert await edge_row(db, s.owner, newer.id, older.id) == applied

    # A dedup shadow edge (NULL metadata) on another pair is still reverted.
    shadow_newer, shadow_older = await verified_pair(db, s, created_at=_base())
    await add_edge(db, s, shadow_newer, shadow_older, "supersedes", origin="semantic")
    outcome = await revert_shadow_merge_edge(
        db,
        user_id=s.owner,
        winner_id=shadow_newer.id,
        loser_id=shadow_older.id,
        prior_edge=prior_edge,
    )
    assert outcome is ShadowEdgeRevert.RESTORED
    after = await edge_row(db, s.owner, shadow_newer.id, shadow_older.id)
    if prior:
        assert (after.edge_type, after.origin, after.weight) == (
            "neural_association",
            "hebbian",
            0.2,
        )
    else:
        assert after is None


# ------------------------------------------------------------------ rollback


async def _undo(db, s, action) -> dict:
    from services.sleep.supersede_apply import undo_apply_supersede

    summary: dict = {}
    await undo_apply_supersede(db, action, SimpleNamespace(user_id=s.owner), summary)
    await db.flush()
    return summary


@pytest.mark.asyncio
async def test_rollback_removes_the_link_and_restores_the_suggestion_once(db_session):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    newer_id, older_id = newer.id, older.id
    stored = dict(newer.supersede_candidate)
    report = await new_report(db, s, utcnow() - timedelta(hours=3))
    await run_phase(db, s, report)
    (action,) = await apply_actions(db, report.id)

    summary = await _undo(db, s, action)

    assert summary == {"supersede_links_reversed": 1, "supersede_candidates_restored": 1}
    assert await edge_row(db, s.owner, newer_id, older_id) is None
    _, _, restored = await candidate_column(db, newer_id)
    assert restored == {**stored, "sleep_apply_reverted": str(report.id)}

    # Sleep leaves the restored suggestion alone ...
    again = await new_report(db, s, utcnow() - timedelta(hours=2))
    _, result = await run_phase(db, s, again)
    assert result.details["candidates_considered"] == 0
    assert await edge_row(db, s.owner, newer_id, older_id) is None

    # ... until the pair is detected again (a fresh dict without the key).
    await db.execute(
        update(Memory).where(Memory.id == newer_id).values(supersede_candidate=dict(stored))
    )
    third = await new_report(db, s, utcnow() - timedelta(hours=1))
    _, result = await run_phase(db, s, third)
    assert result.details["applied"] == 1
    assert (await edge_row(db, s.owner, newer_id, older_id)).edge_metadata == {
        "source": "sleep_supersede_apply",
        "report_id": str(third.id),
    }


@pytest.mark.asyncio
async def test_rollback_puts_a_retyped_edge_back(db_session):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    newer_id, older_id = newer.id, older.id
    await add_edge(
        db,
        s,
        newer,
        older,
        "neural_association",
        origin="hebbian",
        weight=0.3,
        metadata={"source": "tag_cooccurrence"},
    )
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    await run_phase(db, s, report)
    (action,) = await apply_actions(db, report.id)

    summary = await _undo(db, s, action)

    assert summary["supersede_links_reversed"] == 1
    edge = await edge_row(db, s.owner, newer_id, older_id)
    assert (edge.edge_type, edge.origin, edge.weight, edge.edge_metadata) == (
        "neural_association",
        "hebbian",
        0.3,
        {"source": "tag_cooccurrence"},
    )


@pytest.mark.asyncio
async def test_rollback_keeps_a_link_a_person_declared_since(db_session):
    from services.edge_service import create_declared_edge

    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    newer_id, older_id = newer.id, older.id
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    await run_phase(db, s, report)
    (action,) = await apply_actions(db, report.id)
    await create_declared_edge(
        db,
        user_id=s.owner,
        source_id=newer_id,
        target_id=older_id,
        edge_type="supersedes",
        weight=1.0,
        confidence=1.0,
        workspace_id=s.ws_id,
        context_id=s.ctx_id,
        overwrite=True,
    )
    await db.flush()

    summary = await _undo(db, s, action)

    assert summary == {"supersede_links_kept": 1}
    edge = await edge_row(db, s.owner, newer_id, older_id)
    assert (edge.edge_type, edge.origin) == ("supersedes", "declared")
    assert (await candidate_column(db, newer_id))[0] is True


@pytest.mark.asyncio
async def test_rollback_keeps_a_suggestion_that_landed_since(db_session):
    db = db_session
    s = await scope(db)
    newer, older = await verified_pair(db, s, created_at=_base())
    newer_id, older_id = newer.id, older.id
    report = await new_report(db, s, utcnow() - timedelta(hours=1))
    await run_phase(db, s, report)
    (action,) = await apply_actions(db, report.id)
    fresh = {"memory_id": str(uuid4()), "similarity": 0.88, "detected_at": "2026-02-01T00:00:00Z"}
    await db.execute(update(Memory).where(Memory.id == newer_id).values(supersede_candidate=fresh))

    summary = await _undo(db, s, action)

    assert summary == {"supersede_links_reversed": 1}
    assert await edge_row(db, s.owner, newer_id, older_id) is None
    assert (await candidate_column(db, newer_id))[2] == fresh


@pytest.mark.asyncio
async def test_rollback_sleep_run_end_to_end(committed):
    """The one committed test: an orchestrator run with the switch on, then
    ``handle_rollback_sleep_run``."""
    from mcp_server.tools.sleep import handle_rollback_sleep_run
    from models.sleep import SleepReport

    db = committed.db
    s = await scope(db, ids=committed)
    pairs = [await verified_pair(db, s, created_at=_base() + timedelta(hours=i)) for i in range(2)]
    ids = [(newer.id, older.id, dict(newer.supersede_candidate)) for newer, older in pairs]
    with patch("services.operation_history_writer.record_operation"):
        report = await run_sleep(db, s, started_at=utcnow() - timedelta(hours=1))
    report_id = report.id
    assert report.dedup_result["supersede_apply"]["details"]["applied"] == 2
    assert report.coverage_state["supersede_apply"]["sweep"]["passes"] == 1

    async def _get_db():
        yield db

    with (
        patch("db.base.get_db", new=_get_db),
        patch(
            "mcp_server.tools.sleep._authorize_report_rollback",
            new=AsyncMock(return_value=None),
        ),
        patch("mcp_server.tools.sleep._log_tool_usage", new=AsyncMock()),
        patch("services.operation_history_writer.record_operation"),
    ):
        response = await handle_rollback_sleep_run({"report_id": str(report_id)}, s.owner, None)
    data = json.loads(response[0].text)

    assert data["status"] == "rolled_back", data
    summary = data["rollback_summary"]
    assert summary["errors"] == []
    assert summary["supersede_links_reversed"] == 2
    assert summary["supersede_candidates_restored"] == 2
    assert "supersede_links_kept" not in summary
    status = (
        await db.execute(text("SELECT status FROM sleep_reports WHERE id = :id"), {"id": report_id})
    ).scalar_one()
    assert status == "rolled_back"
    assert (await db.get(SleepReport, report_id)) is not None
    for newer_id, older_id, stored in ids:
        assert await edge_row(db, s.owner, newer_id, older_id) is None
        _, _, restored = await candidate_column(db, newer_id)
        assert restored == {**stored, "sleep_apply_reverted": str(report_id)}
