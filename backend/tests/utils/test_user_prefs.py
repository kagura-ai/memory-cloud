"""Validation of a user's UI language and time zone (#2700).

``is_valid_timezone`` is a set lookup, never ``ZoneInfo(value)``: ZoneInfo
raises ``ValueError`` (not ``ZoneInfoNotFoundError``) for ``""``, absolute
paths and ``..`` segments, so a caller that catches only the latter turns
hostile input into a 500.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import zoneinfo
from pathlib import Path

import pytest

from models.schemas import UpdateUserProfileRequest
from utils.user_prefs import (
    SUPPORTED_LOCALES,
    TIMEZONE_MAX_LENGTH,
    is_valid_locale,
    is_valid_timezone,
    negotiate_locale,
)


class TestLocale:
    def test_frontend_and_backend_offer_the_same_languages(self):
        config = Path(__file__).resolve().parents[3] / "frontend/src/i18n/config.ts"
        if not config.exists():
            pytest.skip("Frontend sources are not present in a backend-only image")
        match = re.search(r"export const locales = (\[[^\]]+\]) as const", config.read_text())
        assert match is not None
        assert tuple(ast.literal_eval(match[1])) == SUPPORTED_LOCALES

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


class TestNegotiateLocale:
    """#2949: the consent page's language. Order: the OIDC ``ui_locales``
    parameter, the account's saved locale, ``Accept-Language``, then ``en``.
    Only a code of :data:`SUPPORTED_LOCALES` ever comes out."""

    def test_nothing_known_answers_english(self) -> None:
        assert negotiate_locale() == "en"
        assert negotiate_locale(ui_locales=None, account_locale=None, accept_language=None) == "en"

    @pytest.mark.parametrize("value", ["ja", "JA", "ja-JP", "Ja-jp", "ja-Hira-JP"])
    def test_ui_locales_primary_subtag_case_insensitive(self, value) -> None:
        assert negotiate_locale(ui_locales=value) == "ja"

    def test_ui_locales_first_supported_tag_in_preference_order_wins(self) -> None:
        assert negotiate_locale(ui_locales="fr ja en") == "ja"
        assert negotiate_locale(ui_locales="fr-CA en-US ja") == "en"
        assert negotiate_locale(ui_locales="  ja  ") == "ja"

    def test_ui_locales_beats_the_account_and_accept_language(self) -> None:
        assert negotiate_locale(ui_locales="en", account_locale="ja", accept_language="ja") == "en"
        assert negotiate_locale(ui_locales="ja", account_locale="en", accept_language="en") == "ja"

    def test_unsupported_ui_locales_falls_to_the_account(self) -> None:
        assert negotiate_locale(ui_locales="fr de", account_locale="ja") == "ja"

    def test_account_locale_beats_accept_language(self) -> None:
        assert negotiate_locale(account_locale="en", accept_language="ja") == "en"
        assert negotiate_locale(account_locale="ja", accept_language="en") == "ja"

    @pytest.mark.parametrize("account", [None, "", "fr", "JA", "ja-JP", "xx"])
    def test_unsupported_account_locale_falls_to_accept_language(self, account) -> None:
        assert negotiate_locale(account_locale=account, accept_language="ja") == "ja"

    @pytest.mark.parametrize(
        "header,expected",
        [
            ("ja", "ja"),
            ("ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7", "ja"),
            ("en-US,en;q=0.9,ja;q=0.8", "en"),
            ("fr;q=1, ja;q=0.5", "ja"),
            ("en;q=0.3, ja;q=0.9", "ja"),
            ("en;q=0.3, JA-jp;q=0.9", "ja"),
            ("ja;q=0, en;q=0.1", "en"),
            ("ja;q=0", "en"),
            ("*", "en"),
            ("fr, de", "en"),
        ],
    )
    def test_accept_language_with_q_values(self, header, expected) -> None:
        assert negotiate_locale(accept_language=header) == expected

    @pytest.mark.parametrize(
        "header",
        ["ja;q=abc", "ja;q=2", "ja;q=-1", "ja;q=nan", "ja;q=", ";;;", ",,,", "j@;q=1"],
    )
    def test_malformed_accept_language_entries_are_skipped(self, header) -> None:
        assert negotiate_locale(accept_language=header) == "en"

    def test_a_malformed_entry_does_not_hide_a_good_one(self) -> None:
        assert negotiate_locale(accept_language="ja;q=abc, ja-JP;q=0.4") == "ja"

    @pytest.mark.parametrize(
        "junk",
        [
            "",
            "   ",
            '"><script>alert(1)</script>',
            "ja<script>",
            "ja-<b>",
            "ja_JP",
            "ja;q=1",
            "ja,en",
            "../ja",
            "ja\x00",
            "j" * 9,
            "ja-" + "x" * 9,
        ],
    )
    def test_junk_ui_locales_is_skipped_and_the_next_tier_answers(self, junk) -> None:
        assert negotiate_locale(ui_locales=junk) == "en"
        assert negotiate_locale(ui_locales=junk, account_locale="ja") == "ja"

    def test_oversized_ui_locales_is_skipped_whole(self) -> None:
        oversized = "fr " * 100 + "ja"
        assert negotiate_locale(ui_locales=oversized) == "en"
        assert negotiate_locale(ui_locales=oversized, account_locale="ja") == "ja"

    def test_oversized_accept_language_is_skipped_whole(self) -> None:
        oversized = "fr;q=0.1, " * 200 + "ja"
        assert negotiate_locale(accept_language=oversized) == "en"

    @pytest.mark.parametrize(
        "value",
        ["ja", "EN", "fr", "<x>", "ja-JP", "*", "ja;q=0.5", "x" * 5000, "\u3042", "-ja", "ja-"],
    )
    def test_only_a_supported_code_ever_comes_out(self, value) -> None:
        for kwargs in (
            {"ui_locales": value},
            {"account_locale": value},
            {"accept_language": value},
        ):
            assert negotiate_locale(**kwargs) in SUPPORTED_LOCALES


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
