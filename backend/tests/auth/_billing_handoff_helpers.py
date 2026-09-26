"""Shared helpers for the billing hand-off signer tests.

Used by ``test_billing_handoff_signer.py`` (signer behaviour) and
``test_billing_handoff_joserfc.py`` (wire compatibility across the
``authlib.jose`` → ``joserfc`` move, #1708), so the keypair, settings stand-in
and token-segment readers cannot drift between the two.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ISSUER = "kagura-memory-cloud"
AUDIENCE = "kagura-billing"


def ed25519_keypair() -> tuple[str, str]:
    """Return ``(private_pem, public_pem)`` for a fresh Ed25519 key."""
    priv = Ed25519PrivateKey.generate()
    private_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def handoff_settings(
    signing_key: str,
    *,
    kid: str = "kid-1",
    iss: str = ISSUER,
    aud: str = AUDIENCE,
    ttl: int = 120,
) -> SimpleNamespace:
    """A minimal settings stand-in carrying only the fields the signer reads."""
    return SimpleNamespace(
        billing_handoff_signing_key=signing_key,
        billing_handoff_key_id=kid,
        billing_handoff_issuer=iss,
        billing_handoff_audience=aud,
        billing_handoff_ttl_seconds=ttl,
    )


def b64url_json(segment: str) -> dict:
    """Decode one base64url JWT segment into a dict."""
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def token_header(token: str) -> dict:
    """The decoded JOSE header of a compact token."""
    return b64url_json(token.split(".")[0])


def token_payload(token: str) -> dict:
    """The decoded claims of a compact token, without verifying it."""
    return b64url_json(token.split(".")[1])
