"""Integration tests for share keys against a real Postgres (#1027).

Two invariants a mocked session cannot prove, pinned here:

1. **Fail-closed allow-list (CSO gate-1 invariant #1).** A minted share key is
   stored in ``share_keys`` and is structurally invisible to the ``api_keys``
   auth path — so it can never authenticate ``/memory/remember`` or any other
   non-recall surface. We mint a real share key and assert ``APIKeyManager``
   returns ``None`` for it while ``ShareKeyManager`` accepts it.

2. **Owner-scoped ``list_keys``.** Every list query ANDs ``user_id == caller``;
   a mocked ``db.execute`` returns a fixed row list regardless of the WHERE
   clause, so the cross-user isolation is only meaningful against a real DB
   with two users' keys seeded.

Mirrors ``tests/integration/test_binding_introspection_owner_scope.py``: seed
User → Workspace → Context tiers (FK order), exercise, tear down.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from auth.api_keys import APIKeyManager
from auth.share_keys import ShareKeyManager
from models.auth import Context, ShareKey, User, Workspace
from services.agent_state_service import AgentStateService


@pytest_asyncio.fixture(loop_scope="session")
async def two_users_one_context(db_session: AsyncSession) -> AsyncIterator[SimpleNamespace]:
    """User A and User B, plus one context (owned by A) to bind keys to."""
    user_a = f"a_{uuid4().hex[:8]}"
    user_b = f"b_{uuid4().hex[:8]}"
    workspace_id = uuid4()
    ctx_id = uuid4()

    for uid in (user_a, user_b):
        db_session.add(
            User(
                email=f"{uid}@share-key.invalid",
                user_id=uid,
                name="Share Key User",
                role="user",
                is_initial_admin=False,
                auth_method="oauth",
                auth_provider="google",
            )
        )
    await db_session.flush()  # users
    db_session.add(
        Workspace(
            id=workspace_id,
            name=f"share-key-{uuid4().hex[:8]}",
            plan_name="pro",
            owner_user_id=user_a,
            daily_api_limit=500,
            weekly_api_limit=2500,
            deleted_at=None,
        )
    )
    await db_session.flush()  # workspace
    db_session.add(Context(id=ctx_id, workspace_id=workspace_id, name="ctx", created_by=user_a))
    await db_session.commit()

    yield SimpleNamespace(user_a=user_a, user_b=user_b, ctx_id=ctx_id, workspace_id=workspace_id)

    await db_session.execute(
        ShareKey.__table__.delete().where(ShareKey.user_id.in_([user_a, user_b]))
    )
    await db_session.execute(Context.__table__.delete().where(Context.workspace_id == workspace_id))
    await db_session.execute(Workspace.__table__.delete().where(Workspace.id == workspace_id))
    await db_session.execute(User.__table__.delete().where(User.user_id.in_([user_a, user_b])))
    await db_session.commit()


class TestShareKeyAllowList:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_share_key_not_honored_by_api_key_path(self, two_users_one_context, db_session):
        """A minted share key authenticates the share path but NOT the api-key
        path — the structural fail-closed allow-list (#1027)."""
        s = two_users_one_context
        plaintext, _ = await ShareKeyManager(db_session).create_key(
            name="dash", user_id=s.user_a, context_id=s.ctx_id
        )
        await db_session.commit()

        # api-key auth path: a share key is invisible (different table) → None.
        assert await APIKeyManager(db_session).verify_key(plaintext) is None

        # share-key auth path: accepted, confined to the bound context.
        verified = await ShareKeyManager(db_session).verify_key(plaintext)
        assert verified is not None
        assert verified.user_id == s.user_a
        assert verified.context_id == s.ctx_id


class TestShareKeyListOwnerScope:
    @pytest.mark.asyncio(loop_scope="session")
    async def test_list_keys_returns_only_callers_keys(self, two_users_one_context, db_session):
        s = two_users_one_context
        mgr = ShareKeyManager(db_session)
        await mgr.create_key(name="a-first", user_id=s.user_a, context_id=s.ctx_id)
        await mgr.create_key(name="a-second", user_id=s.user_a, context_id=s.ctx_id)
        await mgr.create_key(name="b-only", user_id=s.user_b, context_id=s.ctx_id)
        await db_session.commit()

        a_keys = await mgr.list_keys(s.user_a)
        assert {k.name for k in a_keys} == {"a-first", "a-second"}
        assert all(k.user_id == s.user_a for k in a_keys)
        # newest-first ordering (>= tolerates same-statement created_at ties).
        for earlier, later in zip(a_keys, a_keys[1:], strict=False):
            assert earlier.created_at >= later.created_at

        b_keys = await mgr.list_keys(s.user_b)
        assert {k.name for k in b_keys} == {"b-only"}


class TestShareKeySessionObservation:
    """#1064: the agent session-state read is confined to one context."""

    @pytest.mark.asyncio(loop_scope="session")
    async def test_list_state_detail_confined_to_context(self, two_users_one_context, db_session):
        s = two_users_one_context
        # A second context in the same workspace — its state must NOT leak into
        # the bound context's observation view.
        other_ctx = uuid4()
        db_session.add(
            Context(id=other_ctx, workspace_id=s.workspace_id, name="other", created_by=s.user_a)
        )
        await db_session.flush()

        svc = AgentStateService(db_session)
        await svc.set_state(s.ctx_id, "thread-1", {"status": "awaiting_approval"})
        await svc.set_state(s.ctx_id, "thread-2", {"status": "running"})
        await svc.set_state(other_ctx, "thread-x", {"status": "running"})

        entries = await svc.list_state_detail(s.ctx_id)
        keys = {e["key"] for e in entries}
        assert keys == {"thread-1", "thread-2"}  # bound context only
        assert "thread-x" not in keys  # the other context's state is not leaked
        # Each entry carries its own recency + the raw value.
        assert all(e.get("updated_at") is not None and e["value"] is not None for e in entries)
