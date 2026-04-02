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
