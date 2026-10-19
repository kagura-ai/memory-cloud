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
    async def test_release_db_before_call_commits_between_lookup_and_call(
        self, llm_service, mock_db
    ):
        """#2070: the credential lookup's transaction ends before the round
        trip, so the pooled connection is not held while the provider answers."""
        order: list[str] = []

        async def _resolve(*_a, **_k):
            order.append("lookup")
            return mock_provider

        async def _call(**_k):
            order.append("call")
            return _make_provider_response('{"ok": true}')

        mock_provider = AsyncMock()
        mock_provider.complete_json.side_effect = _call
        mock_db.commit.side_effect = lambda: order.append("commit")

        with patch.object(llm_service, "_get_provider", side_effect=_resolve):
            await llm_service.complete_json(
                user_id="user-1", prompt="p", release_db_before_call=True
            )

        assert order == ["lookup", "commit", "call"]
        mock_db.rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_session_is_left_alone_by_default(self, llm_service, mock_db):
        mock_provider = AsyncMock()
        mock_provider.complete_json.return_value = _make_provider_response('{"ok": true}')

        with patch.object(
            llm_service, "_get_provider", new_callable=AsyncMock, return_value=mock_provider
        ):
            await llm_service.complete_json(user_id="user-1", prompt="p")

        mock_db.commit.assert_not_called()

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
            llm_service,
            "_resolve_api_key",
            new_callable=AsyncMock,
            return_value=("sk-test", "env"),
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

    @pytest.mark.asyncio
    async def test_get_provider_forwards_strict_flag_to_key_resolution(
        self, llm_service, monkeypatch
    ):
        """#1242 chain link 2: ``_get_provider`` must forward the flag to
        the REAL ``_get_user_api_key`` — the complete_json-level test
        mocks _get_provider away, so without this test a refactor that
        drops the kwarg re-enables the platform env fallback silently.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-1242")
        with pytest.raises(ConfigurationError):
            await llm_service._get_provider("u1", "openai", disallow_env_fallback=True)
        # Default mode still resolves the env key end-to-end.
        provider = await llm_service._get_provider("u1", "openai")
        assert provider is not None


def _db_returning_row(mock_db, row):
    """Make every ``await db.execute(...)`` yield ``row`` from scalar_one_or_none."""
    from unittest.mock import MagicMock

    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=row)
    mock_db.execute = AsyncMock(return_value=result)
    return result


class TestPlatformOnlyKeyResolution:
    """#1569 managed lane: ``platform_only`` never touches ``external_api_keys``.

    The deployment pays for the managed lane, so a workspace's stored key
    must not be billed even when one exists; the platform env credential is
    the only source.
    """

    @pytest.mark.asyncio
    async def test_platform_only_skips_stored_key_and_uses_env(
        self, llm_service, mock_db, monkeypatch
    ):
        from uuid import uuid4

        monkeypatch.setenv("OPENAI_API_KEY", "sk-platform-1569")
        _db_returning_row(mock_db, object())  # a stored key IS present
        key = await llm_service._get_user_api_key(
            "u1", "openai", workspace_id=str(uuid4()), platform_only=True
        )
        assert key == "sk-platform-1569"
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_platform_only_without_env_raises(self, llm_service, mock_db, monkeypatch):
        from uuid import uuid4

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _db_returning_row(mock_db, object())
        with pytest.raises(ConfigurationError):
            await llm_service._get_user_api_key(
                "u1", "openai", workspace_id=str(uuid4()), platform_only=True
            )
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_flags_are_mutually_exclusive(self, llm_service):
        with pytest.raises(ValueError, match="mutually exclusive"):
            await llm_service._get_user_api_key(
                "u1", "openai", disallow_env_fallback=True, platform_only=True
            )
        with pytest.raises(ValueError, match="mutually exclusive"):
            await llm_service._get_provider(
                "u1", "openai", disallow_env_fallback=True, platform_only=True
            )

    @pytest.mark.asyncio
    async def test_complete_json_threads_platform_only(self, llm_service):
        provider = AsyncMock()
        provider.complete_json = AsyncMock(return_value=_make_provider_response('{"ok": true}'))
        llm_service._get_provider = AsyncMock(return_value=provider)

        await llm_service.complete_json("u1", "prompt", workspace_id="ws", platform_only=True)
        kwargs = llm_service._get_provider.call_args.kwargs
        assert kwargs.get("platform_only") is True
        assert kwargs.get("disallow_env_fallback") is False

    @pytest.mark.asyncio
    async def test_platform_only_self_hosted_uses_settings_not_stored_row(
        self, llm_service, mock_db
    ):
        """self_hosted: the base URL comes from settings, the workspace's
        ``ExternalAPIKey`` row (which may point elsewhere) is never read."""
        from uuid import uuid4

        _db_returning_row(mock_db, object())
        with patch("config.settings.get_settings") as mock_settings:
            mock_settings.return_value.self_hosted_base_url = "http://platform-vllm:8000"
            mock_settings.return_value.self_hosted_api_key = ""
            provider = await llm_service._get_provider(
                "u1", "self_hosted", workspace_id=str(uuid4()), platform_only=True
            )
        from services.llm_providers import SelfHostedProvider

        assert isinstance(provider, SelfHostedProvider)
        assert llm_service._last_self_hosted_base_url == "http://platform-vllm:8000"
        mock_db.execute.assert_not_called()


class TestResolveStoredByokKeysDisabled:
    """#1569 D6: ``RESOLVE_STORED_BYOK_KEYS=false`` ignores stored keys."""

    @pytest.fixture
    def stored_keys_disabled(self, monkeypatch):
        monkeypatch.setenv("RESOLVE_STORED_BYOK_KEYS", "false")
        monkeypatch.setenv("ENABLE_BYOK", "false")
        monkeypatch.setattr("config.settings._settings", None)
        monkeypatch.setattr("services.byok_resolution._disabled_logged", False)

    @pytest.mark.asyncio
    async def test_stored_key_ignored_env_used_logged_once(
        self, llm_service, mock_db, monkeypatch, stored_keys_disabled
    ):
        from uuid import uuid4

        from structlog.testing import capture_logs

        monkeypatch.setenv("OPENAI_API_KEY", "sk-platform-only")
        _db_returning_row(mock_db, object())  # stored key present but must be ignored
        with capture_logs() as logs:
            first = await llm_service._get_user_api_key("u1", "openai", workspace_id=str(uuid4()))
            second = await llm_service._get_user_api_key("u1", "openai", workspace_id=str(uuid4()))
        assert (first, second) == ("sk-platform-only", "sk-platform-only")
        mock_db.execute.assert_not_called()
        events = [e["event"] for e in logs if e["event"] == "byok_key_resolution_disabled"]
        assert events == ["byok_key_resolution_disabled"], "logged once per process"

    @pytest.mark.asyncio
    async def test_self_hosted_base_url_from_settings_only(
        self, llm_service, mock_db, stored_keys_disabled
    ):
        from uuid import uuid4

        _db_returning_row(mock_db, object())
        provider = await llm_service._get_provider("u1", "self_hosted", workspace_id=str(uuid4()))
        assert provider is not None
        mock_db.execute.assert_not_called()


class TestResolvePaidBy:
    """#2019: ``paid_by`` for a ledger row follows the key the call used.

    Mirrors ``EmbeddingService.resolve_paid_by``: a stored (BYOK) key →
    ``"byok"``; the platform env credential (incl. the #1569 managed lane,
    which never reads stored keys) → ``"platform"``.
    """

    @staticmethod
    def _row_result(entry):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=entry)
        return result

    @pytest.mark.asyncio
    async def test_no_call_yet_is_platform(self, llm_service):
        assert llm_service.resolve_paid_by() == "platform"

    @pytest.mark.asyncio
    async def test_env_key_is_platform(self, llm_service, mock_db, monkeypatch):
        from uuid import uuid4

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        mock_db.execute = AsyncMock(return_value=self._row_result(None))
        await llm_service._get_provider("u1", "openai", workspace_id=str(uuid4()))
        assert llm_service.resolve_paid_by() == "platform"

    @pytest.mark.asyncio
    async def test_stored_key_is_byok(self, llm_service, mock_db, monkeypatch):
        from unittest.mock import MagicMock
        from uuid import uuid4

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        entry = MagicMock(encrypted_value="blob")
        mock_db.execute = AsyncMock(return_value=self._row_result(entry))
        enc = MagicMock()
        enc.decrypt = MagicMock(return_value="sk-db")
        with patch("services.llm_service.get_encryptor", return_value=enc):
            await llm_service._get_provider("u1", "openai", workspace_id=str(uuid4()))
        assert llm_service.resolve_paid_by() == "byok"

    @pytest.mark.asyncio
    async def test_managed_lane_is_platform_even_with_a_stored_key(
        self, llm_service, mock_db, monkeypatch
    ):
        from unittest.mock import MagicMock
        from uuid import uuid4

        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        mock_db.execute = AsyncMock(return_value=self._row_result(MagicMock()))
        await llm_service._get_provider(
            "u1", "openai", workspace_id=str(uuid4()), platform_only=True
        )
        mock_db.execute.assert_not_called()
        assert llm_service.resolve_paid_by() == "platform"

    @pytest.mark.asyncio
    async def test_self_hosted_stored_base_url_is_byok(self, llm_service, mock_db, monkeypatch):
        from unittest.mock import MagicMock
        from uuid import uuid4

        monkeypatch.delenv("SELF_HOSTED_BASE_URL", raising=False)
        mock_db.execute = AsyncMock(return_value=self._row_result(MagicMock()))
        enc = MagicMock()
        enc.decrypt = MagicMock(return_value="http://ws-box:8000")
        with patch("services.llm_service.get_encryptor", return_value=enc):
            await llm_service._get_provider("u1", "self_hosted", workspace_id=str(uuid4()))
        assert llm_service.resolve_paid_by() == "byok"

    @pytest.mark.asyncio
    async def test_self_hosted_env_base_url_is_platform(self, llm_service, mock_db, monkeypatch):
        from uuid import uuid4

        monkeypatch.setenv("SELF_HOSTED_BASE_URL", "http://platform:8000")
        mock_db.execute = AsyncMock(return_value=self._row_result(None))
        await llm_service._get_provider("u1", "self_hosted", workspace_id=str(uuid4()))
        assert llm_service.resolve_paid_by() == "platform"

    @pytest.mark.asyncio
    async def test_blank_stored_self_hosted_url_falls_back_and_is_platform(
        self, llm_service, mock_db, monkeypatch
    ):
        """A stored row whose base URL is blank is not used: the platform URL
        serves the call, so it must not be labelled byok."""
        from unittest.mock import MagicMock
        from uuid import uuid4

        monkeypatch.delenv("SELF_HOSTED_BASE_URL", raising=False)
        mock_db.execute = AsyncMock(return_value=self._row_result(MagicMock()))
        enc = MagicMock()
        enc.decrypt = MagicMock(return_value="")
        with (
            patch("services.llm_service.get_encryptor", return_value=enc),
            patch("config.settings.get_settings") as settings,
        ):
            settings.return_value.self_hosted_base_url = "http://platform:8000"
            settings.return_value.self_hosted_api_key = ""
            await llm_service._get_provider("u1", "self_hosted", workspace_id=str(uuid4()))
        assert llm_service._last_self_hosted_base_url == "http://platform:8000"
        assert llm_service.resolve_paid_by() == "platform"

    @pytest.mark.asyncio
    async def test_failed_resolution_clears_an_earlier_source(
        self, llm_service, mock_db, monkeypatch
    ):
        from unittest.mock import MagicMock
        from uuid import uuid4

        mock_db.execute = AsyncMock(return_value=self._row_result(MagicMock()))
        enc = MagicMock()
        enc.decrypt = MagicMock(return_value="sk-db")
        with patch("services.llm_service.get_encryptor", return_value=enc):
            await llm_service._get_provider("u1", "openai", workspace_id=str(uuid4()))
        assert llm_service.resolve_paid_by() == "byok"
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        mock_db.execute = AsyncMock(return_value=self._row_result(None))
        with pytest.raises(ConfigurationError):
            await llm_service._get_provider("u1", "anthropic", workspace_id=str(uuid4()))
        assert llm_service.resolve_paid_by() == "platform"

    @pytest.mark.asyncio
    async def test_self_hosted_settings_base_url_is_platform(self, llm_service, monkeypatch):
        monkeypatch.delenv("SELF_HOSTED_BASE_URL", raising=False)
        llm_service._resolve_api_key = AsyncMock(side_effect=ConfigurationError("none"))
        with patch("config.settings.get_settings") as settings:
            settings.return_value.self_hosted_base_url = "http://platform:8000"
            settings.return_value.self_hosted_api_key = ""
            await llm_service._get_provider("u1", "self_hosted", workspace_id="ws")
        assert llm_service.resolve_paid_by() == "platform"


class TestFailedCallUsage:
    """#2019 review: a failed call that still spent tokens carries them on
    ``LLMServiceError.usage`` so a ledger can record the spend."""

    @pytest.mark.asyncio
    async def test_double_parse_failure_carries_both_attempts(self, llm_service):
        provider = AsyncMock()
        provider.complete_json.side_effect = [
            _make_provider_response("not json", total=30, input_tokens=20, output_tokens=10),
            _make_provider_response("still not", total=35, input_tokens=21, output_tokens=14),
        ]
        with patch.object(llm_service, "_get_provider", new=AsyncMock(return_value=provider)):
            with pytest.raises(LLMServiceError) as excinfo:
                await llm_service.complete_json("u1", "p", provider="openai", model="m")
        usage = excinfo.value.usage
        assert usage is not None
        assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (41, 24, 65)
        assert (usage.provider, usage.model) == ("openai", "m")

    @pytest.mark.asyncio
    async def test_parse_failure_then_api_error_carries_the_first_attempt(self, llm_service):
        provider = AsyncMock()
        provider.complete_json.side_effect = [
            _make_provider_response("not json", total=30, input_tokens=20, output_tokens=10),
            RuntimeError("upstream 500"),
        ]
        with patch.object(llm_service, "_get_provider", new=AsyncMock(return_value=provider)):
            with pytest.raises(LLMServiceError) as excinfo:
                await llm_service.complete_json("u1", "p", provider="openai", model="m")
        usage = excinfo.value.usage
        assert usage is not None and (usage.input_tokens, usage.output_tokens) == (20, 10)

    @pytest.mark.asyncio
    async def test_first_attempt_api_error_carries_no_usage(self, llm_service):
        provider = AsyncMock()
        provider.complete_json.side_effect = RuntimeError("API down")
        with patch.object(llm_service, "_get_provider", new=AsyncMock(return_value=provider)):
            with pytest.raises(LLMServiceError) as excinfo:
                await llm_service.complete_json("u1", "p")
        assert excinfo.value.usage is None


class TestMaxAttempts:
    """#2144: ``max_attempts=1`` makes one provider call; the default keeps two."""

    @pytest.mark.asyncio
    async def test_single_attempt_raises_on_parse_failure_with_its_usage(self, llm_service):
        provider = AsyncMock()
        provider.complete_json.side_effect = [
            _make_provider_response("not json", total=30, input_tokens=20, output_tokens=10),
            _make_provider_response('{"never": "asked"}'),
        ]
        with patch.object(llm_service, "_get_provider", new=AsyncMock(return_value=provider)):
            with pytest.raises(LLMServiceError, match="JSON parse failed") as excinfo:
                await llm_service.complete_json(
                    "u1", "p", provider="openai", model="m", max_attempts=1
                )
        assert provider.complete_json.await_count == 1
        usage = excinfo.value.usage
        assert usage is not None
        assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (20, 10, 30)

    @pytest.mark.asyncio
    async def test_single_attempt_success_is_one_call(self, llm_service):
        provider = AsyncMock()
        provider.complete_json.return_value = _make_provider_response('{"ok": 1}')
        with patch.object(llm_service, "_get_provider", new=AsyncMock(return_value=provider)):
            resp = await llm_service.complete_json("u1", "p", max_attempts=1)
        assert resp.parsed == {"ok": 1}
        assert provider.complete_json.await_count == 1

    @pytest.mark.asyncio
    async def test_default_still_retries_once(self, llm_service):
        provider = AsyncMock()
        provider.complete_json.side_effect = [
            _make_provider_response("not json"),
            _make_provider_response('{"ok": 2}'),
        ]
        with patch.object(llm_service, "_get_provider", new=AsyncMock(return_value=provider)):
            resp = await llm_service.complete_json("u1", "p")
        assert resp.parsed == {"ok": 2}
        assert provider.complete_json.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, 3, -1])
    async def test_out_of_range_is_refused_before_any_call(self, llm_service, bad):
        with patch.object(llm_service, "_get_provider", new=AsyncMock()) as get_provider:
            with pytest.raises(ValueError, match="max_attempts"):
                await llm_service.complete_json("u1", "p", max_attempts=bad)
        get_provider.assert_not_called()
