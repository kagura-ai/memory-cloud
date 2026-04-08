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
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import SessionUser, require_admin
from auth.oauth2_server import create_authorization_server
from db.base import get_db, get_sync_session
from models.auth import OAuth2Client, OAuth2Token, User
from models.schemas import TokenIntrospectionResponse
from utils.datetime import utcnow
from utils.logger import get_logger
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
    owner_id: str
    provider: str  # Migration 036
    created_at: str  # ISO 8601 with 'Z' (UTC)
    # Migration 034-035: Zero-knowledge visibility
    plaintext_secret: str | None  # Only if visible + owner
    is_visible: bool
    visibility_expires_at: str | None  # ISO 8601 with 'Z' (UTC)

    class Config:
        from_attributes = True


class OAuth2ClientWithSecretResponse(OAuth2ClientResponse):
    """OAuth2 Client response with client secret (only on creation)."""

    client_secret: str


class OAuth2ClientCreateRequest(BaseModel):
    """OAuth2 Client creation request."""

    client_name: str = Field(
        ..., min_length=1, max_length=100, description="Human-readable client name"
    )
    redirect_uris: list[str] = Field(..., min_items=1, description="Allowed redirect URIs")
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
        default="memory:read memory:write offline_access",  # Issue #132: offline_access for refresh token
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
    redirect_uris: list[str] = Field(..., min_items=1, description="Redirect URIs")
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
    redirect_uris: list[str] | None = Field(None, min_items=1)
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
                    created_at=client.created_at.isoformat() + "Z",  # Issue #175: UTC explicit
                    plaintext_secret=plaintext_secret,
                    is_visible=is_visible,
                    visibility_expires_at=(
                        client.visibility_expires_at.isoformat() + "Z"
                        if client.visibility_expires_at
                        else None
                    ),  # Issue #175: UTC explicit
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
            created_at=client.created_at.isoformat() + "Z",  # Issue #175: UTC explicit
            client_secret=client_secret,  # ⚠️ Only shown once!
            # Migration 034-035: Visibility fields
            plaintext_secret=client_secret,  # Same as client_secret
            is_visible=True,  # Newly created = visible
            visibility_expires_at=visibility_expires_at.isoformat()
            + "Z",  # Issue #175: UTC explicit
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


@router.post(
    "/register",
    response_model=OAuth2ClientWithSecretResponse,
    status_code=status.HTTP_201_CREATED,
)
async def dynamic_client_registration(
    request: Request,
    data: DynamicClientRegistrationRequest,
) -> OAuth2ClientWithSecretResponse:
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
        Created client with client_secret

    Example:
        POST /api/v1/oauth/register
        {
          "client_name": "ChatGPT MCP",
          "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
          "token_endpoint_auth_method": "none"
        }

    Spec:
        RFC 7591 - OAuth 2.0 Dynamic Client Registration Protocol
        MCP Authorization Spec (2025-03-26)
    """
    # Get client IP for rate limiting
    client_ip = request.client.host if request.client else "unknown"

    # Check rate limit (5 registrations per minute per IP)
    from db.redis import increment_counter

    rate_limit_key = f"dcr_rate_limit:{client_ip}"
    count = await increment_counter(rate_limit_key, ttl=60)

    if count > 5:
        logger.warning(
            "dcr_rate_limit_exceeded",
            ip=client_ip,
            count=count,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many registration requests. Please try again later.",
        )

    # Detect provider from redirect_uri patterns
    redirect_uri = data.redirect_uris[0] if data.redirect_uris else ""
    detected_provider = "custom"

    if "chatgpt.com" in redirect_uri or "chat.openai.com" in redirect_uri:
        detected_provider = "chatgpt"
    elif "claude.ai" in redirect_uri or "anthropic.com" in redirect_uri:
        detected_provider = "claude"
    elif "cursor.sh" in redirect_uri or "cursor.com" in redirect_uri:
        detected_provider = "cursor"

    # Provider whitelist check
    ALLOWED_PROVIDERS = ["chatgpt", "claude", "cursor"]
    if detected_provider not in ALLOWED_PROVIDERS:
        logger.warning(
            "dcr_provider_rejected",
            ip=client_ip,
            redirect_uri=redirect_uri,
            detected_provider=detected_provider,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Dynamic client registration is only allowed for: {', '.join(ALLOWED_PROVIDERS)}",
        )

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
        visibility_expires_at = utcnow() + timedelta(minutes=10)

        # For DCR: auto-set scope if not provided
        scope = data.scope if data.scope else "memory:read memory:write offline_access"

        # Create OAuth2Client (DCR: owner_id=None, workspace_id=None, auth_method=none)
        client = OAuth2Client(
            client_id=client_id,
            client_secret_hash=client_secret_hash,
            client_name=data.client_name,
            redirect_uris=data.redirect_uris,
            grant_types=data.grant_types,
            response_types=data.response_types,
            scope=scope,
            token_endpoint_auth_method="none",  # Force public client for DCR
            owner_id=None,  # DCR clients have no owner
            workspace_id=None,  # Global clients
            provider=detected_provider,
            plaintext_secret_encrypted=plaintext_secret_encrypted,
            visibility_expires_at=visibility_expires_at,
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

        # Return response with client_secret
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
            created_at=client.created_at.isoformat() + "Z",
            client_secret=client_secret,
            plaintext_secret=client_secret,
            is_visible=True,
            visibility_expires_at=visibility_expires_at.isoformat() + "Z",
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
            created_at=client.created_at.isoformat() + "Z",  # Issue #175: UTC explicit
            # Migration 034-035: Visibility fields
            plaintext_secret=plaintext_secret,
            is_visible=is_visible,
            visibility_expires_at=(
                client.visibility_expires_at.isoformat() + "Z"
                if client.visibility_expires_at
                else None
            ),  # Issue #175: UTC explicit
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
            created_at=client.created_at.isoformat() + "Z",  # Issue #175: UTC explicit
            # Migration 034-035: Visibility fields (no secret on update)
            plaintext_secret=None,
            is_visible=client.hidden_at is None,
            visibility_expires_at=(
                client.visibility_expires_at.isoformat() + "Z"
                if client.visibility_expires_at
                else None
            ),  # Issue #175: UTC explicit
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
            created_at=client.created_at.isoformat() + "Z",  # Issue #175: UTC explicit
            client_secret=new_client_secret,  # New secret - only shown once!
            # Migration 034-035: Visibility fields
            plaintext_secret=new_client_secret,  # Same as client_secret
            is_visible=True,  # Newly regenerated = visible
            visibility_expires_at=(
                client.visibility_expires_at.isoformat() + "Z"
                if client.visibility_expires_at
                else None
            ),  # Issue #175: UTC explicit
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
def get_current_user_from_session(request: Request):
    """Get authenticated user from SessionMiddleware.

    Args:
        request: FastAPI request with request.state.user

    Returns:
        User object

    Raises:
        HTTPException: If not authenticated
    """
    if not hasattr(request.state, "user") or not request.state.user:
        return None

    # Get user from request.state
    user_data = request.state.user

    # Create user-like object for OAuth2 server
    class UserStub:
        def __init__(self, user_id: str, email: str):
            self.user_id = user_id
            self.email = email

    if isinstance(user_data, dict):
        return UserStub(
            user_id=user_data.get("user_id") or user_data.get("sub") or user_data.get("email"),
            email=user_data.get("email"),
        )
    else:
        return UserStub(
            user_id=getattr(user_data, "user_id", None) or getattr(user_data, "email", None),
            email=getattr(user_data, "email", None),
        )


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
    logger.info(f"GET /authorize: client_id={client_id}, state={state}, resource={resource}")

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
                f"client_id={client_id}, redirect_uri={redirect_uri!r}"
            )
            return _render_invalid_redirect_uri_error(request, locale, redirect_uri)

        # Get i18n messages
        messages = get_oauth_messages(locale)

        # Build query string for POST action (maintain OAuth2 params in query)
        from urllib.parse import urlencode

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


def _handle_authorize_sync(request, user_dict):
    """Synchronous OAuth2 authorization handler.

    Runs in thread pool to avoid blocking FastAPI event loop.
    Session is created and closed within this function.
    """
    db_session = get_sync_session()
    try:
        server = create_authorization_server(db_session)
        response = server.create_authorization_response(request, grant_user=user_dict)
        db_session.commit()
        return response
    except Exception:
        db_session.rollback()
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
                f"client_id={client_id}, redirect_uri={redirect_uri!r}"
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
            f"{redirect_uri}?{urlencode(params)}",
            status_code=303,  # See Other: POST→GET redirect
        )

    # Run Authlib operations in thread pool to avoid blocking event loop
    try:
        response = await asyncio.to_thread(_handle_authorize_sync, request, user)

        # Use 303 See Other to convert POST to GET redirect
        # Claude.ai callback expects GET, not POST
        if hasattr(response, "location") and response.location:
            return RedirectResponse(response.location, status_code=303)

        # No location - should not happen in normal flow
        raise HTTPException(500, "No redirect location in OAuth response")

    except Exception as e:
        import traceback

        from authlib.oauth2.rfc6749.errors import OAuth2Error
        from fastapi.responses import HTMLResponse

        # Log full traceback for debugging
        tb = traceback.format_exc()
        logger.error(f"oauth_authorize_failed: {type(e).__name__}: {e}\n{tb}")

        # Handle OAuth2Error with redirect
        if isinstance(e, OAuth2Error):
            if redirect_uri:
                params = {"error": e.error}
                if hasattr(e, "description") and e.description:
                    params["error_description"] = e.description
                if state:
                    params["state"] = state
                return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=303)

            # No redirect_uri - return HTML error
            return HTMLResponse(
                content=f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="utf-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1">
                    <title>OAuth Error</title>
                </head>
                <body style="font-family: system-ui; padding: 20px; max-width: 600px; margin: 0 auto;">
                    <h1>OAuth Authorization Error</h1>
                    <p><strong>Error:</strong> {e.error}</p>
                    <p><strong>Description:</strong> {e.description if hasattr(e, "description") else "Unknown error"}</p>
                    <button onclick="window.history.back()" style="padding: 10px 20px; margin-top: 20px;">Go Back</button>
                </body>
                </html>
                """,
                status_code=400,
            )

        # Other exceptions - try to redirect if possible
        if redirect_uri:
            params = {"error": "server_error", "error_description": str(e)}
            if state:
                params["state"] = state
            return RedirectResponse(f"{redirect_uri}?{urlencode(params)}", status_code=303)

        # No redirect_uri - re-raise
        raise


def _handle_token_sync(request):
    """Synchronous OAuth2 token handler.

    Runs in thread pool to avoid blocking FastAPI event loop.
    Session is created and closed within this function.
    """
    db_session = get_sync_session()
    try:
        server = create_authorization_server(db_session)
        authlib_resp = server.create_token_response(request)
        db_session.commit()
        return authlib_resp
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()


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

    # Run Authlib operations in thread pool to avoid blocking event loop
    authlib_resp = await asyncio.to_thread(_handle_token_sync, request)

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


@router.post("/introspect", response_model=TokenIntrospectionResponse)
async def introspect_token(
    token: str = Form(..., description="Access token to introspect"),
    db: AsyncSession = Depends(get_db),
) -> TokenIntrospectionResponse:
    """Token Introspection endpoint (RFC 7662).

    Issue #157: MCP SDK compliance - Token Introspection

    Allows Resource Servers to validate access tokens.

    Args:
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

    try:
        # Look up token in database
        result = await db.execute(select(OAuth2Token).where(OAuth2Token.access_token == token))
        oauth_token = result.scalar_one_or_none()

        if not oauth_token:
            return TokenIntrospectionResponse(active=False)

        # Check if token is expired
        if oauth_token.is_expired():
            return TokenIntrospectionResponse(active=False)

        # Check if token is revoked
        if oauth_token.is_revoked():
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
        logger.error(f"Database error in token introspection: {e}")
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
