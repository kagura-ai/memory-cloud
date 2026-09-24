"""Every MCP tool carries a title and accurate standard annotations (#1683).

The classification rule lives in ``mcp_server.tools._annotations``'s docstring.
Here: the metadata contract every definition must meet, a table that covers
exactly the registry, the classifications a reviewer would question (deletes,
overwrites, argument-controlled branches, ``recall``'s learning writes), and
that tool profiles keep the metadata. The transport half — both ``tools/list``
paths — is in ``test_transport_tool_profiles``.
"""

from __future__ import annotations

import pytest

from mcp_server.tools import get_tool_definitions
from mcp_server.tools._annotations import TOOL_ANNOTATIONS, annotate_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS, select_tool_definitions

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def _by_name() -> dict[str, dict]:
    return {tool["name"]: tool for tool in get_tool_definitions()}


def _hints(name: str) -> dict:
    return _by_name()[name]["annotations"]


# -------------------------------------------------------------------- contract


def test_every_tool_has_a_title_and_all_four_hints():
    for tool in get_tool_definitions():
        name = tool["name"]
        title = tool.get("title")
        assert isinstance(title, str) and title.strip(), f"{name}: no title"
        annotations = tool.get("annotations")
        assert isinstance(annotations, dict), f"{name}: no annotations"
        assert set(annotations) == {"title", *HINTS}, f"{name}: {sorted(annotations)}"
        assert annotations["title"] == title, f"{name}: title and annotations.title differ"
        for hint in HINTS:
            assert isinstance(annotations[hint], bool), f"{name}.{hint} is not a boolean"


def test_names_are_unique_and_within_the_64_character_limit():
    names = [tool["name"] for tool in get_tool_definitions()]
    assert len(names) == len(set(names))
    assert max(len(name) for name in names) <= 64


def test_titles_are_unique():
    titles = [tool["title"] for tool in get_tool_definitions()]
    assert len(titles) == len(set(titles))


def test_the_table_covers_exactly_the_registry():
    """No stale entry for a removed tool, no missing entry for a new one."""
    assert set(TOOL_ANNOTATIONS) == {tool["name"] for tool in get_tool_definitions()}


def test_a_tool_without_an_entry_fails_loudly():
    with pytest.raises(KeyError, match="no_such_tool"):
        annotate_tool_definitions([{"name": "no_such_tool", "inputSchema": {}}])


def test_hints_are_coherent():
    """A read-only tool is trivially non-destructive and idempotent."""
    for tool in get_tool_definitions():
        annotations = tool["annotations"]
        if annotations["readOnlyHint"]:
            assert annotations["destructiveHint"] is False, tool["name"]
            assert annotations["idempotentHint"] is True, tool["name"]


def test_legacy_read_only_mirrors_read_only_hint():
    """Kept for clients that read it; present (and true) exactly when readOnlyHint is."""
    for tool in get_tool_definitions():
        assert tool.get("readOnly", False) is tool["annotations"]["readOnlyHint"], tool["name"]


def test_each_call_returns_fresh_annotation_objects():
    first = get_tool_definitions()
    name = first[0]["name"]
    first[0]["annotations"]["readOnlyHint"] = "mutated"
    first[0]["title"] = "mutated"
    second = get_tool_definitions()
    assert isinstance(TOOL_ANNOTATIONS[name]["readOnlyHint"], bool)
    assert second[0]["annotations"] == TOOL_ANNOTATIONS[name]
    assert second[0]["title"] == TOOL_ANNOTATIONS[name]["title"]


# -------------------------------------------------------------------- semantics

# Named in the issue: every delete / revoke / rollback / merge, and the
# argument-controlled ingest_events delete / upsert.
ISSUE_DESTRUCTIVE = [
    "forget",
    "delete_edge",
    "delete_context",
    "merge_contexts",
    "rollback_sleep_run",
    "ingest_events",
    "delete_file",
    "delete_agent",
    "unbind_agent_context",
    "secret_revoke_grant",
]

# Overwrite an existing value (soft delete and overwrite are not additive).
OVERWRITES = [
    "update_memory",
    "create_edge",
    "update_edge",
    "update_context",
    "update_search_config",
    "set_state",
    "update_agent",
    "update_agent_binding",
    "secret_put",
]

# Destructive only on a branch the arguments pick, and not safe to repeat:
# forget(query=) deletes the next top-k, update_memory(external_id=) replaces
# the memory again, merge_contexts copies again, ingest_events appends events,
# secret_put mints a version and revokes unlisted grants.
CONDITIONAL_NON_IDEMPOTENT = [
    "forget",
    "update_memory",
    "merge_contexts",
    "ingest_events",
    "secret_put",
]

# Only add rows.
ADDITIVE = [
    "remember",
    "create_context",
    "setup_resource",
    "setup_connector",
    "analyze_context",
    "init_file_upload",
    "complete_file_upload",
    "feedback",
    "record_measurement",
    "register_agent",
    "bind_agent_context",
    "secret_register_pubkey",
]

CLEAR_READS = [
    "list_contexts",
    "get_context_info",
    "list_tags",
    "reference",
    "explore",
    "list_edges",
    "load_pinned",
    "load_guardrails",
    "recall_upcoming",
    "recall_nearby",
    "get_state",
    "recall_series",
    "get_usage",
    "list_files",
    "get_file_download_url",
    "list_my_bindings",
    "describe_binding",
    "secret_list",
]


@pytest.mark.parametrize("name", ISSUE_DESTRUCTIVE + OVERWRITES)
def test_deletes_and_overwrites_are_destructive(name):
    hints = _hints(name)
    assert hints["readOnlyHint"] is False
    assert hints["destructiveHint"] is True


@pytest.mark.parametrize("name", CONDITIONAL_NON_IDEMPOTENT)
def test_argument_controlled_branches_are_destructive_and_not_idempotent(name):
    hints = _hints(name)
    assert hints["destructiveHint"] is True
    assert hints["idempotentHint"] is False


@pytest.mark.parametrize("name", ADDITIVE)
def test_additive_writes_are_neither_read_only_nor_destructive(name):
    hints = _hints(name)
    assert hints["readOnlyHint"] is False
    assert hints["destructiveHint"] is False


@pytest.mark.parametrize("name", CLEAR_READS)
def test_clear_reads_are_read_only(name):
    assert _hints(name)["readOnlyHint"] is True


@pytest.mark.parametrize("name", ["recall", "get_agent_bootstrap"])
def test_recall_is_not_read_only_because_it_learns(name):
    """recall persists Hebbian graph edges and promotes working memories it
    returns (``MemoryService._recall_run_hebbian_learning`` /
    ``_check_and_promote``); both change later results, so it is not read-only.
    Nothing is removed or overwritten, and a repeat strengthens the graph
    further, so: not destructive, not idempotent. get_agent_bootstrap runs the
    same recall when given a query. The legacy ``readOnly`` flag used to say
    true for both; a client that confirms non-read-only tools now asks first.
    """
    hints = _hints(name)
    assert hints["readOnlyHint"] is False
    assert hints["destructiveHint"] is False
    assert hints["idempotentHint"] is False
    assert "readOnly" not in _by_name()[name]


def test_an_audited_read_stays_read_only():
    """secret_get writes a tamper-evident audit entry and nothing else; audit
    logging does not count as modifying the environment."""
    assert _hints("secret_get")["readOnlyHint"] is True


def test_only_the_chat_connector_is_open_world():
    open_world = sorted(
        tool["name"] for tool in get_tool_definitions() if tool["annotations"]["openWorldHint"]
    )
    assert open_world == ["setup_connector"]


# --------------------------------------------------------------------- profiles


@pytest.mark.parametrize(
    "query",
    [None, b"profile=full", b"profile=core", b"tools=forget,recall,secret_get,setup_connector"],
)
def test_profiles_keep_the_metadata(query):
    selected = select_tool_definitions(query)
    assert selected
    for tool in selected:
        assert tool["annotations"] == TOOL_ANNOTATIONS[tool["name"]], tool["name"]
        assert tool["title"] == TOOL_ANNOTATIONS[tool["name"]]["title"]


def test_the_core_profile_keeps_its_destructive_tool_marked():
    core = {tool["name"]: tool for tool in select_tool_definitions(b"profile=core")}
    assert set(core) == set(CORE_TOOLS)
    assert core["forget"]["annotations"]["destructiveHint"] is True
