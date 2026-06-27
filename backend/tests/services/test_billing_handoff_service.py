"""Security-critical unit tests for the billing handoff token mint (Issue #1093).

These tests stand alone — no DB, no app harness. They generate an ephemeral
Ed25519 keypair, point the signing key at the private half, mint a handoff
token, and assert every security property of the JWT that the external payment
service will rely on (RFC kagura-payment §1, Layer-1 user-auth handoff):

- algorithm is pinned to EdDSA and the header carries the rotation ``kid``;
- the exact claim set is present and minted from (user_id, workspace_id);
- the token is verifiable with the matching public key and *only* that key;
- alg-confusion tokens (``alg:none`` / HS256-with-pubkey) are rejected by the
  verify path;
- a missing signing key fails closed with HANDOFF-001 (503), never minting.
"""

from __future__ import annotations

import base64
import json

import pytest
from authlib.jose import JsonWebToken
from authlib.jose.errors import JoseError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from config.settings import get_settings
from services.billing_handoff_service import (
    HANDOFF_TTL_SECONDS,
    mint_handoff_token,
    verify_handoff_token,
)
from utils.exceptions import MemoryCloudException

USER_ID = "user-abc-123"
WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
KID = "handoff-key-2026-06"


def _ed25519_pem_pair() -> tuple[str, str]:
    """Return (private_pem_pkcs8, public_pem_spki) as PEM strings."""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = (
        priv.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    return priv_pem, pub_pem


def _decode_segment(segment: str) -> dict:
    """Base64url-decode one compact-JWT segment into its JSON dict."""
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


@pytest.fixture
def signing_key(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Install an ephemeral private signing key + kid; yield (priv, pub) PEMs."""
    priv_pem, pub_pem = _ed25519_pem_pair()
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_handoff_signing_key", priv_pem)
    monkeypatch.setattr(settings, "billing_handoff_kid", KID)
    return priv_pem, pub_pem


def test_header_pins_eddsa_and_kid(signing_key: tuple[str, str]) -> None:
    token = mint_handoff_token(USER_ID, WORKSPACE_ID)
    header = _decode_segment(token.split(".")[0])
    assert header["alg"] == "EdDSA"
    assert header["kid"] == KID
    # No HS*/none must ever appear in a minted header.
    assert not header["alg"].lower().startswith("hs")
    assert header["alg"].lower() != "none"


def test_claims_are_exact_and_from_arguments(signing_key: tuple[str, str]) -> None:
    _, pub_pem = signing_key
    token = mint_handoff_token(USER_ID, WORKSPACE_ID)
    claims = verify_handoff_token(token, pub_pem)

    assert claims["iss"] == "memory-cloud"
    assert claims["aud"] == "payment.kagura-ai.com"
    assert claims["sub"] == USER_ID
    assert claims["workspace_id"] == WORKSPACE_ID
    assert claims["role"] == "owner"
    assert claims["ver"] == 1
    # jti is a unique, opaque, non-empty identifier.
    assert isinstance(claims["jti"], str) and claims["jti"]


def test_jti_is_unique_per_mint(signing_key: tuple[str, str]) -> None:
    _, pub_pem = signing_key
    a = verify_handoff_token(mint_handoff_token(USER_ID, WORKSPACE_ID), pub_pem)
    b = verify_handoff_token(mint_handoff_token(USER_ID, WORKSPACE_ID), pub_pem)
    assert a["jti"] != b["jti"]


def test_ttl_is_exactly_120s(signing_key: tuple[str, str]) -> None:
    _, pub_pem = signing_key
    claims = verify_handoff_token(mint_handoff_token(USER_ID, WORKSPACE_ID), pub_pem)
    assert HANDOFF_TTL_SECONDS == 120
    assert claims["exp"] - claims["iat"] == 120


def test_workspace_id_uuid_is_stringified(signing_key: tuple[str, str]) -> None:
    from uuid import UUID

    _, pub_pem = signing_key
    ws = UUID(WORKSPACE_ID)
    claims = verify_handoff_token(mint_handoff_token(USER_ID, ws), pub_pem)
    assert claims["workspace_id"] == WORKSPACE_ID
    assert isinstance(claims["workspace_id"], str)


def test_token_verifies_only_with_matching_public_key(signing_key: tuple[str, str]) -> None:
    _, pub_pem = signing_key
    token = mint_handoff_token(USER_ID, WORKSPACE_ID)

    # Correct key verifies.
    assert verify_handoff_token(token, pub_pem)["sub"] == USER_ID

    # A different keypair's public key must NOT verify the signature.
    _, wrong_pub = _ed25519_pem_pair()
    with pytest.raises(JoseError):  # BadSignatureError
        verify_handoff_token(token, wrong_pub)


def test_alg_none_token_is_rejected(signing_key: tuple[str, str]) -> None:
    """An unsigned ``alg:none`` token must never verify (alg-confusion)."""
    _, pub_pem = signing_key

    def b64(d: bytes) -> bytes:
        return base64.urlsafe_b64encode(d).rstrip(b"=")

    forged = (
        b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        + b"."
        + b64(json.dumps({"sub": "attacker", "role": "owner"}).encode())
        + b"."
    ).decode()
    with pytest.raises(JoseError):  # UnsupportedAlgorithmError
        verify_handoff_token(forged, pub_pem)


def test_hs256_pubkey_confusion_is_rejected(signing_key: tuple[str, str]) -> None:
    """A token signed HS256 (the classic pubkey-as-HMAC-secret attack) must be
    rejected because the verify path pins EdDSA."""
    _, pub_pem = signing_key
    hs = JsonWebToken(["HS256"])
    forged = hs.encode(
        {"alg": "HS256"},
        {"sub": "attacker", "role": "owner"},
        b"attacker-controlled-hmac-secret-0123456789",
    ).decode()
    with pytest.raises(JoseError):  # UnsupportedAlgorithmError
        verify_handoff_token(forged, pub_pem)


def test_unset_signing_key_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No signing key configured → HANDOFF-001 (503), never an unsigned token."""
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_handoff_signing_key", "")
    monkeypatch.setattr(settings, "billing_handoff_kid", "")

    with pytest.raises(MemoryCloudException) as exc_info:
        mint_handoff_token(USER_ID, WORKSPACE_ID)

    assert exc_info.value.status_code == 503
    assert exc_info.value.error_code == "HANDOFF-001"
