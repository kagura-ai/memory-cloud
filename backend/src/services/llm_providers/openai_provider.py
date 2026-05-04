"""OpenAI LLM provider implementation.

Issue #546: Extracted from LLMService.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from services.llm_providers.base import LLMProvider, ProviderResponse, Usage
from utils.logger import get_logger

# Bound LLM request duration so a hung / unreachable provider cannot
# stall the analysis pipeline indefinitely (Issue #533 silent-hang
# mitigation; matches OpenAI SDK's `timeout=` kwarg semantics).
_LLM_REQUEST_TIMEOUT_S = 60.0

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI provider for structured JSON completions."""

    provider_name = "openai"

    def __init__(self, api_key: str):
        """Initialize OpenAI provider.

        Args:
            api_key: OpenAI API key.
        """
        self._client = AsyncOpenAI(
            api_key=api_key,
            timeout=_LLM_REQUEST_TIMEOUT_S,
        )

    @staticmethod
    def _supports_custom_temperature(model: str) -> bool:
        """OpenAI gpt-5 / o-series reasoning models reject any temperature
        value other than 1.

        Returns False for those models so the caller omits the kwarg and
        lets the SDK use the model's fixed default.
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
        """Build kwargs for ``AsyncOpenAI.chat.completions.create``.

        Branches on the model name:
        - GPT-4o / GPT-3.5 / non-OpenAI: pass ``temperature`` as given.
        - GPT-5 / o-series reasoning models: omit ``temperature`` (only
          default accepted) AND pass ``reasoning_effort="minimal"`` (#426).
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

    async def complete_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        model: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        **kwargs,
    ) -> ProviderResponse:
        """Call OpenAI with JSON mode."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        create_kwargs = self._build_create_kwargs(model, messages, temperature, max_tokens)

        response = await self._client.chat.completions.create(**create_kwargs)
        content = response.choices[0].message.content or "{}"
        usage = self.extract_usage(response)

        logger.debug(
            "openai_complete_json",
            model=model,
            tokens=usage.total,
            input=usage.input,
            cached=usage.cached,
            output=usage.output,
        )
        return ProviderResponse(content=content, usage=usage)

    def extract_usage(self, raw_response) -> Usage:
        """Read per-class token counts out of an OpenAI SDK response."""
        usage = getattr(raw_response, "usage", None)
        if usage is None:
            return Usage(total=0, input=0, output=0, cached=0)

        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        total = getattr(usage, "total_tokens", prompt + completion) or (prompt + completion)

        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
        cached = cached or 0

        standard_input = max(prompt - cached, 0)
        return Usage(
            total=total,
            input=standard_input,
            output=completion,
            cached=cached,
        )

    async def list_models(self) -> list[dict]:
        """List available OpenAI models."""
        try:
            response = await self._client.models.list()
            return [{"id": m.id, "name": m.id} for m in response.data if getattr(m, "id", None)]
        except Exception as e:
            logger.warning("openai_list_models_failed", error=str(e))
            return []
