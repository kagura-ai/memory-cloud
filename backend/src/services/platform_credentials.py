"""Platform credentials of the LLM lanes, from settings and env only (Issue #2768).

The operations probe and the lane snapshot (both run by the scheduler leader)
build their provider clients here. Nothing in this module takes a database
session, a workspace, a context or a user, and nothing here imports the
services that resolve a workspace's own key (the LLM, embedding and reranker
services) or the stored-key model: a probe can only ever reach what the
deployment itself configured. ``tests/services/test_platform_credentials.py``
pins this with an import-graph check and a signature check.

Every client built here has SDK retries off, so one probe is one request.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from config.constants import EMBEDDING_MODEL_REGISTRY
from config.self_hosted_aliases import resolve_self_hosted_model_id
from config.settings import MANAGED_LLM_PLATFORM_KEY_ENV
from services.llm_providers import (
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    OllamaCloudProvider,
    OpenAIProvider,
    SelfHostedProvider,
)
from services.reranker_service import (
    DEFAULT_SELF_HOSTED_RERANK_MODEL,
    DEFAULT_VLLM_RERANK_MODEL,
    SelfHostedReranker,
    VLLMReranker,
)

if TYPE_CHECKING:
    from config.settings import Settings
    from services.reranker_service import RerankerProvider

# The env variable holding each API-key provider's platform credential. The
# managed lane's map plus the providers only the Sleep judge can name.
PLATFORM_LLM_KEY_ENV: dict[str, str] = {
    **MANAGED_LLM_PLATFORM_KEY_ENV,
    "ollama_cloud": "OLLAMA_API_KEY",
}

_API_KEY_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama_cloud": OllamaCloudProvider,
}

# Timeout of the embedding probe client (the probe also bounds every call).
_EMBEDDING_CLIENT_TIMEOUT_SECONDS = 20.0
_KEYLESS_PLACEHOLDER = "not-needed"


def platform_llm_credential(provider: str, settings: Settings) -> str | None:
    """The platform credential of an LLM ``provider``, or ``None``.

    - ``self_hosted``: the base URL (``SELF_HOSTED_BASE_URL`` env, then the
      setting), as ``LLMService`` resolves it for the platform lane.
    - an API-key provider: its env variable (:data:`PLATFORM_LLM_KEY_ENV`).
    - anything else: ``None`` (no fallback to another provider's key).
    """
    if provider == "self_hosted":
        return os.getenv("SELF_HOSTED_BASE_URL") or settings.self_hosted_base_url or None
    env_var = PLATFORM_LLM_KEY_ENV.get(provider)
    if env_var is None:
        return None
    return os.getenv(env_var) or None


def _without_retries(provider: LLMProvider) -> LLMProvider:
    """Turn the SDK's own retries off on ``provider``'s client, where it has any."""
    ensure = getattr(provider, "_ensure_client", None)
    try:
        client = ensure() if callable(ensure) else getattr(provider, "_client", None)
    except Exception:  # noqa: BLE001 - an SDK that is not installed fails at the call
        return provider
    with_options = getattr(client, "with_options", None)
    if callable(with_options):
        try:
            provider._client = with_options(max_retries=0)  # type: ignore[attr-defined]
        except TypeError:
            pass
    return provider


def platform_llm_provider(provider: str, settings: Settings) -> LLMProvider | None:
    """A provider client on the platform credential, or ``None`` when there is none."""
    credential = platform_llm_credential(provider, settings)
    if credential is None:
        return None
    if provider == "self_hosted":
        return _without_retries(SelfHostedProvider(base_url=credential))
    provider_cls = _API_KEY_PROVIDERS.get(provider)
    if provider_cls is None:
        return None
    return _without_retries(provider_cls(credential))


@dataclass(frozen=True)
class EmbeddingLane:
    """The deployment's default embedding lane (what recall's query embeds use)."""

    provider: str
    model: str
    dimensions: int


def embedding_lane(settings: Settings) -> EmbeddingLane:
    """Provider, model and dimensions of the deployment's default embedding model."""
    model = settings.embedding_model
    if model in EMBEDDING_MODEL_REGISTRY:
        dimensions, provider = EMBEDDING_MODEL_REGISTRY[model]
    else:
        dimensions, provider = settings.embedding_dimensions, settings.embedding_provider
    return EmbeddingLane(provider=provider, model=model, dimensions=dimensions)


def embedding_credential_present(settings: Settings) -> bool:
    """Whether the leader holds the embedding lane's platform credential."""
    provider = embedding_lane(settings).provider
    if provider == "self_hosted":
        return bool(settings.self_hosted_base_url)
    if provider == "openai":
        return bool(os.getenv("OPENAI_API_KEY"))
    return False


@dataclass(frozen=True)
class PlatformEmbeddingClient:
    """An embeddings client on the platform credential (cache bypassed)."""

    client: AsyncOpenAI
    wire_model: str
    extra: dict[str, Any] = field(default_factory=dict)


def platform_embedding_client(settings: Settings) -> PlatformEmbeddingClient | None:
    """The default embedding lane's client, or ``None`` without a credential."""
    lane = embedding_lane(settings)
    if lane.provider == "self_hosted":
        base_url = settings.self_hosted_base_url
        if not base_url:
            return None
        client = AsyncOpenAI(
            base_url=f"{base_url}/v1",
            api_key=settings.self_hosted_api_key or _KEYLESS_PLACEHOLDER,
            timeout=_EMBEDDING_CLIENT_TIMEOUT_SECONDS,
            max_retries=0,
        )
        wire_model = resolve_self_hosted_model_id(lane.model, settings.self_hosted_model_aliases)
        return PlatformEmbeddingClient(client=client, wire_model=wire_model)
    if lane.provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        client = AsyncOpenAI(
            api_key=api_key, timeout=_EMBEDDING_CLIENT_TIMEOUT_SECONDS, max_retries=0
        )
        return PlatformEmbeddingClient(
            client=client, wire_model=lane.model, extra={"dimensions": lane.dimensions}
        )
    return None


@dataclass(frozen=True)
class RerankLane:
    """The platform rerank lane: always self-hosted (API-key rerankers are BYOK only)."""

    provider: str
    model: str


def rerank_lane(settings: Settings) -> RerankLane | None:
    """The platform rerank lane, or ``None`` when the deployment has none.

    Reranking must be on, and served by ``RERANK_BASE_URL`` or by the
    self-hosted backend as the default reranker of new contexts.
    """
    if not settings.enable_reranking:
        return None
    if settings.rerank_base_url:
        return RerankLane(
            provider="self_hosted", model=settings.rerank_model or DEFAULT_VLLM_RERANK_MODEL
        )
    if settings.default_reranker_provider == "self_hosted" and settings.self_hosted_base_url:
        return RerankLane(
            provider="self_hosted",
            model=settings.self_hosted_rerank_model or DEFAULT_SELF_HOSTED_RERANK_MODEL,
        )
    return None


def platform_reranker(settings: Settings) -> RerankerProvider | None:
    """The platform rerank lane's reranker, built from settings only."""
    lane = rerank_lane(settings)
    if lane is None:
        return None
    if settings.rerank_base_url:
        return VLLMReranker(
            base_url=settings.rerank_base_url,
            model=lane.model,
            api_key=settings.rerank_api_key or None,
            prompt_format=settings.rerank_prompt_format,
            instruction=settings.rerank_instruction,
        )
    return SelfHostedReranker(
        base_url=settings.self_hosted_base_url,
        model=lane.model,
        api_key=settings.self_hosted_api_key or None,
        prompt_format=settings.rerank_prompt_format,
        instruction=settings.rerank_instruction,
    )


__all__ = [
    "PLATFORM_LLM_KEY_ENV",
    "EmbeddingLane",
    "PlatformEmbeddingClient",
    "RerankLane",
    "embedding_credential_present",
    "embedding_lane",
    "platform_embedding_client",
    "platform_llm_credential",
    "platform_llm_provider",
    "platform_reranker",
    "rerank_lane",
]
