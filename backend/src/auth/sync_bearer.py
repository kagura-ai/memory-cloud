"""Sync-only opaque credentials; never resolve a human or a general API key."""

from typing import Annotated

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth.oidc_limits import PresentedCredential, check_oidc_rate_limit
from config.settings import get_settings
from services.sync_credentials import TOKEN_PATTERN

_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="SyncService",
    description="Installation-bound resource.sync service token; not a human, API key or Resource Token.",
)


async def sync_service_token(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
) -> str:
    """Bound namespace/header checks and fail-closed burst limit before DB work.

    Over the peer budget the limiter recognises the token on a short session
    of its own and counts it for the token's credential client (#2903).
    """
    settings = get_settings()
    if not (
        settings.oidc_enabled
        and settings.sync_integrations_enabled
        and settings.sync_credentials_enabled
    ):
        raise HTTPException(404, "not_found")
    headers = request.headers.getlist("authorization")
    parts = headers[0].split() if len(headers) == 1 else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or not TOKEN_PATTERN.fullmatch(parts[1]):
        raise HTTPException(401, "invalid_token", headers={"WWW-Authenticate": "Bearer"})
    limited = await check_oidc_rate_limit(request, PresentedCredential("sync_token", parts[1]))
    if limited is not None:
        raise HTTPException(
            limited.status_code,
            "temporarily_unavailable",
            headers={"Retry-After": limited.headers.get("Retry-After", "1")},
        )
    return parts[1]
