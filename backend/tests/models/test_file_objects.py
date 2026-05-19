"""Tests for ``file_objects`` + ``workspace_storage_usage`` ORM models (Issue #485).

These are pure metadata-level checks — no DB connection — so they run
in ``make test-local``. End-to-end constraint behaviour (CHECK
violations, partial-unique conflicts, generated-column population) is
covered by the integration suite under ``backend/tests/integration/``.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import UUID

from models.file_objects import FileObject, WorkspaceStorageUsage


class TestFileObjectModel:
    def test_table_name(self):
        assert FileObject.__tablename__ == "file_objects"

    def test_required_columns_present(self):
        cols = {c.name for c in FileObject.__table__.columns}
        # Identity + workspace + content
        assert {"id", "workspace_id", "sha256", "size_bytes", "filename", "content_type"} <= cols
        # Storage routing (R2-only after #616 dropped pg_inline scaffolding)
        assert {"storage_backend", "storage_key"} <= cols
        assert "inline_bytes" not in cols
        # Upload state machine (R3)
        assert {"status", "expires_at", "uploaded_at", "deleted_at"} <= cols

    def test_id_is_uuid(self):
        col = FileObject.__table__.c.id
        assert isinstance(col.type, UUID)
        assert col.primary_key is True

    def test_check_constraints_declared(self):
        names = {c.name for c in FileObject.__table_args__ if hasattr(c, "name")}
        assert "valid_file_storage_backend" in names
        assert "valid_file_status" in names
        assert "valid_file_storage_shape" in names

    def test_partial_unique_index_declared(self):
        """The (workspace_id, lower(sha256)) WHERE deleted_at IS NULL AND
        status != 'failed' case-insensitive partial unique index is the
        dedup gate (mirrors alembic e07_556_sha256_lowercase_index #556)."""
        for arg in FileObject.__table_args__:
            if hasattr(arg, "name") and arg.name == "uq_file_objects_workspace_sha256_active":
                assert arg.unique is True
                # Expression-based: column 0 = workspace_id (bare column ref,
                # SQLAlchemy resolves to ``file_objects.workspace_id``),
                # column 1 = lower(sha256) functional expression.
                exprs = [str(c) for c in arg.expressions]
                assert "workspace_id" in exprs[0]
                assert "lower(" in exprs[1]
                assert "sha256" in exprs[1]
                # Partial WHERE clause stored as a dialect-specific option
                where = arg.dialect_options.get("postgresql", {}).get("where")
                assert where is not None
                assert "deleted_at IS NULL" in str(where)
                assert "status" in str(where)
                return
        msg = "uq_file_objects_workspace_sha256_active not found in __table_args__"
        raise AssertionError(msg)


class TestWorkspaceStorageUsageModel:
    def test_table_name(self):
        assert WorkspaceStorageUsage.__tablename__ == "workspace_storage_usage"

    def test_workspace_id_is_pk(self):
        col = WorkspaceStorageUsage.__table__.c.workspace_id
        assert col.primary_key is True

    def test_counter_columns_have_zero_default(self):
        used = WorkspaceStorageUsage.__table__.c.used_bytes
        count = WorkspaceStorageUsage.__table__.c.file_count
        assert used.server_default is not None
        assert count.server_default is not None
        assert used.nullable is False
        assert count.nullable is False

    def test_nonneg_check_constraints_declared(self):
        names = {c.name for c in WorkspaceStorageUsage.__table_args__ if hasattr(c, "name")}
        assert "nonneg_used_bytes" in names
        assert "nonneg_file_count" in names


class TestMemoryExternalBlobComputedColumns:
    """Issue #485 R1: ``Memory`` exposes the discriminated-union blob
    reference via two persisted generated columns."""

    def test_columns_declared(self):
        from models.memory import Memory

        cols = {c.name for c in Memory.__table__.columns}
        assert "external_blob_backend" in cols
        assert "external_blob_ref" in cols

    def test_columns_are_computed_persisted(self):
        from models.memory import Memory

        for name in ("external_blob_backend", "external_blob_ref"):
            col = Memory.__table__.c[name]
            assert col.computed is not None, f"{name} must be a Computed column"
            assert col.computed.persisted is True, f"{name} must be persisted=True"

    def test_computed_expression_uses_nested_arrow(self):
        """``details->'external_blob'->>'backend|ref'`` two-step extraction."""
        from models.memory import Memory

        backend_expr = str(Memory.__table__.c.external_blob_backend.computed.sqltext)
        ref_expr = str(Memory.__table__.c.external_blob_ref.computed.sqltext)
        assert "external_blob" in backend_expr and "backend" in backend_expr
        assert "external_blob" in ref_expr and "ref" in ref_expr
