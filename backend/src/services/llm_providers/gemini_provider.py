"""Google Gemini LLM provider implementation.

Issue #546: Gemini adapter for multimodal-ready LLM support.
"""

from __future__ import annotations

from services.llm_providers.base import LLMProvider, ProviderResponse, Usage
from utils.exceptions import ConfigurationError
from utils.logger import get_logger

logger = get_logger(__name__)


class GeminiProvider(LLMProvider):
    """Google Gemini provider for structured JSON completions."""

    provider_name = "gemini"

    def __init__(self, api_key: str):
        """Initialize Gemini provider.

        Args:
            api_key: Google AI Studio API key.
        """
        self._api_key = api_key

    def _client(self):
        """Lazy-load Google GenAI client."""
        try:
            from google import genai
        except ImportError as exc:
            raise ConfigurationError(
                "Google GenAI SDK not installed. Install with: pip install google-genai"
            ) from exc
        return genai.Client(api_key=self._api_key)

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
        """Call Gemini with JSON mode."""
        client = self._client()

        contents = prompt
        config_kwargs: dict = {
            "response_mime_type": "application/json",
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt

        try:
            from google.genai import types

            config = types.GenerateContentConfig(**config_kwargs)
        except Exception:
            # Fallback for older SDK shapes
            config = config_kwargs

        response = await client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        content = getattr(response, "text", "") or "{}"
        usage = self.extract_usage(response)

        logger.debug(
            "gemini_complete_json",
            model=model,
            tokens=usage.total,
            input=usage.input,
            output=usage.output,
        )
        return ProviderResponse(content=content, usage=usage)

    def extract_usage(self, raw_response) -> Usage:
        """Read Gemini usage metadata."""
        meta = getattr(raw_response, "usage_metadata", None)
        if meta is None:
            return Usage(total=0, input=0, output=0, cached=0)

        prompt = getattr(meta, "prompt_token_count", 0) or 0
        completion = getattr(meta, "candidates_token_count", 0) or 0
        total = getattr(meta, "total_token_count", prompt + completion) or (prompt + completion)

        # Gemini does not expose cache-read / cache-write tokens in the
        # current API response; leave both at 0.
        return Usage(
            total=total,
            input=prompt,
            output=completion,
            cached=0,
        )

    async def list_models(self) -> list[dict]:
        """List available Gemini models."""
        client = self._client()
        try:
            response = await client.aio.models.list()
            return [
                {"id": m.name, "name": m.display_name or m.name}
                for m in response.models
                if getattr(m, "name", None)
            ]
        except Exception as e:
            logger.warning("gemini_list_models_failed", error=str(e))
            return [
                {"id": "gemini-3.1-pro", "name": "Gemini 3.1 Pro"},
                {"id": "gemini-3.1-flash-lite", "name": "Gemini 3.1 Flash-Lite"},
            ]
