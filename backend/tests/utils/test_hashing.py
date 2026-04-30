"""Tests for utils.hashing primitives."""

import hashlib
import hmac

from utils.hashing import hmac_sha256_hex, sha256_hex


class TestSha256Hex:
    def test_returns_64_char_hex(self):
        assert len(sha256_hex("anything")) == 64

    def test_deterministic(self):
        assert sha256_hex("foo") == sha256_hex("foo")

    def test_salt_changes_output(self):
        assert sha256_hex("foo", salt="a") != sha256_hex("foo", salt="b")

    def test_no_salt_default(self):
        expected = hashlib.sha256(b"foo").hexdigest()
        assert sha256_hex("foo") == expected


class TestHmacSha256Hex:
    def test_returns_64_char_hex(self):
        assert len(hmac_sha256_hex("anything", key="k")) == 64

    def test_matches_stdlib_hmac(self):
        expected = hmac.new(b"my-key", b"alice@example.com", hashlib.sha256).hexdigest()
        assert hmac_sha256_hex("alice@example.com", key="my-key") == expected

    def test_different_keys_yield_different_digest(self):
        a = hmac_sha256_hex("alice@example.com", key="k1")
        b = hmac_sha256_hex("alice@example.com", key="k2")
        assert a != b

    def test_resists_naive_salt_attack(self):
        """An attacker who knows the value but not the key cannot reproduce the digest.

        Contrast with sha256_hex(value, salt=public_salt): attacker who knows
        salt + value can compute the digest. HMAC requires the key.
        """
        target = hmac_sha256_hex("victim@example.com", key="server-secret")
        attacker_guesses = [
            sha256_hex("victim@example.com"),
            sha256_hex("victim@example.com", salt=""),
            sha256_hex("victim@example.com", salt="kagura-erasure-v1"),
        ]
        assert target not in attacker_guesses

    def test_empty_value_hashable(self):
        digest = hmac_sha256_hex("", key="k")
        assert len(digest) == 64

    def test_unicode_value(self):
        digest = hmac_sha256_hex("ユーザー@例.jp", key="k")
        assert len(digest) == 64
