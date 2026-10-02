"""No integer PK leaks through the REST schema (#1008, #1813).

Fails if a response model of API keys, share keys, resource tokens,
workspace connectors' token, workspace invitations or member credential keys
exposes an integer ``id`` / ``key_id`` / ``token_id`` again, or if one of the
routes addressing them takes an integer path id; or if the user profile,
OAuth client or system-admin responses regain their integer ``id`` /
``initial_admin_id`` (#1813).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from api.main import app
from api.routes import api_keys, resource_tokens, share_keys, workspace_connectors
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


def _model(name: str) -> type[BaseModel]:
    for module in (api_keys, share_keys, resource_tokens, workspace_connectors, schemas):
        model = getattr(module, name, None)
        if model is not None:
            return model
    raise LookupError(name)


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
    assert prop.get("type") != "integer", (schema, field, prop)
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
                assert prop.get("type") != "integer", (name, field)


# Responses addressed by a string id the client already has (users by
# ``user_id``, OAuth clients by ``client_id``): the integer PK was dropped
# outright (#1813).
NO_INTEGER_ID_SCHEMAS = [
    ("UserProfileResponse", "id"),
    ("OAuth2ClientResponse", "id"),
    ("OAuth2ClientWithSecretResponse", "id"),
    ("UserWithAdminFlag", "id"),
    ("SystemAdminListResponse", "initial_admin_id"),
]


@pytest.mark.parametrize(("schema", "field"), NO_INTEGER_ID_SCHEMAS)
def test_integer_pk_field_is_gone(openapi: dict[str, Any], schema: str, field: str) -> None:
    props = openapi["components"]["schemas"][schema]["properties"]
    assert field not in props, (schema, field)
    for name, prop in props.items():
        if name == "id" or name.endswith("_id"):
            assert prop.get("type") != "integer", (schema, name, prop)
