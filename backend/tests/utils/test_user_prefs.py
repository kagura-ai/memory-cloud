"""Validation of a user's UI language and time zone (#2700).

``is_valid_timezone`` is a set lookup, never ``ZoneInfo(value)``: ZoneInfo
raises ``ValueError`` (not ``ZoneInfoNotFoundError``) for ``""``, absolute
paths and ``..`` segments, so a caller that catches only the latter turns
hostile input into a 500.
"""

from __future__ import annotations

import importlib.util
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


class TestTzdataRuntimeDependency:
    """``tzdata`` is a runtime dependency (#2700): zoneinfo falls back to it for
    names the system tz database lacks, and Debian 13 slim images (the backend
    base) ship none of the legacy aliases browsers still report. Without the
    package those users' time zone is dropped to UTC."""

    def test_tzdata_is_installed_so_legacy_browser_names_validate(self) -> None:
        assert importlib.util.find_spec("tzdata") is not None

    @pytest.mark.parametrize(
        "name",
        ["Asia/Calcutta", "Europe/Kiev", "Asia/Saigon", "America/Buenos_Aires", "US/Eastern"],
    )
    def test_legacy_alias_a_browser_reports_validates(self, name) -> None:
        assert is_valid_timezone(name) is True
        zoneinfo.ZoneInfo(name)  # and loads
