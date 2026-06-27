"""Billing handoff token minting (Issue #1093 — RFC kagura-payment §1, Layer-1).

memory-cloud is the authority for *who* a user is and *which* workspace they
own. The external payment service (payment.kagura-ai.com) must not re-run our
login; instead memory-cloud mints a short-lived, Ed25519-signed JWT that the
payment service redeems on a single browser-redirect hop. This module owns that
mint (and a reference verify used by the security tests / documenting the
contract the payment service implements).

Security properties (asserted in tests/services/test_billing_handoff_service.py):

- **EdDSA only.** The algorithm list on the ``JsonWebToken`` instance is pinned
  to ``["EdDSA"]``. authlib refuses to encode or verify any token whose header
  ``alg`` is not in that list, which closes the classic alg-confusion holes
  (``alg:none`` and HS256-with-public-key-as-secret). We never fall back to a
  symmetric algorithm.
- **Fail-closed.** An unset signing key raises HANDOFF-001 (503) — a
  misconfigured deployment returns "not configured", never an unsigned or
  forgeable token. Mirrors ``internal_billing.verify_billing_service_token``.
- **No request-controlled claims.** The token is minted purely from
  ``(user_id, workspace_id)`` that the caller already proved ownership of via
  the owner-only session dependency. Nothing from the request body reaches the
  claims.
- **Short TTL.** 120 seconds: the token is redeemed immediately on the redirect
  and is never stored.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any
from uuid import UUID

from authlib.jose import JsonWebToken

from config.settings import get_settings
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException

# Token lifetime in seconds. Short by design: redeemed on the redirect hop,
# never persisted. The route echoes this as ``expires_in``.
HANDOFF_TTL_SECONDS = 120

# EdDSA only. Pinning the algorithm allow-list on the JsonWebToken instance is
# the primary defense against alg-confusion: authlib will not encode or decode a
# token whose header ``alg`` is outside this list, so an ``alg:none`` or
# HS256(public-key) token is rejected before any signature check.
_ALLOWED_ALGS = ["EdDSA"]

_jwt = JsonWebToken(_ALLOWED_ALGS)


def mint_handoff_token(user_id: str, workspace_id: str | UUID) -> str:
    """Mint a short-lived Ed25519 handoff JWT for ``(user_id, workspace_id)``.

    Args:
        user_id: The owner's user id (already authorized by the caller — the
            owner-only session dependency).
        workspace_id: The workspace the user owns, taken from the SESSION (never
            from request input). Stringified into the ``workspace_id`` claim.

    Returns:
        The compact-serialized JWT as a ``str``.

    Raises:
        MemoryCloudException: HANDOFF-001 (503) when the signing key is unset —
            the endpoint is fail-closed.
    """
    settings = get_settings()
    signing_key = settings.billing_handoff_signing_key
    if not signing_key:
        # Mirror internal_billing's fail-closed contract: raise the base
        # MemoryCloudException so the global handler emits the canonical
        # envelope, and we avoid a raw HTTPException (#992 ratchet).
        raise MemoryCloudException(
            "Billing handoff endpoint is not configured",
            status_code=503,
            error_code="HANDOFF-001",
        )

    # Epoch seconds. ``utcnow()`` is naive UTC by project convention; attach UTC
    # before ``.timestamp()`` so the epoch is correct regardless of host tz.
    iat = int(utcnow().replace(tzinfo=UTC).timestamp())
    exp = iat + HANDOFF_TTL_SECONDS

    header: dict[str, Any] = {"alg": "EdDSA", "kid": settings.billing_handoff_kid}
    claims: dict[str, Any] = {
        "iss": "memory-cloud",
        "aud": "payment.kagura-ai.com",
        "sub": str(user_id),
        "workspace_id": str(workspace_id),
        "role": "owner",
        "iat": iat,
        "exp": exp,
        "jti": uuid.uuid4().hex,
        "ver": 1,
    }

    token = _jwt.encode(header, claims, signing_key)
    # authlib returns bytes for the compact serialization; the route embeds the
    # token in a URL/JSON string.
    return token.decode("utf-8") if isinstance(token, bytes) else token


def verify_handoff_token(token: str, public_key_pem: str) -> dict[str, Any]:
    """Verify and decode a handoff token, pinning EdDSA.

    Reference implementation of the contract the payment service performs. It is
    exercised by the security tests and documents that verification MUST pin the
    algorithm: ``_jwt`` only accepts ``alg=EdDSA``, so ``alg:none`` and
    HS256-with-public-key tokens are rejected here just as they must be on the
    payment side.

    Args:
        token: The compact JWT string.
        public_key_pem: Ed25519 public key (PEM, SubjectPublicKeyInfo).

    Returns:
        The decoded claims as a plain dict.

    Raises:
        authlib.jose.errors.JoseError: on a bad signature, an unsupported
            algorithm (alg-confusion), or malformed input.
    """
    claims = _jwt.decode(token, public_key_pem)
    return dict(claims)
