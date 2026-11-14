"""Real signatures and strict service assertion boundaries (#2806)."""

import base64
import hashlib
import json

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from auth.sync_service_jwt import InvalidAssertion, public_key, verify_assertion

ISSUER = "https://identity.example.test/oidc"
NOW = 1800000000


@pytest.fixture(scope="module")
def key():
    return RSAKey.generate_key(2048, parameters={"kid": "worker-key", "alg": "RS256"})


def assertion(key, **changes):
    claims = {
        "iss": "worker",
        "sub": "worker",
        "aud": ISSUER,
        "iat": NOW,
        "exp": NOW + 60,
        "jti": "unique-assertion-1234",
    }
    claims.update(changes)
    return jwt.encode(
        {"alg": "RS256", "kid": "worker-key", "typ": "client-authentication+jwt"},
        claims,
        key,
        default_type=None,
    )


def verify(value, key):
    return verify_assertion(
        value, public_key(key.as_dict(private=False)), client_id="worker", issuer=ISSUER, now=NOW
    )


def test_valid_assertion_is_bound_and_jti_is_not_retained(key):
    proof = verify(assertion(key), key)
    assert proof.client_id == "worker"
    assert proof.kid == "worker-key"
    assert proof.jti_hash == hashlib.sha256(b"unique-assertion-1234").hexdigest()
    assert "unique-assertion-1234" not in repr(proof)


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "other"},
        {"sub": "other"},
        {"aud": ISSUER + "/token"},
        {"aud": [ISSUER, "https://other.test"]},
        {"aud": [ISSUER]},
        {"exp": NOW},
        {"exp": NOW + 61},
        {"iat": NOW + 1},
        {"iat": NOW - 1},
        {"exp": True},
        {"iat": float(NOW)},
        {"nbf": NOW + 1},
        {"jti": ""},
        {"jti": "x" * 129},
        {"jti": ["not-text"]},
    ],
)
def test_assertion_claims_cannot_change_recipient_or_lifetime(key, changes):
    with pytest.raises(InvalidAssertion):
        verify(assertion(key, **changes), key)


def test_different_signature_and_modified_payload_are_rejected(key):
    foreign = RSAKey.generate_key(2048)
    with pytest.raises(InvalidAssertion):
        verify(assertion(foreign), key)
    parts = assertion(key).split(".")
    parts[1] = base64.urlsafe_b64encode(json.dumps({"sub": "other"}).encode()).decode().rstrip("=")
    with pytest.raises(InvalidAssertion):
        verify(".".join(parts), key)


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none"},
        {"jku": "https://attacker.test/keys"},
        {"crit": ["x"]},
        {"typ": "at+jwt"},
        {"kid": "other"},
    ],
)
def test_untrusted_headers_never_choose_remote_keys_or_other_algorithms(key, header):
    token = assertion(key)
    parts = token.split(".")
    original = {"alg": "RS256", "kid": "worker-key", "typ": "client-authentication+jwt"}
    original.update(header)
    parts[0] = base64.urlsafe_b64encode(json.dumps(original).encode()).decode().rstrip("=")
    with pytest.raises(InvalidAssertion):
        verify(".".join(parts), key)


def test_duplicate_json_claims_are_rejected_before_verification(key):
    parts = assertion(key).split(".")
    parts[1] = base64.urlsafe_b64encode(b'{"iss":"other","iss":"worker"}').decode().rstrip("=")
    with pytest.raises(InvalidAssertion):
        verify(".".join(parts), key)


def test_only_public_key_material_can_be_registered(key):
    with pytest.raises(ValueError):
        public_key(key.as_dict(private=True))
    with pytest.raises(ValueError):
        public_key({**key.as_dict(private=False), "jku": "https://attacker.test"})


def test_key_material_fingerprint_is_independent_of_alias_and_integer_padding(key):
    from auth.sync_service_jwt import key_fingerprint

    document = public_key(key.as_dict(private=False))
    raw = base64.urlsafe_b64decode(document["n"] + "=" * (-len(document["n"]) % 4))
    alias = {
        **document,
        "kid": "another-id",
        "n": base64.urlsafe_b64encode(b"\x00" + raw).decode().rstrip("="),
    }
    try:
        fingerprint = key_fingerprint(alias)
    except ValueError:
        return  # Refusing noncanonical integers is also safe.
    assert fingerprint == key_fingerprint(document)
