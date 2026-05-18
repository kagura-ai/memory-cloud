"""Tests for EmbeddingService."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.embedding_service import EmbeddingService
from utils.exceptions import ConfigurationError, OpenAIError


def _make_mock_db() -> AsyncMock:
    """Return an AsyncMock DB session whose execute() returns no API-key row."""
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    db.execute.return_value = execute_result
    return db


class TestEmbeddingService:
    """Test EmbeddingService for OpenAI embedding generation."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return _make_mock_db()

    @pytest.fixture
    def service(self, mock_db):
        """Create EmbeddingService."""
        return EmbeddingService(mock_db)

    # ------------------------------------------------------------------
    # Patch Redis cache helpers for every test so embed() never touches
    # a real Redis connection.
    # ------------------------------------------------------------------
    @pytest.fixture(autouse=True)
    def patch_cache(self):
        with (
            patch(
                "services.embedding_service.get_cache",
                new_callable=AsyncMock,
                return_value=None,  # cache miss by default
            ) as _get,
            patch(
                "services.embedding_service.set_cache",
                new_callable=AsyncMock,
            ) as _set,
        ):
            yield _get, _set

    def test_init(self, mock_db):
        """Test EmbeddingService initialization."""
        service = EmbeddingService(mock_db)

        assert service.db == mock_db
        assert service.model == "text-embedding-3-small"
        assert service.dimensions == 512

    @pytest.mark.asyncio
    async def test_get_user_api_key_from_env(self, service):
        """Test getting API key from environment variable."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-api-key"}):
            api_key = await service._get_user_api_key("test_user")

            assert api_key == "test-api-key"

    @pytest.mark.asyncio
    async def test_get_user_api_key_not_configured(self, service):
        """Test error when API key not configured."""
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ConfigurationError) as exc_info:
                await service._get_user_api_key("test_user")

            assert "not configured" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_embed_success(self, service):
        """Test successful embedding generation."""
        # Mock OpenAI response
        mock_embedding = [0.1, 0.2, 0.3] * 171  # 512 dimensions (approx)
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=mock_embedding[:512])]

        # Mock AsyncOpenAI client
        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                result = await service.embed("test text", "test_user")

                # Check result
                assert len(result) == 512
                assert all(isinstance(x, float) for x in result)

                # Check OpenAI was called correctly
                mock_client.embeddings.create.assert_called_once()
                call_kwargs = mock_client.embeddings.create.call_args.kwargs
                assert call_kwargs["input"] == "test text"
                assert call_kwargs["model"] == "text-embedding-3-small"
                assert call_kwargs["dimensions"] == 512

    @pytest.mark.asyncio
    async def test_embed_api_error(self, service):
        """Test handling of OpenAI API errors."""
        # Mock OpenAI client that raises error
        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(side_effect=Exception("API Error"))

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                with pytest.raises(OpenAIError) as exc_info:
                    await service.embed("test text", "test_user")

                assert "Embedding generation failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_embed_empty_text(self, service):
        """Test embedding with empty text."""
        # Mock OpenAI response for empty text
        mock_embedding = [0.0] * 512
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=mock_embedding)]

        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                result = await service.embed("", "test_user")

                assert len(result) == 512

    @pytest.mark.asyncio
    async def test_embed_batch_processing(self, service):
        """Test batch embedding processing."""
        texts = ["text1", "text2", "text3"]

        # Mock OpenAI responses
        mock_embeddings = [[0.1] * 512, [0.2] * 512, [0.3] * 512]
        mock_responses = [MagicMock(data=[MagicMock(embedding=emb)]) for emb in mock_embeddings]

        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(side_effect=mock_responses)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                results = [await service.embed(text, "test_user") for text in texts]

                assert len(results) == 3
                assert all(len(r) == 512 for r in results)

                # Check all calls were made
                assert mock_client.embeddings.create.call_count == 3

    @pytest.mark.asyncio
    async def test_embed_unicode_text(self, service):
        """Test embedding with unicode/Japanese text."""
        japanese_text = "これはテストです"

        mock_embedding = [0.5] * 512
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=mock_embedding)]

        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                result = await service.embed(japanese_text, "test_user")

                assert len(result) == 512

                # Check Japanese text was passed correctly
                call_kwargs = mock_client.embeddings.create.call_args.kwargs
                assert call_kwargs["input"] == japanese_text

    @pytest.mark.asyncio
    async def test_embed_long_text(self, service):
        """Test embedding with long text."""
        # Create long text (> 8000 tokens)
        long_text = "test " * 10000

        mock_embedding = [0.7] * 512
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=mock_embedding)]

        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                # Should not raise error (OpenAI handles truncation)
                result = await service.embed(long_text, "test_user")

                assert len(result) == 512

    @pytest.mark.asyncio
    async def test_embed_retry_on_rate_limit(self, service):
        """Test retry logic on rate limit errors."""
        # First call fails with rate limit, second succeeds
        mock_embedding = [0.8] * 512
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=mock_embedding)]

        rate_limit_error = Exception("Rate limit exceeded")
        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(side_effect=[rate_limit_error, mock_response])

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                # Should raise on first error (no retry logic in current implementation)
                with pytest.raises(OpenAIError):
                    await service.embed("test text", "test_user")

    @pytest.mark.asyncio
    async def test_embed_dimension_validation(self, service):
        """Test that embedding dimensions are returned as-is (no server-side validation)."""
        # Mock response with fewer dimensions than configured
        mock_embedding = [0.1] * 100  # Fewer than configured 512 dimensions
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=mock_embedding)]

        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                # Production code passes the vector through without dimension validation;
                # the caller receives whatever the API returns.
                result = await service.embed("test text", "test_user")

                assert len(result) == 100

    @pytest.mark.asyncio
    async def test_embed_different_users(self, service):
        """Test embedding for different users."""
        users = ["user1", "user2", "user3"]

        mock_embedding = [0.9] * 512
        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=mock_embedding)]

        mock_client = MagicMock()
        mock_client.embeddings = MagicMock()
        mock_client.embeddings.create = AsyncMock(return_value=mock_response)

        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with patch("services.embedding_service.AsyncOpenAI", return_value=mock_client):
                for user_id in users:
                    result = await service.embed("test", user_id)
                    assert len(result) == 512

                # All users should use the same API key (from env)
                assert mock_client.embeddings.create.call_count == len(users)


class TestContextAwareBYOKThreading:
    """#708 loop 4: context_id MUST thread through the BYOK probe chain.

    ``_prepare_spend_cap_gate`` and ``resolve_paid_by`` both call
    ``has_byok_key`` to decide whether the embedding cost is BYOK-billed.
    Without ``context_id``, a workspace whose only BYOK row is scoped to
    a DIFFERENT context would be falsely treated as having BYOK — the
    cap would be applied to env-fallback calls and ``paid_by`` would log
    "byok" for actually-platform-billed calls. These fences keep the
    accounting check aligned with ``_get_user_api_key``'s key selection.
    """

    @pytest.fixture
    def service(self):
        """EmbeddingService with mock DB."""
        return EmbeddingService(_make_mock_db())

    @pytest.mark.asyncio
    async def test_prepare_spend_cap_gate_passes_context_id_to_has_byok_key(self, service):
        """``_prepare_spend_cap_gate`` MUST forward ``context_id`` to ``has_byok_key``."""
        service.has_byok_key = AsyncMock(
            return_value=False
        )  # returns early; we only care about the call

        ws_id = "00000000-0000-0000-0000-000000000001"
        ctx_id = "00000000-0000-0000-0000-000000000002"

        await service._prepare_spend_cap_gate(ws_id, context_id=ctx_id)

        service.has_byok_key.assert_called_once_with(ws_id, context_id=ctx_id)

    @pytest.mark.asyncio
    async def test_resolve_paid_by_passes_context_id_to_has_byok_key(self, service):
        """``resolve_paid_by`` MUST forward ``context_id`` to ``has_byok_key``."""
        service.has_byok_key = AsyncMock(return_value=True)

        ws_id = "00000000-0000-0000-0000-000000000001"
        ctx_id = "00000000-0000-0000-0000-000000000002"

        result = await service.resolve_paid_by(ws_id, context_id=ctx_id)

        service.has_byok_key.assert_called_once_with(ws_id, context_id=ctx_id)
        assert result == "byok"

    @pytest.mark.asyncio
    async def test_resolve_paid_by_returns_platform_when_byok_not_applicable(self, service):
        """``resolve_paid_by`` returns "platform" when context-aware probe misses.

        Regression fence: a workspace with BYOK scoped to a DIFFERENT
        context (probe returns False) MUST log as "platform", matching
        what ``_get_user_api_key`` will actually do (env fallback).
        """
        service.has_byok_key = AsyncMock(return_value=False)

        result = await service.resolve_paid_by(
            "00000000-0000-0000-0000-000000000001",
            context_id="00000000-0000-0000-0000-000000000002",
        )

        assert result == "platform"

    @pytest.mark.asyncio
    async def test_prepare_spend_cap_gate_context_id_optional_for_backward_compat(self, service):
        """``context_id`` defaults to None for legacy callers.

        Loop 7 fix: when ``context_id`` is None, ``has_byok_key`` matches
        ONLY workspace-wide keys (``ExternalAPIKey.context_id IS NULL``)
        — mirroring ``_get_user_api_key``'s legacy priority exactly.
        Pre-loop-7 the probe accepted any context-scoped key in the
        workspace, which diverged from the key-lookup it claimed to
        mirror and reintroduced the accounting drift this gate exists
        to prevent.
        """
        service.has_byok_key = AsyncMock(return_value=False)

        await service._prepare_spend_cap_gate("00000000-0000-0000-0000-000000000001")

        service.has_byok_key.assert_called_once_with(
            "00000000-0000-0000-0000-000000000001", context_id=None
        )


class TestDisallowEnvFallback:
    """#708 loop 7: ``disallow_env_fallback`` propagation for Option A reads.

    Closes a TOCTOU race between the preflight ``has_byok_key`` probe
    and the actual ``_get_user_api_key`` call. Without this guard, a
    BYOK key disabled between the probe and the embed call would route
    silently through ``OPENAI_API_KEY`` env — bypassing both the H1
    Option A guard and PR #711's BYOK-only spend cap.
    """

    @pytest.fixture
    def service(self):
        return EmbeddingService(_make_mock_db())

    @pytest.mark.asyncio
    async def test_disallow_env_fallback_raises_notfound_not_configerror(
        self, service, monkeypatch
    ):
        """Loop 8 fix: TOCTOU deny path MUST raise ``NotFoundException``,
        NOT ``ConfigurationError`` — the latter's message interpolates the
        source ``workspace_id`` and the MCP exception serializer would
        leak it (CWE-639 / OWASP A01).

        Even with ``OPENAI_API_KEY`` set, the env fallback is skipped for
        Option A reads. The raised ``NotFoundException`` is then mapped
        by the handler to the uniform ``context_not_found`` response.
        """
        from utils.exceptions import NotFoundException

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-platform-key")

        # Mock DB: no key found
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = None
        service.db.execute = AsyncMock(return_value=execute_result)

        source_ws = "00000000-0000-0000-0000-000000000001"

        with pytest.raises(NotFoundException) as exc_info:
            await service._get_user_api_key(
                user_id="caller",
                context_id="00000000-0000-0000-0000-000000000002",
                workspace_id=source_ws,
                disallow_env_fallback=True,
            )

        # Defense-in-depth: even though the handler crafts the response
        # body (not the exception), assert the exception message does
        # NOT include the source workspace_id in case it ever surfaces.
        assert source_ws not in exc_info.value.message
        assert source_ws not in str(exc_info.value.details)
        # The exception type itself triggers the uniform mapping at the
        # MCP handler — assert that's what we raised.
        assert isinstance(exc_info.value, NotFoundException)
        assert not isinstance(exc_info.value, ConfigurationError)

    @pytest.mark.asyncio
    async def test_default_allows_env_fallback(self, service, monkeypatch):
        """Regression fence: default ``disallow_env_fallback=False`` still uses env."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-platform-key")

        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = None
        service.db.execute = AsyncMock(return_value=execute_result)

        result = await service._get_user_api_key(
            user_id="caller",
            context_id="00000000-0000-0000-0000-000000000002",
            workspace_id="00000000-0000-0000-0000-000000000001",
            # disallow_env_fallback omitted → False
        )

        assert result == "sk-test-platform-key"

    @pytest.mark.asyncio
    async def test_disallow_env_fallback_does_not_block_db_key(self, service, monkeypatch):
        """``disallow_env_fallback=True`` MUST NOT block a legitimate DB key.

        The flag only affects priority-3 env fallback. When a BYOK row
        exists at priority 1 or 2, the lookup should still return it.
        """
        # The env is set but should not be consulted
        monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-be-used")

        # Mock DB: BYOK key found
        api_key_entry = MagicMock()
        api_key_entry.encrypted_value = "encrypted-byok-key-data"
        execute_result = MagicMock()
        execute_result.scalar_one_or_none.return_value = api_key_entry
        service.db.execute = AsyncMock(return_value=execute_result)

        # Patch the encryptor to return a known plaintext
        with patch("services.embedding_service.get_encryptor") as enc:
            enc.return_value.decrypt.return_value = "sk-actual-byok-key"

            result = await service._get_user_api_key(
                user_id="caller",
                context_id="00000000-0000-0000-0000-000000000002",
                workspace_id="00000000-0000-0000-0000-000000000001",
                disallow_env_fallback=True,
            )

        assert result == "sk-actual-byok-key"
        assert result != "sk-must-not-be-used"

    @pytest.mark.asyncio
    async def test_embed_with_usage_propagates_disallow_env_fallback(self, service, monkeypatch):
        """``embed_with_usage(disallow_env_fallback=True)`` MUST propagate to ``_get_client``."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

        captured_kwargs: dict = {}

        async def _fake_get_client(user_id, context_id, workspace_id, **kwargs):
            captured_kwargs.update(kwargs)
            # Return a dummy client whose embeddings.create returns a vector
            client = MagicMock()
            client.embeddings = MagicMock()
            client.embeddings.create = AsyncMock(
                return_value=MagicMock(
                    data=[MagicMock(embedding=[0.1] * 512)],
                    usage=MagicMock(prompt_tokens=10, total_tokens=10),
                )
            )
            return client

        service._get_client = _fake_get_client
        service._prepare_spend_cap_gate = AsyncMock(return_value=(None, None))

        # patch cache to skip the hit path
        with (
            patch("services.embedding_service.get_cache", new=AsyncMock(return_value=None)),
            patch("services.embedding_service.set_cache", new=AsyncMock(return_value=None)),
        ):
            await service.embed_with_usage(
                "test",
                user_id="caller",
                context_id="00000000-0000-0000-0000-000000000002",
                workspace_id="00000000-0000-0000-0000-000000000001",
                disallow_env_fallback=True,
            )

        assert captured_kwargs.get("disallow_env_fallback") is True
