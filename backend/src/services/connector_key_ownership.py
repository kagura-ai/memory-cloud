"""What a departing user's workspace credentials become (#2305, #2352, #2373).

Every path that deletes a user's ``api_keys`` rows — account erasure, member
removal, the admin user delete and ``cli.delete_admin`` — runs
:func:`settle_departing_credentials` first, inside its own transaction, right
before that delete. In each workspace someone else owns:

- the user's live connector write keys move to the workspace's owner (#2305,
  #2352): the worker holds the plaintext, and deleting the row would silently
  stop the connector;
- the user's agent-bound keys are deleted with the rest, never moved (#2373):
  an agent key is minted for a member/viewer service identity only
  (``_require_downgrade_target`` in ``api/routes/member_credentials.py``), and
  moving it to the owner would give the agent the owner's scope; deletion
  never widens (invariant 4 of ``docs/design/agent-registry-and-bindings.md``).
  An active agent left without a live key is suspended, so its owner sees that
  it needs a new credential: re-activate it, then mint a key for another
  service member (``create_key`` refuses an agent that is not active);
- the agents the user registered name the workspace's owner instead
  (``agents.owner_user_id`` is display only: agent management is gated on the
  workspace role, nothing authorizes on it).

A workspace the user still owns is left alone: it is deleted with everything in
it (a sole-owned workspace on erasure and in ``cli.delete_admin``), or the
removal is refused (an owner cannot be removed); the admin API transfers every
owned workspace before reaching this helper. Each workspace owner gets one
``departed_member_credentials`` audit row per workspace in which anything
happened.

Lock order: the agents (id order, ``FOR NO KEY UPDATE``), then the user's keys
(id order) — the order ``verify_key`` takes (the agent's ``last_seen_at``, then
the key's ``last_used_at``) and an agent delete's ``api_keys.agent_id``
cascade. ``NO KEY``: a member-key rotation holds its old key row while it
inserts the new one, whose foreign key share-locks the agent; under a plain
``FOR UPDATE`` that insert would wait here while this transaction waits on the
old key row.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Iterable
from typing import Any, Literal, cast
from uuid import UUID

from sqlalchemy import ColumnElement, Select, Update, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from config.settings import get_settings
from models.agent import AGENT_STATUS_ACTIVE, AGENT_STATUS_SUSPENDED, Agent
from models.auth import APIKey, AuditLog, User, Workspace
from services.erasure_details import locked_ids
from utils.hashing import hmac_sha256_hex

DEPARTED_MEMBER_CREDENTIALS_ACTION = "departed_member_credentials"

# Which deletion it was (``user_metadata.path`` of the audit row); the admin
# API and ``cli.delete_admin`` are both ``user_deletion``.
DeparturePath = Literal["erasure", "member_removal", "user_deletion"]


def _scoped(
    preds: tuple[ColumnElement[bool], ...], row: Any, workspace_id: UUID | None
) -> tuple[ColumnElement[bool], ...]:
    """``preds``, narrowed to one workspace when ``workspace_id`` is given
    (member removal leaves the user's rows in other workspaces alone)."""
    if workspace_id is None:
        return preds
    return (*preds, row.workspace_id == workspace_id)


def _not_owned_by(user_id: str, workspace_column: Any) -> tuple[ColumnElement[bool], ...]:
    """The row's workspace is someone else's (joined through ``workspaces``)."""
    return (workspace_column == Workspace.id, Workspace.owner_user_id != user_id)


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
    caller (a sole-owned workspace on erasure and in ``cli.delete_admin``),
    or the removal is refused (an owner cannot be removed). The admin API
    transfers every owned workspace before reaching this helper.
    The UPDATE bypasses ``create_key``'s per-owner active-name check; an owner
    already holding an active ``connector:<same id>`` key cannot happen (one
    active key per connector; rotation revokes before it mints).

    Lock order: the rows are the user's alone, locked in id order
    (``locked_ids``) — the same rows the caller's following DELETE locks, so
    no new order. The lock sub-select has no owner predicate, so it also locks
    the user's live connector keys in workspaces they own, which the guard
    then leaves alone; harmless, since that DELETE locks those rows anyway.
    The other writers of one of them, ``rotate_kmc_key`` (by
    ``key_hash``) and ``delete_connector`` (by ``connector_id``), queue on
    them.
    """

    def live(k: Any) -> tuple[ColumnElement[bool], ...]:
        return _scoped(
            (k.user_id == user_id, k.connector_id.is_not(None), k.revoked_at.is_(None)),
            k,
            workspace_id,
        )

    return (
        update(APIKey)
        .where(
            APIKey.id.in_(locked_ids(APIKey, live)),
            *live(APIKey),
            *_not_owned_by(user_id, APIKey.workspace_id),
        )
        .values(user_id=Workspace.owner_user_id)
        .execution_options(synchronize_session=False)
    )


def _live_agent_keys(
    k: Any, user_id: str, workspace_id: UUID | None
) -> tuple[ColumnElement[bool], ...]:
    """``k`` is one of ``user_id``'s live agent-bound keys (in scope)."""
    return _scoped(
        (k.user_id == user_id, k.agent_id.is_not(None), k.revoked_at.is_(None)),
        k,
        workspace_id,
    )


def _registered_by(a: Any, user_id: str, workspace_id: UUID | None) -> ColumnElement[bool]:
    """``a`` is an agent ``user_id`` registered (in scope)."""
    return and_(*_scoped((a.owner_user_id == user_id,), a, workspace_id))


def _losing_its_last_key(a: Any, user_id: str, workspace_id: UUID | None) -> ColumnElement[bool]:
    """``a`` is active, bound to a live key of ``user_id`` (in scope), and no
    other account holds a live key bound to it: the caller's delete leaves it
    without a credential. An agent another member still holds a key for keeps
    working (suspending it would stop that member's key too)."""
    mine = aliased(APIKey)
    others = aliased(APIKey)
    return and_(
        a.status == AGENT_STATUS_ACTIVE,
        a.id.in_(select(mine.agent_id).where(*_live_agent_keys(mine, user_id, workspace_id))),
        ~select(others.id)
        .where(
            others.agent_id == a.id,
            others.user_id != user_id,
            others.revoked_at.is_(None),
        )
        .exists(),
    )


def lock_agents_stmt(user_id: str, *, workspace_id: UUID | None = None) -> Select[Any]:
    """Lock, in id order, every agent :func:`reown_agents_stmt` and
    :func:`suspend_agents_stmt` may write: one phase, before any key row.

    ``FOR NO KEY UPDATE`` (see the module docstring). No workspace predicate,
    like the connector-key lock: a join would lock the ``workspaces`` rows too.
    The agents of a workspace the user owns are locked and then left alone.
    """
    a = aliased(Agent)
    return (
        select(a.id)
        .where(
            or_(
                _registered_by(a, user_id, workspace_id),
                _losing_its_last_key(a, user_id, workspace_id),
            )
        )
        .order_by(a.id)
        .with_for_update(key_share=True)
    )


def reown_agents_stmt(user_id: str, *, workspace_id: UUID | None = None) -> Update:
    """The UPDATE that names each workspace's owner on the agents ``user_id``
    registered there, whatever their status; nothing else of the agent
    changes. Returns ``(workspace_id, owner)`` per agent. Run after
    :func:`lock_agents_stmt`."""
    return (
        update(Agent)
        .where(
            _registered_by(Agent, user_id, workspace_id),
            *_not_owned_by(user_id, Agent.workspace_id),
        )
        .values(owner_user_id=Workspace.owner_user_id)
        .returning(Agent.workspace_id, Workspace.owner_user_id)
        .execution_options(synchronize_session=False)
    )


def suspend_agents_stmt(user_id: str, *, workspace_id: UUID | None = None) -> Update:
    """The UPDATE that suspends the agents the caller's key delete leaves
    without a credential (``_losing_its_last_key``). Bindings, enforcement
    mode and owner stay. Returns ``(workspace_id, owner)`` per agent. Run after
    :func:`lock_agents_stmt`."""
    return (
        update(Agent)
        .where(
            _losing_its_last_key(Agent, user_id, workspace_id),
            *_not_owned_by(user_id, Agent.workspace_id),
        )
        .values(status=AGENT_STATUS_SUSPENDED)
        .returning(Agent.workspace_id, Workspace.owner_user_id)
        .execution_options(synchronize_session=False)
    )


def count_agent_keys_stmt(
    user_id: str, *, workspace_id: UUID | None = None
) -> Select[UUID | None, str, int]:
    """``(workspace_id, owner, n)``: ``user_id``'s live agent-bound keys in
    the workspaces someone else owns, which the caller's following DELETE
    removes. The rows are locked in id order first, so the count is the one
    that DELETE sees (it re-locks them for free)."""
    return (
        select(APIKey.workspace_id, Workspace.owner_user_id, func.count())
        .where(
            APIKey.id.in_(locked_ids(APIKey, lambda k: _live_agent_keys(k, user_id, workspace_id))),
            *_not_owned_by(user_id, APIKey.workspace_id),
        )
        .group_by(APIKey.workspace_id, Workspace.owner_user_id)
    )


async def reown_connector_keys(
    db: AsyncSession, user_id: str, *, workspace_id: UUID | None = None
) -> int:
    """Run :func:`reown_connector_keys_stmt` alone; returns how many keys
    moved. The deletion paths call :func:`settle_departing_credentials`."""
    result = cast(
        CursorResult[Any],
        await db.execute(reown_connector_keys_stmt(user_id, workspace_id=workspace_id)),
    )
    return result.rowcount or 0


@dataclasses.dataclass(frozen=True)
class SettledCredentials:
    """What :func:`settle_departing_credentials` did, over every workspace
    someone else owns (the field names are the summary / response keys)."""

    connector_api_keys_reowned: int = 0
    # counted here, deleted by the caller's following DELETE
    agent_api_keys_deleted: int = 0
    agents_suspended: int = 0
    agents_reowned: int = 0

    def counts(self) -> dict[str, int]:
        return dataclasses.asdict(self)


async def settle_departing_credentials(
    db: AsyncSession,
    user_id: str,
    *,
    path: DeparturePath,
    workspace_id: UUID | None = None,
) -> SettledCredentials:
    """Settle ``user_id``'s agents and keys before the caller deletes its keys
    (see the module docstring); ``workspace_id`` narrows everything to one
    workspace (member removal).

    Stages one ``departed_member_credentials`` audit row per workspace owner
    and workspace in which anything happened, on the owner's trail:
    ``user_id`` / ``user_email`` are the owner's (the ``users`` row is read,
    not locked), the departing user is only ``new_value_hash`` = its HMAC
    under ``audit_hmac_key`` (the audit log's no-plaintext rule, as the
    ``identity_link`` rows), and ``user_metadata`` carries ``workspace_id``,
    ``path`` and the four counts. The INSERT is flushed here, before the
    caller's deletes.
    """
    tally: dict[tuple[str, UUID], Counter[str]] = {}

    def add(rows: Iterable[Any], key: str) -> int:
        total = 0
        for ws_id, owner, *n in rows:
            count = n[0] if n else 1
            tally.setdefault((owner, ws_id), Counter())[key] += count
            total += count
        return total

    await db.execute(lock_agents_stmt(user_id, workspace_id=workspace_id))
    agents_reowned = add(
        (await db.execute(reown_agents_stmt(user_id, workspace_id=workspace_id))).all(),
        "agents_reowned",
    )
    agents_suspended = add(
        (await db.execute(suspend_agents_stmt(user_id, workspace_id=workspace_id))).all(),
        "agents_suspended",
    )
    connector_keys = add(
        (
            await db.execute(
                reown_connector_keys_stmt(user_id, workspace_id=workspace_id).returning(
                    APIKey.workspace_id, APIKey.user_id
                )
            )
        ).all(),
        "connector_api_keys_reowned",
    )
    agent_keys = add(
        (await db.execute(count_agent_keys_stmt(user_id, workspace_id=workspace_id))).all(),
        "agent_api_keys_deleted",
    )
    if tally:
        await _audit(db, user_id, path, tally)
    return SettledCredentials(
        connector_api_keys_reowned=connector_keys,
        agent_api_keys_deleted=agent_keys,
        agents_suspended=agents_suspended,
        agents_reowned=agents_reowned,
    )


async def _audit(
    db: AsyncSession,
    user_id: str,
    path: DeparturePath,
    tally: dict[tuple[str, UUID], Counter[str]],
) -> None:
    owners = sorted({owner for owner, _ in tally})
    emails: dict[str, str] = dict(
        (await db.execute(select(User.user_id, User.email).where(User.user_id.in_(owners)))).all()
    )
    departing = hmac_sha256_hex(user_id, get_settings().audit_hmac_key)
    fields = [f.name for f in dataclasses.fields(SettledCredentials)]
    db.add_all(
        [
            AuditLog(
                user_email=emails.get(owner, ""),
                user_id=owner,
                action=DEPARTED_MEMBER_CREDENTIALS_ACTION,
                resource="api_key",
                new_value_hash=departing,
                user_metadata={
                    "workspace_id": str(ws_id),
                    "path": path,
                    **{name: tally[owner, ws_id][name] for name in fields},
                },
            )
            for owner, ws_id in sorted(tally, key=lambda k: (k[0], str(k[1])))
        ]
    )
    await db.flush()
