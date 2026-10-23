"""A user API key's MCP default context over REST (#2129).

``POST /config/api-keys`` takes an optional ``default_context_id``; ``PATCH
/config/api-keys/{key_id}`` sets or clears it; the responses carry the id and
the readable name. The context must be readable by the key's owner (viewer,
in the key's workspace scope) — otherwise the uniform 404. Public-bound and
agent-bound keys cannot carry one. DB and resolver are mocked; the routes are
called directly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError

from api.routes.api_keys import (
    APIKeyCreate,
    APIKeyUpdate,
    create_api_key,
    list_api_keys,
    regenerate_api_key,
    update_api_key,
)
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException

USER = {"user_id": "user-1", "email": "u@example.com", "role": "user"}
REQUEST = SimpleNamespace(client=SimpleNamespace(host="192.0.2.1"), headers={})
PID = "akey_" + "k" * 22
CONTEXT_ID = uuid.uuid4()
WORKSPACE_ID = uuid.uuid4()


def _key(**over) -> SimpleNamespace:
    base = {
        "id": 7,
        "public_id": PID,
        "key_prefix": "kagura_abcdefgh",
        "name": "laptop",
        "user_id": "user-1",
        "workspace_id": None,
        "bound_context_id": None,
        "agent_id": None,
        "default_context_id": None,
        "connector_id": None,  # #1952
        "created_at": utcnow(),
        "last_used_at": None,
        "revoked_at": None,
        "expires_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _db(key) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=key)))
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    return db


def _readable(name: str | None):
    """Patch the read check: a context named ``name``, or a deny for ``None``."""
    result = SimpleNamespace(id=CONTEXT_ID, name=name) if name is not None else None
    return patch("api.routes.api_keys.readable_context", new=AsyncMock(return_value=result))


# ------------------------------------------------------------------ schema


def test_create_schema_takes_an_optional_default_context():
    assert APIKeyCreate(name="k").default_context_id is None
    assert APIKeyCreate(name="k", default_context_id=CONTEXT_ID).default_context_id == CONTEXT_ID


def test_create_schema_refuses_a_value_that_is_not_a_uuid():
    with pytest.raises(ValidationError, match="default_context_id"):
        APIKeyCreate(name="k", default_context_id="my-project")


def test_update_schema_requires_the_field_and_accepts_null():
    with pytest.raises(ValidationError, match="default_context_id"):
        APIKeyUpdate.model_validate({})
    assert APIKeyUpdate.model_validate({"default_context_id": None}).default_context_id is None


# ------------------------------------------------------------------ PATCH


@pytest.mark.asyncio
async def test_patch_sets_a_readable_default_and_returns_its_name():
    key = _key(workspace_id=WORKSPACE_ID)
    db = _db(key)
    with _readable("proj-alpha") as readable:
        response = await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=CONTEXT_ID), user=USER, db=db
        )

    assert key.default_context_id == CONTEXT_ID
    assert response.default_context_id == CONTEXT_ID
    assert response.default_context_name == "proj-alpha"
    db.commit.assert_awaited_once()
    # Checked in the key's own workspace scope, as the key's owner.
    kwargs = readable.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["context_id"] == CONTEXT_ID
    assert kwargs["key_workspace_id"] == WORKSPACE_ID


@pytest.mark.asyncio
async def test_patch_with_null_clears_without_a_read():
    key = _key(default_context_id=CONTEXT_ID)
    db = _db(key)
    with _readable("unused") as readable:
        response = await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=None), user=USER, db=db
        )

    assert key.default_context_id is None
    assert response.default_context_id is None
    assert response.default_context_name is None
    readable.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_with_an_unreadable_context_is_a_uniform_404_and_writes_nothing():
    key = _key()
    db = _db(key)
    with _readable(None), pytest.raises(MemoryCloudException) as exc:
        await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=CONTEXT_ID), user=USER, db=db
        )

    assert exc.value.status_code == 404
    assert exc.value.message == "Context not found"
    assert key.default_context_id is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_on_another_users_key_is_404():
    db = _db(None)  # the ownership filter matched no row
    with pytest.raises(MemoryCloudException) as exc:
        await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=CONTEXT_ID), user=USER, db=db
        )

    assert exc.value.status_code == 404
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "over",
    [{"bound_context_id": uuid.uuid4()}, {"agent_id": uuid.uuid4(), "workspace_id": WORKSPACE_ID}],
)
async def test_patch_refuses_a_bound_key_with_422(over):
    key = _key(**over)
    db = _db(key)
    with _readable("proj"), pytest.raises(MemoryCloudException) as exc:
        await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=CONTEXT_ID), user=USER, db=db
        )

    assert exc.value.status_code == 422
    assert "default context" in exc.value.message
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_refuses_a_connector_key_with_422():
    connector_id = uuid.uuid4()
    key = _key(
        name=f"connector:{connector_id}", workspace_id=WORKSPACE_ID, connector_id=connector_id
    )
    db = _db(key)
    with _readable("proj"), pytest.raises(MemoryCloudException) as exc:
        await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=CONTEXT_ID), user=USER, db=db
        )

    assert exc.value.status_code == 422
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_lets_an_unlinked_key_with_the_prefix_take_a_default():
    """#1952: the link decides, not the name — a legacy key named like a
    connector key but without ``connector_id`` is an ordinary key."""
    key = _key(name=f"connector:{uuid.uuid4()}", workspace_id=WORKSPACE_ID)
    db = _db(key)
    with _readable("proj"):
        await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=CONTEXT_ID), user=USER, db=db
        )

    assert key.default_context_id == CONTEXT_ID
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_patch_refuses_a_revoked_key():
    key = _key(revoked_at=utcnow())
    with pytest.raises(MemoryCloudException) as exc:
        await update_api_key(
            key_id=PID, data=APIKeyUpdate(default_context_id=None), user=USER, db=_db(key)
        )

    assert exc.value.status_code == 400


# ------------------------------------------------------------------ create


def _manager(created) -> MagicMock:
    manager = MagicMock()
    manager.db = MagicMock(commit=AsyncMock())
    manager.create_key = AsyncMock(return_value=("plaintext", created))
    return manager


@pytest.mark.asyncio
async def test_create_stores_a_readable_default():
    created = _key(default_context_id=CONTEXT_ID)
    manager = _manager(created)
    with (
        _readable("proj-alpha") as readable,
        patch("api.routes.api_keys.schedule_security_notification"),
    ):
        response = await create_api_key(
            data=APIKeyCreate(name="laptop", default_context_id=CONTEXT_ID),
            request=REQUEST,
            background_tasks=BackgroundTasks(),
            user=USER,
            manager=manager,
        )

    assert manager.create_key.await_args.kwargs["default_context_id"] == CONTEXT_ID
    assert readable.await_args.kwargs.get("key_workspace_id") is None
    assert response.default_context_id == CONTEXT_ID
    assert response.default_context_name == "proj-alpha"


@pytest.mark.asyncio
async def test_create_with_an_unreadable_default_is_404_and_mints_nothing():
    manager = _manager(_key())
    with _readable(None), pytest.raises(MemoryCloudException) as exc:
        await create_api_key(
            data=APIKeyCreate(name="laptop", default_context_id=CONTEXT_ID),
            request=REQUEST,
            background_tasks=BackgroundTasks(),
            user=USER,
            manager=manager,
        )

    assert exc.value.status_code == 404
    manager.create_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_without_a_default_reads_nothing():
    manager = _manager(_key())
    with (
        _readable("unused") as readable,
        patch("api.routes.api_keys.schedule_security_notification"),
    ):
        response = await create_api_key(
            data=APIKeyCreate(name="laptop"),
            request=REQUEST,
            background_tasks=BackgroundTasks(),
            user=USER,
            manager=manager,
        )

    readable.assert_not_awaited()
    assert manager.create_key.await_args.kwargs["default_context_id"] is None
    assert response.default_context_id is None


# ------------------------------------------------------------------ list / regenerate


@pytest.mark.asyncio
async def test_list_names_each_default_once_per_context_and_scope():
    other = uuid.uuid4()
    keys = [
        _key(id=1, default_context_id=CONTEXT_ID),
        _key(id=2, default_context_id=CONTEXT_ID),
        _key(id=3, default_context_id=other, workspace_id=WORKSPACE_ID),
        _key(id=4),
    ]
    manager = MagicMock(db=MagicMock())
    manager.list_keys = AsyncMock(return_value=keys)

    async def name_of(_db, *, user_id, context_id, key_workspace_id=None):
        return "alpha" if context_id == CONTEXT_ID else None  # ``other`` is unreadable

    reader = AsyncMock(side_effect=name_of)
    with patch("services.credential_default_context.readable_context_name", new=reader):
        response = await list_api_keys(user=USER, manager=manager)

    assert [r.default_context_name for r in response] == ["alpha", "alpha", None, None]
    assert [r.default_context_id for r in response] == [CONTEXT_ID, CONTEXT_ID, other, None]
    assert reader.await_count == 2
    scoped = [c.kwargs["key_workspace_id"] for c in reader.await_args_list]
    assert scoped == [None, WORKSPACE_ID]


@pytest.mark.asyncio
async def test_regenerate_keeps_the_default_context():
    old = _key(default_context_id=CONTEXT_ID)
    new = _key(id=8, public_id="akey_" + "n" * 22, default_context_id=CONTEXT_ID)
    manager = _manager(new)
    with (
        patch(
            "services.credential_default_context.readable_context_name",
            new=AsyncMock(return_value="proj-alpha"),
        ),
        patch("api.routes.api_keys.schedule_security_notification"),
    ):
        response = await regenerate_api_key(
            key_id=PID,
            request=REQUEST,
            background_tasks=BackgroundTasks(),
            user=USER,
            manager=manager,
            db=_db(old),
        )

    assert manager.create_key.await_args.kwargs["default_context_id"] == CONTEXT_ID
    assert response.default_context_name == "proj-alpha"


@pytest.mark.asyncio
async def test_create_key_refuses_a_default_on_a_bound_key():
    """The service mirrors the CHECK constraints."""
    from auth.api_keys import APIKeyManager

    manager = APIKeyManager(MagicMock())
    with pytest.raises(ValueError, match="default_context_id"):
        await manager.create_key(
            name="k", user_id="u", bound_context_id=uuid.uuid4(), default_context_id=CONTEXT_ID
        )
    with pytest.raises(ValueError, match="default_context_id"):
        await manager.create_key(
            name="k",
            user_id="u",
            workspace_id=WORKSPACE_ID,
            agent_id=uuid.uuid4(),
            default_context_id=CONTEXT_ID,
        )


@pytest.mark.asyncio
async def test_member_regenerate_keeps_the_default_context():
    """The workspace-key regenerate path carries the default like /config does."""
    from api.routes import member_credentials as mc

    old_key = SimpleNamespace(
        id=7,
        public_id="akey_" + "7" * 22,
        name="ws-key",
        revoked_at=None,
        default_context_id=CONTEXT_ID,
        connector_id=None,  # #1952
    )
    new_key = SimpleNamespace(id=8, public_id="akey_" + "8" * 22, key_prefix="kagura_new")
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=old_key))
    )
    db.commit = AsyncMock()
    mgr = MagicMock()
    mgr.create_key = AsyncMock(return_value=("kagura_PLAIN", new_key))
    with (
        patch("api.routes.member_credentials.check_permission", new=AsyncMock()),
        patch("api.routes.member_credentials.MemberCredentialsService"),
        patch("api.routes.member_credentials.APIKeyManager", return_value=mgr),
        patch("api.routes.member_credentials.schedule_security_notification"),
    ):
        await mc.regenerate_api_key(
            WORKSPACE_ID,
            "member-1",
            SimpleNamespace(client=None, headers={}),
            BackgroundTasks(),
            {"user_id": "member-1", "sub": "m"},
            db,
        )

    assert mgr.create_key.await_args.kwargs["default_context_id"] == CONTEXT_ID
    assert mgr.create_key.await_args.kwargs["connector_id"] is None


@pytest.mark.asyncio
async def test_member_regenerate_keeps_the_connector_link():
    """#1952: regenerating a member's newest key — which can be a connector's
    key — keeps it linked to its connector."""
    from api.routes import member_credentials as mc

    connector_id = uuid.uuid4()
    old_key = SimpleNamespace(
        id=7,
        public_id="akey_" + "7" * 22,
        name=f"connector:{connector_id}",
        revoked_at=None,
        default_context_id=None,
        connector_id=connector_id,
    )
    new_key = SimpleNamespace(id=8, public_id="akey_" + "8" * 22, key_prefix="kagura_new")
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=old_key))
    )
    db.commit = AsyncMock()
    mgr = MagicMock()
    mgr.create_key = AsyncMock(return_value=("kagura_PLAIN", new_key))
    with (
        patch("api.routes.member_credentials.check_permission", new=AsyncMock()),
        patch("api.routes.member_credentials.MemberCredentialsService"),
        patch("api.routes.member_credentials.APIKeyManager", return_value=mgr),
        patch("api.routes.member_credentials.schedule_security_notification"),
    ):
        await mc.regenerate_api_key(
            WORKSPACE_ID,
            "member-1",
            SimpleNamespace(client=None, headers={}),
            BackgroundTasks(),
            {"user_id": "member-1", "sub": "m"},
            db,
        )

    assert mgr.create_key.await_args.kwargs["connector_id"] == connector_id


# ------------------------------------------------- member self-mint (#2188)

SESSION = {"user_id": "member-1", "sub": "member-1", "email": "m@example.com", "role": "user"}


def _member_mint_manager(**key_over):
    new_key = _key(
        id=9,
        public_id="akey_" + "9" * 22,
        user_id="member-1",
        workspace_id=WORKSPACE_ID,
        visibility_expires_at=utcnow(),
        **key_over,
    )
    mgr = MagicMock()
    mgr.create_key = AsyncMock(return_value=("kagura_PLAIN", new_key))
    return mgr


async def _member_mint(body: dict, mgr, readable_result):
    from api.routes import member_credentials as mc
    from models.schemas import CreateAPIKeyRequest

    db = MagicMock()
    db.commit = AsyncMock()
    reader = AsyncMock(return_value=readable_result)
    with (
        patch("api.routes.member_credentials.readable_context", new=reader),
        patch("api.routes.member_credentials.APIKeyManager", return_value=mgr),
        patch("api.routes.member_credentials.schedule_security_notification"),
    ):
        response = await mc.create_api_key(
            WORKSPACE_ID,
            "member-1",
            CreateAPIKeyRequest(**body),
            REQUEST,
            BackgroundTasks(),
            SESSION,
            db,
        )
    return response, reader


@pytest.mark.asyncio
async def test_member_self_mint_sets_a_readable_default_in_one_call():
    from models.schemas import MemberAPIKeyResponse

    mgr = _member_mint_manager(default_context_id=CONTEXT_ID)
    response, reader = await _member_mint(
        {"name": "laptop", "default_context_id": str(CONTEXT_ID)},
        mgr,
        SimpleNamespace(id=CONTEXT_ID, name="proj-alpha"),
    )

    assert mgr.create_key.await_args.kwargs["default_context_id"] == CONTEXT_ID
    kwargs = reader.await_args.kwargs
    assert kwargs["user_id"] == "member-1"
    assert kwargs["context_id"] == CONTEXT_ID
    assert kwargs["key_workspace_id"] == WORKSPACE_ID  # the key's own scope
    body = MemberAPIKeyResponse(**response)
    assert body.default_context_id == str(CONTEXT_ID)
    assert body.default_context_name == "proj-alpha"
    assert body.agent_id is None


@pytest.mark.asyncio
async def test_member_self_mint_without_a_default_reads_nothing():
    mgr = _member_mint_manager()
    response, reader = await _member_mint({"name": "laptop"}, mgr, None)

    reader.assert_not_awaited()
    assert mgr.create_key.await_args.kwargs["default_context_id"] is None
    assert response["default_context_id"] is None
    assert response["default_context_name"] is None


@pytest.mark.asyncio
async def test_member_self_mint_with_an_unreadable_default_is_404_and_mints_nothing():
    mgr = _member_mint_manager()
    with pytest.raises(MemoryCloudException) as exc:
        await _member_mint({"name": "laptop", "default_context_id": str(CONTEXT_ID)}, mgr, None)

    assert exc.value.status_code == 404
    assert exc.value.message == "Context not found"
    mgr.create_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_self_mint_refuses_a_default_on_a_public_bound_key():
    """422 (the PATCH rule) and nothing is minted."""
    from api.routes import member_credentials as mc
    from models.schemas import CreateAPIKeyRequest

    mgr = _member_mint_manager()
    ctx = SimpleNamespace(
        id=uuid.uuid4(), workspace_id=WORKSPACE_ID, is_public=True, created_by="member-1"
    )
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=ctx)),
            MagicMock(
                scalar_one_or_none=MagicMock(return_value=SimpleNamespace(plan_name="promax"))
            ),
        ]
    )
    reader = AsyncMock()
    with (
        patch("api.routes.member_credentials.readable_context", new=reader),
        patch("api.routes.member_credentials.APIKeyManager", return_value=mgr),
        patch("config.plan_tiers.has_feature", return_value=True),
        patch(
            "config.plan_tiers.get_plan_tier",
            return_value=SimpleNamespace(bound_public_calls_per_minute=60),
        ),
        pytest.raises(MemoryCloudException) as exc,
    ):
        await mc.create_api_key(
            WORKSPACE_ID,
            "member-1",
            CreateAPIKeyRequest(
                name="pub", bound_context_id=str(ctx.id), default_context_id=CONTEXT_ID
            ),
            REQUEST,
            BackgroundTasks(),
            SESSION,
            db,
        )

    assert exc.value.status_code == 422
    reader.assert_not_awaited()
    mgr.create_key.assert_not_awaited()
