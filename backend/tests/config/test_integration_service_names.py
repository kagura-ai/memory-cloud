"""``INTEGRATION_SERVICE_DISPLAY_NAMES``: parsing, start-up validation and the
collapse by display name (#3010)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from config.integration_service_names import (
    DisplayedService,
    display_services,
    parse_integration_service_display_names,
)
from config.settings import Settings

D1 = datetime(2026, 12, 1, tzinfo=UTC)
D2 = datetime(2026, 12, 20, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["", "  ", "{}"])
def test_empty_maps_nothing(raw: str) -> None:
    assert parse_integration_service_display_names(raw) == {}


def test_a_valid_object_is_parsed_and_stripped() -> None:
    raw = '{"example-sync": "  Example Service ", "example-sync-eu": "Example Service"}'
    assert parse_integration_service_display_names(raw) == {
        "example-sync": "Example Service",
        "example-sync-eu": "Example Service",
    }


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '["Example Service"]',
        '"Example Service"',
        '{"example-sync": 1}',
        '{"example-sync": null}',
        '{"example-sync": "   "}',
        '{"example-sync": ""}',
        '{"example-sync": "' + "x" * 61 + '"}',
        '{"example-sync": "Example\\u0007Service"}',
        '{"example-sync": "Example\\nService"}',
    ],
)
def test_a_malformed_value_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match="INTEGRATION_SERVICE_DISPLAY_NAMES"):
        parse_integration_service_display_names(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"": "Example Service"}', "1-128 characters"),
        ('{"   ": "Example Service"}', "1-128 characters"),
        ('{"' + "k" * 129 + '": "Example Service"}', "1-128 characters"),
        ('{"example\\u0007sync": "Example Service"}', "control character"),
        ('{" example-sync": "Example Service"}', "surrounding whitespace"),
        ('{"example-sync ": "Example Service"}', "surrounding whitespace"),
        ('{"example-sync\\t": "Example Service"}', "control character"),
    ],
)
def test_a_malformed_key_is_refused(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_integration_service_display_names(raw)


def test_a_128_character_key_is_allowed() -> None:
    raw = '{"' + "k" * 128 + '": "Example Service"}'
    assert parse_integration_service_display_names(raw) == {"k" * 128: "Example Service"}


def test_sixty_characters_are_allowed() -> None:
    raw = '{"example-sync": "' + "x" * 60 + '"}'
    assert parse_integration_service_display_names(raw) == {"example-sync": "x" * 60}


def test_settings_refuse_to_start_on_a_malformed_value() -> None:
    with pytest.raises(ValidationError, match="INTEGRATION_SERVICE_DISPLAY_NAMES"):
        Settings(integration_service_display_names='{"example-sync": ""}')


def test_settings_default_is_empty() -> None:
    settings = Settings()
    assert settings.integration_service_display_names == ""
    assert settings.integration_service_display_name_map == {}


def test_same_name_collapses_to_the_latest_end_sorted_by_name() -> None:
    names = {"example-sync": "Example Service", "example-sync-eu": "Example Service"}
    result = display_services(
        [("zeta-sync", D1), ("example-sync", D1), ("example-sync-eu", D2), ("alpha-sync", D2)],
        names,
    )
    assert result == (
        DisplayedService("Example Service", D2),
        DisplayedService("alpha-sync", D2),
        DisplayedService("zeta-sync", D1),
    )


def test_nothing_in_force_is_an_empty_tuple() -> None:
    assert display_services([], {"example-sync": "Example Service"}) == ()
