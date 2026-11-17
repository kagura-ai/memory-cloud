"""The registered feed observation API advertises the actual bounded wire contract."""

from api.main import app


def test_registered_feed_observation_schemas_keep_types_bounds_and_nested_items():
    document = app.openapi()
    schemas = document["components"]["schemas"]
    page = schemas["FeedConfigurationPage"]["properties"]
    detail = schemas["FeedConfigurationDetail"]["properties"]
    item = schemas["FeedObservation"]["properties"]
    assert page["items"]["type"] == "array"
    assert page["items"]["maxItems"] == 20
    assert page["items"]["items"]["$ref"] == "#/components/schemas/FeedObservation"
    for properties in (page, detail):
        assert properties["workspace_id"]["format"] == "uuid"
        assert properties["installation_id"]["format"] == "uuid"
        assert properties["as_of"]["format"] == "date-time"
        assert properties["generation"]["type"] == "integer"
        assert properties["generation"]["minimum"] == 1
        assert properties["generation"]["maximum"] == 2**31 - 1
        assert {entry["type"] for entry in properties["schema_hash"]["anyOf"]} == {
            "string",
            "null",
        }
    for properties in (item, detail):
        assert properties["feed_id"]["format"] == "uuid"
        assert properties["kind"]["enum"] == ["product", "faq"]
        assert properties["config_hash"]["pattern"] == "^[0-9a-f]{64}$"
        assert properties["config_version"]["minimum"] == 1
        assert properties["config_version"]["maximum"] == 2**31 - 1
        assert properties["revision"]["minimum"] == 0
    prefix = "/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/activity/feeds"
    for suffix, name in (("", "FeedConfigurationPage"), ("/{feed_id}", "FeedConfigurationDetail")):
        response = document["paths"][prefix + suffix]["get"]["responses"]["200"]
        assert response["content"]["application/json"]["schema"]["$ref"] == (
            "#/components/schemas/" + name
        )
