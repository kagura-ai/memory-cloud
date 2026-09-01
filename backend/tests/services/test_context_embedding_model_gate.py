"""The embedding-model gate at ContextService.create_context (#1517).

The REST and MCP routes each check registry membership before calling the
service, but ``workspace_service`` calls ``create_context`` directly and
bypassed both. Gating here covers every caller — which matters more than usual
because the embedding model is immutable after creation (#146), so a context
created with the wrong model cannot be repaired in place.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.exceptions import ValidationError


def _service_with_settings(allowlist: str):
    """A ContextService whose create_context reaches the gate and stops there.

    Everything after the gate (name validation, uniqueness probe, INSERT) is
    irrelevant to these assertions, so the workspace lookup is stubbed and the
    tests assert on what the gate does before any of it runs.
    """
    from services.context_service import ContextService

    settings = MagicMock()
    settings.embedding_model_allowlist = allowlist
    settings.embedding_model = "text-embedding-3-small"
    settings.embedding_dimensions = 512

    service = ContextService(db=AsyncMock())
    workspace = MagicMock()
    workspace.plan_name = "pro"
    service._get_workspace_or_raise = AsyncMock(return_value=workspace)
    return service, settings


async def _create(service, settings, **kwargs):
    with (
        patch("services.context_service.get_settings", return_value=settings),
        patch.object(
            type(service), "validate_context_name", MagicMock(return_value=None), create=True
        ),
        patch.object(
            service, "get_context_by_name_for_workspace", AsyncMock(return_value=None), create=True
        ),
    ):
        return await service.create_context(**kwargs)


async def _gate_cleared(service, settings, **kwargs) -> bool:
    """Did create_context get PAST the embedding-model gate?

    The uniqueness probe is the first thing after the gate, so its having been
    called proves the gate did not raise. Asserting that — rather than
    swallowing whatever the mocked DB work raises further down — is what keeps
    the "this model is accepted" tests from passing vacuously: without it they
    would also pass if create_context blew up *before* ever reaching the gate.
    """
    probe = AsyncMock(return_value=None)
    # No collection creation: past the gate the real code would reach Qdrant,
    # and this helper is about the gate, not about provisioning.
    kwargs.setdefault("create_collection", False)
    with (
        patch("services.context_service.get_settings", return_value=settings),
        patch.object(
            type(service), "validate_context_name", MagicMock(return_value=None), create=True
        ),
        patch.object(service, "get_context_by_name_for_workspace", probe, create=True),
    ):
        try:
            await service.create_context(**kwargs)
        except ValidationError:
            # The gate's own refusal. Anything else propagates and fails the
            # test, per review of #1517.
            return False
    return probe.await_count > 0


@pytest.mark.asyncio
class TestCallerSuppliedModel:
    async def test_a_model_outside_the_allowlist_is_refused(self):
        service, settings = _service_with_settings("qwen3-embedding:4b")
        with pytest.raises(ValidationError) as exc:
            await _create(
                service,
                settings,
                workspace_id=MagicMock(),
                name="ctx",
                embedding_model="text-embedding-3-large",
            )
        # The message must separate "not offered here" from "no such model":
        # one is fixed by the operator, the other by the caller.
        assert "not available on this deployment" in str(exc.value)

    async def test_an_unknown_model_is_named_as_unknown(self):
        service, settings = _service_with_settings("")
        with pytest.raises(ValidationError) as exc:
            await _create(
                service,
                settings,
                workspace_id=MagicMock(),
                name="ctx",
                embedding_model="not-a-real-model",
            )
        assert "Unknown embedding model" in str(exc.value)

    async def test_the_default_allowlist_still_accepts_every_registry_model(self):
        # Guard the no-behaviour-change promise: an empty setting must not
        # start rejecting anything that worked before.
        from config.constants import EMBEDDING_MODEL_REGISTRY

        for model in EMBEDDING_MODEL_REGISTRY:
            service, settings = _service_with_settings("")
            assert await _gate_cleared(
                service,
                settings,
                workspace_id=MagicMock(),
                name="ctx",
                embedding_model=model,
            ), f"{model} was rejected under the default allowlist"


@pytest.mark.asyncio
class TestOperatorConfiguredDefault:
    async def test_a_custom_default_model_is_not_policed(self):
        """A self-host pointing EMBEDDING_MODEL at a custom model is deliberate.

        The gate applies to caller-supplied models only; the operator's default
        keeps its declared EMBEDDING_DIMENSIONS, which is how a custom model's
        size is stated in the first place.
        """
        service, settings = _service_with_settings("")
        settings.embedding_model = "my-private-model"
        settings.embedding_dimensions = 1234

        # No embedding_model argument → falls through to the operator default,
        # which the gate must not police.
        assert await _gate_cleared(service, settings, workspace_id=MagicMock(), name="ctx"), (
            "operator-configured default was rejected by the gate"
        )


@pytest.mark.asyncio
class TestWorkspaceCreationUnderANarrowedAllowlist:
    """The caller the gate was moved down to catch must not be the one it breaks.

    Review of #1517 found that create_workspace() manufactured
    embedding_model="text-embedding-3-small" — a caller-supplied value as far as
    the gate is concerned — so on a deployment whose allowlist does not include
    it — the very case the setting exists for — every workspace creation 422'd,
    with no caller-side workaround.
    """

    async def test_create_workspace_passes_the_model_through_unchanged(self):
        import inspect

        from services.workspace_service import WorkspaceService

        src = inspect.getsource(WorkspaceService.create_workspace)
        assert '"text-embedding-3-small"' not in src, (
            "create_workspace must not manufacture an embedding model name — it "
            "turns the operator default into a gated caller-supplied value (#1517)."
        )
        assert "embedding_model=default_context_embedding_model," in src

    async def test_the_operator_default_is_used_when_the_caller_names_nothing(self):
        # With no caller-supplied model, the gate is skipped entirely and the
        # deployment's own EMBEDDING_MODEL applies — even under a narrow allowlist.
        service, settings = _service_with_settings("qwen3-embedding:4b")
        settings.embedding_model = "qwen3-embedding:4b"
        settings.embedding_dimensions = 2560
        assert await _gate_cleared(service, settings, workspace_id=MagicMock(), name="default"), (
            "workspace default context was rejected by the gate"
        )


class TestWorkspaceRouteAcceptsServableModels:
    def test_the_route_pattern_covers_the_whole_registry(self):
        """A BYOK caller on a self-hosted deployment needs a legal value to send."""
        import re

        from api.routes.workspaces import WorkspaceCreate
        from config.constants import EMBEDDING_MODEL_REGISTRY

        pattern = WorkspaceCreate.model_fields["default_context_embedding_model"].metadata
        rx = None
        for m in pattern:
            rx = getattr(m, "pattern", None) or rx
        assert rx, "expected a pattern constraint on default_context_embedding_model"
        for model in EMBEDDING_MODEL_REGISTRY:
            assert re.match(rx, model), f"{model} is in the registry but the route rejects it"


class TestErrorSurfacesDoNotAdvertiseRefusedModels:
    """REST and MCP must not name models the deployment will refuse.

    Review of #1517 found both route checks tested membership against the whole
    registry and listed every registry entry as "Supported" — so on a narrowed
    deployment the unknown-model error recommended models the service rejects,
    preserving exactly the disclosure this change removes from
    GET /system/embedding/models.
    """

    def test_rest_and_mcp_check_the_deployment_policy_not_the_raw_registry(self):
        import inspect

        from api.routes import contexts as rest_contexts
        from mcp_server.tools import context as mcp_contexts

        for module, label in ((rest_contexts, "REST"), (mcp_contexts, "MCP")):
            src = inspect.getsource(module)
            assert "allowed_embedding_models" in src, (
                f"the {label} create-context surface must validate against the "
                "deployment allowlist, not EMBEDDING_MODEL_REGISTRY (#1517)"
            )

    def test_neither_surface_still_lists_the_whole_registry(self):
        import inspect

        from api.routes import contexts as rest_contexts
        from mcp_server.tools import context as mcp_contexts

        for module, label in ((rest_contexts, "REST"), (mcp_contexts, "MCP")):
            src = inspect.getsource(module)
            assert "EMBEDDING_MODEL_REGISTRY.keys()" not in src, (
                f"the {label} error message must enumerate what the deployment "
                "offers, not every registry entry (#1517)"
            )
