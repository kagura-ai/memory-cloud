"""The credential default context service against a real database (#2129).

``readable_context`` / ``readable_context_options`` (the read rules: private
contexts, other workspaces), ``set_grant_default_context`` (upsert, clear),
``list_grants`` (live tokens grouped by client, plus settings rows). Needs
the DB container (``TEST_DATABASE_URL``).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from models.auth import (
    APIKey,
    Context,
    OAuth2Client,
    OAuth2Token,
    OAuthGrantSetting,
    Workspace,
    WorkspaceMember,
    WorkspaceRole,
)
from services.credential_default_context import (
    grant_default_context_id,
    list_grants,
    readable_context,
    readable_context_options,
    set_grant_default_context,
)
from utils.datetime import utcnow


async def _seed(db, user: str):
    """Two workspaces of ``user`` and one of a stranger, with a few contexts."""
    stranger = f"stranger-{uuid4().hex[:6]}"
    ws_a = Workspace(id=uuid4(), name=f"A-{uuid4().hex[:6]}", owner_user_id=user)
    ws_b = Workspace(id=uuid4(), name=f"B-{uuid4().hex[:6]}", owner_user_id=user)
    ws_x = Workspace(id=uuid4(), name=f"X-{uuid4().hex[:6]}", owner_user_id=stranger)
    db.add_all([ws_a, ws_b, ws_x])
    await db.flush()
    db.add_all(
        [
            WorkspaceMember(workspace_id=ws_a.id, user_id=user, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws_b.id, user_id=user, role=WorkspaceRole.OWNER),
            WorkspaceMember(workspace_id=ws_x.id, user_id=stranger, role=WorkspaceRole.OWNER),
        ]
    )

    def ctx(ws, name, *, created_by=user, private=False):
        return Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=name,
            display_name=name,
            created_by=created_by,
            is_private=private,
        )

    contexts = {
        "a1": ctx(ws_a, "zeta"),
        "a2": ctx(ws_a, "alpha"),
        "b1": ctx(ws_b, "beta"),
        "x1": ctx(ws_x, "foreign", created_by=stranger),
    }
    db.add_all(contexts.values())
    await db.flush()
    return ws_a, ws_b, contexts


async def _client(db, name: str = "Desktop App") -> str:
    client_id = f"c-{uuid4().hex[:12]}"
    db.add(
        OAuth2Client(
            client_id=client_id,
            client_secret_hash="x" * 64,
            client_name=name,
            redirect_uris=["http://127.0.0.1/cb"],
            grant_types=["authorization_code"],
            response_types=["code"],
            scope="memory:read memory:write",
            token_endpoint_auth_method="none",
        )
    )
    await db.flush()
    return client_id


def _token(client_id: str, user: str, scope: str, *, revoked=False, age_minutes=0):
    return OAuth2Token(
        client_id=client_id,
        user_id=user,
        access_token=uuid4().hex,
        scope=scope,
        revoked=revoked,
        issued_at=utcnow() - timedelta(minutes=age_minutes),
    )


@pytest.mark.asyncio
async def test_readable_context_follows_the_read_rules(db_session):
    user = f"u-{uuid4().hex[:8]}"
    ws_a, _ws_b, ctx = await _seed(db_session, user)

    assert (await readable_context(db_session, user_id=user, context_id=ctx["a1"].id)).id == (
        ctx["a1"].id
    )
    assert await readable_context(db_session, user_id=user, context_id=ctx["x1"].id) is None
    assert await readable_context(db_session, user_id=user, context_id=uuid4()) is None
    # A workspace-scoped key is confined to its workspace.
    assert (
        await readable_context(
            db_session, user_id=user, context_id=ctx["b1"].id, key_workspace_id=ws_a.id
        )
        is None
    )


@pytest.mark.asyncio
async def test_options_span_the_users_workspaces_sorted_and_capped(db_session):
    user = f"u-{uuid4().hex[:8]}"
    ws_a, ws_b, ctx = await _seed(db_session, user)

    options = await readable_context_options(db_session, user_id=user)

    assert [(o.workspace_name, o.name) for o in options] == [
        (ws_a.name, "alpha"),
        (ws_a.name, "zeta"),
        (ws_b.name, "beta"),
    ]
    assert ctx["x1"].id not in {o.context_id for o in options}
    assert len(await readable_context_options(db_session, user_id=user, limit=2)) == 2


@pytest.mark.asyncio
async def test_set_grant_default_upserts_and_clears(db_session):
    user = f"u-{uuid4().hex[:8]}"
    _ws_a, _ws_b, ctx = await _seed(db_session, user)
    client_id = await _client(db_session)

    # Clearing without a row writes nothing.
    await set_grant_default_context(db_session, user_id=user, client_id=client_id, context_id=None)
    assert await db_session.get(OAuthGrantSetting, (user, client_id)) is None

    await set_grant_default_context(
        db_session, user_id=user, client_id=client_id, context_id=ctx["a1"].id
    )
    assert (
        await grant_default_context_id(db_session, user_id=user, client_id=client_id)
        == ctx["a1"].id
    )

    await set_grant_default_context(
        db_session, user_id=user, client_id=client_id, context_id=ctx["b1"].id
    )
    assert (
        await grant_default_context_id(db_session, user_id=user, client_id=client_id)
        == ctx["b1"].id
    )

    await set_grant_default_context(db_session, user_id=user, client_id=client_id, context_id=None)
    row = await db_session.get(OAuthGrantSetting, (user, client_id))
    assert row is not None and row.default_context_id is None


@pytest.mark.asyncio
async def test_list_grants_groups_live_tokens_and_keeps_settings_rows(db_session):
    user = f"u-{uuid4().hex[:8]}"
    _ws_a, _ws_b, ctx = await _seed(db_session, user)
    newer = await _client(db_session, "Newer App")
    older = await _client(db_session, "Older App")
    gone = await _client(db_session, "Revoked App")
    other_users = await _client(db_session, "Someone Else's App")
    db_session.add_all(
        [
            _token(newer, user, "memory:read", age_minutes=1),
            _token(newer, user, "memory:read memory:write", age_minutes=2),
            _token(older, user, "memory:read", age_minutes=30),
            _token(gone, user, "memory:read", revoked=True),
            _token(other_users, "someone-else", "memory:read"),
        ]
    )
    await db_session.flush()
    await set_grant_default_context(
        db_session, user_id=user, client_id=older, context_id=ctx["a2"].id
    )
    # A revoked client whose setting is still stored stays listed (clearable).
    await set_grant_default_context(
        db_session, user_id=user, client_id=gone, context_id=ctx["a1"].id
    )

    grants = await list_grants(db_session, user_id=user)

    assert [g.client_id for g in grants] == [newer, older, gone]
    first, second, third = grants
    assert first.client_name == "Newer App"
    assert first.scopes == ["memory:read", "memory:write"]
    assert first.default_context_id is None
    assert second.default_context_id == ctx["a2"].id
    assert third.scopes == [] and third.last_issued_at is None
    assert third.default_context_id == ctx["a1"].id


@pytest.mark.asyncio
async def test_api_key_default_is_cleared_when_its_context_is_deleted(db_session):
    """``ON DELETE SET NULL`` on both columns (the model's FKs)."""
    from sqlalchemy import delete, select

    user = f"u-{uuid4().hex[:8]}"
    _ws_a, _ws_b, ctx = await _seed(db_session, user)
    client_id = await _client(db_session)
    key = APIKey(
        key_hash=uuid4().hex.ljust(64, "0"),
        key_prefix="kagura_test",
        name=f"k-{uuid4().hex[:6]}",
        user_id=user,
        default_context_id=ctx["a1"].id,
    )
    db_session.add(key)
    await set_grant_default_context(
        db_session, user_id=user, client_id=client_id, context_id=ctx["a1"].id
    )
    await db_session.flush()

    await db_session.execute(delete(Context).where(Context.id == ctx["a1"].id))
    await db_session.flush()

    key_default = (
        await db_session.execute(select(APIKey.default_context_id).where(APIKey.id == key.id))
    ).scalar_one()
    grant_default = (
        await db_session.execute(
            select(OAuthGrantSetting.default_context_id).where(
                OAuthGrantSetting.user_id == user, OAuthGrantSetting.client_id == client_id
            )
        )
    ).scalar_one()
    assert key_default is None
    assert grant_default is None
