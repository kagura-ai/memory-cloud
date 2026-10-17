"""Pins for the ``llm_call_log`` caller vocabulary (#474, #2019).

``LLM_CALL_LOG_CALLERS``, the model's ``valid_llm_call_log_caller`` CHECK and
the latest alembic migration that rewrites it (``f00_2019_llm_log_supersede``,
compared by ``tests/test_schema_drift.py``) must agree byte for byte; a new
caller is APPENDED, never reordered.
"""

from models.llm_call_log import LLM_CALL_LOG_CALLERS, LLMCallLog


def _check(name: str) -> str:
    c = next(c for c in LLMCallLog.__table_args__ if getattr(c, "name", None) == name)
    return c.sqltext.text


def test_caller_tuple_is_appended_never_reordered():
    assert LLM_CALL_LOG_CALLERS == (
        "sleep",
        "recall",
        "rerank",
        "ask",
        "admin",
        "supersede_verify",  # #2019 (appended, never reordered)
    )


def test_caller_check_matches_tuple_and_migration():
    expected = "caller IN ('sleep', 'recall', 'rerank', 'ask', 'admin', 'supersede_verify')"
    assert _check("valid_llm_call_log_caller") == expected
    assert expected == "caller IN ({})".format(", ".join(f"'{c}'" for c in LLM_CALL_LOG_CALLERS))


def test_caller_fits_the_column():
    width = LLMCallLog.__table__.c.caller.type.length
    assert all(len(c) <= width for c in LLM_CALL_LOG_CALLERS)
