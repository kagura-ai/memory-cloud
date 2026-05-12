"""Integration tests for migration ``e10_626_apikey_bound_context_id`` (#626).

Covers:

1. ``api_keys.bound_context_id`` is added (UUID, nullable, indexed).
2. ``usage_stats.api_key_id`` is added (Integer, nullable, indexed).
3. The CHECK constraint ``ck_api_keys_binding_workspace_exclusion`` rejects
   rows where both ``bound_context_id`` and ``workspace_id`` are non-NULL
   (the two scopings are mutually exclusive — workspace-scoped #169 keys
   grant access to all contexts in a workspace, public-bound #626 keys
   grant attribution for one is_public=true context only).
4. FK ``fk_api_keys_bound_context_id`` is SET NULL on delete (deleting the
   bound context disables the key without cascading the key's deletion).
5. ``downgrade()`` drops both columns, the constraint, the FK, and both
   indexes cleanly.

The pre-revision is ``e09_608_dcr_default_narrow``.
"""

import uuid

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from tests.integration.test_alembic_migrations import (
    _alembic_at_test_db,
    _get_alembic_config,
    _reset_alembic_state,
    _sync_engine,
)

# Pin the pre-revision so this test stays correct as more migrations land on
# top of e10_626. The downgrade tests assume a clean rewind to exactly this
# revision.
PRE_E10_REV = "e09_608_dcr_default_narrow"
E10_REV = "e10_626_apikey_bound_context_id"


def _seed_workspace(conn, plan_name: str = "pro", owner_user_id: str = "owner-626") -> str:
    ws_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, owner_user_id, plan_name) "
            "VALUES (:id, :name, :owner, :plan)"
        ),
        {"id": ws_id, "name": f"ws-{ws_id[:8]}", "owner": owner_user_id, "plan": plan_name},
    )
    return ws_id


def _seed_context(conn, workspace_id: str, is_public: bool = True) -> str:
    ctx_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO contexts (id, workspace_id, name, created_by, is_public) "
            "VALUES (:id, :ws, :name, :owner, :is_pub)"
        ),
        {
            "id": ctx_id,
            "ws": workspace_id,
            "name": f"ctx-{ctx_id[:8]}",
            "owner": "owner-626",
            "is_pub": is_public,
        },
    )
    return ctx_id


def _seed_api_key(
    conn,
    user_id: str = "owner-626",
    workspace_id: str | None = None,
    bound_context_id: str | None = None,
    key_suffix: str = "test",
) -> int:
    """Insert an api_keys row and return its integer id.

    Raises:
        IntegrityError: When the row violates the new CHECK constraint
            (``bound_context_id IS NULL OR workspace_id IS NULL``).
    """
    conn.execute(
        text(
            "INSERT INTO api_keys "
            "(key_hash, key_prefix, name, user_id, workspace_id, bound_context_id) "
            "VALUES (:hash, :prefix, :name, :uid, :ws, :bound)"
        ),
        {
            # 64-char SHA256-shaped hash so the unique index is satisfied
            # for each row (length must match ``String(64)``).
            "hash": f"hash_{key_suffix}".ljust(64, "0"),
            "prefix": f"kagura_{key_suffix[:8]}".ljust(16, "x")[:16],
            "name": f"key-{key_suffix}",
            "uid": user_id,
            "ws": workspace_id,
            "bound": bound_context_id,
        },
    )
    return conn.execute(
        text("SELECT id FROM api_keys WHERE name = :name"),
        {"name": f"key-{key_suffix}"},
    ).scalar_one()


class TestE10ApiKeyBoundContextIdMigration:
    """Data-shape and constraint checks for ``e10_626_apikey_bound_context_id``."""

    def test_upgrade_adds_bound_context_id_column(self) -> None:
        """``api_keys.bound_context_id`` exists with the expected shape."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E10_REV)

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            cols = {c["name"]: c for c in inspector.get_columns("api_keys")}
            assert "bound_context_id" in cols
            assert cols["bound_context_id"]["nullable"] is True

            indexes = {idx["name"] for idx in inspector.get_indexes("api_keys")}
            assert "idx_api_keys_bound_context_id" in indexes
        finally:
            engine.dispose()

    def test_upgrade_adds_api_key_id_to_usage_stats(self) -> None:
        """``usage_stats.api_key_id`` exists, nullable, indexed."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E10_REV)

        engine = _sync_engine()
        try:
            inspector = inspect(engine)
            cols = {c["name"]: c for c in inspector.get_columns("usage_stats")}
            assert "api_key_id" in cols
            assert cols["api_key_id"]["nullable"] is True

            indexes = {idx["name"] for idx in inspector.get_indexes("usage_stats")}
            assert "idx_usage_stats_api_key_id" in indexes
        finally:
            engine.dispose()

    def test_mutual_exclusion_check_rejects_both_set(self) -> None:
        """A key with both ``workspace_id`` and ``bound_context_id`` is rejected."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E10_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                ws = _seed_workspace(conn)
                ctx = _seed_context(conn, ws, is_public=True)
                try:
                    _seed_api_key(
                        conn,
                        workspace_id=ws,
                        bound_context_id=ctx,
                        key_suffix="conflict",
                    )
                    raise AssertionError(
                        "Expected IntegrityError on both workspace_id and "
                        "bound_context_id being set"
                    )
                except IntegrityError:
                    pass
        finally:
            engine.dispose()

    def test_either_one_or_neither_is_permitted(self) -> None:
        """Rows with workspace_id-only, bound_context_id-only, or neither are valid."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E10_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                ws = _seed_workspace(conn)
                ctx = _seed_context(conn, ws, is_public=True)
                # Owner-scoped (neither set): valid
                _seed_api_key(conn, key_suffix="owner")
                # Workspace-scoped (#169): valid
                _seed_api_key(conn, workspace_id=ws, key_suffix="ws")
                # Public-bound (#626): valid
                _seed_api_key(conn, bound_context_id=ctx, key_suffix="bound")
        finally:
            engine.dispose()

    def test_fk_set_null_on_context_delete(self) -> None:
        """Deleting the bound context nulls ``api_keys.bound_context_id``."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E10_REV)

        engine = _sync_engine()
        try:
            with engine.begin() as conn:
                ws = _seed_workspace(conn)
                ctx = _seed_context(conn, ws, is_public=True)
                key_id = _seed_api_key(
                    conn,
                    bound_context_id=ctx,
                    key_suffix="setnull",
                )
                # Delete the bound context.
                conn.execute(text("DELETE FROM contexts WHERE id = :id"), {"id": ctx})

            with engine.begin() as conn:
                bound = conn.execute(
                    text("SELECT bound_context_id FROM api_keys WHERE id = :id"),
                    {"id": key_id},
                ).scalar_one()
            assert bound is None
        finally:
            engine.dispose()

    def test_downgrade_removes_column_and_constraint(self) -> None:
        """Downgrade drops both new columns, the FK, the indexes, and the CHECK."""
        _reset_alembic_state()
        with _alembic_at_test_db():
            command.upgrade(_get_alembic_config(), E10_REV)
            command.downgrade(_get_alembic_config(), PRE_E10_REV)

        engine = _sync_engine()
        try:
            inspector = inspect(engine)

            ak_cols = {c["name"] for c in inspector.get_columns("api_keys")}
            assert "bound_context_id" not in ak_cols

            us_cols = {c["name"] for c in inspector.get_columns("usage_stats")}
            assert "api_key_id" not in us_cols

            ak_indexes = {idx["name"] for idx in inspector.get_indexes("api_keys")}
            assert "idx_api_keys_bound_context_id" not in ak_indexes
            us_indexes = {idx["name"] for idx in inspector.get_indexes("usage_stats")}
            assert "idx_usage_stats_api_key_id" not in us_indexes

            # Confirm the CHECK constraint is gone too.
            ak_constraints = {c["name"] for c in inspector.get_check_constraints("api_keys")}
            assert "ck_api_keys_binding_workspace_exclusion" not in ak_constraints
        finally:
            # Convention: leave DB at head after each test.
            with _alembic_at_test_db():
                command.upgrade(_get_alembic_config(), "head")
            engine.dispose()
