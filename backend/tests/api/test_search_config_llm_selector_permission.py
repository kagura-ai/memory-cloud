"""#2144: changing ``llm_selector_enabled`` (REST) — owner rule, plan gate, fields.

Mirrors ``test_search_config_content_chunks_permission.py`` for the recall LLM
selector (no store check: the selector stores nothing). Turning the flag on
sends the context's memory text to the deployment's judge model at recall
time, so a request that CHANGES the stored value needs ``check_context_owner``;
editors keep every other field, and a request that leaves the flag as it is
(omitted, or the stored value) is not a change and is not written.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.schemas import ContextSearchConfigUpdate
from utils.exceptions import AuthorizationError

_MODULE = "api.routes.context_search_config"
_POLICY = "services.llm_selector_policy"

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
    repo.get_by_context = AsyncMock(
        return_value=SimpleNamespace(content_chunks_enabled=False, llm_selector_enabled=stored)
    )
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
            ContextSearchConfigUpdate(**_BASE, llm_selector_enabled=requested),
            stored=stored,
            owner=False,
        )
    assert exc.value.status_code == 403
    assert exc.value.error_code == "AUTH-101"
    assert "llm_selector_enabled" in exc.value.message


@pytest.mark.asyncio
async def test_an_editor_can_change_other_fields():
    perms, repo = await _put(ContextSearchConfigUpdate(**_BASE), stored=False, owner=False)
    perms.check_context_owner.assert_not_awaited()
    repo.update.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_an_editor_sending_the_stored_value_is_a_no_op(stored):
    perms, repo = await _put(
        ContextSearchConfigUpdate(**{**_BASE, "use_rerank": True}, llm_selector_enabled=stored),
        stored=stored,
        owner=False,
    )
    perms.check_context_owner.assert_not_awaited()
    applied = repo.update.await_args.args[1]
    assert "llm_selector_enabled" not in applied.model_fields_set
    assert applied.use_rerank is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "requested"), [(False, True), (True, False)])
async def test_an_owner_or_workspace_admin_can_change_the_flag(stored, requested):
    perms, repo = await _put(
        ContextSearchConfigUpdate(**_BASE, llm_selector_enabled=requested),
        stored=stored,
        owner=True,
    )
    perms.check_context_owner.assert_awaited_once()
    applied = repo.update.await_args.args[1]
    assert applied.llm_selector_enabled is requested
    assert "llm_selector_enabled" in applied.model_fields_set


@pytest.mark.asyncio
async def test_an_editor_cannot_reset_while_the_flag_is_on():
    with pytest.raises(AuthorizationError):
        await _reset(stored=True, owner=False)


@pytest.mark.asyncio
async def test_an_editor_reset_with_the_flag_off_leaves_the_flag_alone():
    perms, repo = await _reset(stored=False, owner=False)
    perms.check_context_owner.assert_not_awaited()
    assert repo.reset_to_default.await_args.kwargs == {
        "reset_content_chunks": False,
        "reset_llm_selector": False,
    }


@pytest.mark.asyncio
async def test_an_owner_reset_turns_the_flag_off():
    perms, repo = await _reset(stored=True, owner=True)
    perms.check_context_owner.assert_awaited_once()
    assert repo.reset_to_default.await_args.kwargs == {
        "reset_content_chunks": False,
        "reset_llm_selector": True,
    }


@pytest.mark.asyncio
async def test_a_missing_config_row_still_runs_the_gate():
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
            ContextSearchConfigUpdate(**_BASE, llm_selector_enabled=True),
            {"user_id": "u-1"},
            db=AsyncMock(),
        )
    perms.check_context_owner.assert_awaited()
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_lancedb_store_does_not_block_the_selector():
    with patch("services.permission_service.chunk_points_supported", return_value=False):
        _, repo = await _put(
            ContextSearchConfigUpdate(**_BASE, llm_selector_enabled=True),
            stored=False,
            owner=True,
        )
    assert repo.update.await_args.args[1].llm_selector_enabled is True


# ---------------------------------------------------------------------------
# The plan feature and the per-plan cap (LLM_SELECTOR_REQUIRE_PLAN)
# ---------------------------------------------------------------------------


async def _put_gated(
    *, stored: bool, requested: bool, owner: bool, order: list, plan=None, cap=None, repo=None
):
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
        patch(f"{_POLICY}.ensure_llm_selector_plan", new=_plan),
        patch(f"{_POLICY}.ensure_llm_selector_capacity", new=_cap),
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.build_search_config_response", AsyncMock(return_value=None)),
    ):
        await update_context_search_config(
            uuid4(),
            ContextSearchConfigUpdate(**_BASE, llm_selector_enabled=requested),
            {"user_id": "u-1"},
            db=AsyncMock(),
        )
    return repo


def _feat():
    from utils.exceptions import FeatureNotAvailableError

    return FeatureNotAvailableError.for_feature("free", "llm_selector")


def _quota():
    from services.llm_selector_policy import _raise_cap
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
    assert exc.value.details["feature"] == "llm_selector"
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
    assert exc.value.details["quota_type"] == "llm_selector_contexts"
    assert order == ["plan", "owner", "cap"]
    repo.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_turning_off_is_always_allowed():
    order: list = []
    repo = await _put_gated(
        stored=True, requested=False, owner=True, order=order, plan=_feat(), cap=_quota()
    )
    assert order == ["owner"]
    assert repo.update.await_args.args[1].llm_selector_enabled is False


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
            return_value=SimpleNamespace(llm_selector_require_plan=False),
        ),
        patch(f"{_MODULE}.PermissionService", return_value=perms),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.build_search_config_response", AsyncMock(return_value=None)),
    ):
        await update_context_search_config(
            uuid4(),
            ContextSearchConfigUpdate(**_BASE, llm_selector_enabled=True),
            {"user_id": "u-1"},
            db=AsyncMock(),
        )
    perms.db.execute.assert_not_awaited()
    repo.update.assert_awaited_once()


# ---------------------------------------------------------------------------
# The server-computed fields
# ---------------------------------------------------------------------------


def _row(*, enabled: bool):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 19, tzinfo=UTC)
    return SimpleNamespace(
        context_id=uuid4(),
        semantic_weight=0.6,
        bm25_weight=0.4,
        fetch_factor=3,
        use_rerank=False,
        reranker_provider="voyage",
        reranker_model="rerank-2",
        embedding_model="qwen3-embedding:4b",
        embedding_dimensions=2560,
        reinforce_enabled=True,
        reinforce_max_boost=0.15,
        reinforce_require_host_arbitration=False,
        routing_mode="off",
        content_chunks_enabled=False,
        llm_selector_enabled=enabled,
        created_at=now,
        updated_at=now,
    )


def _status(stored: bool, **gated):
    """The policy's answer for the response builder: ungated unless ``gated`` is given."""
    from services.llm_selector_policy import LLMSelectorStatus

    if gated:
        return LLMSelectorStatus(stored=stored, gated=True, **gated)
    return LLMSelectorStatus(stored=stored, gated=False, in_use=0)


async def _get(row, *, owner: bool, status=None):
    from api.routes.context_search_config import get_context_search_config
    from services.content_chunk_policy import ContentChunksStatus

    repo = MagicMock()
    repo.create_or_get = AsyncMock(return_value=row)
    repo.content_chunk_backfill_pending = AsyncMock(return_value=0)
    status = status or _status(row.llm_selector_enabled)
    chunks = ContentChunksStatus(stored=False, supported=True, gated=False, in_use=0)
    with (
        patch(f"{_MODULE}.PermissionService", return_value=_perms(owner=owner)),
        patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
        patch(f"{_MODULE}.chunk_points_supported", return_value=True),
        patch(f"{_MODULE}.content_chunks_status", AsyncMock(return_value=chunks)),
        patch(f"{_MODULE}.llm_selector_status", AsyncMock(return_value=status)),
    ):
        return await get_context_search_config(uuid4(), {"user_id": "u-1"}, db=AsyncMock())


@pytest.mark.asyncio
async def test_get_with_the_gate_off_reports_the_flag_without_a_limit():
    owner = await _get(_row(enabled=False), owner=True)
    assert (owner.llm_selector_enabled, owner.llm_selector_effective) == (False, False)
    assert owner.llm_selector_limit is None
    assert owner.llm_selector_can_change is True
    assert owner.llm_selector_disabled_reason is None
    on = await _get(_row(enabled=True), owner=True)
    assert (on.llm_selector_enabled, on.llm_selector_effective) == (True, True)


@pytest.mark.asyncio
async def test_get_reports_an_active_context_within_the_cap():
    status = _status(True, plan_name="pro", limit=5, in_use=2, earlier=1)
    response = await _get(_row(enabled=True), owner=True, status=status)
    assert response.llm_selector_effective is True
    assert (response.llm_selector_limit, response.llm_selector_in_use) == (5, 2)
    assert response.llm_selector_disabled_reason is None
    assert response.llm_selector_can_change is True
    # The stored value, never the effective one: a client echoing it back must
    # not turn the flag off.
    assert response.llm_selector_enabled is True


@pytest.mark.asyncio
async def test_get_reports_an_inactive_context_past_the_cap_that_may_still_turn_off():
    status = _status(True, plan_name="basic", limit=1, in_use=2, earlier=1)
    response = await _get(_row(enabled=True), owner=True, status=status)
    assert response.llm_selector_enabled is True
    assert response.llm_selector_effective is False
    assert response.llm_selector_disabled_reason == "cap"
    assert response.llm_selector_can_change is True  # off is always allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gated", "reason"),
    [
        ({"plan_name": "basic", "limit": 1, "in_use": 1}, "cap"),
        ({"plan_name": "free", "plan_has_feature": False, "limit": 0, "in_use": 0}, "plan"),
    ],
)
async def test_get_disables_turning_on_for_the_owner_by_plan_or_cap(gated, reason):
    response = await _get(_row(enabled=False), owner=True, status=_status(False, **gated))
    assert response.llm_selector_can_change is False
    assert response.llm_selector_disabled_reason == reason
    assert response.llm_selector_effective is False


@pytest.mark.asyncio
async def test_get_names_permission_only_when_nothing_else_blocks():
    editor = await _get(_row(enabled=False), owner=False)
    assert editor.llm_selector_disabled_reason == "permission"
    assert editor.llm_selector_can_change is False
    blocked = _status(False, plan_name="basic", limit=1, in_use=1)
    capped_editor = await _get(_row(enabled=False), owner=False, status=blocked)
    assert capped_editor.llm_selector_disabled_reason == "cap"


@pytest.mark.asyncio
async def test_a_failed_count_degrades_the_fields_not_the_response():
    """The PUT has already committed when the response is built: a failed
    count must not answer 500 for a change that landed."""
    from api.routes.context_search_config import get_context_search_config
    from services.content_chunk_policy import ContentChunksStatus

    repo = MagicMock()
    repo.create_or_get = AsyncMock(return_value=_row(enabled=True))
    repo.content_chunk_backfill_pending = AsyncMock(return_value=0)
    chunks = ContentChunksStatus(stored=False, supported=True, gated=False, in_use=0)
    for gated in (False, True):
        with (
            patch(f"{_MODULE}.PermissionService", return_value=_perms(owner=True)),
            patch(f"{_MODULE}.ContextSearchConfigRepository", return_value=repo),
            patch(f"{_MODULE}.chunk_points_supported", return_value=True),
            patch(f"{_MODULE}.content_chunks_status", AsyncMock(return_value=chunks)),
            patch(
                f"{_MODULE}.llm_selector_status",
                AsyncMock(side_effect=RuntimeError("statement timeout")),
            ),
            patch(f"{_MODULE}.llm_selector_gated", return_value=gated),
        ):
            response = await get_context_search_config(uuid4(), {"user_id": "u-1"}, db=AsyncMock())
        assert response.llm_selector_enabled is True
        # Ungated: the flag decides; gated without counts: inactive.
        assert response.llm_selector_effective is (not gated)
        assert response.llm_selector_in_use == 0
