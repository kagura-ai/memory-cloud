"""Titles and standard MCP ``ToolAnnotations`` for every tool (#1683).

``get_tool_definitions()`` attaches these on its way out, so the per-tool
entries in ``_definitions.py`` stay schema and prose only. Every tool gets a
top-level ``title`` (MCP 2025-06-18 and later) and the same string as
``annotations.title`` (2025-03-26 and later), plus all four hints written out:
an absent hint means the spec's conservative default, which says nothing about
the tool.

What counts as modifying the environment (``readOnlyHint=false``): changing
stored memories, contexts, edges, files, secrets and grants, agents and
bindings, settings, or learned state that changes later results — the Hebbian
graph edges and working → persistent promotions a ``recall`` writes, or a
``feedback`` row the ranking reads. Usage and audit logging, access bookkeeping
(``access_count``, ``reference_count``, ``last_used_at``) and sweeping
already-expired state do not count, although re-ranking and consolidation read
those counters later: the call changes nothing a user stored or can see.
``destructiveHint`` is true when a call can remove or overwrite existing data —
soft delete, a value overwritten, a grant revoked — including branches picked by
arguments (``forget(query=...)``, ``ingest_events`` delete / upsert,
``merge_contexts(delete_source=true)``); a call that only adds rows is not
destructive. ``idempotentHint`` is true only when repeating the call with the
same arguments changes nothing further (set-to-value updates, deleting a named
target); creates that mint new ids or tokens are not. ``openWorldHint`` is true
when a tool connects this server to an outside system (``setup_connector``, a
third-party chat platform). The model providers the server calls to process
data it already holds (embedding, reranking, analysis labelling) do not make a
tool open-world.

These are hints for a client's confirmation UI, not authorization: every tool
keeps its role checks, and a client is free to ignore them.

The legacy top-level ``readOnly`` flag predates the standard hints. It stays for
clients that read it, derived from ``readOnlyHint`` (present and ``true``
exactly when ``readOnlyHint`` is), so the two never disagree.
"""

from typing import Any


def _hints(
    title: str, *, read_only: bool, destructive: bool, idempotent: bool, open_world: bool
) -> dict[str, Any]:
    return {
        "title": title,
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "idempotentHint": idempotent,
        "openWorldHint": open_world,
    }


def _read(title: str) -> dict[str, Any]:
    """Changes nothing a user stored (see the module docstring)."""
    return _hints(title, read_only=True, destructive=False, idempotent=True, open_world=False)


def _additive(title: str, *, idempotent: bool = False, open_world: bool = False) -> dict[str, Any]:
    """Writes, but only adds: nothing existing is removed or overwritten."""
    return _hints(
        title, read_only=False, destructive=False, idempotent=idempotent, open_world=open_world
    )


def _destructive(title: str, *, idempotent: bool) -> dict[str, Any]:
    """Can remove or overwrite existing data, on some argument branch at least."""
    return _hints(title, read_only=False, destructive=True, idempotent=idempotent, open_world=False)


# Registry order. ``tests/mcp_server/test_tool_annotations.py`` requires exactly
# one entry per tool and pins the classifications a reviewer would question.
TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "list_my_bindings": _read("List My Key Bindings"),
    "describe_binding": _read("Describe Key Binding"),
    "remember": _additive("Store Memory"),  # supersedes adds an edge; the old memory is untouched
    # external_id mode replaces the memory with a new one on every call.
    "update_memory": _destructive("Update Memory", idempotent=False),
    # Writes Hebbian graph edges and promotes working memories it returns.
    "recall": _additive("Search Memories"),
    "reference": _read("Read Memory"),  # bumps reference_count: bookkeeping
    "recall_upcoming": _read("List Upcoming Memories"),
    "recall_nearby": _read("List Nearby Memories"),
    "load_pinned": _read("Load Pinned Memories"),
    "load_guardrails": _read("Load Guardrails"),
    # query mode deletes the current top-k, so a repeat deletes the next ones.
    "forget": _destructive("Forget Memories", idempotent=False),
    "explore": _read("Explore Memory Graph"),  # bumps access_count: bookkeeping
    "list_edges": _read("List Memory Edges"),
    # Overwrites an automatic edge's type and weight, or a declared one with overwrite=true.
    "create_edge": _destructive("Create Memory Edge", idempotent=True),
    "update_edge": _destructive("Update Memory Edge", idempotent=True),
    "delete_edge": _destructive("Delete Memory Edge", idempotent=True),
    "get_context_info": _read("Get Context Info"),
    "list_contexts": _read("List Contexts"),
    "list_tags": _read("List Tags"),
    "create_context": _additive("Create Context"),
    "update_context": _destructive("Update Context", idempotent=True),
    "delete_context": _destructive("Delete Context", idempotent=True),
    # Copies again on every call; delete_source=true soft-deletes the source.
    "merge_contexts": _destructive("Merge Contexts", idempotent=False),
    "update_search_config": _destructive("Update Search Config", idempotent=True),
    "get_usage": _read("Get Usage"),
    "get_sleep_history": _read("Get Sleep History"),
    "get_sleep_report": _read("Get Sleep Report"),
    # A rolled-back report is refused, so a repeat changes nothing.
    "rollback_sleep_run": _destructive("Roll Back Sleep Run", idempotent=True),
    "setup_resource": _additive("Set Up Resource"),
    # Stores a chat platform's OAuth tokens for a connector that reads from it.
    "setup_connector": _additive("Set Up Connector", open_world=True),
    "ingest_events": _destructive("Ingest Resource Events", idempotent=False),
    "get_resource_impact": _read("Get Resource Impact"),
    "get_resource_schema": _read("Get Resource Schema"),
    "list_resource_tokens": _read("List Resource Tokens"),
    "analyze_context": _additive("Analyze Context"),  # dry_run=true writes nothing
    "get_analysis": _read("Get Analysis"),
    "list_analyses": _read("List Analyses"),
    "get_active_analysis": _read("Get Active Analysis"),
    "get_cluster": _read("Get Analysis Cluster"),
    "init_file_upload": _additive("Start File Upload"),
    "complete_file_upload": _additive("Complete File Upload", idempotent=True),
    "get_file_download_url": _read("Get File Download URL"),
    "delete_file": _destructive("Delete File", idempotent=True),
    "list_files": _read("List Files"),
    "feedback": _additive("Record Recall Feedback"),  # re-ranking reads it
    "set_state": _destructive("Set Agent State", idempotent=True),  # upsert overwrites the key
    "get_state": _read("Get Agent State"),
    "record_measurement": _additive("Record Measurement"),
    "recall_series": _read("Get Measurement Series"),
    "register_agent": _additive("Register Agent"),
    "list_agents": _read("List Agents"),
    "get_agent": _read("Get Agent"),
    "update_agent": _destructive("Update Agent", idempotent=True),
    "delete_agent": _destructive("Delete Agent", idempotent=True),
    "bind_agent_context": _additive("Bind Agent to Context"),
    "list_agent_bindings": _read("List Agent Bindings"),
    "update_agent_binding": _destructive("Update Agent Binding", idempotent=True),
    "unbind_agent_context": _destructive("Unbind Agent from Context", idempotent=True),
    # With a query it runs recall, with recall's learning writes.
    "get_agent_bootstrap": _additive("Get Agent Bootstrap"),
    "secret_register_pubkey": _additive("Register Secret Public Key"),
    # New version each call; grants not listed are revoked.
    "secret_put": _destructive("Store Secret", idempotent=False),
    "secret_get": _read("Get Secret"),  # writes an audit entry only
    "secret_list": _read("List Secrets"),
    "secret_revoke_grant": _destructive("Revoke Secret Grant", idempotent=True),
}


def annotate_tool_definitions(tools: list[dict]) -> list[dict]:
    """Attach ``title``, ``annotations`` and the legacy ``readOnly`` in place.

    Raises:
        KeyError: A tool has no ``TOOL_ANNOTATIONS`` entry — add one when adding
            a tool.
    """
    for tool in tools:
        name = tool["name"]
        if name not in TOOL_ANNOTATIONS:
            raise KeyError(f"MCP tool {name!r} has no entry in TOOL_ANNOTATIONS")
        annotations = dict(TOOL_ANNOTATIONS[name])  # a fresh copy per tools/list
        tool["title"] = annotations["title"]
        tool["annotations"] = annotations
        if annotations["readOnlyHint"]:
            tool["readOnly"] = True
        else:
            tool.pop("readOnly", None)
    return tools
