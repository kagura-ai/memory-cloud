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
    "recall": _get_timeout("recall", 60.0),
    "forget": _get_timeout("forget", 15.0),
    "reference": _get_timeout("reference", 10.0),
    "explore": _get_timeout("explore", 45.0),
    "get_context_info": _get_timeout("get_context_info", 15.0),
    "list_contexts": _get_timeout("list_contexts", 10.0),
    "create_context": _get_timeout("create_context", 15.0),
    "update_context": _get_timeout("update_context", 15.0),
    "update_search_config": _get_timeout("update_search_config", 10.0),
    "kagura_memory_usage_guide": _get_timeout("kagura_memory_usage_guide", 5.0),
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

Kagura Memory Cloud organizes memories into "contexts" — isolated namespaces within a workspace.

**Examples:**
- "Sales Team" context → Deals and customer info
- "Dev Team" context → Technical knowledge and bug fixes
- "Personal Notes" context → Private information

**Context Management (via MCP tools):**
- `list_contexts()` — See all available contexts and quota info
- `create_context(name="my-project")` — Create a new context
- `update_context(context_id, summary="...", usage_guide="...")` — Update context settings
- `get_context_info(context_id)` — Get context details, stats, and usage guidelines

### Search Tuning

Each context can have its own search configuration:
- `update_search_config(context_id, semantic_weight=0.5, bm25_weight=0.5)` — Adjust hybrid search balance
- Enable reranking: `update_search_config(context_id, use_rerank=true, reranker_provider="voyage")`
- Weights must sum to 1.0 (default: semantic 0.6 + BM25 0.4)

### Context Settings (Recommended)

Each context can have "instructions for AI" configured via `update_context()` or the web admin panel.
When you set up instructions, the AI automatically:
- Adds appropriate tags
- Judges importance levels
- Saves in searchable formats

**Via MCP:**
```
update_context(context_id, summary="Project X knowledge base", usage_guide="Tag all memories with project-x. Set importance 0.8+ for decisions.")
```

**Via Web Admin:**
1. Open context settings
2. Enter "Summary (for AI)" - what this context is for
3. Enter "Instructions (for AI)" - how to organize memories

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

## Available Tools (11 tools)

| Tool | Purpose |
|------|---------|
| `remember` | Store memories (3-layer: summary, context, details) |
| `recall` | Search memories (hybrid: semantic + BM25 keyword) |
| `forget` | Delete memories (soft delete, recoverable) |
| `reference` | Get full memory details (Layer 3) by ID |
| `explore` | Discover related memories via Neural Memory graph |
| `get_context_info` | Get context settings, stats, and usage guide |
| `list_contexts` | List all contexts with quota info |
| `create_context` | Create a new context namespace |
| `update_context` | Update context settings (summary, usage_guide, etc.) |
| `update_search_config` | Tune hybrid search weights and reranker |
| `kagura_memory_usage_guide` | Show this usage guide |

All tools except `list_contexts`, `create_context`, and `kagura_memory_usage_guide` require `context_id`.

---

## Troubleshooting

- **No search results**: Try different keywords or ask "Find anything about [topic]"
- **Want related info**: Ask "Show me everything related to this"
- **Clean up old info**: Tell the AI "Delete outdated information about [topic]"
- **Context full**: Check `list_contexts()` for quota info; upgrade plan or add addon
"""
