"""Wire-format guards for the billing hand-off token (#1708, #1727).

``auth/billing_handoff.py`` signs and verifies with ``joserfc`` under the RFC 9864
algorithm name ``"Ed25519"`` (#1727; it was ``"EdDSA"`` before). The billing
service verifies these tokens with its own implementation, so the compact
serialization is pinned here:

- a recorded golden vector (``alg="Ed25519"``) still verifies;
- the signer produces the *same bytes* as a plain ``joserfc`` encode of the
  same key, header and claims — Ed25519 signatures are deterministic
  (RFC 8032), so the token string itself is comparable;
- the verifier's algorithm allow-list is ``Ed25519`` only: the same key
  signing under the old ``"EdDSA"`` name is refused;
- nothing emits joserfc's RFC 9864 ``SecurityWarning`` any more, so no filter
  is needed;
- ``backend/src`` does not import ``authlib.jose``.

Only the golden vector's PUBLIC key is committed; the private key was
discarded after minting.
"""

from __future__ import annotations

import calendar
import re
import warnings
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from joserfc import jwt
from joserfc.errors import SecurityWarning, UnsupportedAlgorithmError
from joserfc.jwk import OKPKey

from auth.billing_handoff import (
    BillingHandoffInvalid,
    BillingHandoffSigner,
    BillingHandoffStale,
    verify_handoff_token,
)

from ._billing_handoff_helpers import (
    AUDIENCE,
    ISSUER,
    ed25519_keypair,
    handoff_settings,
    token_header,
)

# backend/tests/auth/test_billing_handoff_joserfc.py -> auth -> tests -> backend
_SRC = Path(__file__).resolve().parents[2] / "src"

# Minted with joserfc under alg="Ed25519" (#1727), iat = 2026-09-27T00:00:00Z,
# jti pinned, exp = 2099-01-01.
_GOLDEN_PUBLIC_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAnXx8eaKEnJ0QjHqgIbyPEkI7DhfFrmvNbMIZbgAhn9E=
-----END PUBLIC KEY-----
"""
_GOLDEN_TOKEN = (
    "eyJhbGciOiJFZDI1NTE5IiwidHlwIjoiSldUIiwia2lkIjoiZ29sZGVuLTIwMjYifQ."
    "eyJpc3MiOiJrYWd1cmEtbWVtb3J5LWNsb3VkIiwiYXVkIjoia2FndXJhLWJpbGxpbmciLCJzdWIiOiJ1c2VyLWdvbGRlbiIsIndvcmtzcGFjZV9pZCI6IjBkM2Y2YTJlLTFiNGMtNGU4Zi05YTdkLTJjNWU4ZjFhM2I2YyIsInJvbGUiOiJvd25lciIsImVwb2NoIjoyLCJpYXQiOjE3OTA0NjcyMDAsImV4cCI6NDA3MDkwODgwMCwianRpIjoiZ29sZGVuLWp0aSJ9."
    "zGAAKzA2-0SovOFmmHilm2culDU80kqeOEdjFnFCZZZvC1VtnwTUlzOYsG_Ea7oV5ke0RGdx4U5S4Qvo_RmuAg"
)
_GOLDEN_HEADER = {"alg": "Ed25519", "typ": "JWT", "kid": "golden-2026"}
_GOLDEN_CLAIMS = {
    "iss": ISSUER,
    "aud": AUDIENCE,
    "sub": "user-golden",
    "workspace_id": "0d3f6a2e-1b4c-4e8f-9a7d-2c5e8f1a3b6c",
    "role": "owner",
    "epoch": 2,
    "iat": 1790467200,
    "exp": 4070908800,
    "jti": "golden-jti",
}


class TestGoldenVector:
    def test_recorded_token_still_verifies(self) -> None:
        claims = verify_handoff_token(
            _GOLDEN_TOKEN,
            _GOLDEN_PUBLIC_PEM,
            current_epoch=2,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        assert claims == _GOLDEN_CLAIMS
        assert token_header(_GOLDEN_TOKEN) == _GOLDEN_HEADER

    def test_recorded_token_still_goes_stale(self) -> None:
        # The epoch gate runs after signature/claims on the recorded token too.
        with pytest.raises(BillingHandoffStale):
            verify_handoff_token(
                _GOLDEN_TOKEN,
                _GOLDEN_PUBLIC_PEM,
                current_epoch=3,
                issuer=ISSUER,
                audience=AUDIENCE,
            )

    def test_recorded_token_rejects_wrong_audience(self) -> None:
        with pytest.raises(BillingHandoffInvalid):
            verify_handoff_token(
                _GOLDEN_TOKEN,
                _GOLDEN_PUBLIC_PEM,
                current_epoch=2,
                issuer=ISSUER,
                audience="someone-else",
            )


class TestWireFormat:
    """The signer's bytes are a plain joserfc encode — nothing custom on the wire."""

    def test_signer_matches_a_plain_joserfc_encode(self) -> None:
        private_pem, _ = ed25519_keypair()
        fixed = datetime(2026, 9, 27, 12, 0, 0)  # naive UTC, as utcnow() returns
        # secrets.token_urlsafe is patched on the stdlib module itself (the
        # signer has no local alias), so it is process-wide for this block;
        # nothing else mints inside it.
        with (
            patch("auth.billing_handoff.utcnow", lambda: fixed),
            patch("secrets.token_urlsafe", lambda nbytes: "fixed-jti"),
        ):
            ours = (
                BillingHandoffSigner(settings=handoff_settings(private_pem, kid="rotate-1"))
                .mint(user_id="user-1", workspace_id="ws-1", ownership_epoch=5)
                .token
            )

        iat = calendar.timegm(fixed.timetuple())
        expected = jwt.encode(
            {"alg": "Ed25519", "typ": "JWT", "kid": "rotate-1"},
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": "user-1",
                "workspace_id": "ws-1",
                "role": "owner",
                "epoch": 5,
                "iat": iat,
                "exp": iat + 120,
                "jti": "fixed-jti",
            },
            OKPKey.import_key(private_pem),
            algorithms=["Ed25519"],
            default_type=None,
        )

        # Header key order ({alg, typ, kid}) and the compact JSON encoding are part
        # of the signed bytes; Ed25519 is deterministic, so the strings must match.
        assert ours == expected
        assert token_header(ours) == {"alg": "Ed25519", "typ": "JWT", "kid": "rotate-1"}


class TestAlgorithmAllowList:
    def test_verifier_rejects_the_same_key_under_the_old_eddsa_name(self) -> None:
        # The same Ed25519 key signs a token whose header says alg="EdDSA" (the
        # pre-#1727 name). The signature is valid for the key, so only the
        # verifier's allow-list can refuse it (#183 algorithm-confusion guard).
        private_pem, public_pem = ed25519_keypair()
        with warnings.catch_warnings():
            # Minting the old name is what warns; the verifier must not.
            warnings.simplefilter("ignore", SecurityWarning)
            token = jwt.encode(
                {"alg": "EdDSA", "typ": "JWT", "kid": "kid-1"},
                dict(_GOLDEN_CLAIMS),
                OKPKey.import_key(private_pem),
                algorithms=["EdDSA"],
            )

        with pytest.raises(BillingHandoffInvalid):
            verify_handoff_token(
                token,
                public_pem,
                current_epoch=2,
                issuer=ISSUER,
                audience=AUDIENCE,
            )

    def test_mint_and_verify_emit_no_security_warning(self) -> None:
        # #1727 acceptance: no filter is installed, so any SecurityWarning from
        # joserfc (e.g. a regression back to "EdDSA") fails here.
        private_pem, public_pem = ed25519_keypair()
        with warnings.catch_warnings():
            warnings.simplefilter("error", SecurityWarning)
            minted = BillingHandoffSigner(settings=handoff_settings(private_pem)).mint(
                user_id="u", workspace_id="w"
            )
            verify_handoff_token(
                minted.token,
                public_pem,
                current_epoch=0,
                issuer=ISSUER,
                audience=AUDIENCE,
            )

    def test_minted_token_verifies_only_as_ed25519(self) -> None:
        private_pem, public_pem = ed25519_keypair()
        minted = BillingHandoffSigner(settings=handoff_settings(private_pem)).mint(
            user_id="u", workspace_id="w"
        )

        key = OKPKey.import_key(public_pem)
        assert jwt.decode(minted.token, key, algorithms=["Ed25519"]).claims["sub"] == "u"
        # A verifier that allows only something else fails closed.
        with pytest.raises(UnsupportedAlgorithmError):
            jwt.decode(minted.token, key, algorithms=["ES256"])


class TestClaimTypes:
    """The verifier refuses wrongly typed registered claims.

    joserfc checks the types of ``iss`` and ``sub`` from 1.7.3 on, which is why
    ``pyproject.toml`` sets that floor; before it, an ``iss`` array that merely
    contained the expected issuer was accepted.
    """

    @staticmethod
    def _token(private_pem: str, **overrides: object) -> str:
        claims = {**_GOLDEN_CLAIMS, **overrides}
        return jwt.encode(
            {"alg": "Ed25519", "typ": "JWT", "kid": "kid-1"},
            claims,
            OKPKey.import_key(private_pem),
            algorithms=["Ed25519"],
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"iss": [ISSUER, "someone-else"]}, id="iss-array-containing-issuer"),
            pytest.param({"iss": 1}, id="iss-number"),
            pytest.param({"sub": 5}, id="sub-number"),
        ],
    )
    def test_wrongly_typed_claim_is_invalid(self, overrides: dict) -> None:
        private_pem, public_pem = ed25519_keypair()

        with pytest.raises(BillingHandoffInvalid):
            verify_handoff_token(
                self._token(private_pem, **overrides),
                public_pem,
                current_epoch=2,
                issuer=ISSUER,
                audience=AUDIENCE,
            )

    def test_audience_array_containing_the_audience_is_accepted(self) -> None:
        # RFC 7519 allows `aud` to be an array; the Authlib verifier accepted
        # it too, so this is unchanged behaviour, pinned so it stays deliberate.
        private_pem, public_pem = ed25519_keypair()

        claims = verify_handoff_token(
            self._token(private_pem, aud=[AUDIENCE, "another-service"]),
            public_pem,
            current_epoch=2,
            issuer=ISSUER,
            audience=AUDIENCE,
        )
        assert claims["aud"] == [AUDIENCE, "another-service"]


class TestNoAuthlibJoseInSource:
    def test_backend_src_does_not_import_authlib_jose(self) -> None:
        # Acceptance for #1708. The suite-wide filterwarnings entry in
        # pyproject.toml turns the deprecation into an error as well, but a
        # lazily imported module would only trip that when its test runs.
        pattern = re.compile(
            r"^\s*(from|import)\s+authlib\.jose\b"
            r"|^\s*from\s+authlib\s+import\b.*\bjose\b"
            r"|(import_module|__import__)\(\s*[\"']authlib\.jose",
            re.MULTILINE,
        )
        offenders = [
            str(path.relative_to(_SRC))
            for path in sorted(_SRC.rglob("*.py"))
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []
