"""#2015: changing ``content_chunks_enabled`` takes the context owner (REST).

Turning the flag on is the first time memory content, not only summaries,
goes to the embedding and rerank providers, so a request that CHANGES the
stored value needs ``check_context_owner`` (a context owner, the creator of a
private context, or a workspace owner/admin). Editors keep every other
search-config field, and a request that leaves the flag as it is (omitted, or
the stored value) is not a change and is not written.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import ContextSearchConfigUpdate
from utils.exceptions import AuthorizationError

_MODULE = "api.routes.context_search_config"

_BASE = {
    "semantic_weight": 0.6,
    "bm25_weight": 0.4,
    "fetch_factor": 3,
    "use_rerank": False,
    "reranker_provider": "voyage",
    "reranker_model": "rerank-2",
}


def _perms(*, owner: bool) -> MagicMock:
    perms = MagicMock()
    perms.check_context_write = AsyncMock()
    perms.check_context_owner = AsyncMock(side_effect=None if owner else AuthorizationError())
    return perms


def _repo(stored: bool) -> MagicMock:
    repo = MagicMock()
    repo.get_by_context = AsyncMock(return_value=SimpleNamespace(content_chunks_enabled=stored))
    repo.update = AsyncMock(side_effect=lambda _ctx, data: data)
    repo.reset_to_default = AsyncMock(return_value=SimpleNamespace())
    return repo


async def _put(update: ContextSearchConfigUpdate, *, stored: bool, owner: bool):
    from api.routes.context_search_config import update_context_search_config

    perms, repo = _perms(owner=owner), _repo(stored)
    with (
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.ContextSearchConfigResponse.model_validate", side_effect=lambda c: c),
    ):
        await update_context_search_config(uuid4(), update, {"user_id": "u-1"}, db=AsyncMock())
    return perms, repo


async def _reset(*, stored: bool, owner: bool):
    from api.routes.context_search_config import reset_context_search_config

    perms, repo = _perms(owner=owner), _repo(stored)
    with (
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.ContextSearchConfigResponse.model_validate", side_effect=lambda c: c),
    ):
        await reset_context_search_config(uuid4(), {"user_id": "u-1"}, db=AsyncMock())
    return perms, repo


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "requested"), [(False, True), (True, False)])
async def test_an_editor_cannot_change_the_flag(stored, requested):
    with pytest.raises(AuthorizationError) as exc:
        await _put(
            ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=requested),
            stored=stored,
            owner=False,
        )
    assert exc.value.status_code == 403
    assert exc.value.error_code == "AUTH-101"
    assert "content_chunks_enabled" in exc.value.message


@pytest.mark.asyncio
async def test_a_denied_change_writes_nothing():
    perms, repo = _perms(owner=False), _repo(False)
    from api.routes.context_search_config import update_context_search_config

    with (
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        pytest.raises(AuthorizationError),
    ):
        await update_context_search_config(
            uuid4(),
            ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True),
            {"user_id": "u-1"},
            db=AsyncMock(),
        )
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_editor_can_change_other_fields():
    perms, repo = await _put(ContextSearchConfigUpdate(**_BASE), stored=False, owner=False)
    perms.check_context_owner.assert_not_awaited()
    repo.update.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_an_editor_sending_the_stored_value_is_a_no_op(stored):
    perms, repo = await _put(
        ContextSearchConfigUpdate(**{**_BASE, "use_rerank": True}, content_chunks_enabled=stored),
        stored=stored,
        owner=False,
    )
    perms.check_context_owner.assert_not_awaited()
    applied = repo.update.await_args.args[1]
    # Not written at all: a concurrent owner change is never overwritten by it.
    assert "content_chunks_enabled" not in applied.model_fields_set
    assert applied.use_rerank is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "requested"), [(False, True), (True, False)])
async def test_an_owner_or_workspace_admin_can_change_the_flag(stored, requested):
    """``check_context_owner`` passes for a context owner, a private context's
    creator and a workspace owner/admin (``check_context_access`` treats the
    workspace roles as the context owner)."""
    perms, repo = await _put(
        ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=requested),
        stored=stored,
        owner=True,
    )
    perms.check_context_owner.assert_awaited_once()
    applied = repo.update.await_args.args[1]
    assert applied.content_chunks_enabled is requested
    assert "content_chunks_enabled" in applied.model_fields_set


@pytest.mark.asyncio
async def test_an_editor_cannot_reset_while_the_flag_is_on():
    with pytest.raises(AuthorizationError):
        await _reset(stored=True, owner=False)


@pytest.mark.asyncio
async def test_an_editor_reset_with_the_flag_off_leaves_the_flag_alone():
    perms, repo = await _reset(stored=False, owner=False)
    perms.check_context_owner.assert_not_awaited()
    assert repo.reset_to_default.await_args.kwargs == {"reset_content_chunks": False}


@pytest.mark.asyncio
async def test_an_owner_reset_turns_the_flag_off():
    perms, repo = await _reset(stored=True, owner=True)
    perms.check_context_owner.assert_awaited_once()
    assert repo.reset_to_default.await_args.kwargs == {"reset_content_chunks": True}
