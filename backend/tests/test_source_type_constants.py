"""Regression tests for #887: source_type provenance constant + CHECK + forge guard.

``source_type`` is server-authoritative provenance on Memory:
``file`` | ``url`` | ``vault`` | ``api`` | ``manual`` | ``connector``. Like
``delivery_mode`` / edge ``origin``, the DB CHECK is derived from an ordered
Python tuple (``_ALL_SOURCE_TYPES``) so ``create_all()`` stays byte-identical to
the alembic head. ``connector`` is server-only (resource_indexer); the request
schema must not let a client forge it.
"""

import pytest
from pydantic import ValidationError

from models.memory import (
    _ALL_SOURCE_TYPES,
    SOURCE_TYPE_API,
    SOURCE_TYPE_CONNECTOR,
    SOURCE_TYPE_FILE,
    SOURCE_TYPE_MANUAL,
    SOURCE_TYPE_URL,
    SOURCE_TYPE_VAULT,
    Memory,
)
from models.schemas import RememberRequest


def test_all_source_types_tuple_matches_constants() -> None:
    assert _ALL_SOURCE_TYPES == (
        SOURCE_TYPE_FILE,
        SOURCE_TYPE_URL,
        SOURCE_TYPE_VAULT,
        SOURCE_TYPE_API,
        SOURCE_TYPE_MANUAL,
        SOURCE_TYPE_CONNECTOR,
    )


def test_valid_source_type_check_constraint_matches_migration_literal() -> None:
    """``valid_source_type`` CHECK text is byte-identical to the migration literal."""
    expected = "source_type IN ('file', 'url', 'vault', 'api', 'manual', 'connector')"
    check = next(
        c for c in Memory.__table_args__ if getattr(c, "name", None) == "valid_source_type"
    )
    assert check.sqltext.text == expected


def test_client_cannot_forge_connector_source_type() -> None:
    """The request schema only permits user-origin values — ``connector`` is
    server-only, so a caller on the remember path cannot forge external-ingestion
    provenance (the core #887 trust-integrity guarantee)."""
    with pytest.raises(ValidationError) as exc:
        RememberRequest(
            summary="a valid summary",
            content="c",
            type="note",
            source_type="connector",
        )
    # Fail specifically on source_type (not an unrelated missing/short field).
    assert "source_type" in str(exc.value)


@pytest.mark.parametrize("value", ["file", "url", "vault", "api", "manual"])
def test_user_origin_source_types_are_accepted(value: str) -> None:
    """User-origin provenance values stay client-settable (Obsidian vault / file /
    url imports — #213/#262 — must not regress)."""
    req = RememberRequest(summary="a valid summary", content="c", type="note", source_type=value)
    assert req.source_type == value
