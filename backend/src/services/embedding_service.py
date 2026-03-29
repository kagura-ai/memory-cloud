"""Embedding service for vector generation.

Uses user-specific OpenAI API keys stored in database (ExternalAPIKey table).
Issue #1: API keys are DB-managed, not in .env
Issue #84 Phase 2A: Redis caching with xxHash keys (50-80% API reduction, 4x space savings)
Issue #105: DB-first API key retrieval with environment variable fallback
"""

from __future__ import annotations

import json
import os
import unicodedata

import xxhash
from openai import AsyncOpenAI
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.redis import get_cache, set_cache
from models.auth import ExternalAPIKey
from utils.encryption import get_encryptor
from utils.exceptions import ConfigurationError, OpenAIError
from utils.logger import get_logger

logger = get_logger(__name__)


class EmbeddingService:
    """Service for generating embeddings using OpenAI or Ollama.

    Supports multiple providers via OpenAI-compatible API.
    User-specific API keys are retrieved from database (encrypted).
    """

    def __init__(
        self,
        db: AsyncSession,
        model: str | None = None,
        dimensions: int | None = None,
    ):
        """Initialize embedding service.

        Args:
            db: Database session (for retrieving user's API key)
            model: Embedding model name (default: from settings)
            dimensions: Vector dimensions (default: from settings or model registry)
        """
        from config.constants import EMBEDDING_MODEL_REGISTRY
        from config.settings import get_settings

        settings = get_settings()
        self.db = db
        self.model = model or settings.embedding_model
        # Look up dimensions from registry if not explicitly provided
        if dimensions is not None:
            self.dimensions = dimensions
        elif self.model in EMBEDDING_MODEL_REGISTRY:
            self.dimensions = EMBEDDING_MODEL_REGISTRY[self.model][0]
        else:
            self.dimensions = settings.embedding_dimensions
        # Determine provider from registry or settings
        if self.model in EMBEDDING_MODEL_REGISTRY:
            self.provider = EMBEDDING_MODEL_REGISTRY[self.model][1]
        else:
            self.provider = settings.embedding_provider
        self.ollama_base_url = settings.ollama_base_url

    async def _get_user_api_key(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> str:
        """Get user's OpenAI API key from database or environment.

        Issue #105: DB-first API key retrieval with environment variable fallback.
        Issue #146: Added workspace-scoped key support.

        Priority:
        1. Context-scoped key (most specific)
        2. Workspace-scoped key (workspace-wide)
        3. User-scoped key (personal)
        4. Environment variable (OPENAI_API_KEY) - fallback for development

        Args:
            user_id: User ID
            context_id: Optional project ID (Issue #82: context-scoped keys)
            workspace_id: Optional workspace ID (Issue #146: workspace-scoped keys)

        Returns:
            Decrypted OpenAI API key

        Raises:
            ConfigurationError: If API key not configured
        """
        from uuid import UUID

        from sqlalchemy import or_

        # 1. Try DB first (production mode)
        conditions = [
            ExternalAPIKey.user_id == user_id,
            ExternalAPIKey.provider == "openai",
            ExternalAPIKey.enabled.is_(True),
        ]

        # Add scope filters with priority (context OR workspace OR user-only)
        scope_conditions = []
        if context_id:
            context_uuid = UUID(context_id) if isinstance(context_id, str) else context_id
            scope_conditions.append(ExternalAPIKey.context_id == context_uuid)
        if workspace_id:
            workspace_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
            scope_conditions.append(ExternalAPIKey.workspace_id == workspace_uuid)

        if scope_conditions:
            # Match: (project_id OR workspace_id) OR (no context AND no workspace = user-scoped)
            conditions.append(
                or_(
                    *scope_conditions,
                    and_(
                        ExternalAPIKey.context_id.is_(None), ExternalAPIKey.workspace_id.is_(None)
                    ),
                )
            )

        # Priority ordering: context > workspace > user
        query = (
            select(ExternalAPIKey)
            .where(and_(*conditions))
            .order_by(
                ExternalAPIKey.context_id.desc().nulls_last(),
                ExternalAPIKey.workspace_id.desc().nulls_last(),
            )
            .limit(1)
        )

        result = await self.db.execute(query)
        api_key_entry = result.scalar_one_or_none()

        if api_key_entry:
            encryptor = get_encryptor()
            api_key = encryptor.decrypt(str(api_key_entry.encrypted_value))
            logger.debug(
                "openai_api_key_from_db",
                user_id=user_id,
                context_id=context_id,
            )
            return api_key

        # 2. Fallback to environment variable (development mode)
        env_key = os.getenv("OPENAI_API_KEY")
        if env_key:
            logger.debug(
                "openai_api_key_from_env",
                user_id=user_id,
            )
            return env_key

        # 3. No API key found
        raise ConfigurationError(
            f"OpenAI API key not configured for user {user_id}. "
            "Please set OPENAI_API_KEY environment variable or configure in settings."
        )

    async def _get_client(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> AsyncOpenAI:
        """Get the appropriate OpenAI-compatible client for the configured provider.

        Args:
            user_id: User ID (for API key retrieval)
            context_id: Optional context ID
            workspace_id: Optional workspace ID

        Returns:
            AsyncOpenAI client configured for the provider
        """
        if self.provider == "ollama":
            import httpx

            # Verify Ollama is reachable on first use
            try:
                async with httpx.AsyncClient(timeout=5.0) as http:
                    resp = await http.get(self.ollama_base_url)
                    if resp.status_code != 200:
                        raise ConfigurationError(
                            f"Ollama not responding at {self.ollama_base_url} (HTTP {resp.status_code})"
                        )
            except httpx.ConnectError as err:
                raise ConfigurationError(
                    f"Cannot connect to Ollama at {self.ollama_base_url}. "
                    "Is Ollama running? Start with: ollama serve"
                ) from err
            return AsyncOpenAI(
                base_url=f"{self.ollama_base_url}/v1",
                api_key="ollama",  # Ollama doesn't require a real key
            )
        # OpenAI (default)
        api_key = await self._get_user_api_key(user_id, context_id, workspace_id)
        return AsyncOpenAI(api_key=api_key)

    async def embed(
        self,
        text: str,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> list[float]:
        """Generate embedding for text with Redis caching.

        Args:
            text: Text to embed (summary)
            user_id: User ID (to retrieve API key)
            context_id: Optional project ID (Issue #82: context-scoped keys)
            workspace_id: Optional workspace ID (Issue #146: workspace-scoped keys)

        Returns:
            Embedding vector (512 dimensions)

        Raises:
            OpenAIError: If embedding generation fails
            ConfigurationError: If API key not configured

        Example:
            >>> service = EmbeddingService(db)
            >>> vector = await service.embed("認証エラー修正", user_id="kiyota")
            >>> len(vector)
            512

        Note:
            Issue #84 Phase 2A: Uses Redis cache (TTL: 24h) to reduce API calls by 50-80%
        """
        try:
            # ============================================================================
            # BUG FIX #122-2: Unicode normalization for consistent cache keys
            # ============================================================================
            # Problem: Same Japanese text with different Unicode normalization
            #          (NFC vs NFD) produces different xxHash digests, causing
            #          cache misses for identical content.
            #
            # Example: "が" can be encoded as:
            #   - NFC: U+304C (single codepoint) → one xxHash
            #   - NFD: U+304B + U+3099 (two codepoints) → different xxHash
            #
            # Solution: Always normalize to NFC before hashing.
            # ============================================================================
            normalized_text = unicodedata.normalize("NFC", text)

            # Issue #84 Phase 2A: Check cache first (xxHash for 4x speed/space improvement)
            # Include model in cache key to avoid cross-model collisions
            text_hash = xxhash.xxh64(normalized_text.encode()).hexdigest()[:16]
            cache_key = f"emb:{self.model}:{text_hash}"
            cached = await get_cache(cache_key)

            if cached:
                vector = json.loads(cached)
                logger.debug(
                    "embedding_cache_hit",
                    user_id=user_id,
                    text_length=len(text),
                    cache_key=cache_key[:16] + "...",
                )
                return vector

            # Cache miss - generate embedding (use normalized text for consistency)
            client = await self._get_client(user_id, context_id, workspace_id)

            kwargs: dict = {"model": self.model, "input": normalized_text}
            # OpenAI supports dimensions param; Ollama infers from model
            if self.provider == "openai":
                kwargs["dimensions"] = self.dimensions

            response = await client.embeddings.create(**kwargs)

            vector = response.data[0].embedding

            # Cache for 24 hours (86400 seconds)
            await set_cache(cache_key, json.dumps(vector), ttl=86400)

            logger.debug(
                "embedding_generated",
                user_id=user_id,
                text_length=len(text),
                vector_dim=len(vector),
                cached=True,
            )

            return vector

        except ConfigurationError:
            raise

        except Exception as e:
            logger.error("embedding_failed", error=str(e), user_id=user_id)
            raise OpenAIError(f"Embedding generation failed: {e}") from e

    async def embed_batch(
        self,
        texts: list[str],
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> list[list[float]]:
        """Generate embeddings for multiple texts with Redis caching.

        Args:
            texts: List of texts to embed
            user_id: User ID
            context_id: Optional project ID (Issue #82: context-scoped keys)
            workspace_id: Optional workspace ID (Issue #146: workspace-scoped keys)

        Returns:
            List of embedding vectors

        Raises:
            OpenAIError: If embedding generation fails

        Note:
            Issue #84 Phase 2A: Checks cache for each text, only generates uncached ones
        """
        try:
            # Issue #84 Phase 2A: Check cache for each text
            # Issue #122: Normalize all texts first for consistent caching
            normalized_texts = [unicodedata.normalize("NFC", t) for t in texts]

            results: list[list[float] | None] = []
            uncached_indices: list[int] = []
            uncached_texts: list[str] = []

            for i, text in enumerate(normalized_texts):
                text_hash = xxhash.xxh64(text.encode()).hexdigest()[:16]
                cache_key = f"emb:{self.model}:{text_hash}"
                cached = await get_cache(cache_key)

                if cached:
                    results.append(json.loads(cached))
                else:
                    results.append(None)
                    uncached_indices.append(i)
                    uncached_texts.append(text)

            cache_hit_rate = (len(texts) - len(uncached_texts)) / len(texts) * 100 if texts else 0

            logger.debug(
                "batch_embedding_cache_check",
                user_id=user_id,
                total=len(texts),
                cached=len(texts) - len(uncached_texts),
                uncached=len(uncached_texts),
                hit_rate=f"{cache_hit_rate:.1f}%",
            )

            # Generate embeddings only for uncached texts
            if uncached_texts:
                client = await self._get_client(user_id, context_id, workspace_id)

                kwargs: dict = {"model": self.model, "input": uncached_texts}
                if self.provider == "openai":
                    kwargs["dimensions"] = self.dimensions

                response = await client.embeddings.create(**kwargs)

                # Cache new embeddings and fill results
                for i, (idx, item) in enumerate(zip(uncached_indices, response.data, strict=False)):
                    vector = item.embedding
                    results[idx] = vector

                    # Cache for 24 hours (use normalized text from uncached_texts)
                    text_hash = xxhash.xxh64(uncached_texts[i].encode()).hexdigest()[:16]
                    cache_key = f"emb:{self.model}:{text_hash}"
                    await set_cache(cache_key, json.dumps(vector), ttl=86400)

                logger.debug(
                    "batch_embedding_generated",
                    user_id=user_id,
                    generated=len(uncached_texts),
                )

            # Return all vectors (cached + newly generated)
            return [v for v in results if v is not None]

        except Exception as e:
            logger.error("batch_embedding_failed", error=str(e), user_id=user_id)
            raise OpenAIError(f"Batch embedding failed: {e}") from e
