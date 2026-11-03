"""A connector's write target belongs to the workspace (#2417, #2422).

``workspace_connectors.context_id`` names the context a chat connector's
worker writes into. Connectors create theirs private, yet it serves the
workspace, not the account that created it: whatever its privacy flag, it is
never that account's own context, and it never passes to a linked account
through the identity-link hand-over. When its creator leaves it follows the
workspace: the successor the admin picks, or the workspace's owner, who also
holds the connector's write key from then on
(``services.connector_key_ownership``).

The one definition every deletion path reads:

* :func:`connector_write_target_ids` — the ids, as a sub-select for SQL
  predicates (``user_deletion_service._personal``);
* :func:`connector_write_targets_among` — which of a list of contexts are
  write targets, in one query (``identity_link_service.private_context_takeovers``).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.resource import WorkspaceConnector


def connector_write_target_ids() -> Select[Any]:
    """The ids of every connector's write-target context, as a sub-select."""
    return select(WorkspaceConnector.context_id).where(WorkspaceConnector.context_id.is_not(None))


async def connector_write_targets_among(
    db: AsyncSession, context_ids: Collection[UUID]
) -> set[UUID]:
    """Which of ``context_ids`` are a connector's write target: one query for
    the whole collection. Read-only, and it locks nothing."""
    if not context_ids:
        return set()
    rows = await db.execute(
        select(WorkspaceConnector.context_id)
        .where(WorkspaceConnector.context_id.in_(list(context_ids)))
        .distinct()
    )
    return {context_id for context_id in rows.scalars() if context_id is not None}
