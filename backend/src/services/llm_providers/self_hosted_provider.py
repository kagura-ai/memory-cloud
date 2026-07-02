"""Self-hosted LLM provider implementation.

Issue #546: Extracted from LLMService._get_client Ollama branch.
Issue #1160: Renamed ollama → self_hosted and normalized the transport to
pure OpenAI-compatible endpoints (``/v1/models`` for health + model listing)
so any self-hosted OpenAI-compatible backend — Ollama, vLLM, etc. — works
without an engine subfield.
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

# Placeholder sent to keyless backends (Ollama ignores it). A real key is
# used when SELF_HOSTED_API_KEY is configured (e.g. vLLM launched with
# ``--api-key``).
_PLACEHOLDER_API_KEY = "not-needed"

logger = get_logger(__name__)


class SelfHostedProvider(LLMProvider):
    """Self-hosted provider using the OpenAI-compatible API.

    Backs the ``self_hosted`` provider key. Talks pure OpenAI-compatible
    endpoints (``/v1/chat/completions`` for completions, ``/v1/models`` for
    health + listing), so it works against Ollama, vLLM, LM Studio,
    llama.cpp, TGI, and any other OpenAI-compatible server pointed at by
    ``base_url`` (e.g. ``http://localhost:11434`` for Ollama,
    ``http://localhost:8000`` for vLLM).
    """

    provider_name = "self_hosted"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        """Initialize the self-hosted provider.

        Args:
            base_url: OpenAI-compatible base URL (e.g. ``http://localhost:11434``
                for Ollama, ``http://localhost:8000`` for vLLM). Falls back to
                ``settings.self_hosted_base_url`` when omitted.
            api_key: Optional bearer token for backends that require one
                (e.g. vLLM started with ``--api-key``). Falls back to
                ``settings.self_hosted_api_key``; a keyless placeholder is
                used when neither is set (Ollama ignores it).
        """
        settings = get_settings()
        if base_url is None:
            base_url = settings.self_hosted_base_url or ""
        if api_key is None:
            api_key = settings.self_hosted_api_key or ""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._client = AsyncOpenAI(
            base_url=f"{self._base_url}/v1",
            api_key=self._api_key or _PLACEHOLDER_API_KEY,
            timeout=_LLM_REQUEST_TIMEOUT_S,
        )
        self._verified = False

    def _auth_headers(self) -> dict[str, str]:
        """Bearer header for backends started with an API key; empty for keyless."""
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def _verify(self) -> None:
        """Health-check the backend once per instance via ``GET /v1/models``.

        ``/v1/models`` is the OpenAI-compatible listing endpoint served by
        both Ollama and vLLM, so it is a portable liveness probe (unlike the
        bare root ``GET /`` that only Ollama answers with HTTP 200).
        """
        if self._verified:
            return

        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(f"{self._base_url}/v1/models", headers=self._auth_headers())
                if resp.status_code != 200:
                    raise ConfigurationError(
                        f"Self-hosted LLM backend not responding at {self._base_url} "
                        f"(HTTP {resp.status_code})"
                    )
        except httpx.HTTPError as err:
            raise ConfigurationError(
                f"Cannot connect to self-hosted LLM backend at {self._base_url}. "
                "Is your inference server running? (e.g. `ollama serve`, or a vLLM "
                "OpenAI-compatible server)"
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
        """Call the backend with JSON mode via the OpenAI-compatible endpoint."""
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
            "self_hosted_complete_json",
            model=model,
            tokens=usage.total,
            input=usage.input,
            output=usage.output,
        )
        return ProviderResponse(content=content, usage=usage)

    def extract_usage(self, raw_response) -> Usage:
        """Read usage from an OpenAI-compatible response.

        Ollama returns 0 for cached tokens (no cache concept). vLLM returns
        real ``prompt_tokens`` / ``completion_tokens``; ``cached`` stays 0
        because neither exposes a cache-read class.
        """
        usage = getattr(raw_response, "usage", None)
        if usage is None:
            return Usage(total=0, input=0, output=0, cached=0)

        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        total = getattr(usage, "total_tokens", prompt + completion) or (prompt + completion)

        return Usage(total=total, input=prompt, output=completion, cached=0)

    async def list_models(self) -> list[dict]:
        """List available models via the OpenAI-compatible ``GET /v1/models``.

        Both Ollama and vLLM expose ``/v1/models`` returning
        ``{"data": [{"id": ...}, ...]}``. This issues the GET directly (and
        treats any failure as an empty list) rather than gating on
        ``_verify()`` first, which would double the round-trip to the same
        endpoint.
        """
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.get(f"{self._base_url}/v1/models", headers=self._auth_headers())
                resp.raise_for_status()
                # ``data`` may be null (e.g. Ollama with no models pulled),
                # so coalesce to [] before iterating.
                models = resp.json().get("data") or []
                return [{"id": m["id"], "name": m.get("id")} for m in models if "id" in m]
        except Exception as e:
            logger.warning("self_hosted_list_models_failed", error=str(e))
            return []
