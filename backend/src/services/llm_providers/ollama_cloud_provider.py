"""Ollama Cloud LLM provider implementation.

Issue #546: Ollama Cloud adapter using the native ollama Python SDK.
Cloud models run on ollama.com and require an API key for authentication.
"""

from __future__ import annotations

from services.llm_providers.base import LLMProvider, ProviderResponse, Usage
from utils.exceptions import ConfigurationError
from utils.logger import get_logger

# Bound LLM request duration so a hung / unreachable provider cannot
# stall the analysis pipeline indefinitely (Issue #533 silent-hang
# mitigation).
_LLM_REQUEST_TIMEOUT_S = 60.0

logger = get_logger(__name__)


class OllamaCloudProvider(LLMProvider):
    """Ollama Cloud provider using the native ollama SDK.

    Authentication is via ``Authorization: Bearer <api_key>`` header
    against ``https://ollama.com``.
    """

    provider_name = "ollama_cloud"

    def __init__(self, api_key: str):
        """Initialize Ollama Cloud provider.

        Args:
            api_key: Ollama Cloud API key.
        """
        self._api_key = api_key
        self._client = None

    def _ensure_client(self):
        """Lazy-load ollama AsyncClient (cached per instance)."""
        if self._client is not None:
            return self._client
        try:
            import ollama
        except ImportError as exc:
            raise ConfigurationError(
                "ollama SDK not installed. Install with: pip install ollama"
            ) from exc
        self._client = ollama.AsyncClient(
            host="https://ollama.com",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        return self._client

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
        """Call Ollama Cloud with JSON mode."""
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = await self._ensure_client().chat(
            model=model,
            messages=messages,
            format="json",
            options={
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        )
        content = getattr(response, "message", {}).get("content", "{}") or "{}"
        usage = self.extract_usage(response)

        logger.debug(
            "ollama_cloud_complete_json",
            model=model,
            tokens=usage.total,
            input=usage.input,
            output=usage.output,
        )
        return ProviderResponse(content=content, usage=usage)

    def extract_usage(self, raw_response) -> Usage:
        """Read token counts from an Ollama SDK response.

        Ollama reports ``prompt_eval_count`` (input) and ``eval_count``
        (output).  Cloud models do not expose cache-read / cache-write
        tokens, so both are left at 0.
        """
        prompt = getattr(raw_response, "prompt_eval_count", 0) or 0
        completion = getattr(raw_response, "eval_count", 0) or 0
        total = prompt + completion

        return Usage(total=total, input=prompt, output=completion, cached=0)

    async def list_models(self) -> list[dict]:
        """List available Ollama Cloud models."""
        try:
            response = await self._ensure_client().list()
            models = getattr(response, "models", []) or response.get("models", [])
            return [{"id": m.model, "name": m.model} for m in models if getattr(m, "model", None)]
        except Exception as e:
            logger.warning("ollama_cloud_list_models_failed", error=str(e))
            return []
