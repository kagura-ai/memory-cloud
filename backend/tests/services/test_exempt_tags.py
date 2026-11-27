"""The exempt-tag helper (#3022): one reader of ``SLEEP_ARCHIVE_EXEMPT_TAGS`` for new code."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.dialects import postgresql

from services import exempt_tags as module
from services.exempt_tags import carries_exempt_tag, exempt_tags, not_exempt_tagged_predicate


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.fixture(autouse=True)
def _fresh_memo():
    module._parse.cache_clear()
    yield
    module._parse.cache_clear()


def test_default_is_status_open(monkeypatch):
    monkeypatch.delenv("SLEEP_ARCHIVE_EXEMPT_TAGS", raising=False)
    assert exempt_tags() == frozenset({"status:open"})


@pytest.mark.parametrize("raw", ["", "[]", " , "])
def test_an_empty_setting_means_no_exemption(monkeypatch, raw):
    monkeypatch.setenv("SLEEP_ARCHIVE_EXEMPT_TAGS", raw)
    assert exempt_tags() == frozenset()


def test_a_comma_list_is_read(monkeypatch):
    monkeypatch.setenv("SLEEP_ARCHIVE_EXEMPT_TAGS", "keep, status:open")
    assert exempt_tags() == frozenset({"keep", "status:open"})


def test_the_empty_set_predicate_is_true():
    assert _sql(not_exempt_tagged_predicate(frozenset())) == "true"


def test_the_predicate_is_null_safe_and_binds_the_tags_sorted():
    sql = _sql(not_exempt_tagged_predicate({"zeta", "alpha"}))
    assert sql.startswith("memories.tags IS NULL OR NOT (memories.tags && ")
    assert sql.index("'alpha'") < sql.index("'zeta'")


def test_the_predicate_sql_is_stable_across_set_orders():
    assert _sql(not_exempt_tagged_predicate(["b", "a"])) == _sql(
        not_exempt_tagged_predicate(("a", "b"))
    )


def test_carries_exempt_tag_matches_exactly():
    exempt = frozenset({"status:open"})
    assert carries_exempt_tag(["x", "status:open"], exempt) is True
    assert carries_exempt_tag(["Status:Open"], exempt) is False
    assert carries_exempt_tag(["status-open"], exempt) is False
    assert carries_exempt_tag([], exempt) is False
    assert carries_exempt_tag(None, exempt) is False
    assert carries_exempt_tag(["status:open"], frozenset()) is False


def test_the_parser_runs_once_per_distinct_raw_value(monkeypatch):
    monkeypatch.setenv("SLEEP_ARCHIVE_EXEMPT_TAGS", '["not closed')
    with patch.object(
        module, "sleep_archive_exempt_tags_from_env", return_value=("status:open",)
    ) as parser:
        assert exempt_tags() == frozenset({"status:open"})
        assert exempt_tags() == frozenset({"status:open"})
        assert parser.call_count == 1
        monkeypatch.setenv("SLEEP_ARCHIVE_EXEMPT_TAGS", "keep")
        exempt_tags()
        assert parser.call_count == 2


def test_a_malformed_value_warns_once(monkeypatch):
    monkeypatch.setenv("SLEEP_ARCHIVE_EXEMPT_TAGS", '["not closed')
    with patch("neural.config.logger") as logger:
        assert exempt_tags() == frozenset({"status:open"})
        assert exempt_tags() == frozenset({"status:open"})
    assert logger.warning.call_count == 1
