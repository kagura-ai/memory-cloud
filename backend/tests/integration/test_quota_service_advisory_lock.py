"""Integration tests for the workspace-creation advisory lock (#674 sub-C / #677).

These tests exercise the real Postgres ``pg_advisory_xact_lock`` primitive
that ``QuotaService.check_workspace_creation_allowed`` acquires. Mocked
unit tests in ``tests/services/test_quota_service.py`` cannot reproduce
the TOCTOU race because they bypass transaction-level concurrency — the
very thing we are guarding against.

Three test layers:

1. **Deterministic race** — N >> cap parallel creates must yield exactly
   ``cap`` workspaces, never more. Positive proof the lock serializes
   read-then-write under contention.

2. **Negative control** — with the advisory lock monkeypatched to a
   no-op, the same parallel pattern reproduces ``count > cap``. Catches
   the regression where the lock implementation breaks but the positive
   test still passes by lucky scheduling: the negative control proves
   the test setup is genuinely racy when the safeguard is removed.

3. **Metric / log-field assertion** — under contention, at least one
   non-first session records ``lock_wait_ms > 0`` on the
   ``workspace_creation_denied`` log event. Confirms the lock actually
   contended; if every recorded wait is 0, the lock isn't really being
   acquired or every caller bypasses the wait.

The fixture grants ``workspace_slot_bonus = 2`` (cap = 3) and dispatches
20 parallel attempts on independent ``AsyncSession`` instances. Each
attempt runs ``check_workspace_creation_allowed`` + ``INSERT`` inside one
``session.begin()`` block — production callers must do the same for the
xact-scoped lock to extend across both, and the test mirrors that
contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import services.quota_service as quota_service_module
from models.auth import User, Workspace
from services.quota_service import QuotaService

_BONUS = 2
_CAP = 1 + _BONUS  # 3
_PARALLEL_ATTEMPTS = 20


def _new_workspace(owner: str) -> Workspace:
    """Build an active (non-soft-deleted) workspace owned by ``owner``."""
    return Workspace(
        id=uuid4(),
        name=f"toctou-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner,
        memory_limit=1000,
        daily_api_limit=500,
        weekly_api_limit=2500,
        deleted_at=None,
    )


async def _count_owned(session: AsyncSession, user_id: str) -> int:
    result = await session.execute(
        select(func.count(Workspace.id)).where(
            Workspace.owner_user_id == user_id,
            Workspace.deleted_at.is_(None),
        )
    )
    return int(result.scalar() or 0)


@pytest_asyncio.fixture(loop_scope="session")
async def user_with_cap_3(db_session: AsyncSession) -> AsyncIterator[str]:
    """Create a user with ``workspace_slot_bonus = 2`` (cap = 3) and no workspaces.

    Cleans up workspaces and the user after the test so repeated runs of
    the suite do not accumulate rows that would skew subsequent fixtures.
    """
    user_id = f"u_{uuid4().hex[:8]}"
    db_session.add(
        User(
            email=f"{user_id}@toctou.invalid",
            user_id=user_id,
            name="TOCTOU Test User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
            workspace_slot_bonus=_BONUS,
        )
    )
    await db_session.commit()

    yield user_id

    # Best-effort teardown — delete workspaces first to satisfy FKs.
    await db_session.execute(Workspace.__table__.delete().where(Workspace.owner_user_id == user_id))
    await db_session.execute(User.__table__.delete().where(User.user_id == user_id))
    await db_session.commit()


@pytest.fixture
def enforce_cap_on(monkeypatch):
    """Force ``settings.enforce_workspace_cap = True`` for the test scope.

    ``get_settings()`` returns a module-level singleton, so mutating its
    attribute is process-wide. monkeypatch restores the original value
    when the test exits, including on failure.
    """
    from config.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "enforce_workspace_cap", True)
    return settings


async def _attempt_one_create(
    session_maker: async_sessionmaker[AsyncSession],
    user_id: str,
) -> bool:
    """Try to create one workspace; return True iff the insert committed.

    The cap check and the insert run inside a single ``session.begin()``
    block so the xact-scoped advisory lock holds across both. That is
    the contract production callers must honor for the lock to fully
    serialize the read-then-write — this test mirrors that contract.
    """
    async with session_maker() as session:
        async with session.begin():
            quota = QuotaService(session)
            can, _ = await quota.check_workspace_creation_allowed(user_id)
            if can:
                session.add(_new_workspace(user_id))
                return True
            return False


class TestAdvisoryLockSerializesAtCap:
    """The lock must hold ``count <= cap`` under any level of concurrency."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_n_parallel_attempts_produce_exactly_cap(
        self, async_engine, user_with_cap_3, enforce_cap_on
    ):
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        results = await asyncio.gather(
            *[
                _attempt_one_create(session_maker, user_with_cap_3)
                for _ in range(_PARALLEL_ATTEMPTS)
            ],
            return_exceptions=True,
        )

        # Bool successes only; surface unexpected exceptions in the
        # assertion message rather than silently treating them as
        # successes or failures.
        unexpected = [r for r in results if not isinstance(r, bool)]
        assert not unexpected, f"unexpected exceptions from attempts: {unexpected!r}"

        successes = sum(1 for r in results if r is True)
        assert successes == _CAP, (
            f"expected exactly {_CAP} successful creates, got {successes} (results: {results!r})"
        )

        async with session_maker() as session:
            count = await _count_owned(session, user_with_cap_3)
        assert count == _CAP, f"final owned-workspace count is {count}, must equal cap={_CAP}"


class TestAdvisoryLockNegativeControl:
    """Without the lock, the same parallel pattern MUST reproduce ``count > cap``.

    Regression safeguard for the positive test above: if the production
    advisory lock silently breaks, the positive test could still pass
    by lucky scheduling. This negative control confirms our test setup
    is genuinely racy when the lock is removed.
    """

    @pytest.mark.asyncio(loop_scope="session")
    async def test_count_exceeds_cap_when_lock_is_noop(
        self, async_engine, user_with_cap_3, enforce_cap_on, monkeypatch
    ):
        async def _noop(self_, user_id: str) -> float:
            return 0.0

        monkeypatch.setattr(
            QuotaService,
            "_acquire_workspace_create_lock",
            _noop,
            raising=True,
        )

        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        # Without an explicit barrier between the cap-check SELECT and the
        # INSERT, asyncio's cooperative scheduler can interleave each task
        # check + insert serially (later tasks observing prior commits)
        # and the race never materializes. The barrier forces all N
        # workers to complete the cap read BEFORE any of them inserts,
        # which deterministically reproduces the TOCTOU when the lock
        # is disabled (PR #686 Copilot review).
        all_read = asyncio.Barrier(_PARALLEL_ATTEMPTS)

        async def attempt_with_barrier() -> bool:
            async with session_maker() as session:
                async with session.begin():
                    quota = QuotaService(session)
                    can, _ = await quota.check_workspace_creation_allowed(user_with_cap_3)
                    # All workers must have read before any of us inserts.
                    await all_read.wait()
                    if can:
                        session.add(_new_workspace(user_with_cap_3))
                        return True
                    return False

        await asyncio.gather(
            *[attempt_with_barrier() for _ in range(_PARALLEL_ATTEMPTS)],
            return_exceptions=True,
        )

        async with session_maker() as session:
            count = await _count_owned(session, user_with_cap_3)

        assert count > _CAP, (
            f"with lock disabled, expected count > cap={_CAP}; got {count}. "
            f"Either the barrier did not synchronize all workers before "
            f"the INSERT phase, or production code has a second "
            f"serialization path besides the advisory lock that this "
            f"test does not reach."
        )


class TestLockWaitMsReflectsContention:
    """Under a deterministic holder/blocker, ``lock_wait_ms`` reflects the wait.

    A naive "any positive wait_ms proves contention" assertion fails
    silently because the SELECT round-trip itself contributes a small
    positive duration even when the lock is granted instantly (PR #686
    Copilot review). This test instead sets up a holder transaction
    that sleeps for ``HOLD_SEC``, then asserts the waiter's recorded
    wait is at least ``MIN_EXPECTED_WAIT_MS`` (well above any
    round-trip noise).

    The helper now measures only the advisory-lock acquire (SET LOCAL
    is excluded), so without contention the floor is the single SELECT
    round-trip (typically <2 ms) and the assertion of >= 200 ms is a
    clean signal that the test actually waited on the lock.
    """

    @pytest.mark.asyncio(loop_scope="session")
    async def test_wait_ms_at_least_hold_duration(
        self, async_engine, user_with_cap_3, enforce_cap_on, monkeypatch
    ):
        warning_mock = MagicMock()
        monkeypatch.setattr(quota_service_module.logger, "warning", warning_mock)

        HOLD_SEC = 0.3
        MIN_EXPECTED_WAIT_MS = 200

        # Pre-fill to cap so the waiter takes the denial path (which is
        # the path that logs lock_wait_ms via workspace_creation_denied).
        async with async_sessionmaker(async_engine, expire_on_commit=False)() as pre:
            async with pre.begin():
                for _ in range(_CAP):
                    pre.add(_new_workspace(user_with_cap_3))

        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)
        lock_key = f"workspace_create:{user_with_cap_3}"
        holder_acquired = asyncio.Event()

        async def hold_lock() -> None:
            async with session_maker() as holder:
                async with holder.begin():
                    await holder.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:k))").bindparams(k=lock_key)
                    )
                    holder_acquired.set()
                    await asyncio.sleep(HOLD_SEC)
                # commit on block exit releases the lock

        async def waiter_attempt_create() -> bool:
            await holder_acquired.wait()
            return await _attempt_one_create(session_maker, user_with_cap_3)

        results = await asyncio.gather(hold_lock(), waiter_attempt_create())
        waiter_succeeded = results[1]

        assert waiter_succeeded is False, (
            "waiter should have been denied (cap pre-filled) but reported success"
        )

        denied_events = [
            call.kwargs
            for call in warning_mock.call_args_list
            if call.args and call.args[0] == "workspace_creation_denied"
        ]
        assert len(denied_events) == 1, (
            f"expected exactly one workspace_creation_denied event from the waiter, "
            f"got {len(denied_events)} (all calls: {warning_mock.call_args_list!r})"
        )

        wait_ms = denied_events[0].get("lock_wait_ms")
        assert isinstance(wait_ms, (int, float)), f"lock_wait_ms must be numeric; got {wait_ms!r}"
        assert wait_ms >= MIN_EXPECTED_WAIT_MS, (
            f"lock_wait_ms={wait_ms:.1f}ms should be >= {MIN_EXPECTED_WAIT_MS}ms "
            f"(holder held the lock for {HOLD_SEC * 1000:.0f}ms). A value below "
            f"the floor means the helper either bypassed the lock or measured "
            f"the wrong interval."
        )
