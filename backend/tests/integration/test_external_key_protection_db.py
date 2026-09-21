"""DB-level pins for conditional external-key protection (#1613).

The flag half of the rule is unit-tested in
``tests/services/test_external_key_protection.py``. Here the workspace half
runs against real rows — which contexts count as "routed to an OpenAI
embedding model" — and the three route handlers that consume the rule are
invoked directly against the same session:

- ``DELETE /external-keys/{key_name}`` and the disable half of
  ``PATCH /external-keys/{key_name}/toggle`` refuse (400, shapes unchanged)
  only while the key is protected;
- ``GET /external-keys`` reports the same answer as ``is_protected``;
- with ``ENABLE_BYOK=false`` the owner can still list and delete.

The last section sends the same requests through the ASGI app, because the
bodies a client parses are shaped by the global exception handler, not by the
handlers above.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.main import app
from api.routes.external_keys import (
    ExternalKeyCreate,
    ExternalKeyToggle,
    create_external_key,
    delete_external_key,
    encrypt_value,
    list_external_keys,
    toggle_external_key,
)
from auth.dependencies import get_user_from_api_key_or_session
from auth.workspace_roles import WorkspaceRole
from config.settings import Settings
from db.base import get_db
from models.auth import Context, ExternalAPIKey, WorkspaceMember
from models.config import ContextSearchConfig
from repositories.config_repository import ContextSearchConfigRepository
from services.external_key_protection import count_openai_routed_contexts, embedding_provider_of

from ._admin_helpers import make_context, make_user, make_workspace

_FLAG_ENV = [
    "ENABLE_BYOK",
    "RESOLVE_STORED_BYOK_KEYS",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
]

_SELF_HOSTED = {"embedding_provider": "self_hosted", "embedding_model": "qwen3-embedding:0.6b"}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _FLAG_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def deployment(monkeypatch):
    """Install a settings singleton for the route handlers; restored on teardown."""

    def _install(**overrides) -> Settings:
        settings = _settings(**overrides)
        monkeypatch.setattr("config.settings._settings", settings)
        return settings

    return _install


async def _new_workspace(db: AsyncSession, *, keys: tuple[tuple[str, str], ...]) -> dict:
    """A workspace with the given ``(key_name, provider)`` rows and no contexts."""
    owner = make_user()
    db.add(owner)
    await db.flush()
    ws = make_workspace(owner_user_id=owner.user_id)
    db.add(ws)
    await db.flush()
    for key_name, provider in keys:
        db.add(
            ExternalAPIKey(
                key_name=key_name,
                provider=provider,
                encrypted_value=encrypt_value("sk-test-not-a-real-key"),
                user_id=owner.user_id,
                workspace_id=ws.id,
                enabled=True,
            )
        )
    await db.commit()
    return {
        "workspace_id": ws.id,
        "user": {
            "user_id": owner.user_id,
            "email": owner.email,
            "current_workspace_id": ws.id,
        },
    }


@pytest_asyncio.fixture
async def workspace(db_session: AsyncSession) -> dict:
    """A workspace that stores an OpenAI key and a Cohere key."""
    return await _new_workspace(
        db_session, keys=(("OPENAI_API_KEY", "openai"), ("COHERE_API_KEY", "cohere"))
    )


@pytest_asyncio.fixture
async def empty_workspace(db_session: AsyncSession) -> dict:
    """A workspace that stores no key yet."""
    return await _new_workspace(db_session, keys=())


async def _add_context(
    db: AsyncSession,
    workspace: dict,
    *,
    embedding_model: str | None,
    soft_deleted: bool = False,
) -> Context:
    """A context, with a search-config row unless ``embedding_model`` is None."""
    context = make_context(
        workspace_id=workspace["workspace_id"], created_by=workspace["user"]["user_id"]
    )
    if soft_deleted:
        context.deleted_at = func.now()
    db.add(context)
    await db.flush()
    if embedding_model is not None:
        db.add(ContextSearchConfig(context_id=context.id, embedding_model=embedding_model))
    await db.commit()
    return context


async def _key_names(db: AsyncSession, workspace_id: UUID) -> set[str]:
    result = await db.execute(
        select(ExternalAPIKey.key_name).where(ExternalAPIKey.workspace_id == workspace_id)
    )
    return set(result.scalars().all())


# ---------------------------------------------------------------------------
# count_openai_routed_contexts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_contexts_means_nothing_routes_to_openai(db_session, workspace):
    count = await count_openai_routed_contexts(
        db_session, workspace["workspace_id"], _settings(**_SELF_HOSTED)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_only_live_openai_contexts_are_counted(db_session, workspace):
    settings = _settings(**_SELF_HOSTED)
    await _add_context(db_session, workspace, embedding_model="qwen3-embedding:0.6b")
    await _add_context(db_session, workspace, embedding_model="text-embedding-3-small")
    await _add_context(db_session, workspace, embedding_model="text-embedding-3-large")
    await _add_context(
        db_session, workspace, embedding_model="text-embedding-3-small", soft_deleted=True
    )

    assert await count_openai_routed_contexts(db_session, workspace["workspace_id"], settings) == 2


@pytest.mark.asyncio
async def test_other_workspaces_contexts_are_not_counted(db_session, workspace):
    other_owner = make_user()
    db_session.add(other_owner)
    await db_session.flush()
    other_ws = make_workspace(owner_user_id=other_owner.user_id)
    db_session.add(other_ws)
    await db_session.flush()
    other = {"workspace_id": other_ws.id, "user": {"user_id": other_owner.user_id}}
    await _add_context(db_session, other, embedding_model="text-embedding-3-small")

    count = await count_openai_routed_contexts(
        db_session, workspace["workspace_id"], _settings(**_SELF_HOSTED)
    )
    assert count == 0


@pytest.mark.asyncio
async def test_context_without_config_row_counts_as_openai_routed(db_session, workspace):
    """A row-less legacy context is one recall away from an OpenAI model.

    ``ContextSearchConfigRepository.create_or_get`` writes the row with the
    column default, so the context counts on every deployment — not only where
    ``EMBEDDING_MODEL`` is an OpenAI model.
    """
    await _add_context(db_session, workspace, embedding_model=None)

    on_self_hosted = await count_openai_routed_contexts(
        db_session, workspace["workspace_id"], _settings(**_SELF_HOSTED)
    )
    on_openai = await count_openai_routed_contexts(
        db_session, workspace["workspace_id"], _settings()
    )
    assert (on_self_hosted, on_openai) == (1, 1)


@pytest.mark.asyncio
async def test_materialising_the_config_row_does_not_change_the_verdict(
    db_session, workspace, deployment
):
    """Deletable-then-needed must not happen: the lazy row cannot flip the answer."""
    settings = deployment(**_SELF_HOSTED)
    context = await _add_context(db_session, workspace, embedding_model=None)
    before = await _listed_protection(db_session, workspace)

    config = await ContextSearchConfigRepository(db_session).create_or_get(context.id)

    assert embedding_provider_of(config.embedding_model, settings) == "openai"
    assert before["OPENAI_API_KEY"] is True
    assert await _listed_protection(db_session, workspace) == before


# ---------------------------------------------------------------------------
# DELETE /external-keys/{key_name}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_is_refused_while_the_deployment_embeds_with_openai(
    db_session, workspace, deployment
):
    deployment()  # EMBEDDING_PROVIDER=openai, BYOK on
    with pytest.raises(HTTPException) as exc_info:
        await delete_external_key("OPENAI_API_KEY", user=workspace["user"], db=db_session)

    assert exc_info.value.status_code == 400
    assert isinstance(exc_info.value.detail, str)  # shape unchanged: a plain string
    assert "OPENAI_API_KEY" in exc_info.value.detail
    assert "this deployment" in exc_info.value.detail
    assert "OPENAI_API_KEY" in await _key_names(db_session, workspace["workspace_id"])


@pytest.mark.asyncio
async def test_delete_succeeds_when_nothing_embeds_with_openai(db_session, workspace, deployment):
    deployment(**_SELF_HOSTED)
    await _add_context(db_session, workspace, embedding_model="qwen3-embedding:0.6b")

    response = await delete_external_key("OPENAI_API_KEY", user=workspace["user"], db=db_session)

    assert "deleted" in response["message"]
    assert await _key_names(db_session, workspace["workspace_id"]) == {"COHERE_API_KEY"}


@pytest.mark.asyncio
async def test_delete_follows_the_openai_routed_context(db_session, workspace, deployment):
    """Protected while a live context needs the key; deletable once it is gone."""
    deployment(**_SELF_HOSTED)
    context = await _add_context(db_session, workspace, embedding_model="text-embedding-3-small")

    with pytest.raises(HTTPException) as exc_info:
        await delete_external_key("OPENAI_API_KEY", user=workspace["user"], db=db_session)
    assert exc_info.value.status_code == 400
    assert "1 context of this workspace" in exc_info.value.detail

    context.deleted_at = func.now()
    await db_session.commit()

    await delete_external_key("OPENAI_API_KEY", user=workspace["user"], db=db_session)
    assert "OPENAI_API_KEY" not in await _key_names(db_session, workspace["workspace_id"])


@pytest.mark.asyncio
async def test_delete_of_a_non_candidate_key_is_never_refused(db_session, workspace, deployment):
    deployment()
    await delete_external_key("COHERE_API_KEY", user=workspace["user"], db=db_session)
    assert await _key_names(db_session, workspace["workspace_id"]) == {"OPENAI_API_KEY"}


# ---------------------------------------------------------------------------
# PATCH /external-keys/{key_name}/toggle (disable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_is_refused_while_protected(db_session, workspace, deployment):
    deployment()
    with pytest.raises(HTTPException) as exc_info:
        await toggle_external_key(
            "OPENAI_API_KEY",
            ExternalKeyToggle(enabled=False),
            user=workspace["user"],
            db=db_session,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "cannot_disable_embeddings"  # shape unchanged
    assert "this deployment" in exc_info.value.detail["message"]


@pytest.mark.asyncio
async def test_disable_succeeds_when_unprotected(db_session, workspace, deployment):
    deployment(**_SELF_HOSTED)

    response = await toggle_external_key(
        "OPENAI_API_KEY",
        ExternalKeyToggle(enabled=False),
        user=workspace["user"],
        db=db_session,
    )

    assert response.enabled is False
    assert response.is_protected is False


@pytest_asyncio.fixture
async def renamed_key_workspace(db_session: AsyncSession) -> dict:
    """A workspace whose OpenAI key is stored under another name (raw API only)."""
    return await _new_workspace(db_session, keys=(("MY_OPENAI_KEY", "openai"),))


@pytest.mark.asyncio
async def test_disable_is_refused_for_an_openai_key_under_another_name(
    db_session, renamed_key_workspace, deployment
):
    """EmbeddingService picks the key by provider, so this is the live credential."""
    deployment()
    with pytest.raises(HTTPException) as exc_info:
        await toggle_external_key(
            "MY_OPENAI_KEY",
            ExternalKeyToggle(enabled=False),
            user=renamed_key_workspace["user"],
            db=db_session,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "cannot_disable_embeddings"
    assert "MY_OPENAI_KEY" in exc_info.value.detail["message"]


@pytest.mark.asyncio
async def test_openai_key_under_another_name_follows_the_same_conditions(
    db_session, renamed_key_workspace, deployment
):
    deployment(**_SELF_HOSTED)

    response = await toggle_external_key(
        "MY_OPENAI_KEY",
        ExternalKeyToggle(enabled=False),
        user=renamed_key_workspace["user"],
        db=db_session,
    )

    assert (response.enabled, response.is_protected) == (False, False)


@pytest.mark.asyncio
async def test_openai_key_under_another_name_stays_deletable(
    db_session, renamed_key_workspace, deployment
):
    """Unchanged: delete and ``is_protected`` are name-based (#149)."""
    deployment()
    assert await _listed_protection(db_session, renamed_key_workspace) == {"MY_OPENAI_KEY": False}

    await delete_external_key("MY_OPENAI_KEY", user=renamed_key_workspace["user"], db=db_session)
    assert await _key_names(db_session, renamed_key_workspace["workspace_id"]) == set()


# ---------------------------------------------------------------------------
# POST /external-keys with enabled=false (the same disable guard)
# ---------------------------------------------------------------------------

_DISABLED_OPENAI_KEY = ExternalKeyCreate(
    key_name="OPENAI_API_KEY", provider="openai", value="sk-test-not-a-real-key", enabled=False
)


@pytest.mark.asyncio
async def test_create_refuses_a_disabled_key_while_protected(
    db_session, empty_workspace, deployment
):
    deployment()
    with pytest.raises(HTTPException) as exc_info:
        await create_external_key(_DISABLED_OPENAI_KEY, user=empty_workspace["user"], db=db_session)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "cannot_disable_embeddings"
    assert await _key_names(db_session, empty_workspace["workspace_id"]) == set()


@pytest.mark.asyncio
async def test_create_refuses_a_disabled_openai_key_under_another_name(
    db_session, empty_workspace, deployment
):
    deployment()
    request = _DISABLED_OPENAI_KEY.model_copy(update={"key_name": "MY_OPENAI_KEY"})
    with pytest.raises(HTTPException) as exc_info:
        await create_external_key(request, user=empty_workspace["user"], db=db_session)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["error"] == "cannot_disable_embeddings"
    assert await _key_names(db_session, empty_workspace["workspace_id"]) == set()


@pytest.mark.asyncio
async def test_create_stores_a_disabled_key_when_unprotected(
    db_session, empty_workspace, deployment
):
    deployment(**_SELF_HOSTED)

    response = await create_external_key(
        _DISABLED_OPENAI_KEY, user=empty_workspace["user"], db=db_session
    )

    assert (response.enabled, response.is_protected) == (False, False)


# ---------------------------------------------------------------------------
# GET /external-keys → is_protected
# ---------------------------------------------------------------------------


async def _listed_protection(db: AsyncSession, workspace: dict) -> dict[str, bool]:
    response = await list_external_keys(user=workspace["user"], db=db)
    return {key.key_name: key.is_protected for key in response.keys}


@pytest.mark.asyncio
async def test_list_flags_the_protected_key_only(db_session, workspace, deployment):
    deployment()
    assert await _listed_protection(db_session, workspace) == {
        "OPENAI_API_KEY": True,
        "COHERE_API_KEY": False,
    }


@pytest.mark.asyncio
async def test_list_flags_nothing_when_embeddings_do_not_use_openai(
    db_session, workspace, deployment
):
    deployment(**_SELF_HOSTED)
    assert await _listed_protection(db_session, workspace) == {
        "OPENAI_API_KEY": False,
        "COHERE_API_KEY": False,
    }


@pytest.mark.asyncio
async def test_list_flags_the_key_for_an_openai_routed_context(db_session, workspace, deployment):
    deployment(**_SELF_HOSTED)
    await _add_context(db_session, workspace, embedding_model="text-embedding-3-large")
    assert (await _listed_protection(db_session, workspace))["OPENAI_API_KEY"] is True


# ---------------------------------------------------------------------------
# ENABLE_BYOK=false: the owner can still see and withdraw a stored key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_byok_off_owner_lists_and_deletes_the_openai_key(db_session, workspace, deployment):
    deployment(enable_byok=False)  # EMBEDDING_PROVIDER stays openai
    await _add_context(db_session, workspace, embedding_model="text-embedding-3-small")

    assert (await _listed_protection(db_session, workspace))["OPENAI_API_KEY"] is False

    await delete_external_key("OPENAI_API_KEY", user=workspace["user"], db=db_session)
    assert "OPENAI_API_KEY" not in await _key_names(db_session, workspace["workspace_id"])


@pytest.mark.asyncio
async def test_stored_key_resolution_off_owner_deletes_the_openai_key(
    db_session, workspace, deployment
):
    deployment(enable_byok=False, resolve_stored_byok_keys=False)

    await delete_external_key("OPENAI_API_KEY", user=workspace["user"], db=db_session)
    assert "OPENAI_API_KEY" not in await _key_names(db_session, workspace["workspace_id"])


# ---------------------------------------------------------------------------
# Over HTTP: the refusals as a client receives them
# ---------------------------------------------------------------------------

_KEY_URL = "/api/v1/external-keys/OPENAI_API_KEY"


@pytest_asyncio.fixture
async def owner_http(async_engine, db_session, workspace):
    """An HTTP client signed in as the workspace owner.

    Only authentication and the session factory are overridden.
    ``require_workspace_owner`` runs for real against the membership row, and
    the global exception handler shapes the bodies — the envelope the External
    Keys page parses (``details.detail.error``).
    """
    db_session.add(
        WorkspaceMember(
            workspace_id=workspace["workspace_id"],
            user_id=workspace["user"]["user_id"],
            role=WorkspaceRole.OWNER,
        )
    )
    await db_session.commit()

    # A fresh session per request, like test_resource_cross_workspace.
    session_maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _db():
        async with session_maker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    async def _user() -> dict:
        return workspace["user"]

    app.dependency_overrides[get_user_from_api_key_or_session] = _user
    app.dependency_overrides[get_db] = _db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_user_from_api_key_or_session, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_http_refusals_keep_their_wire_shape(owner_http, db_session, workspace, deployment):
    deployment()  # EMBEDDING_PROVIDER=openai, BYOK on

    deleted = await owner_http.delete(_KEY_URL)
    assert deleted.status_code == 400, deleted.text
    assert deleted.json()["message"].startswith("Cannot delete OPENAI_API_KEY:")

    disabled = await owner_http.patch(f"{_KEY_URL}/toggle", json={"enabled": False})
    assert disabled.status_code == 400, disabled.text
    detail = disabled.json()["details"]["detail"]
    assert detail["error"] == "cannot_disable_embeddings"
    assert detail["message"].startswith("Cannot disable OPENAI_API_KEY:")

    assert "OPENAI_API_KEY" in await _key_names(db_session, workspace["workspace_id"])


@pytest.mark.asyncio
async def test_http_disable_and_delete_succeed_when_unprotected(
    owner_http, db_session, workspace, deployment
):
    deployment(**_SELF_HOSTED)

    disabled = await owner_http.patch(f"{_KEY_URL}/toggle", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    assert (disabled.json()["enabled"], disabled.json()["is_protected"]) == (False, False)

    deleted = await owner_http.delete(_KEY_URL)
    assert deleted.status_code == 200, deleted.text
    assert "OPENAI_API_KEY" not in await _key_names(db_session, workspace["workspace_id"])


@pytest.mark.asyncio
async def test_http_owner_gate_is_the_real_one(db_session, workspace, owner_http, deployment):
    """The 200s above are not a bypass: the same client as a member gets 403."""
    deployment(**_SELF_HOSTED)
    member = make_user()
    db_session.add(member)
    await db_session.flush()
    db_session.add(
        WorkspaceMember(
            workspace_id=workspace["workspace_id"],
            user_id=member.user_id,
            role=WorkspaceRole.MEMBER,
        )
    )
    await db_session.commit()

    async def _member() -> dict:
        return {
            "user_id": member.user_id,
            "email": member.email,
            "current_workspace_id": workspace["workspace_id"],
        }

    app.dependency_overrides[get_user_from_api_key_or_session] = _member

    assert (await owner_http.delete(_KEY_URL)).status_code == 403
    assert "OPENAI_API_KEY" in await _key_names(db_session, workspace["workspace_id"])
