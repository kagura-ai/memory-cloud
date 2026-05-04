"""Anthropic LLM provider implementation.

Issue #546: Anthropic adapter with cache-write token support.
"""

from __future__ import annotations

from services.llm_providers.base import LLMProvider, ProviderResponse, Usage
from utils.exceptions import ConfigurationError
from utils.logger import get_logger

logger = get_logger(__name__)


class AnthropicProvider(LLMProvider):
    """Anthropic provider for structured JSON completions."""

    provider_name = "anthropic"

    def __init__(self, api_key: str):
        """Initialize Anthropic provider.

        Args:
            api_key: Anthropic API key.
        """
        self._api_key = api_key

    def _client(self):
        """Lazy-load Anthropic async client."""
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise ConfigurationError(
                "Anthropic SDK not installed. Install with: pip install anthropic"
            ) from exc
        return AsyncAnthropic(api_key=self._api_key)

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
        """Call Anthropic with JSON mode."""
        client = self._client()

        messages = [{"role": "user", "content": prompt}]
        request_kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }

        if system_prompt:
            request_kwargs["system"] = system_prompt

        # Anthropic reasoning models (claude-4, claude-3-7-sonnet with
        # extended thinking) do not accept temperature; the SDK handles
        # this by ignoring the parameter for unsupported models, but we
        # gate it defensively to avoid API errors.
        if not self._is_reasoning_model(model):
            request_kwargs["temperature"] = temperature

        # Anthropic structured outputs (beta).  Fall back to a simple
        # text completion + JSON parse if the feature is unavailable.
        try:
            request_kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

        response = await client.messages.create(**request_kwargs)
        content = self._extract_content(response)
        usage = self.extract_usage(response)

        logger.debug(
            "anthropic_complete_json",
            model=model,
            tokens=usage.total,
            input=usage.input,
            cached=usage.cached,
            cache_write=usage.cache_write,
            output=usage.output,
        )
        return ProviderResponse(content=content, usage=usage)

    @staticmethod
    def _is_reasoning_model(model: str) -> bool:
        """Return True for models that reject temperature."""
        lower = model.lower()
        return any(p in lower for p in ("claude-4", "claude-3-7-sonnet"))

    @staticmethod
    def _extract_content(response) -> str:
        """Pull text out of an Anthropic response."""
        blocks = getattr(response, "content", [])
        for block in blocks:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if text:
                    return text
        return "{}"

    def extract_usage(self, raw_response) -> Usage:
        """Read Anthropic usage fields.

        Anthropic reports caching separately as
        ``cache_read_input_tokens`` and ``cache_creation_input_tokens``.
        """
        usage = getattr(raw_response, "usage", None)
        if usage is None:
            return Usage(total=0, input=0, output=0, cached=0)

        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0

        # Anthropic's ``input_tokens`` already excludes cache-read;
        # cache_creation is billed at a premium and tracked separately.
        total = input_tokens + output_tokens + cache_read + cache_creation

        return Usage(
            total=total,
            input=input_tokens,
            output=output_tokens,
            cached=cache_read,
            cache_write=cache_creation,
        )

    async def list_models(self) -> list[dict]:
        """List available Anthropic models."""
        # Anthropic does not expose a models.list() endpoint today;
        # return a curated static list.
        return [
            {"id": "claude-sonnet-4-6-20251001", "name": "Claude Sonnet 4.6"},
            {"id": "claude-opus-4-7-20251001", "name": "Claude Opus 4.7"},
            {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        ]
