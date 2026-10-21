"""The consent form's CSRF token is a keyed hash of the browser session (#2199).

Stateless: the token is recomputed from the session id on every POST, so
every tab of one session shares it and a new sign-in (new session id)
invalidates an open consent page. The validator is type-strict and fails
closed, so a test double or a missing value never reaches the HMAC.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from auth.consent_csrf import consent_csrf_token, consent_csrf_valid
from config.settings import get_settings


def test_same_session_gives_the_same_token() -> None:
    assert consent_csrf_token("sid-a") == consent_csrf_token("sid-a")


def test_sessions_get_different_tokens() -> None:
    assert consent_csrf_token("sid-a") != consent_csrf_token("sid-b")


def test_token_is_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    before = consent_csrf_token("sid-a")
    monkeypatch.setattr(get_settings(), "audit_hmac_key", "another-key")
    assert consent_csrf_token("sid-a") != before


def test_token_is_not_the_session_id() -> None:
    token = consent_csrf_token("sid-a")
    assert "sid-a" not in token
    assert len(token) == 64


def test_matching_token_is_valid() -> None:
    assert consent_csrf_valid("sid-a", consent_csrf_token("sid-a")) is True


@pytest.mark.parametrize(
    "presented",
    [None, "", "wrong", "0" * 64, "0" * 63, consent_csrf_token("sid-b"), MagicMock(), 123],
)
def test_other_presented_values_are_invalid(presented: object) -> None:
    assert consent_csrf_valid("sid-a", presented) is False


@pytest.mark.parametrize("session_id", [None, "", MagicMock(), 7])
def test_no_usable_session_id_is_invalid(session_id: object) -> None:
    assert consent_csrf_valid(session_id, consent_csrf_token("sid-a")) is False
