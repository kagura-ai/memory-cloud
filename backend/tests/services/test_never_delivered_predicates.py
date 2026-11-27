"""The never-delivered predicate (#3022): every clause, in the decided order, written once."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, select
from sqlalchemy.dialects import postgresql

from config.constants import NEVER_DELIVERED_AGE_DAYS
from models.memory import (
    Memory,
    not_pinned_predicate,
    not_resource_owned_predicate,
    not_tool_triggered_predicate,
)
from services.never_delivered import never_delivered_predicates


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _now() -> datetime:
    return datetime(2026, 11, 27, 12, 0, 0)


def _all(now: datetime | None = None, exempt=frozenset({"status:open"})) -> str:
    """The predicates in a statement over ``memories``, as the callers run them."""
    preds = never_delivered_predicates("viewer-1", now=now or _now(), exempt=exempt)
    return _sql(select(func.count()).select_from(Memory).where(*preds))


def test_the_threshold_is_180_days():
    assert NEVER_DELIVERED_AGE_DAYS == 180


def test_eleven_clauses_with_exempt_tags_ten_without():
    now = _now()
    assert len(never_delivered_predicates("v", now=now, exempt={"status:open"})) == 11
    assert len(never_delivered_predicates("v", now=now, exempt=frozenset())) == 10


def test_every_clause_is_present():
    sql = _all()
    cutoff = _now() - timedelta(days=NEVER_DELIVERED_AGE_DAYS)
    assert "memories.user_id = 'viewer-1'" in sql
    assert "identity_links" in sql
    assert f"memories.created_at < '{cutoff.isoformat(sep=' ')}'" in sql
    assert "memories.first_delivered_at IS NULL" in sql
    assert "memories.access_count = 0" in sql
    assert "memories.reference_count = 0" in sql
    assert "memories.last_used_at IS NULL" in sql
    assert "memories.delivery_mode != 'always'" in sql
    assert "memories.type != 'time'" in sql
    assert "memories.resource_id IS NOT NULL" in sql
    assert (
        "memories.tags IS NULL OR NOT (memories.tags && CAST(ARRAY['status:open'] AS VARCHAR[]))"
    ) in sql
    assert "(memories.details -> 'tool_trigger') IS NULL" in sql
    assert "contexts.is_public IS true" in sql
    assert "neural_memory_edges.edge_type = 'supersedes'" in sql
    assert "neural_memory_edges.src_id = memories.id" in sql
    assert "neural_memory_edges.context_id = memories.context_id" in sql
    assert sql.count("NOT (EXISTS") == 2


def test_both_subqueries_correlate_to_the_outer_row():
    sql = _all()
    assert "FROM contexts \nWHERE" in sql
    assert "FROM neural_memory_edges \nWHERE" in sql
    assert sql.count("FROM memories") == 1


def test_the_cutoff_follows_the_injected_now():
    later = _now() + timedelta(days=3)
    cutoff = later - timedelta(days=NEVER_DELIVERED_AGE_DAYS)
    assert f"memories.created_at < '{cutoff.isoformat(sep=' ')}'" in _all(now=later)


def test_the_clauses_come_in_the_decided_order():
    sql = _all()
    fragments = [
        "memories.user_id =",
        "memories.created_at <",
        "memories.first_delivered_at IS NULL",
        "memories.access_count = 0",
        "memories.delivery_mode !=",
        "memories.type !=",
        "memories.resource_id IS NOT NULL",
        "memories.tags IS NULL",
        "'tool_trigger'",
        "contexts.is_public",
        "neural_memory_edges.edge_type",
    ]
    positions = [sql.index(f) for f in fragments]
    assert positions == sorted(positions), list(zip(fragments, positions, strict=True))
    first_exists = sql.index("NOT (EXISTS")
    assert sql.index("'tool_trigger'") < first_exists


def test_an_empty_exempt_set_emits_no_tags_clause():
    assert "memories.tags" not in _all(exempt=frozenset())


def test_the_lane_predicates_are_the_model_ones():
    preds = never_delivered_predicates("v", now=_now(), exempt=frozenset({"status:open"}))
    compiled = [_sql(p) for p in preds]
    assert _sql(not_pinned_predicate()) in compiled
    assert _sql(not_resource_owned_predicate()) in compiled
    assert _sql(not_tool_triggered_predicate()) in compiled


def test_the_viewer_is_inside_the_predicate():
    assert "memories.user_id = 'someone-else'" in _sql(
        and_(*never_delivered_predicates("someone-else", now=_now(), exempt=frozenset()))
    )


# Invariant: the rule is written once. Only the recorder and the predicate
# access the column.
_SRC = Path(__file__).resolve().parents[2] / "src"
_ACCESS = re.compile(r"\.first_delivered_at\b")


def test_only_the_recorder_and_the_predicate_access_the_column():
    hits = {
        p.relative_to(_SRC).as_posix()
        for p in _SRC.rglob("*.py")
        if _ACCESS.search(p.read_text(encoding="utf-8"))
    }
    assert hits == {
        "repositories/memory.py",
        "services/never_delivered.py",
    }
