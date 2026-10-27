"""Tool profiles: the endpoint URL picks which tools ``tools/list`` returns (#1601).

``tools/list`` used to hand all 64 definitions to every client. A client that
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

# Measured after #1602 trimmed the descriptions: the ``core`` list serializes to
# 29,112 characters (``json.dumps``, the encoding both transports put on the
# wire) against 85,001 for the full list — 46,141 and 114,674 before the trim.
# The budget is that figure plus 10% headroom: a description can grow a little,
# but a tool quietly joining ``CORE_TOOLS`` cannot. The per-tool and full-list
# budgets live in ``test_tool_definition_budget``.
# #1683 (a title and annotations on every tool) and #1685 (reference()'s paging
# parameters) took it to 32,999; a tool joining ``CORE_TOOLS`` still overshoots.
# #1743 (paging / max_chars parameters on the bounded tools) took it to 34,203.
# #1850 moved the manual into ``guide`` and trimmed every core description: 25,590.
# #1973 added create_context / update_context / delete_context: 41,088 (from 36,617).
# #2126 (bootstrap's format parameter) took it to 40,528.
CORE_CHAR_BUDGET = 43_100  # (#1973) 21 core tools

REGISTRY = [tool["name"] for tool in get_tool_definitions()]


def _names(query: bytes | str | None) -> list[str]:
    return [tool["name"] for tool in select_tool_definitions(query)]


@pytest.fixture
def logs():
    """The selector's log events, whatever ran earlier in the process.

    A repeat ``setup_logger()`` (an ``api.main`` import) keeps the processor
    list that cached loggers hold, so ``capture_logs`` reaches them (#2167).
    """
    with structlog.testing.capture_logs() as captured:
        yield captured


# ---------------------------------------------------------------- the core set


def test_core_tools_all_exist_in_the_registry():
    assert set(CORE_TOOLS) <= set(REGISTRY)


def test_core_tools_are_unique_and_in_registry_order():
    assert len(set(CORE_TOOLS)) == len(CORE_TOOLS)
    assert list(CORE_TOOLS) == [name for name in REGISTRY if name in CORE_TOOLS]


def test_core_tools_are_the_documented_set():
    assert set(CORE_TOOLS) == {
        "remember",
        "remember_batch",
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
        "create_context",
        "update_context",
        "delete_context",
        "feedback",
        "guide",
        "describe_tools",
        "list",
        "changes_since",
        "bootstrap",
    }


def test_context_lifecycle_is_in_core_but_merge_and_search_config_are_not():
    """#1973: a client on the default URL can create, rename and delete a
    context without reconnecting with ``?profile=full`` (the server
    instructions tell it to call ``create_context``). ``merge_contexts`` and
    ``update_search_config`` are rare and stay behind ``describe_tools``."""
    assert {"create_context", "update_context", "delete_context"} <= set(CORE_TOOLS)
    assert "merge_contexts" in REGISTRY and "merge_contexts" not in CORE_TOOLS
    assert "update_search_config" in REGISTRY and "update_search_config" not in CORE_TOOLS


def test_load_guardrails_stays_out_of_core():
    """Client hooks call ``load_guardrails`` through ``tools/call`` (a profile
    is a view and never gates a call); a core client's model has no reason to
    call it, so it does not pay for the schema on every session."""
    assert "load_guardrails" in REGISTRY
    assert "load_guardrails" not in CORE_TOOLS


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
    [None, b"", "", b"profile=core", b"session_id=mcp-1", b"unrelated=1&x=core"],
)
def test_no_selection_returns_the_core_list(query):
    """#1849: the default view is the core profile; ``full`` is one parameter away."""
    core = [tool for tool in get_tool_definitions() if tool["name"] in CORE_TOOLS]
    assert select_tool_definitions(query) == core
    # Byte-for-byte: what reaches the wire is the serialization.
    assert json.dumps(select_tool_definitions(query)) == json.dumps(core)


@pytest.mark.parametrize("query", [b"profile=full", "profile=full", b"session_id=x&profile=full"])
def test_profile_full_returns_the_whole_registry_unchanged(query):
    assert select_tool_definitions(query) == get_tool_definitions()
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
    # ``Recall`` must not fold into ``recall``: a lower-casing parser would
    # list both tools here, and would serve the second query instead of
    # rejecting it.
    assert _names(b"tools=Recall,remember") == ["remember"]
    with pytest.raises(ToolProfileError):
        select_tool_definitions(b"tools=Recall")


def test_first_value_wins_when_a_parameter_repeats():
    assert _names(b"tools=recall&tools=remember") == ["recall"]
    assert _names(b"profile=core&profile=full") == list(CORE_TOOLS)


def test_other_query_parameters_are_left_alone():
    assert _names(b"session_id=mcp-1&tools=recall&x=1") == ["recall"]


# ------------------------------------------------------------------- add_tools (#2354)


def _core_plus(*names: str) -> list[str]:
    wanted = set(CORE_TOOLS) | set(names)
    return [name for name in REGISTRY if name in wanted]


def test_add_tools_alone_adds_to_the_default_core_profile():
    assert "get_usage" not in CORE_TOOLS
    assert _names(b"add_tools=get_usage") == _core_plus("get_usage")


def test_add_tools_adds_to_an_explicit_core_profile_in_registry_order():
    assert _names(b"profile=core&add_tools=merge_contexts,list_edges") == _core_plus(
        "list_edges", "merge_contexts"
    )
    # Parameter order does not matter.
    assert _names(b"add_tools=list_edges&profile=core") == _core_plus("list_edges")


def test_add_tools_on_the_full_profile_is_the_whole_registry():
    assert _names(b"profile=full&add_tools=get_usage,no_such_tool") == REGISTRY


def test_add_tools_naming_a_core_tool_changes_nothing():
    assert _names(b"add_tools=recall,remember") == list(CORE_TOOLS)


@pytest.mark.parametrize(
    "query",
    [
        b"add_tools=get_usage%2Csecret_put",
        b"add_tools=+get_usage+,%20secret_put",
        b"add_tools=,get_usage,,secret_put,,get_usage",
    ],
)
def test_add_tools_names_are_decoded_trimmed_and_deduplicated(query):
    assert _names(query) == _core_plus("get_usage", "secret_put")


def test_add_tools_names_are_case_sensitive():
    assert _names(b"add_tools=Get_Usage") == list(CORE_TOOLS)


def test_add_tools_first_value_wins_when_it_repeats():
    assert _names(b"add_tools=get_usage&add_tools=secret_put") == _core_plus("get_usage")


@pytest.mark.parametrize(
    "query", [b"add_tools=no_such_tool", b"add_tools=", b"add_tools=,,", b"add_tools=%20"]
)
def test_add_tools_matching_nothing_is_not_an_error(query):
    """The base profile still answers: nothing to add is not a broken URL."""
    assert _names(query) == list(CORE_TOOLS)


def test_add_tools_unknown_names_are_ignored_and_logged(logs):
    assert _names(b"add_tools=get_usage,no_such_tool") == _core_plus("get_usage")

    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert event["param"] == "add_tools"
    assert event["unknown"] == ["no_such_tool"]
    assert event["unknown_count"] == 1


def test_add_tools_known_names_log_nothing(logs):
    _names(b"add_tools=get_usage")
    _names(b"profile=core&add_tools=get_usage")
    assert logs == []


def test_add_tools_reads_only_the_first_names(logs):
    padding = ",".join(f"bogus_{i}" for i in range(MAX_TOOL_NAMES))
    # ``secret_put`` sits past the cap, so it is never read.
    assert _names(f"add_tools=get_usage,{padding},secret_put") == _core_plus("get_usage")

    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert event["truncated"] is True


def test_tools_wins_over_add_tools_and_the_ignored_add_tools_is_logged(logs):
    assert _names(b"tools=recall&add_tools=get_usage") == ["recall"]
    assert _names(b"add_tools=get_usage&profile=full&tools=recall") == ["recall"]

    events = [e for e in logs if e["event"] == "mcp_tool_profile_add_tools_ignored"]
    assert len(events) == 2
    # The allowlist's own rules are unchanged: matching nothing is still an error.
    with pytest.raises(ToolProfileError):
        select_tool_definitions(b"tools=no_such_tool&add_tools=get_usage")


def test_add_tools_does_not_rescue_an_unknown_profile():
    with pytest.raises(ToolProfileError):
        select_tool_definitions(b"profile=minimal&add_tools=get_usage")


def test_add_tools_is_a_view_not_an_authorization_boundary():
    """``tools/call`` never reads the selection: this module only filters
    definitions, and ``add_tools`` can only widen the profile's list."""
    for query in (b"add_tools=get_usage", b"profile=core&add_tools=secret_put"):
        assert set(CORE_TOOLS) <= set(_names(query))


# --------------------------------------------------------------- unknown names


def test_unknown_names_are_ignored_and_logged_once(logs):
    assert _names(b"tools=recall,no_such_tool,also_missing") == ["recall"]

    events = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert len(events) == 1
    assert events[0]["log_level"] == "info"
    assert events[0]["unknown"] == ["no_such_tool", "also_missing"]
    assert events[0]["unknown_count"] == 2


def test_known_names_alone_log_nothing(logs):
    _names(b"tools=recall,remember")
    _names(b"profile=core")
    _names(None)
    assert logs == []

    # Not vacuous: the same capture does see an event when there is one.
    _names(b"tools=recall,no_such_tool")
    assert [e["event"] for e in logs] == ["mcp_tool_profile_names_ignored"]


def test_logged_unknown_names_are_capped(logs):
    query = "tools=recall," + ",".join(f"bogus_{i}" for i in range(60))
    assert _names(query) == ["recall"]

    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert len(event["unknown"]) == 20
    assert event["unknown_count"] == 60


def test_a_long_unknown_name_is_truncated_where_it_is_shown(logs):
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


def test_only_the_first_names_of_an_over_long_allowlist_are_read(logs):
    padding = ",".join(f"bogus_{i}" for i in range(MAX_TOOL_NAMES))
    # ``recall`` sits past the cap, so it is never read.
    assert _names(f"tools=remember,{padding},recall") == ["remember"]

    (event,) = [e for e in logs if e["event"] == "mcp_tool_profile_names_ignored"]
    assert event["unknown_count"] == MAX_TOOL_NAMES - 1
    assert event["truncated"] is True


def test_max_tool_names_is_one_hundred():
    assert MAX_TOOL_NAMES == 100


def test_a_query_with_thousands_of_parameters_is_still_served():
    """``parse_qsl`` has no field limit unless ``max_num_fields`` is passed, so
    a long query is parsed, not turned into an unhandled ``ValueError``."""
    padding = "&".join(f"p{i}=1" for i in range(5000))
    assert _names(f"{padding}&tools=recall") == ["recall"]
    assert _names(f"{padding}&profile=core") == list(CORE_TOOLS)
    with pytest.raises(ToolProfileError):
        select_tool_definitions(f"{padding}&profile=minimal")


def test_undecodable_query_bytes_do_not_raise():
    assert _names(b"tools=recall&junk=\xff\xfe") == ["recall"]
