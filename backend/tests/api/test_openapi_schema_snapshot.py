"""A structural snapshot of the REST OpenAPI schema (#1720).

The MCP tool definitions are pinned by ``tests/mcp_server/test_tool_definition_budget.py``:
every definition minus its prose must equal a committed skeleton. The REST
surface had no equivalent, so a new route, a renamed field, a changed status
code or a new error model reached ``main`` without a diff anyone had to
approve. This module is the REST half of that guard, and the REST half of the
pre-1.0 enumeration (#622).

``skeleton`` keeps what a client can observe — paths, methods, parameters,
request and response schemas, status codes, ``operationId``, ``required``,
enums, bounds, types, formats and ``$ref`` targets — and drops prose and
examples (``description`` and ``summary`` strings, ``example``, ``examples``),
which may change freely, plus ``info.version``, which every release bumps.
Keys are sorted so the fixture diff shows the change, not a reordering.

A deliberate surface change regenerates the snapshot::

    UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/api/test_openapi_schema_snapshot.py

and the diff of ``fixtures/openapi_schema_snapshot.json`` is reviewed like code.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from api.main import app

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "openapi_schema_snapshot.json"
REGENERATE = "UPDATE_OPENAPI_SNAPSHOT=1 pytest tests/api/test_openapi_schema_snapshot.py"

# Keys whose values are prose or illustrations: free to change without a
# fixture update. ``description`` and ``summary`` are only prose when the
# value is a string — a model field named ``description`` maps to a schema
# object and is part of the surface.
_PROSE_STRING_KEYS = frozenset({"description", "summary"})
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
        The normalized copy. Lists keep their order: ``required`` and ``enum``
        are ordered in the document a client reads.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key in sorted(node):
            value = node[key]
            if not keys_are_names and _is_prose(key, value):
                continue
            out[key] = normalize(value, keys_are_names=(not keys_are_names) and key in _NAME_MAPS)
        return out
    if isinstance(node, list):
        return [normalize(item) for item in node]
    return node


def skeleton(document: dict) -> dict:
    """The comparable shape of an OpenAPI document.

    ``normalize`` minus ``info.version``: the release ceremony bumps
    ``APP_VERSION`` on every release, and a version string is not a route,
    parameter, field, status code or enum, so it must not move the fixture.
    """
    shape = normalize(document)
    if isinstance(shape.get("info"), dict):
        shape["info"].pop("version", None)
    return shape


def _render(shape: dict) -> str:
    return json.dumps(shape, ensure_ascii=False, indent=2) + "\n"


def _changed_entries(expected: dict, actual: dict) -> list[str]:
    """Name the paths, component schemas and other top-level keys that differ."""
    changed: list[str] = []
    for section, label in (("paths", "path"), ("components", "component")):
        exp, act = expected.get(section, {}), actual.get(section, {})
        if section == "components":
            exp, act = exp.get("schemas", {}), act.get("schemas", {})
            label = "schema"
        keys = sorted(exp.keys() | act.keys())
        changed += [f"{label} {key}" for key in keys if exp.get(key) != act.get(key)]
    for key in sorted(expected.keys() | actual.keys()):
        if key == "paths":
            continue
        if key == "components":
            exp_rest = {k: v for k, v in expected.get(key, {}).items() if k != "schemas"}
            act_rest = {k: v for k, v in actual.get(key, {}).items() if k != "schemas"}
            if exp_rest != act_rest:
                changed.append("components (other than schemas)")
            continue
        if expected.get(key) != actual.get(key):
            changed.append(f"top-level {key}")
    return changed


# ------------------------------------------------------------------- snapshot


def test_rest_schema_matches_the_committed_snapshot():
    """Prose, examples and the version may change; nothing else in the schema may."""
    current = _render(skeleton(app.openapi()))
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(current, encoding="utf-8")
    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if current != committed:
        changed = _changed_entries(json.loads(committed), json.loads(current))
        shown = changed[:_REPORT_LIMIT]
        more = f" (+{len(changed) - len(shown)} more)" if len(changed) > len(shown) else ""
        pytest.fail(
            f"the REST OpenAPI schema changed structurally: {shown}{more}. If that is "
            f"intended, regenerate the snapshot with `{REGENERATE}` and review the "
            "fixture diff."
        )


def test_snapshot_is_normalized_and_sorted():
    """The committed file must be what ``skeleton`` produces, not a hand edit."""
    committed = SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert _render(skeleton(json.loads(committed))) == committed


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


def _sample(mutate) -> dict:
    """A deep copy of ``_SAMPLE`` after ``mutate`` has edited it in place."""
    doc = copy.deepcopy(_SAMPLE)
    mutate(doc)
    return doc


def _reword(doc: dict) -> None:
    doc["info"]["description"] = "new prose"
    get = doc["paths"]["/items"]["get"]
    get["summary"] = "List all the items"
    get["description"] = "new prose"
    get["parameters"][0]["description"] = "new prose"
    get["parameters"][0]["schema"]["examples"] = ["b", "c"]
    get["responses"]["200"]["description"] = "Success"
    get["responses"]["200"]["content"]["application/json"]["example"] = {"id": 2}
    item = doc["components"]["schemas"]["Item"]["properties"]
    item["id"]["description"] = "new prose"
    item["description"]["description"] = "new prose"


def test_prose_only_changes_normalize_to_the_same_skeleton():
    assert normalize(_sample(_reword)) == normalize(_SAMPLE)


def test_a_version_bump_does_not_change_the_skeleton():
    def bump(doc: dict) -> None:
        doc["info"]["version"] = "2"

    assert skeleton(_sample(bump)) == skeleton(_SAMPLE)
    assert "version" not in skeleton(_SAMPLE)["info"]
    assert skeleton(_SAMPLE)["info"]["title"] == "Sample"


def test_a_field_named_description_stays_but_loses_its_own_prose():
    fields = normalize(_SAMPLE)["components"]["schemas"]["Item"]["properties"]
    assert fields["description"] == {"type": "string"}
    assert fields["id"] == {"type": "integer"}


def test_normalize_sorts_keys_but_keeps_list_order():
    reordered = {"paths": {}, "openapi": "3.1.0", "info": {"version": "1", "title": "S"}}
    assert _render(normalize(reordered)) == _render(normalize(dict(reversed(reordered.items()))))
    assert normalize(_SAMPLE)["components"]["schemas"]["Item"]["properties"]["kind"]["enum"] == [
        "a",
        "b",
    ]


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
def test_structural_changes_are_visible_after_normalization(mutate):
    assert normalize(_sample(mutate)) != normalize(_SAMPLE)


def test_failure_message_names_the_changed_entries():
    expected = normalize(_SAMPLE)
    actual = normalize(_sample(lambda doc: (_add_path(doc), _rename_field(doc))))
    assert _changed_entries(expected, actual) == ["path /items/{id}", "schema Item"]
