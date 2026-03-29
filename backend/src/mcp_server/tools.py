"""MCP Tool definitions for Kagura Memory Cloud.

Defines 9 memory tools:
- remember: Store new memory
- recall: Search memories (Neural Memory enabled)
- forget: Delete memory
- reference: Get full memory details
- explore: Graph traversal (Neural Memory)
- get_context_info: Get context information and stats
- list_contexts: List available contexts
- create_context: Create a new context (owner/admin only)
- kagura_memory_usage_guide: Get usage guide and examples
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from mcp.types import TextContent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Issue #163: Tool execution timeouts (seconds)
# Per-tool timeouts configurable via environment variables
# Example: MCP_TIMEOUT_RECALL=120 to set recall timeout to 120s


def _get_timeout(tool_name: str, default: float) -> float:
    """Get timeout from environment variable or use default."""
    env_key = f"MCP_TIMEOUT_{tool_name.upper()}"
    return float(os.getenv(env_key, default))


TOOL_TIMEOUTS: dict[str, float] = {
    "remember": _get_timeout("remember", 30.0),  # Embedding + Qdrant upsert
    "recall": _get_timeout("recall", 60.0),  # Embedding + Qdrant search + optional reranking
    "forget": _get_timeout("forget", 15.0),  # Simple delete operation
    "reference": _get_timeout("reference", 10.0),  # Simple lookup
    "explore": _get_timeout("explore", 45.0),  # Graph traversal can be complex
    "get_context_info": _get_timeout("get_context_info", 15.0),  # Stats aggregation
    # Issue #169: Context management tools
    "list_contexts": _get_timeout("list_contexts", 10.0),  # Database query
    "create_context": _get_timeout("create_context", 15.0),  # Context creation + Qdrant collection
    "update_context": _get_timeout("update_context", 15.0),  # Context update
    "update_search_config": _get_timeout("update_search_config", 10.0),  # Search config update
    # Issue #240: switch_context removed - use context_id argument in each tool
    "kagura_memory_usage_guide": _get_timeout("kagura_memory_usage_guide", 5.0),  # Static text
}
DEFAULT_TOOL_TIMEOUT = float(os.getenv("MCP_TIMEOUT_DEFAULT", 60.0))

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
3. explore() - Find related memories via graph traversal

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

# Usage guide for kagura_memory_usage_guide tool
KAGURA_MEMORY_USAGE_GUIDE = """# Kagura Memory Cloud - Usage Guide

## Overview
Kagura Memory Cloud is a memory system that allows AI assistants to store, search, and utilize information.
It accumulates knowledge across conversations and leverages past experiences.

---

## What You Can Do

### Save Information
Tell the AI what to remember - it will store it for you.

**Examples:**
```
"Remember this meeting content"
"Save this customer info with tag 'important-client'"
"Keep this procedure for future reference"
```

### Search Past Information
Ask the AI to find what you previously saved.

**Examples:**
```
"What did we discuss with Mr. Tanaka?"
"Find the expense report procedures"
"Show me all info about Project X"
```

**Search Tips:**
- Use specific keywords
- ❌ "What was that?" → ✅ "Tanaka budget proposal"

### View Full Details
Ask to see the complete saved content.

**Examples:**
```
"Show me the full details"
"Let me see everything about this"
```

### Find Related Information
Discover connected memories automatically.

**Examples:**
```
"What else is related to this customer?"
"Show me the complete picture of this project"
"Find all related decisions"
```

### Delete Unnecessary Information
Remove outdated or incorrect memories.

**Examples:**
```
"Delete this old information"
"This memory is no longer needed"
```

---

## How It Works

### 3-Layer Architecture
1. **summary**: Search-optimized summary (always returned)
2. **context_summary**: Background and usage explanation
3. **content/details**: Complete content (retrieved via reference)

### Neural Memory
- Memories automatically link to each other
- Explore related memories via graph traversal
- Frequently co-accessed memories strengthen their connections

### Hybrid Search
- Semantic search (meaning-based) 60%
- Keyword search (term-based) 40%
- Best of both worlds for accuracy

---

## Common Use Cases

### 📋 Business

**Client & Deal Management**
```
You: "Remember: Yamada Corp contract renewal in March.
     5-year contract, $120K/year. Contact: Director Sato.
     Watch for competitor switches."
```

**Meeting Decisions**
```
You: "Save this decision: New product pricing at $9.80.
     Based on 35% cost ratio and competitor analysis.
     Press release next month."
```

**Procedures & Workflows**
```
You: "Remember the expense report deadline: 25th of each month.
     Process: System entry by 25th → Manager approval → Finance.
     Photo receipts are acceptable."
```

---

### 🏠 Personal

**Ideas & Notes**
```
You: "Save this side business idea: online language tutoring.
     2 hours on weekends, target $30/hour. Check platform X."
```

**Learning & Insights**
```
You: "Remember this from 'The Art of Communication' book:
     Start presentations with the conclusion in first 30 seconds.
     Audience attention peaks at the beginning."
```

**Health & Life**
```
You: "Save my primary care doctor info:
     Tanaka Clinic, Internal Medicine, Dr. Tanaka.
     Closed Thursdays, Phone: 03-xxxx-xxxx, parking available."
```

**Travel Plans**
```
You: "Remember Kyoto autumn foliage spots for November:
     Tofukuji, Eikando, Arashiyama.
     Tip: Visit Tofukuji early morning to avoid crowds."
```

---

## How to Ask the AI

### To save something

```
"Remember this meeting content"
"Save this decision"
"Keep this as a note - it's important"
"Save with tag 'Project A'"
```

### To search memories

```
"What were the past interactions with Yamada Corp?"
"What did we decide in last month's meeting?"
"Have we discussed this before?"
"Show me memories tagged 'contracts'"
```

### To explore related info

```
"Find related information too"
"What other memories connect to this?"
"I want to see the full picture of this project"
```

### To organize/delete

```
"Delete outdated information"
"This memory is no longer needed"
"Clean up duplicate memories"
```

---

## Updating Memories

When saved information changes, **delete the old memory and save it again**.

### How to Update

**Method 1: Ask in one go (Easy)**
```
"Mr. Tanaka's budget changed from $50K to $80K. Update it."
```
→ AI automatically finds and deletes old memory → saves new one

**Method 2: Step by step (Reliable)**
```
1. "Delete the memory about Tanaka's $50K budget"
2. "Save: Tanaka's budget is now $80K"
3. "Search for Tanaka's budget to confirm"
```

### When to Update

- Customer budget changed
- Contract terms updated
- Contact information changed
- Procedures/rules revised

**Note:**
Saving the same content repeatedly creates duplicates. Remember to delete the old version.

---

## Making It Even Better

### Important Note

**You don't need to understand technical commands!**
- Just talk naturally: "Remember this", "Find that"
- The AI handles all technical details automatically
- If unsure, simply ask: "How should I save this?" or "Can you help me search for X?"

### Using Contexts

Kagura Memory Cloud lets you organize memories into "contexts" (separate workspaces).

**Examples:**
- "Sales Team" context → Deals and customer info
- "Dev Team" context → Technical knowledge and bug fixes
- "Personal Notes" context → Private information

### Context Settings (Recommended)

Each context can have "instructions for AI" configured in the web admin panel.
When you set up instructions, the AI automatically:
- Adds appropriate tags
- Judges importance levels
- Saves in searchable formats

**How to Configure:**
1. Open context settings in web admin
2. Enter "Summary (for AI)" - what this context is for
3. Enter "Instructions (for AI)" - how to organize memories

**Don't know how to write instructions?** No problem!
Just use the memory system normally, and ask the AI when you need help.

---

## Effective Usage Tips

### ✅ Good Examples

```
"Summarize past interactions with Mr. Tanaka for next week's meeting"
→ recall + reference to gather related info

"Save what I learned today. Tag it 'marketing'"
→ Clear instruction for proper storage

"Show me everything related to this customer"
→ explore to comprehensively retrieve related memories
```

### ❌ Examples to Avoid

```
"Do you remember that?"
→ Too vague for search

"Remember everything"
→ Unclear what's important

"That thing I mentioned before"
→ No keywords to search
```

---

## Best Practices

| Do This | Why |
|---------|-----|
| Write conclusions in summary | "Decided X" better than "Discussed X" |
| Use tags | Filter by customer, project names later |
| Set importance | Important memories won't get buried |
| Save frequently | Before you forget |

---

## Troubleshooting

- **No search results**: Try different keywords or ask "Find anything about [topic]"
- **Want related info**: Ask "Show me everything related to this"
- **Clean up old info**: Tell the AI "Delete outdated information about [topic]"
"""

T = TypeVar("T")


def get_tool_timeout(tool_name: str) -> float:
    """Get timeout for a specific tool.

    Args:
        tool_name: Name of the MCP tool

    Returns:
        Timeout in seconds for the tool
    """
    return TOOL_TIMEOUTS.get(tool_name, DEFAULT_TOOL_TIMEOUT)


async def execute_with_timeout(
    coro: Coroutine[Any, Any, T],
    timeout: float | None = None,
    operation_name: str = "tool",
) -> T:
    """Execute a coroutine with timeout protection.

    Issue #163: Prevents tool execution from hanging indefinitely due to
    downstream service issues (Qdrant, embedding API, reranker).

    Args:
        coro: Coroutine to execute
        timeout: Timeout in seconds (default: uses per-tool timeout or 60s)
        operation_name: Name for logging purposes (also used to look up timeout)

    Returns:
        Result of the coroutine

    Raises:
        TimeoutError: If execution exceeds timeout
    """
    # Use per-tool timeout if not explicitly specified
    if timeout is None:
        timeout = get_tool_timeout(operation_name)

    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        logger.error(f"Tool execution timeout: operation={operation_name}, timeout={timeout}s")
        raise


def get_tool_definitions() -> list[dict]:
    """Get static tool definitions for HTTP transport.

    Returns tool schemas without MCP server instance.
    Used by Streamable HTTP transport for tools/list responses.

    Returns:
        List of tool definition dicts (compatible with MCP spec)
    """
    return [
        {
            "name": "remember",
            "description": """Store important information, decisions, code snippets, or context into long-term memory. Use this when you want to preserve information for future recall across conversations.

Supports 3-layer architecture:
- summary: Concise overview for search (10-500 chars) - write the reusable conclusion/decision, not the process
  Include synonyms and related terms that users might search for.
  ✅ Good: "Database performance: PostgreSQL JSONB GIN index optimization for faster queries"
  ✅ Good: "JWT expiry caused 401. Fixed with refresh token rotation and clock skew handling."
  ❌ Bad: "Discussed auth errors in today's meeting."
  ❌ Bad: "JSONB index optimization" (too narrow — won't match "database performance")
- context_summary: Why this matters and how to use it
- details: Complete data, code, or structured information

CHUNKING BEST PRACTICES for long documents:
• Optimal summary length: 100-250 characters (max: 500)
• For long documents (>2000 chars): Create multiple semantic memories
  - ✅ DO: "User authentication module" + "Database models" + "API routes"
  - ❌ DON'T: "Document part 1/3" + "Document part 2/3" + "Document part 3/3"
• Link related memories using:
  - Tags: common tags like ["context-x", "api"]
  - Context: {"parent_doc_id": "...", "section": "intro"}
• Include context overlap: Add 50-100 chars from adjacent sections in context_summary

EXAMPLE - Large codebase file:
❌ Bad: remember(summary="auth.py file", content=<entire 5000 line file>)
✅ Good: remember(summary="OAuth2 login implementation", content=<login function>, tags=["auth", "oauth2"])
        remember(summary="JWT token validation logic", content=<validation function>, tags=["auth", "jwt"])
        remember(summary="Session management utilities", content=<session helpers>, tags=["auth", "session"])

To maximize search quality, enrich your memories with:

• Tags: Extract key entities/topics (e.g., ["OAuth2", "FastAPI", "Python"])
• Importance: Assign 0.0-1.0 score (critical=0.9-1.0, useful=0.6-0.8, reference=0.3-0.5)
• Type: Use semantic types (code, note, decision, bug-fix, feature, learning)
• Context: Provide rich background (context, related issues, reasoning)

Multi-context management (personal/work/contexts in one collection):
• Use domain tags: ["domain:personal"], ["domain:work"], ["domain:context-a"]
• Set visibility: context={"visibility": "private", "shareable": False}
• Filter on recall: filters={"tags": ["domain:work"]} to separate contexts

The more semantic metadata you provide, the better the search relevance.

IMPORTANT: Always specify context_id to ensure you're using the intended context. Use list_contexts() to discover available context IDs.""",
            "inputSchema": {
                "type": "object",
                "required": ["summary", "content", "type", "context_id"],
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Concise summary for search and retrieval (10-500 characters). This is what will be matched during recall.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Main content of the memory. Can be code, notes, explanations, or any text you want to preserve.",
                    },
                    "type": {
                        "type": "string",
                        "description": "Type of memory for categorization. Examples: 'code', 'note', 'decision', 'bug-fix', 'feature', 'config', 'learning'",
                    },
                    "context_summary": {
                        "type": "string",
                        "description": "Brief explanation (max 2000 chars). Helps understand why this memory was created and how to use it.",
                    },
                    "details": {
                        "type": "object",
                        "description": "Structured details as JSON. Use for storing additional metadata, code locations, or related information.",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Importance score 0.0-1.0 (default: 0.5). Higher values indicate critical information that should rank higher in search results.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization and filtering (e.g., ['python', 'fastapi', 'auth']). Helps organize and search memories.",
                    },
                    "context": {
                        "type": "object",
                        "description": "Additional context metadata as JSON. Can include context info, related issue numbers, or custom fields.",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID. Use list_contexts() to discover available IDs.",
                    },
                },
            },
        },
        {
            "name": "recall",
            "readOnly": True,
            "description": """Search and retrieve memories using advanced Hybrid Search (60% semantic + 40% full-text) with Neural Memory boosting. Returns relevant past information, code examples, decisions, or context from previous conversations.

For best results with question-style queries, try generating a hypothetical answer first and searching with that instead (HyDE technique - typically 3-10% better results, up to 25% in optimal cases).

Examples:
• EN: "How to fix auth errors?" → Generate: "Auth errors are caused by expired JWT tokens. Use refresh token to re-authenticate..." → Search with generated answer.
• JP: "認証エラーの対処法" → Generate: "認証エラーはJWTトークン期限切れが原因。リフレッシュトークンで再認証する..." → Search with generated answer.

You can also expand queries with related terms for comprehensive coverage:
• "認証エラー" → Try also: "OAuth2", "JWT", "401 error"
• Use tag filters for precision: filters={"tags": ["python", "fastapi"]}
• Advanced filters: importance={"gte": 0.8}, scope="persistent", type="code"
• Combine multiple searches for thorough exploration

If few results: Try shorter query, remove filters, use related terms, or lower importance threshold.

Common workflow: Bug fix → recall("error message") → find similar past fixes → reference() for details → apply solution.

Returns summaries and context (Layers 1-2) optimized for quick understanding.

IMPORTANT: Always specify context_id to ensure you're searching the intended context. Use list_contexts() to discover available context IDs.""",
            "inputSchema": {
                "type": "object",
                "required": ["query", "context_id"],
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query. Can be a question, keywords, or description of what you're looking for. Supports both semantic understanding and keyword matching.",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 5, max: 100). Start with 5-10 for focused results, increase for broader exploration.",
                    },
                    "use_rerank": {
                        "type": "boolean",
                        "description": "Enable reranking for higher quality results (default: false). Uses Voyage AI or Cohere based on your configured provider. Set to true if you have API keys configured.",
                    },
                    "filters": {
                        "type": "object",
                        "description": "Optional filters as JSON. Examples: {'type': 'code'}, {'tags': ['python']}, {'importance': {'gte': 0.7}}",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID. Use list_contexts() to discover available IDs.",
                    },
                },
            },
        },
        {
            "name": "reference",
            "readOnly": True,
            "description": """Retrieve complete details (Layer 3) of a specific memory by ID. Use this after recall() when you need the full content and metadata of a particular memory.

Unlike recall() which returns summaries for quick scanning, reference() returns everything including complete content, structured context, and all metadata.

Typical workflow:
1. recall() to find relevant memories
2. Analyze summaries to identify most interesting
3. reference() to get full details of selected memories
4. Present comprehensive information to user

Returns all 3 layers: summary, context_summary, and complete details/content.

IMPORTANT: Always specify context_id to ensure you're retrieving from the intended context. Use list_contexts() to discover available context IDs.""",
            "inputSchema": {
                "type": "object",
                "required": ["memory_id", "context_id"],
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "UUID of the memory to retrieve. Obtained from recall() results.",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID. Use list_contexts() to discover available IDs.",
                    },
                },
            },
        },
        {
            "name": "forget",
            "description": """Delete memories that are no longer needed, outdated, or incorrect (soft delete, can be recovered within retention period).

Recommended safe workflow: recall() to preview → verify memory_id → forget(). Avoid query mode unless bulk cleanup needed.

Use with caution - always verify before deleting:
• Check importance score (warn user if importance > 0.8)
• Show memory summary for confirmation
• Get explicit user approval for deletion

For bulk deletion:
1. Use recall() to find candidates
2. Review list with user
3. Get confirmation
4. Loop forget() for each memory_id

Common issues:
• "Not found" → Memory already deleted or wrong ID (verify with recall)
• "Permission denied" → Trying to delete another user's memory

Note: Soft delete with 30-day retention. Associated graph edges are automatically cleaned up. Supports deletion by specific memory_id or by search query (deletes top-k matches).

IMPORTANT: Always specify context_id to ensure you're deleting from the intended context. Use list_contexts() to discover available context IDs.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "UUID of specific memory to delete. Use this when you know exactly which memory to remove (e.g., from recall results).",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query to find and delete matching memories. Use when you want to bulk-delete similar memories (e.g., 'outdated test data').",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Number of memories to delete when using query mode (default: 10). Acts as a safety limit to prevent accidental mass deletion.",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID. Use list_contexts() to discover available IDs.",
                    },
                },
            },
        },
        {
            "name": "explore",
            "readOnly": True,
            "description": """Discover related memories through Neural Memory graph traversal using activation spreading. Starting from a seed memory, this explores the knowledge graph to find connected concepts, related decisions, or associated code.

Useful when user asks "What else is related to X?" or after recall() to build comprehensive context.

Parameter tuning for optimal results:
• depth: Controls exploration breadth (1=direct connections, 2=recommended default, 3+=broader but slower)
• min_weight: Filters connection strength (typical edge weights: 0.02-0.05)
  - 0.0: All connections (recommended for initial exploration)
  - 0.05: Most connections (balanced, good default)
  - 0.1: Stronger connections only
  - 0.3+: Very strong only (may return few/no results)

Typical workflow: recall() to find seed → explore(seed, depth=2, min_weight=0.05) → present related context.

Response format:
• total_activated: Number of nodes activated in graph traversal
• returned: Number of results after min_weight filtering
• If returned=0 but total_activated>0: Lower min_weight to 0.0-0.05 to see connections (typical edge weights: 0.02-0.05)

Optional relation_types filter for specific edge types:
• 'neural_association' (default, Hebbian learning)
• 'related_to', 'depends_on', 'learned_from'
• Omit to explore all connection types

Returns memories ranked by activation strength (graph-based relevance).

IMPORTANT: Always specify context_id to ensure you're exploring the intended context. Use list_contexts() to discover available context IDs.""",
            "inputSchema": {
                "type": "object",
                "required": ["memory_id", "context_id"],
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "UUID of the starting memory (seed node). Usually obtained from recall results. Exploration radiates outward from this point.",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum number of hops in the graph traversal (default: 2, max: 5). Higher values find more distant connections but may be less relevant.",
                    },
                    "min_weight": {
                        "type": "number",
                        "description": "Minimum edge weight threshold (default: 0.05, range: 0.0-1.0, typical edge weights: 0.02-0.05). Higher values return only strong connections, lower values explore weaker associations.",
                        "default": 0.05,
                    },
                    "relation_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional filter for specific relation types. Examples: ['related_to', 'caused_by', 'implements']. Omit to explore all connections.",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID. Use list_contexts() to discover available IDs.",
                    },
                },
            },
        },
        {
            "name": "get_context_info",
            "readOnly": True,
            "description": """Get current context information, usage guidelines, and memory instructions.

**Call this at session start** to load:
- context.usage_guide: Context-specific guidelines
- context.is_private: Whether this is a private or shared context
- instructions: General best practices for using memory tools

Also call after switching contexts to reload guidelines.

Returns:
• context.id, context.name: Current context identifier
• context.summary: Brief description of this context's purpose (200-500 chars)
• context.usage_guide: Guidelines for how AI should use memories in this context
• context.is_private: true=only you can see, false=workspace members can see
• stats (if include_details=true): Memory counts and storage usage
• instructions: Quick reference guide for memory tools

This helps you understand what the current context is for and how to properly use memories within it.

IMPORTANT: Specify context_id to get info for a specific context. Use list_contexts() to discover available context IDs.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "include_details": {
                        "type": "boolean",
                        "description": "Include detailed breakdown by type and importance (default: true). Set to false for quick summary only.",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Context UUID to get info for. Use list_contexts() to discover available IDs.",
                    },
                },
            },
        },
        # =================================================================
        # Issue #169: Context Management Tools
        # =================================================================
        # Issue #169: Context management tools (create/update via Web UI only)
        {
            "name": "list_contexts",
            "readOnly": True,
            "description": """List all available contexts in the current workspace.

Returns contexts sorted by recent usage (most recently used first).

Use this to discover context IDs for other tool calls:
- User asks what contexts are available
- You need to show context options
- User asks about their memory workspaces

All other tools require context_id. Use this tool first to discover available context IDs.

Response includes:
- contexts: Array of {id, name, summary, is_private, last_used_at}
- count: Total number of contexts""",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_stats": {
                        "type": "boolean",
                        "description": "Include memory count per context (default: false). Set true if user asks about usage.",
                    },
                },
            },
        },
        # Issue #240: switch_context removed - use context_id argument in each tool
        {
            "name": "create_context",
            "description": """Create a new context in the current workspace.

Contexts organize memories into separate namespaces (e.g., per-project, per-topic).

Requires owner or admin role in the workspace.
- Owners can create both private and shared contexts
- Admins can create shared contexts only

Context name rules:
- Lowercase alphanumeric characters, hyphens, and underscores only
- Pattern: ^[a-z0-9_-]+$
- Must be unique within the workspace

Returns the created context's id, name, and metadata.

Use list_contexts() after creation to verify.""",
            "inputSchema": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Context name (lowercase alphanumeric + hyphen/underscore). Example: 'my-project', 'work_notes'",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Human-readable display name. Defaults to name if omitted.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description of the context's purpose.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "LLM-oriented summary (200-500 chars). Helps AI understand context purpose.",
                    },
                    "usage_guide": {
                        "type": "string",
                        "description": "LLM-oriented memory usage guidelines for this context.",
                    },
                    "is_private": {
                        "type": "boolean",
                        "description": "Privacy flag (default: true). true=only creator can access, false=workspace members can access (requires Pro plan).",
                    },
                },
            },
        },
        # =================================================================
        # Tool: update_context (Issue #354)
        # =================================================================
        {
            "name": "update_context",
            "description": """Update an existing context's settings.

Modify summary, usage_guide, description, display_name, resource_id, or is_public of a context.

Requires owner or editor role in the context.
- summary/usage_guide/resource_id/is_public: Owner-only fields
- display_name/description: Editor access sufficient

Use get_context_info() to see current values before updating.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "description": "Context UUID to update. Use list_contexts() to find IDs.",
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Updated human-readable display name (max 200 chars).",
                    },
                    "description": {
                        "type": "string",
                        "description": "Updated context description (max 500 chars).",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Updated LLM-oriented summary (max 500 chars). Helps AI understand context purpose.",
                    },
                    "usage_guide": {
                        "type": "string",
                        "description": "Updated LLM-oriented usage guidelines (max 2000 chars). Instructions for how AI should use memories in this context.",
                    },
                    "resource_id": {
                        "type": "string",
                        "description": "Resource ID for external data ingestion via Resource Tokens. Lowercase alphanumeric and underscores only (e.g., 'github_issues'). Must be unique within the workspace.",
                    },
                    "is_public": {
                        "type": "boolean",
                        "description": "Make context publicly accessible via REST API. Requires owner permission and higher tier plan.",
                    },
                },
            },
        },
        # =================================================================
        # Usage Guide Tool
        # =================================================================
        # Tool: update_search_config (Issue #25)
        # =================================================================
        {
            "name": "update_search_config",
            "description": """Update context search configuration (hybrid search weights, reranker settings).

Tune search quality per context by adjusting semantic vs keyword (BM25) weights.

Requires owner or editor role in the context.

Examples:
- Increase keyword matching: semantic_weight=0.5, bm25_weight=0.5
- Semantic-heavy: semantic_weight=0.7, bm25_weight=0.3
- Enable reranking: use_rerank=true, reranker_provider="voyage"

Weights must sum to 1.0.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "description": "Context UUID to configure.",
                    },
                    "semantic_weight": {
                        "type": "number",
                        "description": "Semantic (vector) search weight (0.0-1.0). Default: 0.6.",
                    },
                    "bm25_weight": {
                        "type": "number",
                        "description": "BM25 (keyword) search weight (0.0-1.0). Default: 0.4.",
                    },
                    "fetch_factor": {
                        "type": "integer",
                        "description": "Candidate retrieval multiplier (1-10). Default: 3.",
                    },
                    "use_rerank": {
                        "type": "boolean",
                        "description": "Enable/disable reranking. Requires reranker API key.",
                    },
                    "reranker_provider": {
                        "type": "string",
                        "description": "Reranker provider: 'voyage' or 'cohere'.",
                    },
                    "reranker_model": {
                        "type": "string",
                        "description": "Provider-specific model name (e.g., 'rerank-2', 'rerank-multilingual-v3.0').",
                    },
                },
            },
        },
        # =================================================================
        {
            "name": "kagura_memory_usage_guide",
            "description": """Get the comprehensive usage guide for Kagura Memory Cloud.

Use this tool when users ask:
- "How do I use Kagura Memory Cloud?"
- "What can this memory system do?"
- "Show me examples of using memory tools"
- "How should I save/search memories?"

Returns a complete guide including:
- Tool explanations (remember, recall, forget, reference, explore)
- How the system works (3-layer architecture, Neural Memory, Hybrid Search)
- Business and personal use case examples
- Prompt examples for asking the AI
- Best practices and tips

No parameters required - just call this tool to get the full guide.""",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
            "readOnly": True,  # This tool only returns static text, no side effects
        },
    ]


# ============================================================================
# Tool Execution Helpers (Issue #172: DRY reduction)
# ============================================================================


def _error_response(error: str, message: str, **extra: Any) -> list[TextContent]:
    """Create a standardized error response.

    Args:
        error: Error code
        message: Human-readable error message
        **extra: Additional fields to include in response

    Returns:
        List with single TextContent error response
    """
    return [
        TextContent(
            type="text",
            text=json.dumps({"status": "error", "error": error, "message": message, **extra}),
        )
    ]


async def _resolve_context(
    db: "AsyncSession",
    user_id: str,
    context_id: UUID,
) -> Any:
    """Resolve and validate context access.

    Args:
        db: Database session
        user_id: User ID
        context_id: Context UUID

    Returns:
        Context object

    Raises:
        _ContextNotFoundError: If context not found or access denied
    """
    from services.context_service import ContextService
    from utils.exceptions import NotFoundException

    context_service = ContextService(db)
    try:
        return await context_service.get_context(user_id, context_id)
    except Exception as e:
        if isinstance(e, NotFoundException):
            error_msg = "Context not found or you don't have access to it."
        else:
            error_msg = str(e)
        raise _ContextNotFoundError(context_id, error_msg) from e


class _ContextNotFoundError(Exception):
    """Internal error for context resolution failures."""

    def __init__(self, context_id: UUID, message: str):
        self.context_id = context_id
        self.message = message
        super().__init__(message)

    def to_response(self) -> list[TextContent]:
        return _error_response(
            "context_not_found",
            self.message,
            context_id=str(self.context_id),
            help="Use list_contexts() to see contexts you have access to.",
        )


async def _check_viewer_permission(
    db: "AsyncSession",
    user_id: str,
    workspace_id: UUID | None,
    operation: str,
) -> list[TextContent] | None:
    """Check if user is a viewer (read-only). Returns error response if so, None otherwise.

    Args:
        db: Database session
        user_id: User ID
        workspace_id: Workspace ID (None = skip check)
        operation: Operation description for error message

    Returns:
        Error response if viewer, None if allowed
    """
    if not workspace_id:
        return None

    user_role = await _get_workspace_member_role(db, user_id, workspace_id)
    if user_role == "viewer":
        return _error_response(
            "permission_denied",
            f"Viewers have read-only access. Cannot {operation}.",
            your_role="viewer",
            required_role="member",
            help="Contact your workspace owner to upgrade your role to 'member' for write access.",
        )
    return None


async def _log_tool_usage(
    db: "AsyncSession",
    user_id: str,
    tool_name: str,
    start_time: float,
    status_code: int,
    context_id: UUID | str | None = None,
    workspace_id: UUID | None = None,
) -> None:
    """Log tool usage metrics.

    Args:
        db: Database session
        user_id: User ID
        tool_name: Tool name
        start_time: Start time from time.time()
        status_code: HTTP-style status code (200=success, 500=error)
        context_id: Context ID (optional)
        workspace_id: Workspace ID (optional)
    """
    from db.base import get_db
    from utils.usage_logger import log_usage

    response_time_ms = int((time.time() - start_time) * 1000)
    try:
        # Use independent session to avoid affecting tool handler's transaction
        async for log_db in get_db():
            await log_usage(
                db=log_db,
                user_id=user_id,
                endpoint=f"mcp:{tool_name}",
                method="MCP",
                status_code=status_code,
                response_time_ms=response_time_ms,
                context_id=str(context_id) if context_id else None,
                workspace_id=str(workspace_id) if workspace_id else None,
            )
    except Exception as e:
        logger.warning("tool_usage_log_failed", tool=tool_name, error=str(e))


def _context_response_fields(context: Any) -> dict[str, Any]:
    """Extract common context fields for tool responses.

    Args:
        context: Context object (or None)

    Returns:
        Dict with context_id, context_name, context_display_name, context_is_private
    """
    if not context:
        return {
            "context_id": None,
            "context_name": None,
            "context_display_name": None,
            "context_is_private": None,
        }
    return {
        "context_id": str(context.id),
        "context_name": context.name,
        "context_display_name": context.display_name,
        "context_is_private": context.is_private,
    }


def _validate_memory_id(
    args: dict[str, Any], tool_name: str
) -> tuple[UUID | None, list[TextContent] | None]:
    """Validate and parse memory_id from args.

    Args:
        args: Tool arguments
        tool_name: Tool name for error messages

    Returns:
        (memory_uuid, None) on success, (None, error_response) on failure
    """
    if "memory_id" not in args:
        return None, _error_response(
            "memory_id_required",
            f"{tool_name} requires memory_id argument.",
            help="Get memory_id from recall() results first.",
        )
    try:
        return UUID(args["memory_id"]), None
    except (ValueError, AttributeError, TypeError):
        return None, _error_response(
            "invalid_memory_id_format",
            f"Invalid memory_id format: '{args['memory_id']}'. Expected a UUID.",
            help="Use recall() to get valid memory IDs.",
        )


# ============================================================================
# Main Tool Execution Entry Point
# ============================================================================


async def execute_tool_call(
    tool_name: str,
    arguments: dict[str, Any],
    user_id: str,
    workspace_id: UUID | None = None,
) -> list[TextContent]:
    """Execute MCP tool call directly (used by Streamable HTTP Transport).

    Issue #172: Refactored to extract common boilerplate into helpers.
    Issue #245: context_id is now obtained from arguments["context_id"] (required).

    Args:
        tool_name: Tool name (remember, recall, forget, reference, explore, get_context_info, list_contexts)
        arguments: Tool arguments dict (must include context_id for most tools)
        user_id: User ID for this request
        workspace_id: Workspace ID (Issue #146)

    Returns:
        List of TextContent with execution result
    """
    args = arguments or {}

    # Validate context_id early for tools that require it
    if tool_name not in (
        "list_contexts",
        "create_context",
        "update_context",
        "kagura_memory_usage_guide",
    ):
        if "context_id" not in args:
            return _error_response(
                "context_id_required",
                f"{tool_name} requires context_id argument.",
                help="Use list_contexts() first to discover available context IDs.",
                example=f'{tool_name}(..., context_id="<uuid-from-list_contexts>")',
            )
        try:
            _resolve_context_id(args["context_id"])
        except ValueError as e:
            return _error_response("invalid_context_id_format", str(e))

    try:
        # Import here to avoid circular imports
        from db.base import get_db
        from models.schemas import (
            ExploreRequest,
            ForgetRequest,
            RecallRequest,
            ReferenceRequest,
            RememberRequest,
        )
        from services.memory_service import MemoryService

        # =================================================================
        # Tool 1: remember
        # =================================================================
        if tool_name == "remember":
            if "summary" not in args or "content" not in args or "type" not in args:
                return _error_response(
                    "missing_fields",
                    "Missing required fields: summary, content, type",
                )

            request = RememberRequest(
                summary=args["summary"],
                context_summary=args.get("context_summary"),
                content=args["content"],
                details=args.get("details"),
                type=args["type"],
                importance=args.get("importance", 0.5),
                tags=args.get("tags", []),
                context=args.get("context"),
            )

            start_time = time.time()
            async for db in get_db():
                try:
                    current_context_id = _resolve_context_id(args["context_id"])

                    # Check viewer write permission
                    perm_error = await _check_viewer_permission(
                        db, user_id, workspace_id, "create memories"
                    )
                    if perm_error:
                        return perm_error

                    current_context = await _resolve_context(db, user_id, current_context_id)

                    service = MemoryService(db)
                    result = await execute_with_timeout(
                        service.remember(
                            request,
                            user_id=user_id,
                            client="mcp",
                            current_context_id=current_context_id,
                            current_workspace_id=workspace_id,
                        ),
                        operation_name="remember",
                    )

                    await _log_tool_usage(
                        db, user_id, "remember", start_time, 200, current_context_id, workspace_id
                    )
                    await db.commit()

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "memory_id": str(result.memory_id),
                                    "scope": result.scope,
                                    **_context_response_fields(current_context),
                                }
                            ),
                        )
                    ]
                except _ContextNotFoundError as e:
                    await db.rollback()
                    return e.to_response()
                except Exception:
                    await db.rollback()
                    await _log_tool_usage(
                        db,
                        user_id,
                        "remember",
                        start_time,
                        500,
                        args.get("context_id"),
                        workspace_id,
                    )
                    raise

        # =================================================================
        # Tool 2: recall
        # =================================================================
        elif tool_name == "recall":
            if "query" not in args:
                return _error_response("missing_fields", "Missing required field: query")

            request = RecallRequest(
                query=args["query"],
                k=args.get("k", 5),
                use_rerank=args.get("use_rerank", False),
                filters=args.get("filters"),
            )

            start_time = time.time()
            async for db in get_db():
                try:
                    current_context_id = _resolve_context_id(args["context_id"])
                    current_context = await _resolve_context(db, user_id, current_context_id)

                    service = MemoryService(db)
                    result = await execute_with_timeout(
                        service.recall(
                            request,
                            user_id=user_id,
                            current_context_id=current_context_id,
                            current_workspace_id=workspace_id,
                        ),
                        operation_name="recall",
                    )

                    results_data = [
                        {
                            "memory_id": str(r.memory_id),
                            "summary": r.summary,
                            "context_summary": r.context_summary,
                            "type": r.type,
                            "importance": r.importance,
                            "scope": r.scope,
                            "score": r.score,
                            "tags": r.tags,
                        }
                        for r in result.results
                    ]

                    related_tags_data = [
                        {"tag": tag.tag, "count": tag.count, "sample_summary": tag.sample_summary}
                        for tag in result.related_tags
                    ]

                    await _log_tool_usage(
                        db, user_id, "recall", start_time, 200, current_context_id, workspace_id
                    )
                    await db.commit()

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "results": results_data,
                                    "count": len(results_data),
                                    "related_tags": related_tags_data,
                                    **_context_response_fields(current_context),
                                }
                            ),
                        )
                    ]
                except _ContextNotFoundError as e:
                    await db.rollback()
                    return e.to_response()
                except Exception:
                    await db.rollback()
                    await _log_tool_usage(
                        db, user_id, "recall", start_time, 500, args.get("context_id"), workspace_id
                    )
                    raise

        # =================================================================
        # Tool 3: forget
        # =================================================================
        elif tool_name == "forget":
            memory_id = args.get("memory_id")
            request = ForgetRequest(
                memory_id=UUID(memory_id) if memory_id else None,
                query=args.get("query"),
                k=args.get("k", 10),
            )

            start_time = time.time()
            async for db in get_db():
                try:
                    current_context_id = _resolve_context_id(args["context_id"])

                    # Check viewer delete permission
                    perm_error = await _check_viewer_permission(
                        db, user_id, workspace_id, "delete memories"
                    )
                    if perm_error:
                        return perm_error

                    current_context = await _resolve_context(db, user_id, current_context_id)

                    service = MemoryService(db)
                    result = await execute_with_timeout(
                        service.forget(
                            request,
                            user_id=user_id,
                            current_context_id=current_context_id,
                        ),
                        operation_name="forget",
                    )

                    await _log_tool_usage(
                        db, user_id, "forget", start_time, 200, current_context_id, workspace_id
                    )
                    await db.commit()

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "deleted_count": result.deleted_count,
                                    "memory_ids": [str(mid) for mid in result.memory_ids],
                                    "context_id": str(current_context.id)
                                    if current_context
                                    else None,
                                    "context_name": current_context.name
                                    if current_context
                                    else None,
                                }
                            ),
                        )
                    ]
                except _ContextNotFoundError as e:
                    await db.rollback()
                    return e.to_response()
                except Exception:
                    await db.rollback()
                    await _log_tool_usage(
                        db, user_id, "forget", start_time, 500, args.get("context_id"), workspace_id
                    )
                    raise

        # =================================================================
        # Tool 4: reference
        # =================================================================
        elif tool_name == "reference":
            memory_uuid, error = _validate_memory_id(args, "reference")
            if error or memory_uuid is None:
                return error or _error_response("invalid_memory_id_format", "Invalid memory_id")

            request = ReferenceRequest(memory_id=memory_uuid)

            start_time = time.time()
            async for db in get_db():
                try:
                    current_context_id = _resolve_context_id(args["context_id"])
                    await _resolve_context(db, user_id, current_context_id)

                    service = MemoryService(db)
                    try:
                        result = await execute_with_timeout(
                            service.reference(request.memory_id, user_id=user_id),
                            operation_name="reference",
                        )
                    except Exception as e:
                        from utils.exceptions import NotFoundException

                        if isinstance(e, NotFoundException):
                            return _error_response(
                                "memory_not_found",
                                f"Memory not found or you don't have access: {request.memory_id}",
                                help="Use recall() to find memories you have access to.",
                            )
                        raise

                    reference_data = {
                        "memory_id": str(result.memory_id),
                        "summary": result.summary,
                        "context_summary": result.context_summary,
                        "content": result.content,
                        "details": result.details,
                        "type": result.type,
                        "importance": result.importance,
                        "tags": result.tags,
                        "context": result.context,
                        "created_at": result.created_at.isoformat(),
                        "client": result.client,
                    }

                    await _log_tool_usage(
                        db, user_id, "reference", start_time, 200, current_context_id, workspace_id
                    )
                    await db.commit()

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps({"status": "success", "memory": reference_data}),
                        )
                    ]
                except _ContextNotFoundError as e:
                    await db.rollback()
                    return e.to_response()
                except Exception:
                    await db.rollback()
                    await _log_tool_usage(
                        db,
                        user_id,
                        "reference",
                        start_time,
                        500,
                        args.get("context_id"),
                        workspace_id,
                    )
                    raise

        # =================================================================
        # Tool 5: explore
        # =================================================================
        elif tool_name == "explore":
            memory_uuid, error = _validate_memory_id(args, "explore")
            if error or memory_uuid is None:
                return error or _error_response("invalid_memory_id_format", "Invalid memory_id")

            request = ExploreRequest(
                memory_id=memory_uuid,
                depth=args.get("depth", 2),
                relation_types=args.get("relation_types"),
                min_weight=args.get("min_weight", 0.05),
            )

            start_time = time.time()
            async for db in get_db():
                try:
                    current_context_id = _resolve_context_id(args["context_id"])
                    await _resolve_context(db, user_id, current_context_id)

                    service = MemoryService(db)
                    result = await execute_with_timeout(
                        service.explore(
                            request,
                            user_id=user_id,
                            current_context_id=current_context_id,
                            current_workspace_id=workspace_id,
                        ),
                        operation_name="explore",
                    )

                    explore_data = {
                        "seed_memory": {
                            "memory_id": str(result.seed_memory.memory_id),
                            "summary": result.seed_memory.summary,
                            "type": result.seed_memory.type,
                        },
                        "related_memories": [
                            {
                                "memory_id": str(r.memory_id),
                                "summary": r.summary,
                                "activation": r.activation,
                                "hop": r.hop,
                                "weight": r.weight,
                                "path": [str(p) for p in r.path],
                            }
                            for r in result.related_memories
                        ],
                        "metadata": result.metadata,
                    }

                    await _log_tool_usage(
                        db, user_id, "explore", start_time, 200, current_context_id, workspace_id
                    )
                    await db.commit()

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps({"status": "success", "exploration": explore_data}),
                        )
                    ]
                except _ContextNotFoundError as e:
                    await db.rollback()
                    return e.to_response()
                except Exception:
                    await db.rollback()
                    await _log_tool_usage(
                        db,
                        user_id,
                        "explore",
                        start_time,
                        500,
                        args.get("context_id"),
                        workspace_id,
                    )
                    raise

        # =================================================================
        # Tool 6: get_context_info (Issue #160)
        # =================================================================
        elif tool_name == "get_context_info":
            include_details = args.get("include_details", True)

            start_time = time.time()
            async for db in get_db():
                try:
                    from services.context_service import ContextService

                    current_context_id = _resolve_context_id(args["context_id"])

                    # Get context details (with fallback for stats if access check fails)
                    context_service = ContextService(db)
                    current_context = None
                    context_for_stats = None

                    if current_context_id:
                        try:
                            current_context = await context_service.get_context(
                                user_id, current_context_id
                            )
                            context_for_stats = current_context
                        except Exception as e:
                            logger.warning(
                                f"Failed to fetch context with access check {current_context_id}: {e}"
                            )
                            from sqlalchemy import select

                            from models.auth import Context

                            result = await db.execute(
                                select(Context).where(
                                    Context.id == current_context_id,
                                    Context.deleted_at.is_(None),
                                )
                            )
                            context_for_stats = result.scalar_one_or_none()

                    # Issue #204: Check if context is shared or if user is workspace owner
                    is_shared = False
                    workspace = None
                    logger.info(
                        f"MCP get_context_info: workspace_id={workspace_id}, current_context={current_context is not None}, context_for_stats={context_for_stats is not None}"
                    )

                    if context_for_stats and workspace_id:
                        from sqlalchemy import select

                        from models.auth import Workspace

                        workspace_result = await db.execute(
                            select(Workspace).where(Workspace.id == workspace_id)
                        )
                        workspace = workspace_result.scalar_one_or_none()
                        logger.info(
                            f"MCP workspace lookup: workspace_id={workspace_id}, workspace_found={workspace is not None}"
                        )
                        is_workspace_owner = workspace and workspace.owner_user_id == user_id
                        is_shared = is_workspace_owner or not context_for_stats.is_private

                    service = MemoryService(db)
                    result = await execute_with_timeout(
                        service.get_stats(
                            user_id=user_id,
                            workspace_id=str(workspace_id) if workspace_id else None,
                            context_id=str(current_context_id) if current_context_id else None,
                            include_details=include_details,
                            time_window_hours=168,
                            is_shared_context=is_shared,
                        ),
                        operation_name="get_context_info",
                    )

                    context_data = None
                    if current_context:
                        context_data = {
                            "id": str(current_context.id),
                            "name": current_context.name,
                            "display_name": current_context.display_name,
                            "summary": current_context.summary
                            or "No summary provided. Please add a summary in the context settings.",
                            "usage_guide": current_context.usage_guide
                            or "No usage guide provided. Please add usage guidelines in the context settings.",
                            "is_private": current_context.is_private,
                        }

                    workspace_data = None
                    if workspace:
                        workspace_data = {
                            "id": str(workspace.id),
                            "name": workspace.name,
                            "description": workspace.description,
                        }

                    stats_data: dict[str, Any] = {
                        "total_memories": result.total_count,
                        "working_memories": result.working_count,
                        "persistent_memories": result.persistent_count,
                    }
                    if include_details:
                        stats_data["details"] = {
                            "by_type": result.by_type,
                            "by_importance": result.by_importance,
                            "recent_7days": result.recent_activity,
                        }

                    await _log_tool_usage(
                        db,
                        user_id,
                        "get_context_info",
                        start_time,
                        200,
                        current_context_id,
                        workspace_id,
                    )
                    await db.commit()

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "context": context_data,
                                    "workspace": workspace_data,
                                    "stats": stats_data,
                                    "instructions": KAGURA_MEMORY_INSTRUCTIONS,
                                }
                            ),
                        )
                    ]
                except Exception as e:
                    await db.rollback()
                    await _log_tool_usage(
                        db,
                        user_id,
                        "get_context_info",
                        start_time,
                        500,
                        args.get("context_id"),
                        workspace_id,
                    )
                    logger.error(f"get_context_info_failed: {e}", exc_info=True)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "error",
                                    "error": str(e),
                                    "message": "Failed to retrieve context info. Please try again.",
                                }
                            ),
                        )
                    ]

        # =================================================================
        # Tool 7: create_context
        # =================================================================
        elif tool_name == "create_context":
            if "name" not in args:
                return _error_response(
                    "missing_fields",
                    "Missing required field: name",
                    help="Provide a context name (lowercase alphanumeric + hyphen/underscore).",
                )

            start_time = time.time()
            async for db in get_db():
                try:
                    from services.context_service import ContextService
                    from services.quota_service import QuotaService

                    # Check workspace_id is available
                    if not workspace_id:
                        return _error_response(
                            "workspace_required",
                            "Workspace ID is required to create a context.",
                            help="Ensure your MCP connection is configured with a workspace.",
                        )

                    # Check role: only owner/admin can create contexts
                    user_role = await _get_workspace_member_role(db, user_id, workspace_id)
                    if user_role not in ("owner", "admin"):
                        return _error_response(
                            "permission_denied",
                            "Only workspace owners and admins can create contexts.",
                            your_role=user_role or "not_a_member",
                            required_role="owner or admin",
                        )

                    # Check context creation quota
                    quota_service = QuotaService(db)
                    can_create, error_msg = await quota_service.check_context_creation_allowed(
                        workspace_id
                    )
                    if not can_create:
                        return _error_response(
                            "quota_exceeded",
                            error_msg or "Context creation limit reached.",
                            help="Delete unused contexts or upgrade your plan.",
                        )

                    # Create context
                    is_private = args.get("is_private", True)
                    context_service = ContextService(db)
                    context = await execute_with_timeout(
                        context_service.create_context(
                            workspace_id=workspace_id,
                            name=args["name"],
                            display_name=args.get("display_name"),
                            description=args.get("description"),
                            summary=args.get("summary"),
                            usage_guide=args.get("usage_guide"),
                            created_by=user_id,
                            is_private=is_private,
                        ),
                        operation_name="create_context",
                    )

                    await _log_tool_usage(
                        db,
                        user_id,
                        "create_context",
                        start_time,
                        200,
                        str(context.id),
                        workspace_id,
                    )

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "message": f"Context '{args['name']}' created successfully.",
                                    **_context_response_fields(context),
                                }
                            ),
                        )
                    ]
                except _ContextNotFoundError as e:
                    await db.rollback()
                    return e.to_response()
                except Exception as e:
                    await db.rollback()
                    error_str = str(e)
                    # Surface validation errors clearly
                    if "already exists" in error_str or "ValidationError" in type(e).__name__:
                        return _error_response(
                            "validation_error",
                            error_str,
                            help="Check the context name and try again.",
                        )
                    logger.error(f"create_context_failed: {e}", exc_info=True)
                    return _error_response("create_context_error", error_str)

        # =================================================================
        # Tool: update_context (Issue #354)
        # =================================================================
        elif tool_name == "update_context":
            if "context_id" not in args:
                return _error_response(
                    "missing_fields",
                    "Missing required field: context_id",
                    help="Use list_contexts() to find context IDs.",
                )

            start_time = time.time()
            async for db in get_db():
                try:
                    from uuid import UUID as _UUID

                    from services.permission_service import PermissionService

                    ctx_uuid = _UUID(args["context_id"])

                    perm_service = PermissionService(db)
                    owner_fields = {"summary", "usage_guide", "resource_id", "is_public"}
                    requested_fields = {
                        k
                        for k in (
                            "summary",
                            "usage_guide",
                            "display_name",
                            "description",
                            "resource_id",
                            "is_public",
                        )
                        if k in args
                    }

                    if not requested_fields:
                        return _error_response(
                            "no_changes",
                            "No fields to update. Provide at least one of: summary, usage_guide, display_name, description, resource_id, is_public.",
                        )

                    # Permission check using PermissionService (same as REST API)
                    try:
                        if requested_fields & owner_fields:
                            # Owner-only fields → require context owner
                            context = await perm_service.check_context_owner(user_id, ctx_uuid)
                        else:
                            # display_name/description → require editor access
                            context, _ = await perm_service.check_context_access(
                                user_id, ctx_uuid, required_role="editor"
                            )
                    except Exception as perm_err:
                        return _error_response(
                            "permission_denied",
                            str(perm_err),
                            help="You need owner access for summary/usage_guide/resource_id/is_public, or editor access for display_name/description.",
                        )

                    # Apply updates
                    if "display_name" in args:
                        context.display_name = args["display_name"]
                    if "description" in args:
                        context.description = args["description"]
                    if "summary" in args:
                        context.summary = args["summary"]
                    if "usage_guide" in args:
                        context.usage_guide = args["usage_guide"]
                    if "is_public" in args:
                        is_public = args["is_public"]
                        if is_public and not context.is_public:
                            # Making public: check plan allows it
                            from config.plan_tiers import get_plan_tier
                            from models.auth import Workspace

                            ws = await db.get(Workspace, context.workspace_id)
                            if ws:
                                plan = get_plan_tier(ws.plan_name)
                                if not plan.allows_shared_contexts:
                                    return _error_response(
                                        "plan_required",
                                        "Public contexts require a higher tier plan.",
                                    )
                        if not is_public and context.is_public and context.resource_id:
                            return _error_response(
                                "cannot_make_private",
                                "Cannot make private: context has a resource_id. Revoke tokens and remove resource_id first.",
                            )
                        context.is_public = is_public

                    if "resource_id" in args:
                        import re as _re

                        rid = args["resource_id"]
                        if not _re.match(r"^[a-z0-9_-]+$", rid) or len(rid) > 255:
                            return _error_response(
                                "invalid_resource_id",
                                "resource_id must be lowercase alphanumeric, underscores, and hyphens only (max 255 chars).",
                            )

                        # Revoke old tokens if resource_id is changing
                        old_rid = context.resource_id
                        if old_rid and old_rid != rid:
                            from sqlalchemy import select as _select

                            from auth.resource_tokens import ResourceTokenManager
                            from models.resource import ResourceToken

                            token_mgr = ResourceTokenManager(db)
                            old_tokens = await db.execute(
                                _select(ResourceToken).where(
                                    ResourceToken.resource_id == old_rid,
                                    ResourceToken.created_by == user_id,
                                    ResourceToken.is_active == True,  # noqa: E712
                                )
                            )
                            for token in old_tokens.scalars().all():
                                await token_mgr.revoke_token(token.id)

                        context.resource_id = rid

                    try:
                        await db.commit()
                    except Exception as commit_err:
                        await db.rollback()
                        if "unique_context_resource_id" in str(commit_err) or "resource_id" in str(
                            commit_err
                        ):
                            return _error_response(
                                "resource_id_conflict",
                                f"Resource ID '{args.get('resource_id', '')}' is already used by another context in this workspace.",
                            )
                        raise
                    await db.refresh(context)

                    await _log_tool_usage(
                        db,
                        user_id,
                        "update_context",
                        start_time,
                        200,
                        str(context.id),
                        workspace_id,
                    )

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "message": f"Context '{context.name}' updated successfully.",
                                    "updated_fields": list(requested_fields),
                                    **_context_response_fields(context),
                                }
                            ),
                        )
                    ]
                except ValueError:
                    return _error_response(
                        "invalid_context_id",
                        f"Invalid context_id format: {args['context_id']}",
                        help="Context ID must be a valid UUID.",
                    )
                except Exception as e:
                    await db.rollback()
                    logger.error(f"update_context_failed: {e}", exc_info=True)
                    return _error_response("update_context_error", str(e))

        # =================================================================
        # Tool 8: list_contexts (Issue #169)
        # =================================================================
        elif tool_name == "list_contexts":
            include_stats = args.get("include_stats", False)

            start_time = time.time()
            async for db in get_db():
                try:
                    from services.context_service import ContextService

                    context_service = ContextService(db)
                    contexts = await execute_with_timeout(
                        context_service.list_contexts(user_id),
                        operation_name="list_contexts",
                    )

                    from datetime import datetime

                    contexts_sorted = sorted(
                        contexts,
                        key=lambda c: c.last_used_at or datetime.min,
                        reverse=True,
                    )

                    context_list = []
                    for ctx in contexts_sorted:
                        ctx_data: dict[str, Any] = {
                            "id": str(ctx.id),
                            "name": ctx.name,
                            "summary": ctx.summary,
                            "is_private": ctx.is_private,
                            "last_used_at": ctx.last_used_at.isoformat()
                            if ctx.last_used_at
                            else None,
                        }
                        if include_stats:
                            try:
                                stats = await context_service.get_context_stats(user_id, ctx.id)
                                ctx_data["memory_count"] = stats.get("memory_count", 0)
                            except Exception:
                                ctx_data["memory_count"] = 0
                        context_list.append(ctx_data)

                    await _log_tool_usage(
                        db, user_id, "list_contexts", start_time, 200, None, workspace_id
                    )

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "contexts": context_list,
                                    "count": len(context_list),
                                }
                            ),
                        )
                    ]
                except Exception as e:
                    await db.rollback()
                    logger.error(f"list_contexts_failed: {e}", exc_info=True)
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps({"status": "error", "error": str(e)}),
                        )
                    ]

        # =================================================================
        # Tool: update_search_config (Issue #25)
        # =================================================================
        elif tool_name == "update_search_config":
            if "context_id" not in args:
                return _error_response("missing_fields", "Missing required field: context_id")

            start_time = time.time()
            async for db in get_db():
                try:
                    from uuid import UUID as _UUID

                    from models.schemas import ContextSearchConfigUpdate
                    from repositories.config_repository import ContextSearchConfigRepository
                    from services.permission_service import PermissionService

                    # Parse context_id
                    try:
                        ctx_uuid = _UUID(args["context_id"])
                    except ValueError:
                        return _error_response(
                            "invalid_context_id",
                            f"Invalid context_id: {args['context_id']}",
                        )

                    # Permission check (owner/editor)
                    perm_service = PermissionService(db)
                    try:
                        await perm_service.check_context_write(user_id, ctx_uuid)
                    except Exception as perm_err:
                        return _error_response("permission_denied", str(perm_err))

                    repo = ContextSearchConfigRepository(db)
                    config = await repo.get_by_context(ctx_uuid)

                    if not config:
                        return _error_response(
                            "not_found",
                            f"No search config for context {args['context_id']}",
                        )

                    # Build update with current values as defaults
                    update_fields = {
                        "semantic_weight": args.get(
                            "semantic_weight", float(config.semantic_weight)
                        ),
                        "bm25_weight": args.get("bm25_weight", float(config.bm25_weight)),
                        "fetch_factor": args.get("fetch_factor", config.fetch_factor),
                        "use_rerank": args.get("use_rerank", config.use_rerank),
                        "reranker_provider": args.get(
                            "reranker_provider", config.reranker_provider or "voyage"
                        ),
                        "reranker_model": args.get(
                            "reranker_model", config.reranker_model or "rerank-2"
                        ),
                    }

                    # Validate via Pydantic (same as REST API)
                    try:
                        update_data = ContextSearchConfigUpdate(**update_fields)
                    except Exception as validation_err:
                        return _error_response("invalid_search_config", str(validation_err))

                    # Apply via repository (same as REST API)
                    config = await repo.update(ctx_uuid, update_data)

                    await _log_tool_usage(
                        db,
                        user_id,
                        "update_search_config",
                        start_time,
                        200,
                        str(ctx_uuid),
                        workspace_id,
                    )

                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "success",
                                    "message": "Search configuration updated.",
                                    "context_id": str(ctx_uuid),
                                    "config": {
                                        "semantic_weight": float(config.semantic_weight),
                                        "bm25_weight": float(config.bm25_weight),
                                        "fetch_factor": config.fetch_factor,
                                        "use_rerank": config.use_rerank,
                                        "reranker_provider": config.reranker_provider,
                                        "reranker_model": config.reranker_model,
                                    },
                                }
                            ),
                        )
                    ]
                except Exception as e:
                    await db.rollback()
                    logger.error(f"update_search_config_failed: {e}", exc_info=True)
                    return _error_response("update_search_config_error", str(e))

        # =================================================================
        # Tool: kagura_memory_usage_guide
        # =================================================================
        elif tool_name == "kagura_memory_usage_guide":
            return [TextContent(type="text", text=KAGURA_MEMORY_USAGE_GUIDE)]

        # =================================================================
        # Unknown tool
        # =================================================================
        else:
            return _error_response("unknown_tool", f"Unknown tool: {tool_name}")

    except Exception as e:
        logger.error(f"mcp_tool_{tool_name}_failed: {e}", exc_info=True)
        return [
            TextContent(
                type="text",
                text=json.dumps({"status": "error", "error": str(e)}),
            )
        ]


async def _get_workspace_member_role(
    db: "AsyncSession", user_id: str, workspace_id: UUID
) -> str | None:
    """Get user's role in workspace.

    Args:
        db: Database session
        user_id: User ID
        workspace_id: Workspace ID

    Returns:
        Role string ('owner', 'admin', 'member', 'viewer') or None if not a member
    """
    from sqlalchemy import select

    from models.auth import WorkspaceMember

    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user_id, WorkspaceMember.workspace_id == workspace_id
        )
    )
    member = result.scalar_one_or_none()
    return member.role if member else None


def _resolve_context_id(arg_context_id: str) -> UUID:
    """Parse and return context_id from tool argument.

    Args:
        arg_context_id: Context ID from tool argument (required)

    Returns:
        Parsed UUID

    Raises:
        ValueError: If arg_context_id is invalid UUID format
    """
    try:
        return UUID(arg_context_id)
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError(
            f"Invalid context_id format: '{arg_context_id}'. "
            f"Expected a UUID (example: 'b3abeabe-7ab1-44bd-8e52-18a191bda66b'). "
            f"Use list_contexts() to discover valid context IDs."
        ) from e
