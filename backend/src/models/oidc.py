"""First-party OIDC grants, isolated from the memory/MCP OAuth credentials.

Only credential verifiers are stored. Spent codes and refresh-token hashes stay
attached to their original grant so reuse can revoke the whole token family.
Relationships are consumed by the synchronous Authlib adapter; async routes
must explicitly load them before calling these protocol helpers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import bcrypt
from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base
from utils.datetime import utcnow

OIDC_SCOPES = frozenset({"openid", "profile", "workspaces:read", "integrations:manage"})


def _epoch_now() -> int:
    """Return UTC epoch seconds without interpreting naive UTC as local time."""
    return int(utcnow().replace(tzinfo=UTC).timestamp())


class OIDCClient(Base):
    """Pre-registered confidential web client; no MCP permission fallback."""

    __tablename__ = "oidc_clients"

    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    client_secret_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(255), nullable=False, default="openid profile")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    grants: Mapped[list[OIDCGrant]] = relationship(
        "OIDCGrant", back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )

    def get_client_id(self) -> str:
        """Return the registered client identifier."""
        return self.client_id

    def get_default_redirect_uri(self) -> str | None:
        """Return the first exact callback for a live client."""
        if self.revoked_at is not None or not self.redirect_uris:
            return None
        return self.redirect_uris[0]

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        """Accept only an exact registered callback, with no wildcard logic."""
        return self.revoked_at is None and redirect_uri in self.redirect_uris

    def has_client_secret(self) -> bool:
        """Return whether this live client has a confidential verifier."""
        return self.revoked_at is None and bool(self.client_secret_hash)

    def check_client_secret(self, secret: str) -> bool:
        """Verify bcrypt without legacy password truncation or text exceptions."""
        if not self.has_client_secret():
            return False
        try:
            encoded = secret.encode("utf-8")
            if len(encoded) > 72:
                return False
            return bcrypt.checkpw(encoded, self.client_secret_hash.encode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            return False

    def check_token_endpoint_auth_method(self, method: str) -> bool:
        """Allow confidential-client POST or HTTP Basic authentication."""
        return self.revoked_at is None and method in {"client_secret_post", "client_secret_basic"}

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        """Expose the Authlib endpoint authentication interface."""
        return endpoint == "token" and self.check_token_endpoint_auth_method(method)

    def check_response_type(self, response_type: str) -> bool:
        """Restrict browser authorization to the code response type."""
        return self.revoked_at is None and response_type == "code"

    def check_grant_type(self, grant_type: str) -> bool:
        """Allow code exchange and rotation within the original login grant."""
        return self.revoked_at is None and grant_type in {"authorization_code", "refresh_token"}

    def get_allowed_scope(self, scope: str | None) -> str | None:
        """Intersect requested, registered and supported OIDC-only scopes."""
        if self.revoked_at is not None:
            return None
        registered = set(self.scope.split()) & OIDC_SCOPES
        requested = self.scope if scope is None else scope
        allowed = dict.fromkeys(part for part in requested.split() if part in registered)
        return " ".join(allowed) or None


class OIDCGrant(Base):
    """Original login authority and the fixed lifetime of one token family."""

    __tablename__ = "oidc_grants"
    __table_args__ = (
        UniqueConstraint("client_id", "nonce", name="uq_oidc_grants_client_nonce"),
        CheckConstraint("auth_time >= 0", name="ck_oidc_grants_auth_time"),
        CheckConstraint("expires_at > created_at", name="ck_oidc_grants_lifetime"),
        Index("ix_oidc_grants_user_id", "user_id"),
        Index("ix_oidc_grants_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("oidc_clients.client_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    nonce: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_time: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    client: Mapped[OIDCClient] = relationship("OIDCClient", back_populates="grants")
    authorization_codes: Mapped[list[OIDCAuthorizationCode]] = relationship(
        "OIDCAuthorizationCode",
        back_populates="grant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tokens: Mapped[list[OIDCToken]] = relationship(
        "OIDCToken", back_populates="grant", cascade="all, delete-orphan", passive_deletes=True
    )

    def is_active(self) -> bool:
        """Require both original grant and client authority to remain live."""
        return (
            self.revoked_at is None
            and self.expires_at > utcnow()
            and self.client.revoked_at is None
        )


class OIDCAuthorizationCode(Base):
    """One-use code verifier retained for nonce and code-replay detection."""

    __tablename__ = "oidc_authorization_codes"
    __table_args__ = (
        CheckConstraint("length(code_hash) = 64", name="ck_oidc_codes_hash_length"),
        CheckConstraint("code_challenge_method = 'S256'", name="ck_oidc_codes_pkce_s256"),
        Index("ix_oidc_authorization_codes_grant_id", "grant_id"),
        Index("ix_oidc_authorization_codes_expires_at", "expires_at"),
    )

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("oidc_grants.id", ondelete="CASCADE"), nullable=False
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(10), nullable=False, default="S256")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    grant: Mapped[OIDCGrant] = relationship("OIDCGrant", back_populates="authorization_codes")

    def get_client_id(self) -> str:
        """Return the original grant's client identifier."""
        return self.grant.client_id

    def get_user_id(self) -> str:
        """Return the stable KMC subject bound to the grant."""
        return self.grant.user_id

    def get_redirect_uri(self) -> str:
        """Return the exact callback bound at authorization."""
        return self.redirect_uri

    def get_scope(self) -> str:
        """Return the original grant's OIDC scope."""
        return self.grant.scope

    def get_nonce(self) -> str:
        """Return the nonce bound to this login grant."""
        return self.grant.nonce

    def get_auth_time(self) -> int:
        """Return the end-user authentication time in UTC epoch seconds."""
        return self.grant.auth_time

    def get_acr(self) -> None:
        """Do not assert an unverified authentication assurance class."""
        return None

    def get_amr(self) -> None:
        """Do not invent authentication methods for the browser session."""
        return None

    def get_code_challenge(self) -> str:
        """Return the mandatory PKCE challenge."""
        return self.code_challenge

    def get_code_challenge_method(self) -> str:
        """Return the bound S256 method."""
        return self.code_challenge_method

    def is_expired(self) -> bool:
        """Refuse spent, expired or no-longer-authorized codes."""
        return (
            self.consumed_at is not None
            or self.expires_at <= utcnow()
            or not self.grant.is_active()
        )


class OIDCToken(Base):
    """Hashed token pair, keeping spent refresh verifiers for family revocation."""

    __tablename__ = "oidc_tokens"
    __table_args__ = (
        UniqueConstraint("access_token_hash", name="oidc_tokens_access_token_hash_key"),
        UniqueConstraint("refresh_token_hash", name="oidc_tokens_refresh_token_hash_key"),
        CheckConstraint("length(access_token_hash) = 64", name="ck_oidc_tokens_access_hash_length"),
        CheckConstraint(
            "length(refresh_token_hash) = 64", name="ck_oidc_tokens_refresh_hash_length"
        ),
        CheckConstraint("issued_at >= 0", name="ck_oidc_tokens_issued_at"),
        CheckConstraint("expires_in > 0", name="ck_oidc_tokens_expires_in"),
        Index("ix_oidc_tokens_grant_id", "grant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("oidc_grants.id", ondelete="CASCADE"), nullable=False
    )
    access_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(512), nullable=False)
    issued_at: Mapped[int] = mapped_column(Integer, nullable=False, default=_epoch_now)
    expires_in: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    refresh_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    grant: Mapped[OIDCGrant] = relationship("OIDCGrant", back_populates="tokens")

    def get_client_id(self) -> str:
        """Return the original client, including after refresh rotation."""
        return self.grant.client_id

    def get_user_id(self) -> str:
        """Return the stable KMC subject for this token family."""
        return self.grant.user_id

    def get_scope(self) -> str:
        """Return this issuance's scope, including any refresh-time narrowing."""
        return self.scope

    def get_expires_in(self) -> int:
        """Return the short access-token lifetime in seconds."""
        return self.expires_in

    def get_expires_at(self) -> int:
        """Return the access-token expiry in UTC epoch seconds."""
        return self.issued_at + self.expires_in

    def is_expired(self) -> bool:
        """Access expiry cannot outlive the original grant."""
        return self.get_expires_at() <= _epoch_now() or self.grant.expires_at <= utcnow()

    def is_revoked(self) -> bool:
        """Client and grant revocation apply to every issued token pair."""
        return (
            self.revoked_at is not None
            or self.grant.revoked_at is not None
            or self.grant.client.revoked_at is not None
        )

    def is_refresh_token_active(self) -> bool:
        """Allow one refresh while the original, non-extended grant remains live."""
        return (
            bool(self.refresh_token_hash)
            and self.refresh_used_at is None
            and self.revoked_at is None
            and self.grant.is_active()
        )

    def check_client(self, client: OIDCClient) -> bool:
        """Bind refresh-token use to the original confidential client."""
        return self.grant.client_id == client.client_id and client.revoked_at is None
