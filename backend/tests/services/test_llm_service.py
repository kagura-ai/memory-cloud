"""Tests for LLM Service.

Issue #101: Multi-provider LLM client for Sleep Maintenance.
Issue #546: Adapter pattern refactor.
"""

from unittest.mock import AsyncMock, patch

import pytest

from services.llm_providers.base import ProviderResponse, Usage
from services.llm_service import LLMService, LLMServiceError
from utils.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def clear_model_cache():
    """Clear the module-level model cache between tests."""
    import services.llm_service as _svc

    _svc._MODEL_CACHE.clear()
    yield
    _svc._MODEL_CACHE.clear()


@pytest.fixture
def mock_db():
    """Mock AsyncSession."""
    db = AsyncMock()
    return db


@pytest.fixture
def llm_service(mock_db):
    """LLM service with mocked DB."""
    return LLMService(mock_db)


def _make_provider_response(
    content: str,
    *,
    total: int = 42,
    input_tokens: int = 20,
    output_tokens: int = 22,
    cached: int = 0,
    cache_write: int = 0,
) -> ProviderResponse:
    """Create a mock provider response."""
    return ProviderResponse(
        content=content,
        usage=Usage(
            total=total,
            input=input_tokens,
            output=output_tokens,
            cached=cached,
            cache_write=cache_write,
        ),
    )


class TestCompleteJson:
    """Test complete_json method."""

    @pytest.mark.asyncio
    async def test_successful_json_completion(self, llm_service):
        """Test successful JSON response parsing."""
        mock_provider = AsyncMock()
        mock_provider.complete_json.return_value = _make_provider_response(
            '{"result": "ok", "score": 0.95}', total=50
        )

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            resp = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test prompt",
                system_prompt="You are a judge.",
            )

        assert resp.parsed == {"result": "ok", "score": 0.95}
        assert resp.total_tokens == 50
        assert resp.input_tokens == 20
        assert resp.output_tokens == 22
        assert resp.cached_input_tokens == 0
        assert resp.cache_write_tokens == 0
        assert resp.provider == "openai"

    @pytest.mark.asyncio
    async def test_custom_model_and_provider(self, llm_service):
        """Test passing custom model and provider."""
        mock_provider = AsyncMock()
        mock_provider.complete_json.return_value = _make_provider_response('{"ok": true}')

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            resp = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                model="gpt-4o",
                provider="openai",
            )

        mock_provider.complete_json.assert_called_once()
        call_kwargs = mock_provider.complete_json.call_args[1]
        assert call_kwargs["model"] == "gpt-4o"
        assert resp.provider == "openai"

    @pytest.mark.asyncio
    async def test_json_parse_failure_retries(self, llm_service):
        """Test retry on JSON parse failure with higher temperature."""
        bad_response = _make_provider_response("not json {", total=30)
        good_response = _make_provider_response('{"retried": true}', total=35)

        mock_provider = AsyncMock()
        mock_provider.complete_json.side_effect = [bad_response, good_response]

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            resp = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                model="gpt-4o-mini",
            )

        assert resp.parsed == {"retried": True}
        assert resp.total_tokens == 30 + 35

        calls = mock_provider.complete_json.call_args_list
        assert calls[0][1]["temperature"] == 0.1
        assert calls[1][1]["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_json_parse_failure_both_attempts(self, llm_service):
        """Test LLMServiceError when both attempts fail to parse JSON."""
        bad_response = _make_provider_response("not json", total=30)

        mock_provider = AsyncMock()
        mock_provider.complete_json.return_value = bad_response

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            with pytest.raises(LLMServiceError, match="JSON parse failed after retry"):
                await llm_service.complete_json(
                    user_id="user-1",
                    prompt="Test",
                )

    @pytest.mark.asyncio
    async def test_api_error_raises_immediately(self, llm_service):
        """Test that API errors raise without retry."""
        mock_provider = AsyncMock()
        mock_provider.complete_json.side_effect = RuntimeError("API down")

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            with pytest.raises(LLMServiceError, match="LLM API call failed"):
                await llm_service.complete_json(
                    user_id="user-1",
                    prompt="Test",
                )

        assert mock_provider.complete_json.call_count == 1

    @pytest.mark.asyncio
    async def test_null_usage_returns_zero_tokens(self, llm_service):
        """Test handling of zero-usage provider response."""
        mock_provider = AsyncMock()
        mock_provider.complete_json.return_value = ProviderResponse(
            content='{"ok": true}',
            usage=Usage(total=0, input=0, output=0, cached=0),
        )

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            resp = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
            )

        assert resp.total_tokens == 0

    @pytest.mark.asyncio
    async def test_cache_write_tokens_in_response(self, llm_service):
        """Test cache_write_tokens is propagated into LLMResponse (#546)."""
        mock_provider = AsyncMock()
        mock_provider.complete_json.return_value = _make_provider_response(
            '{"ok": true}',
            total=100,
            input_tokens=50,
            output_tokens=30,
            cached=10,
            cache_write=10,
        )

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            resp = await llm_service.complete_json(
                user_id="user-1",
                prompt="Test",
                provider="anthropic",
            )

        assert resp.total_tokens == 100
        assert resp.input_tokens == 50
        assert resp.output_tokens == 30
        assert resp.cached_input_tokens == 10
        assert resp.cache_write_tokens == 10
        assert resp.provider == "anthropic"


class TestListModels:
    """Test list_models method."""

    @pytest.mark.asyncio
    async def test_list_models_returns_cached_results(self, llm_service):
        """Test that list_models caches results."""
        mock_provider = AsyncMock()
        mock_provider.list_models.return_value = [
            {"id": "gpt-4o", "name": "GPT-4o"},
        ]

        with (
            patch.object(
                llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
            ),
            patch.object(
                llm_service,
                "_get_user_api_key",
                new_callable=AsyncMock,
                return_value="sk-test",
            ),
        ):
            # First call fetches from provider
            result1 = await llm_service.list_models("user-1", "openai")
            assert result1 == [{"id": "gpt-4o", "name": "GPT-4o"}]
            assert mock_provider.list_models.call_count == 1

            # Second call uses cache
            result2 = await llm_service.list_models("user-1", "openai")
            assert result2 == [{"id": "gpt-4o", "name": "GPT-4o"}]
            # Provider should NOT be called again
            assert mock_provider.list_models.call_count == 1

    @pytest.mark.asyncio
    async def test_list_models_fallback_on_failure(self, llm_service):
        """Test fallback to stale cache when provider fails."""
        mock_provider = AsyncMock()
        mock_provider.list_models.side_effect = RuntimeError("network error")

        with (
            patch.object(
                llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
            ),
            patch.object(
                llm_service,
                "_get_user_api_key",
                new_callable=AsyncMock,
                return_value="sk-test",
            ),
        ):
            result = await llm_service.list_models("user-1", "openai")
            assert result == []


class TestGetProvider:
    """Test _get_provider routing."""

    @pytest.mark.asyncio
    async def test_openai_provider(self, llm_service):
        """Test OpenAI provider instantiation."""
        with patch.object(
            llm_service, "_get_user_api_key", new_callable=AsyncMock, return_value="sk-test"
        ):
            provider = await llm_service._get_provider("user-1", "openai")

        from services.llm_providers import OpenAIProvider

        assert isinstance(provider, OpenAIProvider)

    @pytest.mark.asyncio
    async def test_self_hosted_provider_no_api_key(self, llm_service):
        """Test self-hosted provider does not require API key."""
        with patch("config.settings.get_settings") as mock_settings:
            mock_settings.return_value.self_hosted_base_url = "http://localhost:11434"
            mock_settings.return_value.self_hosted_api_key = ""
            provider = await llm_service._get_provider("user-1", "self_hosted")

        from services.llm_providers import SelfHostedProvider

        assert isinstance(provider, SelfHostedProvider)

    @pytest.mark.asyncio
    async def test_unknown_provider_raises(self, llm_service):
        """Test unknown provider raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="Unknown LLM provider"):
            await llm_service._get_provider("user-1", "unknown")


class TestStrictByokKeyResolution:
    """#1242: ``disallow_env_fallback`` — strict BYOK for paid features.

    The analysis labeling path requires an explicit BYOK row; the
    ``OPENAI_API_KEY`` env var is the platform embedding credential on
    managed SaaS, so falling back to it mid-run silently shifts BYOK
    costs onto the platform. Mirrors the embedding-path mechanism
    (``EmbeddingService`` #708/#1030).
    """

    @pytest.mark.asyncio
    async def test_env_fallback_used_by_default(self, llm_service, monkeypatch):
        """Self-host/dev contract unchanged: default resolution still
        honors the env var when no BYOK row exists."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-1242")
        key = await llm_service._get_user_api_key("u1", "openai")
        assert key == "sk-env-test-1242"

    @pytest.mark.asyncio
    async def test_disallow_env_fallback_raises_despite_env(self, llm_service, monkeypatch):
        """Strict mode: no BYOK row → ConfigurationError even when the
        env var is set — and the key material never leaks into the
        error message."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-1242")
        with pytest.raises(ConfigurationError) as excinfo:
            await llm_service._get_user_api_key("u1", "openai", disallow_env_fallback=True)
        assert "sk-env-test-1242" not in str(excinfo.value)
        assert "BYOK" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_disallow_env_fallback_with_workspace_but_no_row(
        self, llm_service, mock_db, monkeypatch
    ):
        """Strict mode with a workspace that has no enabled key: the
        DB lookup misses and the env var must still be skipped."""
        from unittest.mock import MagicMock
        from uuid import uuid4

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-1242")
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=result)
        with pytest.raises(ConfigurationError):
            await llm_service._get_user_api_key(
                "u1", "openai", workspace_id=str(uuid4()), disallow_env_fallback=True
            )

    @pytest.mark.asyncio
    async def test_disallow_env_fallback_still_resolves_db_key(
        self, llm_service, mock_db, monkeypatch
    ):
        """Strict mode disables ONLY the env fallback — an enabled BYOK
        row resolves exactly as before."""
        from unittest.mock import MagicMock
        from uuid import uuid4

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-1242")
        entry = MagicMock(encrypted_value="encrypted-blob")
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=entry)
        mock_db.execute = AsyncMock(return_value=result)

        fake_encryptor = MagicMock()
        fake_encryptor.decrypt = MagicMock(return_value="sk-db-key")
        with patch("services.llm_service.get_encryptor", return_value=fake_encryptor):
            key = await llm_service._get_user_api_key(
                "u1", "openai", workspace_id=str(uuid4()), disallow_env_fallback=True
            )
        assert key == "sk-db-key"

    @pytest.mark.asyncio
    async def test_complete_json_threads_flag_to_provider_resolution(self, llm_service):
        """``complete_json(disallow_env_fallback=True)`` must reach
        ``_get_provider`` — the flag is useless if it stops at the
        public surface."""
        provider = AsyncMock()
        provider.complete_json = AsyncMock(return_value=_make_provider_response('{"ok": true}'))
        llm_service._get_provider = AsyncMock(return_value=provider)

        await llm_service.complete_json(
            "u1",
            "prompt",
            workspace_id="ws",
            disallow_env_fallback=True,
        )
        assert llm_service._get_provider.call_args.kwargs.get("disallow_env_fallback") is True
