"""#2026: every nullable JSON column stores a Python ``None`` as SQL NULL.

``models/json_types.py`` holds the rule and the shared types. This walks the
whole ``Base.metadata`` (every module under ``models/`` imported), so a new
nullable JSON / JSONB column declared with the plain type fails here.
"""

import importlib
import pkgutil

import pytest
from sqlalchemy import JSON, Column

import models
from db.base import Base
from models.json_types import NullableJSON, NullableJSONB

# Nullable JSON columns that keep ``none_as_null=False`` on purpose: each has a
# ``default`` / ``server_default``, which ``none_as_null=True`` would make fire
# on an explicit ``None`` (the ORM leaves it out of the INSERT), turning a
# written ``None`` into ``{}``.
EXCEPTIONS = {
    # resource ingest stores the caller's ``event_metadata: null`` and the
    # resource-events API returns it as written.
    ("resource_events", "event_metadata"),
    # indexer metrics start as ``{}``; the indexer never writes None.
    ("indexer_state", "metrics"),
}


def _json_columns() -> list[Column]:
    for module in pkgutil.iter_modules(models.__path__):
        importlib.import_module(f"models.{module.name}")
    return [
        column
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, JSON)
    ]


def _key(column: Column) -> tuple[str, str]:
    return (column.table.name, column.name)


NULLABLE = sorted(
    (c for c in _json_columns() if c.nullable and _key(c) not in EXCEPTIONS), key=_key
)


@pytest.mark.parametrize("column", NULLABLE, ids=lambda c: ".".join(_key(c)))
def test_nullable_json_column_uses_a_shared_type(column: Column) -> None:
    assert column.type is NullableJSON or column.type is NullableJSONB
    assert isinstance(column.type, JSON) and column.type.none_as_null is True
    # A default would fire on an explicit None (left out of the INSERT): such a
    # column belongs in EXCEPTIONS with the plain type instead.
    assert column.default is None and column.server_default is None


def test_the_shared_types_store_none_as_sql_null() -> None:
    assert NullableJSON.none_as_null is True
    assert NullableJSONB.none_as_null is True
    # Same DDL as the plain types: the switch needs no schema migration.
    assert type(NullableJSON) is JSON
    assert NullableJSONB.__visit_name__ == "JSONB"


def test_the_inventory_is_the_expected_one() -> None:
    """Pin the switched set so a column that silently stops being JSON is noticed."""
    assert {".".join(_key(c)) for c in NULLABLE} == {
        "audit_logs.user_metadata",
        "bm25_idf_drift_log.top_divergent_terms",
        "erasure_requests.deleted_data_summary",
        "llm_call_log.call_metadata",
        "measurements.details",
        "memories.context",
        "memories.details",
        "memories.supersede_candidate",
        "memory_access_events.event_metadata",
        "memory_analyses.quality",
        "neural_memory_edges.metadata",
        "resource_events.payload",
        "secret_access_log.req_meta",
        "sleep_actions.details",
        "sleep_reports.consolidation_result",
        "sleep_reports.dedup_result",
        "sleep_reports.edge_discovery_result",
        "sleep_reports.importance_result",
        "sleep_reports.merge_retention_result",
        "sleep_reports.reindex_result",
        "workspace_connectors.channel_ids",
        "workspace_connectors.pii_guardrail_config",
        "workspace_connectors.runtime_config",
    }


@pytest.mark.parametrize("key", sorted(EXCEPTIONS), ids=".".join)
def test_exceptions_still_need_their_exception(key: tuple[str, str]) -> None:
    """An exception is only justified while the column is nullable with a default."""
    column = Base.metadata.tables[key[0]].columns[key[1]]
    assert isinstance(column.type, JSON)
    assert column.nullable
    assert column.default is not None or column.server_default is not None
    assert column.type.none_as_null is False
