"""The result-only wire contract cannot resemble renewed control authority."""

import json
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from api.routes import sync_management
from models.sync_resume_receipt_schemas import SyncResumeReceiptCommand, SyncResumeReceiptView


def body():
    return {
        "operation_id": str(uuid4()),
        "preview_id": str(uuid4()),
        "diff_hash": "a" * 64,
        "original_subject": "original-human",
    }


def view(result="not_found"):
    return {
        **body(),
        "workspace_id": str(uuid4()),
        "installation_id": str(uuid4()),
        "contract_version": "sync-resume-receipt-v1",
        "result": result,
        "receipt": None,
        "as_of": "2026-11-17T00:00:00Z",
    }


@pytest.mark.parametrize("result", ["found", "not_found", "mismatch"])
def test_strict_versioned_outcome_roundtrip(result):
    value = view(result)
    if result == "found":
        value["receipt"] = {
            "mode": "feed",
            "epoch": 3,
            "generation": 3,
            "stop_event_id": 0,
            "reason": None,
        }
    assert (
        SyncResumeReceiptView.model_validate_json(json.dumps(value)).model_dump(mode="json")
        == value
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("original_subject", ""),
        ("original_subject", "x" * 256),
        ("original_subject", "human\nother"),
        ("original_subject", True),
        ("diff_hash", "A" * 64),
        ("diff_hash", "a" * 63),
        ("operation_id", "not-a-uuid"),
        ("preview_id", 23),
        ("authority", "never-accepted"),
        ("expected", {}),
    ],
)
def test_request_rejects_coercion_unknown_authority_and_unbounded_identifiers(field, value):
    with pytest.raises(ValidationError):
        SyncResumeReceiptCommand.model_validate_json(json.dumps({**body(), field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", "sync-resume-receipt-v2"),
        ("result", "unknown"),
        ("generation", 3),
        ("token", "never-accepted"),
        ("as_of", "invalid"),
    ],
)
def test_response_rejects_unknown_or_ambiguous_contract(field, value):
    with pytest.raises(ValidationError):
        SyncResumeReceiptView.model_validate_json(json.dumps({**view(), field: value}))


@pytest.mark.parametrize("result", ["found", "not_found", "mismatch"])
def test_receipt_presence_is_coupled_to_result(result):
    value = view(result)
    if result != "found":
        value["receipt"] = {
            "mode": "feed",
            "epoch": 3,
            "generation": 3,
            "stop_event_id": 0,
            "reason": None,
        }
    with pytest.raises(ValidationError):
        SyncResumeReceiptView.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", "manual"),
        ("generation", True),
        ("generation", 0),
        ("generation", 2**53),
        ("epoch", "3"),
        ("stop_event_id", -1),
        ("reason", "unrelated-private-state"),
        ("content", "never-exposed"),
    ],
)
def test_found_receipt_cannot_leak_extra_data_or_nonresume_state(field, value):
    value = {
        **view("found"),
        "receipt": {
            "mode": "feed",
            "epoch": 3,
            "generation": 3,
            "stop_event_id": 0,
            "reason": None,
            field: value,
        },
    }
    with pytest.raises(ValidationError):
        SyncResumeReceiptView.model_validate_json(json.dumps(value))


def test_naive_database_utc_is_serialized_unambiguously():
    value = SyncResumeReceiptView.model_validate_json(json.dumps(view()))
    value.as_of = datetime(2026, 11, 17, 0, 0, 0)
    assert value.model_dump(mode="json")["as_of"] == "2026-11-17T00:00:00Z"


def test_actual_openapi_response_preserves_version_enum_identifiers_and_receipt_type():
    app = FastAPI()
    app.include_router(sync_management.router, prefix="/api/v1")
    schema = app.openapi()
    path = "/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/management/resume/receipt"
    response = schema["paths"][path]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    definition = schema["components"]["schemas"][response["$ref"].rsplit("/", 1)[1]]
    properties = definition["properties"]
    assert properties["contract_version"]["const"] == "sync-resume-receipt-v1"
    assert properties["result"]["enum"] == ["found", "not_found", "mismatch"]
    for key in ("workspace_id", "installation_id", "operation_id", "preview_id"):
        assert properties[key]["format"] == "uuid"
    assert properties["diff_hash"]["pattern"] == "^[0-9a-f]{64}$"
    assert properties["as_of"]["type"] == "string"
    assert properties["as_of"]["format"] == "date-time"
    assert properties["receipt"]["anyOf"] == [
        {"$ref": "#/components/schemas/SyncResumeModeReceipt"},
        {"type": "null"},
    ]
    assert set(definition["required"]) == set(properties)
