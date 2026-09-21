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
"""

from __future__ import annotations

from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.external_keys import (
    ExternalKeyCreate,
    ExternalKeyToggle,
    create_external_key,
    delete_external_key,
    encrypt_value,
    list_external_keys,
    toggle_external_key,
)
from config.settings import Settings
from models.auth import Context, ExternalAPIKey
from models.config import ContextSearchConfig
from services.external_key_protection import count_openai_routed_contexts

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
async def test_context_without_config_row_inherits_the_deployment_model(db_session, workspace):
    await _add_context(db_session, workspace, embedding_model=None)

    on_self_hosted = await count_openai_routed_contexts(
        db_session, workspace["workspace_id"], _settings(**_SELF_HOSTED)
    )
    on_openai = await count_openai_routed_contexts(
        db_session, workspace["workspace_id"], _settings()
    )
    assert (on_self_hosted, on_openai) == (0, 1)


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
