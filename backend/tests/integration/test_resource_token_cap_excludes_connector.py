"""Integration test: regular resource-token cap excludes connector tokens (#858).

The connector setup flow mints a resource token that intentionally bypasses the
``max_resource_tokens`` gate (connectors are gated by ``max_connectors`` seats).
Before #858, the regular cap count
(``backend/src/api/routes/resource_tokens.py``) counted **all** active tokens
for the user, so a connector-minted token still consumed a regular slot —
asymmetric, and able to prematurely 403 a legitimate regular-token creation.

#858 adds an anti-join against ``workspace_connectors`` to the count. This test
seeds a mix of regular and connector-owned tokens against a real Postgres and
asserts the exclusion. It is an **integration** test on purpose: the fix is a
SQL ``LEFT JOIN ... WHERE wc.id IS NULL``, which a mocked ``db.execute`` (a bare
scalar return) cannot validate. The query under test mirrors the production
cap-count query verbatim; the naive control proves the connector token would
otherwise be counted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import User, Workspace
from models.resource import Resource, ResourceToken, WorkspaceConnector


def _regular_count_query(user_id: str):
    """The #858 production cap-count query (mirrors resource_tokens.py:329)."""
    return (
        select(func.count(ResourceToken.id))
        .outerjoin(
            WorkspaceConnector,
            WorkspaceConnector.resource_pk == ResourceToken.resource_pk,
        )
        .where(
            and_(
                ResourceToken.created_by == user_id,
                ResourceToken.is_active == True,  # noqa: E712
                WorkspaceConnector.id.is_(None),
            )
        )
    )


def _naive_count_query(user_id: str):
    """The pre-#858 count (no connector exclusion) — negative control."""
    return select(func.count(ResourceToken.id)).where(
        and_(
            ResourceToken.created_by == user_id,
            ResourceToken.is_active == True,  # noqa: E712
        )
    )


@pytest_asyncio.fixture(loop_scope="session")
async def seeded_tokens(db_session: AsyncSession) -> AsyncIterator[str]:
    """Seed a user with 2 regular tokens + 1 NULL-resource_pk regular token +
    1 connector-owned token, plus a revoked connector token that must not count.

    Layout (all created_by = user):
      - 2 active regular tokens (distinct regular resources)
      - 1 active regular token with resource_pk = NULL (legacy shadow column)
      - 1 active connector token (resource owned by a workspace_connector)
      - 1 revoked (is_active=False) connector token

    Expected: regular-cap count = 3 (the 3 regular), naive count = 4
    (regular 3 + active connector 1; the revoked one is excluded by is_active).
    """
    user_id = f"u_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    db_session.add(
        User(
            email=f"{user_id}@rt-cap.invalid",
            user_id=user_id,
            name="RT Cap User",
            role="user",
            is_initial_admin=False,
            auth_method="oauth",
            auth_provider="google",
        )
    )
    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"rt-cap-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=user_id,
            daily_api_limit=500,
            weekly_api_limit=2500,
            deleted_at=None,
        )
    )

    def _token(resource_pk, resource_id, *, is_active=True):
        return ResourceToken(
            resource_pk=resource_pk,
            resource_id=resource_id,
            workspace_id=workspace_id,
            token_hash=f"hash_{uuid4().hex}",
            description="rt-cap test",
            created_by=user_id,
            is_active=is_active,
        )

    # 2 regular resources + tokens
    for i in range(2):
        rid = uuid4()
        db_session.add(
            Resource(id=rid, workspace_id=workspace_id, resource_id=f"reg-{i}-{uuid4().hex[:6]}")
        )
        db_session.add(_token(rid, f"reg-{i}"))

    # 1 regular token with NULL resource_pk (legacy shadow-column path).
    # NOTE: once #325 tightens ResourceToken.resource_pk to NOT NULL, this row
    # becomes impossible and this seed block (plus the count math below) can be
    # dropped — the anti-join logic itself is unaffected. The
    # _enforce_resource_pk_invariant before_insert listener blocks creating this
    # via the ORM (resource_id set + resource_pk NULL), but such rows exist in
    # pre-#323 data, so insert it via raw SQL to exercise the NULL-join edge:
    # the anti-join condition wc.resource_pk == NULL never matches, so the token
    # must still be counted toward the regular cap.
    # Flush first so the ORM-staged workspace row is visible to the raw INSERT
    # (which executes immediately, ahead of the final commit) and the FK holds.
    await db_session.flush()
    await db_session.execute(
        text(
            "INSERT INTO resource_tokens "
            "(resource_pk, resource_id, workspace_id, token_hash, created_by, "
            " quota_events_per_hour, is_active) "
            "VALUES (NULL, :rid, :wid, :hash, :uid, 1000, true)"
        ).bindparams(rid="reg-null", wid=workspace_id, hash=f"hash_{uuid4().hex}", uid=user_id)
    )

    # 1 connector resource + workspace_connector + active connector token
    conn_rid = uuid4()
    db_session.add(
        Resource(id=conn_rid, workspace_id=workspace_id, resource_id=f"conn-{uuid4().hex[:6]}")
    )
    db_session.add(
        WorkspaceConnector(
            resource_pk=conn_rid,
            workspace_id=workspace_id,
            connector_type="slack",
            created_by=user_id,
        )
    )
    db_session.add(_token(conn_rid, "conn-tok"))

    # 1 revoked connector token (different connector resource) — excluded by is_active
    conn_rid2 = uuid4()
    db_session.add(
        Resource(id=conn_rid2, workspace_id=workspace_id, resource_id=f"conn2-{uuid4().hex[:6]}")
    )
    db_session.add(
        WorkspaceConnector(
            resource_pk=conn_rid2,
            workspace_id=workspace_id,
            connector_type="slack",
            created_by=user_id,
        )
    )
    db_session.add(_token(conn_rid2, "conn-tok-revoked", is_active=False))

    await db_session.commit()

    yield user_id

    # Teardown — FK order: tokens → connectors → resources → workspace → user.
    await db_session.execute(
        ResourceToken.__table__.delete().where(ResourceToken.workspace_id == workspace_id)
    )
    await db_session.execute(
        WorkspaceConnector.__table__.delete().where(WorkspaceConnector.workspace_id == workspace_id)
    )
    await db_session.execute(
        Resource.__table__.delete().where(Resource.workspace_id == workspace_id)
    )
    await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
    await db_session.execute(User.__table__.delete().where(User.user_id == user_id))
    await db_session.commit()


class TestResourceTokenCapExcludesConnectorTokens:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_anti_join_excludes_connector_owned_tokens(
        self, db_session: AsyncSession, seeded_tokens
    ):
        user_id = seeded_tokens

        regular = int((await db_session.execute(_regular_count_query(user_id))).scalar() or 0)
        naive = int((await db_session.execute(_naive_count_query(user_id))).scalar() or 0)

        # The #858 count sees only the 3 regular tokens (2 with a resource +
        # 1 with NULL resource_pk). The active connector token is excluded; the
        # revoked connector token is excluded by is_active in both queries.
        assert regular == 3, f"regular cap count must exclude connector tokens; got {regular}"

        # Negative control: without the anti-join the active connector token
        # would be counted, inflating the cap to 4. This proves the exclusion
        # is load-bearing, not a no-op on this fixture.
        assert naive == 4, f"naive count should include the connector token; got {naive}"
        assert naive - regular == 1, "exactly one active connector token must be excluded"
