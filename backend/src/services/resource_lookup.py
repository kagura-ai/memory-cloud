"""Resource entity lookup helpers (Issue #390 Phase A).

Shared chokepoint for resolving ``resources.id`` (UUID primary key) from
a workspace-scoped ``(workspace_id, resource_id)`` tuple. Every writer
and reader of the satellite tables (``resource_events``,
``resource_schemas``, ``indexer_state``, ``resource_tokens``) that needs
to populate or filter by ``resource_pk`` routes through this helper, so
the "slug + workspace → UUID" resolution lives in exactly one place.

The reference read-path implementation in
``services/resource_indexer.py:get_indexer_status_for_context`` already
follows this pattern; this module extracts the step into a reusable
helper and exposes the ``workspace_id + resource_id`` tuple form that
callers without a pre-resolved ``Context`` need.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.resource import Resource, ResourceSchema


async def resolve_resource_pk(
    db: AsyncSession,
    workspace_id: UUID,
    resource_id: str,
) -> UUID | None:
    """Resolve ``resources.id`` for a workspace-scoped slug.

    Args:
        db: Async DB session.
        workspace_id: Owning workspace UUID.
        resource_id: External-facing slug.

    Returns:
        The ``resources.id`` UUID, or ``None`` if no matching Resource
        row exists in this workspace. Callers on read paths should
        fail-safe to empty results on ``None`` rather than fall back to
        a slug-only filter — a missing Resource row indicates either a
        pre-a97 orphan or a cross-workspace probe; both cases must not
        surface satellite rows (CWE-639 / OWASP A01).
    """
    result = await db.execute(
        select(Resource.id).where(
            Resource.workspace_id == workspace_id,
            Resource.resource_id == resource_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert_resource(
    db: AsyncSession,
    workspace_id: UUID,
    resource_id: str,
    *,
    name: str | None = None,
    created_by: str | None = None,
) -> UUID:
    """Return the ``resources.id`` for ``(workspace_id, resource_id)``,
    creating the row if missing.

    Used by ``setup_resource`` and any other path that needs to bind a
    new Context to a Resource entity. Safe under concurrent calls —
    relies on ``uq_resources_workspace_resource_id`` and retries the
    SELECT once if the INSERT races.

    Args:
        db: Async DB session.
        workspace_id: Owning workspace UUID.
        resource_id: External-facing slug.
        name: Human-readable label (populated by ``setup_resource``;
            preserved on pre-existing rows — only applied on insert).
        created_by: Creator user ID (same semantics as ``name``).

    Returns:
        The ``resources.id`` UUID.
    """
    existing = await resolve_resource_pk(db, workspace_id, resource_id)
    if existing is not None:
        return existing

    resource = Resource(
        workspace_id=workspace_id,
        resource_id=resource_id,
        name=name,
        created_by=created_by,
    )
    # Nested savepoint (SAVEPOINT) so a concurrent insert that violates the
    # ``uq_resources_workspace_resource_id`` UNIQUE constraint only rolls
    # back THIS insert — the caller's outer transaction (and any prior
    # ``db.add()`` state staged on the session, e.g. ``handle_setup_resource``'s
    # role/plan/quota checks) is preserved. Using ``db.rollback()`` here
    # would silently wipe the caller's session identity map and cause
    # cryptic DetachedInstanceError failures downstream.
    try:
        async with db.begin_nested():
            db.add(resource)
            await db.flush()
    except IntegrityError:
        # Concurrent insert raced on ``uq_resources_workspace_resource_id``;
        # savepoint rolled back, outer tx intact. Narrow exception class so
        # ``CancelledError`` / ``OSError`` / other real failures are not
        # silently reinterpreted as "another writer got here first".
        existing = await resolve_resource_pk(db, workspace_id, resource_id)
        if existing is None:
            raise
        return existing
    # ``resource.id`` is populated by ``server_default=gen_random_uuid()`` on
    # flush. SQLAlchemy types it as ``Column[UUID]`` at the class level, so
    # cast the instance attribute to silence pyright — at instance level it
    # is the actual UUID value.
    assert isinstance(resource.id, UUID)
    return resource.id


async def get_latest_schema(
    db: AsyncSession,
    workspace_id: UUID,
    resource_id: str,
) -> ResourceSchema | None:
    """Return the highest-version ResourceSchema for a workspace-scoped slug.

    Shared chokepoint for reads that need the latest schema: extracts the
    resolve_resource_pk → filter-by-pk → order-by-version-desc → limit-1
    shape so each call site doesn't re-implement it. Fails safe to ``None``
    when the Resource row is missing (pre-a97 orphan, cross-workspace probe).

    Args:
        db: Async DB session.
        workspace_id: Owning workspace UUID.
        resource_id: External-facing slug.

    Returns:
        ResourceSchema with the highest schema_version, or ``None`` if the
        Resource or any schema for it is missing.
    """
    resource_pk = await resolve_resource_pk(db, workspace_id, resource_id)
    if resource_pk is None:
        return None
    result = await db.execute(
        select(ResourceSchema)
        .where(ResourceSchema.resource_pk == resource_pk)
        .order_by(ResourceSchema.schema_version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
