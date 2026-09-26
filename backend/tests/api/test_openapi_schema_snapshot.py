"""A structural snapshot of the REST OpenAPI schema (#1720).

The MCP tool definitions are pinned by ``tests/mcp_server/test_tool_definition_budget.py``:
every definition minus its prose must equal a committed skeleton. The REST
surface had no equivalent, so a new route, a renamed field, a changed status
code or a new error model reached ``main`` without a diff anyone had to
approve. This module is the REST half of that guard, and the REST half of the
pre-1.0 enumeration (#622).

``skeleton`` keeps what a client can observe — paths, methods, parameters,
request and response schemas, status codes, ``operationId``, ``required``,
enums, bounds, types, formats and ``$ref`` targets — and drops what is not a
surface change: prose and labels (``description``, ``summary`` and ``title``
strings), ``example`` and ``examples``, and ``info.version``, which every
release bumps. Keys are sorted, ``required`` lists are sorted and the
top-level ``tags`` are sorted by name, so the fixture diff shows the change,
not a reordering. ``enum`` keeps its order.

A deliberate surface change regenerates the snapshot::

    UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/api/test_openapi_schema_snapshot.py

and the diff of ``fixtures/openapi_schema_snapshot.json`` is reviewed like code.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from api.main import app

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "openapi_schema_snapshot.json"
REGENERATE = "UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/api/test_openapi_schema_snapshot.py"

# Keys whose values are prose, labels or illustrations: free to change without
# a fixture update. ``description``, ``summary`` and ``title`` are only prose
# when the value is a string — a model field named ``description`` maps to a
# schema object and is part of the surface. ``title`` is pydantic's label
# derived from the field or class name (``"Api Key"`` for ``api_key``): the
# name itself is already the key the schema sits under.
_PROSE_STRING_KEYS = frozenset({"description", "summary", "title"})
_EXAMPLE_KEYS = frozenset({"example", "examples"})

# Maps whose keys are user-chosen names (fields, models, paths, status codes,
# header names, reusable components) rather than OpenAPI keywords. Nothing is
# stripped at that level, so a field or a model called ``summary`` or
# ``examples`` stays in the skeleton.
_NAME_MAPS = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "schemas",
        "paths",
        "responses",
        "parameters",
        "headers",
        "requestBodies",
        "securitySchemes",
    }
)

# How many changed entries the failure message lists per section.
_REPORT_LIMIT = 25

Mutator = Callable[[dict], None]


def _is_prose(key: str, value: Any) -> bool:
    if key in _PROSE_STRING_KEYS:
        return isinstance(value, str)
    return key in _EXAMPLE_KEYS


def normalize(node: Any, *, keys_are_names: bool = False) -> Any:
    """Copy ``node`` without prose or examples, with every object's keys sorted.

    Args:
        node: Any JSON value from an OpenAPI document.
        keys_are_names: True while the keys of ``node`` are names (the entries
            of ``properties``, ``components.schemas``, ``paths``, ...) rather
            than OpenAPI keywords, so none of them is treated as prose.

    Returns:
        The normalized copy. ``required`` lists are sorted (JSON Schema treats
        them as sets); every other list, ``enum`` included, keeps the order of
        the document a client reads.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key in sorted(node):
            value = node[key]
            if not keys_are_names and _is_prose(key, value):
                continue
            child = normalize(value, keys_are_names=(not keys_are_names) and key in _NAME_MAPS)
            if (
                not keys_are_names
                and key == "required"
                and isinstance(child, list)
                and all(isinstance(item, str) for item in child)
            ):
                child = sorted(child)
            out[key] = child
        return out
    if isinstance(node, list):
        return [normalize(item) for item in node]
    return node


def skeleton(document: dict) -> dict:
    """The comparable shape of an OpenAPI document.

    ``normalize`` minus ``info.version`` — the release ceremony bumps
    ``APP_VERSION`` on every release, and a version string is not a route,
    parameter, field, status code or enum — with the top-level ``tags`` sorted
    by name, since their order only groups the docs UI.
    """
    shape = normalize(document)
    if isinstance(shape.get("info"), dict):
        shape["info"].pop("version", None)
    if isinstance(shape.get("tags"), list):
        shape["tags"] = sorted(shape["tags"], key=_tag_name)
    return shape


def _tag_name(tag: Any) -> str:
    return str(tag.get("name", "")) if isinstance(tag, dict) else ""


def _render(shape: dict) -> str:
    return json.dumps(shape, ensure_ascii=False, indent=2) + "\n"


def _schemas(doc: dict) -> dict:
    return doc.get("components", {}).get("schemas", {})


def _paths(doc: dict) -> dict:
    return doc.get("paths", {})


def _rest(doc: dict) -> dict:
    """Everything except paths and component schemas, as one name → value map."""
    rest = {key: value for key, value in doc.items() if key not in ("paths", "components")}
    rest["components (other than schemas)"] = {
        key: value for key, value in doc.get("components", {}).items() if key != "schemas"
    }
    return rest


# Reported in this order: a renamed model explains the routes that use it, so
# the few schema entries come before the many path entries.
_SECTIONS: tuple[tuple[str, Callable[[dict], dict]], ...] = (
    ("schema", _schemas),
    ("path", _paths),
    ("top-level", _rest),
)


def _changed_entries(expected: dict, actual: dict, *, limit: int = _REPORT_LIMIT) -> list[str]:
    """Name the schemas, paths and other top-level keys that differ, capped per section."""
    report: list[str] = []
    for label, section in _SECTIONS:
        exp, act = section(expected), section(actual)
        changed = [key for key in sorted(exp.keys() | act.keys()) if exp.get(key) != act.get(key)]
        report += [f"{label} {key}" for key in changed[:limit]]
        if len(changed) > limit:
            report.append(f"(+{len(changed) - limit} more {label} entries)")
    return report


def _fail_mismatch(committed: str, current: str, *, what: str) -> None:
    changed = _changed_entries(json.loads(committed), json.loads(current))
    if changed:
        pytest.fail(
            f"{what}: {changed}. If that is intended, regenerate the snapshot with "
            f"`{REGENERATE}` and review the fixture diff."
        )
    pytest.fail(
        f"{what}: same content, different formatting. Regenerate the snapshot with "
        f"`{REGENERATE}` instead of editing it."
    )


# ------------------------------------------------------------------- snapshot


def test_rest_schema_matches_the_committed_snapshot():
    """Prose, examples and the version may change; nothing else in the schema may."""
    current = _render(skeleton(app.openapi()))
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(current, encoding="utf-8", newline="\n")
    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if current != committed:
        _fail_mismatch(committed, current, what="the REST OpenAPI schema changed structurally")


def test_snapshot_is_normalized_and_sorted():
    """The committed file must be what ``skeleton`` produces, not a hand edit."""
    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    current = _render(skeleton(json.loads(committed)))
    if current != committed:
        _fail_mismatch(committed, current, what="the committed snapshot is not normalized")


# ------------------------------------------------------------------ normalize

_SAMPLE: dict = {
    "openapi": "3.1.0",
    "info": {"title": "Sample", "version": "1", "description": "prose"},
    "paths": {
        "/items": {
            "get": {
                "summary": "List items",
                "description": "prose",
                "operationId": "list_items",
                "tags": ["items"],
                "parameters": [
                    {
                        "name": "q",
                        "in": "query",
                        "description": "prose",
                        "required": False,
                        "schema": {"type": "string", "maxLength": 10, "examples": ["a"]},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Item"},
                                "example": {"id": 1},
                            }
                        },
                    }
                },
            }
        }
    },
    "components": {
        "schemas": {
            "Item": {
                "title": "Item",
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "integer", "description": "prose"},
                    "description": {"type": "string", "description": "the item's own field"},
                    "kind": {"type": "string", "enum": ["a", "b"]},
                },
            }
        }
    },
}


def _sample(mutate: Mutator) -> dict:
    """A deep copy of ``_SAMPLE`` after ``mutate`` has edited it in place."""
    doc = copy.deepcopy(_SAMPLE)
    mutate(doc)
    return doc


def _reword(doc: dict) -> None:
    doc["info"]["description"] = "new prose"
    doc["info"]["title"] = "Sample API"
    get = doc["paths"]["/items"]["get"]
    get["summary"] = "List all the items"
    get["description"] = "new prose"
    get["parameters"][0]["description"] = "new prose"
    get["parameters"][0]["schema"]["examples"] = ["b", "c"]
    get["responses"]["200"]["description"] = "Success"
    get["responses"]["200"]["content"]["application/json"]["example"] = {"id": 2}
    doc["components"]["schemas"]["Item"]["title"] = "An item"
    item = doc["components"]["schemas"]["Item"]["properties"]
    item["id"]["description"] = "new prose"
    item["id"]["title"] = "Identifier"
    item["description"]["description"] = "new prose"


def test_prose_only_changes_normalize_to_the_same_skeleton():
    assert normalize(_sample(_reword)) == normalize(_SAMPLE)


def test_a_version_bump_does_not_change_the_skeleton():
    def bump(doc: dict) -> None:
        doc["info"]["version"] = "2"

    assert skeleton(_sample(bump)) == skeleton(_SAMPLE)
    assert "version" not in skeleton(_SAMPLE)["info"]


def test_a_field_named_description_stays_but_loses_its_own_prose():
    fields = normalize(_SAMPLE)["components"]["schemas"]["Item"]["properties"]
    assert fields["description"] == {"type": "string"}
    assert fields["id"] == {"type": "integer"}


def test_normalize_sorts_keys_and_required_but_keeps_enum_order():
    reordered = {"paths": {}, "openapi": "3.1.0", "info": {"version": "1", "title": "S"}}
    assert _render(normalize(reordered)) == _render(normalize(dict(reversed(reordered.items()))))

    def swap_required(doc: dict) -> None:
        doc["components"]["schemas"]["Item"]["required"] = ["kind", "id"]

    def swap_enum(doc: dict) -> None:
        doc["components"]["schemas"]["Item"]["properties"]["kind"]["enum"] = ["b", "a"]

    with_two_required = _sample(
        lambda doc: doc["components"]["schemas"]["Item"].update(required=["id", "kind"])
    )
    assert normalize(_sample(swap_required)) == normalize(with_two_required)
    assert normalize(_sample(swap_enum)) != normalize(_SAMPLE)


def test_top_level_tags_are_sorted_by_name():
    def tags(doc: dict) -> None:
        doc["tags"] = [{"name": "items", "description": "prose"}, {"name": "auth"}]

    def tags_reversed(doc: dict) -> None:
        doc["tags"] = [{"name": "auth"}, {"name": "items", "description": "other prose"}]

    assert skeleton(_sample(tags)) == skeleton(_sample(tags_reversed))
    assert skeleton(_sample(tags))["tags"] == [{"name": "auth"}, {"name": "items"}]


def _add_response(doc: dict) -> None:
    doc["paths"]["/items"]["get"]["responses"]["404"] = {"description": "Not found"}


def _add_path(doc: dict) -> None:
    doc["paths"]["/items/{id}"] = {"delete": {"operationId": "delete_item", "responses": {}}}


def _rename_field(doc: dict) -> None:
    props = doc["components"]["schemas"]["Item"]["properties"]
    props["identifier"] = props.pop("id")


def _drop_required(doc: dict) -> None:
    doc["components"]["schemas"]["Item"]["required"] = []


def _change_enum(doc: dict) -> None:
    doc["components"]["schemas"]["Item"]["properties"]["kind"]["enum"].append("c")


def _change_bound(doc: dict) -> None:
    doc["paths"]["/items"]["get"]["parameters"][0]["schema"]["maxLength"] = 20


def _change_operation_id(doc: dict) -> None:
    doc["paths"]["/items"]["get"]["operationId"] = "items_list"


def _change_tag(doc: dict) -> None:
    doc["paths"]["/items"]["get"]["tags"] = ["inventory"]


def _add_path_and_rename_field(doc: dict) -> None:
    _add_path(doc)
    _rename_field(doc)


@pytest.mark.parametrize(
    "mutate",
    [
        _add_response,
        _add_path,
        _rename_field,
        _drop_required,
        _change_enum,
        _change_bound,
        _change_operation_id,
        _change_tag,
    ],
    ids=lambda fn: fn.__name__.lstrip("_"),
)
def test_structural_changes_are_visible_after_normalization(mutate: Mutator):
    assert normalize(_sample(mutate)) != normalize(_SAMPLE)


def test_failure_message_names_the_changed_entries_schemas_first():
    expected = normalize(_SAMPLE)
    actual = normalize(_sample(_add_path_and_rename_field))
    assert _changed_entries(expected, actual) == ["schema Item", "path /items/{id}"]


def test_failure_message_caps_each_section_separately():
    def many_paths(doc: dict) -> None:
        for n in range(4):
            doc["paths"][f"/p{n}"] = {"get": {"operationId": f"p{n}", "responses": {}}}

    expected = normalize(_SAMPLE)

    def many_paths_and_rename_field(doc: dict) -> None:
        many_paths(doc)
        _rename_field(doc)

    actual = normalize(_sample(many_paths_and_rename_field))
    assert _changed_entries(expected, actual, limit=2) == [
        "schema Item",
        "path /p0",
        "path /p1",
        "(+2 more path entries)",
    ]
