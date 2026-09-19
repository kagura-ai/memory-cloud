"""``ContextService.create_context`` shared-context gate is registry-driven (#1551).

The gate reads ``has_feature(plan, "shared_contexts")`` — so XL is allowed
without a name list, an unknown tier fails closed, and the refusal names the
minimum tier from the registry instead of a hardcoded "Pro plan".

``update_context`` carries no plan gate of its own (#1583): the REST route is
the one gate, and the service only acts on a real privacy transition.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.plan_tiers import get_plan_tier
from utils.exceptions import ValidationError


class _GateCleared(Exception):
    """Raised by the uniqueness probe: create_context got PAST the plan gates."""


def _service(plan_name: str):
    from services.context_service import ContextService

    workspace = MagicMock()
    workspace.plan_name = plan_name
    result = MagicMock()
    result.scalar_one_or_none.return_value = workspace
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return ContextService(db=db)


async def _create_shared(service):
    """Run create_context(is_private=False) up to the uniqueness probe.

    The probe is the first call after the shared-context and embedding-model
    gates, so reaching it (``_GateCleared``) proves the shared gate let the
    call through; a ``ValidationError`` naming shared contexts is the gate's
    own refusal. Anything else propagates and fails the test.
    """
    settings = MagicMock()
    settings.embedding_model_allowlist = ""
    settings.embedding_model = "text-embedding-3-small"
    settings.embedding_dimensions = 512
    with (
        patch("services.context_service.get_settings", return_value=settings),
        patch(
            "config.embedding_policy.allowed_embedding_models",
            return_value={"text-embedding-3-small"},
        ),
        patch.object(type(service), "validate_context_name", MagicMock(return_value=None)),
        patch.object(
            service,
            "get_context_by_name_for_workspace",
            AsyncMock(side_effect=_GateCleared()),
        ),
    ):
        await service.create_context(workspace_id=MagicMock(), name="team-ctx", is_private=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_name", ["pro", "promax"])
async def test_shared_context_creation_allowed_on_pro_and_xl(plan_name: str) -> None:
    with pytest.raises(_GateCleared):
        await _create_shared(_service(plan_name))


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_name", ["free", "basic", "enterprise"])
async def test_shared_context_creation_refused_below_pro_and_for_unknown_tiers(
    plan_name: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        await _create_shared(_service(plan_name))
    message = str(exc_info.value)
    assert "Shared contexts" in message
    assert get_plan_tier("pro").display_name in message
    assert "Pro plan" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_private", [True, False])
async def test_update_context_treats_a_resubmitted_is_private_as_a_no_op(
    stored_private: bool,
) -> None:
    """#1583: re-sending the stored ``is_private`` with an unrelated edit runs
    no privacy transition and reads no plan, whatever the workspace tier."""
    service = _service("basic")
    service.db.commit = AsyncMock()
    service.db.refresh = AsyncMock()
    context = MagicMock(is_private=stored_private, sleep_mode="skip")

    with (
        patch.object(service, "get_context", AsyncMock(return_value=context)),
        patch.object(service, "_handle_privacy_transition", AsyncMock()) as transition,
    ):
        await service.update_context(
            user_id="owner-1",
            context_id=MagicMock(),
            is_private=stored_private,
            summary="new",
        )

    transition.assert_not_awaited()
    service.db.execute.assert_not_awaited()
    assert (context.summary, context.is_private) == ("new", stored_private)


@pytest.mark.asyncio
async def test_update_context_runs_the_transition_when_privacy_changes() -> None:
    service = _service("pro")
    service.db.commit = AsyncMock()
    service.db.refresh = AsyncMock()
    context = MagicMock(is_private=True, sleep_mode="skip")

    with (
        patch.object(service, "get_context", AsyncMock(return_value=context)),
        patch.object(service, "_handle_privacy_transition", AsyncMock()) as transition,
    ):
        await service.update_context(user_id="owner-1", context_id=MagicMock(), is_private=False)

    transition.assert_awaited_once_with(context, False, "owner-1")
