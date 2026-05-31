"""Resource event quota — Redis-backed per-hour rate limiting.

Issue #332: Shared between HTTP (``api/routes/resource_ingest``) and MCP
(``mcp_server/tools/resource``) ingest paths so the per-hour ceiling is
enforced regardless of entry point. Both paths INCR the same Redis key so
their counts are combined against the same ceiling.

Key format: ``resource:events:{resource_id}:{workspace_id}:hour``

- Workspace-scoped (not token-scoped) so HTTP and MCP requests against the
  same resource share one counter (Issue #332 design decision).
- 1-hour TTL, set only on first INCR (matches ``db.redis.incrby_counter``).
- Fail-open on Redis backend errors — Redis outage MUST NOT block ingest
  (see SECURITY.md "Rate Limiting").
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.redis import get_cache, incrby_counter
from utils.exceptions import RateLimitError, RedisError
from utils.logger import get_logger

logger = get_logger(__name__)

# Fallback ceiling for MCP ingest when the workspace has no ResourceToken to
# derive a quota from (e.g. resource was created without tokens). Matches
# ``ResourceToken.quota_events_per_hour`` default so behaviour is consistent.
MCP_INGEST_DEFAULT_QUOTA_PER_HOUR = 1000

_TTL_SECONDS = 3600


def _build_key(resource_id: str, workspace_id: UUID) -> str:
    return f"resource:events:{resource_id}:{workspace_id}:hour"


async def check_event_quota(
    resource_id: str,
    workspace_id: UUID,
    quota_per_hour: int,
    count: int = 1,
) -> None:
    """Reserve ``count`` units against the per-hour event ceiling.

    Args:
        resource_id: Resource slug being ingested into.
        workspace_id: Owning workspace; combined with ``resource_id`` forms
            the Redis counter key. Both HTTP and MCP paths share this key.
        quota_per_hour: Maximum events allowed per hour. Values <= 0 disable
            the check (skip Redis round-trip).
        count: Number of events to reserve in one shot. Batch ingest passes
            ``len(events)`` so batching cannot bypass the ceiling.

    Raises:
        RateLimitError: If reserving ``count`` would exceed ``quota_per_hour``.
            Status 429, ``retry_after=3600``.

    Fail-open: ``RedisError`` (backend outage) is logged and swallowed so
    ingest is not blocked. Other exceptions propagate.
    """
    if quota_per_hour <= 0:
        return

    if count <= 0:
        # Defensive: a non-positive count would decrement the Redis counter via
        # INCRBY and could silently bypass the per-hour ceiling. This is never
        # valid input from callers (HTTP + MCP both pass len(events) >= 1).
        raise ValueError(f"count must be >= 1, got {count}")

    key = _build_key(resource_id, workspace_id)

    try:
        current_str = await get_cache(key)
        current = int(current_str) if current_str else 0

        if current + count > quota_per_hour:
            logger.warning(
                "resource_event_quota_exceeded",
                resource_id=resource_id,
                workspace_id=str(workspace_id),
                current=current,
                quota=quota_per_hour,
                requested=count,
            )
            raise RateLimitError(
                message=(f"Event quota exceeded: {current}/{quota_per_hour} events per hour"),
                retry_after=_TTL_SECONDS,
            )

        new_count = await incrby_counter(key, count, ttl=_TTL_SECONDS)
        logger.debug(
            "resource_event_quota_checked",
            resource_id=resource_id,
            workspace_id=str(workspace_id),
            previous=current,
            reserved=count,
            new_total=new_count,
            quota=quota_per_hour,
        )
    except RateLimitError:
        raise
    except RedisError as e:
        logger.error("redis_quota_check_failed", error=str(e), key=key)
        return


async def resolve_workspace_event_quota_per_hour(
    db: AsyncSession,
    workspace_id: UUID,
    resource_id: str,
    *,
    resource_pk: UUID | None = None,
) -> int:
    """Derive the effective per-hour event quota for an MCP ingest call.

    MCP authenticates via session user + workspace, not via ResourceToken,
    so it has no per-token ``quota_events_per_hour`` to read. We use the
    maximum across the workspace's active tokens **for this resource_pk** —
    ensuring an MCP caller cannot exceed the most permissive token configured
    for the resource being ingested into. The Redis key still carries the
    human-readable slug for external compatibility, but the DB quota lookup
    filters by ``resource_pk`` + ``workspace_id`` so slug reuse cannot let a
    high-quota token on another tenant's historical resource relax this cap.

    Falls back to ``MCP_INGEST_DEFAULT_QUOTA_PER_HOUR`` when the resource has
    no token in this workspace (e.g. resource created without tokens, or
    pre-#324 legacy tokens with ``workspace_id`` not yet backfilled).
    """
    from models.resource import Resource, ResourceToken

    if resource_pk is None:
        resource_pk = (
            await db.execute(
                select(Resource.id).where(
                    Resource.workspace_id == workspace_id,
                    Resource.resource_id == resource_id,
                )
            )
        ).scalar_one_or_none()
        if resource_pk is None:
            return MCP_INGEST_DEFAULT_QUOTA_PER_HOUR

    result = await db.execute(
        select(func.max(ResourceToken.quota_events_per_hour)).where(
            ResourceToken.workspace_id == workspace_id,
            ResourceToken.resource_pk == resource_pk,
            ResourceToken.is_active.is_(True),
        )
    )
    max_quota = result.scalar_one_or_none()
    # ``is None`` (not truthy) so an explicit 0 from an operator stays as 0
    # — check_event_quota treats <= 0 as "disabled".
    return int(max_quota) if max_quota is not None else MCP_INGEST_DEFAULT_QUOTA_PER_HOUR
