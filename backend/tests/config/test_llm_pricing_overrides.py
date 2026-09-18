"""``LLM_PRICING_OVERRIDES`` parsing + Settings boot-time validation (#1570)."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from config.llm_pricing_overrides import PricingOverride, parse_llm_pricing_overrides
from config.settings import Settings

_ENTRY = {
    "provider": "self_hosted",
    "model": "qwen3-embedding:4b",
    "unit_type": "embedding_tokens",
    "price_per_unit": 0.02,
}


class TestParse:
    def test_empty_means_no_overrides(self):
        assert parse_llm_pricing_overrides("") == ()
        assert parse_llm_pricing_overrides("   ") == ()
        assert parse_llm_pricing_overrides(None) == ()
        assert parse_llm_pricing_overrides("[]") == ()

    def test_minimal_entry_gets_defaults(self):
        (override,) = parse_llm_pricing_overrides(json.dumps([_ENTRY]))
        assert override == PricingOverride(
            provider="self_hosted",
            model="qwen3-embedding:4b",
            unit_type="embedding_tokens",
            price_per_unit=Decimal("0.02"),
            unit_denominator=1_000_000,
            context_min_tokens=0,
        )

    def test_price_is_decimal_via_str_not_binary_float(self):
        # ``Decimal(0.1)`` would be 0.1000000000000000055…; the parser must go
        # through ``str`` so the stored price is exactly what the operator typed.
        (override,) = parse_llm_pricing_overrides(json.dumps([{**_ENTRY, "price_per_unit": 0.1}]))
        assert override.price_per_unit == Decimal("0.1")

    def test_string_price_and_optional_fields(self):
        raw = json.dumps(
            [
                {
                    **_ENTRY,
                    "price_per_unit": "1.5",
                    "unit_denominator": 1000,
                    "context_min_tokens": 200_000,
                }
            ]
        )
        (override,) = parse_llm_pricing_overrides(raw)
        assert override.price_per_unit == Decimal("1.5")
        assert override.unit_denominator == 1000
        assert override.context_min_tokens == 200_000

    def test_zero_price_is_allowed(self):
        # A truly free local model is expressed as an explicit $0 (D3).
        (override,) = parse_llm_pricing_overrides(json.dumps([{**_ENTRY, "price_per_unit": 0}]))
        assert override.price_per_unit == Decimal("0")

    def test_whitespace_around_names_is_stripped(self):
        (override,) = parse_llm_pricing_overrides(
            json.dumps([{**_ENTRY, "provider": " self_hosted ", "model": " m "}])
        )
        assert (override.provider, override.model) == ("self_hosted", "m")

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            '{"provider": "x"}',  # object, not array
            "42",
            json.dumps(["a string"]),  # element is not an object
        ],
    )
    def test_malformed_shape_raises_naming_env_var(self, raw):
        with pytest.raises(ValueError, match="LLM_PRICING_OVERRIDES"):
            parse_llm_pricing_overrides(raw)

    @pytest.mark.parametrize(
        ("patch", "needle"),
        [
            ({"provider": ""}, "provider"),
            ({"model": "   "}, "model"),
            ({"unit_type": "tokens"}, "unit_type"),
            ({"price_per_unit": -0.01}, "price_per_unit"),
            ({"price_per_unit": "abc"}, "price_per_unit"),
            ({"price_per_unit": True}, "price_per_unit"),
            ({"unit_denominator": 0}, "unit_denominator"),
            ({"unit_denominator": 1.5}, "unit_denominator"),
            ({"context_min_tokens": -1}, "context_min_tokens"),
            ({"currency": "EUR"}, "currency"),  # unknown key
        ],
    )
    def test_bad_entry_names_env_var_index_and_field(self, patch, needle):
        raw = json.dumps([_ENTRY, {**_ENTRY, **patch}])
        with pytest.raises(ValueError, match="LLM_PRICING_OVERRIDES") as exc_info:
            parse_llm_pricing_overrides(raw)
        message = str(exc_info.value)
        assert "entry 1" in message
        assert needle in message

    def test_missing_required_key_is_an_error(self):
        entry = dict(_ENTRY)
        del entry["unit_type"]
        with pytest.raises(ValueError, match="LLM_PRICING_OVERRIDES.*entry 0.*unit_type"):
            parse_llm_pricing_overrides(json.dumps([entry]))

    def test_duplicate_lookup_key_is_an_error(self):
        raw = json.dumps([_ENTRY, {**_ENTRY, "price_per_unit": 0.05}])
        with pytest.raises(ValueError, match="LLM_PRICING_OVERRIDES.*entry 1.*duplicate"):
            parse_llm_pricing_overrides(raw)

    def test_same_key_different_tier_is_not_a_duplicate(self):
        raw = json.dumps([_ENTRY, {**_ENTRY, "context_min_tokens": 200_000}])
        assert len(parse_llm_pricing_overrides(raw)) == 2


class TestSettingsValidator:
    def test_default_is_empty(self):
        settings = Settings(_env_file=None)
        assert settings.llm_pricing_overrides == ""
        assert settings.parsed_llm_pricing_overrides == ()

    def test_valid_value_is_parsed(self):
        settings = Settings(_env_file=None, llm_pricing_overrides=json.dumps([_ENTRY]))
        (override,) = settings.parsed_llm_pricing_overrides
        assert override.model == "qwen3-embedding:4b"
        assert override.price_per_unit == Decimal("0.02")

    def test_malformed_value_refuses_to_boot(self):
        with pytest.raises(ValueError, match="LLM_PRICING_OVERRIDES"):
            Settings(_env_file=None, llm_pricing_overrides='[{"provider": "x"}]')
