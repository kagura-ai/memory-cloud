"""Slack connector OAuth install + callback (Spec 2026-06-02, Plan 4).

A workspace admin clicks "Connect Slack" → we redirect to the shared Kagura
Slack app's OAuth consent → Slack calls back with a code → we exchange it for a
bot token + team id and stash the install in Redis under a one-time handle, then
redirect back to the Connectors page. The registration POST resolves that handle
server-side so the bot token never reaches the browser.

Mirrors the GitHub OAuth pattern in ``auth.py`` (httpx token exchange, Redis
CSRF state, ``_safe_redirect_url`` for the final redirect).
"""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from auth.dependencies import WorkspaceAdmin
from config.settings import get_settings
from db.redis import get_redis_client
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


async def pop_slack_install(handle: str) -> dict[str, Any] | None:
    """Fetch and delete a pending Slack install bundle (one-time use)."""
    redis = get_redis_client()
    raw = await redis.get(_install_key(handle))
    if raw is None:
        return None
    await redis.delete(_install_key(handle))
    return json.loads(raw)


async def peek_slack_install(handle: str) -> dict[str, Any] | None:
    """Read a pending Slack install bundle WITHOUT consuming it.

    Used by the connector-create path so a provisioning failure leaves the
    handle intact for retry; the caller calls ``discard_slack_install`` only
    after a successful create.
    """
    raw = await get_redis_client().get(_install_key(handle))
    return json.loads(raw) if raw is not None else None


async def discard_slack_install(handle: str) -> None:
    """Delete a consumed Slack install bundle (best-effort, after success)."""
    await get_redis_client().delete(_install_key(handle))


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
    await get_redis_client().setex(_state_key(state), _STATE_TTL_SECONDS, str(workspace_id))

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
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _SLACK_TOKEN_URL,
            data={
                "client_id": settings.slack_client_id,
                "client_secret": settings.slack_client_secret,
                "code": code,
                "redirect_uri": settings.slack_redirect_uri,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"Slack OAuth error: {data.get('error', 'unknown')}")
    return data


@router.get("/callback")
async def slack_callback(
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    """Handle the Slack OAuth callback: validate state, exchange code, stash install."""
    settings = get_settings()
    redis = get_redis_client()

    workspace_id = await redis.get(_state_key(state))
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired state token (CSRF protection)",
        )
    await redis.delete(_state_key(state))

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
    install = {
        "workspace_id": workspace_id.decode() if isinstance(workspace_id, bytes) else workspace_id,
        "bot_token": data.get("access_token"),
        "team_id": team.get("id"),
        "team_name": team.get("name"),
        "installing_admin_user_id": authed_user.get("id"),
    }
    handle = secrets.token_urlsafe(24)
    await redis.setex(_install_key(handle), _INSTALL_TTL_SECONDS, json.dumps(install))

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
    raw = await get_redis_client().get(_install_key(handle))
    if raw is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No pending Slack install")
    install = json.loads(raw)
    if str(install.get("workspace_id")) != str(workspace_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No pending Slack install")
    return {
        "team_id": install.get("team_id"),
        "team_name": install.get("team_name"),
        "installing_admin_user_id": install.get("installing_admin_user_id"),
    }
