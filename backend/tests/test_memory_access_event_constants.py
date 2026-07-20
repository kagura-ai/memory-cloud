"""Regression tests for #1278: memory_access_events CHECK + classification pins.

The CHECK constraints are derived from ordered Python tuples so
``Base.metadata.create_all()`` produces literals byte-identical to the alembic
migration (``e66``); adding a value requires the coordinated edits caught here.
"""

from models.memory_access_event import (
    MAE_METADATA_MAX_BYTES,
    MAE_MUTABLE_COLUMNS,
    MAE_OPERATIONS,
    MAE_OUTCOMES,
    MAE_POLICY_DECISIONS,
    MAE_PRINCIPAL_TYPES,
    MAE_SURFACES,
    MemoryAccessEvent,
)


def _check(name: str) -> str:
    c = next(c for c in MemoryAccessEvent.__table_args__ if getattr(c, "name", None) == name)
    return c.sqltext.text


def test_operation_tuple_is_the_audited_ops():
    assert MAE_OPERATIONS == (
        "recall",
        "reference",
        "remember",
        "update",
        "forget",
        "load_pinned",
        "bootstrap",
        "feedback",
        "explore",  # #1401 (appended, never reordered)
    )


def test_check_literals_byte_identical_to_migration():
    assert _check("valid_mae_operation") == (
        "operation IN ('recall', 'reference', 'remember', 'update', 'forget', "
        "'load_pinned', 'bootstrap', 'feedback', 'explore')"
    )
    assert _check("valid_mae_outcome") == "outcome IN ('success', 'denied', 'error', 'partial')"
    assert _check("valid_mae_surface") == "surface IN ('mcp', 'rest')"
    assert _check("valid_mae_principal") == "principal_type IN ('api_key', 'oauth', 'session')"
    assert _check("valid_mae_policy") == (
        "policy_decision IS NULL OR policy_decision IN ('allowed', 'binding_denied', "
        "'rbac_denied', 'would_deny', 'unbound')"
    )
    assert _check("mae_metadata_size") == "octet_length(event_metadata::text) <= 4096"


def test_constant_value_sets():
    assert MAE_OUTCOMES == ("success", "denied", "error", "partial")
    assert MAE_SURFACES == ("mcp", "rest")
    assert MAE_PRINCIPAL_TYPES == ("api_key", "oauth", "session")
    assert MAE_POLICY_DECISIONS == (
        "allowed",
        "binding_denied",
        "rbac_denied",
        "would_deny",
        "unbound",
    )
    assert MAE_METADATA_MAX_BYTES == 4096


def test_erasure_carve_out_columns():
    # The ONLY columns the append-only trigger permits an UPDATE to touch.
    assert MAE_MUTABLE_COLUMNS == ("user_id", "session_id", "run_id", "event_metadata")


def test_classified_operational():
    from models.data_boundary import OPERATIONAL_TABLES

    assert "memory_access_events" in OPERATIONAL_TABLES


def test_no_foreign_keys():
    # Audit-grade: the trail must survive entity deletion (no FK to
    # agents/contexts/workspaces/keys).
    assert list(MemoryAccessEvent.__table__.foreign_keys) == []
