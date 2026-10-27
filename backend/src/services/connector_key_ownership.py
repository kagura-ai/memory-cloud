"""Hand a user's live connector write keys to their workspace's owner (#2305, #2352).

A connector's write key (``api_keys.connector_id`` set) is the workspace's
ingest credential: the worker holds its plaintext, so deleting the row with the
user's other keys silently stops the connector. Every path that deletes a
user's ``api_keys`` rows — account erasure, member removal, the admin user
delete and ``cli.delete_admin`` — runs this first, inside its own transaction,
right before that delete.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Update, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import APIKey, Workspace


def reown_connector_keys_stmt(user_id: str, *, workspace_id: UUID | None = None) -> Update:
    """The UPDATE that hands ``user_id``'s live connector keys to each key's
    workspace owner; ``workspace_id`` narrows it to one workspace (member
    removal leaves the user's keys in other workspaces alone).

    Only ``user_id`` changes — hash, prefix, name, ``connector_id``,
    ``workspace_id``, expiry, ``hidden_at`` / ``plaintext_encrypted`` stay — so
    the worker's stored plaintext keeps verifying, and ``verify_key`` /
    ``rotate_kmc_key`` / ``delete_connector`` need no change (they key on
    ``key_hash`` / ``connector_id``); the next rotation mints under the owner.

    Scope: the user's rows with ``connector_id`` set and ``revoked_at IS NULL``
    (an expired, unrevoked key is still rotatable, so it moves too); revoked
    connector keys are dead rows and the caller deletes them with the rest.
    The ``owner_user_id <> user`` guard keeps a key of a workspace the user
    still owns where it is — that workspace is deleted with its keys by the
    caller (a sole-owned workspace on erasure, every owned workspace on the
    admin delete) or the removal is refused (an owner cannot be removed). The
    UPDATE bypasses ``create_key``'s per-owner active-name check; an owner
    already holding an active ``connector:<same id>`` key cannot happen (one
    active key per connector; rotation revokes before it mints).

    Lock order: the rows are the user's alone, locked in id order
    (``locked_ids``) — the same rows the caller's following DELETE locks, so
    no new order; the other writers of one of them, ``rotate_kmc_key`` (by
    ``key_hash``) and ``delete_connector`` (by ``connector_id``), queue on
    them. The statement is built synchronously so the sync CLI can run it too.
    """
    # Lazy: ``erasure_details`` pulls in the Qdrant / payload-sync modules,
    # which the routes and the CLI importing this module do not need otherwise.
    from services.erasure_details import locked_ids

    def live(k: Any) -> tuple[ColumnElement[bool], ...]:
        preds: tuple[ColumnElement[bool], ...] = (
            k.user_id == user_id,
            k.connector_id.is_not(None),
            k.revoked_at.is_(None),
        )
        if workspace_id is None:
            return preds
        return (*preds, k.workspace_id == workspace_id)

    return (
        update(APIKey)
        .where(
            APIKey.id.in_(locked_ids(APIKey, live)),
            *live(APIKey),
            APIKey.workspace_id == Workspace.id,
            Workspace.owner_user_id != user_id,
        )
        .values(user_id=Workspace.owner_user_id)
        .execution_options(synchronize_session=False)
    )


async def reown_connector_keys(
    db: AsyncSession, user_id: str, *, workspace_id: UUID | None = None
) -> int:
    """Run :func:`reown_connector_keys_stmt`; returns how many keys moved."""
    result = cast(
        CursorResult[Any],
        await db.execute(reown_connector_keys_stmt(user_id, workspace_id=workspace_id)),
    )
    return result.rowcount or 0
