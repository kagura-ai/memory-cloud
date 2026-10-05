"""Revoke every OAuth2 / MCP grant of an account, in the one lock order.

Two flows end a user's OAuth grants at once — a password reset (#1738, by
emailed link or by the operator CLI, #1866) and account erasure — and three
flows write new ones while a user is signed in:
the ``/authorize`` consent (``save_authorization_code``), the device-flow
approval (``device_confirm``) and the refresh grant. They all meet on the
owner's ``users`` row, so the order below is the one every one of them
follows (#1770):

    users → oauth_authorization_codes → oauth_device_codes → oauth_tokens

- The revoker takes ``users FOR UPDATE`` first, then touches the grant
  tables in that order.
- A grant writer takes ``users FOR KEY SHARE`` (conflicts with FOR UPDATE
  and nothing else) before it locks or writes any grant row, and only
  then re-checks the browser session it is acting for. A reset deletes the
  sessions before it commits, so a writer that waited on the user lock
  finds no session and writes nothing; a writer that got there first
  commits before the reset's statements run, which then cover its grant.
- The code and device-code exchanges lock their own grant row and never
  the user, so they cannot form a cycle with any of the above. That holds
  because ``oauth_authorization_codes``, ``oauth_device_codes`` and
  ``oauth_tokens`` have no foreign key to ``users``: an INSERT into them
  takes no implicit KEY SHARE on the user row. Adding such a key would put
  the exchanges into this order too — give them the user lock first.

Nothing here commits: the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import Executable

from models.auth import OAuth2AuthorizationCode, OAuth2DeviceCode, OAuth2Token, User
from utils.datetime import utcnow


@dataclass(frozen=True)
class RevokedGrants:
    """What one revocation removed or revoked, per table."""

    authorization_codes: int
    device_codes: int
    tokens: int


def grant_revocation_statements(
    user_id: str, *, delete_tokens: bool = False
) -> Iterator[Executable]:
    """Yield the revocation's statements in the one lock order.

    ``users FOR UPDATE``, then the DELETE of the pending authorization codes,
    the DELETE of the pending device codes and last the token statement (an
    UPDATE that revokes, or a DELETE with ``delete_tokens=True``). The async
    service and the synchronous operator CLI both execute exactly this
    sequence, so the order cannot drift between them.

    Lazy on purpose: execute each statement before asking for the next. The
    token UPDATE takes its revocation time when it is built, i.e. after the
    DELETEs returned — which may have waited for an exchange in flight.
    """
    yield select(User.user_id).where(User.user_id == user_id).with_for_update()
    yield delete(OAuth2AuthorizationCode).where(OAuth2AuthorizationCode.user_id == user_id)
    yield delete(OAuth2DeviceCode).where(OAuth2DeviceCode.user_id == user_id)
    if delete_tokens:
        yield delete(OAuth2Token).where(OAuth2Token.user_id == user_id)
        return
    now = utcnow()
    yield (
        update(OAuth2Token)
        .where(
            OAuth2Token.user_id == user_id,
            or_(
                OAuth2Token.access_token_revoked_at.is_(None),
                OAuth2Token.refresh_token_revoked_at.is_(None),
            ),
        )
        .values(
            revoked=True,
            access_token_revoked_at=func.coalesce(OAuth2Token.access_token_revoked_at, now),
            refresh_token_revoked_at=func.coalesce(OAuth2Token.refresh_token_revoked_at, now),
        )
    )


async def revoke_oauth_grants(
    db: AsyncSession, user_id: str, *, delete_tokens: bool = False
) -> RevokedGrants:
    """Lock the owner, drop the pending codes, revoke (or delete) the tokens.

    Runs inside the caller's transaction and locks the ``users`` row
    ``FOR UPDATE`` (a no-op for a caller that already holds it) so grant
    writers serialize on it. Pending authorization and device codes are
    deleted: an exchange that starts later finds no code, and one already
    under way holds its code row until it has stored its token, so the
    DELETE waits for it and the token statement below — a later statement
    with a fresh snapshot — then covers that token too. A reset revokes the
    tokens and keeps the rows (timestamps already set on a rotated or revoked
    pair are preserved, so the history survives); an erasure passes
    ``delete_tokens=True`` and the rows go in the same lock order.

    Returns:
        The rows deleted (codes) and revoked or deleted (tokens), per table.
    """
    results = [
        await db.execute(statement)
        for statement in grant_revocation_statements(user_id, delete_tokens=delete_tokens)
    ]
    return _revoked(results)


def revoke_oauth_grants_sync(
    db: Session, user_id: str, *, delete_tokens: bool = False
) -> RevokedGrants:
    """``revoke_oauth_grants`` for a synchronous session (the operator CLI).

    Same statements, same order, same transaction rules: nothing commits here.

    Returns:
        The rows deleted (codes) and revoked or deleted (tokens), per table.
    """
    results = [
        db.execute(statement)
        for statement in grant_revocation_statements(user_id, delete_tokens=delete_tokens)
    ]
    return _revoked(results)


def _revoked(results: list[Any]) -> RevokedGrants:
    _lock, codes, devices, tokens = results
    return RevokedGrants(
        authorization_codes=_rowcount(codes),
        device_codes=_rowcount(devices),
        tokens=_rowcount(tokens),
    )


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)
