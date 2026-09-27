"""Shared password policy (Issue #1678).

One rule set for the admin CLIs and the self-service password endpoints:
at least 12 characters, an upper-case letter, a lower-case letter, a digit, a
special character, valid Unicode text and at most bcrypt's 72 bytes.
"""

from __future__ import annotations

import pytest

from auth.password import PASSWORD_NOT_ENCODABLE_MESSAGE, PASSWORD_TOO_LONG_MESSAGE
from auth.password_policy import (
    PASSWORD_MIN_LENGTH,
    PASSWORD_REQUIREMENT_LINES,
    PasswordPolicyError,
    missing_password_requirements,
    validate_password_policy,
)

VALID = "Valid-Pass-123!"


class TestMissingRequirements:
    def test_a_valid_password_misses_nothing(self) -> None:
        assert missing_password_requirements(VALID) == []

    @pytest.mark.parametrize(
        ("password", "missing"),
        [
            ("Va1!", "at least 12 characters"),
            ("valid-pass-123!", "1 uppercase letter"),
            ("VALID-PASS-123!", "1 lowercase letter"),
            ("Valid-Pass-abc!", "1 digit"),
            ("ValidPass1234a", "1 special character"),
        ],
    )
    def test_each_rule(self, password: str, missing: str) -> None:
        assert missing_password_requirements(password) == [missing]

    def test_minimum_length_is_twelve(self) -> None:
        assert PASSWORD_MIN_LENGTH == 12
        assert any("12" in line for line in PASSWORD_REQUIREMENT_LINES)


class TestValidate:
    def test_accepts_a_valid_password(self) -> None:
        validate_password_policy(VALID)

    def test_lists_every_missing_rule(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_policy("qqq")
        message = str(exc_info.value)
        assert "at least 12 characters" in message
        assert "1 uppercase letter" in message
        # The message never echoes the password.
        assert "qqq" not in message

    def test_refuses_more_than_72_bytes(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_policy("Aa1!" + "x" * 96)
        assert str(exc_info.value) == PASSWORD_TOO_LONG_MESSAGE

    def test_refuses_unencodable_text(self) -> None:
        with pytest.raises(PasswordPolicyError) as exc_info:
            validate_password_policy("Valid-Pass-123\udcff")
        assert str(exc_info.value) == PASSWORD_NOT_ENCODABLE_MESSAGE

    def test_is_a_value_error(self) -> None:
        assert issubclass(PasswordPolicyError, ValueError)
