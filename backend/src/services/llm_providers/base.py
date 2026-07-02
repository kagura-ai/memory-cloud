"""Abstract base class for LLM providers.

Issue #546: Adapter pattern for multi-provider LLM support.
Follows the RerankerProvider ABC design from reranker_service.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Usage:
    """Per-attempt token counts extracted from a provider response.

    Decomposes the provider's usage object into billing-relevant classes.
    ``cache_write_tokens`` exists so Anthropic's
    ``cache_creation_input_tokens`` can be captured; providers that do not
    expose cache-write (OpenAI, Gemini, self-hosted backends) leave it at 0.
    """

    total: int
    input: int
    output: int
    cached: int
    cache_write: int = 0


@dataclass(frozen=True)
class ProviderResponse:
    """Raw response from an LLM provider before JSON parsing.

    ``LLMService.complete_json`` consumes this, parses ``content`` as JSON,
    and wraps the result in the public ``LLMResponse`` dataclass.
    """

    content: str
    usage: Usage


class LLMProvider(ABC):
    """Abstract base class for LLM providers.

    All LLM implementations must inherit from this class and implement
    ``complete_json`` and ``list_models``.
    """

    provider_name: str

    def __init__(self, api_key: str) -> None:
        """Initialize the provider with an API key.

        Subclasses override to accept variations (e.g. ``base_url`` for the
        self-hosted provider) and typically do NOT call ``super().__init__`` — the stored
        attribute here is a default in case they do. The base body is what
        lets ``provider_cls(api_key)`` in :class:`LLMService._instantiate_provider`
        type-check against ``type[LLMProvider]`` without ruff's B027 firing
        on an empty ABC method.
        """
        self.api_key = api_key

    @abstractmethod
    async def complete_json(
        self,
        prompt: str,
        system_prompt: str | None = None,
        *,
        model: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Call the LLM and return raw text + usage.

        Args:
            prompt: User message content.
            system_prompt: Optional system instruction.
            model: Exact model identifier (e.g. ``"gpt-5-nano"``).
            temperature: Sampling temperature.
            max_tokens: Maximum completion tokens.
            **kwargs: Reserved for future extensions (multimodal inputs,
                reasoning effort, etc.).

        Returns:
            ProviderResponse with ``content`` (raw text) and ``usage``.
        """
        pass

    @abstractmethod
    async def list_models(self) -> list[dict[str, Any]]:
        """Return available models for this provider.

        Returns:
            List of dicts with at least ``id`` and ``name`` keys.
        """
        pass
