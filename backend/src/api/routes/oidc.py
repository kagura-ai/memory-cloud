"""Identity-only OIDC endpoints, isolated from MCP OAuth (#2781)."""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import html
import json
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from typing import Annotated, TypeVar, cast
from urllib.parse import parse_qsl, urlencode

from authlib.common.urls import add_params_to_uri
from authlib.oauth2.rfc6749.errors import OAuth2Error
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.oidc_config import load_oidc_config
from auth.oidc_limits import check_oidc_rate_limit
from auth.oidc_server import (
    IDENTITY_SCOPES,
    TOKEN_AUTH_METHODS,
    IdentityAuthorizationCodeGrant,
    IdentityServer,
    credential_hash,
    profile,
)
from auth.session import SESSION_COOKIE_NAME, get_session_manager
from config.settings import get_settings
from db.base import get_db, get_sync_session
from models.auth import User
from models.oidc import OIDCClient, OIDCGrant, OIDCToken
from services.sync_credentials import SyncCredentialError, issue_token
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException
from utils.hashing import hmac_sha256_hex
from utils.logger import get_logger

router = APIRouter(tags=["oidc"])
logger = get_logger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="oidc-sync")
T = TypeVar("T")
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


async def _run_sync(fn: Callable[[], T]) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, contextvars.copy_context().run, fn)


def _error(error: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error}, status_code=status, headers=_NO_STORE)


def _bearer_error(*, missing: bool = False) -> JSONResponse:
    challenge = "Bearer" if missing else 'Bearer error="invalid_token"'
    return JSONResponse(
        {} if missing else {"error": "invalid_token"},
        status_code=401,
        headers={**_NO_STORE, "WWW-Authenticate": challenge},
    )


def _configured() -> bool:
    return get_settings().oidc_enabled


def _browser(request: Request) -> tuple[str, str] | None:
    data = getattr(request.state, "user", None)
    if not isinstance(data, dict):
        return None
    user_id = data.get("user_id") or data.get("sub")
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not isinstance(user_id, str) or not user_id or not session_id:
        return None
    return user_id, session_id


def _csrf(request: Request, identity: tuple[str, str]) -> str:
    payload = json.dumps(["oidc-consent:v1", *identity, sorted(request.query_params.multi_items())])
    return hmac_sha256_hex(payload, get_settings().audit_hmac_key)


def _query_valid(request: Request) -> bool:
    params = request.query_params
    if len(str(params)) > 8192 or len(params) > 32:
        return False
    if any(len(params.getlist(key)) != 1 for key in params):
        return False
    # These optional request modes are not implemented by this first-party profile.
    return params.get("response_mode", "query") == "query" and not set(params) & {
        "request",
        "request_uri",
        "claims",
        "prompt",
        "max_age",
        "resource",
    }


async def _load_form(request: Request) -> dict[str, str] | None:
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        return None
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > 8192:
            return None
    try:
        items = parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=32)
    except (ValueError, UnicodeError):
        return None
    result = dict(items)
    if len(result) != len(items):
        return None
    request.state.form_data = result
    request.state.form_items = items
    return result


@router.get("/.well-known/openid-configuration")
async def discovery() -> Response:
    """Publish the dedicated issuer and supported confidential-client profile."""
    if not _configured():
        return _error("not_found", 404)
    try:
        config = await _run_sync(load_oidc_config)
    except ValueError:
        return _error("temporarily_unavailable", 503)
    issuer = config.issuer
    sync_enabled = (
        get_settings().sync_credentials_enabled and get_settings().sync_integrations_enabled
    )
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": issuer + "/authorize",
            "token_endpoint": issuer + "/token",
            "jwks_uri": issuer + "/jwks",
            "userinfo_endpoint": issuer + "/userinfo",
            "revocation_endpoint": issuer + "/revoke",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"]
            + (["client_credentials"] if sync_enabled else []),
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": TOKEN_AUTH_METHODS
            + (["private_key_jwt"] if sync_enabled else []),
            "revocation_endpoint_auth_methods_supported": TOKEN_AUTH_METHODS,
            **(
                {"token_endpoint_auth_signing_alg_values_supported": ["RS256"]}
                if sync_enabled
                else {}
            ),
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": sorted(
                IDENTITY_SCOPES | ({"resource.sync"} if sync_enabled else set())
            ),
            "claims_supported": [
                "iss",
                "sub",
                "aud",
                "iat",
                "exp",
                "nonce",
                "auth_time",
                "at_hash",
                "name",
                "picture",
            ],
        },
        headers=_NO_STORE,
    )


@router.get("/jwks")
async def jwks() -> Response:
    """Publish public RSA parameters only; never expose the signing file."""
    if not _configured():
        return _error("not_found", 404)
    try:
        return JSONResponse((await _run_sync(load_oidc_config)).public_jwks, headers=_NO_STORE)
    except ValueError:
        return _error("temporarily_unavailable", 503)


@router.get("/authorize")
@router.post("/authorize")
async def authorize(request: Request) -> Response:
    """Validate before login/consent; issue a code only for a live browser user."""
    if not _configured():
        return _error("not_found", 404)
    limited = await check_oidc_rate_limit(request)
    if limited is not None:
        return limited
    if not _query_valid(request):
        return _error("invalid_request")
    form = None
    if request.method == "POST":
        form = await _load_form(request)
        if form is None or set(form) != {"confirm", "csrf_token"}:
            return _error("invalid_request")
    identity = _browser(request)

    def perform() -> Response:
        with get_sync_session() as db:
            validated_redirect: str | None = None
            try:
                config = load_oidc_config()
                server = IdentityServer(db, config)
                # Consent parameters always live in the query, never the form.
                from auth.starlette_oauth2_request import StarletteOAuth2Request

                oauth_request = StarletteOAuth2Request(request, include_form=False)
                client = server.query_client(request.query_params.get("client_id", ""))
                callback = request.query_params.get("redirect_uri", "")
                if client is None or not callback or not client.check_redirect_uri(callback):
                    return _error("invalid_request")
                validated_redirect = callback
                grant = cast(
                    IdentityAuthorizationCodeGrant, server.get_authorization_grant(oauth_request)
                )
                grant.validate_authorization_request()
                manager = get_session_manager()
                user = server.lock_user(identity[0]) if identity else None
                live = bool(
                    identity and manager and manager.session_holds_user(identity[1], identity[0])
                )
                if user is None or not live or identity is None or manager is None:
                    db.rollback()
                    return RedirectResponse(
                        config.issuer.removesuffix("/oidc")
                        + "/login?"
                        + urlencode({"return_to": "/oidc/authorize?" + str(request.query_params)}),
                        status_code=303,
                        headers=_NO_STORE,
                    )
                if request.method == "GET":
                    csrf = _csrf(request, identity)
                    action = "/oidc/authorize?" + str(request.query_params)
                    descriptions = {
                        "openid": "Identify your KMC account",
                        "profile": "Read your display name and profile picture",
                        "workspaces:read": "Read your workspace memberships and permissions",
                        "integrations:manage": "Manage integrations within your current KMC permissions",
                        "sync.records:read": "Read approved synchronized product and FAQ records within your current KMC permissions",
                    }
                    scope_items = "".join(
                        f"<li>{html.escape(descriptions[scope])}</li>"
                        for scope in dict.fromkeys(request.query_params["scope"].split())
                    )
                    page = (
                        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Confirm sign in</title>'
                        f"<h1>Sign in to {html.escape(cast(OIDCClient, oauth_request.client).client_name)}</h1>"
                        f"<p>Continue as {html.escape(user.name or 'your current KMC account')}.</p>"
                        f"<p>Allow this application to:</p><ul>{scope_items}</ul>"
                        f'<form method="post" action="{html.escape(action, quote=True)}">'
                        f'<input type="hidden" name="csrf_token" value="{csrf}">'
                        '<button name="confirm" value="yes">Continue</button>'
                        '<button name="confirm" value="no">Cancel</button></form></html>'
                    )
                    db.rollback()
                    return HTMLResponse(
                        page,
                        headers={
                            **_NO_STORE,
                            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                            "Referrer-Policy": "no-referrer",
                            "X-Content-Type-Options": "nosniff",
                        },
                    )
                if form is None:
                    return _error("invalid_request")
                if not hmac.compare_digest(
                    form["csrf_token"].encode(), _csrf(request, identity).encode()
                ):
                    return _error("invalid_request", 403)
                if form["confirm"] not in {"yes", "no"}:
                    return _error("invalid_request")
                signed_in_at = manager.signed_in_at(identity[1], identity[0])
                if signed_in_at is None:
                    return _error("login_required", 401)
                server.auth_time = int(
                    (
                        signed_in_at.replace(tzinfo=UTC)
                        if signed_in_at.tzinfo is None
                        else signed_in_at
                    ).timestamp()
                )
                user_id, session_id = identity
                server.session_live = lambda: bool(manager.session_holds_user(session_id, user_id))
                response = cast(
                    Response,
                    server.create_authorization_response(
                        oauth_request,
                        grant_user=user if form["confirm"] == "yes" else None,
                        grant=grant,
                    ),
                )
                if response.status_code < 400:
                    db.commit()
                else:
                    db.rollback()
                return response
            except OAuth2Error as exc:
                db.rollback()
                if validated_redirect is not None:
                    params = [("error", exc.error)]
                    state = request.query_params.get("state")
                    if state is not None:
                        params.append(("state", state))
                    return RedirectResponse(
                        add_params_to_uri(validated_redirect, params),
                        status_code=303,
                        headers=_NO_STORE,
                    )
                return _error(exc.error)
            except IntegrityError:
                db.rollback()
                return _error("invalid_request")
            except Exception:
                db.rollback()
                logger.error("oidc_authorization_failed")
                return _error("temporarily_unavailable", 503)

    return await _run_sync(perform)


@router.post("/token")
async def token(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> Response:
    """Exchange or refresh in one transaction, including ID-token signing."""
    if not _configured():
        return _error("not_found", 404)
    limited = await check_oidc_rate_limit(request)
    if limited is not None:
        return limited
    form = await _load_form(request)
    if request.query_params or form is None or len(request.headers.getlist("authorization")) > 1:
        return _error("invalid_request")
    if form.get("grant_type") == "client_credentials":
        if not (
            get_settings().sync_credentials_enabled and get_settings().sync_integrations_enabled
        ):
            return _error("unsupported_grant_type")
        if request.headers.getlist("authorization"):
            return _error("invalid_client")
        try:
            return JSONResponse(await issue_token(db, form), headers=_NO_STORE)
        except SyncCredentialError as exc:
            return _error(
                "invalid_client" if exc.error == "invalid_token" else exc.error,
                400 if exc.error == "invalid_token" else exc.status,
            )
        except MemoryCloudException:
            return _error("access_denied", 403)
        except Exception as exc:
            logger.warning("sync_token_unavailable", failure_type=type(exc).__name__)
            return _error("temporarily_unavailable", 503)
    allowed = {"grant_type", "client_id", "client_secret"}
    if form.get("grant_type") == "authorization_code":
        allowed |= {"code", "redirect_uri", "code_verifier"}
    elif form.get("grant_type") == "refresh_token":
        allowed |= {"refresh_token", "scope"}
    if set(form) - allowed or (request.headers.get("authorization") and form.get("client_secret")):
        return _error("invalid_request")

    def perform() -> Response:
        with get_sync_session() as db:
            try:
                server = IdentityServer(db, load_oidc_config())
                response = cast(Response, server.create_token_response(request))
                if response.status_code < 400 or server.persist_revocation:
                    db.commit()
                else:
                    db.rollback()
                return response
            except Exception:
                db.rollback()
                logger.error("oidc_token_failed")
                return _error("temporarily_unavailable", 503)

    return await _run_sync(perform)


@router.get("/userinfo")
async def userinfo(request: Request) -> Response:
    """Resolve the current identity grant; cookies and ID tokens do not suffice."""
    if not _configured():
        return _error("not_found", 404)
    authorization = request.headers.get("authorization", "")
    if not authorization:
        return _bearer_error(missing=True)
    parts = authorization.split()
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not re.fullmatch(r"kmc_oidc_at_[A-Za-z0-9_-]{43}", parts[1])
    ):
        return _bearer_error()
    access_token = parts[1]
    if len(request.headers.getlist("authorization")) != 1:
        return _bearer_error()
    limited = await check_oidc_rate_limit(request)
    if limited is not None:
        return limited

    def perform() -> Response:
        with get_sync_session() as db:
            row = db.execute(
                select(OIDCToken, OIDCGrant, User)
                .join(OIDCGrant, OIDCGrant.id == OIDCToken.grant_id)
                .join(User, User.user_id == OIDCGrant.user_id)
                .join(OIDCClient, OIDCClient.client_id == OIDCGrant.client_id)
                .where(
                    OIDCToken.access_token_hash == credential_hash(access_token),
                    OIDCToken.revoked_at.is_(None),
                    OIDCGrant.revoked_at.is_(None),
                    OIDCClient.revoked_at.is_(None),
                    OIDCGrant.expires_at > utcnow(),
                )
            ).first()
            if row is None or row[0].issued_at + row[0].expires_in <= time.time():
                return _bearer_error()
            return JSONResponse(profile(row[2], row[0].get_scope()), headers=_NO_STORE)

    try:
        return await _run_sync(perform)
    except Exception:
        logger.error("oidc_userinfo_failed")
        return _error("temporarily_unavailable", 503)


@router.post("/revoke")
async def revoke(request: Request) -> Response:
    """Revoke this client's identity grant without touching the KMC browser session."""
    if not _configured():
        return _error("not_found", 404)
    limited = await check_oidc_rate_limit(request)
    if limited is not None:
        return limited
    form = await _load_form(request)
    if request.query_params or form is None or not form.get("token") or len(form["token"]) > 256:
        return _error("invalid_request")

    def perform() -> Response:
        with get_sync_session() as db:
            try:
                server = IdentityServer(db, load_oidc_config())
                oauth_request = server.create_oauth2_request(request)
                client = server.authenticate_client(oauth_request, TOKEN_AUTH_METHODS)
                digest = credential_hash(form["token"])
                grant_id = db.scalar(
                    select(OIDCToken.grant_id).where(
                        (OIDCToken.access_token_hash == digest)
                        | (OIDCToken.refresh_token_hash == digest)
                    )
                )
                grant = server.lock_grant(grant_id, client.get_client_id(), allow_expired=True)
                if grant:
                    grant.revoked_at = utcnow()
                db.commit()
                return Response(status_code=200, headers=_NO_STORE)
            except OAuth2Error as exc:
                db.rollback()
                return cast(Response, server.handle_error_response(oauth_request, exc))
            except Exception:
                db.rollback()
                logger.error("oidc_revocation_failed")
                return _error("temporarily_unavailable", 503)

    return await _run_sync(perform)
