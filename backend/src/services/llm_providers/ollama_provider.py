"""Ollama LLM provider implementation.

Issue #546: Extracted from LLMService._get_client Ollama branch.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from config.settings import get_settings
from services.llm_providers.base import LLMProvider, ProviderResponse, Usage
from utils.exceptions import ConfigurationError
from utils.logger import get_logger

# Bound LLM request duration so a hung / unreachable provider cannot
# stall the analysis pipeline indefinitely (Issue #533 silent-hang
# mitigation; matches OpenAI SDK's `timeout=` kwarg semantics).
_LLM_REQUEST_TIMEOUT_S = 60.0

logger = get_logger(__name__)


class OllamaProvider(LLMProvider):
    """Ollama provider using OpenAI-compatible API."""

    provider_name = "ollama"

    def __init__(self, base_url: str | None = None):
        """Initialize Ollama provider.

        Args:
            base_url: Ollama API base URL (e.g. ``http://localhost:11434``).
                Falls back to ``settings.ollama_base_url`` when omitted.
        """
        if base_url is None:
            base_url = get_settings().ollama_base_url or ""
        self._base_url = base_url.rstrip("/")
        self._client = AsyncOpenAI(
            base_url=f"{self._base_url}/v1",
            api_key="ollama",
            timeout=_LLM_REQUEST_TIMEOUT_S,
        )
        self._verified = False

    async def _verify(self) -> None:
        """Health-check Ollama once per instance."""
        if self._verified:
            return

        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(self._base_url)
                if resp.status_code != 200:
                    raise ConfigurationError(
                        f"Ollama not responding at {self._base_url} (HTTP {resp.status_code})"
                    )
        except httpx.ConnectError as err:
            raise ConfigurationError(
                f"Cannot connect to Ollama at {self._base_url}. "
                "Is Ollama running? Start with: ollama serve"
            ) from err

        self._verified = True

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
        """Call Ollama with JSON mode via OpenAI-compatible endpoint."""
        await self._verify()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        usage = self.extract_usage(response)

        logger.debug(
            "ollama_complete_json",
            model=model,
            tokens=usage.total,
            input=usage.input,
            output=usage.output,
        )
        return ProviderResponse(content=content, usage=usage)

    def extract_usage(self, raw_response) -> Usage:
        """Read usage from an OpenAI-compatible response.

        Ollama returns 0 for cached tokens because it has no cache concept.
        """
        usage = getattr(raw_response, "usage", None)
        if usage is None:
            return Usage(total=0, input=0, output=0, cached=0)

        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        total = getattr(usage, "total_tokens", prompt + completion) or (prompt + completion)

        return Usage(total=total, input=prompt, output=completion, cached=0)

    async def list_models(self) -> list[dict]:
        """List available Ollama models."""
        await self._verify()

        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(f"{self._base_url}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                models = data.get("models", [])
                return [
                    {"id": m["name"], "name": m.get("model", m["name"])}
                    for m in models
                    if "name" in m
                ]
        except Exception as e:
            logger.warning("ollama_list_models_failed", error=str(e))
            return []
