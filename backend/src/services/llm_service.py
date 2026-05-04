"""LLM service for Sleep Maintenance.

Issue #101: Multi-provider async LLM client for structured JSON completions.
Issue #385: API key retrieval is workspace-keyed, matching EmbeddingService.
Issue #546: Multi-provider adapter pattern (OpenAI, Anthropic, Gemini, Ollama).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import ExternalAPIKey
from services.llm_providers import (
    AnthropicProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAIProvider,
)
from services.llm_providers.base import LLMProvider, Usage
from utils.encryption import get_encryptor
from utils.exceptions import ConfigurationError
from utils.logger import get_logger

logger = get_logger(__name__)


class LLMServiceError(Exception):
    """Error from LLM service (parse failure, API error, etc.)."""


@dataclass(frozen=True)
class LLMResponse:
    """Cost-grade LLM response payload (Issue #471).

    Replaces the prior ``tuple[dict, int]`` return shape so callers can
    record per-(provider, model) cost breakdowns without re-deriving which
    model was used.

    **Back-compat**: this dataclass also unpacks as
    ``(parsed, total_tokens)`` via ``__iter__`` below.

    Token counts decompose ``response.usage`` for billing purposes:

    - ``input_tokens``: standard-rate input (net of cache).
    - ``cached_input_tokens``: cache-read portion.
    - ``output_tokens``: completion tokens.
    - ``total_tokens``: legacy aggregate.
    - ``cache_write_tokens``: cache-creation tokens (Anthropic only).
    """

    parsed: dict
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    provider: str
    model: str
    cache_write_tokens: int = 0
    tokenizer_version: str | None = None

    def __iter__(self):
        """Yield (parsed, total_tokens) so tuple-unpacking callers still work."""
        yield self.parsed
        yield self.total_tokens


# Provider registry ---------------------------------------------------------

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}

# Module-level model cache: {(provider_name, api_key_fingerprint): (timestamp, models)}
_MODEL_CACHE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_MODEL_CACHE_TTL_S = 30 * 60  # 30 minutes


def _api_key_fingerprint(key: str) -> str:
    """Stable short fingerprint for cache keying."""
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()[:16]


class LLMService:
    """Multi-provider async LLM client for Sleep Maintenance."""

    def __init__(self, db: AsyncSession):
        """Initialize LLM service.

        Args:
            db: Database session (for API key retrieval)
        """
        self.db = db
        self._ollama_provider: OllamaProvider | None = None
        self._last_ollama_base_url: str | None = None

    # -- public API --------------------------------------------------------

    async def complete_json(
        self,
        user_id: str,
        prompt: str,
        system_prompt: str | None = None,
        *,
        context_id: str | None = None,
        workspace_id: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        """Call LLM with JSON mode and return cost-grade response.

        Args:
            user_id: User ID for API key retrieval
            prompt: User message prompt
            system_prompt: Optional system message
            context_id: Optional context ID for scoped API key
            workspace_id: Optional workspace ID for scoped API key
            model: LLM model name (default: from config)
            provider: LLM provider (default: from config)
            temperature: Sampling temperature
            max_tokens: Max response tokens

        Returns:
            LLMResponse — parsed JSON + per-class token counts +
            (provider, model) identity.

        Raises:
            LLMServiceError: On API error or JSON parse failure after retry
        """
        resolved_model = model or os.getenv("SLEEP_LLM_MODEL", "gpt-5-nano")
        resolved_provider = provider or os.getenv("SLEEP_LLM_PROVIDER", "openai")

        provider_instance = await self._get_provider(
            user_id, resolved_provider, context_id, workspace_id
        )

        # First attempt
        first_usage = Usage(total=0, input=0, output=0, cached=0)
        content = ""
        try:
            response = await provider_instance.complete_json(
                prompt=prompt,
                system_prompt=system_prompt,
                model=resolved_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.content
            first_usage = response.usage
            parsed = json.loads(content)
            logger.debug(
                "llm_complete_json_success",
                provider=resolved_provider,
                model=resolved_model,
                tokens=first_usage.total,
                input=first_usage.input,
                cached=first_usage.cached,
                output=first_usage.output,
            )
            return self._build_response(
                parsed=parsed,
                first=first_usage,
                retry=None,
                provider=resolved_provider,
                model=resolved_model,
            )

        except json.JSONDecodeError:
            logger.warning(
                "llm_json_parse_failed_retrying",
                provider=resolved_provider,
                model=resolved_model,
                content_preview=content[:200],
            )

        except Exception as e:
            raise LLMServiceError(
                f"LLM API call failed ({resolved_provider}/{resolved_model}): {e}"
            ) from e

        # Retry with higher temperature (no-op on reasoning models that
        # only accept default — the provider omits temperature internally).
        try:
            response = await provider_instance.complete_json(
                prompt=prompt,
                system_prompt=system_prompt,
                model=resolved_model,
                temperature=0.3,
                max_tokens=max_tokens,
            )
            content = response.content
            retry_usage = response.usage
            parsed = json.loads(content)
            logger.info(
                "llm_complete_json_retry_success",
                provider=resolved_provider,
                model=resolved_model,
                tokens=first_usage.total + retry_usage.total,
            )
            return self._build_response(
                parsed=parsed,
                first=first_usage,
                retry=retry_usage,
                provider=resolved_provider,
                model=resolved_model,
            )

        except json.JSONDecodeError as e:
            raise LLMServiceError(
                f"LLM JSON parse failed after retry ({resolved_provider}/{resolved_model}): {e}"
            ) from e
        except Exception as e:
            raise LLMServiceError(
                f"LLM API call failed on retry ({resolved_provider}/{resolved_model}): {e}"
            ) from e

    async def list_models(
        self,
        user_id: str,
        provider: str,
        *,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict]:
        """Return available models for the given provider.

        Results are cached for 30 minutes per (provider, api_key) pair.

        Args:
            user_id: User ID for API key retrieval.
            provider: Provider name.
            context_id: Optional context ID for scoped API key.
            workspace_id: Optional workspace ID for scoped API key.

        Returns:
            List of model dicts with ``id`` and ``name`` keys.
        """
        # Resolve cache key up-front so we can skip instantiation on hit.
        try:
            api_key = await self._get_user_api_key(user_id, provider, context_id, workspace_id)
        except ConfigurationError:
            api_key = ""
        cache_key = (provider, _api_key_fingerprint(api_key))

        now = time.time()

        # Prune stale entries to cap memory growth (Copilot review feedback).
        stale = [k for k, (ts, _) in _MODEL_CACHE.items() if now - ts >= _MODEL_CACHE_TTL_S]
        for k in stale:
            del _MODEL_CACHE[k]

        # Check cache
        cached = _MODEL_CACHE.get(cache_key)
        if cached:
            timestamp, models = cached
            if now - timestamp < _MODEL_CACHE_TTL_S:
                logger.debug("llm_list_models_cache_hit", provider=provider)
                return models

        # Fetch from provider
        provider_instance = await self._get_provider(user_id, provider, context_id, workspace_id)
        try:
            models = await provider_instance.list_models()
        except Exception as e:
            logger.warning(
                "llm_list_models_failed",
                provider=provider,
                error=str(e),
            )
            # Fallback to stale cache if available
            if cached:
                return cached[1]
            return []

        _MODEL_CACHE[cache_key] = (now, models)
        return models

    # -- internal helpers --------------------------------------------------

    @staticmethod
    def _build_response(
        *,
        parsed: dict,
        first: Usage,
        retry: Usage | None,
        provider: str,
        model: str,
    ) -> LLMResponse:
        """Combine first-attempt and optional retry usage into one LLMResponse."""
        if retry is None:
            return LLMResponse(
                parsed=parsed,
                total_tokens=first.total,
                input_tokens=first.input,
                output_tokens=first.output,
                cached_input_tokens=first.cached,
                cache_write_tokens=first.cache_write,
                provider=provider,
                model=model,
            )

        return LLMResponse(
            parsed=parsed,
            total_tokens=first.total + retry.total,
            input_tokens=first.input + retry.input,
            output_tokens=first.output + retry.output,
            cached_input_tokens=first.cached + retry.cached,
            cache_write_tokens=first.cache_write + retry.cache_write,
            provider=provider,
            model=model,
        )

    async def _get_provider(
        self,
        user_id: str,
        provider_name: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> LLMProvider:
        """Instantiate the correct LLM provider with resolved API key."""
        provider_cls = _PROVIDERS.get(provider_name)
        if provider_cls is None:
            raise ConfigurationError(
                f"Unknown LLM provider: {provider_name}. Supported: {', '.join(_PROVIDERS)}"
            )

        if provider_name == "ollama":
            # Ollama uses a base URL rather than an API key. Resolve from
            # ExternalAPIKey first, then env var, then settings.
            base_url: str | None = None
            try:
                base_url = await self._get_user_api_key(user_id, "ollama", context_id, workspace_id)
            except ConfigurationError:
                pass
            if not base_url:
                base_url = os.getenv("OLLAMA_BASE_URL") or None
            if not base_url:
                from config.settings import get_settings

                base_url = get_settings().ollama_base_url or None

            # Re-use the same provider instance per service so the one-time
            # _verify() health-check does not run on every request.
            if self._ollama_provider is None or self._last_ollama_base_url != base_url:
                self._ollama_provider = OllamaProvider(base_url=base_url)
                self._last_ollama_base_url = base_url
            return self._ollama_provider

        api_key = await self._get_user_api_key(user_id, provider_name, context_id, workspace_id)
        return provider_cls(api_key)

    async def _get_user_api_key(
        self,
        user_id: str,
        provider: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        """Resolve the API key for the calling user's workspace context.

        Mirrors EmbeddingService._get_user_api_key — same Issue #385 contract:
        the lookup is workspace-keyed; user_id is for audit logging only.

        Priority:
        1. Context-scoped key (context_id matches AND workspace_id matches)
        2. Workspace-scoped key (workspace_id matches AND context_id IS NULL)
        3. Environment variable (OPENAI_API_KEY) — development fallback only

        Args:
            user_id: Caller's user ID — logged for audit, NOT used as a filter.
            provider: Provider name (e.g. ``"openai"``, ``"anthropic"``).
            context_id: Optional context UUID.
            workspace_id: Workspace UUID; when omitted the DB lookup is skipped.

        Returns:
            Decrypted API key.

        Raises:
            ConfigurationError: If neither a DB key nor an env var is available.
        """
        from uuid import UUID

        from sqlalchemy import or_

        api_key_entry = None
        if workspace_id:
            workspace_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
            conditions = [
                ExternalAPIKey.workspace_id == workspace_uuid,
                ExternalAPIKey.provider == provider,
                ExternalAPIKey.enabled.is_(True),
            ]
            if context_id:
                context_uuid = UUID(context_id) if isinstance(context_id, str) else context_id
                conditions.append(
                    or_(
                        ExternalAPIKey.context_id == context_uuid,
                        ExternalAPIKey.context_id.is_(None),
                    )
                )
            else:
                conditions.append(ExternalAPIKey.context_id.is_(None))

            query = (
                select(ExternalAPIKey)
                .where(and_(*conditions))
                .order_by(ExternalAPIKey.context_id.desc().nulls_last())
                .limit(1)
            )
            result = await self.db.execute(query)
            api_key_entry = result.scalar_one_or_none()

        if api_key_entry:
            encryptor = get_encryptor()
            api_key = encryptor.decrypt(str(api_key_entry.encrypted_value))
            logger.debug(
                "llm_api_key_from_db",
                provider=provider,
                user_id=user_id,
                context_id=context_id,
                workspace_id=workspace_id,
            )
            return api_key

        # Environment fallback — provider-specific env var first, then generic
        env_var_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "gemini": "GOOGLE_API_KEY",
        }
        env_var = env_var_map.get(provider, "OPENAI_API_KEY")
        env_key = os.getenv(env_var)
        if env_key:
            logger.debug("llm_api_key_from_env", provider=provider, user_id=user_id)
            return env_key

        if workspace_id:
            raise ConfigurationError(
                f"{provider} API key not configured for workspace {workspace_id}. "
                f"Configure a workspace {provider} API key in settings, or set the "
                f"{env_var} environment variable."
            )
        raise ConfigurationError(
            f"{provider} API key not configured: no workspace context was provided. "
            f"Provide a workspace_id, configure a workspace {provider} API key in "
            f"settings, or set the {env_var} environment variable."
        )
