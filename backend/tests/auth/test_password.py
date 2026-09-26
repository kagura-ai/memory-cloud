"""bcrypt password hashing under bcrypt 5 (Issue #1707).

bcrypt 5 raises ``ValueError`` from ``hashpw`` / ``checkpw`` for passwords
longer than 72 bytes, where bcrypt 4 silently hashed the first 72 bytes.
Pinned here:

- ``verify_password`` compares the first 72 UTF-8 bytes, so a hash made under
  bcrypt 4 from a long password still verifies and an over-long wrong password
  is a plain mismatch (never an exception);
- ``hash_password`` refuses a password over 72 bytes with a clear error
  instead of truncating it;
- passwords of 72 bytes or fewer behave as before.
"""

from __future__ import annotations

import copy
import pickle

import bcrypt
import pytest

from auth.password import (
    PASSWORD_MAX_BYTES,
    PasswordTooLongError,
    hash_password,
    is_password_too_long,
    verify_password,
)


def _bcrypt4_hash(password: str) -> str:
    """What bcrypt 4 stored for ``password``: the hash of its first 72 bytes."""
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=4)).decode()


def test_limit_is_bcrypts_72_bytes() -> None:
    assert PASSWORD_MAX_BYTES == 72


@pytest.mark.skipif(
    int(bcrypt.__version__.split(".")[0]) < 5,
    reason="bcrypt < 5 truncates > 72 bytes silently; verify_password is correct either way",
)
def test_installed_bcrypt_rejects_long_passwords() -> None:
    """Guard the premise: this suite runs against a bcrypt that refuses > 72 bytes.

    Skipped on bcrypt 4 (``pyproject`` still allows it). If a future bcrypt 5+
    truncates again this test fails and the workaround can be revisited; either
    way ``verify_password`` stays correct.
    """
    salt = bcrypt.gensalt(rounds=4)
    with pytest.raises(ValueError):
        bcrypt.hashpw(b"y" * 73, salt)


class TestVerifyPassword:
    def test_long_password_verifies_against_a_bcrypt4_era_hash(self) -> None:
        password = "Aa1!" + "x" * 96  # 100 bytes
        assert verify_password(password, _bcrypt4_hash(password)) is True

    def test_long_wrong_password_is_a_mismatch_not_an_error(self) -> None:
        stored = _bcrypt4_hash("Aa1!" + "x" * 96)
        assert verify_password("Bb2@" + "z" * 96, stored) is False

    def test_long_password_matches_on_its_first_72_bytes(self) -> None:
        """bcrypt 4 semantics: bytes past 72 never took part in the hash."""
        prefix = "p" * 72
        stored = _bcrypt4_hash(prefix + "tail-one")
        assert verify_password(prefix + "a-different-tail", stored) is True

    def test_long_password_against_a_short_hash_is_a_mismatch(self) -> None:
        stored = bcrypt.hashpw(b"Short-Pass-1!", bcrypt.gensalt(rounds=4)).decode()
        assert verify_password("Short-Pass-1!" + "x" * 80, stored) is False

    def test_multibyte_char_straddling_byte_72(self) -> None:
        """71 ASCII bytes + one 3-byte char: bcrypt 4 hashed bytes [0:72]."""
        password = "a" * 71 + "あ"  # 74 bytes, the char spans bytes 71-73
        assert len(password.encode()) == 74
        assert verify_password(password, _bcrypt4_hash(password)) is True

    def test_exactly_72_bytes_round_trips(self) -> None:
        password = "q" * 72
        stored = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()
        assert verify_password(password, stored) is True
        assert verify_password("q" * 71 + "r", stored) is False

    def test_short_password_unchanged(self) -> None:
        stored = bcrypt.hashpw(b"Correct-Horse-1!", bcrypt.gensalt(rounds=4)).decode()
        assert verify_password("Correct-Horse-1!", stored) is True
        assert verify_password("Wrong-Horse-1!", stored) is False


class TestHashPassword:
    def test_over_72_bytes_raises_a_clear_error(self) -> None:
        with pytest.raises(PasswordTooLongError, match="at most 72 bytes"):
            hash_password("x" * 73)

    def test_error_is_a_value_error(self) -> None:
        assert issubclass(PasswordTooLongError, ValueError)

    def test_error_never_echoes_the_password(self) -> None:
        secret = "S3cret!" + "k" * 80
        with pytest.raises(PasswordTooLongError) as exc_info:
            hash_password(secret)
        assert secret not in str(exc_info.value)

    def test_error_survives_pickle_and_copy(self) -> None:
        error = PasswordTooLongError()
        for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error)):
            assert type(clone) is PasswordTooLongError
            assert str(clone) == str(error)

    def test_multibyte_counts_in_bytes_not_characters(self) -> None:
        # 25 characters, 75 bytes
        with pytest.raises(PasswordTooLongError):
            hash_password("あ" * 25)

    def test_72_bytes_hashes_and_verifies(self) -> None:
        password = "Aa1!" + "m" * 68
        stored = hash_password(password)
        assert stored.startswith("$2b$12$")  # production cost unchanged
        assert verify_password(password, stored) is True


class TestIsPasswordTooLong:
    @pytest.mark.parametrize(
        ("password", "expected"),
        [
            ("", False),
            ("x" * 72, False),
            ("x" * 73, True),
            ("あ" * 24, False),  # 72 bytes
            ("あ" * 24 + "x", True),  # 73 bytes
        ],
    )
    def test_measures_utf8_bytes(self, password: str, expected: bool) -> None:
        assert is_password_too_long(password) is expected
