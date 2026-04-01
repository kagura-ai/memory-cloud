"""Reranker service with multi-provider support.

Issue #105: Add multiple reranker options (Voyage AI, Cohere).
Provides an abstract RerankerProvider base class and concrete implementations.

Architecture:
- RerankerProvider: Abstract base class for reranker providers
- VoyageReranker: Voyage AI implementation (rerank-2.5-lite default)
- CohereReranker: Cohere implementation (rerank-multilingual-v3.0)
- RerankerService: Factory pattern to select active provider from DB
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import ExternalAPIKey
from repositories.config_repository import ContextSearchConfigRepository
from utils.encryption import get_encryptor
from utils.exceptions import CohereError, VoyageError
from utils.logger import get_logger

logger = get_logger(__name__)

# Provider constants (must match external_keys.py)
RERANKER_PROVIDERS = {"cohere", "voyage"}


class RerankerProvider(ABC):
    """Abstract base class for reranker providers.

    All reranker implementations must inherit from this class
    and implement the rerank() method.
    """

    provider_name: str

    @abstractmethod
    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank documents by relevance to query.

        Args:
            query: Search query
            documents: List of document texts to rerank
            top_n: Number of top results to return

        Returns:
            List of dicts with 'index' and 'relevance_score' keys,
            sorted by relevance (descending)
        """
        pass


class VoyageReranker(RerankerProvider):
    """Voyage AI reranker implementation.

    Uses rerank-2.5-lite model by default (cost-effective).
    API Reference: https://docs.voyageai.com/docs/reranker

    Issue #105: Voyage AI integration for cost reduction.
    """

    provider_name = "voyage"

    def __init__(self, api_key: str, model: str = "rerank-2.5-lite"):
        """Initialize Voyage reranker.

        Args:
            api_key: Voyage AI API key
            model: Model name (default: rerank-2.5-lite, also: rerank-2.5)
        """
        self.api_key = api_key
        self.model = model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank using Voyage AI API.

        Note: Voyage client is synchronous, so we run in executor.

        Args:
            query: Search query
            documents: Documents to rerank
            top_n: Number of results

        Returns:
            Reranked results with index and relevance_score

        Raises:
            VoyageError: If API call fails
            ValueError: If top_n <= 0
        """
        # Input validation
        if not documents:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        try:
            import voyageai  # type: ignore[reportMissingImports]

            def _sync_rerank():
                client = voyageai.Client(api_key=self.api_key)  # pyright: ignore[reportPrivateImportUsage]
                return client.rerank(
                    query=query,
                    documents=documents,
                    model=self.model,
                    top_k=top_n,
                )

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _sync_rerank)

            return [
                {"index": r.index, "relevance_score": r.relevance_score} for r in response.results
            ]
        except ValueError:
            raise
        except Exception as e:
            logger.error(
                "voyage_rerank_failed",
                error=str(e),
                model=self.model,
                query_length=len(query),
                doc_count=len(documents),
            )
            raise VoyageError(f"Reranking failed for model {self.model}: {e}") from e


class CohereReranker(RerankerProvider):
    """Cohere reranker implementation.

    Uses rerank-multilingual-v3.0 model by default.
    API Reference: https://docs.cohere.com/reference/rerank
    """

    provider_name = "cohere"

    def __init__(self, api_key: str, model: str = "rerank-multilingual-v3.0"):
        """Initialize Cohere reranker.

        Args:
            api_key: Cohere API key
            model: Model name (default: rerank-multilingual-v3.0)
        """
        self.api_key = api_key
        self.model = model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank using Cohere API.

        Args:
            query: Search query
            documents: Documents to rerank
            top_n: Number of results

        Returns:
            Reranked results with index and relevance_score

        Raises:
            CohereError: If API call fails
            ValueError: If top_n <= 0
        """
        # Input validation
        if not documents:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        try:
            import cohere

            client = cohere.AsyncClient(self.api_key)

            response = await client.rerank(
                model=self.model,
                query=query,
                documents=documents,
                top_n=top_n,
            )

            return [
                {"index": r.index, "relevance_score": r.relevance_score} for r in response.results
            ]
        except ValueError:
            raise
        except Exception as e:
            logger.error(
                "cohere_rerank_failed",
                error=str(e),
                model=self.model,
                query_length=len(query),
                doc_count=len(documents),
            )
            raise CohereError(f"Reranking failed for model {self.model}: {e}") from e


class RerankerService:
    """Service for dynamic reranker provider selection.

    Issue #105: Factory pattern to select active provider based on
    enabled ExternalAPIKey configuration.

    Usage:
        reranker_service = RerankerService(db)
        results = await reranker_service.rerank(
            query="search query",
            candidates=search_results,
            user_id="user123",
            k=10,
            context_id="context-uuid"
        )
    """

    def __init__(self, db: AsyncSession):
        """Initialize reranker service.

        Args:
            db: Database session
        """
        self.db = db

    async def get_active_provider(
        self,
        user_id: str,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> RerankerProvider | None:
        """Get the active reranker provider for user/context/workspace.

        Queries ExternalAPIKey table for enabled reranker key.
        Only ONE reranker can be enabled at a time (validated by external_keys.py).

        Priority: context-scoped > workspace-scoped > user-scoped > env var

        Args:
            user_id: User ID
            context_id: Optional context ID (Issue #82: context-scoped keys)
            workspace_id: Optional workspace ID (Issue #146: workspace-scoped keys)

        Returns:
            RerankerProvider instance or None if no reranker enabled
        """
        from uuid import UUID

        from sqlalchemy import or_

        conditions = [
            ExternalAPIKey.user_id == user_id,
            ExternalAPIKey.provider.in_(RERANKER_PROVIDERS),
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

        if not api_key_entry:
            logger.debug("no_reranker_configured", user_id=user_id, context_id=context_id)
            return None

        # Decrypt API key
        encryptor = get_encryptor()
        api_key = encryptor.decrypt(str(api_key_entry.encrypted_value))
        provider_name = str(api_key_entry.provider)

        # Load project search config for model selection (Issue #130)
        model_name = await self._get_reranker_model(context_id, provider_name)

        # Return appropriate provider with dynamic model
        if provider_name == "voyage":
            logger.debug("using_voyage_reranker", user_id=user_id, model=model_name)
            return VoyageReranker(api_key, model=model_name)
        elif provider_name == "cohere":
            logger.debug("using_cohere_reranker", user_id=user_id, model=model_name)
            return CohereReranker(api_key, model=model_name)

        logger.warning("unknown_reranker_provider", provider=provider_name)
        return None

    async def rerank(
        self,
        query: str,
        candidates: list[dict],
        user_id: str,
        k: int,
        context_id: str | None = None,
        workspace_id: str | None = None,  # NEW: Workspace ID (Issue #146)
    ) -> list[dict]:
        """Rerank search results using active provider.

        Args:
            query: Search query
            candidates: Candidate results with 'payload' containing 'summary'
            user_id: User ID
            k: Number of top results to return
            context_id: Optional context ID (Issue #82)
            workspace_id: Optional workspace ID (Issue #146)

        Returns:
            Reranked results or original candidates if no reranker available
        """
        if not candidates:
            return candidates

        provider = await self.get_active_provider(user_id, context_id, workspace_id)

        if not provider:
            return candidates

        # Prepare documents for reranking
        documents = [
            f"{c['payload']['summary']} {c['payload'].get('context_summary', '')}"
            for c in candidates
        ]

        # Rerank
        results = await provider.rerank(query, documents, k)

        # Map reranked results back to candidates
        reranked = []
        for result in results:
            idx = result["index"]
            candidate = candidates[idx]
            candidate["rerank_score"] = result["relevance_score"]
            candidate["hybrid_score"] = result["relevance_score"]  # Override hybrid score
            reranked.append(candidate)

        logger.debug(
            "reranking_completed",
            provider=provider.provider_name,
            input_count=len(candidates),
            output_count=len(reranked),
        )

        return reranked

    async def _get_reranker_model(self, context_id: str | None, provider_name: str) -> str:
        """Get reranker model name from context configuration.

        Issue #130: Dynamic model selection based on context settings.

        Args:
            context_id: Context ID (string format)
            provider_name: Reranker provider name ('voyage' or 'cohere')

        Returns:
            Model name string (provider-specific)
        """
        # Default models per provider
        default_models = {
            "voyage": "rerank-2",
            "cohere": "rerank-multilingual-v3.0",
        }

        if not context_id:
            model = default_models.get(provider_name, "rerank-2")
            logger.debug(
                "reranker_model_default",
                provider=provider_name,
                model=model,
                reason="no_context_id",
            )
            return model

        # Load config from database
        try:
            repo = ContextSearchConfigRepository(self.db)
            config = await repo.create_or_get(UUID(context_id))

            # Verify provider matches config
            if config.reranker_provider != provider_name:
                logger.warning(
                    "reranker_provider_mismatch",
                    context_id=context_id,
                    config_provider=config.reranker_provider,
                    active_provider=provider_name,
                    fallback="using_config_model_anyway",
                )

            model = config.reranker_model or default_models.get(provider_name, "rerank-2")
            logger.debug(
                "reranker_model_loaded",
                context_id=context_id,
                provider=provider_name,
                model=model,
            )
            return model

        except Exception as e:
            model = default_models.get(provider_name, "rerank-2")
            logger.warning(
                "reranker_model_load_failed",
                context_id=context_id,
                provider=provider_name,
                error=str(e),
                fallback_model=model,
            )
            return model
