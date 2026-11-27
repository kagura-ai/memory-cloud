"""``never_delivered`` in the list filter compiler and the shared list scope (#3022)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy import and_, func, select
from sqlalchemy.dialects import postgresql

from models.memory import Memory
from services import never_delivered as nd_module
from services.memory_listing import compile_memory_filters, listing_scope, recall_row_predicates
from services.never_delivered import never_delivered_predicates

_CTX = UUID("11111111-1111-1111-1111-111111111111")


def _sql(preds) -> str:
    stmt = select(func.count()).select_from(Memory).where(and_(*preds))
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _frozen_now() -> datetime:
    return datetime(2026, 11, 27, 9, 30, 0)


def test_the_filter_compiles_to_the_predicate():
    now = _frozen_now()
    with (
        patch.object(nd_module, "utcnow", return_value=now),
        patch.object(nd_module, "exempt_tags", return_value=frozenset({"status:open"})),
    ):
        compiled = compile_memory_filters({"never_delivered": True}, viewer_user_id="u")
    expected = never_delivered_predicates("u", now=now, exempt=frozenset({"status:open"}))
    assert _sql(compiled) == _sql(expected)


def test_without_a_viewer_the_key_is_refused():
    with pytest.raises(ValueError, match="never_delivered is not supported here"):
        compile_memory_filters({"never_delivered": True})


@pytest.mark.parametrize("value", [False, "true", 1, None, 0, [True]])
def test_only_true_is_accepted(value):
    with pytest.raises(ValueError, match="never_delivered accepts true only"):
        compile_memory_filters({"never_delivered": value}, viewer_user_id="u")


def test_it_ands_with_the_other_filters():
    sql = _sql(
        compile_memory_filters(
            {"type": "note", "tags": ["a"], "never_delivered": True}, viewer_user_id="u"
        )
    )
    assert "memories.type IN ('note')" in sql
    assert "memories.tags && " in sql
    assert "memories.first_delivered_at IS NULL" in sql


def test_recall_rows_ignore_the_key():
    assert recall_row_predicates({"never_delivered": True}) == []
    sql = _sql(recall_row_predicates({"never_delivered": True, "type": "note"}))
    assert "first_delivered_at" not in sql
    assert "memories.type IN ('note')" in sql


def test_the_scope_without_owner_and_binding():
    sql = _sql(listing_scope(context_id=_CTX, owner_user_id=None, binding=None))
    assert sql.endswith(
        "WHERE memories.deleted_at IS NULL "
        "AND memories.context_id = '11111111-1111-1111-1111-111111111111'"
    )


def test_the_scope_with_owner_and_binding_in_that_order():
    binding = Memory.type.in_(["note"])
    preds = listing_scope(context_id=_CTX, owner_user_id="owner-1", binding=binding)
    assert len(preds) == 4
    sql = _sql(preds)
    order = [
        "memories.deleted_at IS NULL",
        "memories.context_id = ",
        "memories.user_id = 'owner-1'",
        "memories.type IN ('note')",
    ]
    positions = [sql.index(f) for f in order]
    assert positions == sorted(positions)


def test_the_scope_with_a_binding_only():
    preds = listing_scope(context_id=_CTX, owner_user_id=None, binding=Memory.type.in_(["x"]))
    assert len(preds) == 3
    assert "memories.user_id" not in _sql(preds)
