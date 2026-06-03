"""Regression tests for #886: delivery_mode constant + CHECK invariants.

``delivery_mode`` is an orthogonal delivery attribute on Memory (NOT a new
memory ``type``): ``always`` | ``on_recall`` | ``on_trigger`` (#885 design memory
242cb28a). Like ``edge_type``/``origin`` on NeuralMemoryEdge, the DB CHECK
constraint is derived from an ordered Python tuple (``_ALL_DELIVERY_MODES``) so
``Base.metadata.create_all()`` (tests, fresh dev DBs) produces a CHECK string
byte-identical to the alembic head — preventing ``alembic revision
--autogenerate`` from emitting a spurious no-op migration.

Adding a new delivery_mode requires THREE coordinated edits (caught here if any
are missed): (1) add ``DELIVERY_MODE_NEW`` to the constants block, (2) append it
to ``_ALL_DELIVERY_MODES``, (3) update the expected literal below — plus an
alembic migration that ALTERs the prod CHECK.
"""

from models.memory import (
    _ALL_DELIVERY_MODES,
    DELIVERY_MODE_ALWAYS,
    DELIVERY_MODE_ON_RECALL,
    DELIVERY_MODE_ON_TRIGGER,
    Memory,
)


def test_all_delivery_modes_tuple_matches_constants() -> None:
    """``_ALL_DELIVERY_MODES`` enumerates exactly the three named constants.

    Registration order is fixed (always, on_recall, on_trigger); new modes are
    APPENDED (never reordered) to preserve byte-identity with the migration
    literal.
    """
    assert _ALL_DELIVERY_MODES == (
        DELIVERY_MODE_ALWAYS,
        DELIVERY_MODE_ON_RECALL,
        DELIVERY_MODE_ON_TRIGGER,
    )


def test_delivery_mode_values_are_orthogonal_not_types() -> None:
    """The delivery_mode values are the design-fixed orthogonal set (242cb28a)."""
    assert DELIVERY_MODE_ALWAYS == "always"
    assert DELIVERY_MODE_ON_RECALL == "on_recall"
    assert DELIVERY_MODE_ON_TRIGGER == "on_trigger"


def test_valid_delivery_mode_check_constraint_matches_migration_literal() -> None:
    """``valid_delivery_mode`` CHECK text is byte-identical to the migration.

    The CHECK is derived from ``_ALL_DELIVERY_MODES`` via f-string (registration
    order, single quotes, exact whitespace). This pins the exact output against
    the literal the alembic migration installs on production, so create_all and
    the migration head stay in sync and autogenerate sees no drift.
    """
    expected = "delivery_mode IN ('always', 'on_recall', 'on_trigger')"

    check = next(
        c for c in Memory.__table_args__ if getattr(c, "name", None) == "valid_delivery_mode"
    )
    # ``.sqltext.text`` is the raw CheckConstraint string (no SQLAlchemy
    # compilation), so byte-identical comparison stays stable across upgrades.
    assert check.sqltext.text == expected
