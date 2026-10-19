"""``?context=<uuid>`` parsing for the connection default context (#2125).

Same rules as ``?guardrails=`` (``test_guardrail_digest.py::test_select_*``):
pure, first value wins, never raises, keeps only the length of a bad value.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from mcp_server.tools._default_context import (
    NO_DEFAULT_CONTEXT,
    DefaultContextSelection,
    select_default_context,
)


@pytest.mark.parametrize("query", [None, b"", "", b"profile=core", b"guardrails=off&tools=recall"])
def test_absent_parameter_is_no_default(query):
    assert select_default_context(query) == NO_DEFAULT_CONTEXT
    assert NO_DEFAULT_CONTEXT == DefaultContextSelection("none")


def test_a_uuid_is_the_url_default():
    ctx = uuid4()
    assert select_default_context(f"context={ctx}".encode()) == DefaultContextSelection("url", ctx)


def test_surrounding_whitespace_is_stripped():
    ctx = uuid4()
    selection = select_default_context(f"profile=core&context=%20{ctx}%20".encode())
    assert selection == DefaultContextSelection("url", ctx)


def test_first_value_wins():
    first, second = uuid4(), uuid4()
    selection = select_default_context(f"context={first}&context={second}")
    assert selection.context_id == first


def test_first_value_wins_even_when_it_is_invalid():
    selection = select_default_context(f"context=nope&context={uuid4()}")
    assert selection.mode == "invalid"


@pytest.mark.parametrize("value", ["", "   ", "my-project", "1234", "b3abeabe-7ab1-44bd-8e52"])
def test_a_value_that_is_not_a_uuid_is_invalid_and_keeps_only_its_length(value):
    selection = select_default_context(f"context={value}")
    assert selection.mode == "invalid"
    assert selection.context_id is None
    assert selection.raw_length == len(value)
    assert value not in repr(selection) or value == ""


def test_undecodable_bytes_do_not_raise():
    selection = select_default_context(b"context=\xff\xfe&junk=\x80")
    assert selection.mode == "invalid"


def test_other_parameters_named_like_it_are_not_read():
    assert select_default_context(f"context_id={uuid4()}").mode == "none"
    assert select_default_context(f"guardrails={uuid4()}").mode == "none"
