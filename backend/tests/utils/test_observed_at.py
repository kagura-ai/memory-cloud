"""Pure-logic tests for utils/observed_at.py (#2330 WHEN axis).

The ``details.observed_at`` write-side contract, the column / response /
filter-bound readers, and the parity of the regex literals with the
generated column's SQL in the model and the migration.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from models.memory import OBSERVED_AT_SQL, Memory
from utils.observed_at import (
    DATE_RE,
    DATETIME_RE,
    ObservedAtValidationError,
    canonical_observed_at,
    column_value,
    normalize_observed_at,
    observed_at_from_details,
    parse_observed_bound,
)

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "f16_2330_observed_at.py"
)


class TestCanonical:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2024-03-07", "2024-03-07"),
            ("2024-03-07T10:00:00Z", "2024-03-07T10:00:00Z"),
            ("2024-03-07T10:00:00", "2024-03-07T10:00:00Z"),  # naive = UTC
            ("2024-03-07T10:00:00+09:00", "2024-03-07T01:00:00Z"),
            ("2024-03-07T01:00:00-05:00", "2024-03-07T06:00:00Z"),
            ("2024-03-07T10:00:00.987654Z", "2024-03-07T10:00:00Z"),
            ("2024-03-07T10:00:00.5+09:00", "2024-03-07T01:00:00Z"),
            ("  2024-03-07  ", "2024-03-07"),
            ("0999-01-01T00:00:00Z", "0999-01-01T00:00:00Z"),  # four-digit year
        ],
    )
    def test_accepted_forms(self, value, expected):
        assert canonical_observed_at(value) == expected

    @pytest.mark.parametrize(
        "value",
        [True, 20240307, 2024.0, None, "", "   ", "2024-3-7", "2024-02-30", "now", "x" * 41],
    )
    def test_rejected(self, value):
        with pytest.raises(ObservedAtValidationError):
            canonical_observed_at(value)

    @pytest.mark.parametrize(
        "value",
        [
            "20240307",  # basic form: would be stored as a datetime and miss a date filter
            "20240307T100000Z",
            "2024-W10-4",  # week date
            "2024-067",  # ordinal date
            "2024-03-07 10:00:00",  # space separator
            "2024-03-07T10:00Z",  # no seconds
            "2024-03-07T10",
            "2024-03-07T10:00:00+0900",  # basic offset
            "2024-03-07T10:00:00+09",
        ],
    )
    def test_only_the_extended_forms_are_accepted(self, value):
        with pytest.raises(ObservedAtValidationError):
            canonical_observed_at(value)

    def test_trailing_newline_is_stripped_on_input(self):
        assert canonical_observed_at("2024-03-07\n") == "2024-03-07"

    def test_an_offset_past_year_one_is_rejected_not_raised_raw(self):
        with pytest.raises(ObservedAtValidationError):
            canonical_observed_at("0001-01-01T00:00:00+09:00")

    def test_it_is_a_value_error(self):
        assert issubclass(ObservedAtValidationError, ValueError)


class TestNormalize:
    def test_none_and_absent_pass_through(self):
        assert normalize_observed_at(None) is None
        details = {"other": 1}
        assert normalize_observed_at(details) is details

    def test_writes_the_stored_form_back(self):
        assert normalize_observed_at({"observed_at": "2024-03-07T10:00:00+09:00"}) == {
            "observed_at": "2024-03-07T01:00:00Z"
        }

    def test_null_removes_the_key(self):
        assert normalize_observed_at({"observed_at": None, "k": 1}) == {"k": 1}

    def test_invalid_raises(self):
        with pytest.raises(ObservedAtValidationError):
            normalize_observed_at({"observed_at": "yesterday"})


class TestReaders:
    def test_column_value_pads_a_date(self):
        assert column_value({"observed_at": "2024-03-07"}) == "2024-03-07T00:00:00Z"
        assert column_value({"observed_at": "2024-03-07T10:00:00Z"}) == "2024-03-07T10:00:00Z"

    @pytest.mark.parametrize(
        "details",
        [
            None,
            [],
            {},
            {"observed_at": None},
            {"observed_at": "2024/03/07"},
            {"observed_at": 1},
            # The SQL regex ($ = end of string) gives NULL for these too.
            {"observed_at": "2024-03-07\n"},
            {"observed_at": "2024-03-07T10:00:00Z\n"},
        ],
    )
    def test_column_value_none_for_anything_else(self, details):
        assert column_value(details) is None

    def test_from_details_keeps_the_writers_precision(self):
        assert observed_at_from_details({"observed_at": "2024-03-07"}) == "2024-03-07"
        assert (
            observed_at_from_details({"observed_at": "2024-03-07T10:00:00Z"})
            == "2024-03-07T10:00:00Z"
        )

    @pytest.mark.parametrize(
        "details",
        [
            None,
            "x",
            {},
            {"observed_at": "2024-03-07T10:00:00+09:00"},
            {"observed_at": 5},
            {"observed_at": "2024-03-07\n"},
        ],
    )
    def test_from_details_none_for_legacy_values(self, details):
        assert observed_at_from_details(details) is None

    def test_bound_from_a_date_is_the_start_of_the_day(self):
        assert parse_observed_bound("2024-03-08", "observed_before") == (
            "2024-03-08T00:00:00Z",
            datetime(2024, 3, 8, tzinfo=UTC),
        )

    def test_bound_from_a_datetime_is_utc(self):
        column, moment = parse_observed_bound("2024-03-08T09:00:00+09:00", "observed_after")
        assert column == "2024-03-08T00:00:00Z"
        assert moment == datetime(2024, 3, 8, tzinfo=UTC)

    @pytest.mark.parametrize("value", [None, 1, True, "soon", ["2024-03-07"]])
    def test_bad_bound_names_the_filter(self, value):
        with pytest.raises(ObservedAtValidationError, match="observed_after"):
            parse_observed_bound(value, "observed_after")


class TestSqlParity:
    def test_the_regexes_are_the_columns(self):
        assert f"'{DATE_RE}'" in OBSERVED_AT_SQL
        assert f"'{DATETIME_RE}'" in OBSERVED_AT_SQL

    def test_the_model_column_uses_the_expression(self):
        computed = Memory.__table__.c.observed_at.computed
        assert computed is not None
        assert str(computed.sqltext) == OBSERVED_AT_SQL
        assert computed.persisted is True

    def test_the_migration_expression_is_byte_identical(self):
        spec = importlib.util.spec_from_file_location("f16_2330_observed_at", _MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.OBSERVED_AT_SQL == OBSERVED_AT_SQL

    def test_the_migration_copy_of_the_parser_agrees(self):
        spec = importlib.util.spec_from_file_location("f16_2330_observed_at", _MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value in (
            "2024-03-07",
            "2024-03-07T10:00:00+09:00",
            "2024-03-07T10:00:00.5",
            "0999-01-01T00:00:00Z",
            "2024-03-07\n",  # stripped on both sides
        ):
            assert module._canonical(value) == canonical_observed_at(value)
        for value in ("March 7", "20240307", "2024-03-07 10:00:00.5", "2024-W10-4"):
            assert module._canonical(value) is None
            with pytest.raises(ObservedAtValidationError):
                canonical_observed_at(value)
