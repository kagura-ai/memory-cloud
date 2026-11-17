"""Purpose-separated payment-method setup tokens; never generic billing authority."""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from joserfc import jwt
from joserfc.jwk import OKPKey

from config.settings import get_settings
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import Settings

logger = get_logger(__name__)
SETUP_AUDIENCE = "kagura-billing-sync-setup"
SETUP_TOKEN_TYPE = "kagura-sync-setup+jwt"  # noqa: S105 -- public protocol discriminator
SETUP_PURPOSE = "sync_addon_setup"
MAX_SETUP_TTL_SECONDS = 60
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


class SyncSetupHandoffUnavailable(MemoryCloudException):
    """A setup credential cannot safely be issued; no key or native cause escapes."""

    def __init__(self) -> None:
        super().__init__(
            "Sync add-on setup handoff is unavailable",
            status_code=503,
            error_code="SYNC_SETUP_UNAVAILABLE",
        )


@dataclass(frozen=True)
class MintedSyncSetupHandoff:
    """Opaque one-time setup token and finite metadata for the fixed redirect."""

    token: str = field(repr=False)
    kid: str
    jti: str
    issued_at: datetime
    expires_at: datetime


def _text(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("Invalid trusted setup binding")
    return value


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


class SyncSetupHandoffSigner:
    """Sign only the setup purpose, using the existing operator-managed Ed25519 key."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()

    def mint(
        self,
        *,
        user_id: str,
        workspace_id: UUID,
        service_id: str,
        client_id: str,
        application_id: UUID,
        application_revision: int,
        input_digest: str,
        ownership_epoch: int,
        valid_until: datetime,
    ) -> MintedSyncSetupHandoff:
        """Caller holds current owner/registration fences through the final COMMIT.

        Single-use redemption and a setup-only session belong to payment. This
        method never creates, wraps or derives a generic owner billing token.
        """
        try:
            settings = self._settings
            if settings.billing_handoff_audience == SETUP_AUDIENCE:
                raise ValueError("Credential audiences must be distinct")
            private_pem = settings.billing_handoff_signing_key
            # PEM is a multiline cryptographic format, not a one-line claim.
            # Its parser validates the key; the size/type bound limits the input.
            if not isinstance(private_pem, str) or not 1 <= len(private_pem) <= 16_384:
                raise ValueError("Unsupported signing material")
            private_pem = private_pem.strip()
            kid = _text(settings.billing_handoff_key_id, 128).strip()
            issuer = _text(settings.billing_handoff_issuer, 255)
            ttl = settings.billing_handoff_ttl_seconds
            if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
                raise ValueError("Invalid setup lifetime")
            if (
                type(application_revision) is not int
                or not 1 <= application_revision <= 2_147_483_646
                or type(ownership_epoch) is not int
                or not 0 <= ownership_epoch <= _MAX_SAFE_INTEGER
                or not isinstance(input_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", input_digest) is None
                or not isinstance(service_id, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", service_id) is None
            ):
                raise ValueError("Invalid trusted setup binding")
            subject, client = _text(user_id, 255), _text(client_id, 255)
            workspace, application = str(UUID(str(workspace_id))), str(UUID(str(application_id)))
            now = _utc(utcnow())
            end = min(now + timedelta(seconds=min(ttl, MAX_SETUP_TTL_SECONDS)), _utc(valid_until))
            issued_seconds = int(now.replace(tzinfo=UTC).timestamp())
            expires_seconds = int(end.replace(tzinfo=UTC).timestamp())
            if expires_seconds <= issued_seconds:
                raise ValueError("Original authority expired")
            # Import a genuine private Ed25519 key before joserfc can select any
            # signing behavior. Public-only/wrong-curve material fails closed.
            if not isinstance(
                load_pem_private_key(private_pem.encode("utf-8"), password=None),
                Ed25519PrivateKey,
            ):
                raise ValueError("Unsupported signing material")
            key = OKPKey.import_key(private_pem)
            jti = secrets.token_urlsafe(24)
            claims = {
                "iss": issuer,
                "aud": SETUP_AUDIENCE,
                "purpose": SETUP_PURPOSE,
                "sub": subject,
                "workspace_id": workspace,
                "service_id": service_id,
                "client_id": client,
                "application_id": application,
                "application_revision": application_revision,
                "input_digest": input_digest,
                "epoch": ownership_epoch,
                "iat": issued_seconds,
                "exp": expires_seconds,
                "jti": jti,
            }
            token = jwt.encode(
                {"alg": "Ed25519", "typ": SETUP_TOKEN_TYPE, "kid": kid},
                claims,
                key,
                algorithms=("Ed25519",),
                default_type=None,
            )
            return MintedSyncSetupHandoff(
                token=token,
                kid=kid,
                jti=jti,
                issued_at=now,
                expires_at=datetime.fromtimestamp(expires_seconds, UTC).replace(tzinfo=None),
            )
        except Exception:
            # Cryptographic and settings exceptions may include key material.
            # Neither those strings nor subject/application/request data is logged.
            logger.warning("sync_setup_handoff_unavailable")
            raise SyncSetupHandoffUnavailable() from None
