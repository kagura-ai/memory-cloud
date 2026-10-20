"""The OAuth apps the user has authorized, and their settings (#2129).

``GET /api/v1/me/oauth/grants`` lists the clients the caller holds a live
token for (plus any with a stored setting). Each grant can carry an MCP
default context — a context-scoped tool called over a bearer token of that
client without ``context_id`` runs in it (``?context=`` on the URL wins).
``PUT .../{client_id}/default-context`` sets it, ``DELETE`` clears it; the
consent page offers the same choice when the app is authorized.

Session-only, like API key management: an API key or another app's OAuth
token must not list the user's apps or re-point another app's default. Only
the caller's own grants are visible or changeable, and a grant that is not the
caller's answers the same 404 as one that does not exist.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.credential_default_context import (
    GrantSummary,
    list_grants,
    readable_context,
    readable_context_name,
    set_grant_default_context,
)
from utils import get_user_id
from utils.exceptions import NotFoundException
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/me/oauth", tags=["me-oauth"])


class OAuthGrantResponse(TZAwareBaseModel):
    """One OAuth app the caller has authorized."""

    client_id: str = Field(..., description="The OAuth client's id")
    client_name: str = Field(..., description="The client's registered name")
    scopes: list[str] = Field(
        default_factory=list, description="Scopes of the caller's live tokens for this client"
    )
    default_context_id: UUID | None = Field(
        None, description="The grant's MCP default context; null when none"
    )
    default_context_name: str | None = Field(
        None,
        description=(
            "Name of the default context; null when none is set or the caller can no longer read it"
        ),
    )
    last_issued_at: datetime | None = Field(
        None, description="When the newest live token was issued; null when none is live"
    )


class OAuthGrantDefaultContextUpdate(BaseModel):
    """Body of ``PUT /me/oauth/grants/{client_id}/default-context``."""

    default_context_id: UUID | None = Field(
        ..., description="The grant's MCP default context; null clears it"
    )


async def _response(
    db: AsyncSession, user: dict, grant: GrantSummary, *, name: str | None = None
) -> OAuthGrantResponse:
    if name is None and grant.default_context_id is not None:
        name = await readable_context_name(
            db,
            user_id=get_user_id(user),
            context_id=grant.default_context_id,
        )
    return OAuthGrantResponse(
        client_id=grant.client_id,
        client_name=grant.client_name,
        scopes=grant.scopes,
        default_context_id=grant.default_context_id,
        default_context_name=name,
        last_issued_at=grant.last_issued_at,
    )


async def _own_grant(db: AsyncSession, user_id: str, client_id: str) -> GrantSummary:
    for grant in await list_grants(db, user_id=user_id, client_id=client_id):
        return grant
    raise NotFoundException("OAuth grant")


@router.get("/grants", response_model=list[OAuthGrantResponse])
async def list_oauth_grants(
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> list[OAuthGrantResponse]:
    """List the OAuth apps the caller has authorized, newest issuance first."""
    grants = await list_grants(db, user_id=get_user_id(user))
    return [await _response(db, user, grant) for grant in grants]


@router.put(
    "/grants/{client_id}/default-context",
    response_model=OAuthGrantResponse,
)
async def set_oauth_grant_default_context(
    client_id: str,
    data: OAuthGrantDefaultContextUpdate,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> OAuthGrantResponse:
    """Set (or, with ``null``, clear) the MCP default context of one grant.

    The context must be readable by the caller now (viewer); otherwise 404
    ``Context not found``.

    Raises:
        NotFoundException: The caller has no grant for ``client_id``, or
            cannot read the context (404).
    """
    user_id = get_user_id(user)
    grant = await _own_grant(db, user_id, client_id)
    name: str | None = None
    if data.default_context_id is not None:
        context = await readable_context(
            db,
            user_id=user_id,
            context_id=data.default_context_id,
        )
        if context is None:
            raise NotFoundException("Context")
        name = context.name
    await set_grant_default_context(
        db, user_id=user_id, client_id=client_id, context_id=data.default_context_id
    )
    await db.commit()
    logger.info(
        "oauth_grant_default_context_changed",
        user_id=user_id,
        client_id=client_id,
        default_context_set=data.default_context_id is not None,
    )
    updated = GrantSummary(
        client_id=grant.client_id,
        client_name=grant.client_name,
        scopes=grant.scopes,
        default_context_id=data.default_context_id,
        last_issued_at=grant.last_issued_at,
    )
    return await _response(db, user, updated, name=name)


@router.delete(
    "/grants/{client_id}/default-context",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def clear_oauth_grant_default_context(
    client_id: str,
    user: SessionUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Clear the MCP default context of one grant.

    Raises:
        NotFoundException: The caller has no grant for ``client_id`` (404).
    """
    user_id = get_user_id(user)
    await _own_grant(db, user_id, client_id)
    await set_grant_default_context(db, user_id=user_id, client_id=client_id, context_id=None)
    await db.commit()
    logger.info("oauth_grant_default_context_changed", user_id=user_id, client_id=client_id)
