"""Tests for LLM Service.

Issue #101: Multi-provider LLM client for Sleep Maintenance.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import AsyncOpenAI

from services.llm_service import LLMService, LLMServiceError


@pytest.fixture
def mock_db():
    """Mock AsyncSession."""
    db = AsyncMock()
    return db


@pytest.fixture
def llm_service(mock_db):
    """LLM service with mocked DB."""
    return LLMService(mock_db)


def _make_completion_response(content: str, total_tokens: int = 42):
    """Create a mock chat completion response."""
    usage = MagicMock()
    usage.total_tokens = total_tokens

    message = MagicMock()
    message.content = content

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


class TestCompleteJson:
    """Test complete_json method."""

    @pytest.mark.asyncio
    async def test_successful_json_completion(self, llm_service):
        """Test successful JSON response parsing."""
        mock_response = _make_completion_response(
            '{"result": "ok", "score": 0.95}', total_tokens=50
        )

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_get_client.return_value = mock_client

            result, tokens = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test prompt",
                system_prompt="You are a judge.",
            )

        assert result == {"result": "ok", "score": 0.95}
        assert tokens == 50

        # Verify system + user messages
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert len(call_kwargs["messages"]) == 2
        assert call_kwargs["messages"][0]["role"] == "system"
        assert call_kwargs["messages"][1]["role"] == "user"
        assert call_kwargs["response_format"] == {"type": "json_object"}

        # Regression guard (#421): OpenAI gpt-5/o-series reject `max_tokens` with
        # HTTP 400 and require `max_completion_tokens`. Pin the kwarg name here
        # so a future rename back to `max_tokens` fails loudly in tests instead
        # of silently in production.
        assert call_kwargs["max_completion_tokens"] == 1024
        assert "max_tokens" not in call_kwargs

    @pytest.mark.asyncio
    async def test_max_completion_tokens_mapped_from_max_tokens_arg(self, llm_service):
        """Regression guard (#421): caller-facing `max_tokens` must map to `max_completion_tokens` on the OpenAI call."""
        mock_response = _make_completion_response('{"ok": true}', total_tokens=20)

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_get_client.return_value = mock_client

            await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                max_tokens=512,
            )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["max_completion_tokens"] == 512
        assert "max_tokens" not in call_kwargs

    @pytest.mark.asyncio
    async def test_gpt5_omits_temperature_kwarg(self, llm_service):
        """Regression guard (#424): gpt-5 / o-series only accept temperature=1.

        The SDK call must NOT include the `temperature` kwarg for these models;
        the SDK falls back to the model's fixed default. Sending any custom
        value (even our default 0.1) returns HTTP 400.
        """
        mock_response = _make_completion_response('{"ok": true}')

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_get_client.return_value = mock_client

            await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                model="gpt-5-nano",
                temperature=0.1,
            )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert "temperature" not in call_kwargs
        assert call_kwargs["model"] == "gpt-5-nano"

    @pytest.mark.asyncio
    async def test_gpt4_includes_temperature_kwarg(self, llm_service):
        """Regression guard (#424): GPT-4 family must still receive the temperature kwarg."""
        mock_response = _make_completion_response('{"ok": true}')

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_get_client.return_value = mock_client

            await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                model="gpt-4o-mini",
                temperature=0.1,
            )

        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["temperature"] == 0.1

    def test_supports_custom_temperature_helper(self):
        """Unit test for the model-prefix detection helper (#424)."""
        # Reasoning models — must omit temperature
        assert LLMService._supports_custom_temperature("gpt-5-nano") is False
        assert LLMService._supports_custom_temperature("gpt-5") is False
        assert LLMService._supports_custom_temperature("o1-preview") is False
        assert LLMService._supports_custom_temperature("o1-mini") is False
        assert LLMService._supports_custom_temperature("o3-mini") is False
        assert LLMService._supports_custom_temperature("o4-mini") is False
        # Non-reasoning models — temperature OK
        assert LLMService._supports_custom_temperature("gpt-4o-mini") is True
        assert LLMService._supports_custom_temperature("gpt-4o") is True
        assert LLMService._supports_custom_temperature("gpt-3.5-turbo") is True
        assert LLMService._supports_custom_temperature("llama-3") is True

    @pytest.mark.asyncio
    async def test_no_system_prompt(self, llm_service):
        """Test completion without system prompt."""
        mock_response = _make_completion_response('{"ok": true}')

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_get_client.return_value = mock_client

            result, _ = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
            )

        assert result == {"ok": True}
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert len(call_kwargs["messages"]) == 1
        assert call_kwargs["messages"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_json_parse_failure_retries(self, llm_service):
        """Test retry on JSON parse failure with higher temperature.

        Pinned to a GPT-4 family model so the temperature kwarg is actually sent
        — GPT-5 / o-series omit it (see test_gpt5_omits_temperature_kwarg).
        """
        bad_response = _make_completion_response("not json {", total_tokens=30)
        good_response = _make_completion_response('{"retried": true}', total_tokens=35)

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = [
                bad_response,
                good_response,
            ]
            mock_get_client.return_value = mock_client

            result, tokens = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                model="gpt-4o-mini",
            )

        assert result == {"retried": True}
        assert tokens == 30 + 35  # Both attempts counted

        # Verify retry used higher temperature (GPT-4 path, temperature kwarg present)
        calls = mock_client.chat.completions.create.call_args_list
        assert calls[0][1]["temperature"] == 0.1  # First attempt
        assert calls[1][1]["temperature"] == 0.3  # Retry

    @pytest.mark.asyncio
    async def test_json_parse_failure_both_attempts(self, llm_service):
        """Test LLMServiceError when both attempts fail to parse JSON."""
        bad_response = _make_completion_response("not json", total_tokens=30)

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = bad_response
            mock_get_client.return_value = mock_client

            with pytest.raises(LLMServiceError, match="JSON parse failed after retry"):
                await llm_service.complete_json(
                    user_id="user-1",
                    prompt="Test",
                )

    @pytest.mark.asyncio
    async def test_api_error_raises_immediately(self, llm_service):
        """Test that API errors raise without retry."""
        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.side_effect = RuntimeError("API down")
            mock_get_client.return_value = mock_client

            with pytest.raises(LLMServiceError, match="LLM API call failed"):
                await llm_service.complete_json(
                    user_id="user-1",
                    prompt="Test",
                )

        # Only one call — no retry on API error
        assert mock_client.chat.completions.create.call_count == 1

    @pytest.mark.asyncio
    async def test_custom_model_and_provider(self, llm_service):
        """Test passing custom model and provider."""
        mock_response = _make_completion_response('{"ok": true}')

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = mock_response
            mock_get_client.return_value = mock_client

            await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                model="gpt-4o",
                provider="openai",
            )

        mock_get_client.assert_called_once_with("user-1", "openai", None, None)
        call_kwargs = mock_client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_null_usage_returns_zero_tokens(self, llm_service):
        """Test handling of null usage in response."""
        response = MagicMock()
        message = MagicMock()
        message.content = '{"ok": true}'
        choice = MagicMock()
        choice.message = message
        response.choices = [choice]
        response.usage = None

        with patch.object(llm_service, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.chat.completions.create.return_value = response
            mock_get_client.return_value = mock_client

            _, tokens = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
            )

        assert tokens == 0


class TestGetClient:
    """Test _get_client provider routing."""

    @pytest.mark.asyncio
    async def test_openai_provider(self, llm_service):
        """Test OpenAI client creation."""
        with patch.object(
            llm_service, "_get_user_api_key", new_callable=AsyncMock, return_value="sk-test-key"
        ):
            client = await llm_service._get_client("user-1", "openai")

        assert isinstance(client, AsyncOpenAI)

    @pytest.mark.asyncio
    async def test_ollama_provider(self, llm_service):
        """Test Ollama client creation with connectivity check."""
        with (
            patch("config.settings.get_settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_settings.return_value.ollama_base_url = "http://localhost:11434"
            mock_http = AsyncMock()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_http.get.return_value = mock_resp
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_httpx_cls.return_value = mock_http

            client = await llm_service._get_client("user-1", "ollama")

        assert isinstance(client, AsyncOpenAI)
        assert llm_service._ollama_verified is True
