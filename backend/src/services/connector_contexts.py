"""A connector's write target belongs to the workspace (#2417, #2422).

``workspace_connectors.context_id`` names the context a chat connector's
worker writes into by default, and every ``route`` row of
``workspace_connector_channel_routes`` names one more: the context one channel
is written into instead (#1759). Each is a write target, the routed ones on
the same terms as the default one. Connectors create theirs private, yet it serves the
workspace, not the account that created it: whatever its privacy flag, it is
never that account's own context, and it never passes to a linked account
through the identity-link hand-over. When its creator leaves it goes to the
workspace's owner, who also holds the connector's write key from then on
(``services.connector_key_ownership``): on every deletion path, so the key and
the context it writes into keep one owner (#2557, #2420).

The one definition every deletion path reads:

* :func:`connector_write_target_ids` — the ids, as a sub-select for SQL
  predicates (``user_deletion_service._personal``): the default contexts and
  the route destinations, in one ``UNION``;
* :func:`connector_write_targets_among` — which of a list of contexts are
  write targets, in one query (``identity_link_service.private_context_takeovers``,
  ``user_deletion_service.transfer_user_ownership``);
* :func:`reown_connector_contexts` — account erasure hands the subject's to
  the workspace's owner before it pseudonymizes ``contexts.created_by``.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CompoundSelect, select, union, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.resource import WorkspaceConnector, WorkspaceConnectorChannelRoute
from services.erasure_details import locked_ids


def connector_write_target_ids() -> CompoundSelect[Any]:
    """The ids of every connector's write-target context, as a sub-select:
    each connector's default context and each channel route's destination
    (#1759). Plain reads of both tables: used inside a ``FOR UPDATE`` query it
    locks only that query's own rows, never a connector or a route."""
    return union(
        select(WorkspaceConnector.context_id).where(WorkspaceConnector.context_id.is_not(None)),
        select(WorkspaceConnectorChannelRoute.context_id).where(
            WorkspaceConnectorChannelRoute.context_id.is_not(None)
        ),
    )


async def connector_write_targets_among(
    db: AsyncSession, context_ids: Collection[UUID]
) -> set[UUID]:
    """Which of ``context_ids`` are a connector's write target: one query for
    the whole collection. Read-only, and it locks nothing."""
    if not context_ids:
        return set()
    wanted = list(context_ids)
    rows = await db.execute(
        union(
            select(WorkspaceConnector.context_id).where(WorkspaceConnector.context_id.in_(wanted)),
            select(WorkspaceConnectorChannelRoute.context_id).where(
                WorkspaceConnectorChannelRoute.context_id.in_(wanted)
            ),
        )
    )
    return {context_id for context_id in rows.scalars() if context_id is not None}


async def reown_connector_contexts(db: AsyncSession, user_id: str) -> int:
    """Hand every connector write target ``user_id`` created to its
    workspace's owner; returns how many moved.

    Account erasure runs it after the identity-link hand-over, which never
    takes one, and before the authorship sweep pseudonymizes
    ``contexts.created_by``: under the pseudonym nobody could read the
    context the connector, its key re-owned to the same owner, keeps writing
    into. The ``owner_user_id <> user_id`` guard is
    ``reown_connector_keys_stmt``'s: a workspace the user still owns goes
    with its contexts.

    Lock order: the rows are reached through ``locked_ids`` (contexts, in id
    order), which the caller already holds (the erasure locks every context
    the subject created before the hand-over), so the re-lock is free;
    ``workspaces`` and ``workspace_connectors`` are only read.
    ``synchronize_session="fetch"``: the hand-over loaded these rows into the
    session, which must not keep the old creator.
    """

    def created_targets(c: Any) -> tuple[Any, ...]:
        return (c.created_by == user_id, c.id.in_(connector_write_target_ids()))

    result = await db.execute(
        update(Context)
        .where(
            Context.id.in_(locked_ids(Context, created_targets)),
            Context.workspace_id == Workspace.id,
            Workspace.owner_user_id != user_id,
        )
        .values(created_by=Workspace.owner_user_id)
        .execution_options(synchronize_session="fetch")
    )
    return cast(CursorResult[Any], result).rowcount or 0
