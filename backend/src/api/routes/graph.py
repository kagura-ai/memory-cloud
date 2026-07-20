"""Graph Memory API Routes - Neural Memory Statistics and Exploration.

Provides endpoints for Neural Memory graph statistics and visualization.
Issue #46 Phase 5 - Rich Memory Overview
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from auth.workspace_roles import WorkspaceRole
from db.base import get_db
from models.memory import Memory
from services.edge_service import VALID_EDGE_TYPES, create_declared_edge
from services.graph_service import GraphService
from services.permission_service import PermissionService
from utils.datetime import to_utc_iso, utcnow
from utils.exceptions import (
    ConflictError,
    InternalError,
    MemoryCloudException,
    NotFoundException,
    ValidationError,
)
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
    """Graph edge for visualization.

    `created_at` and `confidence` are surfaced for downstream UI consumers
    (#430 — graph edge metadata overlay, future #435). They were already
    on the underlying `NeuralMemoryEdge` row but previously stripped at
    this response model. Both stay optional so existing API clients that
    don't read the new fields continue to deserialize unchanged.
    """

    source: str
    target: str
    weight: float
    type: str
    created_at: str | None = None
    confidence: float | None = None


class GraphDataResponse(BaseModel):
    """Graph data for visualization."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    stats: dict


class CreateEdgeRequest(BaseModel):
    """Body for ``POST /graph/edges`` (#1416).

    Mirrors the MCP ``create_edge`` tool's inputs (edge_type default,
    weight/confidence ranges, overwrite semantics) so the two transports stay
    contract-consistent. ``context_id`` scopes the write; both endpoints must be
    live memories in that context (enforced by the route).
    """

    context_id: UUID
    source_id: UUID
    target_id: UUID
    edge_type: str = "related_to"
    # Same ranges as the MCP tool: weight 0–3 (declared assertion defaults to
    # full 1.0), confidence 0–1.
    weight: float = Field(default=1.0, ge=0.0, le=3.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    # Opt-in to re-asserting an existing declared edge with different values.
    overwrite: bool = False

    @field_validator("edge_type")
    @classmethod
    def _validate_edge_type(cls, v: str) -> str:
        if v not in VALID_EDGE_TYPES:
            raise ValueError(f"edge_type must be one of: {', '.join(sorted(VALID_EDGE_TYPES))}")
        return v


class CreateEdgeResponse(BaseModel):
    """Response for ``POST /graph/edges``.

    ``operation`` is one of created / updated / unchanged (conflict surfaces as a
    409, never a 200 body). ``edge`` is the serialized post-state; ``previous``
    is the pre-image when an existing non-protected edge was upserted.
    """

    operation: str
    edge: dict | None
    previous: dict | None = None


# ============================================================================
# Endpoints
# ============================================================================


@router.get("/stats", response_model=GraphStatsResponse)
async def get_graph_stats(
    user: SessionUser,
    context_id: UUID | None = Query(
        None,
        description="Optional context ID. When absent, returns an empty graph "
        "(no scope = no visible edges) per Issue #383.",
    ),
    db: AsyncSession = Depends(get_db),
):
    """Get Neural Memory graph statistics for specified context.

    Issue #82: Context-scoped stats.
    Issue #383: Authorization is delegated to ``PermissionService``. Previously
    this endpoint hard-filtered by ``user_id == caller``, hiding shared-context
    memories authored by other workspace members. Now:

        - Shared context (``is_private = false``): any workspace member sees
          the full graph for the context.
        - Private context (``is_private = true``): only the private-context
          creator can access the graph for that context. The creator-only
          rule matches ``can_access_memory`` (returns ``False`` for
          non-creators) and ``check_context_access`` (raises 403 for
          non-creators). This endpoint deliberately **remaps that denial
          to 404**, unifying with the cross-workspace-probe shape so
          neither path leaks existence — an intentional divergence from
          ``check_context_access``'s 403 status code, not from its
          authorization semantics.
        - Cross-workspace probes and private-context access by non-creators
          both surface as 404 (CWE-639 / OWASP A01 uniform disclosure)
          to avoid existence leakage.

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
                last_updated=to_utc_iso(utcnow()) or "",
            )

        context = await PermissionService(db).resolve_context_for_workspace_read(
            user_id=user_id, context_id=context_id
        )
        workspace_id = str(context.workspace_id)
        str_context_id = str(context.id)
        owner_filter = user_id if context.is_private else None

        graph_service = GraphService(
            user_id, db, workspace_id=workspace_id, context_id=str_context_id
        )
        stats = await graph_service.stats(owner_filter=owner_filter)

        top_nodes_data = await graph_service.edge_repo.get_top_connected_nodes(
            user_id=owner_filter,
            limit=10,
            workspace_id=workspace_id,
            context_id=str_context_id,
        )

        # Fetch memory summaries for top nodes.
        # Defense in depth against a stale/corrupt edge row that references a
        # Memory outside the resolved (workspace_id, context_id): re-scope the
        # Memory lookup itself so cross-context metadata cannot leak via summaries
        # even if the edge invariant is violated. Follow-up AC 6 enforces the
        # invariant at write time; this keeps the read path safe in the meantime.
        top_node_ids = [node_id for node_id, _ in top_nodes_data]
        memories_result = await db.execute(
            select(Memory).where(
                Memory.id.in_(top_node_ids),
                Memory.workspace_id == context.workspace_id,
                Memory.context_id == context.id,
                Memory.deleted_at.is_(None),
            )
        )
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
            last_updated=to_utc_iso(utcnow()) or "",
        )

    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error("graph_stats_failed", error=str(e))
        raise InternalError("Failed to retrieve graph statistics") from e


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

        context = await PermissionService(db).resolve_context_for_workspace_read(
            user_id=user_id, context_id=context_id
        )
        workspace_id = str(context.workspace_id)
        str_context_id = str(context.id)
        owner_filter = user_id if context.is_private else None

        graph_service = GraphService(
            user_id, db, workspace_id=workspace_id, context_id=str_context_id
        )

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

        # Fetch memories for all nodes — scoped to (workspace_id, context_id)
        # for the same defense-in-depth reason as /graph/stats: prevents stale
        # or invariant-violating edge rows from leaking cross-context memory
        # metadata even before AC 6 (edge invariant) enforcement lands.
        memories_result = await db.execute(
            select(Memory).where(
                Memory.id.in_(list(all_node_ids)),
                Memory.workspace_id == context.workspace_id,
                Memory.context_id == context.id,
                Memory.deleted_at.is_(None),
            )
        )
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
            # Same post-Memory-scope stats as the main path below — counting
            # edges that reference out-of-scope/deleted memories would report
            # "edges exist" when nothing is actually renderable.
            visible_node_ids = set(memories_map.keys())
            visible_edges = [
                edge
                for edge in all_edges
                if str(edge.src_id) in visible_node_ids and str(edge.dst_id) in visible_node_ids
            ]
            return GraphDataResponse(
                nodes=[],
                edges=[],
                stats={
                    "total_nodes": len(visible_node_ids),
                    "total_edges": len(visible_edges),
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
                    created_at=to_utc_iso(memory.created_at),
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
                        created_at=to_utc_iso(edge.created_at),
                        confidence=edge.confidence,
                    )
                )

        # Base totals on the post-Memory-scope set so they stay consistent
        # with what the UI can actually render. A stale edge whose src/dst
        # Memory has been soft-deleted or has drifted out of the resolved
        # (workspace_id, context_id) scope is counted in ``all_edges`` but
        # cannot be rendered — including it in ``total_edges`` would produce
        # a "10 edges, 0 rendered" mismatch that confuses the viewer.
        visible_node_ids = set(memories_map.keys())
        visible_edges = [
            edge
            for edge in all_edges
            if str(edge.src_id) in visible_node_ids and str(edge.dst_id) in visible_node_ids
        ]
        # total_* = post-Memory-scope; filtered_* = post-limit/post-memory_types cut.
        stats = {
            "total_nodes": len(visible_node_ids),
            "total_edges": len(visible_edges),
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

    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error("graph_data_failed", error=str(e), user_id=user.get("user_id"))
        raise InternalError("Failed to retrieve graph data") from e


@router.post("/edges", response_model=CreateEdgeResponse)
async def create_graph_edge(
    body: CreateEdgeRequest,
    user: SessionUser,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Create a user-declared edge between two memories in a context (#1416).

    REST twin of the MCP ``create_edge`` tool: both call the shared
    ``services.edge_service.create_declared_edge`` so the deterministic
    declared-duplicate contract (#1321) and the #1403 supersede accept/self-heal
    behave identically. The web UI's confirm→create flow for a supersede
    suggestion posts here with ``edge_type="supersedes"``; accepting a stored
    suggestion clears the ``supersede_candidate`` in the same transaction.

    Authorization: mirrors the MCP ``create_edge`` tool and the REST
    ``remember`` / ``patch`` writers — workspace membership at ``member`` level
    or higher (viewers are read-only). Resolution goes through
    ``resolve_context_for_workspace_read``, which returns a **uniform 404** on
    every deny path (context not found, caller not a workspace member, viewer,
    private-context non-creator, whitelist/agent-binding miss) so context
    existence never leaks across workspace/privacy boundaries (CWE-639 / OWASP
    A01). Both endpoints must be live memories in the resolved (workspace,
    context) — a missing/out-of-scope/deleted endpoint is a 404, which also
    closes the edge write-time invariant the ``/graph`` read paths defend
    against.

    Status: 201 when a new edge is created; 200 for updated / unchanged; 409
    when a differing declared edge already exists (unless ``overwrite=true``).
    """
    try:
        user_id = user["user_id"]

        if body.source_id == body.target_id:
            raise ValidationError(
                "source_id and target_id must be different (self-loops are not supported).",
                field="target_id",
            )

        # Write authz: member-or-higher workspace role (blocks read-only viewers),
        # matching the MCP create_edge / remember / patch access model rather than
        # the stricter context-role EDITOR gate. resolve_context_for_workspace_read
        # collapses every deny reason to a uniform 404 (no existence leak).
        context = await PermissionService(db).resolve_context_for_workspace_read(
            user_id,
            body.context_id,
            required_role=WorkspaceRole.MEMBER,
        )

        # Context invariant: both endpoints must be live memories in this
        # (workspace, context). Prevents forging an edge to an out-of-scope
        # memory and keeps the graph read paths' defense-in-depth honest by
        # enforcing the invariant at write time.
        found = await db.execute(
            select(Memory.id).where(
                Memory.id.in_([body.source_id, body.target_id]),
                Memory.workspace_id == context.workspace_id,
                Memory.context_id == context.id,
                Memory.deleted_at.is_(None),
            )
        )
        found_ids = {row[0] for row in found.all()}
        if body.source_id not in found_ids or body.target_id not in found_ids:
            raise NotFoundException("Source or target memory in this context")

        result = await create_declared_edge(
            db,
            user_id=user_id,
            source_id=body.source_id,
            target_id=body.target_id,
            edge_type=body.edge_type,
            weight=body.weight,
            confidence=body.confidence,
            workspace_id=str(context.workspace_id),
            context_id=str(context.id),
            overwrite=body.overwrite,
        )

        if result.operation == "conflict":
            await db.rollback()
            raise ConflictError(
                "A declared edge already exists between these memories with different "
                "values. Pass overwrite=true to re-assert it.",
                existing_edge=result.existing,
            )

        await db.commit()

        # 201 for a genuine create; 200 for an idempotent re-assert (unchanged)
        # or an upsert over a non-declared auto-edge (updated).
        response.status_code = 201 if result.operation == "created" else 200
        return CreateEdgeResponse(
            operation=result.operation, edge=result.edge, previous=result.previous
        )

    except (HTTPException, MemoryCloudException):
        raise
    except Exception as e:
        logger.error("graph_edge_create_failed", error=str(e), user_id=user.get("user_id"))
        raise InternalError("Failed to create edge") from e
