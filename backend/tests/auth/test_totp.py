"""``verify_totp`` with a code that cannot be UTF-8 encoded (Issue #1718).

pyotp's ``strings_equal`` encodes the code, so a lone surrogate used to raise
``UnicodeEncodeError`` from ``/auth/mfa/verify``. It is a wrong code.
"""

from __future__ import annotations

import pyotp

from auth.totp import generate_totp_secret, verify_totp


def test_current_code_verifies() -> None:
    secret = generate_totp_secret()
    assert verify_totp(secret, pyotp.TOTP(secret).now()) is True


def test_lone_surrogate_code_is_a_wrong_code() -> None:
    secret = generate_totp_secret()
    assert verify_totp(secret, "12345\ud800") is False
    assert verify_totp(secret, pyotp.TOTP(secret).now() + "\udc00") is False
