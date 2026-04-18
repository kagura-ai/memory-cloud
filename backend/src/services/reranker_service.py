"""Reranker service with multi-provider support.

Issue #105: Add multiple reranker options (Voyage AI, Cohere).
Issue #70: Add Ollama as local reranker provider.

Architecture:
- RerankerProvider: Abstract base class for reranker providers
- VoyageReranker: Voyage AI implementation (rerank-2.5-lite default)
- CohereReranker: Cohere implementation (rerank-multilingual-v3.0)
- OllamaReranker: Local Ollama reranker (no API key needed)
- RerankerService: Factory pattern to select active provider from DB
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import ExternalAPIKey
from repositories.config_repository import ContextSearchConfigRepository
from utils.encryption import get_encryptor
from utils.exceptions import CohereError, VoyageError
from utils.logger import get_logger

logger = get_logger(__name__)

# Provider constants (must match external_keys.py for API-key providers)
RERANKER_PROVIDERS = {"cohere", "voyage"}

# Default Ollama reranker model
DEFAULT_OLLAMA_RERANK_MODEL = "dengcao/Qwen3-Reranker-8B:Q5_K_M"


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


class OllamaReranker(RerankerProvider):
    """Ollama local reranker using prompt-based relevance scoring (Issue #70).

    Uses a local LLM to score document relevance on a 0-1 scale.
    No API key required — runs on local Ollama instance.
    """

    provider_name = "ollama"

    def __init__(self, base_url: str, model: str = DEFAULT_OLLAMA_RERANK_MODEL):
        """Initialize Ollama reranker.

        Args:
            base_url: Ollama API base URL (e.g. http://localhost:11434)
            model: Ollama model name for reranking
        """
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> list[dict[str, Any]]:
        """Rerank using Ollama local model.

        Scores each document individually via /api/generate with a relevance prompt.

        Args:
            query: Search query
            documents: Documents to rerank
            top_n: Number of results

        Returns:
            Reranked results with index and relevance_score

        Raises:
            Exception: If Ollama API call fails
        """
        if not documents:
            return []
        if top_n <= 0:
            raise ValueError("top_n must be positive")

        scored: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Score documents concurrently in batches
            semaphore = asyncio.Semaphore(5)  # Limit concurrent Ollama requests

            async def score_doc(idx: int, doc: str) -> dict[str, Any]:
                async with semaphore:
                    prompt = (
                        f"Score the relevance of the document to the query "
                        f"on a scale of 0 to 1.\n\n"
                        f"Query: {query}\n"
                        f"Document: {doc[:500]}\n\n"
                        f"Score: "
                    )
                    try:
                        resp = await client.post(
                            f"{self.base_url}/api/generate",
                            json={
                                "model": self.model,
                                "prompt": prompt,
                                "stream": False,
                                "options": {"temperature": 0, "num_predict": 5, "stop": ["\n"]},
                            },
                        )
                        resp.raise_for_status()
                        text = resp.json().get("response", "").strip()
                        # Parse score — extract first float-like value
                        score = _parse_relevance_score(text)
                    except Exception as e:
                        logger.warning(
                            "ollama_rerank_score_failed",
                            index=idx,
                            error=str(e),
                        )
                        score = 0.0

                    return {"index": idx, "relevance_score": score}

            tasks = [score_doc(i, doc) for i, doc in enumerate(documents)]
            scored = await asyncio.gather(*tasks)

        # Sort by score descending and take top_n
        scored.sort(key=lambda x: x["relevance_score"], reverse=True)

        logger.debug(
            "ollama_rerank_completed",
            model=self.model,
            doc_count=len(documents),
            top_n=top_n,
        )

        return scored[:top_n]


def _parse_relevance_score(text: str) -> float:
    """Parse a relevance score from model output.

    Handles formats: "0.8", "0.8/1", "80%", "0", "1", etc.

    Returns:
        Float between 0.0 and 1.0
    """
    text = text.strip()
    if not text:
        return 0.0

    # Try percentage anywhere: "80%" -> 0.8
    pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if pct_match:
        return min(float(pct_match.group(1)) / 100.0, 1.0)

    # Try fraction anywhere: "0.8/1" -> 0.8
    frac_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
    if frac_match:
        num, den = float(frac_match.group(1)), float(frac_match.group(2))
        return min(num / den, 1.0) if den > 0 else 0.0

    # Try plain number anywhere (clamp to 0.0-1.0)
    num_match = re.search(r"(\d+(?:\.\d+)?)", text)
    if num_match:
        return min(float(num_match.group(1)), 1.0)

    return 0.0


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
        """Get the active reranker provider for the current workspace context.

        Queries ExternalAPIKey for the enabled reranker key. Issue #105's
        cross-provider exclusivity (one of Cohere / Voyage per workspace) is
        enforced at validate_reranker_exclusivity call sites in external_keys.py
        plus by the a99 migration's pre-flight check.

        Issue #385: key resolution is workspace-keyed. When workspace_id is
        omitted, the reranker is treated as not configured (returns None) —
        there is no user-scoped or global fallback tier anymore.

        Priority within a workspace: context-scoped > workspace-scoped.

        Args:
            user_id: Caller's user ID — logged for audit, NOT used as a filter (#385).
            context_id: Optional context UUID (context-scoped keys take priority).
            workspace_id: Workspace UUID; when omitted returns None.

        Returns:
            RerankerProvider instance, or None if no reranker key configured.
        """
        from uuid import UUID

        from sqlalchemy import or_

        # Issue #70: Check context config first — Ollama takes priority (no API key needed)
        if context_id:
            from config.settings import get_settings
            from models.config import ContextSearchConfig

            ctx_result = await self.db.execute(
                select(ContextSearchConfig).where(
                    ContextSearchConfig.context_id == UUID(context_id)
                )
            )
            ctx_config = ctx_result.scalar_one_or_none()
            if ctx_config and ctx_config.reranker_provider == "ollama" and ctx_config.use_rerank:
                settings = get_settings()
                # Avoid non-Ollama default models (e.g. "rerank-2" from previous provider)
                non_ollama_defaults = {"rerank-2", "rerank-2-lite", "rerank-multilingual-v3.0"}
                model = ctx_config.reranker_model
                if not model or model in non_ollama_defaults:
                    model = DEFAULT_OLLAMA_RERANK_MODEL
                logger.debug("using_ollama_reranker", user_id=user_id, model=model)
                return OllamaReranker(base_url=settings.ollama_base_url, model=model)

        # API-key providers (Voyage, Cohere).
        # Issue #385: workspace-keyed lookup; user_id is for audit, not a filter.
        # Without a workspace context the reranker is treated as not configured —
        # API-key reranker rows live in the external_api_keys table which is now
        # workspace-scoped (NOT NULL), so a no-workspace caller has nothing to find.
        if not workspace_id:
            logger.debug("no_reranker_configured", user_id=user_id, reason="no_workspace_context")
            return None

        workspace_uuid = UUID(workspace_id) if isinstance(workspace_id, str) else workspace_id
        conditions = [
            ExternalAPIKey.workspace_id == workspace_uuid,
            ExternalAPIKey.provider.in_(RERANKER_PROVIDERS),
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
            "ollama": DEFAULT_OLLAMA_RERANK_MODEL,
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
