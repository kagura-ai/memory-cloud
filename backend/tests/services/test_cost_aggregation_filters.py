"""#2019: both cost legs filter from ONE bound-parameter dict.

The Sleep and ``llm_call_log`` legs share the query's parameters, so their
WHERE fragments must be derived from the same dict, never from two dicts that
merely happen to use the same names.
"""

from uuid import uuid4

from services.cost_aggregation_service import (
    COST_SOURCES,
    LEDGER_COST_SOURCES,
    _filter_clauses,
    _filter_params,
    _ledger_source_case,
)


def test_clauses_for_each_alias_come_from_the_same_params():
    params = _filter_params(workspace_id=uuid4(), user_id="u", source="sleep", paid_by="byok")
    assert set(params) == {"workspace_id", "user_id", "source", "paid_by"}
    assert _filter_clauses(params, alias="sr", source_column="source") == [
        "sr.workspace_id = :workspace_id",
        "sr.user_id = :user_id",
        "sr.source = :source",
        "sr.paid_by = :paid_by",
    ]
    assert _filter_clauses(params, alias="l", source_column="caller") == [
        "l.workspace_id = :workspace_id",
        "l.user_id = :user_id",
        "l.caller = :source",
        "l.paid_by = :paid_by",
    ]


def test_unset_filters_add_neither_params_nor_clauses():
    params = _filter_params(workspace_id=None, user_id=None, source=None, paid_by="platform")
    assert params == {"paid_by": "platform"}
    assert _filter_clauses(params, alias="l", source_column="caller") == ["l.paid_by = :paid_by"]


def test_ledger_sources_include_the_recall_selector_stage():
    """#2154: the selector stage (#2144) is a ledger source and a filter value."""
    assert LEDGER_COST_SOURCES == ("supersede_verify", "llm_selector")
    assert COST_SOURCES == ("sleep", "analysis", "supersede_verify", "llm_selector")


def test_ledger_source_case_matches_the_selector_by_caller_and_stage():
    """Recall writes embeddings under the same caller: only completion rows with
    ``stage='llm_selector'`` are the selector (call_type is in the leg's WHERE)."""
    assert _ledger_source_case() == (
        "CASE WHEN l.caller = 'supersede_verify' THEN 'supersede_verify' "
        "WHEN l.caller = 'recall' AND l.call_metadata ->> 'stage' = 'llm_selector' "
        "THEN 'llm_selector' END"
    )
