"""Freeze the narrow lifecycle response rather than accepting data authority."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.sync_mode_access_schemas import SyncModeAccess


def wire():
    return {
        "contract_version": "sync-mode-access-v1",
        "subject": "current-human",
        "target": {
            **{
                key: str(uuid4())
                for key in (
                    "workspace_id",
                    "installation_id",
                    "resource_pk",
                    "context_id",
                    "endpoint_id",
                )
            },
            "generation": 1,
            "target_available": True,
        },
        "management": {
            "mode": "feed",
            "epoch": 1,
            "generation": 1,
            "stop_event_id": 0,
            "reason": None,
        },
        "permissions": {"read": True, "stop": True, "finalize": True},
    }


def test_complete_profile_roundtrips_without_added_authority():
    value = wire()
    assert SyncModeAccess.model_validate_json(json.dumps(value)).model_dump(mode="json") == value


@pytest.mark.parametrize("path", [("contract_version",), ("permissions", "read")])
def test_contract_marker_and_metadata_permission_are_required(path):
    value = wire()
    target = value if len(path) == 1 else value[path[0]]
    del target[path[-1]]
    with pytest.raises(ValidationError):
        SyncModeAccess.model_validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "section,key,value",
    [
        (None, "contract_version", "unknown"),
        (None, "subject", ""),
        (None, "access_token", "never-admit-credentials"),
        ("target", "public_path", "/never-admit-endpoint"),
        ("target", "generation", True),
        ("target", "generation", 0),
        ("target", "generation", 2**53),
        ("target", "target_available", 1),
        ("management", "generation", 2),
        ("management", "epoch", True),
        ("management", "epoch", 2**53),
        ("management", "stop_event_id", -1),
        ("management", "stop_event_id", 2**53),
        ("management", "mode", "resume"),
        ("management", "records", []),
        ("management", "reason", "x" * 129),
        ("permissions", "read", False),
        ("permissions", "stop", 1),
        ("permissions", "resume", True),
        ("permissions", "sync.manage", True),
    ],
)
def test_profile_rejects_ambiguous_or_expanded_contract(section, key, value):
    body = wire()
    (body if section is None else body[section])[key] = value
    with pytest.raises(ValidationError):
        SyncModeAccess.model_validate_json(json.dumps(body))
