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
        patch(f"{_MODULE}.build_search_config_response", AsyncMock(return_value=None)),
    ):
        await update_context_search_config(uuid4(), update, {"user_id": "u-1"}, db=AsyncMock())
    return perms, repo


async def _reset(*, stored: bool, owner: bool):
    from api.routes.context_search_config import reset_context_search_config

    perms, repo = _perms(owner=owner), _repo(stored)
    with (
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.build_search_config_response", AsyncMock(return_value=None)),
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


@pytest.mark.asyncio
async def test_a_missing_config_row_still_runs_the_gate():
    """Fail closed: with no stored row the flag counts as off, so an editor's
    request to turn it on is refused before any write path is reached."""
    perms, repo = _perms(owner=False), _repo(False)
    repo.get_by_context = AsyncMock(return_value=None)
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
    perms.check_context_owner.assert_awaited()
    repo.update.assert_not_awaited()


# ---------------------------------------------------------------------------
# #2072: the LanceDB store holds no chunk points, so turning the flag on there
# is refused (it would be reported on and do nothing); turning it off is not.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_turning_the_flag_on_is_refused_without_chunk_point_support(owner):
    """Refused before the owner check: an editor is not sent to an owner whose
    request would be refused too."""
    from api.routes.context_search_config import update_context_search_config
    from utils.exceptions import ValidationError

    perms, repo = _perms(owner=owner), _repo(False)
    with (
        patch("services.permission_service.chunk_points_supported", return_value=False),
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        pytest.raises(ValidationError) as exc,
    ):
        await update_context_search_config(
            uuid4(),
            ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True),
            {"user_id": "u-1"},
            db=AsyncMock(),
        )
    assert exc.value.status_code == 422
    assert exc.value.error_code == "VAL-001"
    assert exc.value.details["field"] == "content_chunks_enabled"
    repo.update.assert_not_awaited()
    perms.check_context_owner.assert_not_awaited()


@pytest.mark.asyncio
async def test_turning_the_flag_off_is_allowed_without_chunk_point_support():
    with patch("services.permission_service.chunk_points_supported", return_value=False):
        _, repo = await _put(
            ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=False),
            stored=True,
            owner=True,
        )
    assert repo.update.await_args.args[1].content_chunks_enabled is False


@pytest.mark.asyncio
async def test_sending_the_stored_on_value_is_a_no_op_without_chunk_point_support():
    with patch("services.permission_service.chunk_points_supported", return_value=False):
        _, repo = await _put(
            ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True),
            stored=True,
            owner=False,
        )
    assert "content_chunks_enabled" not in repo.update.await_args.args[1].model_fields_set


# ---------------------------------------------------------------------------
# #2015 PR 2b (D7): the server-computed fields the UI reads.
# ---------------------------------------------------------------------------


def _row(*, enabled: bool, model: str = "qwen3-embedding:4b"):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 18, tzinfo=UTC)
    return SimpleNamespace(
        context_id=uuid4(),
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
        embedding_model=model,
        embedding_dimensions=2560,
        reinforce_enabled=True,
        reinforce_max_boost=0.15,
        reinforce_require_host_arbitration=False,
        routing_mode="off",
        content_chunks_enabled=enabled,
        created_at=now,
        updated_at=now,
    )


def _status(stored: bool, *, supported: bool = True, **gated):
    """The policy's answer for the response builder: ungated unless ``gated`` is given."""
    from services.content_chunk_policy import ContentChunksStatus

    if gated:
        return ContentChunksStatus(stored=stored, supported=supported, gated=True, **gated)
    return ContentChunksStatus(stored=stored, supported=supported, gated=False, in_use=0)


async def _get(row, *, owner: bool, supported: bool = True, pending: int = 0, status=None):
    from api.routes.context_search_config import get_context_search_config

    perms = _perms(owner=owner)
    repo = MagicMock()
    repo.create_or_get = AsyncMock(return_value=row)
    repo.content_chunk_backfill_pending = AsyncMock(return_value=pending)
    status = status or _status(row.content_chunks_enabled, supported=supported)
    with (
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.chunk_points_supported", return_value=supported),
        patch(f"{_MODULE}.content_chunks_status", AsyncMock(return_value=status)),
    ):
        response = await get_context_search_config(uuid4(), {"user_id": "u-1"}, db=AsyncMock())
    return response, repo


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_get_says_whether_the_caller_may_change_the_flag(owner):
    response, _ = await _get(_row(enabled=False), owner=owner)
    assert response.content_chunks_can_change is owner
    assert response.content_chunks_supported is True
    assert response.embedding_provider == "self_hosted"


@pytest.mark.asyncio
async def test_get_reports_an_unsupported_store_and_the_openai_provider():
    response, _ = await _get(
        _row(enabled=False, model="text-embedding-3-small"), owner=True, supported=False
    )
    assert response.content_chunks_supported is False
    assert response.embedding_provider == "openai"


@pytest.mark.asyncio
async def test_backfill_pending_is_counted_only_while_the_flag_is_on():
    on, repo_on = await _get(_row(enabled=True), owner=True, pending=12)
    off, repo_off = await _get(_row(enabled=False), owner=True, pending=12)
    assert on.content_chunks_backfill_pending == 12
    repo_on.content_chunk_backfill_pending.assert_awaited_once()
    assert off.content_chunks_backfill_pending == 0
    repo_off.content_chunk_backfill_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_and_reset_return_the_same_fields():
    from api.routes.context_search_config import (
        reset_context_search_config,
        update_context_search_config,
    )

    for owner in (True, False):
        perms = _perms(owner=owner)
        repo = MagicMock()
        repo.get_by_context = AsyncMock(return_value=SimpleNamespace(content_chunks_enabled=False))
        repo.update = AsyncMock(return_value=_row(enabled=False))
        repo.reset_to_default = AsyncMock(return_value=_row(enabled=False))
        with (
            patch(f"{_MODULE}.PermissionService", return_value=perms),
            patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
            patch(f"{_MODULE}.content_chunks_status", AsyncMock(return_value=_status(False))),
        ):
            put = await update_context_search_config(
                uuid4(), ContextSearchConfigUpdate(**_BASE), {"user_id": "u-1"}, db=AsyncMock()
            )
            reset = await reset_context_search_config(uuid4(), {"user_id": "u-1"}, db=AsyncMock())
        assert put.content_chunks_can_change is owner
        assert reset.content_chunks_can_change is owner
        assert put.embedding_provider == reset.embedding_provider == "self_hosted"


# ---------------------------------------------------------------------------
# #2103: the plan feature and the per-plan cap (CONTENT_CHUNKS_REQUIRE_PLAN)
# ---------------------------------------------------------------------------

_POLICY = "services.content_chunk_policy"


async def _put_gated(
    *, stored: bool, requested: bool, owner: bool, order: list, plan=None, cap=None, repo=None
):
    """A PUT with the policy's plan and cap checks recorded (and refusing when given)."""

    async def _plan(*_a):
        order.append("plan")
        if plan:
            raise plan

    async def _cap(*_a):
        order.append("cap")
        if cap:
            raise cap

    perms = _perms(owner=owner)
    owner_check = perms.check_context_owner.side_effect

    async def _owner(*_a, **_k):
        order.append("owner")
        if owner_check:
            raise owner_check

    perms.check_context_owner = AsyncMock(side_effect=_owner)
    repo = repo or _repo(stored)
    from api.routes.context_search_config import update_context_search_config

    with (
        patch(f"{_POLICY}.ensure_content_chunks_plan", new=_plan),
        patch(f"{_POLICY}.ensure_content_chunk_capacity", new=_cap),
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.build_search_config_response", AsyncMock(return_value=None)),
    ):
        await update_context_search_config(
            uuid4(),
            ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=requested),
            {"user_id": "u-1"},
            db=AsyncMock(),
        )
    return repo


def _feat():
    from utils.exceptions import FeatureNotAvailableError

    return FeatureNotAvailableError.for_feature("free", "content_chunks")


def _quota():
    from services.content_chunk_policy import _raise_cap
    from utils.exceptions import QuotaExceededError

    try:
        _raise_cap("basic", 1, 1)
    except QuotaExceededError as e:
        return e
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_turning_on_checks_plan_then_owner_then_cap():
    order: list = []
    repo = await _put_gated(stored=False, requested=True, owner=True, order=order)
    assert order == ["plan", "owner", "cap"]
    repo.update.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_a_plan_without_the_feature_is_refused_before_the_owner_check(owner):
    from utils.exceptions import FeatureNotAvailableError

    order: list = []
    with pytest.raises(FeatureNotAvailableError) as exc:
        await _put_gated(stored=False, requested=True, owner=owner, order=order, plan=_feat())
    assert exc.value.status_code == 403
    assert exc.value.error_code == "FEAT-001"
    assert order == ["plan"]


@pytest.mark.asyncio
async def test_an_editor_never_reaches_the_cap_lock():
    order: list = []
    with pytest.raises(AuthorizationError):
        await _put_gated(stored=False, requested=True, owner=False, order=order, cap=_quota())
    assert order == ["plan", "owner"]


@pytest.mark.asyncio
async def test_at_the_cap_the_owner_is_refused_and_nothing_is_written():
    from utils.exceptions import QuotaExceededError

    order: list = []
    repo = _repo(False)
    with pytest.raises(QuotaExceededError) as exc:
        await _put_gated(
            stored=False, requested=True, owner=True, order=order, cap=_quota(), repo=repo
        )
    assert exc.value.status_code == 429
    assert exc.value.details["quota_type"] == "content_chunk_contexts"
    assert order == ["plan", "owner", "cap"]
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_turning_off_is_always_allowed():
    order: list = []
    repo = await _put_gated(
        stored=True, requested=False, owner=True, order=order, plan=_feat(), cap=_quota()
    )
    assert order == ["owner"]
    assert repo.update.await_args.args[1].content_chunks_enabled is False


@pytest.mark.asyncio
async def test_a_deployment_without_the_gate_never_refuses_by_plan():
    """Self-hosted default: no plan lookup, no lock, no refusal, on any plan."""
    perms, repo = _perms(owner=True), _repo(False)
    perms.db = MagicMock()
    perms.db.execute = AsyncMock()
    from api.routes.context_search_config import update_context_search_config

    with (
        patch(
            f"{_POLICY}.get_settings",
            return_value=SimpleNamespace(content_chunks_require_plan=False),
        ),
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.build_search_config_response", AsyncMock(return_value=None)),
    ):
        await update_context_search_config(
            uuid4(),
            ContextSearchConfigUpdate(**_BASE, content_chunks_enabled=True),
            {"user_id": "u-1"},
            db=AsyncMock(),
        )
    perms.db.execute.assert_not_awaited()
    repo.update.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_reports_an_active_context_within_the_cap():
    status = _status(True, plan_name="pro", limit=5, in_use=2, earlier=1)
    response, _ = await _get(_row(enabled=True), owner=True, status=status)
    assert response.content_chunks_effective is True
    assert (response.content_chunks_limit, response.content_chunks_in_use) == (5, 2)
    assert response.content_chunks_disabled_reason is None
    assert response.content_chunks_can_change is True
    # The stored value, never the effective one (#2080): a client echoing it
    # back must not turn the flag off.
    assert response.content_chunks_enabled is True


@pytest.mark.asyncio
async def test_get_reports_an_inactive_context_past_the_cap_that_may_still_turn_off():
    status = _status(True, plan_name="basic", limit=1, in_use=2, earlier=1)
    response, _ = await _get(_row(enabled=True), owner=True, status=status)
    assert response.content_chunks_enabled is True
    assert response.content_chunks_effective is False
    assert response.content_chunks_disabled_reason == "cap"
    assert response.content_chunks_can_change is True  # off is always allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gated", "reason"),
    [
        ({"plan_name": "basic", "limit": 1, "in_use": 1}, "cap"),
        ({"plan_name": "free", "plan_has_feature": False, "limit": 0, "in_use": 0}, "plan"),
    ],
)
async def test_get_disables_turning_on_for_the_owner_by_plan_or_cap(gated, reason):
    response, _ = await _get(_row(enabled=False), owner=True, status=_status(False, **gated))
    assert response.content_chunks_can_change is False
    assert response.content_chunks_disabled_reason == reason
    assert response.content_chunks_effective is False


@pytest.mark.asyncio
async def test_get_names_permission_only_when_nothing_else_blocks():
    editor, _ = await _get(_row(enabled=False), owner=False)
    assert editor.content_chunks_disabled_reason == "permission"
    assert editor.content_chunks_can_change is False
    owner, _ = await _get(_row(enabled=False), owner=True)
    assert owner.content_chunks_disabled_reason is None
    assert owner.content_chunks_limit is None  # not gated
    blocked = _status(False, plan_name="basic", limit=1, in_use=1)
    capped_editor, _ = await _get(_row(enabled=False), owner=False, status=blocked)
    assert capped_editor.content_chunks_disabled_reason == "cap"
