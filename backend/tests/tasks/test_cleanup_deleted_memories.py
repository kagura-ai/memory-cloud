"""#1521: the daily 30-day cleanup must not purge tombstones a Sleep retention
lane owns, or ``sleep_merge_retention_days = 0`` ("retain forever") is a lie.

Real-DB tests: the lane split is a WHERE clause over ``deleted_by`` and the
context's liveness, which a mocked session cannot exercise.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from models.auth import Context, Workspace
from models.memory import Memory
from tasks.neural_tasks import cleanup_deleted_memories_task
from tests.tasks.conftest import mock_get_db_factory
from utils.datetime import utcnow


def _tombstone(user: str, deleted_by: str | None, *, age_days: int, context_id=None) -> Memory:
    return Memory(
        id=uuid4(),
        user_id=user,
        summary=f"tombstone {deleted_by}",
        content="c",
        type="note",
        client="pytest",
        scope="working",
        context_id=context_id,
        deleted_at=utcnow() - timedelta(days=age_days),
        deleted_by=deleted_by,
    )


async def _run_cleanup(db_session, *, sleep_enabled: bool) -> None:
    # The task commits; keep the fixture transaction so the rows roll back.
    db_session.commit = AsyncMock(side_effect=db_session.flush)
    with (
        patch("tasks.neural_tasks.get_db", mock_get_db_factory(db_session)),
        patch.dict("os.environ", {"SLEEP_ENABLED": "true" if sleep_enabled else "false"}),
    ):
        await cleanup_deleted_memories_task()


async def _remaining(db_session, user: str) -> set:
    rows = await db_session.execute(select(Memory.id).where(Memory.user_id == user))
    return set(rows.scalars().all())


@pytest.fixture
async def contexts(db_session):
    """A live context and a soft-deleted one, both with a real workspace."""
    user = f"cleanup-user-{uuid4()}"
    ws_id = uuid4()
    db_session.add(Workspace(id=ws_id, name="ws", owner_user_id=user))
    await db_session.flush()
    live_id, dead_id = uuid4(), uuid4()
    db_session.add(Context(id=live_id, workspace_id=ws_id, name="live"))
    db_session.add(Context(id=dead_id, workspace_id=ws_id, name="dead", deleted_at=utcnow()))
    await db_session.flush()
    return user, live_id, dead_id


class TestSleepEnabled:
    async def test_lane_owned_tombstones_survive_and_residue_is_purged(self, db_session, contexts):
        user, live_id, dead_id = contexts
        merge_loser = _tombstone(user, "sleep_maintenance", age_days=31, context_id=live_id)
        archived = _tombstone(user, "sleep_consolidation", age_days=31, context_id=live_id)
        forgotten_live = _tombstone(user, "some-user-sub", age_days=31, context_id=live_id)
        legacy_null_live = _tombstone(user, None, age_days=31, context_id=live_id)
        forgotten_dead = _tombstone(user, "some-user-sub", age_days=31, context_id=dead_id)
        no_context = _tombstone(user, "some-user-sub", age_days=31, context_id=None)
        db_session.add_all(
            [merge_loser, archived, forgotten_live, legacy_null_live, forgotten_dead, no_context]
        )
        await db_session.flush()

        await _run_cleanup(db_session, sleep_enabled=True)

        remaining = await _remaining(db_session, user)
        # Owned by sleep_merge_retention_days / sleep_forget_retention_days.
        assert {merge_loser.id, archived.id, forgotten_live.id, legacy_null_live.id} <= remaining
        # Owned by no lane: the residue the 30-day sweep still handles.
        assert forgotten_dead.id not in remaining
        assert no_context.id not in remaining

    async def test_sleep_tombstone_in_a_dead_context_is_still_lane_owned(
        self, db_session, contexts
    ):
        """The merge lane's purge does not check context liveness, so a sleep
        tombstone stays the merge window's regardless of its context."""
        user, _live_id, dead_id = contexts
        loser = _tombstone(user, "sleep_maintenance", age_days=40, context_id=dead_id)
        db_session.add(loser)
        await db_session.flush()

        await _run_cleanup(db_session, sleep_enabled=True)

        assert loser.id in await _remaining(db_session, user)

    async def test_young_residue_is_not_purged(self, db_session, contexts):
        user, _live_id, dead_id = contexts
        young = _tombstone(user, "some-user-sub", age_days=29, context_id=dead_id)
        db_session.add(young)
        await db_session.flush()

        await _run_cleanup(db_session, sleep_enabled=True)

        assert young.id in await _remaining(db_session, user)


class TestSleepDisabled:
    async def test_legacy_sweep_purges_every_old_tombstone(self, db_session, contexts):
        """Nothing else would ever purge with Sleep off — the 30-day sweep must
        keep covering every tombstone class, exactly as before #1521."""
        user, live_id, _dead_id = contexts
        merge_loser = _tombstone(user, "sleep_maintenance", age_days=31, context_id=live_id)
        forgotten = _tombstone(user, "some-user-sub", age_days=31, context_id=live_id)
        young = _tombstone(user, "sleep_maintenance", age_days=5, context_id=live_id)
        db_session.add_all([merge_loser, forgotten, young])
        await db_session.flush()

        await _run_cleanup(db_session, sleep_enabled=False)

        assert await _remaining(db_session, user) == {young.id}
