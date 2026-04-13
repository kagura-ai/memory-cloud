"""OAuth2 Authorization Server implementation using Authlib.

Issue #33 - OAuth2 authentication support for ChatGPT MCP integration

Provides OAuth2 authorization server functionality with:
- Authorization Code Grant (RFC 6749 Section 4.1)
- Refresh Token Grant (RFC 6749 Section 6)
- Confidential Clients only (no PKCE public clients)

Architecture:
    Built on Authlib's SQLAlchemy integration pattern, adapted for FastAPI with
    synchronous SQLAlchemy sessions (Authlib requires sync).

    Note: Even though memory-cloud uses async sessions everywhere else, Authlib
    requires synchronous database access. We create temporary sync sessions for
    OAuth2 operations only.

Usage:
    >>> from auth.oauth2_server import create_authorization_server
    >>> from db.base import sync_session_maker
    >>>
    >>> session = sync_session_maker()
    >>> server = create_authorization_server(session)
    >>> session.close()

References:
    - https://docs.authlib.workspace/en/latest/flask/2/authorization-server.html
    - RFC 6749: The OAuth 2.0 Authorization Framework
"""

import logging
import secrets
from datetime import timedelta
from typing import Any

from authlib.oauth2 import OAuth2Request
from authlib.oauth2.rfc6749 import grants
from sqlalchemy.orm import Session

from models.auth import OAuth2AuthorizationCode, OAuth2Client, OAuth2Token
from utils.datetime import utcnow

logger = logging.getLogger(__name__)


# ============================================================================
# Query Functions (Required by Authlib)
# ============================================================================


def query_client(session: Session, client_id: str) -> OAuth2Client | None:
    """Query OAuth2 client by client_id.

    Required by Authlib.

    Args:
        session: SQLAlchemy session
        client_id: Client identifier

    Returns:
        OAuth2Client or None
    """
    import logging

    logger = logging.getLogger(__name__)

    client = session.query(OAuth2Client).filter_by(client_id=client_id).first()
    logger.info(f"query_client: client_id={client_id}, found={client is not None}")

    return client


def save_token(token: dict[str, Any], request: OAuth2Request, session: Session) -> None:
    """Save access token to database.

    Required by Authlib.
    Issue #157: Save resource parameter (RFC 8707).

    Args:
        token: Token data from Authlib
        request: OAuth2 request object
        session: SQLAlchemy session
    """
    # Extract client and user from request
    client_id = request.client.client_id
    user_id = request.user.user_id if hasattr(request, "user") else None

    # For refresh token grant, user comes from existing token
    if not user_id and hasattr(request, "credential"):
        user_id = request.credential.user_id

    if not user_id:
        logger.error("Cannot save token: user_id not found in request")
        raise ValueError("User ID required for token issuance")

    # Extract resource parameter (RFC 8707, Issue #157)
    # Resource can come from:
    # 1. Authorization code (stored during authorization)
    # 2. Token request data (for refresh token grant)
    resource = None
    if hasattr(request, "credential") and hasattr(request.credential, "resource"):
        resource = request.credential.resource  # From authorization code
    elif hasattr(request, "data"):
        resource = request.data.get("resource")  # From token request

    # Create new token record
    oauth_token = OAuth2Token(
        client_id=client_id,
        user_id=user_id,
        token_type=token.get("token_type", "Bearer"),
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        scope=token.get("scope", ""),
        expires_in=token.get("expires_in", 3600),
        resource=resource,  # RFC 8707 (Issue #157)
        revoked=False,
    )

    session.add(oauth_token)
    session.commit()

    logger.info(
        f"Token saved: client={client_id}, user={user_id}, "
        f"expires_in={token.get('expires_in', 3600)}s, resource={resource}"
    )


# ============================================================================
# Token Generation Helper (Issue #141)
# ============================================================================


def _generate_token_with_expiry(
    grant_type: str, client: OAuth2Client, expires_in: int, scope: str
) -> dict[str, Any]:
    """Generate OAuth2 token with explicit expiration.

    Issue #141: Common token generation logic to avoid DRY violation.

    Args:
        grant_type: Grant type (authorization_code, refresh_token)
        client: OAuth2Client instance
        expires_in: Token expiration in seconds
        scope: Scope string

    Returns:
        Token dict with access_token, refresh_token, expires_in, etc.
    """
    token = {
        "token_type": "Bearer",
        "access_token": secrets.token_urlsafe(32),
        "expires_in": expires_in,
        "scope": scope,
        "refresh_token": secrets.token_urlsafe(32),
    }

    logger.info(
        f"Token generated: grant={grant_type}, client={client.client_id}, expires_in={expires_in}s"
    )

    return token


# ============================================================================
# Authorization Code Grant
# ============================================================================


class AuthorizationCodeGrant(grants.AuthorizationCodeGrant):
    """Authorization Code Grant implementation.

    Implements RFC 6749 Section 4.1 (Authorization Code Grant) with
    support for both confidential and public clients.

    Issue #157: Public Client support for ChatGPT/Claude Web UI.

    Flow:
        1. Client requests authorization: GET /api/v1/oauth/authorize
        2. User consents
        3. Server issues authorization code
        4. Client exchanges code for token: POST /api/v1/oauth/token
        5. Server validates code and issues access token

    Security:
        - Confidential clients: client_secret required
        - Public clients: PKCE (code_verifier) required, client_secret optional
        - Authorization codes expire after 10 minutes
        - Codes are single-use (deleted after exchange)
        - Client secret required (confidential clients only)
    """

    # Token endpoint auth methods (Issue #157: Public Client + PKCE support)
    TOKEN_ENDPOINT_AUTH_METHODS = [
        "none",  # Public clients (ChatGPT/Claude) with PKCE
        "client_secret_post",
        "client_secret_basic",
    ]

    # Token expires in 1 hour (3600 seconds)
    TOKEN_EXPIRES_IN = 3600

    def generate_token(
        self,
        user=None,
        scope=None,
        grant_type=None,
        expires_in=None,
        include_refresh_token=True,
    ) -> dict[str, Any]:
        """Generate OAuth2 token with explicit expires_in.

        Authlib calls this with keyword arguments only.
        Client is obtained from self.request.client (not passed as parameter).

        Args:
            user: User object (optional)
            scope: Scope string (optional)
            grant_type: Grant type (optional, uses self.GRANT_TYPE if not provided)
            expires_in: Token expiration seconds (optional, uses self.TOKEN_EXPIRES_IN if not provided)
            include_refresh_token: Whether to include refresh token (not used)

        Returns:
            Token dict with access_token, refresh_token, expires_in, etc.
        """
        # Get client from request (Authlib pattern)
        client = self.request.client

        # Use class-level defaults if not provided
        if expires_in is None:
            expires_in = self.TOKEN_EXPIRES_IN
        if grant_type is None:
            grant_type = self.GRANT_TYPE

        return _generate_token_with_expiry(grant_type, client, expires_in, scope)

    def save_authorization_code(self, code: str, request: OAuth2Request) -> OAuth2AuthorizationCode:
        """Save authorization code to database.

        Called by Authlib after user authorization.
        Issue #157: Save resource parameter (RFC 8707).

        Args:
            code: Generated authorization code
            request: OAuth2 request object

        Returns:
            Saved OAuth2AuthorizationCode instance
        """
        # Extract data from request
        client_id = request.client.client_id
        user_id = request.user.user_id if hasattr(request, "user") else None
        redirect_uri = request.redirect_uri
        scope = request.scope

        # Get request data (Authlib 1.x compatibility, Issue #157)
        # Authlib 1.x: request.payload contains query params and form data
        request_data = {}
        if hasattr(request, "payload") and request.payload is not None:
            if isinstance(request.payload, dict):
                request_data = request.payload
                logger.debug(f"Using request.payload (dict), keys: {list(request_data.keys())}")
            elif hasattr(request.payload, "data"):
                request_data = request.payload.data or {}
                logger.debug(
                    f"Using request.payload.data, keys: {list(request_data.keys()) if request_data else 'empty'}"
                )
            else:
                logger.warning(f"request.payload exists but unknown type: {type(request.payload)}")
        elif hasattr(request, "data") and request.data is not None:
            request_data = request.data
            logger.debug(f"Using request.data (Authlib 0.x), keys: {list(request_data.keys())}")
        else:
            logger.warning("No request.payload or request.data available")

        # PKCE support (RFC 7636) - for public clients (ChatGPT/Claude)
        code_challenge = request_data.get("code_challenge")
        code_challenge_method = request_data.get("code_challenge_method")

        # RFC 8707 Resource Indicators (Issue #157)
        # Try to get from request data first, then from Redis (saved in GET /authorize)
        resource = request_data.get("resource")

        # Restore from Redis if state is provided (saved in GET /authorize)
        state = request_data.get("state")
        if state:
            from redis import Redis

            from config.database import get_redis_url

            redis_url = get_redis_url()
            redis = Redis.from_url(redis_url, decode_responses=True)

            # Restore resource (RFC 8707)
            if not resource:
                resource = redis.get(f"oauth_state:{state}:resource")
                if resource:
                    logger.debug(
                        f"Restored resource from Redis: state={state}, resource={resource}"
                    )
                    redis.delete(f"oauth_state:{state}:resource")  # One-time use

            # Restore PKCE parameters (RFC 7636)
            if not code_challenge:
                code_challenge = redis.get(f"oauth_state:{state}:code_challenge")
                if code_challenge:
                    code_challenge_method = redis.get(f"oauth_state:{state}:code_challenge_method")
                    logger.debug(
                        f"Restored PKCE from Redis: state={state}, method={code_challenge_method}"
                    )
                    redis.delete(f"oauth_state:{state}:code_challenge")
                    if code_challenge_method:
                        redis.delete(f"oauth_state:{state}:code_challenge_method")

        logger.info(
            f"Authorization code data: resource={resource}, code_challenge={code_challenge is not None}"
        )

        if not user_id:
            logger.error("Cannot save authorization code: user_id not found")
            raise ValueError("User ID required for authorization")

        # Create authorization code record (expires in 10 minutes)
        auth_code = OAuth2AuthorizationCode(
            code=code,
            client_id=client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            resource=resource,  # RFC 8707 (Issue #157)
            auth_time=utcnow(),
            expires_at=utcnow() + timedelta(seconds=600),  # 10 min
        )

        self.server.db_session.add(auth_code)
        self.server.db_session.commit()

        logger.info(f"Authorization code saved: client={client_id}, user={user_id}, scope={scope}")

        return auth_code

    def query_authorization_code(
        self, code: str, client: OAuth2Client
    ) -> OAuth2AuthorizationCode | None:
        """Query authorization code from database.

        Called by Authlib during token exchange.

        Args:
            code: Authorization code
            client: OAuth2Client instance

        Returns:
            OAuth2AuthorizationCode or None
        """
        auth_code = (
            self.server.db_session.query(OAuth2AuthorizationCode)
            .filter_by(code=code, client_id=client.client_id)
            .first()
        )

        # Check expiration
        if auth_code and auth_code.is_expired():
            logger.warning(
                f"Authorization code expired: code={code[:8]}..., client={client.client_id}"
            )
            return None

        return auth_code

    def delete_authorization_code(self, authorization_code: OAuth2AuthorizationCode) -> None:
        """Delete authorization code after use.

        Called by Authlib after successful token exchange.
        Codes are single-use only.

        Args:
            authorization_code: OAuth2AuthorizationCode instance to delete
        """
        self.server.db_session.delete(authorization_code)
        self.server.db_session.commit()

        logger.info(
            f"Authorization code deleted: code={authorization_code.code[:8]}..., "
            f"client={authorization_code.client_id}"
        )

    def authenticate_user(self, authorization_code: OAuth2AuthorizationCode) -> Any:
        """Get user from authorization code.

        Called by Authlib to attach user to token request.

        Args:
            authorization_code: OAuth2AuthorizationCode instance

        Returns:
            User object (must have user_id attribute)
        """

        # Return a minimal user object
        class UserStub:
            def __init__(self, user_id: str):
                self.user_id = user_id

        return UserStub(user_id=authorization_code.user_id)


# ============================================================================
# Refresh Token Grant
# ============================================================================


class RefreshTokenGrant(grants.RefreshTokenGrant):
    """Refresh Token Grant implementation.

    Implements RFC 6749 Section 6 (Refreshing an Access Token).

    Flow:
        1. Client sends refresh token: POST /api/v1/oauth/token
        2. Server validates refresh token
        3. Server issues new access token (and optionally new refresh token)

    Security:
        - Refresh tokens can be revoked independently
        - Refresh tokens are long-lived (no automatic expiration)
        - Client secret required for confidential clients
    """

    # Token endpoint auth methods (Issue #157: Public Client + PKCE support)
    TOKEN_ENDPOINT_AUTH_METHODS = [
        "none",  # Public clients (ChatGPT/Claude) with PKCE
        "client_secret_post",
        "client_secret_basic",
    ]

    # Token expires in 1 hour (3600 seconds)
    TOKEN_EXPIRES_IN = 3600

    def generate_token(
        self,
        user=None,
        scope=None,
        grant_type=None,
        expires_in=None,
        include_refresh_token=True,
    ) -> dict[str, Any]:
        """Generate OAuth2 token with explicit expires_in.

        Authlib calls this with keyword arguments only.
        Client is obtained from self.request.client (not passed as parameter).

        Args:
            user: User object (optional)
            scope: Scope string (optional)
            grant_type: Grant type (optional, uses self.GRANT_TYPE if not provided)
            expires_in: Token expiration seconds (optional, uses self.TOKEN_EXPIRES_IN if not provided)
            include_refresh_token: Whether to include refresh token (not used)

        Returns:
            Token dict with access_token, refresh_token, expires_in, etc.
        """
        # Get client from request (Authlib pattern)
        client = self.request.client

        # Use class-level defaults if not provided
        if expires_in is None:
            expires_in = self.TOKEN_EXPIRES_IN
        if grant_type is None:
            grant_type = self.GRANT_TYPE

        return _generate_token_with_expiry(grant_type, client, expires_in, scope)

    def authenticate_refresh_token(self, refresh_token: str) -> OAuth2Token | None:
        """Query and validate refresh token.

        Called by Authlib during refresh token grant.

        Args:
            refresh_token: Refresh token value

        Returns:
            OAuth2Token or None
        """
        token = (
            self.server.db_session.query(OAuth2Token).filter_by(refresh_token=refresh_token).first()
        )

        # Validate token
        if token and token.is_refresh_token_active():
            return token

        if token:
            logger.warning(
                f"Refresh token invalid: token={refresh_token[:8]}..., "
                f"revoked={token.refresh_token_revoked_at is not None}"
            )

        return None

    def authenticate_user(self, credential: OAuth2Token) -> Any:
        """Get user from refresh token.

        Called by Authlib to attach user to token request.

        Args:
            credential: OAuth2Token instance

        Returns:
            User object (must have user_id attribute)
        """

        class UserStub:
            def __init__(self, user_id: str):
                self.user_id = user_id

        return UserStub(user_id=credential.user_id)

    def revoke_old_credential(self, credential: OAuth2Token) -> None:
        """Revoke old access and refresh tokens.

        Called by Authlib after issuing new token pair.
        Revokes both the old access token and the old refresh token
        to enforce refresh token rotation (RFC 6819 Section 5.2.2.3).

        Args:
            credential: Old OAuth2Token instance
        """
        now = utcnow()
        credential.access_token_revoked_at = now
        credential.refresh_token_revoked_at = now
        self.server.db_session.commit()

        logger.info(
            "oauth_refresh_token_rotated: client=%s, user=%s",
            credential.client_id,
            credential.user_id,
        )


# ============================================================================
# Authorization Server Factory
# ============================================================================


class OAuth2AuthorizationServer:
    """OAuth2 Authorization Server wrapper.

    Encapsulates Authlib's AuthorizationServer with SQLAlchemy session management.
    """

    def __init__(self, session: Session):
        """Initialize OAuth2 server.

        Args:
            session: SQLAlchemy session for database operations (sync)
        """
        self.db_session = session

        # Import here to avoid circular dependency
        from authlib.oauth2 import AuthorizationServer

        # Create custom AuthorizationServer that implements create_oauth2_request
        class CustomAuthorizationServer(AuthorizationServer):
            def create_oauth2_request(self, request):
                """Convert FastAPI/Starlette Request to OAuth2Request.

                This is the correct Authlib integration pattern for FastAPI.
                Authlib 1.6.5 calls this synchronously, so it must be a sync function.

                Uses StarletteOAuth2Request which properly implements payload property.
                """
                # If already OAuth2Request, return as-is
                if isinstance(request, OAuth2Request):
                    return request

                # Use Starlette/FastAPI integration wrapper
                from starlette.requests import Request as StarletteRequest

                from auth.starlette_oauth2_request import StarletteOAuth2Request

                if isinstance(request, StarletteRequest):
                    return StarletteOAuth2Request(request)

                raise TypeError(f"Unsupported request type: {type(request)!r}")

            def handle_response(self, status_code, payload, headers):
                """Handle OAuth2 response.

                Authlib framework integration must implement this to convert
                Authlib's response format to framework's response format.

                Args:
                    status_code: HTTP status code
                    payload: Response body (str or dict)
                    headers: Response headers

                Returns:
                    Simple object with status_code, body, headers, and location
                """
                import logging

                logger = logging.getLogger(__name__)
                logger.info(
                    f"OAuth2 handle_response: status={status_code}, payload={payload}, headers={headers}"
                )

                class SimpleResponse:
                    def __init__(self, status, body, headers):
                        self.status_code = status
                        self.body = body
                        self.headers = headers
                        # Extract location from headers for redirects
                        self.location = None
                        for key, value in headers:
                            if key.lower() == "location":
                                self.location = value
                                break

                return SimpleResponse(status_code, payload, headers)

            def send_signal(self, name, **kwargs):
                """Authlib hook system for framework integrations.

                Authlib calls send_signal() for extensibility points like:
                - after_authenticate_client
                - before_validate_authorization_request
                - after_create_token_response

                For FastAPI integration, we use no-op implementation.
                Framework integrations can override this for logging/monitoring.

                Args:
                    name: Signal name
                    **kwargs: Signal-specific arguments (client, grant, etc.)
                """
                # No-op implementation for FastAPI
                # Signals are optional; OAuth2 flow works without them
                import logging

                logger = logging.getLogger(__name__)
                logger.debug(f"OAuth2 signal: {name}, kwargs={list(kwargs.keys())}")
                return

        # Create query_client function
        def query_client_func(client_id: str) -> OAuth2Client | None:
            return query_client(session, client_id)

        # Create save_token function
        def save_token_func(token: dict, request: OAuth2Request) -> None:
            save_token(token, request, session)

        # Create Authlib server using custom subclass
        self.server = CustomAuthorizationServer()

        # Set db_session for grant access
        self.server.db_session = session

        # Set query_client and save_token as attributes (Authlib v1.3+ style)
        self.server.query_client = query_client_func
        self.server.save_token = save_token_func

        # Issue #141: Token generation is now handled by Grant classes
        # (AuthorizationCodeGrant.generate_token() and RefreshTokenGrant.generate_token())
        # No need to set server.generate_token - Grant methods take precedence

        # Register grants
        self._register_grants()

    def _register_grants(self) -> None:
        """Register grant types with the server."""
        # Authorization Code Grant (no PKCE for confidential clients)
        self.server.register_grant(AuthorizationCodeGrant)

        # Refresh Token Grant
        self.server.register_grant(RefreshTokenGrant)

        logger.info(
            "OAuth2 server initialized: grants=[authorization_code, refresh_token], "
            "confidential_clients_only=true"
        )

    def get_consent_grant(self, request: Any, end_user: Any = None) -> Any:
        """Get consent grant for authorization page.

        Args:
            request: OAuth2 request
            end_user: End user object (optional)

        Returns:
            Grant object for consent page
        """
        return self.server.get_consent_grant(request=request, end_user=end_user)

    def create_authorization_response(self, request: Any, grant_user: Any) -> Any:
        """Create authorization response.

        Args:
            request: OAuth2 request (FastAPI Request or OAuth2Request)
            grant_user: User object granting authorization

        Returns:
            Authorization response (redirect or error)
        """
        return self.server.create_authorization_response(request, grant_user=grant_user)

    def create_token_response(self, request: Any) -> Any:
        """Create token response.

        Args:
            request: OAuth2 request (FastAPI Request or OAuth2Request)

        Returns:
            Token response (JSON with access_token)
        """
        return self.server.create_token_response(request)

    def validate_consent_request(self, request: Any) -> Any:
        """Validate authorization request for consent screen.

        Args:
            request: OAuth2 request

        Returns:
            Grant object for rendering consent screen
        """
        return self.server.validate_consent_request(request)


def create_authorization_server(session: Session) -> OAuth2AuthorizationServer:
    """Factory function to create OAuth2 authorization server.

    Args:
        session: SQLAlchemy session (sync)

    Returns:
        Configured OAuth2AuthorizationServer instance

    Example:
        >>> from db.base import sync_session_maker
        >>> session = sync_session_maker()
        >>> try:
        >>>     server = create_authorization_server(session)
        >>>     # Use server...
        >>> finally:
        >>>     session.close()
    """
    return OAuth2AuthorizationServer(session)
