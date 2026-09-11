"""#1521: the platform-wide tombstone sweep has a configurable window and
applies to every tombstone class, so the Sleep retention settings can be
honest about what ``0`` means (no additional purge, not "retain forever").

Real-DB tests: the sweep is a set-based DELETE whose predicate is the thing
under test.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.constants import (
    CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT,
    CLEANUP_TOMBSTONE_RETENTION_DAYS_ENV,
)
from models.auth import Context, Workspace
from models.memory import Memory
from tasks.neural_tasks import cleanup_deleted_memories_task
from tests.tasks.conftest import mock_get_db_factory
from utils.datetime import utcnow


def _tombstone(
    user: str, deleted_by: str | None, *, age_days: int, context_id: UUID | None = None
) -> Memory:
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


async def _run_cleanup(db_session: AsyncSession, *, window: str | None) -> None:
    """Run the task against the fixture session with the window env set.

    The task commits; commit is redirected to flush so the fixture's
    transaction still rolls the rows back.
    """
    db_session.commit = AsyncMock(side_effect=db_session.flush)  # type: ignore[method-assign]
    env = {} if window is None else {CLEANUP_TOMBSTONE_RETENTION_DAYS_ENV: window}
    with (
        patch("tasks.neural_tasks.get_db", mock_get_db_factory(db_session)),
        patch.dict("os.environ", env, clear=False),
    ):
        if window is None:
            # Make sure a developer's shell value does not leak into the test.
            import os

            os.environ.pop(CLEANUP_TOMBSTONE_RETENTION_DAYS_ENV, None)
        await cleanup_deleted_memories_task()


async def _remaining(db_session: AsyncSession, user: str) -> set[UUID]:
    rows = await db_session.execute(select(Memory.id).where(Memory.user_id == user))
    return set(rows.scalars().all())


@pytest.fixture
async def contexts(db_session: AsyncSession) -> tuple[str, UUID, UUID]:
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


class TestDefaultWindow:
    async def test_every_tombstone_class_past_the_window_is_purged(
        self, db_session: AsyncSession, contexts: tuple[str, UUID, UUID]
    ) -> None:
        """The sweep is the outer bound for merge losers, archives, forget()
        rows and dead-context rows alike — no deleted_by or context exemption."""
        user, live_id, dead_id = contexts
        past = CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT + 1
        rows = [
            _tombstone(user, "sleep_maintenance", age_days=past, context_id=live_id),
            _tombstone(user, "sleep_consolidation", age_days=past, context_id=live_id),
            _tombstone(user, "some-user-sub", age_days=past, context_id=live_id),
            _tombstone(user, None, age_days=past, context_id=live_id),
            _tombstone(user, "some-user-sub", age_days=past, context_id=dead_id),
            _tombstone(user, "some-user-sub", age_days=past, context_id=None),
        ]
        db_session.add_all(rows)
        await db_session.flush()

        await _run_cleanup(db_session, window=None)

        assert await _remaining(db_session, user) == set()

    async def test_rows_inside_the_window_survive(
        self, db_session: AsyncSession, contexts: tuple[str, UUID, UUID]
    ) -> None:
        user, live_id, dead_id = contexts
        inside = CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT - 1
        rows = [
            _tombstone(user, "sleep_maintenance", age_days=inside, context_id=live_id),
            _tombstone(user, "some-user-sub", age_days=inside, context_id=dead_id),
        ]
        live = Memory(
            id=uuid4(),
            user_id=user,
            summary="live",
            content="c",
            type="note",
            client="pytest",
            scope="working",
            context_id=live_id,
        )
        db_session.add_all([*rows, live])
        await db_session.flush()

        await _run_cleanup(db_session, window=None)

        assert await _remaining(db_session, user) == {rows[0].id, rows[1].id, live.id}


class TestConfiguredWindow:
    async def test_zero_disables_the_sweep(
        self, db_session: AsyncSession, contexts: tuple[str, UUID, UUID]
    ) -> None:
        user, live_id, _dead_id = contexts
        ancient = _tombstone(user, "sleep_maintenance", age_days=400, context_id=live_id)
        db_session.add(ancient)
        await db_session.flush()

        await _run_cleanup(db_session, window="0")

        assert ancient.id in await _remaining(db_session, user)

    async def test_shorter_window_purges_sooner(
        self, db_session: AsyncSession, contexts: tuple[str, UUID, UUID]
    ) -> None:
        user, live_id, _dead_id = contexts
        eight_days = _tombstone(user, "some-user-sub", age_days=8, context_id=live_id)
        six_days = _tombstone(user, "some-user-sub", age_days=6, context_id=live_id)
        db_session.add_all([eight_days, six_days])
        await db_session.flush()

        await _run_cleanup(db_session, window="7")

        assert await _remaining(db_session, user) == {six_days.id}

    async def test_invalid_value_falls_back_to_the_default(
        self, db_session: AsyncSession, contexts: tuple[str, UUID, UUID]
    ) -> None:
        """A typo must not become an aggressive purge (or a silent no-op)."""
        user, live_id, _dead_id = contexts
        past = _tombstone(
            user,
            "some-user-sub",
            age_days=CLEANUP_TOMBSTONE_RETENTION_DAYS_DEFAULT + 1,
            context_id=live_id,
        )
        recent = _tombstone(user, "some-user-sub", age_days=2, context_id=live_id)
        db_session.add_all([past, recent])
        await db_session.flush()

        await _run_cleanup(db_session, window="thirty")

        assert await _remaining(db_session, user) == {recent.id}
