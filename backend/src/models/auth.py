"""SQLAlchemy models for authentication and authorization.

Based on: kagura-ai/src/kagura/auth/models.py

Provides ORM models for:
- users table (OAuth2 users with RBAC)
- audit_logs table (security audit trail)
- api_keys table (Kagura API key management)
- external_api_keys table (OpenAI, Cohere etc. - DB managed)
- oauth_clients table (OAuth2 client applications - Issue #33)
- oauth_authorization_codes table (OAuth2 authorization codes - Issue #33)
- oauth_tokens table (OAuth2 access/refresh tokens - Issue #33)
- contexts table (Memory contexts - Issue #160, renamed from projects)
- context_members table (Context membership - Issue #160)
- mcp_tool_descriptions table (MCP tool i18n - Issue #160)
"""

import secrets
import uuid
from datetime import date as date_type
from datetime import datetime
from functools import cached_property
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from auth.mcp_scopes import DCR_DEFAULT_SCOPE
from db.base import Base
from utils.datetime import utcnow
from utils.redirect_uri import any_redirect_uri_matches


class User(Base):
    """User model for OAuth2 authentication and RBAC.

    System-level Roles (User.role):
        - admin: System Administrator (platform-wide access)
        - user: Standard user

    Workspace-level Roles (WorkspaceMember.role):
        - owner: Workspace owner
        - admin: Workspace admin (DIFFERENT from system admin)
        - member: Workspace member
        - viewer: Workspace viewer (read-only)

    Issue #166: System Admin vs Workspace Admin separation

    Attributes:
        id: Primary key
        email: User email (unique, from OAuth2)
        user_id: OAuth2 sub claim (unique)
        name: Display name
        picture: Profile picture URL
        role: Access control role (admin/user)
        is_initial_admin: First system admin flag (cannot be deleted/demoted)
        created_at: Account creation timestamp
        updated_at: Last modification timestamp
        last_login_at: Last login timestamp
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # OAuth2 Identity
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    picture: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Issue #175: User Preferences
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="UTC", index=True)

    # Issue #221: i18n support
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="en", index=True)

    # Role & Permissions
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="user", index=True)

    # Issue #166: System Admin Protection
    is_initial_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )

    # Workspace (Issue #115 Phase B)
    current_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Issue #246: current_context_id removed (context always explicit from Frontend/API)

    # Issue #51: Password + MFA authentication
    login_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="oauth", index=True
    )
    totp_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Fernet-encrypted
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    auth_provider: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # Issue #361: google, github

    # Relationships
    current_workspace: Mapped["Workspace | None"] = relationship(
        "Workspace", foreign_keys=[current_workspace_id]
    )

    # Constraints
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="valid_role"),
        CheckConstraint("auth_method IN ('password', 'oauth')", name="valid_auth_method"),
    )

    def __repr__(self) -> str:
        return f"<User(email='{self.email}', role='{self.role}')>"


class AuditLog(Base):
    """Audit log model for security-sensitive operations.

    Security:
        - old_value_hash/new_value_hash store SHA256 hashes, NOT plaintext

    Attributes:
        id: Primary key
        user_email: Email of user who performed the action
        user_id: OAuth2 sub of user
        action: Action type (config_update, role_assign, etc.)
        resource: Resource identifier
        old_value_hash: SHA256 hash of old value
        new_value_hash: SHA256 hash of new value
        user_metadata: Additional context (JSON)
        ip_address: Client IP address
        user_agent: Client user agent
        created_at: Timestamp
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Who
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # What
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Details (SHA256 hashes, NOT plaintext!)
    old_value_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_value_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Context
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<AuditLog(action='{self.action}', resource='{self.resource}')>"


class APIKey(Base):
    """Kagura API Key model for programmatic access.

    Security:
        - SHA256 hash (never stores plaintext after creation)
        - Optional expiration
        - Revocation support

    Attributes:
        id: Primary key
        key_hash: SHA256 hash of API key
        key_prefix: First 16 characters (for display)
        name: Friendly name
        user_id: Owner user ID
        created_at: Creation timestamp
        last_used_at: Last usage timestamp
        revoked_at: Revocation timestamp (NULL = active)
        expires_at: Expiration timestamp (NULL = no expiration)
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # API Key Data
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Issue #169: Workspace-scoped API keys (access all contexts in workspace)
    # Migration 034: context_id removed (deprecated)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Migration 034: Visibility control (Zero-knowledge model)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    visibility_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Migration 035: Encrypted plaintext for display until hidden
    plaintext_encrypted: Mapped[str | None] = mapped_column(String, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_user_name", "user_id", "name"),
        Index("idx_revoked", "revoked_at"),
        Index("idx_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<APIKey(name='{self.name}', prefix='{self.key_prefix}')>"


class ExternalAPIKey(Base):
    """External API Key model for third-party services (OpenAI, Cohere etc.).

    User-specific API keys stored encrypted in database.

    Security:
        - Fernet symmetric encryption (can decrypt for usage)
        - Per-user API keys
        - Audit trail

    Attributes:
        id: Primary key
        key_name: Unique identifier (e.g., "openai_embedding")
        provider: Service provider (e.g., "openai", "cohere")
        encrypted_value: Fernet-encrypted API key value
        user_id: Owner user ID
        created_at: Creation timestamp
        updated_at: Last modification timestamp
        updated_by: Email of user who last modified
    """

    __tablename__ = "external_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # API Key Identity
    key_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Encrypted Value (Fernet)
    encrypted_value: Mapped[str] = mapped_column(String, nullable=False)

    # Owner
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Issue #82 → #160: Context-scoped external keys (renamed from project)
    context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=True,
    )

    # Issue #146: Workspace-scoped external keys
    # Issue #385: NOT NULL — every external API key belongs to exactly one workspace.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Issue #105: Enable/disable state
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Audit Trail
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_external_user_provider", "user_id", "provider"),
        Index("idx_external_updated", "updated_at"),
        Index("idx_external_enabled", "user_id", "provider", "enabled"),  # Issue #105
        # Issue #385: at most one enabled key per (workspace, context, provider).
        # Includes context_id so the service-layer context > workspace priority
        # contract (embedding / llm / reranker) permits a context-scoped key and
        # a workspace-scoped (context_id IS NULL) fallback to coexist for the
        # same provider. Disabled keys are exempt so an owner can hold a spare
        # key in disabled state. NULLS NOT DISTINCT (PG 15+) makes NULL
        # context_ids collide, blocking two workspace-scoped rows for the same
        # provider.
        Index(
            "uq_external_api_keys_workspace_provider_enabled",
            "workspace_id",
            "context_id",
            "provider",
            unique=True,
            postgresql_where=text("enabled = true"),
            postgresql_nulls_not_distinct=True,
        ),
        # Issue #385: unique key_name per workspace. Guarantees the
        # scalar_one_or_none() lookups in the update/toggle/delete handlers can
        # never raise MultipleResultsFound (→ 500) on legacy data pre-#381 that
        # had per-user uniqueness only.
        Index(
            "uq_external_api_keys_workspace_key_name",
            "workspace_id",
            "key_name",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        status = "enabled" if self.enabled else "disabled"
        return f"<ExternalAPIKey(key_name='{self.key_name}', provider='{self.provider}', {status})>"


# ============================================================================
# OAuth2 Authorization Server Models (Issue #33)
# ============================================================================


class OAuth2Client(Base):
    """OAuth2 Client model for registered applications.

    Stores OAuth2 client registration data for applications that want to
    access Kagura Memory Cloud API (e.g., ChatGPT Connectors, Claude Desktop).

    Attributes:
        id: Primary key
        client_id: OAuth2 client identifier (unique, public)
        client_secret_hash: SHA256 hash of client secret (confidential, always required)
        client_name: Human-readable name (e.g., "ChatGPT Connector")
        owner_id: User ID who registered this client. ``None`` for clients
            created via Dynamic Client Registration (RFC 7591) — DCR is a
            public, owner-less endpoint, so the workspace context is resolved
            from the consenting user's session at ``/authorize`` time
            rather than from the client record. See migration revision
            ``d04_519_oauth_owner_nullable`` (issue #519).
        redirect_uris: Allowed redirect URIs (JSON array)
        grant_types: Allowed grant types (JSON array: authorization_code, refresh_token)
        response_types: Allowed response types (JSON array: code)
        scope: Allowed scopes (space-separated: memory:read, memory:write, memory:admin)
        token_endpoint_auth_method: Client authentication method (client_secret_post, client_secret_basic)
        created_at: Registration timestamp
        updated_at: Last modification timestamp

    Authlib Integration:
        Implements the required interface for Authlib's AuthorizationCodeGrant:
        - get_client_id(): Returns client_id
        - get_default_redirect_uri(): Returns first redirect_uri
        - check_redirect_uri(redirect_uri): Validates redirect_uri
        - has_client_secret(): Returns True (always)
        - check_client_secret(secret): Validates client secret
        - check_token_endpoint_auth_method(method): Validates auth method
        - check_response_type(response_type): Validates response_type
        - check_grant_type(grant_type): Validates grant_type
        - get_allowed_scope(scope): Returns intersection of requested and allowed scopes
    """

    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Client Identity
    client_id: Mapped[str] = mapped_column(String(48), nullable=False, unique=True, index=True)
    client_secret_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # SHA256 hash (always required)
    client_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Issue #519 (#513 follow-up): DCR-registered clients (RFC 7591) have no
    # human owner — ``dynamic_client_registration`` creates them with
    # ``owner_id=None``. Admin-managed clients still record the creating
    # user's id. Migration revision ``d04_519_oauth_owner_nullable`` drops
    # the corresponding NOT NULL DB constraint.
    owner_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Issue #169: Workspace-scoped OAuth clients (access all contexts in workspace)
    # Migration 034: context_id removed (deprecated)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # OAuth2 Configuration
    redirect_uris: Mapped[list[str]] = mapped_column(
        JSON, nullable=False
    )  # ["https://example.com/callback"]
    grant_types: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=["authorization_code", "refresh_token"]
    )
    response_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=["code"])
    scope: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        # Canonical set lives in auth.mcp_scopes — #592 drift fix. Previously
        # this default was "memory:read memory:write offline_access" which
        # silently omitted memory:admin and drifted from the well-known
        # metadata endpoints. Existing rows are migrated by Alembic
        # e08_592_oauth_scope_canonicalize.
        default=DCR_DEFAULT_SCOPE,
    )
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(50), nullable=False, default="client_secret_post"
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )

    # Migration 034: Visibility control (Zero-knowledge model)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    visibility_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Migration 035: Encrypted plaintext secret for display until hidden
    plaintext_secret_encrypted: Mapped[str | None] = mapped_column(String, nullable=True)

    # Migration 036: Provider type (claude, chatgpt, custom)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, server_default="custom")

    # Relationships
    tokens: Mapped[list["OAuth2Token"]] = relationship(
        "OAuth2Token", back_populates="client", cascade="all, delete"
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<OAuth2Client(client_id='{self.client_id}', name='{self.client_name}')>"

    # ========================================================================
    # Authlib Interface Methods
    # ========================================================================

    def get_client_id(self) -> str:
        """Get client identifier.

        Required by Authlib.

        Returns:
            Client ID string
        """
        return self.client_id

    def get_default_redirect_uri(self) -> str | None:
        """Get default redirect URI.

        Required by Authlib.

        Returns:
            First redirect URI or None
        """
        if self.redirect_uris and len(self.redirect_uris) > 0:
            return self.redirect_uris[0]
        return None

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        """Validate redirect URI.

        Required by Authlib. Supports trailing ``/*`` wildcard patterns in
        registered ``redirect_uris`` to accommodate ChatGPT/Claude per-connector
        dynamic callback URLs (Issue #207). Exact matching is preserved for
        patterns without ``*``.

        Args:
            redirect_uri: Redirect URI to validate

        Returns:
            True if redirect_uri matches any registered pattern
        """
        return any_redirect_uri_matches(self.redirect_uris, redirect_uri)

    def has_client_secret(self) -> bool:
        """Check if client has a secret.

        Required by Authlib.

        Returns:
            True (always, since client_secret_hash is NOT NULL)
        """
        return True

    def check_client_secret(self, secret: str) -> bool:
        """Validate client secret.

        Required by Authlib.

        Args:
            secret: Client secret to validate

        Returns:
            True if secret matches stored hash
        """
        import hashlib

        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        return secrets.compare_digest(secret_hash, self.client_secret_hash)

    def check_token_endpoint_auth_method(self, method: str) -> bool:
        """Validate token endpoint authentication method.

        Required by Authlib.

        Args:
            method: Authentication method (e.g., "client_secret_post")

        Returns:
            True if method matches registered method
        """
        return method == self.token_endpoint_auth_method

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        """Validate endpoint authentication method.

        Required by Authlib 1.6.5+ for client authentication.

        Args:
            method: Authentication method (e.g., "client_secret_post")
            endpoint: Endpoint type (e.g., "token", "revocation")

        Returns:
            True if method matches registered method
        """
        if endpoint == "token":
            return self.check_token_endpoint_auth_method(method)
        # For other endpoints, allow if it matches token endpoint method
        return method == self.token_endpoint_auth_method

    def check_response_type(self, response_type: str) -> bool:
        """Validate response type.

        Required by Authlib.

        Args:
            response_type: Response type (e.g., "code")

        Returns:
            True if response_type is in registered response_types
        """
        return response_type in self.response_types

    def check_grant_type(self, grant_type: str) -> bool:
        """Validate grant type.

        Required by Authlib.

        Args:
            grant_type: Grant type (e.g., "authorization_code")

        Returns:
            True if grant_type is in registered grant_types
        """
        return grant_type in self.grant_types

    def get_allowed_scope(self, scope: str) -> str:
        """Get intersection of requested and allowed scopes.

        Required by Authlib.

        Args:
            scope: Requested scope (space-separated)

        Returns:
            Allowed scope (intersection of requested and registered)
        """
        if not scope:
            return self.scope

        requested = set(scope.split())
        allowed = set(self.scope.split())
        return " ".join(requested & allowed)


class OAuth2AuthorizationCode(Base):
    """OAuth2 Authorization Code model.

    Stores short-lived authorization codes issued during OAuth2 flow.
    Codes are single-use and expire after 10 minutes (RFC 6749 recommendation).

    Attributes:
        id: Primary key
        code: Authorization code (unique, random)
        client_id: Client that requested the code
        user_id: User who authorized (OAuth2 sub)
        redirect_uri: Redirect URI used in authorization request
        scope: Granted scope (space-separated)
        code_challenge: PKCE code challenge (optional, not used but kept for future)
        code_challenge_method: PKCE method ("S256" or "plain")
        auth_time: Authorization timestamp (when user consented)
        expires_at: Expiration timestamp (auth_time + 600s)

    Authlib Integration:
        Implements the required interface for Authlib's AuthorizationCodeGrant:
        - get_redirect_uri(): Returns redirect_uri
        - get_scope(): Returns scope
        - get_auth_time(): Returns auth_time
        - get_code_challenge(): Returns code_challenge
        - get_code_challenge_method(): Returns code_challenge_method
        - is_expired(): Checks if code is expired
    """

    __tablename__ = "oauth_authorization_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Authorization Code Data
    code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    client_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # OAuth2 Flow Data
    redirect_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # PKCE Support (RFC 7636 - not enforced but kept for future)
    code_challenge: Mapped[str | None] = mapped_column(String(128), nullable=True)
    code_challenge_method: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # RFC 8707 Resource Indicators (Issue #157)
    resource: Mapped[str | None] = mapped_column(String(512), nullable=True)  # Audience for token

    # Timestamps
    auth_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    # Indexes
    __table_args__ = (
        Index("idx_oauth_codes_client_user", "client_id", "user_id"),
        Index("idx_oauth_codes_resource", "resource"),  # RFC 8707
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<OAuth2AuthorizationCode(code='{self.code[:8]}...', client='{self.client_id}')>"

    # ========================================================================
    # Authlib Interface Methods
    # ========================================================================

    def get_redirect_uri(self) -> str:
        """Get redirect URI.

        Required by Authlib.

        Returns:
            Redirect URI string
        """
        return self.redirect_uri

    def get_scope(self) -> str | None:
        """Get granted scope.

        Required by Authlib.

        Returns:
            Scope string or None
        """
        return self.scope

    def get_auth_time(self) -> int:
        """Get authorization timestamp.

        Required by Authlib.

        Returns:
            Unix timestamp (seconds since epoch)
        """
        return int(self.auth_time.timestamp())

    def get_code_challenge(self) -> str | None:
        """Get PKCE code challenge.

        Required by Authlib (PKCE extension).

        Returns:
            Code challenge string or None
        """
        return self.code_challenge

    def get_code_challenge_method(self) -> str | None:
        """Get PKCE code challenge method.

        Required by Authlib (PKCE extension).

        Returns:
            Method ("S256" or "plain") or None
        """
        return self.code_challenge_method

    def is_expired(self) -> bool:
        """Check if authorization code is expired.

        Required by Authlib.

        Returns:
            True if code is expired (current time > expires_at)
        """
        return utcnow() > self.expires_at


def generate_user_code(length: int = 8) -> str:
    """Generate a random user code for Device Authorization Grant.

    Returns an uppercase alphanumeric string of the given length.
    Used by the device authorization endpoint to create human-typable codes.

    Args:
        length: Code length (default 8, per RFC 8628 recommendations).

    Returns:
        Random uppercase alphanumeric string.
    """
    import string

    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class OAuth2DeviceCode(Base):
    """OAuth2 Device Authorization Code model (RFC 8628, Issue #536).

    Stores pending device authorization requests. Created when a CLI client
    calls the device authorization endpoint; updated when the user approves
    or denies via the browser consent flow.

    Authlib Integration:
        Implements the required interface for Authlib's DeviceCodeGrant:
        - get_client_id(): Returns client_id
        - get_user_code(): Returns user_code
        - get_scope(): Returns scope
        - is_expired(): Checks if device code is expired
    """

    __tablename__ = "oauth_device_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    device_code: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    user_code: Mapped[str] = mapped_column(String(8), nullable=False, unique=True, index=True)
    client_id: Mapped[str] = mapped_column(
        String(48),
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    denied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    client: Mapped["OAuth2Client"] = relationship("OAuth2Client")

    def __repr__(self) -> str:
        return f"<OAuth2DeviceCode(device_code='{self.device_code[:8]}...', client='{self.client_id}')>"

    # ========================================================================
    # Authlib Interface Methods
    # ========================================================================

    def get_client_id(self) -> str:
        return self.client_id

    def get_user_code(self) -> str:
        return self.user_code

    def get_scope(self) -> str | None:
        return self.scope

    def is_expired(self) -> bool:
        return utcnow() > self.expires_at


class OAuth2Token(Base):
    """OAuth2 Access Token model.

    Stores access tokens and refresh tokens issued to clients.

    Attributes:
        id: Primary key
        client_id: Client that owns the token
        user_id: User who authorized (OAuth2 sub)
        token_type: Token type (always "Bearer")
        access_token: Access token value (unique, random)
        refresh_token: Refresh token value (optional, unique, random)
        scope: Granted scope (space-separated)
        revoked: Revocation status (False = active)
        issued_at: Token issuance timestamp
        access_token_revoked_at: Access token revocation timestamp (NULL = active)
        refresh_token_revoked_at: Refresh token revocation timestamp (NULL = active)
        expires_in: Access token lifetime in seconds (default: 3600)

    Authlib Integration:
        Implements the required interface for Authlib's AuthorizationServer:
        - get_client_id(): Returns client_id
        - get_scope(): Returns scope
        - get_expires_in(): Returns expires_in
        - get_expires_at(): Returns issued_at + expires_in
        - is_expired(): Checks if access token is expired
        - is_revoked(): Checks if access token is revoked
        - is_refresh_token_active(): Checks if refresh token is valid
    """

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Token Owner
    client_id: Mapped[str] = mapped_column(
        String(48),
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Token Data
    token_type: Mapped[str] = mapped_column(String(20), nullable=False, default="Bearer")
    access_token: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    refresh_token: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Status
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)

    # Timestamps
    issued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    access_token_revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_token_revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Expiration
    expires_in: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)  # 1 hour

    # RFC 8707 Resource Indicators (Issue #157)
    resource: Mapped[str | None] = mapped_column(String(512), nullable=True)  # Audience claim (aud)

    # Relationships
    client: Mapped["OAuth2Client"] = relationship("OAuth2Client", back_populates="tokens")

    # Indexes
    __table_args__ = (
        Index("idx_oauth_tokens_client_user", "client_id", "user_id"),
        Index("idx_oauth_tokens_resource", "resource"),  # RFC 8707
    )

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<OAuth2Token(access_token='{self.access_token[:8]}...', client='{self.client_id}')>"
        )

    # ========================================================================
    # Authlib Interface Methods
    # ========================================================================

    def get_client_id(self) -> str:
        """Get client identifier.

        Required by Authlib.

        Returns:
            Client ID string
        """
        return self.client_id

    def get_scope(self) -> str | None:
        """Get granted scope.

        Required by Authlib.

        Returns:
            Scope string or None
        """
        return self.scope

    def get_expires_in(self) -> int:
        """Get access token lifetime.

        Required by Authlib.

        Returns:
            Lifetime in seconds
        """
        return self.expires_in

    def get_expires_at(self) -> int:
        """Get access token expiration timestamp.

        Required by Authlib.

        Returns:
            Unix timestamp (seconds since epoch)
        """
        return int(self.issued_at.timestamp()) + self.expires_in

    def is_expired(self) -> bool:
        """Check if access token is expired.

        Required by Authlib.

        Returns:
            True if token is expired (current time > issued_at + expires_in)
        """
        # ====================================================================
        # BUG FIX #83-5: OAuth2 token timezone consistency
        # ====================================================================
        # Problem: time.time() (float, UTC timestamp) was compared with
        #          get_expires_at() which uses issued_at (timezone-aware datetime).
        #          This mixing of naive/aware datetimes could cause issues.
        #
        # Solution: Use utcnow().timestamp() for consistency with
        #           issued_at field (which is timezone-aware).
        #
        # Impact: More reliable token expiry checks, especially around DST
        #         transitions or timezone changes.
        # ====================================================================

        return utcnow().timestamp() > self.get_expires_at()

    def is_revoked(self) -> bool:
        """Check if access token is revoked.

        Required by Authlib.

        Returns:
            True if access_token_revoked_at is set or revoked flag is True
        """
        return self.revoked or self.access_token_revoked_at is not None

    def is_refresh_token_active(self) -> bool:
        """Check if refresh token is valid.

        Required by Authlib (for refresh token grant).

        Returns:
            True if refresh token exists and is not revoked
        """
        if not self.refresh_token:
            return False
        if self.refresh_token_revoked_at is not None:
            return False
        return True

    def check_client(self, client: "OAuth2Client") -> bool:
        """Check if token belongs to the given client.

        Required by Authlib RefreshTokenGrant._validate_request_token().

        Args:
            client: OAuth2Client instance to check against

        Returns:
            True if token was issued to this client, False otherwise
        """
        return self.client_id == client.client_id


class UsageStats(Base):
    """API usage statistics for quota management.

    Tracks all API requests for billing and quota enforcement.
    Issue #48 - Usage Statistics - Plan Limits & Usage Tracking

    Attributes:
        id: Primary key
        user_id: OAuth2 user ID (sub claim)
        endpoint: API endpoint path
        method: HTTP method
        status_code: Response status code
        response_time_ms: Response time in milliseconds
        created_at: Request timestamp
        date: Request date (for daily aggregation)
    """

    __tablename__ = "usage_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    context_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("contexts.id", ondelete="SET NULL"), nullable=True
    )
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )  # Deprecated, kept for backward compatibility

    __table_args__ = (
        Index("idx_usage_stats_user_date", "user_id", "date"),
        Index("idx_usage_stats_user_endpoint", "user_id", "endpoint", "date"),
        Index("idx_usage_stats_endpoint", "endpoint", "date"),
        Index("idx_usage_stats_context_date", "context_id", "date"),
        Index("idx_usage_stats_workspace_date", "workspace_id", "date"),
        Index("idx_usage_stats_project_date", "project_id", "date"),
    )


class UserPlan(Base):
    """User subscription plan and quota limits.

    Manages user plan details for billing and quota enforcement.
    Issue #48 - Usage Statistics - Plan Limits & Usage Tracking

    Attributes:
        user_id: OAuth2 user ID (primary key)
        plan_name: Plan type (free/pro/enterprise)
        memory_limit: Maximum memories allowed
        daily_api_limit: Daily API call limit
        weekly_api_limit: Weekly API call limit
        created_at: Plan creation timestamp
        updated_at: Last update timestamp
    """

    __tablename__ = "user_plans"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    plan_name: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    memory_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    daily_api_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    weekly_api_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=5000)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("idx_user_plans_plan_name", "plan_name"),)

    @classmethod
    def default_for_user(cls, user_id: str, settings) -> "UserPlan":
        """Construct the default 'free' plan from settings defaults.

        Shared between two callers so a future column add can't drift
        them apart (Issue #586):
        - ``auth.roles.RoleManager._ensure_user_postgres`` writes this
          row on user creation.
        - ``api.routes.usage.get_current_usage`` builds an in-memory
          instance on the fallback path (no persistence).
        """
        return cls(
            user_id=user_id,
            plan_name="free",
            memory_limit=settings.default_plan_memory_limit,
            daily_api_limit=settings.default_plan_daily_api_limit,
            weekly_api_limit=settings.default_plan_weekly_api_limit,
        )


# ============================================================================
# Context-based Multi-Collection (Issue #82 → #160: renamed from Project)
# ============================================================================


class Context(Base):
    """Context model for multi-collection memory workspace.

    Allows workspaces to organize memories into separate namespaces (work, personal,
    context-specific, etc.). Each context maps to a separate Qdrant collection and
    can be shared among workspace members.

    Issue #82: Context-based Multi-Collection Support
    Issue #115 Phase B: Workspace-level Multi-tenancy
    Issue #160: Renamed from Project to Context
    Issue #165: Added privacy control (private vs shared contexts)

    Attributes:
        id: Primary key (UUID)
        workspace_id: Workspace ID (foreign key) - owner of this context
        name: Context name (lowercase alphanumeric + hyphen/underscore only, used for collection naming)
        display_name: Human-readable display name (can contain spaces, mixed case, special characters)
        description: Optional context description
        summary: LLM-oriented context summary (200-500 chars)
        usage_guide: LLM-oriented memory usage guidelines
        created_by: User ID who created this context
        is_private: Privacy flag (TRUE = creator only, FALSE = shared with workspace)
        created_at: Creation timestamp
        updated_at: Last modification timestamp
        deleted_at: Soft delete timestamp
        deleted_by: User ID who deleted this context
        is_locked: Lock flag (prevents deletion when TRUE)

    Privacy (Issue #165):
        - is_private = TRUE: Only creator can access (default)
        - is_private = FALSE: Workspace members can access (Pro plan required)

    Collection Naming:
        Each context maps to a Qdrant collection named:
        kagura_workspace_{workspace_id}_context_{name}

    Constraints:
        - Context name must match pattern: ^[a-z0-9_-]+$
        - (workspace_id, name) is unique
        - "default" is reserved for auto-created context
    """

    __tablename__ = "contexts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )  # Human-readable display name
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Issue #160: LLM-oriented fields for get_context_info
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # 200-500 chars context overview
    usage_guide: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Memory usage guidelines for AI

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Issue #169: Last used timestamp for recent usage sorting in MCP
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )

    # Issue #165: Privacy control (private vs shared contexts)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    # Issue #238: Public REST API access flag
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Issue #238: Resource linkage for public contexts
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Issue #85: Context lock to prevent accidental deletion
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Issue #101: Sleep Maintenance mode
    # 'full' = all phases (personal AI memory)
    # 'edges_only' = Edge Discovery + Reindex only (resource ingest contexts)
    # 'skip' = no sleep maintenance (large-scale / externally managed; default)
    sleep_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="skip")

    # Constraints
    __table_args__ = (
        # Note: Index idx_contexts_workspace_name is created in migration with WHERE deleted_at IS NULL
        CheckConstraint("name ~ '^[a-z0-9_-]+$'", name="valid_context_name"),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<Context(name='{self.name}', workspace_id='{self.workspace_id}')>"

    @property
    def is_default(self) -> bool:
        """Check if this is the default context.

        Returns:
            True if context name is "default"
        """
        return self.name == "default"


def _zero_floor(base: int, addon: int | None) -> int:
    """Defense-in-depth quota helper (#569).

    A plan-tier ``base`` of 0 means the feature is excluded from the tier —
    no addon row can raise the effective limit above 0. Encoded in the data
    (``base == 0``) rather than by plan name so a future paid tier with
    ``base > 0`` automatically gets addon stacking (mirrors the rule from
    PR #568 / #560 for ``sleep_enabled_contexts_limit``).

    Callers that scale the addon to a different unit before stacking
    (e.g. ``effective_storage_limit_bytes`` converts MB→bytes) must pass
    the **scaled** value as ``addon`` so the zero-floor check happens on
    the final unit.
    """
    if base == 0:
        return 0
    return base + (addon or 0)


class Workspace(Base):
    """Workspace model for multi-user team management.

    Workspaces are the billing and grouping layer for users. Users can belong
    to multiple workspaces, and each workspace can have multiple contexts
    that are shared among members.

    Issue #115 Phase B: Workspace-level Multi-tenancy

    Attributes:
        id: Primary key (UUID)
        name: Workspace display name
        description: Optional description
        owner_user_id: Owner user ID (creator)
        plan_name: Billing plan (free/pro/enterprise)
        memory_limit: Max memories per workspace
        daily_api_limit: Daily API call limit
        weekly_api_limit: Weekly API call limit
        created_at: Creation timestamp
        updated_at: Last modification timestamp
        deleted_at: Soft delete timestamp

    Relationships:
        members: Workspace members (many-to-many through workspace_members)
        contexts: Contexts owned by this workspace (one-to-many)

    Constraints:
        - plan_name must be in: free, pro, enterprise
    """

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # Issue #276: slug removed - not used for routing, caused UX issues
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Billing & Plan
    plan_name: Mapped[str] = mapped_column(String(50), nullable=False, server_default="free")
    memory_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1000")
    daily_api_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1000")
    weekly_api_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5000")

    # Issue #238: Addon bonus columns (Migration 048)
    addon_memory_bonus: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    addon_mcp_quota_bonus: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    addon_rest_quota_bonus: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    addon_public_quota_bonus: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    addon_member_bonus: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    addon_context_bonus: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )  # Issue #15
    addon_analysis_bonus: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )  # Issue #494
    addon_storage_bonus_mb: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )  # Issue #485
    addon_sleep_contexts_bonus: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )  # Issue #560: Sleep-enabled contexts addon (PRO-only)

    # Issue #494: per-workspace default + quality model selection for
    # Memory Broadlistening analyses. Both nullable — analysis is gated
    # by a separate allowlist; until a workspace is opted-in there's no
    # need for a model choice. SET NULL on llm_pricing delete so a
    # pricing row removal does not cascade into the workspace.
    analysis_default_model_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("llm_pricing.id", ondelete="SET NULL"),
        nullable=True,
    )
    analysis_quality_model_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("llm_pricing.id", ondelete="SET NULL"),
        nullable=True,
    )

    @cached_property
    def _plan_tier(self):
        """Cached plan tier lookup (lazy import to avoid circular dependency)."""
        from config.plan_tiers import get_plan_tier

        return get_plan_tier(self.plan_name)

    @property
    def effective_memory_limit(self) -> int:
        """Memory limit including addon bonus."""
        return _zero_floor(self._plan_tier.memory_limit, self.addon_memory_bonus)

    @property
    def effective_mcp_calls_per_day(self) -> int:
        """MCP API calls/day: plan tier base + addon (Issue #238)."""
        return _zero_floor(self._plan_tier.mcp_calls_per_day, self.addon_mcp_quota_bonus)

    @property
    def effective_mcp_calls_per_week(self) -> int:
        """Weekly MCP API call limit: plan tier base + addon (Issue #238)."""
        return _zero_floor(self._plan_tier.mcp_calls_per_week, self.addon_mcp_quota_bonus)

    @property
    def effective_rest_calls_per_day(self) -> int:
        """REST API calls/day: plan tier base + addon (Issue #238).

        FREE has ``rest_calls_per_day == 0`` — the zero-base guard in
        ``_zero_floor`` ensures a manual addon row cannot grant REST access
        to a tier that excludes it (#569).
        """
        return _zero_floor(self._plan_tier.rest_calls_per_day, self.addon_rest_quota_bonus)

    @property
    def effective_rest_calls_per_week(self) -> int:
        """Weekly REST API call limit: plan tier base + addon (Issue #238)."""
        return _zero_floor(self._plan_tier.rest_calls_per_week, self.addon_rest_quota_bonus)

    @property
    def effective_public_calls_per_day(self) -> int:
        """Public REST API calls/day: plan tier base + addon (Issue #238).

        FREE and BASIC have ``public_calls_per_day == 0``. There is **no other
        runtime tier gate** on the public REST routes — ``api/routes/public_search.py``
        rejects non-public contexts via ``context.is_public``, but does not check
        the owner's plan tier. The ``public_contexts`` entry in
        ``plan_tiers.py:FEATURE_MIN_PLANS`` is declared but never consumed at
        runtime (``check_feature_access`` is only called for ``memory_analysis``
        and ``reranking`` today), so the zero-base guard here is the **primary**
        protection against a stray ``WorkspaceAddon`` row granting public-API
        access to a tier that excludes it (#569).
        """
        return _zero_floor(self._plan_tier.public_calls_per_day, self.addon_public_quota_bonus)

    @property
    def effective_public_calls_per_week(self) -> int:
        """Weekly Public REST API call limit: plan tier base + addon (Issue #238)."""
        return _zero_floor(self._plan_tier.public_calls_per_week, self.addon_public_quota_bonus)

    @property
    def effective_max_contexts(self) -> int:
        """Max contexts: plan tier base + addon."""
        return _zero_floor(self._plan_tier.max_contexts_per_workspace, self.addon_context_bonus)

    @property
    def effective_max_members(self) -> int:
        """Max members: plan tier base + addon (Issue #229)."""
        return _zero_floor(self._plan_tier.max_members_per_workspace, self.addon_member_bonus)

    @property
    def effective_analysis_runs_per_day(self) -> int:
        """Memory Broadlistening analysis runs/day: plan tier base + addon (Issue #494).

        FREE and BASIC have ``analysis_runs_per_day == 0``. Access is also
        gated by ``auth.analysis_gates.require_pro_tier`` which checks the
        ``memory_analysis`` feature flag (``plan_tiers.py`` ``FEATURE_MIN_PLANS``);
        the zero-base guard here is defense-in-depth in case that feature
        gate is ever bypassed (#569).
        """
        return _zero_floor(self._plan_tier.analysis_runs_per_day, self.addon_analysis_bonus)

    @property
    def effective_storage_limit_bytes(self) -> int:
        """File-storage hard cap (bytes): plan tier base + addon (Issue #485).

        ``addon_storage_bonus_mb`` stores the bonus in MB to align with the
        ``ADDON_UNIT_VALUES["extra_storage"]`` unit; the conversion to bytes
        happens here so callers always see a single unit at the boundary.

        Defense-in-depth: addon bytes are computed first, then passed through
        ``_zero_floor`` (#569). If a future tier sets ``storage_limit_bytes == 0``
        — and ``api/routes/files.py:reserve_upload`` does not currently have a
        tier-feature gate ahead of the limit check — the zero-floor here is
        what stops a stray ``WorkspaceAddon`` row from granting storage access.
        """
        addon_bytes = (self.addon_storage_bonus_mb or 0) * 1024 * 1024
        return _zero_floor(self._plan_tier.storage_limit_bytes, addon_bytes)

    @property
    def effective_sleep_enabled_contexts_limit(self) -> int:
        """Sleep-enabled contexts cap: plan tier base + addon (Issue #560).

        FREE/BASIC are ``0 + 0 = 0`` — sleep_mode cannot be set to anything
        other than ``skip`` for these tiers. PRO is ``3 + N`` where N comes
        from the ``extra_sleep_contexts`` addon, sold per-unit and accumulated
        into ``addon_sleep_contexts_bonus`` by ``AddonCalculatorService``.

        Defense-in-depth: when the plan tier base is ``0`` (FREE/BASIC), we
        return ``0`` unconditionally — addon rows on a zero-base tier would
        otherwise let a misconfigured Stripe SKU or a manual ``WorkspaceAddon``
        INSERT bypass the tier gate. The rule "zero-base tiers do not stack
        addons" is encoded in the data (``sleep_enabled_contexts_limit == 0``)
        rather than by hard-coding plan names so a future paid tier with
        ``sleep_enabled_contexts_limit > 0`` automatically gets addon stacking.

        Now routed through ``_zero_floor`` to share the rule with every other
        ``effective_*_limit`` property (#569).
        """
        return _zero_floor(
            self._plan_tier.sleep_enabled_contexts_limit,
            self.addon_sleep_contexts_bonus,
        )

    # Stripe billing (Issue #351)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Constraints
    __table_args__ = (
        CheckConstraint("plan_name IN ('free', 'basic', 'pro')", name="valid_plan_name"),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<Workspace(id='{self.id}', name='{self.name}')>"


class WorkspaceMember(Base):
    """Workspace membership model for user-workspace relationships.

    Manages which users belong to which workspaces and their roles within
    the workspace. Users can belong to multiple workspaces.

    Issue #115 Phase B: Workspace-level Multi-tenancy
    Issue #234: Context access restriction via allowed_context_ids

    Attributes:
        id: Primary key
        workspace_id: Workspace ID (foreign key)
        user_id: User ID (OAuth2 sub claim)
        role: User's role in workspace (owner/admin/member/viewer)
        allowed_context_ids: Whitelist of accessible context IDs (member/viewer only)
        invited_by: User ID who invited this member
        invited_at: Invitation timestamp
        joined_at: When user accepted invitation
        created_at: Record creation timestamp
        updated_at: Last modification timestamp

    Roles:
        - owner: Full access, billing management, can delete workspace
        - admin: Manage members and contexts, bypass context permissions
        - member: Access assigned contexts only
        - viewer: Read-only access to all contexts

    Context Access (allowed_context_ids):
        - NULL: No restriction (current behavior, full access per role)
        - []: No context access
        - [uuid, ...]: Only these contexts accessible
        - Note: owner/admin roles always have full access (field is ignored)

    Constraints:
        - (workspace_id, user_id) must be unique
        - role must be in: owner, admin, member, viewer
    """

    __tablename__ = "workspace_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default="member")

    # Context access restriction (Issue #234)
    # NULL = no restriction, [] = no access, [uuid, ...] = whitelist
    # Only applies to member/viewer roles (owner/admin always have full access)
    allowed_context_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True, default=None
    )

    # Invitation tracking
    invited_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )

    # Constraints
    __table_args__ = (
        Index("idx_workspace_members_org", "workspace_id"),
        Index("idx_workspace_members_user", "user_id"),
        Index("idx_workspace_members_role", "workspace_id", "role"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')", name="valid_workspace_member_role"
        ),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<WorkspaceMember(workspace_id='{self.workspace_id}', user='{self.user_id}', role='{self.role}')>"


class WorkspaceInvitation(Base):
    """Workspace invitation model for team onboarding.

    Issue #165: Team Collaboration Features - Workspace Invitation System

    Provides token-based invitation system with secure URLs, optional email
    restrictions, and configurable expiration. Allows workspace owners/admins
    to invite new members via unique invitation links.

    Attributes:
        id: Primary key
        workspace_id: Workspace ID (CASCADE delete on workspace deletion)
        token: Unique invitation token (URL-safe random string, min 20 chars)
        email: Optional email restriction (only this email can accept)
        role: Role to assign upon acceptance (owner/admin/member/viewer)
        invited_by: User ID who created the invitation
        expires_at: Expiration timestamp (NULL = never expires)
        accepted_at: Acceptance timestamp (NULL = pending)
        accepted_by: User ID who accepted
        created_at: Creation timestamp
        updated_at: Last modification timestamp

    Security:
        - Token: 32-byte URL-safe random string (cryptographically secure)
        - Email: Case-insensitive matching on acceptance
        - Expiration: Configurable (7/30/90/365 days or never)
        - Single-use: Cannot be reused after acceptance

    Invitation Flow:
        1. Owner/Admin creates invitation → token generated
        2. Invitation URL shared: https://your-domain.com/invite/{token}
        3. User clicks link → validates token, email, expiration
        4. User accepts → WorkspaceMember created, invitation marked accepted
    """

    __tablename__ = "workspace_invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default="member")
    invited_by: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Migration 042: Context access selection on invitation
    allowed_context_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )

    # Constraints
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')", name="valid_invitation_role"
        ),
        CheckConstraint("length(token) >= 20", name="valid_invitation_token"),
    )

    def is_expired(self) -> bool:
        """Check if invitation is expired.

        Returns:
            True if invitation has expired, False otherwise.
            Never-expiring invitations (expires_at=NULL) return False.
        """
        if self.expires_at is None:
            return False

        return utcnow() > self.expires_at

    def is_accepted(self) -> bool:
        """Check if invitation has been accepted.

        Returns:
            True if invitation has been accepted (accepted_at is set), False otherwise.
        """
        return self.accepted_at is not None

    def __repr__(self) -> str:
        """String representation."""
        status = (
            "accepted" if self.is_accepted() else ("expired" if self.is_expired() else "pending")
        )
        return f"<WorkspaceInvitation(id={self.id}, workspace='{self.workspace_id}', status='{status}')>"


class ContextMember(Base):
    """Context membership model for user-context access control.

    Manages which users have access to which contexts and their permissions.
    Workspace owners and admins automatically have access to all contexts
    (bypassing this table).

    Issue #115 Phase B: Workspace-level Multi-tenancy
    Issue #160: Renamed from ProjectMember to ContextMember

    Attributes:
        id: Primary key
        context_id: Context ID (foreign key)
        user_id: User ID (OAuth2 sub claim)
        role: User's role in context (owner/editor/viewer)
        invited_by: User ID who added this member
        invited_at: When member was added
        created_at: Record creation timestamp
        updated_at: Last modification timestamp

    Roles:
        - owner: Full context access, can manage settings and delete
        - editor: Read/write memories
        - viewer: Read-only access

    Constraints:
        - (context_id, user_id) must be unique
        - role must be in: owner, editor, viewer
    """

    __tablename__ = "context_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    context_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default="editor")

    # Invitation tracking
    invited_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )

    # Constraints
    __table_args__ = (
        Index("idx_context_members_context", "context_id"),
        Index("idx_context_members_user", "user_id"),
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="valid_context_member_role"),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<ContextMember(context_id='{self.context_id}', user='{self.user_id}', role='{self.role}')>"


# ============================================================================
# Plan Change Audit Model (Issue #149)
# ============================================================================


class PlanChange(Base):
    """Plan change audit log.

    Issue #149: Track workspace plan tier changes for audit purposes.

    Attributes:
        id: Primary key
        workspace_id: Workspace that was changed
        old_plan: Previous plan tier name
        new_plan: New plan tier name
        changed_by: User ID (admin) who made the change
        changed_at: Timestamp of the change
        reason: Optional reason for the change
        old_*: Previous quota limits (for audit)
        new_*: New quota limits (for audit)
    """

    __tablename__ = "plan_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    old_plan: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_plan: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_by: Mapped[str] = mapped_column(String(255), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Old limits (for audit)
    old_memory_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_daily_api_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_weekly_api_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # New limits (for audit)
    new_memory_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_daily_api_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_weekly_api_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("idx_plan_changes_workspace", "workspace_id"),
        Index("idx_plan_changes_date", "changed_at", postgresql_ops={"changed_at": "DESC"}),
        Index("idx_plan_changes_changed_by", "changed_by"),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<PlanChange(workspace_id='{self.workspace_id}', {self.old_plan} -> {self.new_plan}, by='{self.changed_by}')>"


# ============================================================================
# MCP Tool Descriptions (Issue #160 - i18n support)
# ============================================================================


class MCPToolDescription(Base):
    """MCP Tool description model with i18n support.

    Stores localized descriptions for MCP tools (remember, recall, forget,
    reference, explore, get_context_info). Admins can edit descriptions
    via the Web UI.

    Issue #160: MCP Tool i18n Support

    Attributes:
        id: Primary key
        tool_name: Tool name (remember, recall, etc.)
        locale: Locale code (en, ja, etc.)
        description: Tool description in the specified locale
        created_at: Creation timestamp
        updated_at: Last modification timestamp

    Constraints:
        - (tool_name, locale) must be unique
    """

    __tablename__ = "mcp_tool_descriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    locale: Mapped[str] = mapped_column(String(10), nullable=False, server_default="en")
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, onupdate=func.now()
    )

    # Constraints
    __table_args__ = (
        Index("idx_mcp_tool_descriptions_tool_locale", "tool_name", "locale", unique=True),
    )

    def __repr__(self) -> str:
        """String representation."""
        return f"<MCPToolDescription(tool='{self.tool_name}', locale='{self.locale}')>"
