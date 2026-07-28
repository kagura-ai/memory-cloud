"""#1209: per-merge undo service.

Pins the correction-loop contract: one dedup merge is individually
reversible (row restore + vector re-embed + audited ``undo_merge`` action on
the same report), with a stable error code for every non-restorable state —
including the retention-purged case, whose message names the setting that
bounds reversibility.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.undo import UndoMergeError, undo_merge_action


def _action(*, action_type: str = "merge", phase: str = "dedup_merge") -> MagicMock:
    action = MagicMock()
    action.id = 42
    action.action_type = action_type
    action.phase = phase
    action.memory_id = uuid4()  # winner
    action.target_id = uuid4()  # loser
    action.report_id = uuid4()
    return action


def _report() -> MagicMock:
    report = MagicMock()
    report.user_id = "admin-user"
    report.workspace_id = uuid4()
    report.context_id = None  # default embedding info path (no config row)
    return report


def _db(first_row, memory=None) -> AsyncMock:
    """AsyncMock db: 1st execute -> (action, report) join, 2nd -> memory,
    3rd -> the restore UPDATE."""
    db = AsyncMock()
    join_result = MagicMock()
    join_result.first.return_value = first_row
    mem_result = MagicMock()
    mem_result.scalar_one_or_none.return_value = memory
    db.execute.side_effect = [join_result, mem_result, MagicMock()]
    return db


@pytest.mark.asyncio
async def test_action_not_found() -> None:
    db = _db(None)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "action_not_found"


@pytest.mark.asyncio
async def test_not_a_merge() -> None:
    db = _db((_action(action_type="promote"), _report()))
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "not_a_merge"


@pytest.mark.asyncio
async def test_purged_loser_names_the_retention_setting() -> None:
    """Reversibility is bounded by the declared window — the error must say
    which setting bounded it."""
    db = _db((_action(), _report()), memory=None)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "memory_purged"
    assert "sleep_merge_retention_days" in exc.value.message


@pytest.mark.asyncio
async def test_already_restored() -> None:
    loser = MagicMock()
    loser.deleted_at = None
    db = _db((_action(), _report()), memory=loser)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "already_restored"


@pytest.mark.asyncio
async def test_refuses_non_sleep_deletion() -> None:
    loser = MagicMock()
    loser.deleted_at = MagicMock()
    loser.deleted_by = "user_delete"
    db = _db((_action(), _report()), memory=loser)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "not_merge_deleted"


@pytest.mark.asyncio
async def test_happy_path_restores_reembeds_and_audits() -> None:
    action = _action()
    report = _report()
    loser = MagicMock()
    loser.id = action.target_id
    loser.deleted_at = MagicMock()
    loser.deleted_by = "sleep_maintenance"
    db = _db((action, report), memory=loser)

    with (
        patch("services.sleep.undo.re_embed_memory_to_qdrant", new=AsyncMock()) as re_embed,
        patch("services.embedding_service.EmbeddingService"),
    ):
        summary = await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert summary["restored_memory_id"] == str(action.target_id)
    assert summary["winner_id"] == str(action.memory_id)
    assert summary["undone_action_id"] == 42

    re_embed.assert_awaited_once()

    # The undo is audited as an undo_merge action on the SAME report.
    added = db.add.call_args.args[0]
    assert added.action_type == "undo_merge"
    assert added.report_id == action.report_id
    assert added.details["undone_action_id"] == 42
    assert added.details["undone_by"] == "admin-user"
    db.commit.assert_awaited_once()


# ------------------------------------------------------------ shadow merges


def _shadow_action(prior_edge=None) -> MagicMock:
    action = _action()
    action.details = {"mode": "shadow", "prior_edge": prior_edge}
    return action


def _shadow_db(
    first_row,
    revert_rowcount: int,
    current_edge_type: str | None = None,
) -> AsyncMock:
    """AsyncMock db: 1st execute -> (action, report) join, 2nd -> the edge
    revert (UPDATE restore or verified DELETE) with the given rowcount.

    #1450: when the revert matches no row, the helper reads the edge back to
    tell "already undone" from "a later writer got there first", so a third
    execute is served — ``current_edge_type`` is what that read finds (None =
    no edge at all).
    """
    db = AsyncMock()
    join_result = MagicMock()
    join_result.first.return_value = first_row
    revert_result = MagicMock()
    revert_result.rowcount = revert_rowcount
    current_result = MagicMock()
    current_result.scalar_one_or_none.return_value = current_edge_type
    db.execute.side_effect = [join_result, revert_result, current_result]
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_shadow_undo_deletes_created_edge_and_audits() -> None:
    """#1208: a shadow merge never deleted the loser — undo means removing
    the supersedes edge, audited as undo_merge with mode=shadow."""
    action = _shadow_action(prior_edge=None)
    db = _shadow_db((action, _report()), revert_rowcount=1)

    summary = await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert summary["restored_memory_id"] == str(action.target_id)
    assert summary["undone_action_id"] == 42
    added = db.add.call_args.args[0]
    assert added.action_type == "undo_merge"
    assert added.details["mode"] == "shadow"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_undo_restores_prior_edge_state() -> None:
    """When the shadow merge retyped a pre-existing edge, undo must issue an
    UPDATE restoring the snapshot, not a DELETE (the association survives)."""
    from sqlalchemy.sql.dml import Update

    prior = {
        "edge_type": "neural_association",
        "origin": "hebbian",
        "weight": 0.4,
        "confidence": 0.9,
        "edge_metadata": None,
    }
    action = _shadow_action(prior_edge=prior)
    db = _shadow_db((action, _report()), revert_rowcount=1)

    await undo_merge_action(db, 42, acting_user_id="admin-user")

    revert_stmt = db.execute.call_args_list[1].args[0]
    assert isinstance(revert_stmt, Update)


@pytest.mark.asyncio
async def test_shadow_undo_without_snapshot_deletes() -> None:
    """No snapshot means the merge CREATED the edge — undo deletes it."""
    from sqlalchemy.sql.dml import Delete

    action = _shadow_action(prior_edge=None)
    db = _shadow_db((action, _report()), revert_rowcount=1)

    await undo_merge_action(db, 42, acting_user_id="admin-user")

    revert_stmt = db.execute.call_args_list[1].args[0]
    assert isinstance(revert_stmt, Delete)


@pytest.mark.asyncio
async def test_shadow_undo_gone_edge_is_already_restored() -> None:
    """Edge already gone → benign no-op, stable error, nothing audited."""
    action = _shadow_action(prior_edge=None)
    db = _shadow_db((action, _report()), revert_rowcount=0, current_edge_type=None)

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "already_restored"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_undo_retyped_edge_is_not_already_restored() -> None:
    """#1450: a later writer's edge is NOT "already undone".

    Both matched zero rows and so shared one ``already_restored`` code, but the
    outcomes differ: nothing to do vs. the merge is still in effect and someone
    has to look at the newer edge. Same 409, distinguishable ``error_code``.
    """
    action = _shadow_action(prior_edge=None)
    db = _shadow_db(
        (action, _report()),
        revert_rowcount=0,
        current_edge_type="neural_association",
    )

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "edge_retyped"
    assert "still in effect" in exc.value.message
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_undo_snapshot_already_restored_is_benign() -> None:
    """#1450: with a snapshot, "already undone" means the edge is back at the
    snapshot's type — that must not read as a later writer's interference."""
    prior = {"edge_type": "neural_association", "origin": "hebbian"}
    action = _shadow_action(prior_edge=prior)
    db = _shadow_db(
        (action, _report()),
        revert_rowcount=0,
        current_edge_type="neural_association",
    )

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "already_restored"


@pytest.mark.asyncio
async def test_shadow_undo_snapshot_overwritten_is_blocked() -> None:
    """#1450: with a snapshot, an edge of some OTHER type means the snapshot
    can no longer be restored from here — blocked, not already-undone."""
    prior = {"edge_type": "neural_association", "origin": "hebbian"}
    action = _shadow_action(prior_edge=prior)
    db = _shadow_db(
        (action, _report()),
        revert_rowcount=0,
        current_edge_type="contradicts",
    )

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "edge_retyped"
