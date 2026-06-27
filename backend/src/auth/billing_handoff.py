"""Ed25519 signer for owner-scoped billing handoff tokens (#1093).

The external billing service (kagura-billing) needs to trust an authenticated
workspace **owner** without re-implementing user auth on the billing host.
memory-cloud mints a short-lived, **Ed25519-signed** JWT bound to
``(user_id, workspace_id, role=owner)`` with ``iss/aud/exp/iat/jti``; the billing
service verifies it with the matching **public** key (``kid``-based rotation).

Design boundaries (kept deliberately narrow — the rest is the verifier's job):

- **Mint only.** This signer produces a unique ``jti`` per token. **Single-use /
  replay rejection is the verifier's responsibility** — memory-cloud mints and
  immediately hands off; it does not keep a consumed-``jti`` store. The token is
  short-lived (``billing_handoff_ttl_seconds``, default 120s) so the replay
  window is small even before the verifier's own ``jti`` check.
- **Fail-closed.** An unset signing key OR an unset ``kid`` raises
  :class:`BillingHandoffNotConfigured` (503, ``BILLING-002``) — a misconfigured
  deployment never mints an unsigned or unrotatable token. Mirrors
  ``internal_billing.verify_billing_service_token``'s unset-token 503.
- **EdDSA via authlib.jose** — the same JWT toolkit the OAuth2 server uses; no
  new crypto dependency (``cryptography`` already ships Ed25519 for the keypair).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from authlib.jose import JsonWebKey, JsonWebToken

from config.settings import get_settings
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException
from utils.logger import get_logger

if TYPE_CHECKING:
    from config.settings import Settings

logger = get_logger(__name__)

# EdDSA is opt-in per authlib's JsonWebToken allow-list — pin it explicitly so
# the signer can never be downgraded to a weaker alg by a permissive default.
_JWT = JsonWebToken(["EdDSA"])
_ALG = "EdDSA"
_TOKEN_TYP = "JWT"
# The handoff token always asserts the owner role — the route only mints after a
# successful owner gate, so the claim is a constant, not caller-supplied.
_ROLE_OWNER = "owner"


class BillingHandoffNotConfigured(MemoryCloudException):
    """The billing handoff signing key (or its ``kid``) is unset — fail closed.

    Surfaces 503 (not 500): the endpoint is *disabled by configuration*, the
    same shape ``internal_billing`` uses for an unset ``BILLING_SERVICE_TOKEN``.
    """

    def __init__(self) -> None:
        super().__init__(
            "Billing handoff signing is not configured",
            status_code=503,
            error_code="BILLING-002",
        )


@dataclass(frozen=True)
class MintedHandoffToken:
    """A freshly minted handoff token plus the metadata the route echoes back."""

    token: str
    jti: str
    kid: str
    issued_at: datetime
    expires_at: datetime
    workspace_id: str
    user_id: str


class BillingHandoffSigner:
    """Mints Ed25519-signed owner handoff tokens from configured key material."""

    def __init__(self, settings: Settings | None = None) -> None:
        # Accept an injected settings object (tests pass a lightweight stand-in);
        # default to the process settings. Only a handful of fields are read.
        self._settings = settings if settings is not None else get_settings()

    def _stripped_material(self) -> tuple[str, str]:
        """Return ``(signing_key, kid)`` with surrounding whitespace stripped.

        Single source of truth for "what key material do we have?" so the
        ``is_configured`` gate, the unconfigured-log breadcrumb, and ``mint``
        can never disagree about whether a value is blank.
        """
        return (
            (self._settings.billing_handoff_signing_key or "").strip(),
            (self._settings.billing_handoff_key_id or "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        """True only when BOTH a non-blank signing key AND a ``kid`` are set."""
        key, kid = self._stripped_material()
        return bool(key) and bool(kid)

    def mint(self, *, user_id: str, workspace_id: UUID | str) -> MintedHandoffToken:
        """Mint a signed handoff token for ``user_id`` scoped to ``workspace_id``.

        The caller MUST have already verified that ``user_id`` owns
        ``workspace_id`` (the route does this via
        ``PermissionService.check_workspace_owner``). This signer enforces only
        the token invariants: owner-role claim, short expiry, unique ``jti``,
        and ``kid``-tagged EdDSA signature.

        Args:
            user_id: The authenticated owner (becomes the ``sub`` claim).
            workspace_id: The target workspace (becomes the ``workspace_id``
                claim) — the route binds this to the request body, never the
                caller's mutable ``current_workspace_id``.

        Returns:
            The minted token and its metadata.

        Raises:
            BillingHandoffNotConfigured: 503 if the signing key or ``kid`` is unset.
        """
        key_pem, kid = self._stripped_material()
        if not key_pem or not kid:
            logger.error(
                "billing_handoff_signing_unconfigured",
                has_key=bool(key_pem),
                has_kid=bool(kid),
            )
            raise BillingHandoffNotConfigured()

        settings = self._settings
        try:
            key = JsonWebKey.import_key(key_pem)
        except Exception as exc:
            # Malformed key material is a configuration failure, not a runtime
            # bug — fail closed (503) like the unset case rather than 500-leaking
            # a stack trace. The exception detail is logged, never returned.
            logger.error("billing_handoff_signing_key_invalid", error=str(exc))
            raise BillingHandoffNotConfigured() from exc

        # utcnow() is naive UTC by convention; stamp tzinfo before .timestamp()
        # so the unix claims are real UTC seconds, not host-local (#backend rules).
        issued_at = utcnow()
        ttl_seconds = max(1, int(settings.billing_handoff_ttl_seconds))
        expires_at = issued_at + timedelta(seconds=ttl_seconds)
        jti = secrets.token_urlsafe(24)
        workspace_str = str(workspace_id)

        header = {"alg": _ALG, "typ": _TOKEN_TYP, "kid": kid}
        payload = {
            "iss": settings.billing_handoff_issuer,
            "aud": settings.billing_handoff_audience,
            "sub": user_id,
            "workspace_id": workspace_str,
            "role": _ROLE_OWNER,
            "iat": int(issued_at.replace(tzinfo=UTC).timestamp()),
            "exp": int(expires_at.replace(tzinfo=UTC).timestamp()),
            "jti": jti,
        }

        try:
            token = _JWT.encode(header, payload, key)
        except Exception as exc:
            # A well-formed key of the WRONG type (e.g. a PUBLIC key, or a
            # non-Ed25519 OKP key) imports cleanly but cannot sign — also a
            # configuration failure, so fail closed (503) rather than 500-leaking.
            logger.error("billing_handoff_signing_failed", error=str(exc))
            raise BillingHandoffNotConfigured() from exc
        if isinstance(token, bytes):
            token = token.decode("ascii")

        logger.info(
            "billing_handoff_minted",
            user_id=user_id,
            workspace_id=workspace_str,
            jti=jti,
            kid=kid,
            expires_at=expires_at.isoformat(),
        )

        return MintedHandoffToken(
            token=token,
            jti=jti,
            kid=kid,
            issued_at=issued_at,
            expires_at=expires_at,
            workspace_id=workspace_str,
            user_id=user_id,
        )
