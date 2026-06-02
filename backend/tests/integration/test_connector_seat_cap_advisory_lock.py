"""Integration tests for the connector seat-cap advisory lock (#857).

These tests exercise the real Postgres ``pg_advisory_xact_lock`` primitive
that ``ConnectorProvisioningService._enforce_connector_seat_cap`` acquires
before counting active connectors. Mocked unit tests in
``tests/services/test_connector_provisioning.py`` cannot reproduce the TOCTOU
race because they bypass transaction-level concurrency — the very thing we
guard against. This mirrors the workspace-creation cap test
(``test_quota_service_advisory_lock.py``, #677), which established the pattern.

Two layers (the #677 layer-3 ``lock_wait_ms`` metric assertion is intentionally
out of scope here — connector provisioning does not log a per-acquire wait):

1. **Deterministic race** — N >> cap parallel provisions must yield exactly
   ``cap`` connectors, never more. Positive proof the lock serializes the
   read-then-write under contention (AC: "exactly one succeeds, the other 403"
   generalized to N).

2. **Negative control** — with the advisory lock monkeypatched to a no-op, an
   ``asyncio.Barrier`` forces all workers to finish the cap-count SELECT before
   any of them inserts, deterministically reproducing ``count > cap``. Proves
   the test setup is genuinely racy when the safeguard is removed, so the
   positive test above cannot pass by lucky scheduling.

The ``pro`` plan grants ``max_connectors = 10`` (cap = 10). The fixture
pre-creates ``_PARALLEL_ATTEMPTS`` resource rows (the 1:1 FK target each
connector needs) so every attempt can stage an insert against a distinct
``resource_pk`` and the only thing bounding inserts is the seat cap.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.auth import User, Workspace
from models.resource import Resource, WorkspaceConnector
from services.connector_provisioning import ConnectorProvisioningService
from utils.exceptions import MemoryCloudException

_CAP = 10  # pro plan max_connectors (Spec 2026-06-02)
_PARALLEL_ATTEMPTS = 20


async def _count_connectors(session: AsyncSession, workspace_id) -> int:
    result = await session.execute(
        select(func.count(WorkspaceConnector.id)).where(
            WorkspaceConnector.workspace_id == workspace_id
        )
    )
    return int(result.scalar() or 0)


@pytest_asyncio.fixture(loop_scope="session")
async def pro_workspace_with_resources(
    db_session: AsyncSession,
) -> AsyncIterator[tuple[object, list[object]]]:
    """A ``pro`` workspace (cap=10) plus N spare resources for connector inserts.

    Cleans up connectors → resources → workspace → user after the test so
    repeated suite runs do not accumulate rows that skew later fixtures.
    """
    user_id = f"u_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    db_session.add(
        User(
            email=f"{user_id}@connector-toctou.invalid",
            user_id=user_id,
            name="Connector TOCTOU User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"conn-toctou-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=user_id,
            daily_api_limit=500,
            weekly_api_limit=2500,
            deleted_at=None,
        )
    )
    resource_pks: list[object] = []
    for i in range(_PARALLEL_ATTEMPTS):
        rid = uuid4()
        db_session.add(
            Resource(
                id=rid,
                workspace_id=workspace_id,
                resource_id=f"conn-res-{i}-{uuid4().hex[:6]}",
                name=f"connector resource {i}",
                created_by=user_id,
            )
        )
        resource_pks.append(rid)
    await db_session.commit()

    yield workspace_id, resource_pks

    await db_session.execute(
        WorkspaceConnector.__table__.delete().where(WorkspaceConnector.workspace_id == workspace_id)
    )
    await db_session.execute(
        Resource.__table__.delete().where(Resource.workspace_id == workspace_id)
    )
    await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
    await db_session.execute(User.__table__.delete().where(User.user_id == user_id))
    await db_session.commit()


async def _attempt_one_provision(
    session_maker: async_sessionmaker[AsyncSession],
    workspace_id,
    resource_pk,
) -> bool:
    """Try to provision one connector; return True iff the insert committed.

    The seat-cap gate (which takes the xact-scoped advisory lock) and the
    connector INSERT run inside a single ``session.begin()`` block so the lock
    holds across both — the contract ``provision_connector`` honors in
    production (gate → ``db.flush()`` of the connector in the same tx).
    """
    async with session_maker() as session:
        async with session.begin():
            svc = ConnectorProvisioningService(session)
            workspace = await svc._get_workspace(workspace_id)
            try:
                await svc._enforce_connector_seat_cap(workspace, workspace_id)
            except MemoryCloudException:
                return False
            session.add(
                WorkspaceConnector(
                    resource_pk=resource_pk,
                    workspace_id=workspace_id,
                    connector_type="slack",
                    created_by="connector-toctou",
                )
            )
            return True


class TestConnectorSeatCapSerializesAtCap:
    """The lock must hold ``count <= cap`` under any level of concurrency."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_n_parallel_provisions_produce_exactly_cap(
        self, async_engine, pro_workspace_with_resources
    ):
        workspace_id, resource_pks = pro_workspace_with_resources
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        results = await asyncio.gather(
            *[
                _attempt_one_provision(session_maker, workspace_id, resource_pks[i])
                for i in range(_PARALLEL_ATTEMPTS)
            ],
            return_exceptions=True,
        )

        unexpected = [r for r in results if not isinstance(r, bool)]
        assert not unexpected, f"unexpected exceptions from attempts: {unexpected!r}"

        successes = sum(1 for r in results if r is True)
        assert successes == _CAP, (
            f"expected exactly {_CAP} successful provisions, got {successes} (results: {results!r})"
        )

        async with session_maker() as session:
            count = await _count_connectors(session, workspace_id)
        assert count == _CAP, f"final connector count is {count}, must equal cap={_CAP}"


class TestConnectorSeatCapNegativeControl:
    """Without the lock, the same parallel pattern MUST reproduce ``count > cap``.

    Regression safeguard for the positive test: if the production advisory lock
    silently breaks, the positive test could still pass by lucky scheduling.
    This negative control confirms the test setup is genuinely racy when the
    lock is removed.
    """

    @pytest.mark.asyncio(loop_scope="session")
    async def test_count_exceeds_cap_when_lock_is_noop(
        self, async_engine, pro_workspace_with_resources, monkeypatch
    ):
        async def _noop(self_, workspace_id) -> None:
            return None

        monkeypatch.setattr(
            ConnectorProvisioningService,
            "_acquire_connector_seat_lock",
            _noop,
            raising=True,
        )

        workspace_id, resource_pks = pro_workspace_with_resources
        session_maker = async_sessionmaker(async_engine, expire_on_commit=False)

        # Without a barrier between the cap-count SELECT and the INSERT,
        # asyncio's cooperative scheduler can interleave each task's
        # check+insert serially (later tasks observing prior commits) and the
        # race never materializes. The barrier forces all N workers to finish
        # the count read BEFORE any of them inserts, deterministically
        # reproducing the TOCTOU when the lock is disabled (mirrors #677).
        all_read = asyncio.Barrier(_PARALLEL_ATTEMPTS)

        async def attempt_with_barrier(resource_pk) -> bool:
            try:
                async with session_maker() as session:
                    async with session.begin():
                        svc = ConnectorProvisioningService(session)
                        workspace = await svc._get_workspace(workspace_id)
                        try:
                            await svc._enforce_connector_seat_cap(workspace, workspace_id)
                            over_cap = False
                        except MemoryCloudException:
                            over_cap = True
                        # All workers must have read before any of us inserts.
                        await all_read.wait()
                        if over_cap:
                            return False
                        session.add(
                            WorkspaceConnector(
                                resource_pk=resource_pk,
                                workspace_id=workspace_id,
                                connector_type="slack",
                                created_by="connector-toctou",
                            )
                        )
                        return True
            except Exception:
                # Any pre-barrier failure must release peer waiters so
                # asyncio.gather does not deadlock. abort() is idempotent.
                try:
                    await all_read.abort()
                except Exception:
                    # Best-effort: the original exception is re-raised below, so a
                    # failure to abort the (possibly already-broken) barrier must
                    # not mask it.
                    pass
                raise

        results = await asyncio.gather(
            *[attempt_with_barrier(resource_pks[i]) for i in range(_PARALLEL_ATTEMPTS)],
            return_exceptions=True,
        )
        # Surface unexpected worker exceptions instead of letting them masquerade
        # as the race outcome (mirrors the positive test's guard). Every worker
        # must return a bool; a non-bool entry is a setup failure, not contention.
        unexpected = [r for r in results if not isinstance(r, bool)]
        assert not unexpected, f"unexpected exceptions from barrier workers: {unexpected!r}"

        async with session_maker() as session:
            count = await _count_connectors(session, workspace_id)

        assert count > _CAP, (
            f"with lock disabled, expected count > cap={_CAP}; got {count}. "
            f"Either the barrier did not synchronize all workers before the "
            f"INSERT phase, or production has a second serialization path "
            f"besides the advisory lock that this test does not reach."
        )
