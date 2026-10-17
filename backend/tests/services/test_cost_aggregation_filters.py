"""#2019: both cost legs filter from ONE bound-parameter dict.

The Sleep and ``llm_call_log`` legs share the query's parameters, so their
WHERE fragments must be derived from the same dict, never from two dicts that
merely happen to use the same names.
"""

from uuid import uuid4

from services.cost_aggregation_service import _filter_clauses, _filter_params


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
