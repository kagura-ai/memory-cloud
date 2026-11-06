"""The five public tool handlers and their projections (#2675).

Each handler reads only rows that pass ``scope.public_rows`` and returns only
the fields listed in its projection: no ``details``, ``scope``,
``importance``, ``client``, versions, links, statistics, workspace or search
configuration. Nothing here writes — no access statistics, no graph learning,
no usage rows, no ``last_used_at`` — so an anonymous read never moves a
signal the workspace's own members rely on. The one row kept is the
embedding cost the search records for ``recall`` (``llm_call_log``).

``recall`` uses the search service the REST public search uses (the
workspace owner's id is only the embedding-key principal, reranking off) and
re-reads the returned ids through the predicate before projecting; ids the
predicate drops are dropped.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from mcp.types import TextContent
from sqlalchemy import event, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.public.limits import (
    PUBLIC_DEFAULT_EXPLORE_LIMIT,
    PUBLIC_DEFAULT_K,
    PUBLIC_DEFAULT_MAX_CHARS,
    PUBLIC_DEFAULT_TAGS_LIMIT,
    PUBLIC_MAX_EXPLORE_DEPTH,
    PUBLIC_MAX_EXPLORE_LIMIT,
    PUBLIC_MAX_FILTER_TAGS,
    PUBLIC_MAX_K,
    PUBLIC_MAX_MAX_CHARS,
    PUBLIC_MAX_MIN_COUNT,
    PUBLIC_MAX_QUERY_CHARS,
    PUBLIC_MAX_TAG_CHARS,
    PUBLIC_MAX_TAGS_LIMIT,
    PUBLIC_MIN_MAX_CHARS,
)
from mcp_server.public.publication import ServingEndpoint
from mcp_server.public.scope import public_rows
from mcp_server.tools._helpers import _dumps, _error_response
from mcp_server.tools.memory import _reference_page, _ReferenceOffsetError
from models.llm_call_log import LLMCallLog
from models.memory import Memory, NeuralMemoryEdge
from utils.datetime import to_utc_iso
from utils.logger import get_logger
from utils.response_budget import fit_items

logger = get_logger(__name__)

# Room left in ``max_chars`` for the recall envelope around the results.
_RECALL_ENVELOPE_RESERVE = 200


class PublicArgumentError(Exception):
    """An argument value the public schema does not accept (``invalid_argument``)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class PublicToolResult:
    """A handler's reply plus what the telemetry record needs."""

    content: list[TextContent]
    outcome: str
    result_count: int | None = None
    continuation: bool = False
    # #2678: the public error code when it is not in the content, and the
    # returned rows' document ids in rank order (transient; hashed downstream).
    error_code: str | None = None
    doc_ids: tuple[str, ...] = ()


def _doc_ids(values: list[str | None]) -> tuple[str, ...]:
    return tuple(v for v in values if v)


@contextlib.asynccontextmanager
async def _read_session() -> AsyncIterator[AsyncSession]:
    """One session for a public read; closed (never committed) afterwards."""
    from db.base import get_db

    async with contextlib.aclosing(get_db()) as sessions:
        async for db in sessions:
            yield db
            return


@contextlib.asynccontextmanager
async def _recall_session() -> AsyncIterator[AsyncSession]:
    """A read session that commits only the search's cost rows (``llm_call_log``).

    The search records its embedding cost on the session it is given, as on
    the REST public search, so owners' cost reports see anonymous use. Any
    other write on the session means nothing is committed.
    """
    other: list[str] = []

    def check(session: Any, *_: Any) -> None:
        other.extend(type(o).__name__ for o in (*session.dirty, *session.deleted))
        other.extend(type(o).__name__ for o in session.new if not isinstance(o, LLMCallLog))

    def dml(state: Any) -> None:
        if state.is_insert or state.is_update or state.is_delete:
            other.append("dml")

    async with _read_session() as db:
        sync = db.sync_session
        event.listen(sync, "before_flush", check)
        event.listen(sync, "do_orm_execute", dml)
        try:
            yield db
            check(sync)
            if other:
                logger.warning("public_mcp_recall_write_dropped", kinds=sorted(set(other)))
            else:
                await db.commit()
        finally:
            event.remove(sync, "before_flush", check)
            event.remove(sync, "do_orm_execute", dml)


# ============================================================================
# Argument checks
# ============================================================================


def _int_arg(args: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
    value = args.get(name)
    if value is None:
        return default
    # ``type(x) is int``: bool subclasses int and is refused.
    if type(value) is not int or not (low <= value <= high):
        raise PublicArgumentError(f"{name} must be an integer between {low} and {high}.")
    return value


def _tags_arg(args: dict[str, Any], name: str) -> list[str]:
    value = args.get(name)
    if value is None:
        return []
    if (
        type(value) is not list
        or len(value) > PUBLIC_MAX_FILTER_TAGS
        or any(type(t) is not str or len(t) > PUBLIC_MAX_TAG_CHARS for t in value)
    ):
        raise PublicArgumentError(
            f"{name} must be an array of at most {PUBLIC_MAX_FILTER_TAGS} strings of at "
            f"most {PUBLIC_MAX_TAG_CHARS} characters."
        )
    return [t.strip() for t in value if t.strip()]


def _memory_id_arg(args: dict[str, Any]) -> UUID:
    value = args.get("memory_id")
    if type(value) is not str:
        raise PublicArgumentError("memory_id is required: an entry's memory_id from recall.")
    try:
        return UUID(value)
    except ValueError:
        raise PublicArgumentError("memory_id must be a UUID.") from None


def _max_chars_arg(args: dict[str, Any]) -> int:
    return _int_arg(
        args, "max_chars", PUBLIC_DEFAULT_MAX_CHARS, PUBLIC_MIN_MAX_CHARS, PUBLIC_MAX_MAX_CHARS
    )


# ============================================================================
# Projections
# ============================================================================


def _source_url(uri: str | None) -> str | None:
    """``uri`` when it is an absolute http(s) URL, else ``None`` (omitted)."""
    if not uri:
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return uri
    return None


def _project(memory: Memory, score: float | None = None) -> dict[str, Any]:
    """The public fields of one memory (recall item with ``score`` / reference head)."""
    item: dict[str, Any] = {
        "memory_id": str(memory.id),
        "doc_id": memory.resource_doc_id,
        "summary": memory.summary,
    }
    if score is not None:
        item["score"] = round(score, 4)
    item["type"] = memory.type
    if memory.tags:
        item["tags"] = list(memory.tags)
    source_url = _source_url(memory.source_uri)
    if source_url is not None:
        item["source_url"] = source_url
    item["created_at"] = to_utc_iso(memory.created_at)
    item["updated_at"] = to_utc_iso(memory.updated_at)
    if memory.observed_at:
        item["observed_at"] = memory.observed_at
    return item


def _not_found(memory_id: UUID) -> list[TextContent]:
    return _error_response(
        "memory_not_found",
        f"Memory not found on this endpoint: {memory_id}",
        help="Use a memory_id returned by recall or explore on this endpoint.",
    )


def _success(payload: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=_dumps({"status": "success", **payload}))]


# ============================================================================
# Handlers
# ============================================================================


async def public_recall(endpoint: ServingEndpoint, args: dict[str, Any]) -> PublicToolResult:
    """Search the published data; project the rows that pass the predicate."""
    query = args.get("query")
    if type(query) is not str or not query.strip() or len(query) > PUBLIC_MAX_QUERY_CHARS:
        raise PublicArgumentError(
            f"query is required: a string of 1 to {PUBLIC_MAX_QUERY_CHARS} characters."
        )
    k = _int_arg(args, "k", PUBLIC_DEFAULT_K, 1, PUBLIC_MAX_K)
    search_mode = args.get("search_mode", "hybrid")
    if search_mode not in ("hybrid", "semantic", "keyword"):
        raise PublicArgumentError("search_mode must be one of: hybrid, semantic, keyword.")
    tags = _tags_arg(args, "tags")
    max_chars = _max_chars_arg(args)

    from services.search_service import SearchService

    degradation: dict[str, Any] = {}
    async with _recall_session() as db:
        hits = await SearchService(db).hybrid_search(
            query=query,
            # The owner is only the embedding-key principal (as REST public search).
            user_id=endpoint.owner_user_id,
            workspace_id=str(endpoint.workspace_id),
            context_id=str(endpoint.context_id),
            k=k,
            use_rerank=False,
            filters={"tags": tags, "tags_match": "all"} if tags else None,
            search_mode=search_mode,
            degradation=degradation,
        )
        scores: dict[UUID, float] = {}
        for hit in hits:
            try:
                memory_id = UUID(str(hit["id"]))
            except (KeyError, ValueError):
                continue
            scores.setdefault(memory_id, float(hit.get("score") or 0.0))
        rows: dict[UUID, Memory] = {}
        if scores:
            stmt = select(Memory).where(Memory.id.in_(list(scores)), public_rows(endpoint))
            if tags:
                stmt = stmt.where(Memory.tags.contains(tags))
            rows = {m.id: m for m in (await db.execute(stmt)).scalars()}

    results = []
    for memory_id, score in scores.items():
        memory = rows.get(memory_id)
        if memory is None:
            continue
        results.append(_project(memory, score))
        if len(results) == k:
            break
    placed = fit_items(results, max_chars - _RECALL_ENVELOPE_RESERVE)
    if results and placed == 0:
        placed = 1  # a reply always carries the best result
    payload: dict[str, Any] = {"count": placed, "results": results[:placed]}
    if placed < len(results):
        payload["truncated"] = True
    degraded = bool(degradation.get("degraded"))
    if degraded:
        payload["degraded"] = True
        if degradation.get("reason"):
            payload["degraded_reason"] = str(degradation["reason"])
    return PublicToolResult(
        _success(payload),
        "degraded" if degraded else "success",
        result_count=placed,
        doc_ids=_doc_ids([r["doc_id"] for r in results[:placed]]),
    )


async def public_reference(endpoint: ServingEndpoint, args: dict[str, Any]) -> PublicToolResult:
    """Read one public entry, paging its content."""
    memory_id = _memory_id_arg(args)
    offset = args.get("content_offset")
    if offset is None:
        offset = 0
    elif type(offset) is not int or offset < 0:
        raise PublicArgumentError("content_offset must be a non-negative integer.")
    max_chars = _max_chars_arg(args)

    async with _read_session() as db:
        memory = (
            await db.execute(select(Memory).where(Memory.id == memory_id, public_rows(endpoint)))
        ).scalar_one_or_none()
    if memory is None:
        return PublicToolResult(_not_found(memory_id), "invalid_request", result_count=0)
    head = _project(memory)
    remaining = max_chars - len(_dumps({"status": "success", "memory": head}))
    try:
        page = _reference_page(
            "content", memory.content or "", offset, remaining, progress=offset > 0
        )
    except _ReferenceOffsetError as exc:
        return PublicToolResult(exc.to_response(), "invalid_request", result_count=0)
    return PublicToolResult(
        _success({"memory": {**head, **page}}),
        "success",
        result_count=1,
        continuation=offset > 0,
        doc_ids=_doc_ids([memory.resource_doc_id]),
    )


@dataclass(frozen=True)
class _Neighbour:
    memory_id: UUID
    doc_id: str | None
    summary: str
    edge_type: str


async def _neighbours(
    db: AsyncSession,
    endpoint: ServingEndpoint,
    frontier: list[UUID],
    exclude: list[UUID],
    limit: int,
) -> list[_Neighbour]:
    """Public neighbours of ``frontier``, one row per neighbour, strongest first.

    Edges are those of the endpoint's workspace and context; the neighbour row
    must pass ``public_rows``. A neighbour reached by several edges (one per
    member who touched the pair, or several edge types) is one row: its
    strongest edge orders it and names its ``edge_type``. Up to ``limit + 1``
    rows, so the caller can tell whether more matched.
    """
    e = NeuralMemoryEdge
    in_scope = (e.workspace_id == endpoint.workspace_id, e.context_id == endpoint.context_id)
    outgoing = select(
        e.dst_id.label("nid"), e.weight.label("weight"), e.edge_type.label("edge_type")
    ).where(*in_scope, e.src_id.in_(frontier))
    incoming = select(
        e.src_id.label("nid"), e.weight.label("weight"), e.edge_type.label("edge_type")
    ).where(*in_scope, e.dst_id.in_(frontier))
    pairs = union_all(outgoing, incoming).subquery()
    ranked = (
        select(
            pairs.c.nid,
            pairs.c.weight,
            pairs.c.edge_type,
            func.row_number()
            .over(partition_by=pairs.c.nid, order_by=(pairs.c.weight.desc(), pairs.c.edge_type))
            .label("rn"),
        )
        .where(pairs.c.nid.not_in(exclude))
        .subquery()
    )
    stmt = (
        select(Memory.id, Memory.resource_doc_id, Memory.summary, ranked.c.edge_type)
        .join(ranked, ranked.c.nid == Memory.id)
        .where(ranked.c.rn == 1, public_rows(endpoint))
        .order_by(ranked.c.weight.desc(), Memory.id)
        .limit(limit + 1)
    )
    return [
        _Neighbour(row.id, row.resource_doc_id, row.summary, str(row.edge_type))
        for row in (await db.execute(stmt)).all()
    ]


async def public_explore(endpoint: ServingEndpoint, args: dict[str, Any]) -> PublicToolResult:
    """Entries related to one public entry, within the public rows only.

    Hop 2 starts only from hop-1 entries that passed the predicate, so a
    non-public entry never relays the walk.
    """
    memory_id = _memory_id_arg(args)
    depth = _int_arg(args, "depth", 1, 1, PUBLIC_MAX_EXPLORE_DEPTH)
    limit = _int_arg(args, "limit", PUBLIC_DEFAULT_EXPLORE_LIMIT, 1, PUBLIC_MAX_EXPLORE_LIMIT)

    async with _read_session() as db:
        seed = (
            await db.execute(
                select(Memory.id, Memory.resource_doc_id, Memory.summary).where(
                    Memory.id == memory_id, public_rows(endpoint)
                )
            )
        ).first()
        if seed is None:
            return PublicToolResult(_not_found(memory_id), "invalid_request", result_count=0)
        related: list[tuple[_Neighbour, int]] = []
        hop1 = await _neighbours(db, endpoint, [seed.id], [seed.id], limit)
        truncated = len(hop1) > limit
        hop1 = hop1[:limit]
        related.extend((n, 1) for n in hop1)
        remaining = limit - len(hop1)
        if depth >= 2 and hop1 and remaining > 0:
            frontier = [n.memory_id for n in hop1]
            hop2 = await _neighbours(db, endpoint, frontier, [seed.id, *frontier], remaining)
            truncated = truncated or len(hop2) > remaining
            related.extend((n, 2) for n in hop2[:remaining])

    exploration: dict[str, Any] = {
        "seed": {
            "memory_id": str(seed.id),
            "doc_id": seed.resource_doc_id,
            "summary": seed.summary,
        },
        "related": [
            {
                "memory_id": str(n.memory_id),
                "doc_id": n.doc_id,
                "summary": n.summary,
                "hop": hop,
                "edge_type": n.edge_type,
            }
            for n, hop in related
        ],
        "returned": len(related),
    }
    if truncated:
        exploration["truncated"] = True
    return PublicToolResult(
        _success({"exploration": exploration}),
        "success",
        result_count=len(related),
        doc_ids=_doc_ids([seed.resource_doc_id, *(n.doc_id for n, _ in related)]),
    )


async def public_list_tags(endpoint: ServingEndpoint, args: dict[str, Any]) -> PublicToolResult:
    """The tags of the public rows with counts."""
    limit = _int_arg(args, "limit", PUBLIC_DEFAULT_TAGS_LIMIT, 1, PUBLIC_MAX_TAGS_LIMIT)
    min_count = _int_arg(args, "min_count", 1, 1, PUBLIC_MAX_MIN_COUNT)
    sort = args.get("sort", "count")
    if sort not in ("count", "recent", "alpha"):
        raise PublicArgumentError("sort must be one of: count, recent, alpha.")
    prefix = args.get("prefix", "")
    if type(prefix) is not str or len(prefix) > PUBLIC_MAX_TAG_CHARS:
        raise PublicArgumentError(
            f"prefix must be a string of at most {PUBLIC_MAX_TAG_CHARS} characters."
        )
    with_tags = _tags_arg(args, "with_tags")

    from services.context_service import aggregate_context_tags

    async with _read_session() as db:
        # The same three columns as ``public_rows``: workspace, context and the
        # resource slug, non-deleted rows only.
        rows = await aggregate_context_tags(
            db,
            workspace_id=endpoint.workspace_id,
            context_id=endpoint.context_id,
            limit=limit + 1,
            min_count=min_count,
            sort=sort,
            prefix=prefix,
            q=None,
            with_tags=with_tags,
            resource_id=endpoint.resource_id,
        )
    tags = [{"tag": row["tag"], "count": row["count"]} for row in rows]
    has_more = len(tags) > limit
    tags = tags[:limit]
    return PublicToolResult(
        _success({"tags": tags, "total": len(tags), "has_more": has_more}),
        "success",
        result_count=len(tags),
    )


async def public_get_context_info(
    endpoint: ServingEndpoint, args: dict[str, Any]
) -> PublicToolResult:
    """The approved public description of the dataset, nothing internal."""
    described: dict[str, Any] = {"public_id": endpoint.public_id, "name": endpoint.public_name}
    if endpoint.public_description:
        described["description"] = endpoint.public_description
    if endpoint.public_usage_guide:
        described["usage_guide"] = endpoint.public_usage_guide
    return PublicToolResult(
        _success(
            {
                "endpoint": described,
                "resource": {"resource_id": endpoint.resource_id},
                "context": {"id": str(endpoint.context_id), "name": endpoint.context_name},
            }
        ),
        "success",
    )


PUBLIC_HANDLERS = {
    "recall": public_recall,
    "reference": public_reference,
    "explore": public_explore,
    "list_tags": public_list_tags,
    "get_context_info": public_get_context_info,
}
