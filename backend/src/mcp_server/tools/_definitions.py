"""MCP tool JSON schema definitions.

Extracted from tools.py for modularity (Issue #7).
"""


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
                        "description": "Tags for categorization and filtering. Tags are indexed for keyword filtering (exact match via recall filters). Include: (1) Category tags: 'category:{domain}' (e.g., 'category:auth', 'category:料理'). (2) Entity tags: key terms. (3) For Japanese: include kanji, katakana, hiragana variations (e.g., ['鯖', 'サバ', 'さば']). Examples: ['python', 'fastapi', 'category:backend'] or ['category:和食', '鯖', 'サバ', 'さば', '味噌煮'].",
                    },
                    "context": {
                        "type": "object",
                        "description": "Additional context metadata as JSON. Can include context info, related issue numbers, or custom fields.",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
                    },
                },
            },
        },
        {
            "name": "update_memory",
            "description": """Update an existing memory in-place or upsert by external ID.

Two modes:
1. **In-place update (memory_id)**: Modifies specific fields while preserving memory ID, graph edges, and creation timestamp. Only re-embeds if summary or content changes. Use when you have a memory_id from recall().
2. **Upsert (external_id)**: Finds existing memory by external resource ID within the context. If found, replaces it (new memory_id). If not found, creates new. Requires summary, content, and type. Use for sync workflows with stable external identifiers.

Provide exactly one of memory_id or external_id.

IMPORTANT: Always specify context_id.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of memory to update in-place (e.g. '550e8400-e29b-41d4-a716-446655440000'). Obtained from recall() results. Do NOT guess or fabricate IDs.",
                    },
                    "external_id": {
                        "type": "string",
                        "description": "External resource ID for upsert lookup (stored in details.resource_id).",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Updated summary (10-500 chars). Required for upsert mode.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Updated content. Required for upsert mode.",
                    },
                    "type": {
                        "type": "string",
                        "description": "Updated memory type. Required for upsert mode.",
                    },
                    "context_summary": {
                        "type": "string",
                        "description": "Updated context summary (max 2000 chars).",
                    },
                    "details": {
                        "type": "object",
                        "description": "Updated structured details (JSON).",
                    },
                    "importance": {
                        "type": "number",
                        "description": "Updated importance (0.0-1.0).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Updated tags.",
                    },
                    "context": {
                        "type": "object",
                        "description": "Updated context metadata.",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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

IMPORTANT: Always specify context_id to ensure you're searching the intended context. Use list_contexts() to discover available context IDs.

Search modes: Use search_mode to control the search strategy.
• hybrid (default): Best for most queries — combines semantic understanding with keyword matching.
• semantic: Vector similarity only — best when you know the exact concept but not the exact words.
• keyword: BM25 only — best for hiragana queries, exact term matching, or when semantic search returns noise. Particularly effective for Japanese hiragana-only queries where embedding models struggle.""",
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
                        "description": "Optional filters as JSON. Tag filter matches ANY of the specified tags by default (exact match). Set tags_match='all' to require ALL tags (AND logic). Date filters: created_after, created_before, updated_after, updated_before (ISO 8601). Examples: {'type': 'code'}, {'tags': ['python', 'fastapi'], 'tags_match': 'all'}, {'importance': {'gte': 0.7}}, {'created_after': '2026-03-01T00:00:00Z'}",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
                    },
                    "search_mode": {
                        "type": "string",
                        "enum": ["hybrid", "semantic", "keyword"],
                        "description": "Search strategy. If omitted, hybrid is used by default: 60% semantic + 40% BM25 with Neural Memory boosting. semantic: vector similarity only (no BM25, Neural Memory skipped). keyword: BM25 only (no embeddings; best for hiragana queries where embedding models struggle).",
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
                        "format": "uuid",
                        "description": "UUID of the memory to retrieve (e.g. '550e8400-e29b-41d4-a716-446655440000'). Obtained from recall() results. Do NOT guess or fabricate IDs.",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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
                        "format": "uuid",
                        "description": "UUID of specific memory to delete (e.g. '550e8400-e29b-41d4-a716-446655440000'). Obtained from recall() results. Do NOT guess or fabricate IDs.",
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
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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
                        "format": "uuid",
                        "description": "UUID of the starting memory (seed node, e.g. '550e8400-e29b-41d4-a716-446655440000'). Obtained from recall() results. Do NOT guess or fabricate IDs.",
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
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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
                        "format": "uuid",
                        "description": "Context UUID to get info for (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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
- count: Total number of contexts
- limit: Maximum contexts allowed by plan
- can_create: Whether new contexts can be created""",
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
                    "embedding_model": {
                        "type": "string",
                        "description": "Embedding model to use for this context. Options: 'text-embedding-3-small' (OpenAI, 512dim), 'qwen3-embedding:8b' (Ollama, 4096dim), etc. Default: global EMBEDDING_MODEL setting. Immutable after creation.",
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

Modify summary, usage_guide, description, display_name, resource_id, is_public, or is_locked of a context.

Requires owner or editor role in the context.
- summary/usage_guide/resource_id/is_public/is_locked: Owner-only fields
- display_name/description: Editor access sufficient

Use get_context_info() to see current values before updating.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Context UUID to update (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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
                    "is_locked": {
                        "type": "boolean",
                        "description": "Deletion protection only. When locked (true), the context cannot be deleted — but reading, writing, and searching memories still work normally. Owner-only.",
                    },
                },
            },
        },
        # =================================================================
        # Tool: delete_context (Issue #77)
        # =================================================================
        {
            "name": "delete_context",
            "description": """Soft-delete a context and all its memories. The context will no longer appear in list_contexts() or be searchable, but data is retained for recovery.

Only the context owner can delete. The default context cannot be deleted.
Locked contexts (is_locked=true) must be unlocked first via update_context(is_locked=false).

IMPORTANT: This action soft-deletes all memories in the context. Use with caution.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Context UUID to delete (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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
                        "format": "uuid",
                        "description": "Context UUID to configure (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
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


# ============================================================================
# Main Tool Execution Entry Point
# ============================================================================
