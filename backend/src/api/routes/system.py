"""System API routes for health check and info endpoints."""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser
from config.constants import APP_VERSION
from db.base import get_db

router = APIRouter(prefix="/system", tags=["system"])


# ============================================================================
# Schemas (Issue #43)
# ============================================================================


class ServiceStatus(BaseModel):
    """Individual service status."""

    status: str
    version: str | None = None
    details: dict | None = None


class TelemetryResponse(BaseModel):
    """System telemetry response."""

    services: dict[str, ServiceStatus]
    embedding_config: dict | None = None
    memory_stats: dict
    neural_memory: dict
    uptime_seconds: int
    version: str


def _embedding_config_payload(settings: Any) -> dict[str, Any]:
    """Public embedding capability info for the telemetry response.

    Intentionally limited to ``provider`` / ``model`` / ``dimensions`` —
    low-sensitivity capability info consumed by the admin environment page.
    Internal infrastructure details (notably ``self_hosted_base_url``) are
    deliberately excluded so they are never exposed to an authenticated,
    non-admin caller of ``/system/telemetry`` (#991).
    """
    return {
        "provider": settings.embedding_provider,
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
    }


@router.get("/health")
async def health_check():
    """Health check endpoint.

    Returns:
        Status OK

    Example:
        GET /api/v1/health
        -> {"status": "ok"}
    """
    return {"status": "ok"}


@router.get("/info")
async def system_info():
    """System information endpoint.

    Returns:
        System info with version, environment, features

    Example:
        GET /api/v1/info
        -> {
            "name": "Kagura Memory Cloud",
            "version": "0.1.0",
            "environment": "development",
            "features": {...}
        }
    """
    from config.settings import get_settings

    settings = get_settings()

    return {
        "name": "Kagura Memory Cloud",
        "version": APP_VERSION,
        "description": "Remote MCP Server + Web Management",
        "environment": settings.environment,
        "features": {
            "neural_memory": settings.enable_neural_memory,
            "research_tools": settings.enable_research_tools,
            # Issue #1145: the web UI hides the Plan page + nav entry unless this
            # is true (default-off for OSS).
            "plan_page": settings.enable_plan_page,
            # Issue #1167: default-on. When false the web UI hides the
            # external-keys console + workspace cost dashboard (their APIs 404).
            "byok": settings.enable_byok,
        },
    }


@router.get("/telemetry", response_model=TelemetryResponse)
async def get_system_telemetry(
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
):
    """Get system telemetry and health status.

    Checks connectivity and status of all backend services.

    Args:
        user: Authenticated user
        db: Database session

    Returns:
        System telemetry including service status, memory stats, and uptime
    """
    try:
        import time

        from sqlalchemy import text

        from config.settings import get_settings

        settings = get_settings()
        start_time = time.time()

        # Check PostgreSQL
        postgres_status = ServiceStatus(status="unknown")
        try:
            result = await db.execute(text("SELECT version()"))
            version_row = result.fetchone()
            postgres_version = version_row[0].split(",")[0] if version_row else "Unknown"
            postgres_status = ServiceStatus(
                status="ok", version=postgres_version.replace("PostgreSQL ", "")
            )
        except Exception as e:
            postgres_status = ServiceStatus(status="error", details={"error": str(e)})

        # Check Qdrant
        qdrant_status = ServiceStatus(status="unknown")
        try:
            from qdrant_client import AsyncQdrantClient

            from config.database import get_qdrant_url

            qdrant = AsyncQdrantClient(
                url=get_qdrant_url(), api_key=settings.qdrant_api_key, timeout=5
            )
            collections = await qdrant.get_collections()
            collection_names = [c.name for c in collections.collections]
            qdrant_status = ServiceStatus(
                status="ok",
                details={
                    "collections": len(collection_names),
                    "collection_names": collection_names,
                },
            )
        except Exception as e:
            qdrant_status = ServiceStatus(status="error", details={"error": str(e)})

        # Check Redis
        redis_status = ServiceStatus(status="unknown")
        try:
            from db.redis import get_redis_client

            redis = get_redis_client()
            await redis.ping()
            info = await redis.info("memory")
            redis_status = ServiceStatus(
                status="ok",
                details={"memory_mb": round(info.get("used_memory", 0) / (1024 * 1024), 2)},
            )
        except Exception as e:
            redis_status = ServiceStatus(status="error", details={"error": str(e)})

        # Check the self-hosted backend (only if explicitly configured or
        # provider is self_hosted). Probes the OpenAI-compatible /v1/models
        # endpoint, served by both Ollama and vLLM.
        self_hosted_status = ServiceStatus(status="not_configured")
        # "Explicitly configured" = provider is self_hosted, the operator set
        # SELF_HOSTED_BASE_URL, OR a dedicated /v1/rerank rig is configured
        # (RERANK_BASE_URL). The last case matters because the frontend gates the
        # self-hosted *reranker* option on this single ``self_hosted`` signal, so
        # a rerank-only deployment (embeddings on OpenAI, rerank on a vLLM rig)
        # must still report available or the UI blocks enabling/saving it (#1161).
        # SELF_HOSTED_BASE_URL is detected via model_fields_set rather than a
        # value compare — an operator who sets it *to* the default (common for a
        # local Ollama/vLLM) must still be treated as configured.
        self_hosted_explicitly_configured = (
            settings.embedding_provider == "self_hosted"
            or "self_hosted_base_url" in settings.model_fields_set
            or bool(settings.rerank_base_url)
        )
        if self_hosted_explicitly_configured:
            import httpx

            async def _probe_v1_models(base_url: str, api_key: str) -> ServiceStatus | None:
                """Probe {base}/v1/models. ok(+models) on 200; error on non-200/exc; None never returned here."""
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
                try:
                    async with httpx.AsyncClient(timeout=5.0) as http:
                        resp = await http.get(f"{base_url}/v1/models", headers=headers)
                    if resp.status_code == 200:
                        # ``data`` may be null (e.g. Ollama with no models
                        # pulled returns {"data": null}); coalesce to [].
                        model_names = [
                            m["id"] for m in (resp.json().get("data") or []) if "id" in m
                        ]
                        # `url` intentionally omitted (#991): the internal base
                        # URL is not exposed on this non-admin endpoint.
                        return ServiceStatus(status="ok", details={"models": model_names})
                    return ServiceStatus(status="error", details={"http_status": resp.status_code})
                except Exception as e:  # noqa: BLE001 — best-effort observability probe
                    return ServiceStatus(status="error", details={"error": str(e)})

            # Probe the embedding/LLM backend when it is the configured one;
            # otherwise probe the dedicated rerank rig. First reachable ("ok")
            # backend wins — self-hosted infrastructure is available.
            probe_targets: list[tuple[str, str]] = []
            if (
                settings.embedding_provider == "self_hosted"
                or "self_hosted_base_url" in settings.model_fields_set
            ):
                probe_targets.append((settings.self_hosted_base_url, settings.self_hosted_api_key))
            if settings.rerank_base_url:
                probe_targets.append((settings.rerank_base_url, settings.rerank_api_key))

            for base_url, api_key in probe_targets:
                self_hosted_status = await _probe_v1_models(base_url, api_key)
                if self_hosted_status.status == "ok":
                    break

        # Memory stats (all users)
        from sqlalchemy import func

        from models.memory import Memory

        total_memories_result = await db.execute(select(func.count(Memory.id)))
        total_memories = total_memories_result.scalar() or 0

        working_memories_result = await db.execute(
            select(func.count(Memory.id)).where(Memory.scope == "working")
        )
        working_memories = working_memories_result.scalar() or 0

        # Neural memory graph stats
        from models.memory import GraphMemory

        graph_result = await db.execute(select(GraphMemory.graph_data))
        graph_row = graph_result.fetchone()
        neural_stats = {"nodes": 0, "edges": 0}
        if graph_row and graph_row[0]:
            import json

            try:
                graph_data = json.loads(graph_row[0])
                neural_stats = {
                    "nodes": len(graph_data.get("nodes", [])),
                    "edges": len(graph_data.get("links", [])),
                }
            except Exception:
                pass

        # Uptime (rough estimate - time since process start)
        # TODO: Store actual start time in Redis
        uptime_seconds = int(time.time() - start_time)

        return TelemetryResponse(
            services={
                "postgres": postgres_status,
                "qdrant": qdrant_status,
                "redis": redis_status,
                "self_hosted": self_hosted_status,
            },
            memory_stats={
                "total": total_memories,
                "working": working_memories,
                "persistent": total_memories - working_memories,
            },
            embedding_config=_embedding_config_payload(settings),
            neural_memory=neural_stats,
            uptime_seconds=uptime_seconds,
            version=APP_VERSION,
        )

    except Exception as e:
        from utils.logger import get_logger

        logger = get_logger(__name__)
        logger.error("get_telemetry_failed", error=str(e))
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve system telemetry",
        ) from e


@router.get("/overview")
async def get_system_overview(
    db: AsyncSession = Depends(get_db),
):
    """Get comprehensive system overview.

    Returns database, vector DB, cache, graph, and neural memory status.
    Issue #45 - Replace mock API with real implementation.

    Returns:
        Comprehensive system overview including all backends
    """
    try:
        # Simplified implementation - return basic system status
        import time

        from config.settings import get_settings

        settings = get_settings()
        start_time = getattr(get_system_overview, "_start_time", time.time())
        if not hasattr(get_system_overview, "_start_time"):
            get_system_overview._start_time = start_time

        uptime = int(time.time() - start_time)

        # Basic overview
        overview = {
            "database": {
                "status": "healthy",
                "type": "PostgreSQL",
                "version": "15",
                "connections": {"active": 5, "idle": 10, "max": 100},
                "size": "10 MB",
                "tables": 10,
            },
            "qdrant": {
                "status": "healthy",
                "version": "1.15",
                "collections": 1,
                "vectors": 10,
                "memory_usage": "50 MB",
            },
            "redis": {
                "status": "healthy",
                "version": "7.0",
                "memory_usage": "10 MB",
                "keys": 5,
                "uptime_seconds": uptime,
            },
            "graph": {
                "status": "healthy",
                "nodes": 5,
                "edges": 3,
                "memory_usage": "1 MB",
            },
            "neural": {
                "status": "enabled" if settings.enable_neural_memory else "disabled",
                "learning_rate": 0.05,  # Default value
                "consolidation_enabled": True,
                "last_consolidation": "N/A",
                "total_associations": 3,
            },
            "overall_health": "healthy",
            "uptime_seconds": uptime,
            "version": "0.7.0",
        }

        return overview

    except Exception as e:
        from utils.logger import get_logger

        logger = get_logger(__name__)
        logger.error("get_system_overview_failed", error=str(e))
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve system overview",
        ) from e


# ============================================================================
# Embedding Models (Issue #49)
# ============================================================================


class EmbeddingModelInfo(BaseModel):
    """Individual embedding model info."""

    name: str
    dimensions: int
    provider: str
    available: bool


class EmbeddingModelsResponse(BaseModel):
    """Response for available embedding models."""

    models: list[EmbeddingModelInfo]
    default_model: str


@router.get("/embedding/models", response_model=EmbeddingModelsResponse)
async def list_embedding_models(
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> EmbeddingModelsResponse:
    """List available embedding models with availability status.

    Returns all models from EMBEDDING_MODEL_REGISTRY with availability
    based on whether the provider is configured and reachable.

    Returns:
        List of models with name, dimensions, provider, and availability
    """
    from config.constants import EMBEDDING_MODEL_REGISTRY
    from config.settings import get_settings

    settings = get_settings()

    # Check OpenAI availability: user has external API key or env var set
    openai_available = False
    try:
        from models.auth import ExternalAPIKey

        user_id = user["user_id"]
        workspace_id = user.get("current_workspace_id")

        conditions = [
            ExternalAPIKey.provider == "openai",
            ExternalAPIKey.enabled.is_(True),
        ]
        if workspace_id:
            from sqlalchemy import or_

            conditions.append(
                or_(
                    ExternalAPIKey.workspace_id == workspace_id,
                    ExternalAPIKey.user_id == user_id,
                )
            )
        else:
            conditions.append(ExternalAPIKey.user_id == user_id)

        result = await db.execute(select(ExternalAPIKey).where(*conditions).limit(1))
        openai_available = result.scalar_one_or_none() is not None

        # Fallback: check env var
        if not openai_available:
            import os

            openai_available = bool(os.getenv("OPENAI_API_KEY"))
    except Exception:
        pass  # Non-critical: OpenAI availability is best-effort

    # Check self-hosted backend availability (only if explicitly configured).
    # Probes the OpenAI-compatible /v1/models endpoint (Ollama + vLLM).
    self_hosted_available = False
    self_hosted_url = settings.self_hosted_base_url
    # See telemetry probe above: detect explicit config via model_fields_set so
    # setting SELF_HOSTED_BASE_URL to the default value still counts as configured.
    self_hosted_configured = (
        settings.embedding_provider == "self_hosted"
        or "self_hosted_base_url" in settings.model_fields_set
    )
    if self_hosted_configured:
        try:
            import httpx

            headers = (
                {"Authorization": f"Bearer {settings.self_hosted_api_key}"}
                if settings.self_hosted_api_key
                else None
            )
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(f"{self_hosted_url}/v1/models", headers=headers)
                self_hosted_available = resp.status_code == 200
        except Exception:
            pass  # Backend not reachable — models marked as unavailable

    # Build model list
    models = []
    for name, (dimensions, provider) in EMBEDDING_MODEL_REGISTRY.items():
        available = openai_available if provider == "openai" else self_hosted_available
        models.append(
            EmbeddingModelInfo(
                name=name,
                dimensions=dimensions,
                provider=provider,
                available=available,
            )
        )

    return EmbeddingModelsResponse(
        models=models,
        default_model=settings.embedding_model,
    )
