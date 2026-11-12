"""The announcement unsubscribe token (Issue #2761).

A Fernet token under a key derived for this purpose only from each entry of
the ``API_KEY_SECRET`` ring. Pinned here:

- round trip, binding to the account row (``users.id`` + ``user_id``);
- anything wrong (tampered, truncated, padded, empty, oversized, foreign
  alphabet, another purpose) reads as ``None``, never raises and never logs
  the token;
- a token issued under the previous key still opens after a rotation, and a
  value encrypted by ``get_encryptor()`` (stored provider keys) never opens as
  a token;
- the List-Unsubscribe headers and the page URL (the token in a fragment).
"""

from __future__ import annotations

import base64
import json
import logging

import pytest
from cryptography.fernet import Fernet

from auth import announcement_unsubscribe as tok
from config.settings import get_settings

SECRET = "unit-test-secret-for-announcement-tokens-0001"
OTHER = "unit-test-secret-for-announcement-tokens-0002"


@pytest.fixture
def ring(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_key_secret", SECRET)
    monkeypatch.setattr(settings, "api_key_secret_previous", "")
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com/")

    def _set(current: str, previous: str = "") -> None:
        monkeypatch.setattr(settings, "api_key_secret", current)
        monkeypatch.setattr(settings, "api_key_secret_previous", previous)

    return _set


class TestRoundTrip:
    def test_reads_back_the_account(self, ring) -> None:
        token = tok.issue_unsubscribe_token(account_id=42, user_id="google-123")

        claim = tok.read_unsubscribe_token(token)

        assert claim == tok.UnsubscribeClaim(account_id=42, user_id="google-123")

    def test_url_safe_without_padding(self, ring) -> None:
        token = tok.issue_unsubscribe_token(account_id=1, user_id="u" * 255)

        assert tok.TOKEN_RE.fullmatch(token)
        assert "=" not in token
        assert len(token) <= tok.MAX_TOKEN_LENGTH

    def test_opaque(self, ring) -> None:
        token = tok.issue_unsubscribe_token(account_id=42, user_id="google-123")
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

        assert "google-123" not in token
        assert b"google-123" not in raw
        assert b"au1" not in raw

    def test_previous_key_still_opens(self, ring) -> None:
        ring(OTHER)
        old = tok.issue_unsubscribe_token(account_id=7, user_id="gh-7")
        ring(SECRET, OTHER)

        assert tok.read_unsubscribe_token(old) == tok.UnsubscribeClaim(7, "gh-7")

    def test_a_dropped_key_does_not_open(self, ring) -> None:
        ring(OTHER)
        old = tok.issue_unsubscribe_token(account_id=7, user_id="gh-7")
        ring(SECRET)

        assert tok.read_unsubscribe_token(old) is None


class TestRefusals:
    @pytest.mark.parametrize(
        "token",
        [
            "",
            "x",
            "gAAAAA",
            "not a token",
            "a/b",
            "a\r\nb",
            "A" * (2049),
        ],
    )
    def test_garbage_is_none(self, ring, token: str) -> None:
        assert tok.read_unsubscribe_token(token) is None

    def test_non_str_is_none(self, ring) -> None:
        assert tok.read_unsubscribe_token(None) is None  # type: ignore[arg-type]
        assert tok.read_unsubscribe_token(b"abc") is None  # type: ignore[arg-type]

    def test_tampered_is_none(self, ring) -> None:
        token = tok.issue_unsubscribe_token(account_id=42, user_id="google-123")
        flipped = token[:-5] + ("A" if token[-5] != "A" else "B") + token[-4:]

        assert tok.read_unsubscribe_token(flipped) is None
        assert tok.read_unsubscribe_token(token[:-1]) is None

    def test_padded_is_none(self, ring) -> None:
        token = tok.issue_unsubscribe_token(account_id=42, user_id="google-123")

        assert tok.read_unsubscribe_token(token + "=") is None

    def test_encryptor_ciphertext_never_opens(self, ring, monkeypatch) -> None:
        from utils.encryption import APIKeyEncryption

        foreign = APIKeyEncryption(SECRET).encrypt(
            json.dumps({"p": "au1", "i": 42, "u": "google-123"})
        )

        assert tok.read_unsubscribe_token(foreign.rstrip("=")) is None

    @pytest.mark.parametrize(
        "payload",
        [
            {"p": "other", "i": 1, "u": "a"},
            {"p": "au1", "i": "1", "u": "a"},
            {"p": "au1", "i": True, "u": "a"},
            {"p": "au1", "i": 0, "u": "a"},
            {"p": "au1", "i": 1, "u": ""},
            {"p": "au1", "i": 1},
            ["au1", 1, "a"],
        ],
    )
    def test_wrong_payload_under_the_right_key_is_none(self, ring, payload) -> None:
        fernet = Fernet(tok._purpose_keys((SECRET,))[0])
        token = fernet.encrypt(json.dumps(payload).encode()).decode().rstrip("=")

        assert tok.read_unsubscribe_token(token) is None

    def test_never_logs_the_token(self, ring, caplog) -> None:
        token = tok.issue_unsubscribe_token(account_id=42, user_id="google-123")
        caplog.set_level(logging.DEBUG)

        tok.read_unsubscribe_token(token[:-3] + "AAA")
        tok.read_unsubscribe_token("garbage-token-value")

        assert token[:-3] not in caplog.text
        assert "garbage-token-value" not in caplog.text


class TestLinks:
    def test_headers(self, ring) -> None:
        headers = tok.build_unsubscribe_headers(account_id=42, user_id="google-123")

        assert set(headers) == {"List-Unsubscribe", "List-Unsubscribe-Post"}
        assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        value = headers["List-Unsubscribe"]
        prefix = "<https://app.example.com/api/v1/announcement-emails/unsubscribe/"
        assert value.startswith(prefix) and value.endswith(">")
        token = value[len(prefix) : -1]
        assert tok.read_unsubscribe_token(token) == tok.UnsubscribeClaim(42, "google-123")

    def test_page_url_carries_the_token_in_a_fragment(self, ring) -> None:
        url = tok.unsubscribe_page_url(account_id=42, user_id="google-123")

        base, _, token = url.partition("#")
        assert base == "https://app.example.com/email/unsubscribe"
        assert "?" not in url
        assert tok.read_unsubscribe_token(token) == tok.UnsubscribeClaim(42, "google-123")
