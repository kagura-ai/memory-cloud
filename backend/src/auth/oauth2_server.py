"""OAuth2 Authorization Server implementation using Authlib.

Issue #33 - OAuth2 authentication support for ChatGPT MCP integration

Provides OAuth2 authorization server functionality with:
- Authorization Code Grant (RFC 6749 Section 4.1) with PKCE (RFC 7636, ``S256``
  only) for public clients (``token_endpoint_auth_method="none"``)
- Refresh Token Grant (RFC 6749 Section 6)
- Both confidential and public clients are supported (Issue #157, #513)
- Resource Indicators (RFC 8707): the ``resource`` of the authorization request
  travels with the code and becomes the token's audience (#1686)

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
from typing import Any, cast

from authlib.oauth2 import OAuth2Request
from authlib.oauth2.rfc6749 import grants
from authlib.oauth2.rfc6749.errors import (
    InvalidGrantError,
    InvalidRequestError,
    InvalidScopeError,
    OAuth2Error,
)
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc7636.challenge import (
    CODE_CHALLENGE_PATTERN,
    CODE_VERIFIER_PATTERN,
    compare_s256_code_challenge,
)
from authlib.oauth2.rfc8628 import DeviceCodeGrant as _DeviceCodeGrant
from sqlalchemy.orm import Session

from auth.mcp_resource import is_same_mcp_resource, mcp_resource_identifier
from auth.mcp_scopes import ALL_ADVERTISED_SCOPES
from config.settings import get_settings
from models.auth import (
    OAuth2AuthorizationCode,
    OAuth2Client,
    OAuth2DeviceCode,
    OAuth2Token,
    User,
    Workspace,
)
from utils.datetime import utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Authlib's grants log each issued token dict at DEBUG ("Issue token %r to
# %r") and log nothing above DEBUG. Holding the library's loggers at INFO keeps
# token values out of the log stream even when LOG_LEVEL=DEBUG (#1686).
logging.getLogger("authlib").setLevel(logging.INFO)

_TOKEN_ENDPOINT_AUTH_METHODS = ["none", "client_secret_post", "client_secret_basic"]

# The one PKCE transformation this server accepts — the value of
# ``code_challenge_methods_supported`` in the authorization-server metadata.
PKCE_METHOD = "S256"


# ============================================================================
# Authorization request rules: scope, PKCE, resource (#1686)
# ============================================================================


class InvalidTargetError(OAuth2Error):
    """RFC 8707 §2 ``invalid_target``: the requested resource is not served here."""

    error = "invalid_target"


def requested_resource(payload: Any) -> str | None:
    """Read the RFC 8707 ``resource`` of an authorization or token request.

    Every ``resource`` value must name this server's MCP resource
    (:func:`auth.mcp_resource.is_same_mcp_resource`: the published identifier,
    a path beneath it, any query). The published identifier is returned, so
    codes and tokens always store the discoverable value. A parameter sent
    without a value counts as omitted (RFC 6749 §3.1).

    Args:
        payload: The request payload (``data`` and ``datalist``).

    Returns:
        The MCP resource identifier, or ``None`` when no ``resource`` was sent.

    Raises:
        InvalidTargetError: A ``resource`` names anything else.
    """
    values = [value for value in payload.datalist.get("resource", []) if value]
    if not values:
        return None
    if not all(is_same_mcp_resource(value) for value in values):
        raise InvalidTargetError(
            description=(
                "Unknown resource. Use the resource published at "
                "/.well-known/oauth-protected-resource."
            )
        )
    return mcp_resource_identifier()


def settle_audience(requested: str | None, bound: str | None) -> str | None:
    """Audience of a token issued for an authorization code or a refresh token.

    Args:
        requested: The token request's ``resource`` as returned by
            :func:`requested_resource` (the published identifier or ``None``).
        bound: The ``resource`` the code or the refreshed token carries. It
            may have been stored before these rules, in any form.

    Returns:
        The published identifier when ``bound`` names the MCP resource in any
        form :func:`auth.mcp_resource.is_same_mcp_resource` accepts;
        ``requested`` when nothing is bound; otherwise ``bound`` unchanged.

    Raises:
        InvalidTargetError: ``bound`` names another resource and the request
            names one too.
    """
    if not bound:
        return requested
    if is_same_mcp_resource(bound):
        return mcp_resource_identifier()
    if requested is not None:
        raise InvalidTargetError(
            description="The resource differs from the one this grant is bound to."
        )
    return bound


def _names_memory_scope(scopes: list[str]) -> bool:
    return any(scope.startswith("memory:") for scope in scopes)


def granted_scope(requested: str | None, registered: str | None) -> str:
    """Scope an authorization request is granted (RFC 6749 §3.3).

    The requested scopes that the client registered and that this server
    defines (``ALL_ADVERTISED_SCOPES``); other requested scopes are dropped,
    which §3.3 allows, and the token response carries the granted ``scope``.
    When that leaves no ``memory:*`` scope (the request had none the client
    may have, e.g. only ``openid`` / ``offline_access`` or scopes this server
    does not define, or no ``scope`` at all), the client's registered scope
    that this server defines is granted instead.

    Args:
        requested: The ``scope`` parameter of the request, if any.
        registered: The client's registered scope.

    Returns:
        The granted scopes, space-separated, in request (or registration)
        order without duplicates. Empty only when the registered scope has
        no ``memory:*`` scope this server defines.
    """
    advertised = set(ALL_ADVERTISED_SCOPES)
    registration = [
        scope for scope in dict.fromkeys((registered or "").split()) if scope in advertised
    ]
    if not _names_memory_scope(registration):
        return ""
    allowed = set(registration)
    granted = [scope for scope in dict.fromkeys((requested or "").split()) if scope in allowed]
    if _names_memory_scope(granted):
        return " ".join(granted)
    return " ".join(registration)


def _raise_if_no_scope(scope: str) -> None:
    if not scope:
        raise InvalidScopeError(description="This client is registered without a memory scope.")


def check_code_challenge(payload: Any, client: OAuth2Client, required: bool) -> None:
    """Validate the PKCE parameters of an authorization request (RFC 7636).

    ``code_challenge_method`` must be ``S256``, the one method the metadata
    advertises; an omitted method means ``plain`` (RFC 7636 §4.3). When
    ``required`` is set, a public client (``token_endpoint_auth_method="none"``)
    must send a ``code_challenge``: the token endpoint requires its verifier.

    Args:
        payload: The authorization request payload.
        client: The requesting client.
        required: Whether PKCE is enforced (``settings.oauth_pkce_required``).

    Raises:
        InvalidRequestError: The parameters break a rule above
            (RFC 7636 §4.4.1).
    """
    challenge = payload.data.get("code_challenge")
    method = payload.data.get("code_challenge_method")
    if not challenge and not method:
        if required and client.token_endpoint_auth_method == "none":
            raise InvalidRequestError(
                "Missing 'code_challenge'. Public clients must use PKCE with S256."
            )
        return
    if not challenge:
        raise InvalidRequestError("Missing 'code_challenge'")
    for name in ("code_challenge", "code_challenge_method"):
        if len(payload.datalist.get(name, [])) > 1:
            raise InvalidRequestError(f"Multiple '{name}' in request.")
    if not CODE_CHALLENGE_PATTERN.match(challenge):
        raise InvalidRequestError("Invalid 'code_challenge'")
    if method != PKCE_METHOD:
        raise InvalidRequestError("Unsupported 'code_challenge_method'. Only S256 is supported.")


def validate_authorization_parameters(client: OAuth2Client, payload: Any) -> str:
    """Apply the authorization request rules before the consent page is shown.

    The consent submission runs the same rules through the grant (scope, then
    PKCE when enforced, then ``resource``), so a request the page would accept
    is one the grant accepts.

    Args:
        client: The requesting client (its ``redirect_uri`` already checked).
        payload: The authorization request payload.

    Returns:
        The granted scope.

    Raises:
        OAuth2Error: ``invalid_scope``, ``invalid_request`` or
            ``invalid_target``.
    """
    scope = granted_scope(payload.data.get("scope"), client.scope)
    _raise_if_no_scope(scope)
    if get_settings().oauth_pkce_required:
        check_code_challenge(payload, client, required=True)
    requested_resource(payload)
    return scope


class S256CodeChallenge(CodeChallenge):
    """PKCE (RFC 7636) with ``S256`` as the only transformation.

    At the authorization endpoint the request must follow
    :func:`check_code_challenge`. At the token endpoint the verifier is
    checked against the stored challenge with ``S256``; a code stored with any
    other method yields ``invalid_grant``.
    """

    SUPPORTED_CODE_CHALLENGE_METHOD = [PKCE_METHOD]
    CODE_CHALLENGE_METHODS = {PKCE_METHOD: compare_s256_code_challenge}

    def validate_code_challenge(self, grant: Any, redirect_uri: Any = None) -> None:
        """Authorization-endpoint hook (``after_validate_authorization_request_payload``)."""
        check_code_challenge(grant.request.payload, grant.request.client, self.required)

    def validate_code_verifier(self, grant: Any, result: Any = None) -> None:
        """Token-endpoint hook (``after_validate_token_request``).

        Raises:
            InvalidRequestError: The verifier is missing or malformed.
            InvalidGrantError: The verifier does not match the challenge.
        """
        request = grant.request
        verifier = request.form.get("code_verifier")
        # A public client must always prove possession (RFC 7636 §4.5).
        if self.required and request.auth_method == "none" and not verifier:
            raise InvalidRequestError("Missing 'code_verifier'")

        authorization_code = request.authorization_code
        challenge = self.get_authorization_code_challenge(authorization_code)
        if not challenge and not verifier:
            return
        if not verifier:
            raise InvalidRequestError("Missing 'code_verifier'")
        if not CODE_VERIFIER_PATTERN.match(verifier):
            raise InvalidRequestError("Invalid 'code_verifier'")

        method = self.get_authorization_code_challenge_method(authorization_code)
        if method != PKCE_METHOD or not compare_s256_code_challenge(verifier, challenge):
            raise InvalidGrantError(description="Code challenge failed.")


class _OAuthUser:
    """Minimal user object for Authlib grant interfaces.

    Provides both ``user_id`` attribute (used by ``save_token``) and
    ``get_user_id()`` method (used by ``DeviceCodeGrant.query_user_grant``).
    """

    def __init__(self, user_id: str = "", email: str | None = None):
        self.user_id = user_id
        self.email = email

    def get_user_id(self) -> str:
        return self.user_id


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
    client = session.query(OAuth2Client).filter_by(client_id=client_id).first()
    logger.info(f"query_client: client_id={client_id}, found={client is not None}")

    return client


def save_token(
    token: dict[str, Any],
    request: OAuth2Request,
    session: Session,
    resource: str | None = None,
) -> None:
    """Save access token to database.

    Required by Authlib.

    Args:
        token: Token data from Authlib
        request: OAuth2 request object
        session: SQLAlchemy session
        resource: The token's audience (RFC 8707), as settled by the grant
            while it validated the token request; ``None`` for no audience.
    """
    # Extract client and user from request. Authlib types request.client as
    # ClientMixin; the runtime object is our OAuth2Client.
    client_id = cast(OAuth2Client, request.client).client_id
    user_id = getattr(getattr(request, "user", None), "user_id", None)

    # Fall back to the user of the credential being exchanged.
    if not user_id:
        user_id = getattr(getattr(request, "credential", None), "user_id", None)

    if not user_id:
        logger.error("Cannot save token: user_id not found in request")
        raise ValueError("User ID required for token issuance")

    # Create new token record
    oauth_token = OAuth2Token(
        client_id=client_id,
        user_id=user_id,
        token_type=token.get("token_type", "Bearer"),
        access_token=token["access_token"],
        refresh_token=token.get("refresh_token"),
        scope=token.get("scope", ""),
        expires_in=token.get("expires_in", 3600),
        resource=resource,  # RFC 8707 audience (Issue #157, #1686)
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


class _ResourceBoundGrant:
    """Hands the audience a grant settled for its token to ``save_token``.

    Each grant sets ``token_resource`` while it validates the token request
    (RFC 8707), and the issued token is stored with that audience.
    """

    #: Audience of the token being issued; ``None`` issues it without one.
    token_resource: str | None = None

    def save_token(self, token: dict[str, Any]) -> None:
        grant = cast(Any, self)
        save_token(token, grant.request, grant.server.db_session, resource=self.token_resource)


# ============================================================================
# Authorization Code Grant
# ============================================================================


class AuthorizationCodeGrant(_ResourceBoundGrant, grants.AuthorizationCodeGrant):
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
        - PKCE accepts ``S256`` only (``S256CodeChallenge``)
        - Granted scope: requested ∩ registered ∩ advertised (``granted_scope``)
        - ``resource`` (RFC 8707) must be this server's MCP resource; the
          code carries it and the token takes it as its audience
        - Authorization codes expire after 10 minutes
        - Codes are single-use (deleted after exchange)
        - Client secret required (confidential clients only)
    """

    # Token endpoint auth methods (Issue #157: Public Client + PKCE support)
    TOKEN_ENDPOINT_AUTH_METHODS = _TOKEN_ENDPOINT_AUTH_METHODS
    TOKEN_EXPIRES_IN = 3600

    def validate_requested_scope(self) -> None:
        """Refuse a request that would be granted no scope (RFC 6749 §3.3).

        Authlib calls this while it validates the authorization request, so an
        error here is reported by redirect to the client.

        Raises:
            InvalidScopeError: No requested scope can be granted.
        """
        client = cast(OAuth2Client, self.request.client)
        _raise_if_no_scope(granted_scope(self.request.payload.scope, client.scope))

    def validate_authorization_request(self) -> str:
        """Validate the authorization request, then its ``resource`` (RFC 8707).

        Returns:
            The validated redirect URI.

        Raises:
            OAuth2Error: The request is invalid; ``invalid_target`` when the
                ``resource`` is not this server's MCP resource.
        """
        redirect_uri = super().validate_authorization_request()
        try:
            requested_resource(self.request.payload)
        except OAuth2Error as error:
            error.redirect_uri = redirect_uri
            raise
        return redirect_uri

    def validate_token_request(self) -> None:
        """Validate the code exchange and settle the token's audience.

        The audience is the ``resource`` the authorization request carried
        with the code, or else the token request's (RFC 8707 §2.2); see
        :func:`settle_audience`.

        Raises:
            OAuth2Error: The request is invalid; ``invalid_target`` when the
                token request's ``resource`` is not this server's MCP resource
                or differs from the code's.
        """
        super().validate_token_request()
        code = cast(OAuth2AuthorizationCode, self.request.authorization_code)
        self.token_resource = settle_audience(
            requested_resource(self.request.payload), code.resource
        )

    def generate_token(
        self,
        user=None,
        scope=None,
        grant_type=None,
        expires_in=None,
        include_refresh_token=True,
    ) -> dict[str, Any]:
        client = self.request.client
        if expires_in is None:
            expires_in = self.TOKEN_EXPIRES_IN
        if grant_type is None:
            grant_type = self.GRANT_TYPE
        return _generate_token_with_expiry(grant_type, client, expires_in, scope)

    def save_authorization_code(self, code: str, request: OAuth2Request) -> OAuth2AuthorizationCode:
        """Save authorization code to database.

        Called by Authlib after user authorization, once the request has
        passed ``validate_authorization_request``. The code stores what that
        validated request carried: the granted scope, the PKCE challenge and
        the ``resource`` (RFC 8707) the token will be bound to.

        Args:
            code: Generated authorization code
            request: OAuth2 request object

        Returns:
            Saved OAuth2AuthorizationCode instance
        """
        # Extract data from request (Authlib ClientMixin → OAuth2Client narrow).
        client = cast(OAuth2Client, request.client)
        client_id = client.client_id
        user_id = getattr(getattr(request, "user", None), "user_id", None)
        payload = request.payload
        request_data = payload.data

        scope = granted_scope(request_data.get("scope"), client.scope)
        code_challenge = request_data.get("code_challenge")
        code_challenge_method = request_data.get("code_challenge_method")
        resource = requested_resource(payload)

        logger.info(
            "authorization_code_data",
            resource=resource,
            code_challenge_present=code_challenge is not None,
        )

        if not user_id:
            logger.error("Cannot save authorization code: user_id not found")
            raise ValueError("User ID required for authorization")

        # Create authorization code record (expires in 10 minutes)
        auth_code = OAuth2AuthorizationCode(
            code=code,
            client_id=client_id,
            user_id=user_id,
            redirect_uri=request_data.get("redirect_uri"),
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            resource=resource,  # RFC 8707 (Issue #157, #1686)
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

        Called by Authlib during token exchange. The row is locked
        (``SELECT ... FOR UPDATE``) until the exchange commits, so a concurrent
        exchange of the same code waits for it and then finds no code.

        Args:
            code: Authorization code
            client: OAuth2Client instance

        Returns:
            OAuth2AuthorizationCode or None
        """
        auth_code = (
            self.server.db_session.query(OAuth2AuthorizationCode)
            .filter_by(code=code, client_id=client.client_id)
            .with_for_update()
            .first()
        )

        # Check expiration
        if auth_code and auth_code.is_expired():
            logger.warning(
                f"Authorization code expired: code={code[:8]}..., client={client.client_id}"
            )
            return None

        return auth_code

    def save_token(self, token: dict[str, Any]) -> None:
        """Consume the authorization code and store the token in one transaction.

        The code row is deleted first and the delete must remove exactly that
        row, so of two exchanges of one code only one issues a token.

        Raises:
            InvalidGrantError: The code was consumed by another exchange.
        """
        code = cast(OAuth2AuthorizationCode, self.request.authorization_code)
        client_id = code.client_id
        session = self.server.db_session
        consumed = (
            session.query(OAuth2AuthorizationCode)
            .filter_by(id=code.id)
            .delete(synchronize_session=False)
        )
        if consumed != 1:
            session.rollback()
            logger.warning("authorization_code_already_consumed", client_id=client_id)
            raise InvalidGrantError("Invalid 'code' in request.")
        super().save_token(token)
        logger.info("authorization_code_consumed", client_id=client_id)

    def delete_authorization_code(self, authorization_code: OAuth2AuthorizationCode) -> None:
        """Called by Authlib after a successful exchange.

        Nothing is left to do: :meth:`save_token` deleted the code in the
        transaction that stored the token.

        Args:
            authorization_code: The exchanged code (no longer in the database).
        """

    def authenticate_user(self, authorization_code: OAuth2AuthorizationCode) -> Any:
        """Get user from authorization code.

        Called by Authlib to attach user to token request.

        Args:
            authorization_code: OAuth2AuthorizationCode instance

        Returns:
            User object (must have user_id attribute)
        """

        return _OAuthUser(user_id=authorization_code.user_id)


# ============================================================================
# Refresh Token Grant
# ============================================================================


class RefreshTokenGrant(_ResourceBoundGrant, grants.RefreshTokenGrant):
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
        - The requested scope cannot exceed the original grant
        - The new token keeps the audience of the refreshed one (RFC 8707)
    """

    # Token endpoint auth methods (Issue #157: Public Client + PKCE support)
    TOKEN_ENDPOINT_AUTH_METHODS = _TOKEN_ENDPOINT_AUTH_METHODS
    TOKEN_EXPIRES_IN = 3600

    def validate_token_request(self) -> None:
        """Validate the refresh request and settle the new token's audience.

        The new token keeps the refreshed token's audience, stored as the
        published identifier when it names the MCP resource in any accepted
        form (a token may carry ``.../mcp/w/<id>`` or ``.../mcp?profile=...``
        from before). A token issued without one takes the request's
        ``resource``, which can only name this server's MCP resource and so
        narrows it. See :func:`settle_audience`.

        Raises:
            OAuth2Error: The request is invalid; ``invalid_target`` when the
                ``resource`` is not this server's MCP resource or differs from
                the refreshed token's audience.
        """
        super().validate_token_request()
        credential = cast(OAuth2Token, self.request.refresh_token)
        self.token_resource = settle_audience(
            requested_resource(self.request.payload), credential.resource
        )

    def generate_token(
        self,
        user=None,
        scope=None,
        grant_type=None,
        expires_in=None,
        include_refresh_token=True,
    ) -> dict[str, Any]:
        client = self.request.client
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

        return _OAuthUser(user_id=credential.user_id)

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
# Device Authorization Grant (RFC 8628, Issue #536)
# ============================================================================


class DeviceAuthorizationGrant(_ResourceBoundGrant, _DeviceCodeGrant):
    """Device Authorization Grant for CLI tools (Claude Code, etc.).

    Implements steps (E) and (F) of RFC 8628 — the polling loop where
    a CLI client repeatedly asks the token endpoint whether the user
    has completed browser-based authorization.
    """

    TOKEN_ENDPOINT_AUTH_METHODS = _TOKEN_ENDPOINT_AUTH_METHODS
    TOKEN_EXPIRES_IN = 3600

    def validate_token_request(self) -> None:
        """Validate the polling request; its ``resource`` sets the audience.

        Raises:
            OAuth2Error: The request is invalid or not yet approved;
                ``invalid_target`` when ``resource`` is not this server's MCP
                resource.
        """
        # Checked first so a polling client learns of it before approval.
        resource = requested_resource(self.request.payload)
        super().validate_token_request()
        self.token_resource = resource

    def generate_token(
        self,
        user=None,
        scope=None,
        grant_type=None,
        expires_in=None,
        include_refresh_token=True,
    ) -> dict[str, Any]:
        # Override required: Authlib's BaseGrant.generate_token delegates to
        # server.generate_token, which is intentionally unset on the wrapper
        # (see _register_grants — "Grant methods take precedence").
        client = self.request.client
        if expires_in is None:
            expires_in = self.TOKEN_EXPIRES_IN
        if grant_type is None:
            grant_type = self.GRANT_TYPE
        token = _generate_token_with_expiry(grant_type, client, expires_in, scope)

        # Attach identity (email + workspace) to the response body for SDK
        # display. Lawful basis: /device consent (RFC 8628 §3.3, APPI 第27条,
        # GDPR Art.6(1)(a)); recipient disclosure: Privacy Policy thirdParty
        # section. NOT security-bearing — do not use for authorization.
        # Workspace is User.current_workspace_id at grant time (point-in-time,
        # not consent-bound — OAuth2DeviceCode lacks a workspace column).
        # ``getattr`` default guards the contract verified by
        # tests/auth/test_device_code_grant.py: callers may pass ``user=None``
        # to assert the base token-shape; identity injection is skipped then.
        user_id = getattr(user, "user_id", None)
        if user_id:
            user_row = self.server.db_session.query(User).filter_by(user_id=user_id).first()
            if user_row:
                token["user_email"] = user_row.email
                if user_row.current_workspace_id:
                    # Filter out soft-deleted workspaces. Matches the project
                    # convention (services/workspace_service.py and others).
                    # The FK ondelete=SET NULL only covers hard deletes; soft
                    # deletes via Workspace.deleted_at would otherwise leak a
                    # stale workspace_id/name into the token response.
                    workspace = (
                        self.server.db_session.query(Workspace)
                        .filter_by(id=user_row.current_workspace_id)
                        .filter(Workspace.deleted_at.is_(None))
                        .first()
                    )
                    if workspace:
                        token["workspace_id"] = str(workspace.id)
                        token["workspace_name"] = workspace.name

        return token

    def query_device_credential(self, device_code: str) -> OAuth2DeviceCode | None:
        return (
            self.server.db_session.query(OAuth2DeviceCode)
            .filter_by(device_code=device_code)
            .first()
        )

    def query_user_grant(self, user_code: str) -> tuple[Any, bool] | None:
        device = (
            self.server.db_session.query(OAuth2DeviceCode).filter_by(user_code=user_code).first()
        )
        if device is None or device.is_expired():
            return None
        if device.denied_at is not None:
            return _OAuthUser(), False
        if device.authorized_at is not None and device.user_id:
            return _OAuthUser(user_id=device.user_id), True
        return None

    def should_slow_down(self, credential: OAuth2DeviceCode) -> bool:
        if credential.last_polled_at is None:
            credential.last_polled_at = utcnow()
            self.server.db_session.commit()
            return False
        interval = get_settings().oauth_device_polling_interval
        elapsed = (utcnow() - credential.last_polled_at).total_seconds()
        credential.last_polled_at = utcnow()
        self.server.db_session.commit()
        return elapsed < interval


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
            # Authlib uses runtime-attached attributes for db_session and the
            # query_client / save_token hooks. Declared here so static analyzers
            # see them; Authlib's behavior is unchanged.
            db_session: Any = None

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
                # Do NOT log ``payload`` or ``headers`` — token responses
                # (RFC 6749 §5.1) carry ``access_token`` / ``refresh_token``
                # in the payload and ``Authorization`` headers may be echoed
                # back, both of which are credentials. Logging them at INFO
                # leaks secrets into the application log stream. Status code
                # alone is enough for operational visibility; deeper detail
                # belongs at DEBUG with explicit redaction in a follow-up.
                logger.info("oauth2_handle_response", status_code=status_code)

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
                logger.debug(f"OAuth2 signal: {name}, kwargs={list(kwargs.keys())}")
                return

        # Create query_client function. Authlib's ClientMixin contract is
        # duck-typed here — OAuth2Client implements the methods Authlib needs
        # but does not formally inherit from ClientMixin (avoiding SQLAlchemy
        # Base × Authlib mixin metaclass conflict). Returning `Any` keeps the
        # assignment to AuthorizationServer.query_client well-typed.
        def query_client_func(client_id: str) -> Any:
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
        """Register grant types with the server.

        Issue #513: register the RFC 7636 ``CodeChallenge`` extension on the
        Authorization Code Grant so that public clients (``token_endpoint_auth_method="none"``,
        ChatGPT/Claude/Cursor/Claude Code CLI) cannot exchange an authorization
        code without a valid ``code_verifier``. Issue #157 added public-client
        support and PKCE plumbing but never registered this extension, leaving
        the gate unenforced — Authlib's ``CodeChallenge`` only triggers when
        explicitly registered.

        The kill-switch ``settings.oauth_pkce_required`` controls whether the
        extension is registered at all. Registering ``CodeChallenge(required=False)``
        still enforces ``code_verifier`` whenever a ``code_challenge`` is stored
        on the authorization code (Authlib's "challenge stored → verifier
        required" branch fires regardless of the flag), so a true rollback to
        pre-#513 behavior requires SKIPPING registration. We therefore:
        - register ``S256CodeChallenge(required=True)`` when the kill-switch is
          on (default): ``S256`` only, and a public client must send a
          ``code_challenge`` at the authorization endpoint (#1686)
        - skip registration entirely when the kill-switch is off (emergency
          rollback path; matches pre-#513 behavior exactly).
        """
        pkce_required = bool(get_settings().oauth_pkce_required)

        if pkce_required:
            self.server.register_grant(
                AuthorizationCodeGrant,
                [S256CodeChallenge(required=True)],
            )
        else:
            # Emergency rollback: pre-#513 behavior with no PKCE enforcement.
            self.server.register_grant(AuthorizationCodeGrant)

        # Refresh Token Grant
        self.server.register_grant(RefreshTokenGrant)

        # Device Authorization Grant (RFC 8628, Issue #536) — no PKCE
        self.server.register_grant(DeviceAuthorizationGrant)

        logger.info(
            "oauth2_server_initialized",
            grants=[
                "authorization_code",
                "refresh_token",
                "urn:ietf:params:oauth:grant-type:device_code",
            ],
            pkce_required=pkce_required,
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
        # AuthorizationServer.validate_consent_request is provided by Authlib
        # at runtime but missing from the type stub. Use cast to silence
        # static check while preserving behavior.
        return cast(Any, self.server).validate_consent_request(request)


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
