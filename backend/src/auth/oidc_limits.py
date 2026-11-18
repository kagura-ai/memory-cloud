"""Strict burst protection for isolated OIDC protocol work.

Every counted request first increments a bucket for its ASGI peer, before any
validation. Over that budget a request is still admitted when it presents a
credential this server issued and one indexed lookup recognises (no password
hashing, no signature work): it is then counted in a second bucket keyed on
the credential's holder, with the same per-minute limit: a sync service token
counts for its sync client, a code or token issued to a user through an OIDC
client counts for that client and that user together, and a signed-in browser
on authorize counts for its user. A request without a recognisable credential gets
the same 429 as before. The lookup only decides admission; the protocol code
that runs afterwards sees the request unchanged.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import Select, select

from auth.oidc_server import credential_hash
from config.settings import get_settings
from db.base import _get_session_factory
from db.redis import increment_counter
from models.oidc import OIDCAuthorizationCode, OIDCGrant, OIDCToken
from models.sync_credential import SyncAccessToken
from utils.logger import get_logger

logger = get_logger(__name__)
_PATHS = frozenset({"/oidc/authorize", "/oidc/token", "/oidc/revoke", "/oidc/userinfo"})
_SYNC_HUMAN_SEGMENTS = frozenset({"sync-installations", "sync-authorizations"})
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

CredentialKind = Literal[
    "authorization_code", "refresh_token", "access_token", "sync_token", "session_user"
]
_SHAPES: dict[str, re.Pattern[str]] = {
    "authorization_code": re.compile(r"kmc_oidc_code_[A-Za-z0-9_-]{43}"),
    "refresh_token": re.compile(r"kmc_oidc_rt_[A-Za-z0-9_-]{43}"),
    "access_token": re.compile(r"kmc_oidc_at_[A-Za-z0-9_-]{43}"),
    "sync_token": re.compile(r"kmc_sync_at_[A-Za-z0-9_-]{43}"),
}


@dataclass(frozen=True)
class PresentedCredential:
    """What a request presents for admission over the peer budget; never logged."""

    kind: CredentialKind
    value: str = field(repr=False)


PresentedSource = Callable[[], Awaitable[PresentedCredential | None]]


def grant_credential(form: Mapping[str, str] | None) -> PresentedCredential | None:
    """The code or refresh token a token request presents for its declared grant."""
    if form is None:
        return None
    if form.get("grant_type") == "authorization_code" and "code" in form:
        return PresentedCredential("authorization_code", form["code"])
    if form.get("grant_type") == "refresh_token" and "refresh_token" in form:
        return PresentedCredential("refresh_token", form["refresh_token"])
    return None


def revocation_credential(form: Mapping[str, str] | None) -> PresentedCredential | None:
    """The access or refresh token a revocation request names, by its shape."""
    token = form.get("token") if form is not None else None
    if not isinstance(token, str):
        return None
    for kind in ("access_token", "refresh_token"):
        if _SHAPES[kind].fullmatch(token):
            return PresentedCredential(kind, token)
    return None


def rate_limit_operation(path: str) -> str | None:
    """The bucket a request path counts in, or ``None`` for an uncounted path."""
    if path in _PATHS:
        return path.rsplit("/", 1)[-1]
    if path == "/api/v1/me/workspaces":
        return "workspace-list"
    if path.startswith("/api/v1/sync/installations/"):
        return "sync-service"
    segments = path.split("/")
    if path.startswith("/api/v1/workspaces/") and (
        "/sync-installations/" in path
        or (len(segments) > 5 and segments[5] in _SYNC_HUMAN_SEGMENTS)
    ):
        return "sync-human"
    if (
        path.startswith("/api/v1/workspaces/")
        and path.endswith("/sync-billing-handoffs")
        and len(segments) == 6
    ):
        return "sync-setup-handoff"
    if (
        path.startswith("/api/v1/me/workspaces/")
        and path.endswith("/access")
        and len(segments) == 7
    ):
        return "workspace-access"
    return None


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"error": "temporarily_unavailable"},
        status_code=503,
        headers={**_NO_STORE, "Retry-After": "1"},
    )


def _over_budget(now: int) -> JSONResponse:
    return JSONResponse(
        {"error": "temporarily_unavailable", "error_description": "OIDC rate limit exceeded."},
        status_code=429,
        headers={**_NO_STORE, "Retry-After": str(max(1, 60 - now % 60))},
    )


def _holder_lookup(
    credential: PresentedCredential,
) -> tuple[tuple[str, ...], Select[*tuple[Any, ...]]] | None:
    """The holder labels and the one indexed read that recognises ``credential``.

    The read selects one column per label. An OIDC code or token is held per
    client and user: it is issued to a user's browser or client, so the user it
    was issued to answers for it together with the client.
    """
    shape = _SHAPES.get(credential.kind)
    if shape is None or not shape.fullmatch(credential.value):
        return None
    digest = credential_hash(credential.value)
    if credential.kind == "sync_token":
        return ("sync-client",), select(SyncAccessToken.client_id).where(
            SyncAccessToken.token_hash == digest
        )
    if credential.kind == "authorization_code":
        return ("oidc-client", "user"), (
            select(OIDCGrant.client_id, OIDCGrant.user_id)
            .join(OIDCAuthorizationCode, OIDCAuthorizationCode.grant_id == OIDCGrant.id)
            .where(OIDCAuthorizationCode.code_hash == digest)
        )
    column = (
        OIDCToken.access_token_hash
        if credential.kind == "access_token"
        else OIDCToken.refresh_token_hash
    )
    return ("oidc-client", "user"), (
        select(OIDCGrant.client_id, OIDCGrant.user_id)
        .join(OIDCToken, OIDCToken.grant_id == OIDCGrant.id)
        .where(column == digest)
    )


async def _first_row(statement: Select[*tuple[Any, ...]]) -> tuple[object, ...] | None:
    """Run one plain read in a short session of its own, never the request's.

    No row lock and no write; the session (and its connection) is closed
    before the limiter returns, so the protocol code that runs next shares no
    transaction, identity map or audit binding with it.
    """
    async with _get_session_factory()() as session:
        row = (await session.execute(statement)).first()
        return None if row is None else tuple(row)


async def _holder(credential: PresentedCredential) -> str | None:
    """Who answers for ``credential``, or ``None`` when it is not one this server issued.

    Existence only: a spent, expired or revoked credential is still recognised,
    and the protocol code refuses it exactly as it would within budget.
    """
    if credential.kind == "session_user":
        # SessionMiddleware already resolved the session cookie for this request.
        return "user:" + credential.value if credential.value else None
    lookup = _holder_lookup(credential)
    if lookup is None:
        return None
    labels, statement = lookup
    row = await _first_row(statement)
    if row is None or len(row) != len(labels):
        return None
    if not all(isinstance(part, str) and part for part in row):
        return None
    return ":".join(f"{label}:{part}" for label, part in zip(labels, row, strict=True))


async def _recognised_holder(
    presented: PresentedCredential | PresentedSource,
) -> str | None:
    try:
        credential = presented if isinstance(presented, PresentedCredential) else await presented()
        return None if credential is None else await _holder(credential)
    except Exception as exc:
        # Refuse like an unknown credential; the exception text may carry input.
        logger.warning("oidc_rate_limit_recognition_unavailable", failure_type=type(exc).__name__)
        return None


async def _count(key: str) -> int | None:
    """The bucket's new count, or ``None`` when the counter cannot be trusted."""
    try:
        count = await increment_counter(key, ttl=60)
    except Exception:
        # Redis wrappers can carry connection URLs in exception strings; keep
        # the failure diagnostic fixed and refuse authentication work.
        logger.warning("oidc_rate_limit_unavailable")
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return None
    return count


async def check_oidc_rate_limit(
    request: Request, presented: PresentedCredential | PresentedSource | None = None
) -> Response | None:
    """Refuse over-budget or unverifiable protocol requests before DB/crypto.

    ``request.client.host`` is the existing ASGI peer identity. Uvicorn may
    replace it only under its configured trusted proxy policy; raw forwarded
    headers are deliberately not interpreted here. No credential, request
    body, user ID or application-supplied client ID participates in the peer
    key. ``presented`` (a credential, or an async source that reads it, for a
    credential carried in the body) is consulted only over the peer budget;
    the holder it resolves to is hashed into the second key.
    """
    operation = rate_limit_operation(request.url.path)
    if operation is None:
        return None
    try:
        limit = get_settings().oidc_rate_limit_per_minute
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return _unavailable()
        peer = request.client.host if request.client else "unknown"
        peer_digest = hashlib.sha256(peer.encode("utf-8")).hexdigest()
        now = int(time.time())
    except Exception:
        logger.warning("oidc_rate_limit_unavailable")
        return _unavailable()
    window = now // 60
    count = await _count(f"oidc_rate_limit:{operation}:{peer_digest}:{window}")
    if count is None:
        return _unavailable()
    if count <= limit:
        return None
    holder = None if presented is None else await _recognised_holder(presented)
    if holder is None:
        return _over_budget(now)
    holder_digest = hashlib.sha256(holder.encode("utf-8")).hexdigest()
    count = await _count(f"oidc_rate_limit:{operation}:holder:{holder_digest}:{window}")
    if count is None:
        return _unavailable()
    return _over_budget(now) if count > limit else None
