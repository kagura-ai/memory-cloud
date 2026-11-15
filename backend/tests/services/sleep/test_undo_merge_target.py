"""#2838: ``undo_merge_action`` for a named report (the operator's undo).

Two keyword-only parameters, both ``None`` by default:

- without them the action is found through a report the caller owns, and no
  ``audit_logs`` row is written (the own-account route);
- with ``target_report_id`` the action is found by its id and that report id,
  with no caller filter, and both success paths write a ``sleep_merge_undo``
  audit row before their one commit; ``actor_email`` is then required.

``undone_by`` is the caller's id, or the fixed string ``operator`` for an
operator principal.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from models.auth import AuditLog
from models.operation_history import OperationHistory
from models.sleep import SleepAction
from services.sleep.undo import UndoMergeError, undo_merge_action

OPERATOR_ID = "operator:ops@example.com"
ADMIN_ID = "admin-1001"
TARGET = "target-user-1"


def _action(*, shadow: bool = False) -> MagicMock:
    action = MagicMock()
    action.id = 42
    action.action_type = "merge"
    action.phase = "dedup_merge"
    action.memory_id = uuid4()  # winner
    action.target_id = uuid4()  # loser
    action.report_id = uuid4()
    action.details = {"mode": "shadow", "prior_edge": None} if shadow else {}
    return action


def _report(action: MagicMock) -> MagicMock:
    report = MagicMock()
    report.id = action.report_id
    report.user_id = TARGET
    report.workspace_id = uuid4()
    report.context_id = None
    return report


@pytest.fixture(autouse=True)
def point_writer_lock():
    with patch("services.sleep.undo.hold_point_writer_lock", new=AsyncMock()) as lock:
        yield lock


def _events(db: AsyncMock) -> list:
    """Record ``add`` and ``commit`` on the session in order."""
    events: list = []
    db.add = MagicMock(side_effect=lambda obj: events.append(obj))
    db.commit.side_effect = lambda: events.append("commit")
    return events


def _tombstone_db(first_row, memory=None) -> AsyncMock:
    """1st execute -> (action, report) join, 2nd -> memory, 3rd -> restore."""
    db = AsyncMock()
    join_result = MagicMock()
    join_result.first.return_value = first_row
    mem_result = MagicMock()
    mem_result.scalar_one_or_none.return_value = memory
    db.execute.side_effect = [join_result, mem_result, MagicMock()]
    return db


def _shadow_db(first_row, revert_rowcount: int = 1) -> AsyncMock:
    """1st execute -> (action, report) join, 2nd -> the edge revert."""
    db = AsyncMock()
    join_result = MagicMock()
    join_result.first.return_value = first_row
    revert_result = MagicMock()
    revert_result.rowcount = revert_rowcount
    current_result = MagicMock()
    current_result.scalar_one_or_none.return_value = None
    db.execute.side_effect = [join_result, revert_result, current_result]
    return db


def _loser(action: MagicMock) -> MagicMock:
    loser = MagicMock()
    loser.id = action.target_id
    loser.deleted_at = MagicMock()
    loser.deleted_by = "sleep_maintenance"
    return loser


def _lookup_sql(db: AsyncMock) -> tuple[str, list]:
    """The WHERE clause of the action lookup (the join's ON is left out) and
    its bound values."""
    stmt = db.execute.await_args_list[0].args[0]
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = " ".join(str(compiled).split())
    assert "JOIN sleep_reports ON sleep_actions.report_id = sleep_reports.id" in sql
    return sql.split(" WHERE ", 1)[1], list(compiled.params.values())


async def _undo(db, *, shadow: bool, **kwargs):
    if shadow:
        return await undo_merge_action(db, 42, **kwargs)
    with (
        patch("services.sleep.undo.re_embed_memory_to_qdrant", new=AsyncMock()),
        patch("services.embedding_service.EmbeddingService"),
    ):
        return await undo_merge_action(db, 42, **kwargs)


def _db_for(action: MagicMock, report: MagicMock, *, shadow: bool) -> AsyncMock:
    if shadow:
        return _shadow_db((action, report))
    return _tombstone_db((action, report), memory=_loser(action))


# ------------------------------------------------------------------ lookup


@pytest.mark.asyncio
async def test_target_lookup_is_by_action_and_report_without_a_caller_filter() -> None:
    db = _tombstone_db(None)
    report_id = uuid4()

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(
            db, 42, acting_user_id=OPERATOR_ID, target_report_id=report_id, actor_email=OPERATOR_ID
        )

    sql, params = _lookup_sql(db)
    assert "sleep_actions.id =" in sql
    assert "sleep_actions.report_id =" in sql
    assert "sleep_reports.user_id =" not in sql
    assert set(params) == {42, report_id}
    assert OPERATOR_ID not in params
    assert exc.value.code == "action_not_found"
    assert exc.value.message == f"Sleep action 42 not found in sleep report {report_id}."
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_own_account_lookup_keeps_the_caller_filter() -> None:
    db = _tombstone_db(None)

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id=ADMIN_ID)

    sql, params = _lookup_sql(db)
    assert "sleep_actions.id =" in sql
    assert "sleep_reports.user_id =" in sql
    assert "sleep_actions.report_id =" not in sql
    assert set(params) == {42, ADMIN_ID}
    assert exc.value.code == "action_not_found"
    assert exc.value.message == "Sleep action 42 not found or not owned by you."


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_email", [None, ""])
async def test_target_mode_requires_the_actor_email(actor_email) -> None:
    db = _tombstone_db(None)

    with pytest.raises(ValueError, match="actor_email"):
        await undo_merge_action(
            db, 42, acting_user_id=ADMIN_ID, target_report_id=uuid4(), actor_email=actor_email
        )

    db.execute.assert_not_awaited()


# ------------------------------------------------------------------ writes


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True], ids=["tombstone", "shadow"])
async def test_target_undo_writes_the_audit_row_before_the_one_commit(shadow: bool) -> None:
    action = _action(shadow=shadow)
    report = _report(action)
    db = _db_for(action, report, shadow=shadow)
    events = _events(db)

    summary = await _undo(
        db,
        shadow=shadow,
        acting_user_id=OPERATOR_ID,
        target_report_id=action.report_id,
        actor_email=OPERATOR_ID,
    )

    assert summary == {
        "restored_memory_id": str(action.target_id),
        "winner_id": str(action.memory_id),
        "report_id": str(action.report_id),
        "undone_action_id": 42,
    }
    assert events[-1] == "commit" and events.count("commit") == 1
    [audit] = [e for e in events if isinstance(e, AuditLog)]
    assert audit.action == "sleep_merge_undo"
    assert audit.user_id == OPERATOR_ID
    assert audit.user_email == OPERATOR_ID
    assert audit.resource == f"sleep_report:{action.report_id}"
    assert audit.user_metadata == {
        "target_user_id": TARGET,
        "workspace_id": str(report.workspace_id),
        "context_id": None,
        "report_id": str(action.report_id),
        "action_id": 42,
        "restored_memory_id": str(action.target_id),
        "winner_id": str(action.memory_id),
    }
    # The actor is in the actor columns only, never in the metadata.
    assert "ops@example.com" not in json.dumps(audit.user_metadata)
    [undo_action] = [e for e in events if isinstance(e, SleepAction)]
    assert undo_action.action_type == "undo_merge"
    assert undo_action.report_id == action.report_id
    [history] = [e for e in events if isinstance(e, OperationHistory)]
    assert history.operation == "sleep.undo"


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True], ids=["tombstone", "shadow"])
async def test_own_account_undo_writes_no_audit_row(shadow: bool) -> None:
    action = _action(shadow=shadow)
    report = _report(action)
    report.user_id = ADMIN_ID
    db = _db_for(action, report, shadow=shadow)
    events = _events(db)

    await _undo(db, shadow=shadow, acting_user_id=ADMIN_ID)

    assert not [e for e in events if isinstance(e, AuditLog)]
    [undo_action] = [e for e in events if isinstance(e, SleepAction)]
    assert undo_action.details["undone_by"] == ADMIN_ID
    assert events.count("commit") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True], ids=["tombstone", "shadow"])
async def test_an_operators_undone_by_is_the_fixed_string(shadow: bool) -> None:
    action = _action(shadow=shadow)
    db = _db_for(action, _report(action), shadow=shadow)
    events = _events(db)

    await _undo(
        db,
        shadow=shadow,
        acting_user_id=OPERATOR_ID,
        target_report_id=action.report_id,
        actor_email=OPERATOR_ID,
    )

    [undo_action] = [e for e in events if isinstance(e, SleepAction)]
    assert undo_action.details["undone_by"] == "operator"
    assert "ops@example.com" not in json.dumps(undo_action.details)
    assert undo_action.details["undone_action_id"] == 42
    assert undo_action.details.get("mode") == ("shadow" if shadow else None)
    # The operation history keeps its own rule: no actor for an operator.
    [history] = [e for e in events if isinstance(e, OperationHistory)]
    assert history.actor_user_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow", [False, True], ids=["tombstone", "shadow"])
async def test_a_session_admins_undone_by_is_the_admins_id(shadow: bool) -> None:
    action = _action(shadow=shadow)
    db = _db_for(action, _report(action), shadow=shadow)
    events = _events(db)

    await _undo(
        db,
        shadow=shadow,
        acting_user_id=ADMIN_ID,
        target_report_id=action.report_id,
        actor_email="admin@example.com",
    )

    [undo_action] = [e for e in events if isinstance(e, SleepAction)]
    assert undo_action.details["undone_by"] == ADMIN_ID
    [audit] = [e for e in events if isinstance(e, AuditLog)]
    assert (audit.user_id, audit.user_email) == (ADMIN_ID, "admin@example.com")
    assert audit.user_metadata["target_user_id"] == TARGET


# ------------------------------------------------------------------ refusals


@pytest.mark.asyncio
async def test_a_refused_target_undo_writes_nothing() -> None:
    """Every later check is the existing one, and none of them adds a row."""
    action = _action()
    report = _report(action)
    already = MagicMock()
    already.deleted_at = None
    db = _tombstone_db((action, report), memory=already)
    events = _events(db)

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(
            db,
            42,
            acting_user_id=OPERATOR_ID,
            target_report_id=action.report_id,
            actor_email=OPERATOR_ID,
        )

    assert exc.value.code == "already_restored"
    assert events == []


@pytest.mark.asyncio
async def test_target_undo_restores_for_the_reports_user_not_the_caller() -> None:
    action = _action(shadow=True)
    report = _report(action)
    db = _shadow_db((action, report))
    _events(db)

    await undo_merge_action(
        db,
        42,
        acting_user_id=OPERATOR_ID,
        target_report_id=action.report_id,
        actor_email=OPERATOR_ID,
    )

    revert = db.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect())
    assert TARGET in revert.params.values()
    assert OPERATOR_ID not in revert.params.values()
