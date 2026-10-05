"""No integer PK leaks through the REST schema (#1008, #1813).

Fails if a response model of API keys, share keys, resource tokens,
workspace connectors' token, workspace invitations or member credential keys
exposes an integer ``id`` / ``key_id`` / ``token_id`` again, or if one of the
routes addressing them takes an integer path id; or if the user profile,
OAuth client or system-admin responses regain their integer ``id`` /
``initial_admin_id`` (#1813), or the admin user-stats payload its integer
``id`` (#1882).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from api.main import app
from api.routes import admin, api_keys, oauth, resource_tokens, share_keys, workspace_connectors
from models import schemas
from utils.public_id import PublicIdPrefix, public_id_pattern

# (component schema, id field, prefix)
PUBLIC_ID_FIELDS = [
    ("APIKeyResponse", "id", PublicIdPrefix.API_KEY),
    ("APIKeyCreateResponse", "id", PublicIdPrefix.API_KEY),
    ("MemberAPIKeyResponse", "id", PublicIdPrefix.API_KEY),
    ("RegenerateAPIKeyResponse", "key_id", PublicIdPrefix.API_KEY),
    ("ShareKeyResponse", "id", PublicIdPrefix.SHARE_KEY),
    ("ShareKeyCreateResponse", "id", PublicIdPrefix.SHARE_KEY),
    ("ResourceTokenResponse", "id", PublicIdPrefix.RESOURCE_TOKEN),
    ("ResourceTokenCreateResponse", "id", PublicIdPrefix.RESOURCE_TOKEN),
    ("WorkspaceConnectorCreateResponse", "token_id", PublicIdPrefix.RESOURCE_TOKEN),
    ("WorkspaceInvitationResponse", "id", PublicIdPrefix.INVITATION),
    ("PendingInvitationItem", "id", PublicIdPrefix.INVITATION),
]

# (path, method, parameter, prefix)
PUBLIC_ID_PATH_PARAMS = [
    ("/api/v1/config/api-keys/{key_id}", "delete", "key_id", PublicIdPrefix.API_KEY),
    ("/api/v1/config/api-keys/{key_id}/revoke", "post", "key_id", PublicIdPrefix.API_KEY),
    ("/api/v1/config/api-keys/{key_id}/regenerate", "post", "key_id", PublicIdPrefix.API_KEY),
    ("/api/v1/config/api-keys/{key_id}/stats", "get", "key_id", PublicIdPrefix.API_KEY),
    ("/api/v1/config/share-keys/{key_id}/revoke", "post", "key_id", PublicIdPrefix.SHARE_KEY),
    ("/api/v1/resource-tokens/{token_id}", "patch", "token_id", PublicIdPrefix.RESOURCE_TOKEN),
    ("/api/v1/resource-tokens/{token_id}", "delete", "token_id", PublicIdPrefix.RESOURCE_TOKEN),
    (
        "/api/v1/workspaces/{workspace_id}/invitations/{invitation_id}",
        "delete",
        "invitation_id",
        PublicIdPrefix.INVITATION,
    ),
    (
        "/api/v1/workspaces/{workspace_id}/members/{user_id}/credentials/api-keys/{key_id}",
        "delete",
        "key_id",
        PublicIdPrefix.API_KEY,
    ),
]


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    return app.openapi()


_MODEL_MODULES = (
    api_keys,
    share_keys,
    resource_tokens,
    workspace_connectors,
    oauth,
    admin,
    schemas,
)


def _model(name: str) -> type[BaseModel]:
    for module in _MODEL_MODULES:
        model = getattr(module, name, None)
        if model is not None:
            return model
    raise LookupError(name)


def _declares_integer(prop: dict[str, Any]) -> bool:
    """True if any variant of a JSON-schema property is an integer.

    A nullable field is emitted as ``anyOf: [{type: ...}, {type: null}]``
    with no top-level ``type``, so reading ``type`` alone lets ``int | None``
    through (#1882).
    """
    declared = prop.get("type")
    if declared == "integer" or (isinstance(declared, list) and "integer" in declared):
        return True
    return any(
        _declares_integer(variant)
        for keyword in ("anyOf", "oneOf", "allOf")
        for variant in prop.get(keyword, ())
    )


def _integer_id_fields(properties: dict[str, dict[str, Any]]) -> list[str]:
    """Names of the ``id`` / ``*_id`` properties that can hold an integer."""
    return sorted(
        name
        for name, prop in properties.items()
        if (name == "id" or name.endswith("_id")) and _declares_integer(prop)
    )


@pytest.mark.parametrize(("schema", "field", "prefix"), PUBLIC_ID_FIELDS)
def test_response_ids_are_prefixed_strings(schema: str, field: str, prefix: PublicIdPrefix) -> None:
    # The validation-mode schema: TZAwareBaseModel's wildcard wrap serializer
    # leaves the serialization-mode (OpenAPI response) properties untyped.
    prop = _model(schema).model_json_schema(mode="validation")["properties"][field]
    assert prop.get("type") == "string", (schema, field, prop)
    assert prop.get("pattern") == public_id_pattern(prefix), (schema, field, prop)


@pytest.mark.parametrize(("schema", "field", "prefix"), PUBLIC_ID_FIELDS)
def test_openapi_response_ids_are_not_integers(
    openapi: dict[str, Any], schema: str, field: str, prefix: PublicIdPrefix
) -> None:
    prop = openapi["components"]["schemas"][schema]["properties"][field]
    assert not _declares_integer(prop), (schema, field, prop)
    if "type" in prop:
        assert prop.get("pattern") == public_id_pattern(prefix), (schema, field, prop)


def test_external_key_response_has_no_id(openapi: dict[str, Any]) -> None:
    # External keys are addressed by key_name; the integer PK was dropped.
    assert "id" not in openapi["components"]["schemas"]["ExternalKeyResponse"]["properties"]


@pytest.mark.parametrize(("path", "method", "param", "prefix"), PUBLIC_ID_PATH_PARAMS)
def test_path_ids_are_prefixed_strings(
    openapi: dict[str, Any], path: str, method: str, param: str, prefix: PublicIdPrefix
) -> None:
    params = openapi["paths"][path][method]["parameters"]
    (spec,) = [p for p in params if p["name"] == param and p["in"] == "path"]
    assert spec["schema"].get("type") == "string", spec
    assert spec["schema"].get("pattern") == public_id_pattern(prefix), spec


def test_no_integer_id_left_on_these_schemas(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]
    names = {s for s, _, _ in PUBLIC_ID_FIELDS} | {"ExternalKeyResponse"}
    for name in names:
        for field in ("id", "key_id", "token_id", "invitation_id"):
            prop = schemas[name]["properties"].get(field)
            if prop is not None:
                assert not _declares_integer(prop), (name, field, prop)


# Responses addressed by a string id the client already has (users by
# ``user_id``, OAuth clients by ``client_id``): the integer PK was dropped
# outright (#1813).
NO_INTEGER_ID_SCHEMAS = [
    ("UserProfileResponse", "id"),
    ("OAuth2ClientResponse", "id"),
    ("OAuth2ClientWithSecretResponse", "id"),
    ("UserWithAdminFlag", "id"),
    ("SystemAdminListResponse", "initial_admin_id"),
    # #1882: ``UserStats.user`` was an untyped dict carrying the integer PK.
    ("UserStatsUser", "id"),
]


def test_user_profile_carries_string_user_id(openapi: dict[str, Any]) -> None:
    # The replacement identifier the #1813 migration note points clients at.
    schema = openapi["components"]["schemas"]["UserProfileResponse"]
    assert "user_id" in schema["properties"]
    assert "user_id" in schema["required"]
    # TZAwareBaseModel leaves the OpenAPI property untyped (see
    # test_response_ids_are_prefixed_strings); the validation-mode schema
    # carries the declared type.
    prop = _model("UserProfileResponse").model_json_schema(mode="validation")["properties"][
        "user_id"
    ]
    assert prop.get("type") == "string", prop


def test_user_stats_user_carries_string_user_id(openapi: dict[str, Any]) -> None:
    # #1882: the typed payload behind ``GET /admin/users/{user_id}/stats``.
    schemas_ = openapi["components"]["schemas"]
    assert schemas_["UserStats"]["properties"]["user"] == {
        "$ref": "#/components/schemas/UserStatsUser"
    }
    schema = schemas_["UserStatsUser"]
    assert schema["properties"]["user_id"].get("type") == "string", schema
    assert "user_id" in schema["required"]


@pytest.mark.parametrize(("schema", "field"), NO_INTEGER_ID_SCHEMAS)
def test_integer_pk_field_is_gone(openapi: dict[str, Any], schema: str, field: str) -> None:
    props = openapi["components"]["schemas"][schema]["properties"]
    assert field not in props, (schema, field)
    assert _integer_id_fields(props) == [], schema
    # The OpenAPI properties of a TZAwareBaseModel are untyped, so the sweep
    # above is blind there: read the declared types as well.
    declared = _model(schema).model_json_schema(mode="validation")["properties"]
    assert field not in declared, (schema, field)
    assert _integer_id_fields(declared) == [], schema


class _NullableIntegerIds(BaseModel):
    id: int
    owner_id: int | None = None
    current_workspace_id: str | None = None
    client_id: str
    count: int


def test_id_sweep_catches_nullable_integer_ids() -> None:
    # ``int | None`` has no top-level ``type``; the sweep must read ``anyOf``.
    props = _NullableIntegerIds.model_json_schema(mode="validation")["properties"]
    assert "type" not in props["owner_id"]
    assert _integer_id_fields(props) == ["id", "owner_id"]


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        ({"type": "integer"}, True),
        ({"type": ["integer", "null"]}, True),
        ({"anyOf": [{"type": "integer"}, {"type": "null"}]}, True),
        ({"oneOf": [{"type": "string"}, {"allOf": [{"type": "integer"}]}]}, True),
        ({"type": "string"}, False),
        ({"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}, False),
        ({}, False),
    ],
)
def test_declares_integer(prop: dict[str, Any], expected: bool) -> None:
    assert _declares_integer(prop) is expected
