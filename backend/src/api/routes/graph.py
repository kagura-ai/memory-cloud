"""Graph Memory API Routes - Neural Memory Statistics and Exploration.

Provides endpoints for Neural Memory graph statistics and visualization.
Issue #46 Phase 5 - Rich Memory Overview
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from db.base import get_db
from models.auth import Context
from models.memory import Memory
from services.context_service import ContextService
from services.graph_service import GraphService
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/graph", tags=["graph"])


# ============================================================================
# Response Models
# ============================================================================


class GraphStats(BaseModel):
    """Graph statistics response."""

    total_nodes: int
    total_edges: int
    avg_edge_weight: float
    max_edge_weight: float
    min_edge_weight: float
    density: float
    top_connections: list[dict]  # Top 10 most connected nodes
    recent_edges: list[dict]  # Recently strengthened edges


class GraphStatsResponse(BaseModel):
    """Graph stats API response."""

    user_id: str
    stats: GraphStats
    last_updated: str


class GraphNode(BaseModel):
    """Graph node for visualization."""

    id: str
    summary: str
    type: str
    importance: float
    degree: int
    created_at: str | None = None


class GraphEdge(BaseModel):
    """Graph edge for visualization."""

    source: str
    target: str
    weight: float
    type: str


class GraphDataResponse(BaseModel):
    """Graph data for visualization."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: dict


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/stats", response_model=GraphStatsResponse)
async def get_graph_stats(
    user: SessionUser,
    context_id: UUID | None = Query(
        None, description="Optional context ID (defaults to current context)"
    ),
    db: AsyncSession = Depends(get_db),
    context_service: ContextService = Depends(lambda db=Depends(get_db): ContextService(db)),
):
    """Get Neural Memory graph statistics for specified context.

    Issue #82: Context-scoped stats.
    Issue #383: Authorization is delegated to ``PermissionService``. Previously
    this endpoint hard-filtered by ``user_id == caller``, hiding shared-context
    memories authored by other workspace members. Now:

        - Shared context (``is_private = false``): any workspace member sees
          the full graph for the context.
        - Private context (``is_private = true``): only the creator of each
          edge/memory sees their own subgraph.
        - Cross-workspace probes surface as 404 (CWE-639 / OWASP A01 uniform
          disclosure) rather than 403 to avoid existence leakage.

    Args:
        context_id: Optional context ID. When absent, returns an empty graph
            (no scope = no visible edges).

    Returns:
        Graph statistics including node/edge counts, top connections, etc.
    """
    try:
        user_id = user["user_id"]

        # Without context scope there is no visible graph (Issue #383).
        if not context_id:
            return GraphStatsResponse(
                user_id=user_id,
                stats=GraphStats(
                    total_nodes=0,
                    total_edges=0,
                    avg_edge_weight=0.0,
                    max_edge_weight=0.0,
                    min_edge_weight=0.0,
                    density=0.0,
                    top_connections=[],
                    recent_edges=[],
                ),
                last_updated=utcnow().isoformat(),
            )

        # Resolve context → workspace + privacy. Unified 404 on miss/forbidden.
        context_result = await db.execute(select(Context).where(Context.id == context_id))
        context = context_result.scalar_one_or_none()
        if not context:
            raise HTTPException(status_code=404, detail=f"Context {context_id} not found")

        from services.permission_service import PermissionService

        perm_service = PermissionService(db)
        try:
            await perm_service.check_workspace_access(
                user_id=user_id,
                workspace_id=context.workspace_id,
                required_role="member",
            )
        except HTTPException:
            # CWE-639 uniform disclosure: cross-workspace probes look identical
            # to non-existent contexts. Do NOT reveal workspace membership state.
            raise HTTPException(status_code=404, detail=f"Context {context_id} not found") from None

        workspace_id = str(context.workspace_id)
        str_context_id = str(context.id)

        # Issue #383: visibility-aware creator filter.
        # - private context: only the caller's own edges are visible
        # - shared context: all workspace members' edges are visible (no filter)
        owner_filter = user_id if context.is_private else None

        # SQL backend: edges are in neural_memory_edges table (Issue #84)
        graph_service = GraphService(
            user_id, db, workspace_id=workspace_id, context_id=str_context_id
        )
        # Get basic stats with 3-level isolation (Issue #383 visibility filter)
        stats = await graph_service.stats(owner_filter=owner_filter)

        # Get top connected nodes (by degree) - SQL backend with isolation (#383)
        top_nodes_data = await graph_service.edge_repo.get_top_connected_nodes(
            user_id=owner_filter,
            limit=10,
            workspace_id=workspace_id,
            context_id=str_context_id,
        )

        # Fetch memory summaries for top nodes

        from models.memory import Memory

        top_node_ids = [node_id for node_id, _ in top_nodes_data]
        memories_result = await db.execute(select(Memory).where(Memory.id.in_(top_node_ids)))
        memories_map = {str(m.id): m for m in memories_result.scalars().all()}

        top_connections = []
        for node_id, degree in top_nodes_data:
            memory = memories_map.get(str(node_id))
            top_connections.append(
                {
                    "node_id": str(node_id),
                    "summary": memory.summary if memory else str(node_id)[:16],
                    "type": memory.type if memory else "unknown",
                    "degree": int(degree),
                    "edge_count": int(degree),
                }
            )

        # Get recent edges (TODO: track edge creation timestamps)
        recent_edges = []

        return GraphStatsResponse(
            user_id=user_id,
            stats=GraphStats(
                total_nodes=stats["total_nodes"],
                total_edges=stats["total_edges"],
                avg_edge_weight=stats["avg_edge_weight"],
                max_edge_weight=stats["max_edge_weight"],
                min_edge_weight=stats.get("min_edge_weight", 0.0),
                density=stats.get("density", 0.0),
                top_connections=top_connections,
                recent_edges=recent_edges,
            ),
            last_updated=utcnow().isoformat(),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph_stats_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve graph statistics",
        ) from e


@router.get("/data", response_model=GraphDataResponse)
async def get_graph_data(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
    context_id: UUID = Query(..., description="Context ID (required for isolation)"),
    limit_nodes: int = Query(100, description="Maximum number of nodes to return", ge=1, le=500),
    min_weight: float = Query(0.0, description="Minimum edge weight to include", ge=0.0, le=3.0),
    memory_types: list[str] | None = Query(
        None, description="Filter by memory types (e.g., code, note)"
    ),
):
    """Get Neural Memory graph data for visualization (context-scoped).

    Args:
        context_id: Context ID for scoping (required for proper isolation)
        limit_nodes: Maximum nodes to return (default: 100, max: 500)
        min_weight: Minimum edge weight threshold
        memory_types: Optional list of memory types to include

    Returns:
        Graph data with nodes and edges
    """
    try:
        user_id = user["user_id"]

        # Resolve context → workspace + privacy. Unified 404 on miss/forbidden.
        context_result = await db.execute(select(Context).where(Context.id == context_id))
        context = context_result.scalar_one_or_none()
        if not context:
            raise HTTPException(status_code=404, detail=f"Context {context_id} not found")

        from services.permission_service import PermissionService

        perm_service = PermissionService(db)
        try:
            await perm_service.check_workspace_access(
                user_id=user_id,
                workspace_id=context.workspace_id,
                required_role="member",
            )
        except HTTPException:
            # CWE-639 uniform disclosure (Issue #383): cross-workspace probes
            # surface as 404, not 403, to avoid leaking workspace membership.
            raise HTTPException(status_code=404, detail=f"Context {context_id} not found") from None

        workspace_id = str(context.workspace_id)
        str_context_id = str(context.id)

        # Issue #383: visibility-aware creator filter.
        owner_filter = user_id if context.is_private else None

        # SQL backend: Load graph (Issue #84)
        graph_service = GraphService(
            user_id, db, workspace_id=workspace_id, context_id=str_context_id
        )

        # Get all edges with 3-level isolation (Issue #383 visibility filter)
        all_edges = await graph_service.edge_repo.get_all_edges(
            user_id=owner_filter,
            min_weight=min_weight,
            workspace_id=workspace_id,
            context_id=str_context_id,
        )

        # Extract unique node IDs from edges
        all_node_ids = set()
        for edge in all_edges:
            all_node_ids.add(edge.src_id)
            all_node_ids.add(edge.dst_id)

        # Fetch memories for all nodes
        memories_result = await db.execute(select(Memory).where(Memory.id.in_(list(all_node_ids))))
        memories_map = {str(m.id): m for m in memories_result.scalars().all()}

        # Calculate node degrees from edges
        node_degrees = {}
        for edge in all_edges:
            src_str = str(edge.src_id)
            dst_str = str(edge.dst_id)
            node_degrees[src_str] = node_degrees.get(src_str, 0) + 1
            node_degrees[dst_str] = node_degrees.get(dst_str, 0) + 1

        # Build node list with scores
        node_scores = []
        for node_id_str, memory in memories_map.items():
            if memory_types and memory.type not in memory_types:
                continue

            degree = node_degrees.get(node_id_str, 0)
            score = memory.importance * (1 + degree)
            node_scores.append((node_id_str, score, degree, memory))

        if not node_scores:
            stats = await graph_service.stats(owner_filter=owner_filter)
            return GraphDataResponse(
                nodes=[],
                edges=[],
                stats={
                    "total_nodes": stats["total_nodes"],
                    "total_edges": stats["total_edges"],
                    "filtered_nodes": 0,
                    "filtered_edges": 0,
                },
            )

        # Sort by score (importance × connectivity)
        node_scores.sort(key=lambda x: x[1], reverse=True)

        # Simplified: Return top nodes (BFS optimization deferred to Phase 1.5.1)
        included_node_ids = set()
        nodes = []

        # Add top scored nodes up to limit
        for node_id_str, _score, degree, memory in node_scores[:limit_nodes]:
            nodes.append(
                GraphNode(
                    id=node_id_str,
                    summary=memory.summary,
                    type=memory.type,
                    importance=memory.importance,
                    degree=int(degree),
                    created_at=memory.created_at.isoformat() if memory.created_at else None,
                )
            )
            included_node_ids.add(node_id_str)

        # Build edge list (SQL backend)
        edges = []
        for edge in all_edges:
            source_str = str(edge.src_id)
            target_str = str(edge.dst_id)

            # Include only edges between included nodes
            if source_str in included_node_ids and target_str in included_node_ids:
                edges.append(
                    GraphEdge(
                        source=source_str,
                        target=target_str,
                        weight=edge.weight,
                        type=edge.edge_type,
                    )
                )

        # Get statistics (Issue #383 visibility filter)
        graph_stats = await graph_service.stats(owner_filter=owner_filter)
        stats = {
            "total_nodes": graph_stats["total_nodes"],
            "total_edges": graph_stats["total_edges"],
            "filtered_nodes": len(nodes),
            "filtered_edges": len(edges),
        }

        logger.info(
            "graph_data_retrieved",
            user_id=user_id,
            nodes=len(nodes),
            edges=len(edges),
            filters={
                "limit_nodes": limit_nodes,
                "min_weight": min_weight,
                "memory_types": memory_types,
            },
        )

        return GraphDataResponse(
            nodes=nodes,
            edges=edges,
            stats=stats,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("graph_data_failed", error=str(e), user_id=user.get("user_id"))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve graph data",
        ) from e
