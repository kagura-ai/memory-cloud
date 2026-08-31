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
            try:
                await _create(
                    service,
                    settings,
                    workspace_id=MagicMock(),
                    name="ctx",
                    embedding_model=model,
                )
            except ValidationError as exc:  # pragma: no cover - failure detail
                pytest.fail(f"{model} was rejected under the default allowlist: {exc}")
            except Exception:
                # Anything past the gate (DB work on mocks) is fine — the gate
                # is what these tests are about.
                pass


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
        # which must NOT raise.
        try:
            await _create(service, settings, workspace_id=MagicMock(), name="ctx")
        except ValidationError as exc:  # pragma: no cover - failure detail
            pytest.fail(f"operator-configured default was rejected: {exc}")
        except Exception:
            pass
