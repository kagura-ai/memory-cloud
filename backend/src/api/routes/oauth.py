"""OAuth2 Server endpoints for ChatGPT/Claude integrations.

Issue #33 - OAuth2 authentication support for ChatGPT MCP integration
Issue #252: OAuth2 client management endpoints now Session-only (no API keys)

Provides OAuth2 client management endpoints:
1. GET /api/v1/oauth/clients - List OAuth2 clients
2. POST /api/v1/oauth/clients - Create OAuth2 client (returns secret once)
3. GET /api/v1/oauth/clients/{id} - Get client details
4. PUT /api/v1/oauth/clients/{id} - Update client
5. DELETE /api/v1/oauth/clients/{id} - Delete client
6. GET /api/v1/oauth/providers - List OAuth2 providers

OAuth2 Authorization Flow endpoints will be added in Phase 1.4 Part B.

Security:
- Client secret validation (SHA256)
- Owner-based access control
- Automatic secret generation
"""

import hashlib
import os
import secrets
import unicodedata
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser, require_admin
from auth.mcp_scopes import DCR_DEFAULT_SCOPE
from auth.oauth2_server import _OAuthUser, create_authorization_server
from config.settings import get_settings
from db.base import get_db, get_sync_session
from db.redis import increment_counter
from models.api_base import TZAwareBaseModel
from models.auth import OAuth2Client, OAuth2DeviceCode, OAuth2Token, User, generate_user_code
from models.schemas import TokenIntrospectionResponse
from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger
from utils.oauth_errors import rfc6749_error_response
from utils.oauth_messages import get_oauth_messages
from utils.redirect_uri import is_valid_redirect_uri_pattern


def _check_redirect_uri_patterns(value: list[str]) -> list[str]:
    """Validate that each redirect_uri is a well-formed pattern.

    Patterns may be exact URIs or trailing-wildcard URIs (e.g.
    ``https://example.com/cb/*``). The wildcard ``*`` is only allowed as a
    trailing path segment to prevent open-redirect attacks (Issue #207).
    """
    for uri in value:
        if not is_valid_redirect_uri_pattern(uri):
            raise ValueError(
                f"Invalid redirect_uri pattern: {uri!r}. "
                "Must be an http/https URL. Wildcard '*' is only allowed as "
                "a trailing path segment (e.g. https://example.com/cb/*)."
            )
    return value


# Initialize Jinja2 templates
TEMPLATES_DIR = Path(__file__).parent.parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ============================================================================
# Dependencies
# ============================================================================


async def preload_form(request: Request):
    """Preload form data for OAuth2 endpoints.

    Authlib's create_oauth2_request is sync, but FastAPI's request.form() is async.
    This dependency reads form data in advance and stores it in request.state.
    """
    if request.method.upper() == "POST":
        form = await request.form()
        request.state.form_data = dict(form)
    else:
        request.state.form_data = {}
    return request


logger = get_logger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth2-server"])

# Per-IP rate limit for /device/audit-unauth (#779). Generous because legitimate
# users routinely land on /device unauthenticated; the cap is to bound log spam
# from device-code spraying attempts, not to throttle real users.
_DEVICE_UNAUTH_AUDIT_RATE_LIMIT = 30


# ============================================================================
# Models
# ============================================================================


class OAuth2ClientResponse(BaseModel):
    """OAuth2 Client response (without secret)."""

    id: int
    client_id: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    scope: str
    token_endpoint_auth_method: str
    # DCR-registered clients have no owner (owner_id=None); admin-managed
    # clients store the creating user's id. Issue #513.
    owner_id: str | None
    provider: str  # Migration 036
    created_at: str  # ISO 8601 with 'Z' (UTC)
    # Migration 034-035: Zero-knowledge visibility
    # ``plaintext_secret`` defaults to None so it is non-required in the OpenAPI
    # schema — the DCR ``/register`` endpoint omits it via response_model_exclude
    # for public clients (Issue #689), and generated SDK clients must not treat
    # the field as required.
    plaintext_secret: str | None = None  # Only if visible + owner
    is_visible: bool
    visibility_expires_at: str | None = None  # ISO 8601 with 'Z' (UTC)

    class Config:
        from_attributes = True


class OAuth2ClientWithSecretResponse(OAuth2ClientResponse):
    """OAuth2 Client response with client secret (only on creation).

    ``client_secret`` is ``None`` for public clients
    (``token_endpoint_auth_method="none"``) per RFC 7591 §3.2.1, which
    forbids issuing a secret to clients that won't authenticate at the
    token endpoint. Confidential clients always receive a value.
    """

    client_secret: str | None = None


class OAuth2ClientCreateRequest(BaseModel):
    """OAuth2 Client creation request."""

    client_name: str = Field(
        ..., min_length=1, max_length=100, description="Human-readable client name"
    )
    redirect_uris: list[str] = Field(..., min_length=1, description="Allowed redirect URIs")
    provider: str = Field(
        default="custom",
        pattern="^(claude|chatgpt|cursor|custom)$",  # Add cursor
        description="OAuth provider type (Migration 036)",
    )
    grant_types: list[str] = Field(
        default=["authorization_code", "refresh_token"], description="Allowed grant types"
    )
    response_types: list[str] = Field(default=["code"], description="Allowed response types")
    scope: str = Field(
        default=DCR_DEFAULT_SCOPE,
        description="Space-separated scopes",
    )
    token_endpoint_auth_method: str = Field(
        default="client_secret_post", description="Client authentication method"
    )

    @field_validator("redirect_uris")
    @classmethod
    def _validate_redirect_uris(cls, value: list[str]) -> list[str]:
        return _check_redirect_uri_patterns(value)


class DynamicClientRegistrationRequest(BaseModel):
    """Dynamic Client Registration (DCR) request for MCP clients.

    Simplified schema for ChatGPT/Claude/Cursor OAuth registration.
    MCP clients use this for automatic registration.
    """

    client_name: str = Field(
        default="MCP Client", min_length=1, max_length=100, description="Client name"
    )
    redirect_uris: list[str] = Field(..., min_length=1, description="Redirect URIs")
    grant_types: list[str] = Field(
        default=["authorization_code", "refresh_token"], description="Grant types"
    )
    response_types: list[str] = Field(default=["code"], description="Response types")
    scope: str | None = Field(default=None, description="Requested scopes (space-separated)")
    token_endpoint_auth_method: str = Field(
        default="none", description="Token endpoint auth method"
    )

    @field_validator("redirect_uris")
    @classmethod
    def _validate_redirect_uris(cls, value: list[str]) -> list[str]:
        return _check_redirect_uri_patterns(value)


class OAuth2ClientUpdateRequest(BaseModel):
    """OAuth2 Client update request."""

    client_name: str | None = Field(None, min_length=1, max_length=100)
    redirect_uris: list[str] | None = Field(None, min_length=1)
    scope: str | None = None
    token_endpoint_auth_method: str | None = Field(
        None,
        description="Client authentication method (none, client_secret_post, client_secret_basic)",
    )

    @field_validator("redirect_uris")
    @classmethod
    def _validate_redirect_uris(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return _check_redirect_uri_patterns(value)


class OAuth2ProviderResponse(BaseModel):
    """OAuth2 Provider response."""

    name: str
    display_name: str
    client_id: str | None
    authorization_url: str
    token_url: str
    scopes: list[str]
    enabled: bool
    configured: bool  # Alias for enabled (frontend compatibility)


# ============================================================================
# Device Authorization Grant Schemas (RFC 8628, Issue #536)
# ============================================================================


class DeviceAuthorizationRequest(BaseModel):
    """Device Authorization Request (RFC 8628 Section 3.1)."""

    client_id: str = Field(..., description="OAuth2 client identifier")
    scope: str | None = Field(None, description="Requested scope (space-separated)")


class DeviceAuthorizationResponse(TZAwareBaseModel):
    """Device Authorization Response (RFC 8628 Section 3.2)."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class DeviceVerifyRequest(BaseModel):
    """Request to look up pending device authorization by user_code."""

    user_code: str = Field(..., min_length=8, max_length=8)


class DeviceVerifyResponse(TZAwareBaseModel):
    """Pending device authorization info returned for the consent screen."""

    user_code: str
    client_name: str
    scope: str | None
    expires_at: str  # ISO 8601 with Z suffix (manually formatted via to_utc_iso)
    is_authorized: bool
    is_expired: bool


class DeviceConfirmRequest(BaseModel):
    """User consent decision for a pending device authorization."""

    user_code: str = Field(..., min_length=8, max_length=8)
    approve: bool = Field(True)


class DeviceConfirmResponse(TZAwareBaseModel):
    """Result of user consent decision."""

    status: str  # "approved" or "denied"
    user_code: str


class DeviceUnauthAuditRequest(BaseModel):
    """Fire-and-forget audit payload for unauthenticated /device hits (Issue #779).

    The frontend auth guard pings this endpoint BEFORE redirecting an
    unauthenticated user to /login. The backend otherwise has no visibility
    into device-code spraying or bot traffic on the /device route.

    Security: user_code_prefix is capped at 4 chars — the full 8-char user_code
    is OAuth bearer material within the 5-10 min TTL (RFC 8628 §5.2) and must
    never be logged.
    """

    user_code_prefix: str = Field("", max_length=4)


# ============================================================================
# Helper Functions
# ============================================================================


def get_current_user_id(request: Request) -> str:
    """Get current authenticated user ID from request.

    Args:
        request: FastAPI request (with SessionMiddleware)

    Returns:
        User ID string

    Raises:
        HTTPException: If user not authenticated
    """
    # SessionMiddleware sets request.state.user
    if not hasattr(request.state, "user") or not request.state.user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please login first.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = request.state.user
    # user can be dict or object, handle both
    if isinstance(user, dict):
        user_id = user.get("user_id") or user.get("sub") or user.get("email")
    else:
        user_id = getattr(user, "user_id", None) or getattr(user, "email", None)

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID not found in session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user_id


# ============================================================================
# OAuth2 Client Management Endpoints
# ============================================================================


@router.get("/clients", response_model=list[OAuth2ClientResponse])
async def list_oauth2_clients(
    request: Request,
    user: SessionUser,
) -> list[OAuth2ClientResponse]:
    """List OAuth2 clients owned by authenticated user's current context.

    Issue #82: Now context-scoped - returns clients for current context only.

    SECURITY FIX (Issue #93-3): Filter by owner_id

    Returns:
        List of user's OAuth2 clients (without secrets)

    Example:
        GET /api/v1/oauth/clients
    """
    db_session = get_sync_session()

    try:
        # Get current user ID (Issue #93-3: SECURITY)
        current_user_id = get_current_user_id(request)
        current_workspace_id = user.get("current_workspace_id")  # Issue #169, Migration 034

        # Filter by owner_id AND workspace_id (Migration 034)
        query = db_session.query(OAuth2Client).filter_by(owner_id=current_user_id)

        if current_workspace_id:
            query = query.filter_by(workspace_id=str(current_workspace_id))

        clients = query.order_by(OAuth2Client.created_at.desc()).all()

        # Migration 034-035: Decrypt secrets for visible + owner
        from utils.encryption import get_encryptor

        encryptor = get_encryptor()
        response_list = []

        for client in clients:
            plaintext_secret = None
            # Check visibility: not hidden AND (no expiration OR not expired yet)
            is_visible = client.hidden_at is None and (
                client.visibility_expires_at is None or client.visibility_expires_at > utcnow()
            )

            # Owner + visible の場合のみ復号化
            if (
                current_user_id == client.owner_id
                and is_visible
                and client.plaintext_secret_encrypted
            ):
                try:
                    plaintext_secret = encryptor.decrypt(client.plaintext_secret_encrypted)
                except Exception as e:
                    logger.error(f"Failed to decrypt secret for client {client.client_id}: {e}")

            response_list.append(
                OAuth2ClientResponse(
                    id=client.id,
                    client_id=client.client_id,
                    client_name=client.client_name,
                    redirect_uris=client.redirect_uris,
                    grant_types=client.grant_types,
                    response_types=client.response_types,
                    scope=client.scope,
                    token_endpoint_auth_method=client.token_endpoint_auth_method,
                    owner_id=client.owner_id,
                    provider=client.provider,  # Migration 036
                    created_at=to_utc_iso(client.created_at) or "",
                    plaintext_secret=plaintext_secret,
                    is_visible=is_visible,
                    visibility_expires_at=to_utc_iso(client.visibility_expires_at),
                )
            )

        return response_list

    finally:
        db_session.close()


@router.post(
    "/clients", response_model=OAuth2ClientWithSecretResponse, status_code=status.HTTP_201_CREATED
)
async def create_oauth2_client(
    request: Request,
    data: OAuth2ClientCreateRequest,
    user: SessionUser,
) -> OAuth2ClientWithSecretResponse:
    """Register a new OAuth2 client.

    Args:
        data: Client registration data

    Returns:
        Created client with client_secret (only shown once)

    Example:
        POST /api/v1/oauth/clients
        {
          "client_name": "My ChatGPT App",
          "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"]
        }

    Security:
        - client_secret is returned ONLY ONCE on creation
        - client_secret is SHA256 hashed in database
        - User must save the secret immediately
    """
    # Get current user ID
    user_id = get_current_user_id(request)
    current_workspace_id = user.get("current_workspace_id")  # Issue #169, Migration 034

    db_session = get_sync_session()

    try:
        # Generate client_id and client_secret
        client_id = f"oauth_{secrets.token_urlsafe(16)}"
        client_secret = secrets.token_urlsafe(32)
        client_secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()

        # Migration 035: Encrypt plaintext for storage
        from datetime import timedelta

        from utils.encryption import get_encryptor

        plaintext_secret_encrypted = get_encryptor().encrypt(client_secret)
        visibility_expires_at = utcnow() + timedelta(minutes=10)  # 10 minutes

        # Migration 036: Provider from request (claude, chatgpt, custom)
        provider = data.provider

        # Create OAuth2Client (Migration 034: workspace-scoped)
        client = OAuth2Client(
            client_id=client_id,
            client_secret_hash=client_secret_hash,
            client_name=data.client_name,
            redirect_uris=data.redirect_uris,
            grant_types=data.grant_types,
            response_types=data.response_types,
            scope=data.scope,
            token_endpoint_auth_method=data.token_endpoint_auth_method,
            owner_id=user_id,
            workspace_id=str(current_workspace_id)
            if current_workspace_id
            else None,  # Migration 034
            provider=provider,  # Migration 036
            plaintext_secret_encrypted=plaintext_secret_encrypted,  # Migration 035
            visibility_expires_at=visibility_expires_at,  # Migration 034
        )

        db_session.add(client)
        db_session.commit()
        db_session.refresh(client)

        logger.info(
            "oauth2_client_created",
            client_id=client_id,
            client_name=data.client_name,
            owner_id=user_id,
        )

        # Return response with client_secret (only shown once)
        return OAuth2ClientWithSecretResponse(
            id=client.id,
            client_id=client.client_id,
            client_name=client.client_name,
            redirect_uris=client.redirect_uris,
            grant_types=client.grant_types,
            response_types=client.response_types,
            scope=client.scope,
            token_endpoint_auth_method=client.token_endpoint_auth_method,
            owner_id=client.owner_id,
            provider=client.provider,  # Migration 036
            created_at=to_utc_iso(client.created_at) or "",
            client_secret=client_secret,  # ⚠️ Only shown once!
            # Migration 034-035: Visibility fields
            plaintext_secret=client_secret,  # Same as client_secret
            is_visible=True,  # Newly created = visible
            visibility_expires_at=to_utc_iso(visibility_expires_at),
        )

    except Exception as e:
        db_session.rollback()
        logger.error("oauth2_client_create_failed", error=str(e), user_id=user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create OAuth2 client: {str(e)}",
        ) from e

    finally:
        db_session.close()


# --- Dynamic Client Registration (DCR) provider detection ----------------
#
# Issue #513: provider detection used to be a substring search on
# ``redirect_uris[0]`` (e.g. ``"chatgpt.com" in redirect_uri``). That blocks
# all RFC 8252 native-app clients (Claude Code CLI, Cursor CLI, etc.) which
# use ``http://localhost``/``http://127.0.0.1``/``http://[::1]`` loopback
# redirects, AND it accepts substring spoofs like
# ``https://attacker.com/?fake=chatgpt.com``. The new detection parses the
# URL and matches by hostname, with a ``client_name`` keyword fallback for
# loopback URIs (where the host alone cannot identify the provider).

# RFC 8252 §7.3 native-app loopback hosts.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Hostname suffixes that identify each pre-existing provider. A redirect_uri
# whose hostname equals one of these (or is a subdomain — ``host.endswith("." + s)``)
# maps to the provider.
_PROVIDER_HOSTNAMES: dict[str, tuple[str, ...]] = {
    "chatgpt": ("chatgpt.com", "chat.openai.com"),
    "claude": ("claude.ai", "anthropic.com"),
    "cursor": ("cursor.sh", "cursor.com"),
}

# For loopback URIs only — the hostname is uninformative, so substring-match
# the (NFKC-normalized, lowercased) ``client_name`` against the provider
# names. Derived from ``_PROVIDER_HOSTNAMES`` so adding a provider only
# requires editing one mapping. Stored as ``tuple`` (not ``frozenset``) so
# iteration order is deterministic — when a ``client_name`` contains more
# than one provider keyword (e.g. "Claude Cursor"), the first match in
# ``_PROVIDER_HOSTNAMES`` insertion order wins, which is reproducible across
# processes (frozenset iteration depends on hash randomization).
#
# ``client_name`` is a user-supplied trust signal: this is intentionally a
# soft check, paired with the existing rate limit (5/min/IP) and the
# ``token_endpoint_auth_method="none"`` + PKCE defaults — see issue #513
# Security note for the threat model.
_LOOPBACK_PROVIDER_KEYWORDS: tuple[str, ...] = tuple(_PROVIDER_HOSTNAMES)


def _normalize_client_name(client_name: str) -> str:
    """NFKC-normalize, strip Cf/Cc invisibles, and lowercase ``client_name``.

    NFKC alone collapses fullwidth homoglyphs (e.g. ``Ｃｌａｕｄｅ`` → ``Claude``)
    but leaves zero-width / format characters (Unicode category ``Cf``) and
    other invisibles (``Cc``) intact, which would let an attacker bypass the
    keyword substring check by inserting ``\\u200B`` (ZWSP) inside a provider
    name. Strip those before lowercasing to neutralize both attack shapes.
    """
    nfkc = unicodedata.normalize("NFKC", client_name)
    visible = "".join(c for c in nfkc if unicodedata.category(c) not in {"Cf", "Cc"})
    return visible.lower()


def detect_dcr_provider(redirect_uri: str, client_name: str) -> str:
    """Detect the DCR provider from ``redirect_uri`` (+ ``client_name`` for loopback).

    Returns one of ``"chatgpt"``, ``"claude"``, ``"cursor"``, or ``"custom"``.
    The caller rejects ``"custom"`` with an RFC 6749 §5.2 error response.

    Strategy:
        1. RFC 8252 loopback redirects (``http://localhost`` / ``127.0.0.1`` /
           ``[::1]``) → fall back to ``client_name`` keyword match (NFKC
           normalized, case-insensitive substring).
        2. Otherwise → match the parsed hostname (case-insensitive) against
           ``_PROVIDER_HOSTNAMES`` as exact host or single-suffix subdomain.

    Spec:
        RFC 8252 §7.3 — Loopback Interface Redirection.
        RFC 7591 §2 — DCR client metadata (``client_name``, ``redirect_uris``).
    """
    try:
        parsed = urlparse(redirect_uri)
    except (ValueError, AttributeError):
        return "custom"

    # Reject malformed authorities — ``urlparse`` is lenient and will happily
    # extract a clean ``hostname`` from inputs that have a junk port
    # (``http://chatgpt.com:443.evil.com/cb`` returns hostname ``"chatgpt.com"``)
    # or userinfo prefix (``http://attacker@chatgpt.com/cb``). A simple
    # hostname-suffix match would let those slip through and re-introduce
    # provider spoofing. Force a parse of the port to surface bad authorities,
    # and reject any redirect_uri that carries username/password — neither is
    # legitimate for the providers this DCR endpoint serves.
    if parsed.username is not None or parsed.password is not None:
        return "custom"
    try:
        _port = parsed.port  # raises ValueError on non-integer or out-of-range
    except ValueError:
        return "custom"
    del _port

    hostname = (parsed.hostname or "").lower()

    # 1) Loopback path (RFC 8252 native apps): http scheme + loopback host.
    if parsed.scheme == "http" and hostname in _LOOPBACK_HOSTS:
        normalized = _normalize_client_name(client_name)
        for keyword in _LOOPBACK_PROVIDER_KEYWORDS:
            if keyword in normalized:
                return keyword
        return "custom"

    # 2) Hostname suffix match for pre-existing providers. ``urlparse`` returns
    # ``None`` for inputs without a netloc (e.g. ``"not a url"``); the
    # ``or ""`` above turned that into an empty string — bail out early.
    if not hostname:
        return "custom"
    for provider, suffixes in _PROVIDER_HOSTNAMES.items():
        for suffix in suffixes:
            if hostname == suffix or hostname.endswith("." + suffix):
                return provider
    return "custom"


@router.post(
    "/register",
    response_model=OAuth2ClientWithSecretResponse,
    # RFC 7591 §3.2.1: DCR clients are always public
    # (``token_endpoint_auth_method="none"``); the spec forbids issuing a
    # ``client_secret`` to them. Returning even ``null`` lets some OAuth
    # client libraries Basic-Auth the token endpoint with an empty secret,
    # which authlib then rejects (Issue #689). Omit the field outright.
    response_model_exclude={"client_secret", "plaintext_secret"},
    status_code=status.HTTP_201_CREATED,
)
async def dynamic_client_registration(
    request: Request,
    data: DynamicClientRegistrationRequest,
) -> OAuth2ClientWithSecretResponse | JSONResponse:
    """Dynamic Client Registration (DCR) for MCP clients.

    Public endpoint for ChatGPT/Claude/Cursor to register themselves automatically.
    No authentication required - this follows MCP specification for DCR.

    Security controls:
    - Provider whitelist (chatgpt, claude, cursor only)
    - IP-based rate limiting (5 registrations per minute per IP)
    - Redirect URI pattern validation
    - Automatic token_endpoint_auth_method="none" (public clients)

    Args:
        request: FastAPI request (for IP-based rate limiting)
        data: DCR request data

    Returns:
        Created client metadata. Per RFC 7591 §3.2.1, public clients
        (``token_endpoint_auth_method="none"``) do NOT receive a
        ``client_secret`` — the field is omitted from the JSON response
        via ``response_model_exclude`` (Issue #689). Clients authenticate
        the token endpoint using PKCE alone.

    Example:
        POST /api/v1/oauth/register
        {
          "client_name": "ChatGPT MCP",
          "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
          "token_endpoint_auth_method": "none"
        }

    Spec:
        RFC 7591 - OAuth 2.0 Dynamic Client Registration Protocol
        RFC 7636 - PKCE (required for public clients)
        MCP Authorization Spec (2025-03-26)
    """
    # Get client IP for rate limiting
    client_ip = request.client.host if request.client else "unknown"

    # Check rate limit (5 registrations per minute per IP)
    rate_limit_key = f"dcr_rate_limit:{client_ip}"
    count = await increment_counter(rate_limit_key, ttl=60)

    if count > 5:
        logger.warning(
            "dcr_rate_limit_exceeded",
            ip=client_ip,
            count=count,
        )
        return rfc6749_error_response(
            error="invalid_request",
            description="Too many registration requests. Please try again later.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # Detect provider from redirect_uri (hostname suffix match) with a
    # client_name keyword fallback for RFC 8252 loopback redirects. See
    # ``detect_dcr_provider`` above for the rationale.
    redirect_uri = data.redirect_uris[0] if data.redirect_uris else ""
    detected_provider = detect_dcr_provider(redirect_uri, data.client_name)

    ALLOWED_PROVIDERS = ("chatgpt", "claude", "cursor")
    if detected_provider not in ALLOWED_PROVIDERS:
        logger.warning(
            "dcr_provider_rejected",
            ip=client_ip,
            redirect_uri=redirect_uri,
            detected_provider=detected_provider,
        )
        return rfc6749_error_response(
            error="invalid_client_metadata",
            description=(
                "Dynamic client registration is only allowed for: "
                f"{', '.join(ALLOWED_PROVIDERS)}. "
                "Native CLIs (RFC 8252) must use http://localhost, "
                "http://127.0.0.1, or http://[::1] with a recognized "
                "client_name (Claude / Cursor / ChatGPT)."
            ),
        )

    db_session = get_sync_session()

    try:
        # See route-decorator comment above for the RFC 7591 §3.2.1 rationale.
        # The sentinel ``client_secret_hash=""`` satisfies the NOT NULL column
        # without storing a forgeable hash — authlib short-circuits the secret
        # check on the "none" branch (kagura-memory 19adf25b).
        client_id = f"oauth_{secrets.token_urlsafe(16)}"

        scope = data.scope if data.scope else DCR_DEFAULT_SCOPE

        client = OAuth2Client(
            client_id=client_id,
            client_secret_hash="",  # Public client sentinel (Issue #689)
            client_name=data.client_name,
            redirect_uris=data.redirect_uris,
            grant_types=data.grant_types,
            response_types=data.response_types,
            scope=scope,
            token_endpoint_auth_method="none",  # Force public client for DCR
            owner_id=None,  # DCR clients have no owner
            workspace_id=None,  # Global clients
            provider=detected_provider,
            plaintext_secret_encrypted=None,  # Issue #689: no secret to store
            visibility_expires_at=None,
        )

        db_session.add(client)
        db_session.commit()
        db_session.refresh(client)

        logger.info(
            "dcr_client_registered",
            client_id=client_id,
            provider=detected_provider,
            ip=client_ip,
        )

        # RFC 7591 §3.2.1: public client → omit client_secret / plaintext_secret
        return OAuth2ClientWithSecretResponse(
            id=client.id,
            client_id=client.client_id,
            client_name=client.client_name,
            redirect_uris=client.redirect_uris,
            grant_types=client.grant_types,
            response_types=client.response_types,
            scope=client.scope,
            token_endpoint_auth_method=client.token_endpoint_auth_method,
            owner_id=client.owner_id,
            provider=client.provider,
            created_at=to_utc_iso(client.created_at) or "",
            client_secret=None,
            plaintext_secret=None,
            is_visible=False,
            visibility_expires_at=None,
        )

    except Exception as e:
        db_session.rollback()
        logger.error("dcr_registration_failed", error=str(e), ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to register client: {str(e)}",
        ) from e

    finally:
        db_session.close()


@router.get("/clients/{client_id}", response_model=OAuth2ClientResponse)
async def get_oauth2_client(
    request: Request,
    client_id: str,
    user: SessionUser,
) -> OAuth2ClientResponse:
    """Get OAuth2 client details.

    Args:
        client_id: OAuth2 client ID

    Returns:
        Client details (without secret)

    Example:
        GET /api/v1/oauth/clients/oauth_abc123
    """
    db_session = get_sync_session()

    try:
        client = db_session.query(OAuth2Client).filter_by(client_id=client_id).first()

        if not client:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth2 client not found: {client_id}",
            )

        # SECURITY: Check owner (Issue #93-3)
        current_user_id = get_current_user_id(request)
        if client.owner_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not allowed to access this client",
            )

        # Decrypt secret if visible + owner
        from utils.encryption import get_encryptor

        plaintext_secret = None
        is_visible = client.hidden_at is None

        if current_user_id == client.owner_id and is_visible and client.plaintext_secret_encrypted:
            try:
                plaintext_secret = get_encryptor().decrypt(client.plaintext_secret_encrypted)
            except Exception as e:
                logger.error(f"Failed to decrypt secret for client {client.client_id}: {e}")

        return OAuth2ClientResponse(
            id=client.id,
            client_id=client.client_id,
            client_name=client.client_name,
            redirect_uris=client.redirect_uris,
            grant_types=client.grant_types,
            response_types=client.response_types,
            scope=client.scope,
            token_endpoint_auth_method=client.token_endpoint_auth_method,
            owner_id=client.owner_id,
            provider=client.provider,
            created_at=to_utc_iso(client.created_at) or "",
            # Migration 034-035: Visibility fields
            plaintext_secret=plaintext_secret,
            is_visible=is_visible,
            visibility_expires_at=to_utc_iso(client.visibility_expires_at),
        )

    finally:
        db_session.close()


@router.put("/clients/{client_id}", response_model=OAuth2ClientResponse)
async def update_oauth2_client(
    request: Request,
    client_id: str,
    data: OAuth2ClientUpdateRequest,
    user: SessionUser,
) -> OAuth2ClientResponse:
    """Update OAuth2 client.

    Args:
        client_id: OAuth2 client ID
        data: Update data

    Returns:
        Updated client

    Example:
        PUT /api/v1/oauth/clients/oauth_abc123
        {
          "client_name": "My App (Updated)",
          "redirect_uris": ["https://example.com/callback"]
        }

    Note:
        - client_secret cannot be updated (create new client if needed)
        - Can update: name, redirect_uris, scope, token_endpoint_auth_method
    """
    db_session = get_sync_session()

    try:
        client = db_session.query(OAuth2Client).filter_by(client_id=client_id).first()

        if not client:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth2 client not found: {client_id}",
            )

        # Update fields
        if data.client_name is not None:
            client.client_name = data.client_name

        if data.redirect_uris is not None:
            client.redirect_uris = data.redirect_uris

        if data.scope is not None:
            client.scope = data.scope

        if data.token_endpoint_auth_method is not None:
            client.token_endpoint_auth_method = data.token_endpoint_auth_method

        db_session.commit()
        db_session.refresh(client)

        logger.info("oauth2_client_updated", client_id=client_id)

        return OAuth2ClientResponse(
            id=client.id,
            client_id=client.client_id,
            client_name=client.client_name,
            redirect_uris=client.redirect_uris,
            grant_types=client.grant_types,
            response_types=client.response_types,
            scope=client.scope,
            token_endpoint_auth_method=client.token_endpoint_auth_method,
            owner_id=client.owner_id,
            provider=client.provider,
            created_at=to_utc_iso(client.created_at) or "",
            # Migration 034-035: Visibility fields (no secret on update)
            plaintext_secret=None,
            is_visible=client.hidden_at is None,
            visibility_expires_at=to_utc_iso(client.visibility_expires_at),
        )

    except HTTPException:
        db_session.rollback()
        raise

    except Exception as e:
        db_session.rollback()
        logger.error("oauth2_client_update_failed", client_id=client_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update OAuth2 client: {str(e)}",
        ) from e

    finally:
        db_session.close()


@router.post("/clients/{client_id}/hide")
async def hide_oauth2_client_secret(
    request: Request,
    client_id: str,
    user: SessionUser,
) -> dict:
    """Hide OAuth2 client secret (Owner only).

    Sets hidden_at timestamp and deletes plaintext_secret_encrypted.

    Args:
        client_id: OAuth2 client ID

    Returns:
        Status message
    """
    db_session = get_sync_session()

    try:
        client = db_session.query(OAuth2Client).filter_by(client_id=client_id).first()

        if not client:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth2 client not found: {client_id}",
            )

        # SECURITY: Check owner
        current_user_id = get_current_user_id(request)
        if client.owner_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only owner can hide OAuth2 client secret",
            )

        # Hide secret

        client.hidden_at = utcnow()
        client.visibility_expires_at = None  # Cancel auto-hide
        client.plaintext_secret_encrypted = None  # Delete encrypted secret

        db_session.commit()

        logger.info("oauth2_client_secret_hidden", client_id=client_id, owner_id=current_user_id)

        return {"status": "hidden", "client_id": client_id}

    except HTTPException:
        db_session.rollback()
        raise

    except Exception as e:
        db_session.rollback()
        logger.error("oauth2_client_hide_failed", client_id=client_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to hide OAuth2 client secret: {str(e)}",
        ) from e

    finally:
        db_session.close()


@router.post(
    "/clients/{client_id}/regenerate-secret", response_model=OAuth2ClientWithSecretResponse
)
async def regenerate_oauth2_client_secret(
    request: Request,
    client_id: str,
    user: SessionUser,
) -> OAuth2ClientWithSecretResponse:
    """Regenerate OAuth2 client secret.

    Issue #169: Secret regeneration feature.

    WARNING: This immediately invalidates the old secret. Update all applications.

    Args:
        client_id: OAuth2 client ID

    Returns:
        Updated client with new client_secret (only shown once)

    Example:
        POST /api/v1/oauth/clients/oauth_abc123/regenerate-secret
    """
    db_session = get_sync_session()

    try:
        client = db_session.query(OAuth2Client).filter_by(client_id=client_id).first()

        if not client:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth2 client not found: {client_id}",
            )

        # SECURITY: Check owner (Issue #93-3)
        current_user_id = get_current_user_id(request)
        if client.owner_id != current_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not allowed to regenerate this client's secret",
            )

        # Generate new secret
        new_client_secret = secrets.token_urlsafe(32)
        new_client_secret_hash = hashlib.sha256(new_client_secret.encode()).hexdigest()

        # Migration 035: Encrypt plaintext for storage
        from datetime import timedelta

        from utils.encryption import get_encryptor

        plaintext_secret_encrypted = get_encryptor().encrypt(new_client_secret)

        # Update client
        client.client_secret_hash = new_client_secret_hash
        client.plaintext_secret_encrypted = plaintext_secret_encrypted  # Migration 035
        client.hidden_at = None  # Make visible
        client.visibility_expires_at = utcnow() + timedelta(minutes=10)  # 10 minutes

        db_session.commit()
        db_session.refresh(client)

        logger.info(
            "oauth2_client_secret_regenerated",
            client_id=client_id,
            owner_id=current_user_id,
        )

        # Return response with new client_secret (only shown once)
        return OAuth2ClientWithSecretResponse(
            id=client.id,
            client_id=client.client_id,
            client_name=client.client_name,
            redirect_uris=client.redirect_uris,
            grant_types=client.grant_types,
            response_types=client.response_types,
            scope=client.scope,
            token_endpoint_auth_method=client.token_endpoint_auth_method,
            owner_id=client.owner_id,
            provider=client.provider,  # Migration 036
            created_at=to_utc_iso(client.created_at) or "",
            client_secret=new_client_secret,  # New secret - only shown once!
            # Migration 034-035: Visibility fields
            plaintext_secret=new_client_secret,  # Same as client_secret
            is_visible=True,  # Newly regenerated = visible
            visibility_expires_at=to_utc_iso(client.visibility_expires_at),
        )

    except HTTPException:
        db_session.rollback()
        raise

    except Exception as e:
        db_session.rollback()
        logger.error("oauth2_client_secret_regenerate_failed", client_id=client_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to regenerate client secret: {str(e)}",
        ) from e

    finally:
        db_session.close()


@router.delete("/clients/{client_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_oauth2_client(
    request: Request,
    client_id: str,
    user: SessionUser,
):
    """Delete OAuth2 client.

    Args:
        client_id: OAuth2 client ID

    Returns:
        No content

    Example:
        DELETE /api/v1/oauth/clients/oauth_abc123

    Note:
        - Deleting a client will CASCADE delete all associated tokens
        - This action cannot be undone
    """
    db_session = get_sync_session()

    try:
        client = db_session.query(OAuth2Client).filter_by(client_id=client_id).first()

        if not client:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth2 client not found: {client_id}",
            )

        db_session.delete(client)
        db_session.commit()

        logger.info("oauth2_client_deleted", client_id=client_id)

    except HTTPException:
        db_session.rollback()
        raise

    except Exception as e:
        db_session.rollback()
        logger.error("oauth2_client_delete_failed", client_id=client_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete OAuth2 client: {str(e)}",
        ) from e

    finally:
        db_session.close()


# ============================================================================
# OAuth2 Provider Management Endpoints
# ============================================================================


@router.get("/providers", response_model=list[OAuth2ProviderResponse])
async def list_oauth2_providers(
    request: Request,
    admin: dict = Depends(require_admin),
) -> list[OAuth2ProviderResponse]:
    """List configured OAuth2 providers.

    Returns:
        List of OAuth2 providers (Google, GitHub, etc.)

    Example:
        GET /api/v1/oauth/providers

    Note:
        This endpoint returns OAuth2 providers configured in .env file
        for user login (not OAuth2 clients registered by users).
    """
    import os

    providers = []

    # Google OAuth2 Provider
    google_client_id = os.getenv("GOOGLE_CLIENT_ID")
    if google_client_id:
        providers.append(
            OAuth2ProviderResponse(
                name="google",
                display_name="Google",
                client_id=google_client_id,
                authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
                token_url="https://oauth2.googleapis.com/token",
                scopes=["openid", "email", "profile"],
                enabled=True,
                configured=True,
            )
        )

    # GitHub OAuth2 Provider
    github_client_id = os.getenv("GITHUB_CLIENT_ID")
    if github_client_id:
        providers.append(
            OAuth2ProviderResponse(
                name="github",
                display_name="GitHub",
                client_id=github_client_id,
                authorization_url="https://github.com/login/oauth/authorize",
                token_url="https://github.com/login/oauth/access_token",
                scopes=["read:user", "user:email"],
                enabled=True,
                configured=True,
            )
        )

    # Azure AD OAuth2 Provider (optional)
    azure_client_id = os.getenv("AZURE_CLIENT_ID")
    if azure_client_id:
        azure_tenant_id = os.getenv("AZURE_TENANT_ID", "common")
        providers.append(
            OAuth2ProviderResponse(
                name="azure",
                display_name="Microsoft Azure AD",
                client_id=azure_client_id,
                authorization_url=f"https://login.microsoftonline.com/{azure_tenant_id}/oauth2/v2.0/authorize",
                token_url=f"https://login.microsoftonline.com/{azure_tenant_id}/oauth2/v2.0/token",
                scopes=["openid", "email", "profile"],
                enabled=True,
                configured=True,
            )
        )

    return providers


# ============================================================================
# OAuth2 Authorization Flow Endpoints (Phase 1.4 Part B)
# ============================================================================


# Helper function: Get current user from session
def get_current_user_from_session(request: Request) -> _OAuthUser | None:
    """Get authenticated user from SessionMiddleware.

    Args:
        request: FastAPI request with request.state.user

    Returns:
        _OAuthUser or None if not authenticated
    """
    if not hasattr(request.state, "user") or not request.state.user:
        return None

    user_data = request.state.user

    if isinstance(user_data, dict):
        return _OAuthUser(
            user_id=user_data.get("user_id") or user_data.get("sub") or user_data.get("email"),
            email=user_data.get("email"),
        )
    else:
        return _OAuthUser(
            user_id=getattr(user_data, "user_id", None) or getattr(user_data, "email", None),
            email=getattr(user_data, "email", None),
        )


def _append_query_params(url: str, extra: dict[str, str]) -> str:
    """Merge query params into ``url``, with ``extra`` winning on collisions.

    Issue #218 (PR review, round 1): the OAuth deny / error redirect
    paths used to do ``f"{redirect_uri}?{urlencode(params)}"`` which
    produces a malformed URL with a double ``?`` whenever the registered
    ``redirect_uri`` already carries a query string. RFC 6749 §3.1.2
    explicitly allows query strings on exact-match redirect URIs, and
    ``is_valid_redirect_uri_pattern`` accepts them, so this is reachable
    in production. The bug silently breaks the OAuth client's view of
    ``error=access_denied`` (it ends up nested inside the existing
    query value).

    Issue #218 (PR review, round 2): on top of the double-``?`` fix,
    collisions between pre-existing keys and ``extra`` must be resolved
    in favour of ``extra``. OAuth response params (``error``, ``state``,
    ``error_description``) are the *authoritative* value for that
    response; if a registered redirect_uri happened to carry a baked-in
    ``state=old`` or similar, duplicating the key would let many clients
    pick the first occurrence and silently ignore the one we just set.

    Args:
        url: Base URL — may already contain a query string and/or fragment.
        extra: Mapping of additional query parameters. Keys in ``extra``
            override any existing parameters with the same name.

    Returns:
        URL with ``extra`` merged into the existing query string using
        ``&`` as the separator. Fragment is preserved.
    """
    parts = urlsplit(url)
    override_keys = set(extra)
    merged = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in override_keys
    ]
    merged.extend(extra.items())
    return urlunsplit(parts._replace(query=urlencode(merged)))


def _redact_redirect_uri_for_log(redirect_uri: str | None) -> str:
    """Reduce a redirect_uri to a safe form for logging.

    Issue #218 (Copilot review): warning logs used to echo the full,
    attacker-controlled ``redirect_uri``. Even validated URIs may carry
    sensitive query strings (RFC 6749 §3.1.2 allows them on exact-match
    patterns), and unvalidated probes can be arbitrarily long or contain
    log-injection payloads. We log scheme + host + path only, capped to
    256 characters.

    Args:
        redirect_uri: The URI to redact, or ``None``.

    Returns:
        ``"<missing>"`` if the input is empty, otherwise
        ``"{scheme}://{netloc}{path}"`` truncated to 256 chars. Falls back
        to a truncated raw value if URL parsing fails.
    """
    if not redirect_uri:
        return "<missing>"
    try:
        parts = urlsplit(redirect_uri)
        if parts.scheme and parts.netloc:
            redacted = f"{parts.scheme}://{parts.netloc}{parts.path}"
        else:
            redacted = redirect_uri
    except ValueError:
        redacted = redirect_uri
    if len(redacted) > 256:
        redacted = redacted[:253] + "..."
    return redacted


def _resolve_oauth_locale(request: Request, db_session, user_email: str | None) -> str:
    """Resolve the locale for OAuth authorize/error pages.

    Priority: ``?locale=`` query param > ``User.locale`` > ``Accept-Language``
    header > ``"en"``. ``db_session`` is the sync OAuth session and may be
    queried for the user's stored preference.

    Args:
        request: FastAPI request whose query params and headers are inspected.
        db_session: Synchronous SQLAlchemy session used for the optional
            ``User.locale`` lookup. May be ``None`` to skip the DB lookup.
        user_email: Email of the authenticated user, or ``None`` if not
            available. Used as the lookup key for ``User.locale``.

    Returns:
        Locale code such as ``"en"`` or ``"ja"``. Always returns a non-empty
        string.
    """
    locale = request.query_params.get("locale")
    if not locale and db_session is not None and user_email:
        db_user = db_session.query(User).filter_by(email=user_email).first()
        if db_user and db_user.locale:
            locale = db_user.locale
    if not locale:
        accept_lang = request.headers.get("Accept-Language", "")
        if accept_lang.startswith("ja"):
            locale = "ja"
    if not locale:
        locale = "en"
    return locale


def _validate_authorize_redirect_uri(
    db_session,
    client_id: str | None,
    redirect_uri: str | None,
) -> bool:
    """Return ``True`` iff the client exists and accepts ``redirect_uri``.

    Issue #218: Used by both GET and POST ``/authorize`` to enforce
    redirect_uri registration *before* the consent UI is rendered or any
    303 redirect is issued. The lookup is intentionally tolerant of missing
    inputs (returning ``False``) so that callers can route every failure
    through the same error path.

    Args:
        db_session: Synchronous SQLAlchemy session.
        client_id: ``client_id`` from the request, or ``None``.
        redirect_uri: Incoming ``redirect_uri`` from the request, or ``None``.

    Returns:
        ``True`` if the client is registered and the redirect_uri matches at
        least one of its registered patterns, ``False`` otherwise.
    """
    if not client_id or not redirect_uri:
        return False
    client = db_session.query(OAuth2Client).filter_by(client_id=client_id).first()
    if not client:
        return False
    return client.check_redirect_uri(redirect_uri)


def _render_invalid_redirect_uri_error(
    request: Request,
    locale: str,
    incoming_redirect_uri: str | None,
) -> HTMLResponse:
    """Render an HTML error page for an unregistered ``redirect_uri``.

    Issue #218: When the incoming ``redirect_uri`` does not match any pattern
    registered with the OAuth client, both GET and POST ``/authorize`` must
    refuse to render the consent screen *and* refuse to 303-redirect to the
    untrusted URI. RFC 6749 §4.1.2.1 explicitly forbids redirecting in this
    case — the authorization server must inform the resource owner directly.

    The page also closes a phishing rendering gadget: without this guard,
    an attacker could craft a link to ``/authorize`` with a legitimate
    ``client_id`` and a hostile ``redirect_uri``; Kagura would render the
    real consent screen (with the legitimate client name) and only fail at
    POST time, normalising "Authorize on kagura-ai.com for unknown sites".

    Args:
        request: FastAPI request, used by Starlette's ``TemplateResponse``.
        locale: Resolved locale code (``"en"``/``"ja"``).
        incoming_redirect_uri: The offending URI from the request, shown to
            the user verbatim (Jinja2 auto-escapes it). May be ``None``.

    Returns:
        ``HTMLResponse`` with ``status_code=400`` and the rendered error
        template.
    """
    messages = get_oauth_messages(locale)
    return templates.TemplateResponse(
        request,
        "oauth_authorize_error.html",
        {
            "messages": messages,
            "locale": locale,
            "redirect_uri": incoming_redirect_uri or "",
        },
        status_code=400,
    )


@router.get("/authorize", response_class=HTMLResponse)
async def oauth_authorize_get(
    request: Request,
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    response_type: str = Query("code"),
    scope: str | None = Query(None),
    state: str | None = Query(None),
    resource: str | None = Query(None),  # RFC 8707 (Issue #157)
    code_challenge: str | None = Query(None),  # PKCE (RFC 7636)
    code_challenge_method: str | None = Query(None),  # PKCE (RFC 7636)
):
    """OAuth2 authorization endpoint (consent screen).

    Issue #157: Save resource parameter for POST request.
    PKCE (RFC 7636): Save code_challenge for public clients (ChatGPT/Claude).
    """
    logger.info(
        "oauth_authorize_get",
        client_id=client_id,
        state=state,
        resource=resource,
        pkce_present=bool(code_challenge),
        code_challenge_method=code_challenge_method,
    )

    user = get_current_user_from_session(request)

    if not user:
        # Redirect to login
        from urllib.parse import quote

        return_to = quote(str(request.url), safe="")
        frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
        return RedirectResponse(f"{frontend_url}/login?return_to={return_to}")

    # Save resource parameter to Redis for POST request (Issue #157)
    logger.info(
        f"Checking resource preservation: resource={resource}, state={state}, condition={resource and state}"
    )
    if state:
        from redis import Redis

        from config.database import get_redis_url

        redis_url = get_redis_url()
        redis = Redis.from_url(redis_url, decode_responses=True)

        # Save resource (RFC 8707)
        if resource:
            redis.setex(f"oauth_state:{state}:resource", 300, resource)  # 5 min TTL
            logger.info(f"Saved resource to Redis: state={state}, resource={resource}")

        # Save PKCE parameters (RFC 7636) for public clients (ChatGPT/Claude)
        if code_challenge:
            redis.setex(f"oauth_state:{state}:code_challenge", 300, code_challenge)
            if code_challenge_method:
                redis.setex(
                    f"oauth_state:{state}:code_challenge_method", 300, code_challenge_method
                )
            logger.info(f"Saved PKCE to Redis: state={state}, method={code_challenge_method}")
    else:
        logger.warning("State not provided, cannot save OAuth params")

    db_session = get_sync_session()
    try:
        client = db_session.query(OAuth2Client).filter_by(client_id=client_id).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        # Issue #221: Detect locale for i18n
        locale = _resolve_oauth_locale(request, db_session, user.email)

        # Issue #218: Reject unregistered redirect_uri *before* rendering the
        # consent screen. Without this guard, an attacker can craft a link
        # with a legitimate client_id and a hostile redirect_uri; Kagura
        # would render the real consent UI (with the real client name) and
        # only fail at POST time. That turns the consent page itself into a
        # phishing rendering gadget. RFC 6749 §4.1.2.1 also forbids
        # redirecting on invalid redirect_uri — the AS must inform the
        # resource owner directly, which is what the error template does.
        if not client.check_redirect_uri(redirect_uri):
            logger.warning(
                "oauth_authorize_get_rejected_redirect_uri: "
                f"client_id={client_id!r}, "
                f"redirect_uri={_redact_redirect_uri_for_log(redirect_uri)!r}"
            )
            return _render_invalid_redirect_uri_error(request, locale, redirect_uri)

        # Get i18n messages
        messages = get_oauth_messages(locale)

        # Build query string for POST action (maintain OAuth2 params in query)
        query_params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
        }
        if scope:
            query_params["scope"] = scope
        if state:
            query_params["state"] = state
        if resource:
            query_params["resource"] = resource  # RFC 8707 (Issue #157)
        if code_challenge:
            query_params["code_challenge"] = code_challenge  # PKCE (RFC 7636)
        if code_challenge_method:
            query_params["code_challenge_method"] = code_challenge_method  # PKCE (RFC 7636)

        query_string = urlencode(query_params)

        # Use Jinja2 template (Issue #52, #221 i18n).
        # Modern Starlette TemplateResponse takes ``request`` as the first
        # positional argument; the legacy form
        # ``TemplateResponse(name, context_with_request)`` causes
        # ``TypeError: unhashable type: 'dict'`` deep inside Jinja2's cache
        # because Starlette interprets the dict as the template name.
        return templates.TemplateResponse(
            request,
            "oauth_authorize.html",
            {
                "client_name": client.client_name,
                "user_email": user.email,
                "query_string": query_string,
                "locale": locale,
                "messages": messages,
            },
        )
    finally:
        db_session.close()


def _run_oauth_sync(action: str, request, **kwargs):
    """Run an OAuth2 server operation in a synchronous session.

    Shared helper for ``_handle_authorize_sync`` and ``_handle_token_sync``.
    Creates/closes the session internally so callers don't manage lifecycle.

    Any exception propagates to FastAPI's default handler which returns a
    plain-text 500 (``Internal Server Error``) — but without a logged
    traceback we can't see WHERE the failure originated. Log with
    ``logger.exception(...)`` here so the next intermittent failure leaves
    a stack trace in api-blue stdout for post-mortem (Issue #635).
    """
    db_session = get_sync_session()
    try:
        server = create_authorization_server(db_session)
        result = getattr(server, action)(request, **kwargs)
        db_session.commit()
        return result
    except Exception:
        db_session.rollback()
        logger.exception("oauth_sync_failed", action=action)
        raise
    finally:
        db_session.close()


@router.post("/authorize")
async def oauth_authorize_post(
    request: Request,
):
    """Process authorization consent.

    OAuth2 params (client_id, redirect_uri, etc.) must be in query string,
    not in POST body. This is required by Authlib 1.6.5 which generates
    payload from query params for authorization endpoint.
    """
    import asyncio

    # Preload form data manually
    await preload_form(request)

    user = get_current_user_from_session(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Get confirm from preloaded form data
    confirm = request.state.form_data.get("confirm")

    # Get OAuth2 params from query
    client_id = request.query_params.get("client_id")
    redirect_uri = request.query_params.get("redirect_uri")
    state = request.query_params.get("state")

    # Issue #218: Pre-validate redirect_uri before *any* downstream branch
    # can act on it. Without this, the deny path and the exception handlers
    # below would 303-redirect to an attacker-controlled URI even when the
    # GET pre-check has already refused to render the consent screen — a
    # CWE-601 Open Redirect that turns /authorize into a phishing pivot.
    # RFC 6749 §4.1.2.1 forbids redirecting on invalid redirect_uri; we
    # render the error page directly instead.
    pre_check_session = get_sync_session()
    try:
        if not _validate_authorize_redirect_uri(pre_check_session, client_id, redirect_uri):
            logger.warning(
                "oauth_authorize_post_rejected_redirect_uri: "
                f"client_id={client_id!r}, "
                f"redirect_uri={_redact_redirect_uri_for_log(redirect_uri)!r}"
            )
            locale = _resolve_oauth_locale(request, pre_check_session, user.email)
            return _render_invalid_redirect_uri_error(request, locale, redirect_uri)
    finally:
        pre_check_session.close()

    if confirm != "yes":
        params = {"error": "access_denied"}
        if state:
            params["state"] = state
        return RedirectResponse(
            _append_query_params(redirect_uri, params),
            status_code=303,  # See Other: POST→GET redirect
        )

    # Run Authlib operations in thread pool to avoid blocking event loop
    try:
        response = await asyncio.to_thread(
            _run_oauth_sync, "create_authorization_response", request, grant_user=user
        )

        # Use 303 See Other to convert POST to GET redirect
        # Claude.ai callback expects GET, not POST
        if hasattr(response, "location") and response.location:
            return RedirectResponse(response.location, status_code=303)

        # No location - should not happen in normal flow
        raise HTTPException(500, "No redirect location in OAuth response")

    except Exception as e:
        import traceback

        from authlib.oauth2.rfc6749.errors import OAuth2Error

        # Log full traceback for debugging
        tb = traceback.format_exc()
        logger.error(f"oauth_authorize_failed: {type(e).__name__}: {e}\n{tb}")

        # Issue #218 (PR review, round 2): by the time we get here, the
        # upfront pre-check has already guaranteed ``redirect_uri`` is
        # a non-empty, registered URI — every earlier failure mode was
        # routed through ``_render_invalid_redirect_uri_error`` and
        # returned before entering this try block. The legacy
        # ``if redirect_uri: ... else: inline-HTML / raise`` fallbacks
        # here are therefore dead code; dropping them removes an
        # unreachable inline HTML response path and keeps the error
        # flow uniform (always redirect to the registered URI with
        # OAuth error params, per RFC 6749 §4.1.2.1).

        # OAuth2Error → redirect with structured error params.
        if isinstance(e, OAuth2Error):
            params = {"error": e.error}
            if hasattr(e, "description") and e.description:
                params["error_description"] = e.description
            if state:
                params["state"] = state
            return RedirectResponse(_append_query_params(redirect_uri, params), status_code=303)

        # Generic exception → redirect with server_error.
        params = {"error": "server_error", "error_description": str(e)}
        if state:
            params["state"] = state
        return RedirectResponse(_append_query_params(redirect_uri, params), status_code=303)


@router.post("/token", include_in_schema=False)
@router.post("/token/")
async def oauth_token(request: Request):
    """OAuth2 token endpoint.

    Accepts authorization_code or refresh_token grants.
    Issue #157: Support public clients (no client_secret) with PKCE.
    Returns Authlib response with proper status code and headers.
    """
    import asyncio
    import json

    from fastapi.responses import JSONResponse, Response

    # Log incoming request for debugging (Issue #157)
    logger.info(
        f"POST /token HIT! Path: {request.url.path}, From: {request.client.host if hasattr(request, 'client') else 'unknown'}"
    )

    # Preload form data manually
    await preload_form(request)

    # Debug: log form data
    logger.info("token_request_form", form=request.state.form_data)

    # Run Authlib operations in thread pool to avoid blocking event loop.
    # Traceback is captured inside _run_oauth_sync; this wrapper only shapes
    # the response so an unhandled exception in the Authlib stack returns an
    # RFC 6749 server_error JSON instead of Starlette's plain-text 500.
    try:
        authlib_resp = await asyncio.to_thread(_run_oauth_sync, "create_token_response", request)
    except Exception:
        return rfc6749_error_response(
            error="server_error",
            description="internal authorization server error",
            status_code=500,
        )

    # Extract status, body, headers from Authlib response
    if isinstance(authlib_resp, tuple) and len(authlib_resp) == 3:
        status_code, body, headers = authlib_resp
        headers_dict = dict(headers) if headers else {}

        logger.info("token_response", status=status_code, body_type=type(body).__name__)

        if isinstance(body, dict):
            return JSONResponse(content=body, status_code=status_code, headers=headers_dict)

        # body is str/bytes
        content = body if isinstance(body, (str, bytes)) else str(body)
        return Response(
            content=content,
            status_code=status_code,
            headers=headers_dict,
            media_type=headers_dict.get("content-type", "application/json"),
        )

    # Response-like object
    status_code = getattr(authlib_resp, "status_code", 200)
    headers_dict = dict(getattr(authlib_resp, "headers", []))
    body = getattr(authlib_resp, "body", {})

    logger.info("token_response_obj", status=status_code, has_body=bool(body))

    if isinstance(body, dict):
        return JSONResponse(content=body, status_code=status_code, headers=headers_dict)

    if isinstance(body, (str, bytes)):
        content = body
    else:
        content = json.dumps(body) if body else "{}"

    return Response(
        content=content,
        status_code=status_code,
        headers=headers_dict,
        media_type=headers_dict.get("content-type", "application/json"),
    )


# ============================================================================
# Device Authorization Grant Endpoints (RFC 8628, Issue #536)
# ============================================================================


@router.post("/device/authorize", response_model=DeviceAuthorizationResponse)
async def device_authorize(body: DeviceAuthorizationRequest) -> DeviceAuthorizationResponse:
    """Device Authorization endpoint (RFC 8628 Section 3.1).

    Called by CLI clients to obtain a device_code + user_code pair.
    No authentication required — this is a public endpoint.
    """
    settings = get_settings()
    db_session = get_sync_session()

    try:
        client = db_session.query(OAuth2Client).filter_by(client_id=body.client_id).first()
        if not client:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unknown client_id",
            )

        device_code = secrets.token_urlsafe(32)
        user_code = generate_user_code()

        scope = client.get_allowed_scope(body.scope or "")

        expires_at_val = utcnow() + timedelta(seconds=settings.oauth_device_code_expires_in)

        device = OAuth2DeviceCode(
            device_code=device_code,
            user_code=user_code,
            client_id=client.client_id,
            scope=scope,
            expires_at=expires_at_val,
        )
        db_session.add(device)
        db_session.commit()

        verification_uri = f"{settings.frontend_url}/device"
        verification_uri_complete = f"{verification_uri}?user_code={user_code}"

        logger.info(
            "device_authorization_created",
            client_id=body.client_id,
            device_code_prefix=device_code[:8],
        )

        return DeviceAuthorizationResponse(
            device_code=device_code,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=verification_uri_complete,
            expires_in=settings.oauth_device_code_expires_in,
            interval=settings.oauth_device_polling_interval,
        )

    except HTTPException:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error("device_authorize_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create device authorization",
        ) from e
    finally:
        db_session.close()


@router.post("/device/verify", response_model=DeviceVerifyResponse)
async def device_verify(body: DeviceVerifyRequest) -> DeviceVerifyResponse:
    """Look up a pending device authorization by user_code.

    Returns enough information for the browser consent screen to render
    (client_name, scope). No authentication required — possession of the
    user_code is the bearer token for this lookup.
    """
    db_session = get_sync_session()

    try:
        device = (
            db_session.query(OAuth2DeviceCode).filter_by(user_code=body.user_code.upper()).first()
        )

        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invalid user code",
            )

        client = db_session.query(OAuth2Client).filter_by(client_id=device.client_id).first()
        client_name = client.client_name if client else "Unknown"

        is_expired = device.is_expired()
        is_authorized = (
            not is_expired and device.authorized_at is not None and device.user_id is not None
        )

        return DeviceVerifyResponse(
            user_code=device.user_code,
            client_name=client_name,
            scope=device.scope,
            expires_at=to_utc_iso(device.expires_at) or "",
            is_authorized=is_authorized,
            is_expired=is_expired,
        )

    except HTTPException:
        raise
    finally:
        db_session.close()


@router.post(
    "/device/audit-unauth",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False,
)
async def device_audit_unauth(request: Request, body: DeviceUnauthAuditRequest) -> Response:
    """Audit-log unauthenticated /device page hits for monitoring (Issue #779).

    Called fire-and-forget by the frontend auth guard BEFORE redirecting an
    unauthenticated user away from /device. Logs prefix-only user_code, IP,
    User-Agent, and UTC timestamp via structlog.

    Rate-limited per-IP (30/min) to prevent log spam. On overflow, the request
    silently drops the audit entry but still returns 204 — the client never
    learns the rate-limit state.

    No authentication required: the act of being on the unauth /device page
    is itself the signal we want to record.
    """
    client_ip = request.client.host if request.client else "unknown"

    rate_limit_key = f"device_unauth_audit:{client_ip}"
    try:
        count = await increment_counter(rate_limit_key, ttl=60)
    except Exception as e:  # noqa: BLE001 - fire-and-forget: must never 500
        # Redis outage degrades audit observability but must not break the
        # /device redirect flow that pings this endpoint.
        logger.warning(
            "device_unauth_audit_redis_failure",
            ip=client_ip,
            error=str(e),
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if count > _DEVICE_UNAUTH_AUDIT_RATE_LIMIT:
        logger.warning(
            "device_unauth_audit_rate_limited",
            ip=client_ip,
            count=count,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    user_agent = request.headers.get("user-agent", "unknown")
    logger.info(
        "device_unauth_hit",
        user_code_prefix=body.user_code_prefix,
        ip=client_ip,
        user_agent=user_agent,
        timestamp_utc=to_utc_iso(utcnow()),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _get_user_from_session(request: Request) -> dict | None:
    """Extract user info from session (delegates to get_current_user_from_session)."""
    user_stub = get_current_user_from_session(request)
    if user_stub is None:
        return None
    return {"user_id": user_stub.user_id, "email": user_stub.email}


@router.post("/device/confirm", response_model=DeviceConfirmResponse)
async def device_confirm(
    request: Request,
    body: DeviceConfirmRequest,
) -> DeviceConfirmResponse:
    """User consent endpoint for device authorization.

    Requires session authentication. Sets authorized_at or denied_at on the
    device code record so the polling CLI receives the decision.
    """
    user = _get_user_from_session(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    db_session = get_sync_session()

    try:
        device = (
            db_session.query(OAuth2DeviceCode)
            .filter_by(user_code=body.user_code.upper())
            .with_for_update()
            .first()
        )

        if not device:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invalid user code",
            )

        if device.is_expired():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="This code has expired",
            )

        if device.authorized_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This code has already been authorized",
            )

        if device.denied_at is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This code has already been denied",
            )

        if body.approve:
            user_id = user.get("user_id") or user.get("sub")
            if not user_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User ID not found in session",
                )
            device.user_id = user_id
            device.authorized_at = utcnow()
            status_str = "approved"
        else:
            device.denied_at = utcnow()
            status_str = "denied"

        db_session.commit()

        logger.info(
            "device_authorization_" + status_str,
            user_code=body.user_code,
            user_id=device.user_id,
        )

        return DeviceConfirmResponse(status=status_str, user_code=body.user_code)

    except HTTPException:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error("device_confirm_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process device authorization",
        ) from e
    finally:
        db_session.close()


@router.post("/introspect", response_model=TokenIntrospectionResponse)
async def introspect_token(
    request: Request,
    token: str = Form(..., description="Access token to introspect"),
    db: AsyncSession = Depends(get_db),
) -> TokenIntrospectionResponse:
    """Token Introspection endpoint (RFC 7662).

    Issue #157: MCP SDK compliance - Token Introspection

    Allows Resource Servers to validate access tokens.

    Args:
        request: FastAPI request (for caller IP logging)
        token: Access token to introspect

    Returns:
        Token metadata if active, or {\"active\": False}

    Example:
        POST /api/v1/oauth/introspect
        Content-Type: application/x-www-form-urlencoded

        token=mcp_abc123...

        Response (active):
        {
            \"active\": true,
            \"client_id\": \"oauth__...\",
            \"scope\": \"memory:read memory:write\",
            \"exp\": 1733414400,
            \"aud\": \"https://your-domain.com/mcp\"
        }

        Response (inactive):
        {\"active\": false}

    Spec: RFC 7662 - OAuth 2.0 Token Introspection
    https://datatracker.ietf.workspace/doc/html/rfc7662
    """
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    caller_ip = request.client.host if request.client else "unknown"
    token_prefix = token[:8] + "..." if token else "(empty)"

    try:
        # Look up token in database
        result = await db.execute(select(OAuth2Token).where(OAuth2Token.access_token == token))
        oauth_token = result.scalar_one_or_none()

        if not oauth_token:
            logger.info(
                "oauth_introspect",
                caller_ip=caller_ip,
                token_prefix=token_prefix,
                active=False,
                reason="not_found",
            )
            return TokenIntrospectionResponse(active=False)

        # Determine introspection result
        is_expired = oauth_token.is_expired()
        is_revoked = oauth_token.is_revoked()
        active = not is_expired and not is_revoked
        reason = "expired" if is_expired else "revoked" if is_revoked else None

        logger.info(
            "oauth_introspect",
            caller_ip=caller_ip,
            token_prefix=token_prefix,
            active=active,
            reason=reason,
            client_id=oauth_token.client_id,
        )

        if not active:
            return TokenIntrospectionResponse(active=False)

        # Return token metadata (RFC 7662) - type-safe response
        return TokenIntrospectionResponse(
            active=True,
            client_id=oauth_token.client_id,
            scope=oauth_token.scope,
            exp=int(oauth_token.get_expires_at()),
            iat=int(oauth_token.issued_at.timestamp()),
            token_type="Bearer",
            aud=oauth_token.resource,  # RFC 8707 (None if not set)
        )

    except SQLAlchemyError as e:
        logger.error("oauth_introspect_db_error", error=str(e), caller_ip=caller_ip)
        await db.rollback()
        raise HTTPException(
            status_code=500, detail="Internal server error during token introspection"
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in token introspection: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.post("/revoke")
async def oauth_revoke(
    request: Request,
    token: str = Form(...),
    token_type_hint: str | None = Form(None),
):
    """Revoke access or refresh token."""
    db_session = get_sync_session()
    try:
        oauth_token = (
            db_session.query(OAuth2Token)
            .filter((OAuth2Token.access_token == token) | (OAuth2Token.refresh_token == token))
            .first()
        )

        if oauth_token:
            if token == oauth_token.access_token:
                oauth_token.access_token_revoked_at = utcnow()
            if token == oauth_token.refresh_token:
                oauth_token.refresh_token_revoked_at = utcnow()
            db_session.commit()
            logger.info("oauth_token_revoked", token_prefix=token[:8])

        return {"status": "ok"}
    finally:
        db_session.close()
