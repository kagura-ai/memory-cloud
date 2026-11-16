"""Optional complete preconditions never silently degrade into an unguarded write."""

import pytest
from pydantic import ValidationError

from models.sync_feed_schemas import SCHEMA_HASH, FeedConfig


def expected(**extra):
    return {
        "config_version": 1,
        "generation": 1,
        "schema_hash": SCHEMA_HASH,
        "schema_version": 1,
        **extra,
    }


def test_complete_preconditions_and_legacy_omission_have_distinct_wire_shapes():
    legacy = FeedConfig.model_validate({"kind": "product", "config_hash": "a" * 64})
    assert legacy.expected is None
    for snapshot in (expected(), expected(config_version=0, schema_hash=None, schema_version=None)):
        body = FeedConfig.model_validate(
            {"kind": "product", "config_hash": "a" * 64, "expected": snapshot}
        )
        assert body.expected.model_dump() == snapshot


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        {},
        {"config_version": 1},
        expected(config_version=-1),
        expected(config_version=2**31),
        expected(config_version=True),
        expected(config_version=1.0),
        expected(generation=0),
        expected(generation=2**31),
        expected(schema_version=0),
        expected(schema_version=2**31),
        expected(schema_hash=None),
        expected(schema_version=None),
        expected(schema_hash="other"),
        expected(source_url="https://private.example.test/feed"),
    ],
)
def test_incomplete_null_coerced_or_unbounded_precondition_is_not_legacy(snapshot):
    with pytest.raises(ValidationError):
        FeedConfig.model_validate(
            {"kind": "product", "config_hash": "a" * 64, "expected": snapshot}
        )


def test_openapi_optional_precondition_is_a_complete_nonnullable_object():
    schema = FeedConfig.model_json_schema()
    assert "expected" not in schema["required"]
    field = schema["properties"]["expected"]
    assert field["$ref"] == "#/$defs/FeedConfigExpected"
    assert "anyOf" not in field and "default" not in field
    assert set(schema["$defs"]["FeedConfigExpected"]["required"]) == {
        "config_version",
        "generation",
        "schema_hash",
        "schema_version",
    }
