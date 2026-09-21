"""Tool profiles: the endpoint URL picks which tools ``tools/list`` returns (#1601).

``tools/list`` used to hand all 63 definitions to every client. A client that
loads schemas eagerly pays for the whole list on every session, so its local
MCP configuration — the URL it already stores — can now ask for less:
``?profile=core`` or ``?tools=remember,recall``.

This file pins the selection rules of ``mcp_server.tools._profiles``; the two
transports are driven end to end in ``test_transport_tool_profiles``.
"""

from __future__ import annotations

import json

import pytest
import structlog

from mcp_server.tools import get_tool_definitions
from mcp_server.tools._profiles import (
    CORE_TOOLS,
    MAX_TOOL_NAMES,
    PROFILES,
    ToolProfileError,
    select_tool_definitions,
)

# Measured at v0.72.0: the ``core`` list serializes to 44,758 characters
# (``json.dumps``, the encoding both transports put on the wire) against
# 113,291 for the full list. The budget is that figure plus 10% headroom: a
# description can grow a little, but a tool quietly joining ``CORE_TOOLS``
# cannot.
CORE_CHAR_BUDGET = 49_200

REGISTRY = [tool["name"] for tool in get_tool_definitions()]


def _names(query: bytes | str | None) -> list[str]:
    return [tool["name"] for tool in select_tool_definitions(query)]


# ---------------------------------------------------------------- the core set


def test_core_tools_all_exist_in_the_registry():
    assert set(CORE_TOOLS) <= set(REGISTRY)


def test_core_tools_are_unique_and_in_registry_order():
    assert len(set(CORE_TOOLS)) == len(CORE_TOOLS)
    assert list(CORE_TOOLS) == [name for name in REGISTRY if name in CORE_TOOLS]


def test_core_tools_are_the_documented_set():
    assert set(CORE_TOOLS) == {
        "remember",
        "recall",
        "reference",
        "update_memory",
        "forget",
        "explore",
        "load_pinned",
        "recall_upcoming",
        "get_context_info",
        "list_contexts",
        "list_tags",
        "feedback",
    }


def test_profiles_are_full_and_core():
    assert list(PROFILES) == ["full", "core"]


def test_core_list_stays_under_its_character_budget():
    size = len(json.dumps(select_tool_definitions("profile=core")))
    assert size < CORE_CHAR_BUDGET, (
        f"core tools/list is {size} characters (budget {CORE_CHAR_BUDGET}). "
        "Trim the descriptions or take a tool out of CORE_TOOLS — do not just raise the budget."
    )


# --------------------------------------------------------------------- default


@pytest.mark.parametrize(
    "query",
    [None, b"", "", b"profile=full", "profile=full", b"session_id=mcp-1", b"unrelated=1&x=core"],
)
def test_no_selection_returns_todays_full_list_unchanged(query):
    assert select_tool_definitions(query) == get_tool_definitions()
    # Byte-for-byte: what reaches the wire is the serialization.
    assert json.dumps(select_tool_definitions(query)) == json.dumps(get_tool_definitions())


# ------------------------------------------------------------------------ core


@pytest.mark.parametrize("query", [b"profile=core", "profile=core", b"profile=%20core%20"])
def test_core_profile_lists_the_core_tools_in_registry_order(query):
    assert _names(query) == list(CORE_TOOLS)


def test_filtered_definitions_are_the_registry_entries_untouched():
    by_name = {tool["name"]: tool for tool in get_tool_definitions()}
    for tool in select_tool_definitions(b"profile=core"):
        assert tool == by_name[tool["name"]]


# ------------------------------------------------------------------- allowlist


def test_allowlist_returns_registry_order_not_request_order():
    assert _names(b"tools=reference,recall,remember") == ["remember", "recall", "reference"]


def test_allowlist_wins_over_profile():
    assert _names(b"profile=core&tools=get_usage") == ["get_usage"]
    # ``profile`` is not read at all once ``tools`` is given.
    assert _names(b"tools=recall&profile=no-such-profile") == ["recall"]


def test_allowlist_can_name_tools_outside_the_core_set():
    assert "secret_put" not in CORE_TOOLS
    assert _names(b"tools=secret_put") == ["secret_put"]


def test_duplicate_names_collapse():
    assert _names(b"tools=recall,recall,remember,recall") == ["remember", "recall"]


@pytest.mark.parametrize(
    "query",
    [
        b"tools=remember%2Crecall",  # percent-encoded comma
        b"tools=remember,%20recall",  # percent-encoded space
        b"tools=+remember+,+recall+",  # form-encoded spaces
        b"tools=,remember,,recall,",  # empty entries
    ],
)
def test_names_are_percent_decoded_and_trimmed(query):
    assert _names(query) == ["remember", "recall"]


def test_names_are_case_sensitive():
    assert _names(b"tools=Recall,recall") == ["recall"]


def test_first_value_wins_when_a_parameter_repeats():
    assert _names(b"tools=recall&tools=remember") == ["recall"]
    assert _names(b"profile=core&profile=full") == list(CORE_TOOLS)


def test_other_query_parameters_are_left_alone():
    assert _names(b"session_id=mcp-1&tools=recall&x=1") == ["recall"]


# --------------------------------------------------------------- unknown names


def test_unknown_names_are_ignored_and_logged_once():
    with structlog.testing.capture_logs() as logs:
        names = _names(b"tools=recall,no_such_tool,also_missing")

    assert names == ["recall"]
    events = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert len(events) == 1
    assert events[0]["log_level"] == "info"
    assert events[0]["unknown"] == ["no_such_tool", "also_missing"]
    assert events[0]["unknown_count"] == 2


def test_known_names_alone_log_nothing():
    with structlog.testing.capture_logs() as logs:
        _names(b"tools=recall,remember")
        _names(b"profile=core")
        _names(None)
    assert logs == []


def test_logged_unknown_names_are_capped():
    query = "tools=recall," + ",".join(f"bogus_{i}" for i in range(60))
    with structlog.testing.capture_logs() as logs:
        assert _names(query) == ["recall"]

    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert len(event["unknown"]) == 20
    assert event["unknown_count"] == 60


def test_a_long_unknown_name_is_truncated_where_it_is_shown():
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(ToolProfileError) as excinfo:
            select_tool_definitions("tools=" + "x" * 5000)

    assert len(excinfo.value.message) < 400
    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert all(len(name) <= 64 for name in event["unknown"])


# ---------------------------------------------------------------------- errors


@pytest.mark.parametrize("query", [b"tools=no_such_tool", b"tools=,,", b"tools=", b"tools=%20"])
def test_allowlist_matching_no_tool_is_an_error(query):
    with pytest.raises(ToolProfileError) as excinfo:
        select_tool_definitions(query)
    assert "tools" in excinfo.value.message
    assert str(excinfo.value) == excinfo.value.message


def test_empty_allowlist_error_names_the_unknown_tools():
    with pytest.raises(ToolProfileError) as excinfo:
        select_tool_definitions(b"tools=rememberr,recal")
    assert "rememberr" in excinfo.value.message
    assert "recal" in excinfo.value.message


@pytest.mark.parametrize("query", [b"profile=minimal", b"profile=Core", b"profile=", b"profile=*"])
def test_unknown_profile_is_an_error_naming_the_valid_profiles(query):
    with pytest.raises(ToolProfileError) as excinfo:
        select_tool_definitions(query)
    for profile in PROFILES:
        assert profile in excinfo.value.message


def test_unknown_profile_echo_is_length_capped():
    with pytest.raises(ToolProfileError) as excinfo:
        select_tool_definitions("profile=" + "p" * 5000)
    assert len(excinfo.value.message) < 300


# ------------------------------------------------------------- over-long input


def test_only_the_first_names_of_an_over_long_allowlist_are_read():
    padding = ",".join(f"bogus_{i}" for i in range(MAX_TOOL_NAMES))
    with structlog.testing.capture_logs() as logs:
        # ``recall`` sits past the cap, so it is never read.
        assert _names(f"tools=remember,{padding},recall") == ["remember"]

    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert event["unknown_count"] == MAX_TOOL_NAMES - 1
    assert event["truncated"] is True


def test_max_tool_names_is_one_hundred():
    assert MAX_TOOL_NAMES == 100


def test_undecodable_query_bytes_do_not_raise():
    assert _names(b"tools=recall&junk=\xff\xfe") == ["recall"]
