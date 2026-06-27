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


class BillingHandoffStale(MemoryCloudException):
    """The token's ownership epoch is older than the workspace's live epoch (#1100).

    Ownership was transferred since the token was minted, so the credential is
    invalidated. 401 (not 403): the token is a once-valid credential that has
    been superseded, mirroring an expired-token rejection.
    """

    def __init__(self) -> None:
        super().__init__(
            "Billing handoff token is stale (workspace ownership changed)",
            status_code=401,
            error_code="BILLING-003",
        )


class BillingHandoffInvalid(MemoryCloudException):
    """The handoff token failed signature or standard-claim (iss/aud/exp) checks.

    Raised by the reference verifier; the staleness check (:class:`BillingHandoffStale`)
    is a *separate*, later gate so a caller can distinguish "forged/expired" from
    "superseded by ownership transfer".
    """

    def __init__(self) -> None:
        super().__init__(
            "Billing handoff token is invalid",
            status_code=401,
            error_code="BILLING-004",
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
    ownership_epoch: int


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

    def mint(
        self,
        *,
        user_id: str,
        workspace_id: UUID | str,
        ownership_epoch: int = 0,
    ) -> MintedHandoffToken:
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
            ownership_epoch: The workspace's live ``ownership_epoch`` at mint time
                (#1100). Embedded as the ``epoch`` claim so the verifier can reject
                the token once ownership is transferred (the epoch advances). The
                route MUST pass the live value; the ``0`` default is the fail-safe
                floor (only valid while the workspace has never been transferred).

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
            "epoch": int(ownership_epoch),
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
            ownership_epoch=int(ownership_epoch),
        )


def verify_handoff_token(
    token: str,
    verifying_key: str,
    *,
    current_epoch: int,
    issuer: str,
    audience: str,
) -> dict:
    """Reference verifier — the canonical ownership-epoch staleness check (#1100).

    memory-cloud mints handoff tokens but does **not** gate their redemption (the
    mint-only boundary documented above): the external billing service is the
    verifier. This helper is the *spec* that verifier implements, and the in-repo
    test hook that makes the "minted at epoch N → rejected after a transfer to
    N+1" acceptance criterion provable without the external repo.

    It verifies the EdDSA signature with ``verifying_key`` (the distributed public
    key), enforces ``iss``/``aud``/``exp``, then rejects the token when its
    embedded ``epoch`` claim is **strictly older** than ``current_epoch`` — i.e.
    ownership was transferred since the token was minted. A missing ``epoch`` claim
    (a token minted before #1100) is treated as ``0``.

    Unlike a token claim consumed blindly, ``epoch`` is **not** trusted as an
    authorization fact — it is a freshness assertion that is always compared
    against the live ``current_epoch``. This is deliberately the opposite stance to
    #649 (where the token's ``workspace_id`` was declared "not security-bearing,
    point-in-time"): there the claim was trusted and so kept non-bearing; here the
    claim is re-validated against live state on every verify, so it can be bearing.

    Args:
        token: The compact EdDSA handoff JWS.
        verifying_key: PEM/JWK public key matching the token's ``kid``.
        current_epoch: The workspace's live ``ownership_epoch`` at verify time.
        issuer: Expected ``iss`` claim.
        audience: Expected ``aud`` claim.

    Returns:
        The validated claims as a plain ``dict``.

    Raises:
        BillingHandoffInvalid: 401 (BILLING-004) — bad signature / iss / aud / exp.
        BillingHandoffStale: 401 (BILLING-003) — the ownership epoch advanced.
    """
    try:
        claims = _JWT.decode(
            token,
            JsonWebKey.import_key(verifying_key),
            claims_options={
                "iss": {"essential": True, "value": issuer},
                "aud": {"essential": True, "value": audience},
            },
        )
        claims.validate()  # exp/iat/iss/aud — raises on any mismatch
    except Exception as exc:
        raise BillingHandoffInvalid() from exc

    # Staleness is a SEPARATE gate, after signature/claims pass, so the caller can
    # tell "forged/expired" (BILLING-004) apart from "superseded by transfer"
    # (BILLING-003). Missing claim → 0 (pre-#1100 token). Strict ``<``: an equal
    # epoch is the same ownership generation and stays valid.
    token_epoch = int(claims.get("epoch", 0) or 0)
    if token_epoch < int(current_epoch):
        raise BillingHandoffStale()
    return dict(claims)
