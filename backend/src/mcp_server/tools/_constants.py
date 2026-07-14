"""Constants for MCP tools — timeouts, instructions, and usage guide.

Extracted from tools.py for modularity (Issue #7).
"""

import os
from typing import TypeVar

T = TypeVar("T")


def _get_timeout(tool_name: str, default: float) -> float:
    """Get timeout from environment variable or use default."""
    env_key = f"MCP_TIMEOUT_{tool_name.upper()}"
    return float(os.getenv(env_key, default))


# Issue #163: Tool execution timeouts (seconds)
TOOL_TIMEOUTS: dict[str, float] = {
    "remember": _get_timeout("remember", 30.0),
    "update_memory": _get_timeout("update_memory", 30.0),
    "recall": _get_timeout("recall", 60.0),
    "forget": _get_timeout("forget", 15.0),
    "reference": _get_timeout("reference", 10.0),
    "explore": _get_timeout("explore", 45.0),
    "get_context_info": _get_timeout("get_context_info", 15.0),
    "get_agent_bootstrap": _get_timeout("get_agent_bootstrap", 15.0),  # #1276 (RFC-0002 P0-3)
    "list_contexts": _get_timeout("list_contexts", 10.0),
    "list_tags": _get_timeout("list_tags", 10.0),  # Issue #614
    "create_context": _get_timeout("create_context", 15.0),
    "update_context": _get_timeout("update_context", 15.0),
    "update_search_config": _get_timeout("update_search_config", 10.0),
    "list_edges": _get_timeout("list_edges", 10.0),
    "create_edge": _get_timeout("create_edge", 10.0),
    "update_edge": _get_timeout("update_edge", 10.0),
    "delete_edge": _get_timeout("delete_edge", 10.0),
    "delete_context": _get_timeout("delete_context", 30.0),
    "merge_contexts": _get_timeout("merge_contexts", 60.0),
    "get_usage": _get_timeout("get_usage", 10.0),
    "get_sleep_history": _get_timeout("get_sleep_history", 10.0),
    "get_sleep_report": _get_timeout("get_sleep_report", 15.0),
    "rollback_sleep_run": _get_timeout("rollback_sleep_run", 120.0),
    "setup_resource": _get_timeout("setup_resource", 20.0),
    "ingest_events": _get_timeout("ingest_events", 30.0),
    "get_resource_impact": _get_timeout("get_resource_impact", 10.0),
    "get_resource_schema": _get_timeout("get_resource_schema", 10.0),
    "list_resource_tokens": _get_timeout("list_resource_tokens", 10.0),
    # Issue #485: file storage tools
    "init_file_upload": _get_timeout("init_file_upload", 20.0),
    "complete_file_upload": _get_timeout("complete_file_upload", 20.0),
    "get_file_download_url": _get_timeout("get_file_download_url", 10.0),
    "delete_file": _get_timeout("delete_file", 15.0),
    "list_files": _get_timeout("list_files", 10.0),
}
DEFAULT_TOOL_TIMEOUT = float(os.getenv("MCP_TIMEOUT_DEFAULT", 60.0))


def get_tool_timeout(tool_name: str) -> float:
    """Get timeout for a specific tool.

    Args:
        tool_name: Name of the MCP tool

    Returns:
        Timeout in seconds for the tool
    """
    return TOOL_TIMEOUTS.get(tool_name, DEFAULT_TOOL_TIMEOUT)


# Issue #215, #240: Instructions for AI clients
# Returned by get_context_info() to help AI clients use memory tools effectively
KAGURA_MEMORY_INSTRUCTIONS = """# Kagura Memory Cloud - Quick Reference

## Session Start
Call get_context_info() once to load:
- context.usage_guide: How to use this context
- context.is_private: Privacy setting (true=only you, false=workspace members can see)
- instructions: General best practices (this guide)

## Core Workflow
1. recall() - Search before starting tasks
2. remember() - Store important decisions/code
3. update_memory() - Modify existing memories (in-place or upsert)
4. explore() - Find related memories via graph traversal

## remember() Tips
- summary: Write reusable conclusions (not process)
  ✅ "JWT expiry caused 401. Fixed with refresh token rotation."
  ❌ "Discussed auth errors in meeting."
- importance: 0.9+ critical, 0.6-0.8 useful, 0.3-0.5 reference
- tags: Include project/domain tags for filtering

## recall() Tips
- Use HyDE: Generate hypothetical answer, then search with it
- Expand queries with related terms
- Use filters: {"type": "decision"}, {"tags": ["project:x"]}

## Context Management
All tools require context_id argument (except list_contexts and create_context).
Use list_contexts() first to discover available context IDs.
Use create_context() to create a new context (requires owner/admin role).
Then pass context_id to other tools: remember(), recall(), forget(), reference(), explore(), get_context_info().

Response includes context_id, context_name, context_display_name to confirm which context was used.

## Security
Never store: passwords, API keys, PII, secrets
"""
