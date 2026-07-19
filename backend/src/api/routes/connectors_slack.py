"""Slack connector OAuth install + callback (Spec 2026-06-02, Plan 4).

A workspace admin clicks "Connect Slack" → we redirect to the shared Kagura
Slack app's OAuth consent → Slack calls back with a code → we exchange it for a
bot token + team id and stash the install in Redis under a one-time handle, then
redirect back to the Connectors page. The registration POST resolves that handle
server-side so the bot token never reaches the browser.

Mirrors the GitHub OAuth pattern in ``auth.py`` (httpx token exchange, Redis
CSRF state, ``_safe_redirect_url`` for the final redirect).

Security notes
--------------
* ``/callback`` requires ``WorkspaceAdmin`` — the CSRF state alone is not
  sufficient to authenticate the caller; the admin dependency ensures only the
  workspace admin who initiated the install can complete it.
* The bot_token is Fernet-encrypted before being stashed in Redis so it cannot
  be read from a Redis dump, MONITOR output, or replication stream.
"""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from auth.dependencies import WorkspaceAdmin
from config.settings import get_settings
from db.redis import get_redis_client
from utils.encryption import get_encryptor
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/connectors/slack", tags=["connectors"])

_SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_STATE_TTL_SECONDS = 600
_INSTALL_TTL_SECONDS = 600


def _state_key(state: str) -> str:
    return f"slack_oauth_state:{state}"


def _install_key(handle: str) -> str:
    return f"slack_install:{handle}"


async def peek_slack_install(handle: str) -> dict[str, Any] | None:
    """Read a pending Slack install bundle WITHOUT consuming it.

    Used by the connector-create path so a provisioning failure leaves the
    handle intact for retry; the caller calls ``discard_slack_install`` only
    after a successful create. The bot_token in the returned dict is the
    decrypted plaintext (decrypted at read time from the Fernet ciphertext
    stored in Redis).
    """
    try:
        raw = await get_redis_client().get(_install_key(handle))
    except Exception:
        logger.warning("slack_install_redis_read_failed", handle=handle[:8])
        return None
    if raw is None:
        return None
    bundle = json.loads(raw)
    # Decrypt the bot_token that was Fernet-encrypted before storage.
    if bundle.get("bot_token_enc"):
        try:
            bundle["bot_token"] = get_encryptor().decrypt(bundle.pop("bot_token_enc"))
        except Exception:
            logger.warning("slack_install_bot_token_decrypt_failed", handle=handle[:8])
            return None
    return bundle


async def discard_slack_install(handle: str) -> None:
    """Delete a consumed Slack install bundle (best-effort, after success)."""
    try:
        await get_redis_client().delete(_install_key(handle))
    except Exception:
        logger.warning("slack_install_discard_failed", handle=handle[:8])


@router.get("/install")
async def slack_install(admin: WorkspaceAdmin) -> RedirectResponse:
    """Begin the Slack OAuth install for the current workspace."""
    settings = get_settings()
    if not settings.slack_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Slack connector is not configured",
        )
    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workspace selected. Please select a workspace first.",
        )

    state = secrets.token_urlsafe(32)
    try:
        await get_redis_client().setex(_state_key(state), _STATE_TTL_SECONDS, str(workspace_id))
    except Exception:
        logger.error("slack_oauth_state_store_failed", workspace_id=str(workspace_id))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to initiate Slack OAuth (storage unavailable)",
        ) from None

    query = urlencode(
        {
            "client_id": settings.slack_client_id,
            "scope": settings.slack_oauth_scopes,
            "redirect_uri": settings.slack_redirect_uri,
            "state": state,
        }
    )
    return RedirectResponse(url=f"{_SLACK_AUTHORIZE_URL}?{query}", status_code=302)


async def _exchange_slack_code(code: str) -> dict[str, Any]:
    """Exchange an OAuth code for a bot token + team identity."""
    settings = get_settings()
    # Explicit connect + read timeouts so a stalled Slack endpoint cannot
    # block the async worker indefinitely (connect stall = OS timeout ~75s).
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _SLACK_TOKEN_URL,
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": settings.slack_redirect_uri,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Slack OAuth error: {data.get('error', 'unknown')}")
    return data


def _error_redirect(frontend_url: str, reason: str) -> RedirectResponse:
    """303 back to the Connectors page with an allowlisted ``slack_error`` reason.

    ``reason`` is always one of ``cancelled``/``failed`` — raw Slack error text
    is never reflected into the redirect URL (same policy as auth.py's
    ``_oauth_cancel_redirect``).
    """
    frontend = frontend_url.rstrip("/")
    return RedirectResponse(
        url=f"{frontend}/workspace/integrations/connectors?slack_error={reason}",
        status_code=303,
    )


@router.get("/callback")
async def slack_callback(
    # WorkspaceAdmin re-asserts the caller's identity at callback time.
    # The CSRF state alone validates origin but not the calling principal —
    # any authenticated admin in the workspace can complete the install.
    admin: WorkspaceAdmin,
    # All optional at the route layer (#1375): a cancelled/denied consent
    # arrives as ?error=access_denied&state=... with NO code, and a required
    # `code` would turn that into a raw 422 JSON dead-end. Annotated form (not
    # `= Query(None)`) so direct calls in tests get real None defaults instead
    # of the Query sentinel object.
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[
        str | None,
        Query(description="OAuth2 error code (e.g. access_denied) when the user cancels"),
    ] = None,
    error_description: Annotated[
        str | None, Query(description="Human-readable OAuth2 error detail (not reflected)")
    ] = None,
) -> RedirectResponse:
    """Handle the Slack OAuth callback: validate state, exchange code, stash install."""
    settings = get_settings()
    redis = get_redis_client()

    # #1375: cancel/error short-circuit BEFORE strict validation. The raw
    # error text goes to the server log only; the redirect carries an
    # allowlisted token.
    if error is not None or code is None:
        reason = "cancelled" if error == "access_denied" else "failed"
        logger.info(
            "slack_oauth_cancelled",
            error=error or "missing_code",
            error_description=error_description,
            reason=reason,
        )
        if state:
            # Best-effort cleanup: the state is single-use and unguessable;
            # TTL expiry is the backstop if this delete fails.
            try:
                await redis.delete(_state_key(state))
            except Exception:
                logger.warning("slack_oauth_state_delete_failed", state=state[:8])
        return _error_redirect(settings.frontend_url, reason)

    if state is None:
        # code without state cannot be CSRF-validated; this is a browser-facing
        # route, so redirect instead of a raw 4xx dead-end.
        logger.warning("slack_oauth_callback_missing_state")
        return _error_redirect(settings.frontend_url, "failed")

    try:
        stored = await redis.get(_state_key(state))
    except Exception:
        logger.error("slack_oauth_state_read_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OAuth state storage unavailable",
        ) from None

    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state token (CSRF protection)",
        )

    # Normalise workspace_id: aioredis may return str or bytes depending on
    # decode_responses config; str(UUID()) and str(bytes) are both valid
    # UUID strings so we normalise via str() on both sides.
    stored_workspace_id = stored.decode() if isinstance(stored, bytes) else stored

    # Validate that the callback is for the same workspace admin who initiated
    # the install, preventing cross-workspace install completion.
    current_workspace_id = str(admin.get("current_workspace_id") or "")
    if stored_workspace_id != current_workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Workspace mismatch — please start the Slack connection again",
        )

    try:
        await redis.delete(_state_key(state))
    except Exception:
        logger.warning("slack_oauth_state_delete_failed", state=state[:8])

    try:
        data = await _exchange_slack_code(code)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("slack_oauth_exchange_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Slack authorization failed",
        ) from exc

    team = data.get("team") or {}
    authed_user = data.get("authed_user") or {}
    bot_token = data.get("access_token") or ""

    # Fernet-encrypt the bot_token before storing in Redis so it is never
    # exposed in a Redis memory dump, MONITOR trace, or replication stream.
    try:
        bot_token_enc = get_encryptor().encrypt(bot_token) if bot_token else ""
    except Exception:
        logger.error("slack_bot_token_encrypt_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to secure Slack credentials",
        ) from None

    install = {
        "workspace_id": stored_workspace_id,
        # The existing shared OAuth client is the compatibility app identity.
        # Multi-app OAuth credentials are a later control-plane extension;
        # carrying this binding now prevents a handle from being rebound.
        "app_key": "default",
        "bot_token_enc": bot_token_enc,
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "installing_admin_user_id": authed_user.get("id"),
    }
    handle = secrets.token_urlsafe(24)
    try:
        await redis.setex(_install_key(handle), _INSTALL_TTL_SECONDS, json.dumps(install))
    except Exception:
        logger.error("slack_install_store_failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to store Slack install (storage unavailable)",
        ) from None

    frontend = settings.frontend_url.rstrip("/")
    return RedirectResponse(
        url=f"{frontend}/workspace/integrations/connectors?slack_install={handle}",
        status_code=303,
    )


@router.get("/pending/{handle}")
async def slack_pending(handle: str, admin: WorkspaceAdmin) -> dict[str, Any]:
    """Return the non-secret summary of a pending Slack install (for the form).

    Peeks WITHOUT consuming (the install is popped at connector-create time) and
    never exposes the bot token. 404 if the handle is unknown/expired or belongs
    to another workspace.
    """
    workspace_id = admin.get("current_workspace_id")
    try:
        raw = await get_redis_client().get(_install_key(handle))
    except Exception:
        logger.warning("slack_pending_redis_read_failed", handle=handle[:8])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage unavailable"
        ) from None
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No pending Slack install"
        )
    install = json.loads(raw)
    if str(install.get("workspace_id")) != str(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No pending Slack install"
        )
    # Never return bot_token_enc or any secret field.
    return {
        "team_id": install.get("team_id"),
        "team_name": install.get("team_name"),
        "installing_admin_user_id": install.get("installing_admin_user_id"),
        "app_key": install.get("app_key") or "default",
    }
