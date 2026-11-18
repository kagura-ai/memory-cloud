"""Versioned examples pin the new read-only contracts without changing observation-v1."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from models.sync_installation_schemas import (
    SyncActivation,
    SyncEstablishedTargetObservation,
    SyncReconnectValidation,
    SyncReconnectValidationView,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    "name,request_model,response_model",
    [
        (
            "sync-established-target-observation-v1",
            SyncActivation,
            SyncEstablishedTargetObservation,
        ),
        ("sync-reconnect-validation-v1", SyncReconnectValidation, SyncReconnectValidationView),
    ],
)
def test_exact_read_only_fixture_shapes(name, request_model, response_model):
    value = json.loads((FIXTURES / (name + ".json")).read_text())
    request = request_model.model_validate(value["request"])
    response = response_model.model_validate(value["response"])
    assert request.application_id == response.application_id
    assert request.authorization_id == response.authorization.authorization_id
    assert response.model_dump(mode="json") == value["response"]
    with pytest.raises(ValidationError):
        request_model.model_validate({**value["request"], "service_client_id": "override"})
    with pytest.raises(ValidationError):
        response_model.model_validate({**value["response"], "owner_override": "foreign"})
    for until in ("2026-11-18T01:00:00Z", "2026-11-18T01:00:31Z"):
        with pytest.raises(ValidationError):
            response_model.model_validate({**value["response"], "valid_until": until})
    for epoch in (True, "1", -1):
        with pytest.raises(ValidationError):
            response_model.model_validate({**value["response"], "ownership_epoch": epoch})
    if "continuity_current" in value["response"]:
        with pytest.raises(ValidationError):
            response_model.model_validate({**value["response"], "continuity_current": 1})
