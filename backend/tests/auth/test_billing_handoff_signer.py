"""Unit tests for the Ed25519 billing-handoff token signer (#1093).

These tests exercise the crypto core in isolation — no DB, no FastAPI. A throw-away
Ed25519 keypair is generated per test; the minted token is verified with the
*public* key exactly as the external billing service would, asserting the
``(user_id, workspace_id, role)`` binding plus ``iss/aud/exp/iat/jti`` claims and
the ``kid``/``alg`` header. The fail-closed-when-unset contract (503, BILLING-002)
is the security-critical invariant.
"""

from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from authlib.jose import JsonWebKey, JsonWebToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from auth.billing_handoff import BillingHandoffNotConfigured, BillingHandoffSigner

_JWT = JsonWebToken(["EdDSA"])


def _keypair() -> tuple[str, str]:
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


def _settings(
    signing_key: str,
    *,
    kid: str = "kid-1",
    iss: str = "kagura-memory-cloud",
    aud: str = "kagura-billing",
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


def _b64url_json(segment: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _header(token: str) -> dict:
    return _b64url_json(token.split(".")[0])


def _payload(token: str) -> dict:
    return _b64url_json(token.split(".")[1])


class TestBillingHandoffSigner:
    def test_mint_produces_eddsa_jwt_verifiable_with_public_key(self) -> None:
        private_pem, public_pem = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem))
        workspace_id = uuid4()

        minted = signer.mint(user_id="user-123", workspace_id=workspace_id)

        # Verify exactly as the billing service would: with the public key only.
        claims = _JWT.decode(minted.token, JsonWebKey.import_key(public_pem))
        claims.validate()  # exp/iat are sane

        assert claims["sub"] == "user-123"
        assert claims["workspace_id"] == str(workspace_id)
        assert claims["role"] == "owner"
        assert claims["iss"] == "kagura-memory-cloud"
        assert claims["aud"] == "kagura-billing"
        assert claims["jti"] == minted.jti
        assert claims["exp"] > claims["iat"]

    def test_wrong_public_key_rejects_signature(self) -> None:
        private_pem, _ = _keypair()
        _, other_public = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem))

        minted = signer.mint(user_id="u", workspace_id=uuid4())

        with pytest.raises(Exception):  # noqa: B017 - authlib BadSignatureError
            _JWT.decode(minted.token, JsonWebKey.import_key(other_public)).validate()

    def test_header_carries_alg_and_kid(self) -> None:
        private_pem, _ = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem, kid="rotate-2026"))

        minted = signer.mint(user_id="u", workspace_id=uuid4())

        header = _header(minted.token)
        assert header["alg"] == "EdDSA"
        assert header["typ"] == "JWT"
        assert header["kid"] == "rotate-2026"
        assert minted.kid == "rotate-2026"

    def test_exp_is_short_lived(self) -> None:
        private_pem, _ = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem, ttl=120))

        minted = signer.mint(user_id="u", workspace_id=uuid4())

        delta = (minted.expires_at - minted.issued_at).total_seconds()
        assert 1 <= delta <= 120

    def test_iat_and_exp_are_utc_unix_seconds(self) -> None:
        # Regression guard: utcnow() is naive UTC; calling .timestamp() on a naive
        # datetime would interpret it as *local* time and shift iat by the host's
        # UTC offset (e.g. -9h in JST). Assert iat tracks real UTC wall-clock.
        private_pem, _ = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem, ttl=120))

        before = int(time.time())
        minted = signer.mint(user_id="u", workspace_id=uuid4())
        after = int(time.time())

        payload = _payload(minted.token)
        assert before - 2 <= payload["iat"] <= after + 2
        assert payload["exp"] - payload["iat"] == 120

    def test_jti_is_unique_per_mint(self) -> None:
        private_pem, _ = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem))

        jtis = {signer.mint(user_id="u", workspace_id=uuid4()).jti for _ in range(25)}
        assert len(jtis) == 25

    def test_workspace_id_accepts_uuid_and_str_equivalently(self) -> None:
        private_pem, public_pem = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem))
        workspace_id = uuid4()

        from_uuid = signer.mint(user_id="u", workspace_id=workspace_id)
        from_str = signer.mint(user_id="u", workspace_id=str(workspace_id))

        for minted in (from_uuid, from_str):
            claims = _JWT.decode(minted.token, JsonWebKey.import_key(public_pem))
            assert claims["workspace_id"] == str(workspace_id)

    def test_fail_closed_when_signing_key_unset(self) -> None:
        signer = BillingHandoffSigner(settings=_settings(""))

        with pytest.raises(BillingHandoffNotConfigured) as exc_info:
            signer.mint(user_id="u", workspace_id=uuid4())

        assert exc_info.value.status_code == 503
        assert exc_info.value.error_code == "BILLING-002"

    def test_fail_closed_when_kid_unset(self) -> None:
        # A signing key without a kid breaks verifier-side rotation — fail closed
        # rather than mint an unrotatable token.
        private_pem, _ = _keypair()
        signer = BillingHandoffSigner(settings=_settings(private_pem, kid=""))

        with pytest.raises(BillingHandoffNotConfigured):
            signer.mint(user_id="u", workspace_id=uuid4())

    def test_malformed_key_fails_closed_not_500(self) -> None:
        # A non-PEM / garbage key passes the non-blank is_configured check but
        # must fail closed (503) on import rather than raising a raw 500.
        signer = BillingHandoffSigner(
            settings=_settings("-----BEGIN PRIVATE KEY-----\nnot-base64\n-----END PRIVATE KEY-----")
        )
        with pytest.raises(BillingHandoffNotConfigured):
            signer.mint(user_id="u", workspace_id=uuid4())

    def test_is_configured_reflects_key_and_kid(self) -> None:
        private_pem, _ = _keypair()
        assert BillingHandoffSigner(settings=_settings(private_pem)).is_configured is True
        assert BillingHandoffSigner(settings=_settings("")).is_configured is False
        assert BillingHandoffSigner(settings=_settings(private_pem, kid="")).is_configured is False

    def test_whitespace_only_key_is_treated_as_unset(self) -> None:
        signer = BillingHandoffSigner(settings=_settings("   \n  "))
        assert signer.is_configured is False
        with pytest.raises(BillingHandoffNotConfigured):
            signer.mint(user_id="u", workspace_id=uuid4())
