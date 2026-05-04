"""Tests for LLM provider adapters.

Issue #546: Verify each provider's extract_usage and complete_json shape.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.llm_providers.anthropic_provider import AnthropicProvider
from services.llm_providers.base import Usage
from services.llm_providers.gemini_provider import GeminiProvider
from services.llm_providers.ollama_provider import OllamaProvider
from services.llm_providers.openai_provider import OpenAIProvider

# ============================================================================
# Helpers
# ============================================================================


def _make_openai_usage(prompt=10, completion=5, cached=2):
    """Build a mock OpenAI usage object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt
    usage.completion_tokens = completion
    usage.total_tokens = prompt + completion
    details = MagicMock()
    details.cached_tokens = cached
    usage.prompt_tokens_details = details
    return usage


def _make_anthropic_usage(input_tokens=10, output_tokens=5, cache_read=2, cache_creation=3):
    """Build a mock Anthropic usage object."""
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_read_input_tokens = cache_read
    usage.cache_creation_input_tokens = cache_creation
    return usage


def _make_gemini_usage(prompt=10, completion=5):
    """Build a mock Gemini usage_metadata object."""
    usage = MagicMock(spec=["prompt_token_count", "candidates_token_count", "total_token_count"])
    usage.prompt_token_count = prompt
    usage.candidates_token_count = completion
    usage.total_token_count = prompt + completion
    return usage


# ============================================================================
# OpenAIProvider
# ============================================================================


class TestOpenAIProvider:
    def test_extract_usage_reads_cached_tokens(self):
        provider = OpenAIProvider(api_key="sk-test")
        raw = MagicMock()
        raw.usage = _make_openai_usage(prompt=100, completion=20, cached=30)

        usage = provider.extract_usage(raw)
        assert usage == Usage(total=120, input=70, output=20, cached=30)

    def test_extract_usage_handles_no_usage(self):
        provider = OpenAIProvider(api_key="sk-test")
        # spec=[] prevents MagicMock from dynamically creating attributes
        usage = provider.extract_usage(MagicMock(spec=[]))
        assert usage == Usage(total=0, input=0, output=0, cached=0)

    def test_extract_usage_handles_no_cached_details(self):
        provider = OpenAIProvider(api_key="sk-test")
        raw = MagicMock()
        raw.usage = MagicMock()
        raw.usage.prompt_tokens = 50
        raw.usage.completion_tokens = 10
        raw.usage.total_tokens = 60
        raw.usage.prompt_tokens_details = None

        usage = provider.extract_usage(raw)
        assert usage == Usage(total=60, input=50, output=10, cached=0)

    @pytest.mark.asyncio
    async def test_complete_json_returns_provider_response(self):
        provider = OpenAIProvider(api_key="sk-test")
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"key": "value"}'))]
        mock_response.usage = _make_openai_usage(prompt=10, completion=5, cached=2)

        with patch.object(
            provider._client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await provider.complete_json("hello", model="gpt-4o")

        assert result.content == '{"key": "value"}'
        assert result.usage == Usage(total=15, input=8, output=5, cached=2)

    def test_supports_custom_temperature(self):
        assert OpenAIProvider._supports_custom_temperature("gpt-4o") is True
        assert OpenAIProvider._supports_custom_temperature("gpt-5-nano") is False
        assert OpenAIProvider._supports_custom_temperature("o3-mini") is False

    def test_build_create_kwargs_gpt5_omits_temperature(self):
        """Regression guard (#424): gpt-5 / o-series must not receive temperature kwarg."""
        kwargs = OpenAIProvider._build_create_kwargs(
            "gpt-5-nano",
            [{"role": "user", "content": "test"}],
            temperature=0.1,
            max_tokens=1024,
        )
        assert "temperature" not in kwargs
        assert kwargs["model"] == "gpt-5-nano"

    def test_build_create_kwargs_gpt5_includes_reasoning_effort(self):
        """Regression guard (#426): gpt-5 / o-series must receive reasoning_effort=minimal."""
        kwargs = OpenAIProvider._build_create_kwargs(
            "gpt-5-nano",
            [{"role": "user", "content": "test"}],
            temperature=0.1,
            max_tokens=1024,
        )
        assert kwargs["reasoning_effort"] == "minimal"

    def test_build_create_kwargs_non_reasoning_includes_temperature(self):
        """Non-reasoning models should pass temperature as given."""
        kwargs = OpenAIProvider._build_create_kwargs(
            "gpt-4o",
            [{"role": "user", "content": "test"}],
            temperature=0.7,
            max_tokens=512,
        )
        assert kwargs["temperature"] == 0.7
        assert "reasoning_effort" not in kwargs
        assert kwargs["max_completion_tokens"] == 512

    @pytest.mark.asyncio
    async def test_list_models_returns_models(self):
        provider = OpenAIProvider(api_key="sk-test")
        mock_model = MagicMock()
        mock_model.id = "gpt-4o"
        mock_list = MagicMock()
        mock_list.data = [mock_model]

        with patch.object(
            provider._client.models, "list", new_callable=AsyncMock, return_value=mock_list
        ):
            models = await provider.list_models()

        assert models == [{"id": "gpt-4o", "name": "gpt-4o"}]


# ============================================================================
# AnthropicProvider
# ============================================================================


class TestAnthropicProvider:
    def test_extract_usage_reads_cache_write(self):
        provider = AnthropicProvider(api_key="sk-ant-test")
        raw = MagicMock()
        raw.usage = _make_anthropic_usage(
            input_tokens=100, output_tokens=20, cache_read=30, cache_creation=10
        )

        usage = provider.extract_usage(raw)
        assert usage == Usage(total=160, input=100, output=20, cached=30, cache_write=10)

    def test_extract_usage_handles_no_usage(self):
        provider = AnthropicProvider(api_key="sk-ant-test")
        usage = provider.extract_usage(MagicMock(spec=[]))
        assert usage == Usage(total=0, input=0, output=0, cached=0)

    @pytest.mark.asyncio
    async def test_complete_json_returns_provider_response(self):
        provider = AnthropicProvider(api_key="sk-ant-test")
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text='{"key": "value"}')]
        mock_response.usage = _make_anthropic_usage(
            input_tokens=10, output_tokens=5, cache_read=2, cache_creation=1
        )

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        with patch.object(provider, "_client", return_value=mock_client):
            result = await provider.complete_json("hello", model="claude-sonnet-4-6")

        assert result.content == '{"key": "value"}'
        assert result.usage == Usage(total=18, input=10, output=5, cached=2, cache_write=1)

    def test_is_reasoning_model(self):
        assert AnthropicProvider._is_reasoning_model("claude-sonnet-4-6") is False
        assert AnthropicProvider._is_reasoning_model("claude-4-opus") is True
        assert AnthropicProvider._is_reasoning_model("claude-3-7-sonnet") is True

    @pytest.mark.asyncio
    async def test_list_models_returns_static_list(self):
        provider = AnthropicProvider(api_key="sk-ant-test")
        models = await provider.list_models()
        assert len(models) == 3
        assert models[0]["id"] == "claude-sonnet-4-6-20251001"


# ============================================================================
# GeminiProvider
# ============================================================================


class TestGeminiProvider:
    def test_extract_usage_reads_metadata(self):
        provider = GeminiProvider(api_key="gemini-test")
        raw = MagicMock()
        raw.usage_metadata = _make_gemini_usage(prompt=100, completion=20)

        usage = provider.extract_usage(raw)
        assert usage == Usage(total=120, input=100, output=20, cached=0)

    def test_extract_usage_handles_no_metadata(self):
        provider = GeminiProvider(api_key="gemini-test")
        usage = provider.extract_usage(MagicMock(spec=[]))
        assert usage == Usage(total=0, input=0, output=0, cached=0)

    @pytest.mark.asyncio
    async def test_complete_json_returns_provider_response(self):
        provider = GeminiProvider(api_key="gemini-test")
        mock_response = MagicMock()
        mock_response.text = '{"key": "value"}'
        mock_response.usage_metadata = _make_gemini_usage(prompt=10, completion=5)

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        with patch.object(provider, "_client", return_value=mock_client):
            result = await provider.complete_json("hello", model="gemini-3.1-pro")

        assert result.content == '{"key": "value"}'
        assert result.usage == Usage(total=15, input=10, output=5, cached=0)

    @pytest.mark.asyncio
    async def test_list_models_returns_models(self):
        provider = GeminiProvider(api_key="gemini-test")
        mock_model = MagicMock()
        mock_model.name = "models/gemini-3.1-pro"
        mock_model.display_name = "Gemini 3.1 Pro"
        mock_list = MagicMock()
        mock_list.models = [mock_model]

        mock_client = MagicMock()
        mock_client.aio.models.list = AsyncMock(return_value=mock_list)
        with patch.object(provider, "_client", return_value=mock_client):
            models = await provider.list_models()

        assert models == [{"id": "models/gemini-3.1-pro", "name": "Gemini 3.1 Pro"}]


# ============================================================================
# OllamaProvider
# ============================================================================


class TestOllamaProvider:
    def test_extract_usage_reads_prompt_completion(self):
        provider = OllamaProvider(base_url="http://localhost:11434")
        raw = MagicMock()
        raw.usage = MagicMock()
        raw.usage.prompt_tokens = 100
        raw.usage.completion_tokens = 20
        raw.usage.total_tokens = 120

        usage = provider.extract_usage(raw)
        assert usage == Usage(total=120, input=100, output=20, cached=0)

    def test_extract_usage_handles_no_usage(self):
        provider = OllamaProvider(base_url="http://localhost:11434")
        usage = provider.extract_usage(MagicMock(spec=[]))
        assert usage == Usage(total=0, input=0, output=0, cached=0)

    @pytest.mark.asyncio
    async def test_complete_json_returns_provider_response(self):
        provider = OllamaProvider(base_url="http://localhost:11434")
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content='{"key": "value"}'))]
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15

        with patch.object(
            provider._client.chat.completions,
            "create",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            result = await provider.complete_json("hello", model="llama3.1")

        assert result.content == '{"key": "value"}'
        assert result.usage == Usage(total=15, input=10, output=5, cached=0)

    @pytest.mark.asyncio
    async def test_list_models_returns_models(self):
        provider = OllamaProvider(base_url="http://localhost:11434")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"models": [{"name": "llama3.1", "model": "llama3.1"}]}

        mock_http = MagicMock()
        mock_http.get = AsyncMock(return_value=mock_resp)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_http):
            models = await provider.list_models()

        assert models == [{"id": "llama3.1", "name": "llama3.1"}]
