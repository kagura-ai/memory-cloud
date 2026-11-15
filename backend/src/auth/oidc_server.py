"""Isolated Authlib OIDC grants; the caller owns the transaction (#2781)."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

from authlib.oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import OAuth2Request, grants
from authlib.oauth2.rfc6749.errors import InvalidGrantError, InvalidRequestError, InvalidScopeError
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oidc.core import UserInfo
from authlib.oidc.core.grants import OpenIDCode
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from auth.oidc_config import OIDCConfig
from auth.oidc_logging import install_oidc_log_redaction
from auth.starlette_oauth2_request import StarletteOAuth2Request
from models.auth import User
from models.oidc import OIDCAuthorizationCode, OIDCClient, OIDCGrant, OIDCToken
from utils.datetime import utcnow

IDENTITY_SCOPES = frozenset(
    {"openid", "profile", "workspaces:read", "integrations:manage", "sync.records:read"}
)
TOKEN_AUTH_METHODS = ["client_secret_basic", "client_secret_post"]


def credential_hash(value: str) -> str:
    """Hash a high-entropy wire credential without retaining its plaintext."""
    return hashlib.sha256(value.encode()).hexdigest()


def profile(user: User, scope: str) -> dict[str, Any]:
    """Return stable identity and only the profile claims the client requested."""
    result: dict[str, Any] = {"sub": user.user_id}
    if "profile" in scope.split():
        if user.name:
            result["name"] = user.name
        if user.picture:
            result["picture"] = user.picture
    return result


class IdentityCode(OpenIDCode):
    """Authlib's OIDC code-flow extension using a separate signing key ring."""

    def __init__(self, server: IdentityServer):
        super().__init__(require_nonce=True)
        self.identity_server = server

    def resolve_client_private_key(self, client: Any) -> Any:
        return self.identity_server.config.private_key

    def get_client_algorithm(self, client: Any) -> str:
        return "RS256"

    def get_client_claims(self, client: Any) -> dict[str, Any]:
        now = int(time.time())
        return {
            "iss": self.identity_server.config.issuer,
            "aud": [client.client_id],
            "iat": now,
            "exp": now + 300,
        }

    def get_encode_header(self, client: Any) -> dict[str, str]:
        return {"alg": "RS256", "kid": self.identity_server.config.active_kid}

    def generate_user_info(self, user: User, scope: str) -> UserInfo:
        return UserInfo(**profile(user, scope))

    def exists_nonce(self, nonce: str, request: OAuth2Request) -> bool:
        return (
            self.identity_server.db.scalar(
                select(OIDCGrant.id)
                .where(
                    OIDCGrant.client_id == request.client.get_client_id(), OIDCGrant.nonce == nonce
                )
                .limit(1)
            )
            is not None
        )


class IdentityAuthorizationCodeGrant(grants.AuthorizationCodeGrant):
    """One-use, client-bound codes with mandatory S256 for every client."""

    TOKEN_ENDPOINT_AUTH_METHODS = TOKEN_AUTH_METHODS

    def validate_authorization_request(self) -> str:
        data = self.request.payload.data
        scope = set((data.get("scope") or "").split())
        if "openid" not in scope or not scope <= IDENTITY_SCOPES:
            raise InvalidScopeError()
        challenge = data.get("code_challenge", "")
        if data.get("code_challenge_method") != "S256" or not re.fullmatch(
            r"[A-Za-z0-9_-]{43}", challenge
        ):
            raise InvalidRequestError("S256 PKCE is required.")
        if not data.get("nonce") or len(data["nonce"]) > 255:
            raise InvalidRequestError("A bounded nonce is required.")
        redirect = super().validate_authorization_request()
        if not scope <= set(cast(OIDCClient, self.request.client).scope.split()):
            raise InvalidScopeError()
        return redirect

    def generate_authorization_code(self) -> str:
        return "kmc_oidc_code_" + secrets.token_urlsafe(32)

    def save_authorization_code(self, code: str, request: OAuth2Request) -> None:
        server: IdentityServer = self.server
        user = server.lock_user(request.user.user_id)
        if user is None or not server.session_live():
            raise InvalidGrantError()
        now = utcnow()
        grant = OIDCGrant(
            client_id=request.client.get_client_id(),
            user_id=user.user_id,
            scope=request.payload.scope,
            nonce=request.payload.data["nonce"],
            auth_time=server.auth_time,
            expires_at=now + timedelta(hours=8),
        )
        server.db.add(grant)
        server.db.flush()
        server.db.add(
            OIDCAuthorizationCode(
                code_hash=credential_hash(code),
                grant_id=grant.id,
                redirect_uri=request.payload.redirect_uri,
                code_challenge=request.payload.data["code_challenge"],
                code_challenge_method="S256",
                expires_at=now + timedelta(seconds=60),
            )
        )
        server.db.flush()

    def query_authorization_code(self, code: str, client: Any) -> Any:
        server: IdentityServer = self.server
        digest = credential_hash(code)
        grant_id = server.db.scalar(
            select(OIDCAuthorizationCode.grant_id).where(OIDCAuthorizationCode.code_hash == digest)
        )
        grant = server.lock_grant(grant_id, client.client_id)
        if grant is None:
            return None
        row = server.db.scalar(
            select(OIDCAuthorizationCode)
            .where(OIDCAuthorizationCode.code_hash == digest)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.consumed_at is not None or row.expires_at <= utcnow():
            return None
        server.active_grant = grant
        return row

    def delete_authorization_code(self, authorization_code: Any) -> None:
        authorization_code.consumed_at = utcnow()
        self.server.db.flush()

    def authenticate_user(self, authorization_code: Any) -> User | None:
        return self.server.db.scalar(
            select(User).where(User.user_id == self.server.active_grant.user_id)
        )


class IdentityRefreshGrant(grants.RefreshTokenGrant):
    """Rotate the credential while keeping the initial absolute deadline."""

    TOKEN_ENDPOINT_AUTH_METHODS = TOKEN_AUTH_METHODS
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, refresh_token: str) -> Any:
        server: IdentityServer = self.server
        digest = credential_hash(refresh_token)
        grant_id = server.db.scalar(
            select(OIDCToken.grant_id).where(OIDCToken.refresh_token_hash == digest)
        )
        grant = server.lock_grant(grant_id, self.request.client.get_client_id())
        if grant is None:
            return None
        token = server.db.scalar(
            select(OIDCToken)
            .where(OIDCToken.refresh_token_hash == digest)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if token is None or token.revoked_at is not None:
            return None
        if token.refresh_used_at is not None:
            grant.revoked_at = utcnow()
            server.persist_revocation = True
            server.db.flush()
            return None
        server.active_grant = grant
        return token

    def authenticate_user(self, refresh_token: Any) -> User | None:
        return self.server.db.scalar(
            select(User).where(User.user_id == self.server.active_grant.user_id)
        )

    def revoke_old_credential(self, refresh_token: Any) -> None:
        refresh_token.refresh_used_at = utcnow()
        self.server.db.flush()

    def _validate_token_scope(self, token: Any) -> None:
        scope = self.request.payload.scope
        if scope and (
            "openid" not in scope.split()
            or not set(scope.split()) <= set(token.get_scope().split())
        ):
            raise InvalidScopeError()


class IdentityServer(AuthorizationServer):
    """Per-request server: no shared request state and no internal commits."""

    def __init__(self, db: Session, config: OIDCConfig):
        install_oidc_log_redaction()
        super().__init__()
        self.db = db
        self.config = config
        self.active_grant: OIDCGrant | None = None
        self.persist_revocation = False
        self.auth_time: int = 0
        self.session_live: Callable[[], bool] = lambda: False
        self.register_token_generator("default", self.generate_identity_token)
        self.register_grant(
            IdentityAuthorizationCodeGrant, [CodeChallenge(required=True), IdentityCode(self)]
        )
        self.register_grant(IdentityRefreshGrant)

    def query_client(self, client_id: str) -> Any:
        return self.db.scalar(
            select(OIDCClient)
            .where(OIDCClient.client_id == client_id, OIDCClient.revoked_at.is_(None))
            .with_for_update(read=True)
        )

    def lock_user(self, user_id: str) -> User | None:
        return self.db.scalar(
            select(User).where(User.user_id == user_id).with_for_update(read=True, key_share=True)
        )

    def lock_grant(
        self, grant_id: Any, client_id: str, *, allow_expired: bool = False
    ) -> OIDCGrant | None:
        if grant_id is None:
            return None
        user_id = self.db.scalar(
            select(OIDCGrant.user_id).where(
                OIDCGrant.id == grant_id, OIDCGrant.client_id == client_id
            )
        )
        if user_id is None or self.lock_user(user_id) is None:
            return None
        grant = self.db.scalar(
            select(OIDCGrant)
            .where(OIDCGrant.id == grant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if grant is None or grant.revoked_at is not None:
            return None
        if not allow_expired and grant.expires_at <= utcnow():
            return None
        return grant

    def generate_identity_token(
        self,
        grant_type: str,
        client: Any,
        user: Any = None,
        scope: str | None = None,
        expires_in: int | None = None,
        include_refresh_token: bool = True,
    ) -> dict[str, Any]:
        if self.active_grant is None:
            raise InvalidGrantError()
        remaining = int((self.active_grant.expires_at - utcnow()).total_seconds())
        if remaining < 1:
            raise InvalidGrantError()
        token: dict[str, Any] = {
            "token_type": "Bearer",
            "access_token": "kmc_oidc_at_" + secrets.token_urlsafe(32),
            "expires_in": min(300, remaining),
            "scope": scope,
        }
        if include_refresh_token:
            token["refresh_token"] = "kmc_oidc_rt_" + secrets.token_urlsafe(32)
        return token

    def save_token(self, token: dict[str, Any], request: OAuth2Request) -> None:
        if self.active_grant is None:
            raise InvalidGrantError()
        self.db.add(
            OIDCToken(
                grant_id=self.active_grant.id,
                access_token_hash=credential_hash(token["access_token"]),
                refresh_token_hash=credential_hash(token["refresh_token"]),
                issued_at=int(time.time()),
                expires_in=token["expires_in"],
                scope=token["scope"],
            )
        )
        self.db.flush()

    def create_oauth2_request(self, request: Any) -> OAuth2Request:
        if isinstance(request, OAuth2Request):
            return request
        converted = StarletteOAuth2Request(request)
        # Authlib reads "Authorization"; a plain dict of Starlette headers
        # has lowercase keys. Preserve the framework's case-insensitive view.
        converted.headers = cast(Any, request.headers)
        return converted

    def handle_response(self, status_code: int, payload: Any, headers: Any) -> Response:
        response_headers = dict(headers)
        response_headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
        if isinstance(payload, dict):
            return JSONResponse(payload, status_code=status_code, headers=response_headers)
        return Response(payload or "", status_code=status_code, headers=response_headers)

    def send_signal(self, name: str, **kwargs: Any) -> None:
        """Do not log Authlib signal values, which may contain credentials."""
