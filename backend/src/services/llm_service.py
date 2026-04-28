"""LLM service for Sleep Maintenance.

Issue #101: Multi-provider async LLM client for structured JSON completions.
Supports OpenAI and Ollama via OpenAI-compatible API.

Issue #385: API key retrieval is workspace-keyed, matching EmbeddingService. The
current workspace's enabled OpenAI key is shared by every workspace member;
`user_id` on the key row is creator metadata only. Priority within a workspace:
context-scoped > workspace-scoped > env var fallback. No user-scoped tier.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from openai import AsyncOpenAI
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import ExternalAPIKey
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
    model was used. Dropping a field would have meant another tuple-unpack
    refactor across every phase; the dataclass scales cleanly.

    **Back-compat**: this dataclass also unpacks as
    ``(parsed, total_tokens)`` via ``__iter__`` below, so any caller
    that still uses the legacy tuple shape (``parsed, tokens = await
    complete_json(...)``) keeps working. New code should use the named
    fields directly to avoid silently dropping per-class token data.

    Token counts decompose ``response.usage`` for billing purposes:

    - ``input_tokens``: ``prompt_tokens`` - ``cached_input_tokens`` (the
      portion billed at the standard input rate).
    - ``cached_input_tokens``: from
      ``response.usage.prompt_tokens_details.cached_tokens`` when the
      provider exposes it (OpenAI / Anthropic prompt caching). 0 otherwise.
    - ``output_tokens``: ``response.usage.completion_tokens``.
    - ``total_tokens``: legacy aggregate (``prompt_tokens +
      completion_tokens``) — kept for the back-compat
      ``sleep_reports.llm_tokens_used`` roll-up column. New cost code
      should use the per-class fields, not this one.

    ``tokenizer_version`` is None today (OpenAI does not expose the
    tokenizer version in the API response). The field exists so future
    Anthropic-SDK or per-deployment-pinned model paths can fill it in
    without another return-shape change.
    """

    parsed: dict
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    provider: str
    model: str
    tokenizer_version: str | None = None

    def __iter__(self):
        """Yield (parsed, total_tokens) so tuple-unpacking callers still work.

        Pre-#471 the function returned ``tuple[dict, int]`` and callers
        wrote ``parsed, tokens = await complete_json(...)``. Yielding
        exactly two fields keeps that pattern functional. Three-element
        unpacking would raise the standard ValueError, which is fine —
        there was no 3-tuple legacy contract.
        """
        yield self.parsed
        yield self.total_tokens


@dataclass(frozen=True)
class _Usage:
    """Per-attempt token counts extracted from an OpenAI-compatible response.

    Internal to ``LLMService``. Decomposes ``response.usage`` into the
    billing-relevant classes (input net of cache, cached input, output)
    and a pre-summed ``total`` for back-compat logging.
    """

    total: int
    input: int
    output: int
    cached: int


_ZERO_USAGE = _Usage(total=0, input=0, output=0, cached=0)


def _extract_usage(response) -> _Usage:
    """Read per-class token counts out of an OpenAI-compatible response.

    Cached tokens come from ``response.usage.prompt_tokens_details.
    cached_tokens`` when the provider exposes the field (modern OpenAI;
    Ollama returns 0 because it has no cache concept). The cached portion
    is *included in* ``prompt_tokens``, so the standard-rate input is
    ``prompt_tokens - cached``.

    PROVIDER-SPECIFIC: this function reads the OpenAI SDK response shape.
    When a future provider with its own SDK is wired in (Anthropic
    direct, etc.), branch on a provider parameter or split into
    ``_extract_usage_openai`` / ``_extract_usage_anthropic``. Anthropic
    in particular reports caching separately as
    ``usage.cache_read_input_tokens`` / ``cache_creation_input_tokens``
    and won't fit through this OpenAI-shaped reader without changes.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return _ZERO_USAGE

    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    total = getattr(usage, "total_tokens", prompt + completion) or (prompt + completion)

    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details is not None else 0
    cached = cached or 0

    # Cached tokens are reported as a subset of prompt_tokens. The
    # standard-rate input is what's left after subtracting cache.
    standard_input = max(prompt - cached, 0)

    return _Usage(total=total, input=standard_input, output=completion, cached=cached)


def _build_response(
    *,
    parsed: dict,
    first: _Usage,
    retry: _Usage | None,
    provider: str,
    model: str,
) -> LLMResponse:
    """Combine first-attempt and (optional) retry usage into one LLMResponse.

    Retries inflate the token bill (we paid for the failed attempt too);
    matching the legacy behavior of ``tokens_used + retry_tokens`` for the
    aggregate total. Per-class fields sum the same way.
    """
    if retry is None:
        return LLMResponse(
            parsed=parsed,
            total_tokens=first.total,
            input_tokens=first.input,
            output_tokens=first.output,
            cached_input_tokens=first.cached,
            provider=provider,
            model=model,
        )

    return LLMResponse(
        parsed=parsed,
        total_tokens=first.total + retry.total,
        input_tokens=first.input + retry.input,
        output_tokens=first.output + retry.output,
        cached_input_tokens=first.cached + retry.cached,
        provider=provider,
        model=model,
    )


class LLMService:
    """Multi-provider async LLM client for Sleep Maintenance.

    Provides structured JSON completions via OpenAI or Ollama.
    Tracks token usage for cost reporting.
    """

    def __init__(self, db: AsyncSession):
        """Initialize LLM service.

        Args:
            db: Database session (for API key retrieval)
        """
        self.db = db
        self._ollama_verified = False

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
            temperature: Sampling temperature (low for deterministic JSON)
            max_tokens: Max response tokens

        Returns:
            LLMResponse — parsed JSON + per-class token counts +
            (provider, model) identity. See ``LLMResponse`` docstring for
            field semantics.

        Raises:
            LLMServiceError: On API error or JSON parse failure after retry
        """
        resolved_model = model or os.getenv("SLEEP_LLM_MODEL", "gpt-5-nano")
        resolved_provider = provider or os.getenv("SLEEP_LLM_PROVIDER", "openai")

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # First attempt
        content = ""
        first_usage = _ZERO_USAGE
        client = await self._get_client(user_id, resolved_provider, context_id, workspace_id)
        try:
            response = await client.chat.completions.create(
                **self._build_create_kwargs(resolved_model, messages, temperature, max_tokens),
            )
            content = response.choices[0].message.content or "{}"
            first_usage = _extract_usage(response)

            parsed = json.loads(content)
            logger.debug(
                "llm_complete_json_success",
                model=resolved_model,
                tokens=first_usage.total,
                input=first_usage.input,
                cached=first_usage.cached,
                output=first_usage.output,
            )
            return _build_response(
                parsed=parsed,
                first=first_usage,
                retry=None,
                provider=resolved_provider,
                model=resolved_model,
            )

        except json.JSONDecodeError:
            # Retry once with slightly higher temperature
            logger.warning(
                "llm_json_parse_failed_retrying",
                model=resolved_model,
                content_preview=content[:200],
            )

        except Exception as e:
            raise LLMServiceError(
                f"LLM API call failed ({resolved_provider}/{resolved_model}): {e}"
            ) from e

        # Retry with higher temperature (no-op on reasoning models that only accept default)
        try:
            response = await client.chat.completions.create(
                **self._build_create_kwargs(resolved_model, messages, 0.3, max_tokens),
            )
            content = response.choices[0].message.content or "{}"
            retry_usage = _extract_usage(response)

            parsed = json.loads(content)
            logger.info(
                "llm_complete_json_retry_success",
                model=resolved_model,
                tokens=first_usage.total + retry_usage.total,
            )
            return _build_response(
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

    @staticmethod
    def _supports_custom_temperature(model: str) -> bool:
        """OpenAI gpt-5 / o-series reasoning models reject any temperature value other than 1.

        Returns False for those models so the caller omits the kwarg and lets the
        SDK use the model's fixed default. Returns True for GPT-4o / GPT-3.5 / etc.
        """
        model_lower = model.lower()
        return not any(p in model_lower for p in ("gpt-5", "o1", "o3", "o4"))

    @classmethod
    def _build_create_kwargs(
        cls,
        model: str,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> dict:
        """Build kwargs for AsyncOpenAI.chat.completions.create.

        Branches on the model name:
        - GPT-4o / GPT-3.5 / non-OpenAI: pass `temperature` as given.
        - GPT-5 / o-series reasoning models: omit `temperature` (only default
          accepted) AND pass `reasoning_effort="minimal"` (#426). Without the
          latter, reasoning tokens consume the entire `max_completion_tokens`
          budget and `response.choices[0].message.content` comes back empty
          (or `"{}"`). The downstream JSON-mode pipeline then parses an empty
          dict, the parser sees no `edges` key, and edge_discovery silently
          classifies nothing. `minimal` matches OpenAI's recommended setting
          for deterministic extraction/classification tasks.
        """
        kwargs: dict = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if cls._supports_custom_temperature(model):
            kwargs["temperature"] = temperature
        else:
            kwargs["reasoning_effort"] = "minimal"
        return kwargs

    async def _get_client(
        self,
        user_id: str,
        provider: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AsyncOpenAI:
        """Get OpenAI-compatible client for the specified provider.

        Args:
            user_id: User ID for API key lookup
            provider: "openai" or "ollama"
            context_id: Optional context scope
            workspace_id: Optional workspace scope

        Returns:
            AsyncOpenAI client configured for the provider
        """
        if provider == "ollama":
            from config.settings import get_settings

            ollama_base_url = get_settings().ollama_base_url

            if not self._ollama_verified:
                import httpx

                try:
                    async with httpx.AsyncClient(timeout=5.0) as http:
                        resp = await http.get(ollama_base_url)
                        if resp.status_code != 200:
                            raise ConfigurationError(
                                f"Ollama not responding at {ollama_base_url} "
                                f"(HTTP {resp.status_code})"
                            )
                except httpx.ConnectError as err:
                    raise ConfigurationError(
                        f"Cannot connect to Ollama at {ollama_base_url}. "
                        "Is Ollama running? Start with: ollama serve"
                    ) from err
                self._ollama_verified = True

            return AsyncOpenAI(
                base_url=f"{ollama_base_url}/v1",
                api_key="ollama",
            )

        # OpenAI (default)
        api_key = await self._get_user_api_key(user_id, context_id, workspace_id)
        return AsyncOpenAI(api_key=api_key)

    async def _get_user_api_key(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> str:
        """Resolve the LLM (OpenAI) API key for the calling user's workspace context.

        Mirrors EmbeddingService._get_user_api_key — same Issue #385 contract:
        the lookup is workspace-keyed; user_id is for audit logging only, not a
        visibility filter. Any workspace member uses the owner-registered key.

        Priority:
        1. Context-scoped key (context_id matches AND workspace_id matches)
        2. Workspace-scoped key (workspace_id matches AND context_id IS NULL)
        3. Environment variable (OPENAI_API_KEY) — development fallback only

        Args:
            user_id: Caller's user ID — logged for audit, NOT used as a filter (#385).
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
                ExternalAPIKey.provider == "openai",
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
                user_id=user_id,
                context_id=context_id,
                workspace_id=workspace_id,
            )
            return api_key

        env_key = os.getenv("OPENAI_API_KEY")
        if env_key:
            logger.debug("llm_api_key_from_env", user_id=user_id)
            return env_key

        if workspace_id:
            raise ConfigurationError(
                f"OpenAI API key not configured for workspace {workspace_id}. "
                "Configure a workspace OpenAI API key in settings, or set the "
                "OPENAI_API_KEY environment variable."
            )
        raise ConfigurationError(
            "OpenAI API key not configured: no workspace context was provided. "
            "Provide a workspace_id, configure a workspace OpenAI API key in "
            "settings, or set the OPENAI_API_KEY environment variable."
        )
