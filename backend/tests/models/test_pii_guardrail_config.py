"""Tests for the PiiGuardrailConfig schema (#866, F6-d follow-up).

Validates the connector pii_guardrail_config contract documented in
docs/pii-guardrail-consumption-contract.md at the provision path.
"""

import pytest
from pydantic import ValidationError

from models.schemas import PiiGuardrailConfig, validate_pii_guardrail_config


def test_accepts_a_valid_enabled_config():
    cfg = PiiGuardrailConfig(
        enabled=True,
        detectors=["EMAIL_ADDRESS", "PHONE_NUMBER"],
        redaction="mask",
        locale="en",
        fail_closed=True,
    )
    assert cfg.enabled is True
    assert cfg.detectors == ["EMAIL_ADDRESS", "PHONE_NUMBER"]
    assert cfg.redaction == "mask"


def test_accepts_disabled_opt_out_config():
    # {"enabled": false} is the explicit opt-out — detectors not required.
    cfg = PiiGuardrailConfig(enabled=False)
    assert cfg.enabled is False


def test_rejects_unknown_key():
    # extra="forbid" — a typo'd key (detector vs detectors) must fail loudly.
    with pytest.raises(ValidationError):
        PiiGuardrailConfig(enabled=True, detector=["EMAIL_ADDRESS"])


def test_rejects_missing_detectors_when_enabled():
    with pytest.raises(ValidationError):
        PiiGuardrailConfig(enabled=True, detectors=[])


def test_rejects_invalid_redaction_enum():
    with pytest.raises(ValidationError):
        PiiGuardrailConfig(enabled=True, detectors=["EMAIL_ADDRESS"], redaction="scramble")


# --- shared validation helper (used at both the REST and MCP provision call-sites) ---


def test_validate_helper_returns_none_for_none():
    # null (unconfigured) is accepted at provision; the worker enforces fail-closed at ingest.
    assert validate_pii_guardrail_config(None) is None


def test_validate_helper_returns_normalized_dict_for_valid_input():
    out = validate_pii_guardrail_config({"enabled": True, "detectors": ["EMAIL_ADDRESS"]})
    assert isinstance(out, dict)
    assert out["enabled"] is True
    assert out["detectors"] == ["EMAIL_ADDRESS"]
    assert out["redaction"] == "mask"  # defaults are materialized


def test_validate_helper_raises_valueerror_on_unknown_key():
    with pytest.raises(ValueError):
        validate_pii_guardrail_config({"enabled": True, "detector": ["EMAIL_ADDRESS"]})


def test_validate_helper_error_message_does_not_echo_input_values():
    # Secret-leak guard: pydantic ValidationError echoes offending input by default.
    # The helper must surface only field locations, never the offending value.
    sensitive = "SENSITIVE-VALUE-DO-NOT-ECHO-123"
    with pytest.raises(ValueError) as exc:
        validate_pii_guardrail_config(
            {"enabled": True, "detectors": ["EMAIL_ADDRESS"], "leaky_key": sensitive}
        )
    assert sensitive not in str(exc.value)
