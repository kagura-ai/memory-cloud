"""Credential hashes cannot themselves be used as bearer secrets (#2399)."""

import hashlib

import pytest

from auth.oauth_token_storage import hash_oauth_token, token_lookup_values, token_matches


def test_digest_is_tagged_sha256_and_null_is_preserved():
    assert hash_oauth_token("credential") == "sha256$" + hashlib.sha256(b"credential").hexdigest()
    assert hash_oauth_token(None) is None


@pytest.mark.parametrize("presented", ["sha256$" + "a" * 64, "sha256$malformed", ""])
def test_stored_representation_is_never_a_presentable_credential(presented):
    assert token_lookup_values(presented) == ()
    assert not token_matches(presented, presented)


def test_lookup_preserves_legacy_and_hash_compatibility():
    raw = "existing-session-secret"
    digest = hash_oauth_token(raw)
    assert token_lookup_values(raw) == (digest, raw)
    assert token_matches(raw, digest)
    assert token_matches(raw, raw)
    assert not token_matches(raw, None)
    assert not token_matches("another", digest)


def test_model_repr_does_not_disclose_any_credential_material():
    from models.auth import OAuth2Token

    raw = "uniquely-identifiable-secret"
    token = OAuth2Token(id=17, client_id="client", access_token=raw, refresh_token=raw)
    assert raw[:8] not in repr(token)
    assert "17" in repr(token)
