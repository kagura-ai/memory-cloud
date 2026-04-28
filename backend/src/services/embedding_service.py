"""Embedding service for vector generation.

Uses workspace-scoped OpenAI API keys stored in the ExternalAPIKey table (#385):
the current workspace's enabled OpenAI key is shared by every member of that
workspace. `user_id` on the key is creator metadata only, not a visibility filter.

Issue #1: API keys are DB-managed, not in .env
Issue #84 Phase 2A: Redis caching with xxHash keys (50-80% API reduction, 4x space savings)
Issue #105: DB-first API key retrieval with environment variable fallback
Issue #385: workspace-keyed lookup (was user-keyed pre-#385)
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
        self._ollama_verified = False

    async def _get_user_api_key(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # Issue #146
    ) -> str:
        """Resolve the OpenAI API key for the calling user's workspace context.

        Issue #105: DB-first lookup with environment variable fallback.
        Issue #146: workspace-scoped keys.
        Issue #385: the lookup is workspace-keyed, not user-keyed — any workspace
            member can use the owner-registered workspace API key. The `user_id`
            parameter is retained for audit logging only; it is NOT a visibility
            filter.

        Priority:
        1. Context-scoped key (context_id matches AND workspace_id matches)
        2. Workspace-scoped key (workspace_id matches AND context_id IS NULL)
        3. Environment variable (OPENAI_API_KEY) — development fallback only

        Args:
            user_id: Caller's user ID — logged for audit, NOT used as a filter (#385).
            context_id: Optional context UUID (#82: context-scoped keys take priority).
            workspace_id: Workspace UUID (#146); when omitted the DB lookup is skipped
                and only the env-var fallback can satisfy the request.

        Returns:
            Decrypted OpenAI API key.

        Raises:
            ConfigurationError: If neither a DB key nor an env var is available.
        """
        from uuid import UUID

        from sqlalchemy import or_

        api_key_entry = None
        if workspace_id:
            workspace_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
            conditions = [
                ExternalAPIKey.workspace_id == workspace_uuid,
                ExternalAPIKey.provider == "openai",
                ExternalAPIKey.enabled.is_(True),
            ]
            if context_id:
                context_uuid = UUID(context_id) if isinstance(context_id, str) else context_id
                # Context-scoped key wins; fall back to workspace-scoped (context_id IS NULL).
                conditions.append(
                    or_(
                        ExternalAPIKey.context_id == context_uuid,
                        ExternalAPIKey.context_id.is_(None),
                    )
                )
            else:
                conditions.append(ExternalAPIKey.context_id.is_(None))

            query = (
                select(ExternalAPIKey)
                .where(and_(*conditions))
                .order_by(ExternalAPIKey.context_id.desc().nulls_last())
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
                workspace_id=workspace_id,
            )
            return api_key

        # Fallback to environment variable (development / env-only deployments).
        env_key = os.getenv("OPENAI_API_KEY")
        if env_key:
            logger.debug(
                "openai_api_key_from_env",
                user_id=user_id,
            )
            return env_key

        if workspace_id:
            raise ConfigurationError(
                f"OpenAI API key not configured for workspace {workspace_id}. "
                "Configure a workspace OpenAI API key in settings, or set the "
                "OPENAI_API_KEY environment variable."
            )
        raise ConfigurationError(
            "OpenAI API key not configured: no workspace context was provided. "
            "Provide a workspace_id, configure a workspace OpenAI API key in "
            "settings, or set the OPENAI_API_KEY environment variable."
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
            # Verify Ollama is reachable on first use only
            if not self._ollama_verified:
                import httpx

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
                self._ollama_verified = True
            return AsyncOpenAI(
                base_url=f"{self.ollama_base_url}/v1",
                api_key="ollama",  # Ollama doesn't require a real key
            )
        # OpenAI (default)
        api_key = await self._get_user_api_key(user_id, context_id, workspace_id)
        return AsyncOpenAI(api_key=api_key)

    def _build_embedding_kwargs(self, input_data: str | list[str]) -> dict:
        """Build kwargs for OpenAI-compatible embeddings.create() call."""
        kwargs: dict = {"model": self.model, "input": input_data}
        # OpenAI supports dimensions param; Ollama infers from model
        if self.provider == "openai":
            kwargs["dimensions"] = self.dimensions
        return kwargs

    async def embed(
        self,
        text: str,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> list[float]:
        """Generate embedding for text with Redis caching.

        Thin wrapper around ``embed_with_usage()`` that discards the
        token count — kept for callers that don't need cost attribution
        (recall, search). Issue #475 will migrate the remaining callers
        to ``embed_with_usage()`` directly.

        Args:
            text: Text to embed (summary)
            user_id: User ID (to retrieve API key)
            context_id: Optional project ID (Issue #82: context-scoped keys)
            workspace_id: Optional workspace ID (Issue #146: workspace-scoped keys)

        Returns:
            Embedding vector (dimensions depend on configured model)

        Raises:
            OpenAIError: If embedding generation fails
            ConfigurationError: If API key not configured
        """
        vector, _ = await self.embed_with_usage(
            text,
            user_id=user_id,
            context_id=context_id,
            workspace_id=workspace_id,
        )
        return vector

    async def embed_with_usage(
        self,
        text: str,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,
    ) -> tuple[list[float], int]:
        """Generate embedding and return token usage for cost tracking (#471).

        Like ``embed()`` but also returns the input token count from the
        provider's API response, so callers can attribute embedding cost
        per-(provider, model) via ``llm_pricing``. Cache hits return
        ``(vector, 0)`` because cached responses consume no API tokens.

        This is an additive companion to ``embed()`` rather than a
        breaking signature change — ``embed()`` keeps working for callers
        that don't track cost (recall path, search service). #475 will
        migrate the remaining callers as part of the
        full-pipeline embedding-cost rollout.

        Args:
            text: Text to embed.
            user_id: User ID for API key lookup.
            context_id: Optional context ID for scoped API key.
            workspace_id: Optional workspace ID for scoped API key.

        Returns:
            ``(vector, tokens_used)``. ``tokens_used`` is 0 for cache
            hits and for providers that don't expose ``response.usage``.

        Raises:
            OpenAIError: If embedding generation fails.
            ConfigurationError: If API key not configured.
        """
        try:
            normalized_text = unicodedata.normalize("NFC", text)

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
                return vector, 0

            client = await self._get_client(user_id, context_id, workspace_id)
            response = await client.embeddings.create(
                **self._build_embedding_kwargs(normalized_text)
            )

            vector = response.data[0].embedding
            # Embeddings only have an "input" side — no completion tokens.
            # OpenAI returns ``usage.prompt_tokens`` (and the same value as
            # ``total_tokens``); Ollama returns 0 because it has no usage
            # accounting. Read defensively — older provider SDKs may omit
            # the usage object entirely.
            usage = getattr(response, "usage", None)
            tokens_used = 0
            if usage is not None:
                tokens_used = (
                    getattr(usage, "prompt_tokens", None)
                    or getattr(usage, "total_tokens", None)
                    or 0
                )

            await set_cache(cache_key, json.dumps(vector), ttl=86400)

            logger.debug(
                "embedding_generated",
                user_id=user_id,
                text_length=len(text),
                vector_dim=len(vector),
                tokens=tokens_used,
                cached=False,
            )

            return vector, tokens_used

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
                response = await client.embeddings.create(
                    **self._build_embedding_kwargs(uncached_texts)
                )

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
