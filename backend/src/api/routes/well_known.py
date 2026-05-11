"""Well-known endpoints for OAuth2 and MCP.

Implements RFC 8414 OAuth 2.0 Authorization Server Metadata
and OpenAI Apps SDK requirements.
"""

import os

from fastapi import APIRouter, Request

from auth.mcp_scopes import ALL_ADVERTISED_SCOPES

router = APIRouter(tags=["well-known"])


@router.get("/oauth-protected-resource/mcp")
async def oauth_protected_resource_mcp():
    """OAuth 2.0 Protected Resource Metadata for /mcp endpoint.

    Claude Desktop may request resource-specific metadata.
    Redirects to main oauth-protected-resource endpoint.
    """
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/.well-known/oauth-protected-resource", status_code=301)


@router.get("/oauth-protected-resource")
async def oauth_protected_resource():
    """OAuth 2.0 Protected Resource Metadata.

    Required by OpenAI Apps SDK for ChatGPT integration.

    Spec: RFC 8707 - Resource Indicators for OAuth 2.0
    https://datatracker.ietf.workspace/doc/html/rfc8707

    Returns:
        Protected resource metadata
    """
    # Get configuration from environment
    base_url = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
    mcp_path = os.getenv("MCP_BASE_PATH", "/mcp")  # Configurable MCP mount path
    mcp_base = f"{base_url}{mcp_path}"  # MCP server canonical URL

    return {
        "resource": mcp_base,  # Must match MCP server URL exactly (RFC 9728)
        "authorization_servers": [base_url],
        "scopes_supported": list(ALL_ADVERTISED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_signing_alg_values_supported": ["RS256", "HS256"],
        "resource_documentation": f"{base_url}/redoc",
        "resource_policy_uri": f"{base_url}/docs",
        # MCP SSE endpoint for Claude Desktop / Claude Code
        "mcp_sse_endpoint": f"{mcp_base}/sse",
    }


@router.get("/openid-configuration")
async def openid_configuration(request: Request):
    """OpenID Connect Discovery 1.0 Metadata.

    Required by MCP clients (Claude Desktop, Claude Code) per MCP Authorization
    specification (June 2025 revision).

    Spec: OpenID Connect Discovery 1.0
    https://openid.net/specs/openid-connect-discovery-1_0.html

    MCP Authorization Spec:
    https://modelcontextprotocol.io/specification/draft/basic/authorization

    Returns:
        OpenID Connect Discovery metadata with PKCE support (required by MCP)

    Note:
        This endpoint is functionally equivalent to /oauth-authorization-server
        but uses OpenID Connect Discovery format, which many authorization servers
        (Azure AD, Auth0, etc.) prefer. MCP clients check this endpoint first,
        then fall back to /oauth-authorization-server if not found.

        CRITICAL: code_challenge_methods_supported MUST be present, or MCP clients
        will refuse to proceed (MCP spec requirement).
    """
    # Get base URL from environment (required for reverse proxy setups)
    # Using request.base_url would give http:// behind Caddy reverse proxy
    base_url = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")

    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/api/v1/oauth/authorize",
        "token_endpoint": f"{base_url}/api/v1/oauth/token",
        "revocation_endpoint": f"{base_url}/api/v1/oauth/revoke",
        "introspection_endpoint": f"{base_url}/api/v1/oauth/introspect",  # RFC 7662 (Issue #157)
        "registration_endpoint": f"{base_url}/api/v1/oauth/register",  # DCR endpoint (RFC 7591)
        # PKCE required by MCP (CRITICAL - clients will refuse if absent)
        "code_challenge_methods_supported": ["S256"],
        # "openid" intentionally omitted: this server does not issue id_token
        # (no OIDC subject path). Clients that strictly require OIDC
        # compliance will fail discovery; MCP clients only need PKCE + scopes.
        "scopes_supported": list(ALL_ADVERTISED_SCOPES),
        # Response types
        "response_types_supported": ["code"],
        # Grant types
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
        ],
        # Token endpoint auth methods (Issue #157: Public Client support)
        "token_endpoint_auth_methods_supported": [
            "none",  # Public clients (ChatGPT/Claude) with PKCE
            "client_secret_post",
            "client_secret_basic",
        ],
        # Introspection endpoint auth methods (RFC 7662, Issue #157)
        "introspection_endpoint_auth_methods_supported": [
            "none",  # Public endpoint
        ],
        # Additional OpenID Connect fields (optional but recommended)
        "response_modes_supported": ["query"],
        "subject_types_supported": ["public"],
    }


@router.get("/oauth-authorization-server")
async def oauth_authorization_server(request: Request):
    """OAuth 2.0 Authorization Server Metadata.

    Provides discovery information about the authorization server.

    Spec: RFC 8414 - OAuth 2.0 Authorization Server Metadata
    https://datatracker.ietf.workspace/doc/html/rfc8414

    This server implements its own OAuth2 Authorization Server (Issue #33).
    For MCP clients like Claude Desktop, use API Key authentication instead.

    Returns:
        Authorization server metadata
    """
    # Get base URL from environment (required for reverse proxy setups)
    # Using request.base_url would give http:// behind Caddy reverse proxy
    base_url = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")

    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/api/v1/oauth/authorize",
        "token_endpoint": f"{base_url}/api/v1/oauth/token",
        "revocation_endpoint": f"{base_url}/api/v1/oauth/revoke",
        "introspection_endpoint": f"{base_url}/api/v1/oauth/introspect",  # RFC 7662 (Issue #157)
        "registration_endpoint": f"{base_url}/api/v1/oauth/register",  # DCR endpoint (RFC 7591)
        "scopes_supported": list(ALL_ADVERTISED_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
        ],
        "code_challenge_methods_supported": ["S256"],  # PKCE required
        "token_endpoint_auth_methods_supported": [
            "none",  # Public clients (ChatGPT/Claude) with PKCE (Issue #157)
            "client_secret_post",
            "client_secret_basic",
        ],
        "introspection_endpoint_auth_methods_supported": [
            "none",  # RFC 7662 (Issue #157)
        ],
    }
