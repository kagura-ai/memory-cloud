"""Wire-compatibility guards for the ``authlib.jose`` → ``joserfc`` move (#1708).

``auth/billing_handoff.py`` now signs and verifies with ``joserfc``. The billing
service verifies tokens minted before this change and may itself still run
``authlib.jose``, so the compact serialization must not move at all:

- a token minted by the ``authlib.jose`` implementation (the golden vector
  below, recorded before the switch) still verifies;
- the ``joserfc`` minter produces the *same bytes* for the same key, header
  and claims — Ed25519 signatures are deterministic (RFC 8032), so the token
  string itself is comparable;
- a ``joserfc``-minted token verifies under ``authlib.jose`` (transitional:
  delete that test together with the Authlib ``<1.9`` bound once Authlib 2.0
  drops ``authlib.jose``);
- ``backend/src`` no longer imports ``authlib.jose``, and the verifier's
  algorithm allow-list is still EdDSA only.

Only the golden vector's PUBLIC key is committed; the private key was
discarded after minting.
"""

from __future__ import annotations

import base64
import calendar
import json
import re
import warnings
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from joserfc import jwt
from joserfc.errors import UnsupportedAlgorithmError
from joserfc.jwk import ECKey, OKPKey

from auth.billing_handoff import (
    BillingHandoffInvalid,
    BillingHandoffSigner,
    BillingHandoffStale,
    verify_handoff_token,
)

# backend/tests/auth/test_billing_handoff_joserfc.py -> auth -> tests -> backend
_SRC = Path(__file__).resolve().parents[2] / "src"

_ISSUER = "kagura-memory-cloud"
_AUDIENCE = "kagura-billing"

# Minted by the authlib.jose implementation (commit before #1708) with
# utcnow() pinned to 2026-06-27T12:00:00Z, jti pinned, exp = 2099-01-01.
_GOLDEN_PUBLIC_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAQyZM0cG+XCnW7IV/aJzNvp6TF5yRulNSu7yaMT52xFM=
-----END PUBLIC KEY-----
"""
_GOLDEN_TOKEN = (
    "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCIsImtpZCI6ImdvbGRlbi0yMDI2In0."
    "eyJpc3MiOiJrYWd1cmEtbWVtb3J5LWNsb3VkIiwiYXVkIjoia2FndXJhLWJpbGxpbmciLCJzdWIiOiJ1c2VyLWdvbGRlbiIsIndvcmtzcGFjZV9pZCI6IjBkM2Y2YTJlLTFiNGMtNGU4Zi05YTdkLTJjNWU4ZjFhM2I2YyIsInJvbGUiOiJvd25lciIsImVwb2NoIjoyLCJpYXQiOjE3ODI1NjE2MDAsImV4cCI6NDA3MDkwODgwMCwianRpIjoiZ29sZGVuLWp0aSJ9."
    "wVu90PPNM6fxJnCOXwezpL9MB1j06khFp6pBDXLeuPH07nuC1NgeS6t34g6JSC77mU2qVheNnHjogL-WiupUCg"
)
_GOLDEN_HEADER = {"alg": "EdDSA", "typ": "JWT", "kid": "golden-2026"}
_GOLDEN_CLAIMS = {
    "iss": _ISSUER,
    "aud": _AUDIENCE,
    "sub": "user-golden",
    "workspace_id": "0d3f6a2e-1b4c-4e8f-9a7d-2c5e8f1a3b6c",
    "role": "owner",
    "epoch": 2,
    "iat": 1782561600,
    "exp": 4070908800,
    "jti": "golden-jti",
}


def _ed25519_keypair() -> tuple[str, str]:
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


def _settings(signing_key: str, *, kid: str = "kid-1", ttl: int = 120) -> SimpleNamespace:
    return SimpleNamespace(
        billing_handoff_signing_key=signing_key,
        billing_handoff_key_id=kid,
        billing_handoff_issuer=_ISSUER,
        billing_handoff_audience=_AUDIENCE,
        billing_handoff_ttl_seconds=ttl,
    )


def _segment(token: str, index: int) -> dict:
    segment = token.split(".")[index]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _authlib_jose():
    """Import the deprecated module without tripping the suite-wide error filter.

    The whole point of #1708 is that nothing in ``backend/src`` imports
    ``authlib.jose`` any more; these tests import it on purpose, as the stand-in
    for a billing service that still runs it.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from authlib.jose import JsonWebKey, JsonWebToken
    return JsonWebKey, JsonWebToken


class TestGoldenVector:
    def test_token_minted_by_authlib_still_verifies(self) -> None:
        claims = verify_handoff_token(
            _GOLDEN_TOKEN,
            _GOLDEN_PUBLIC_PEM,
            current_epoch=2,
            issuer=_ISSUER,
            audience=_AUDIENCE,
        )
        assert claims == _GOLDEN_CLAIMS
        assert _segment(_GOLDEN_TOKEN, 0) == _GOLDEN_HEADER

    def test_token_minted_by_authlib_still_goes_stale(self) -> None:
        # The epoch gate runs after signature/claims on the old token too.
        with pytest.raises(BillingHandoffStale):
            verify_handoff_token(
                _GOLDEN_TOKEN,
                _GOLDEN_PUBLIC_PEM,
                current_epoch=3,
                issuer=_ISSUER,
                audience=_AUDIENCE,
            )

    def test_token_minted_by_authlib_rejects_wrong_audience(self) -> None:
        with pytest.raises(BillingHandoffInvalid):
            verify_handoff_token(
                _GOLDEN_TOKEN,
                _GOLDEN_PUBLIC_PEM,
                current_epoch=2,
                issuer=_ISSUER,
                audience="someone-else",
            )


class TestCrossImplementation:
    """Same key, header and claims → same bytes, and each side verifies the other."""

    @staticmethod
    def _mint_both(private_pem: str) -> tuple[str, str]:
        fixed = datetime(2026, 6, 27, 12, 0, 0)  # naive UTC, as utcnow() returns
        with (
            patch("auth.billing_handoff.utcnow", lambda: fixed),
            patch("auth.billing_handoff.secrets.token_urlsafe", lambda _nbytes: "fixed-jti"),
        ):
            ours = (
                BillingHandoffSigner(settings=_settings(private_pem, kid="rotate-1"))
                .mint(user_id="user-1", workspace_id="ws-1", ownership_epoch=5)
                .token
            )

        iat = calendar.timegm(fixed.timetuple())
        header = {"alg": "EdDSA", "typ": "JWT", "kid": "rotate-1"}
        payload = {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": "user-1",
            "workspace_id": "ws-1",
            "role": "owner",
            "epoch": 5,
            "iat": iat,
            "exp": iat + 120,
            "jti": "fixed-jti",
        }
        JsonWebKey, JsonWebToken = _authlib_jose()
        theirs = JsonWebToken(["EdDSA"]).encode(header, payload, JsonWebKey.import_key(private_pem))
        return ours, theirs.decode() if isinstance(theirs, bytes) else theirs

    def test_joserfc_minter_reproduces_authlib_bytes(self) -> None:
        private_pem, _ = _ed25519_keypair()

        ours, theirs = self._mint_both(private_pem)

        # Header key order ({alg, typ, kid}) and the compact JSON encoding are part
        # of the signed bytes; Ed25519 is deterministic, so the strings must match.
        assert ours == theirs

    def test_joserfc_minted_token_verifies_under_authlib(self) -> None:
        private_pem, public_pem = _ed25519_keypair()
        JsonWebKey, JsonWebToken = _authlib_jose()

        ours, _ = self._mint_both(private_pem)

        claims = JsonWebToken(["EdDSA"]).decode(ours, JsonWebKey.import_key(public_pem))
        claims.validate(now=calendar.timegm(datetime(2026, 6, 27, 12, 0, 30).timetuple()))
        assert claims["sub"] == "user-1"
        assert claims["epoch"] == 5
        assert _segment(ours, 0) == {"alg": "EdDSA", "typ": "JWT", "kid": "rotate-1"}


class TestAlgorithmAllowList:
    def test_verifier_rejects_a_valid_signature_under_another_algorithm(self) -> None:
        # A token correctly signed with ES256, presented with its own EC public
        # key, must still be refused: the verifier accepts EdDSA only, whatever
        # the header says (#183 algorithm-confusion guard, carried over).
        ec_private = generate_private_key(SECP256R1())
        ec_public_pem = (
            ec_private.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        claims = dict(_GOLDEN_CLAIMS)
        token = jwt.encode(
            {"alg": "ES256", "typ": "JWT", "kid": "ec"},
            claims,
            ECKey.import_key(ec_private),
            algorithms=["ES256"],
        )

        with pytest.raises(BillingHandoffInvalid):
            verify_handoff_token(
                token,
                ec_public_pem,
                current_epoch=2,
                issuer=_ISSUER,
                audience=_AUDIENCE,
            )

    def test_minted_token_verifies_only_as_eddsa(self) -> None:
        private_pem, public_pem = _ed25519_keypair()
        minted = BillingHandoffSigner(settings=_settings(private_pem)).mint(
            user_id="u", workspace_id="w"
        )

        key = OKPKey.import_key(public_pem)
        assert jwt.decode(minted.token, key, algorithms=["EdDSA"]).claims["sub"] == "u"
        # joserfc refuses EdDSA unless it is allowed explicitly — a verifier
        # that forgets the allow-list fails closed rather than open.
        with pytest.raises(UnsupportedAlgorithmError):
            jwt.decode(minted.token, key, algorithms=["ES256"])
        with pytest.raises(UnsupportedAlgorithmError):
            jwt.decode(minted.token, key)


class TestNoAuthlibJoseInSource:
    def test_backend_src_does_not_import_authlib_jose(self) -> None:
        # Acceptance for #1708. The suite-wide filterwarnings entry in
        # pyproject.toml turns the deprecation into an error as well, but a
        # lazily imported module would only trip that when its test runs.
        pattern = re.compile(r"^\s*(from|import)\s+authlib\.jose\b", re.MULTILINE)
        offenders = [
            str(path.relative_to(_SRC))
            for path in sorted(_SRC.rglob("*.py"))
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []
