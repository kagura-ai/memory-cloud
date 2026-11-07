"""Validation of a user's UI language and time zone (#2700).

``is_valid_timezone`` is a set lookup, never ``ZoneInfo(value)``: ZoneInfo
raises ``ValueError`` (not ``ZoneInfoNotFoundError``) for ``""``, absolute
paths and ``..`` segments, so a caller that catches only the latter turns
hostile input into a 500.
"""

from __future__ import annotations

import zoneinfo

import pytest

from models.schemas import UpdateUserProfileRequest
from utils.user_prefs import (
    SUPPORTED_LOCALES,
    TIMEZONE_MAX_LENGTH,
    is_valid_locale,
    is_valid_timezone,
)


class TestLocale:
    @pytest.mark.parametrize("value", ["en", "ja"])
    def test_supported(self, value) -> None:
        assert is_valid_locale(value) is True

    @pytest.mark.parametrize(
        "value", [None, "", "EN", "xx", "fr", "ja; x", "ja ", "../etc/passwd", 1, ["ja"]]
    )
    def test_anything_else(self, value) -> None:
        assert is_valid_locale(value) is False

    def test_matches_the_profile_schema_pattern(self) -> None:
        # One list of languages for the sign-up carrier and PUT /users/profile.
        pattern = UpdateUserProfileRequest.model_json_schema()["properties"]["locale"]
        assert "^(en|ja)$" in str(pattern)
        assert SUPPORTED_LOCALES == ("en", "ja")


class TestTimezone:
    @pytest.mark.parametrize("value", ["UTC", "Asia/Tokyo", "America/New_York", "Asia/Calcutta"])
    def test_iana_names(self, value) -> None:
        assert is_valid_timezone(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "Nope/Zone",
            "asia/tokyo",
            "Asia/Tokyo; x",
            "/etc/passwd",
            "../x",
            "../etc/passwd",
            "Asia/../Asia/Tokyo",
            "America/" + "A" * (TIMEZONE_MAX_LENGTH - len("America/") + 1),
            123,
        ],
    )
    def test_anything_else(self, value) -> None:
        assert is_valid_timezone(value) is False

    @pytest.mark.parametrize("value", ["", "/etc/passwd", "../x"])
    def test_inputs_zoneinfo_rejects_with_value_error(self, value) -> None:
        # Why the lookup is not ZoneInfo(): these raise ValueError, which a
        # caller catching ZoneInfoNotFoundError lets escape as a 500.
        with pytest.raises(ValueError) as exc_info:
            zoneinfo.ZoneInfo(value)
        assert not isinstance(exc_info.value, zoneinfo.ZoneInfoNotFoundError)
        assert is_valid_timezone(value) is False

    def test_the_length_cap_is_the_column_width(self) -> None:
        from models.auth import User

        assert User.__table__.c.timezone.type.length == TIMEZONE_MAX_LENGTH
