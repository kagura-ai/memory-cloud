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

SECURITY: DO NOT store secrets, credentials, or sensitive data in memories, including:
- API keys, tokens, passwords, client secrets
- Private keys, certificates
- Personally Identifiable Information (PII)
- OAuth refresh tokens, session cookies
- Contents of .env files or environment variables with credentials
If the user's input contains such data, refuse to store it and ask them to redact the sensitive values first.

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
                    "source_uri": {
                        "type": "string",
                        "description": "Origin URI for external integration (e.g. 'file:///path/to/note.md', 'vault://my-vault/note', 'https://example.com/page'). Max 2048 chars.",
                    },
                    "source_type": {
                        "type": "string",
                        "enum": ["file", "url", "vault", "api", "manual"],
                        "description": "Origin type. Use 'file' for local files, 'url' for web pages, 'vault' for Obsidian vaults, 'api' for API-ingested content, 'manual' for user-entered.",
                    },
                    "linked_memory_ids": {
                        "type": "array",
                        "items": {"type": "string", "format": "uuid"},
                        "description": "Explicit links to existing memories by ID. Creates declared_link edges (weight 1.0). Use for known relationships like Obsidian [[wikilinks]] resolved to memory IDs.",
                    },
                    "linked_source_uris": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit links by source_uri (resolved to memory_id at remember time). Unresolved URIs are silently skipped — the plugin can retry later when the target memory exists.",
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

SECURITY: DO NOT store secrets, credentials, or sensitive data in memories, including:
- API keys, tokens, passwords, client secrets
- Private keys, certificates
- Personally Identifiable Information (PII)
- OAuth refresh tokens, session cookies
- Contents of .env files or environment variables with credentials
If the user's input contains such data, refuse to store it and ask them to redact the sensitive values first.

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
                "required": ["query"],
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
                        "description": "Optional filters as JSON. Tag filter matches ANY of the specified tags by default (exact match). Set tags_match='all' to require ALL tags (AND logic). Date filters: created_after, created_before, updated_after, updated_before (ISO 8601). Source filters: 'source_uri_prefix' for origin prefix match (e.g. 'file://', 'vault://my-vault/'), 'source_type' for exact type match ('file'|'url'|'vault'|'api'|'manual'). Examples: {'type': 'code'}, {'tags': ['python', 'fastapi'], 'tags_match': 'all'}, {'importance': {'gte': 0.7}}, {'created_after': '2026-03-01T00:00:00Z'}, {'source_uri_prefix': 'vault://', 'source_type': 'vault'}",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs. For cross-context search, use context_ids instead.",
                    },
                    "context_ids": {
                        "type": "array",
                        "items": {"type": "string", "format": "uuid"},
                        "minItems": 2,
                        "maxItems": 20,
                        "description": "Search across multiple contexts (Issue #81). List of 2-20 context UUIDs. All contexts must use the same embedding model. Overrides context_id when provided.",
                    },
                    "search_mode": {
                        "type": "string",
                        "enum": ["hybrid", "semantic", "keyword"],
                        "description": "Search strategy. If omitted, hybrid is used by default: 60% semantic + 40% BM25 with Neural Memory boosting. semantic: vector similarity only (no BM25, Neural Memory skipped). keyword: BM25 only (no embeddings; best for hiragana queries where embedding models struggle).",
                    },
                    "include_explore_hints": {
                        "type": "boolean",
                        "description": "Include up to 3 explore_hints in the response suggesting good seed memories for a follow-up explore() call (default: false). Set to true when the user is exploring a topic broadly or asks 'what else is related?' — the hints bridge recall (precision search) and explore (graph discovery) without mixing their scoring. Each hint includes a memory_id and a reason (top_result, high_centrality, or unexplored_neighbor).",
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
                        "description": "Optional filter for specific relation types: 'neural_association', 'related_to', 'depends_on', 'learned_from'. Omit to explore all connections.",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID (e.g. '550e8400-e29b-41d4-a716-446655440000'). MUST be a valid UUID from list_contexts(). Do NOT guess or fabricate IDs.",
                    },
                },
            },
        },
        # =================================================================
        # Edge CRUD tools (Issue #163)
        # =================================================================
        {
            "name": "list_edges",
            "readOnly": True,
            "description": """List all Neural Memory graph edges connected to a specific memory.

Returns both outgoing and incoming edges, showing how this memory relates to others in the knowledge graph.

Use this to:
- Inspect a memory's connections before deciding to remove noisy edges
- Understand the graph structure around a specific memory
- Audit edges created by Sleep Maintenance's Edge Discovery

Response includes: edge_id, source_id, target_id, edge_type, weight, confidence, timestamps.""",
            "inputSchema": {
                "type": "object",
                "required": ["memory_id", "context_id"],
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the memory to list edges for. Obtained from recall() or explore() results.",
                    },
                    "min_weight": {
                        "type": "number",
                        "description": "Minimum edge weight threshold (default: 0.0). Set higher to filter weak edges.",
                        "default": 0.0,
                    },
                    "edge_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by edge types: 'neural_association', 'related_to', 'depends_on', 'learned_from'. Omit to list all.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of edges to return per direction (outgoing/incoming).",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID. MUST be a valid UUID from list_contexts().",
                    },
                },
            },
        },
        {
            "name": "create_edge",
            "description": """Create a manual edge between two memories in the Neural Memory graph.

Use this when you know two memories are related but the automatic Edge Discovery hasn't connected them.
The default weight of 0.5 is suitable for most manually created edges — you only need to specify source and target.

Edge types:
- 'related_to' (default): General relationship between memories
- 'depends_on': Target memory depends on source
- 'learned_from': Knowledge derived from source
- 'neural_association': Auto-created by Hebbian learning (prefer 'related_to' for manual edges)

Weight range: 0.0 (weakest) to 3.0 (strongest). Default: 0.5 (moderate manual connection).""",
            "inputSchema": {
                "type": "object",
                "required": ["source_id", "target_id", "context_id"],
                "properties": {
                    "source_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the source memory.",
                    },
                    "target_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the target memory.",
                    },
                    "edge_type": {
                        "type": "string",
                        "enum": ["neural_association", "related_to", "depends_on", "learned_from"],
                        "description": "Type of relationship (default: 'related_to').",
                        "default": "related_to",
                    },
                    "weight": {
                        "type": "number",
                        "description": "Edge weight 0.0-3.0 (default: 0.5). Higher = stronger connection.",
                        "default": 0.5,
                    },
                    "confidence": {
                        "type": "number",
                        "description": "Confidence score 0.0-1.0 (default: 1.0).",
                        "default": 1.0,
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID. MUST be a valid UUID from list_contexts().",
                    },
                },
            },
        },
        {
            "name": "update_edge",
            "description": """Update an existing edge's weight or type.

Use this to:
- Strengthen an edge: increase weight when memories are confirmed related
- Weaken an edge: decrease weight for less relevant connections
- Change edge type: reclassify the relationship

Identify edges using source_id + target_id (from list_edges or explore results).""",
            "inputSchema": {
                "type": "object",
                "required": ["source_id", "target_id", "context_id"],
                "properties": {
                    "source_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the source memory.",
                    },
                    "target_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the target memory.",
                    },
                    "weight": {
                        "type": "number",
                        "description": "New edge weight 0.0-3.0.",
                    },
                    "edge_type": {
                        "type": "string",
                        "enum": ["neural_association", "related_to", "depends_on", "learned_from"],
                        "description": "New edge type.",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID. MUST be a valid UUID from list_contexts().",
                    },
                },
            },
        },
        {
            "name": "delete_edge",
            "description": """Delete an edge between two memories.

Use this to remove noisy or incorrect connections from the Neural Memory graph.
Typical workflow: explore() → list_edges() → identify bad edge → delete_edge().

This is a hard delete — the edge is permanently removed. If the memories are still co-accessed,
Hebbian learning may recreate a neural_association edge automatically.""",
            "inputSchema": {
                "type": "object",
                "required": ["source_id", "target_id", "context_id"],
                "properties": {
                    "source_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the source memory.",
                    },
                    "target_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "UUID of the target memory.",
                    },
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID. MUST be a valid UUID from list_contexts().",
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
        # Tool: merge_contexts (Issue #90)
        # =================================================================
        {
            "name": "merge_contexts",
            "description": """Copy all memories from a source context into a target context.

Both contexts must use the same embedding model (no re-embedding needed).
Memories are copied, not moved — source context retains its memories unless delete_source=true.

Use cases:
- Consolidate a test context into production
- Merge split contexts back into one
- Clean up after project completion

Requires owner access to both contexts. Same workspace required.""",
            "inputSchema": {
                "type": "object",
                "required": ["source_id", "target_id"],
                "properties": {
                    "source_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Source context UUID to copy memories FROM. MUST be a valid UUID from list_contexts().",
                    },
                    "target_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID to copy memories INTO. MUST be a valid UUID from list_contexts().",
                    },
                    "delete_source": {
                        "type": "boolean",
                        "description": "Soft-delete source context after successful merge (default: false). Locked contexts cannot be deleted.",
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
- Local reranking (free): use_rerank=true, reranker_provider="ollama", reranker_model="dengcao/Qwen3-Reranker-8B:Q5_K_M"

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
                        "description": "Enable/disable reranking. Requires API key for Voyage/Cohere, or use 'ollama' for free local reranking.",
                    },
                    "reranker_provider": {
                        "type": "string",
                        "description": "Reranker provider: 'voyage', 'cohere', or 'ollama' (local, no API key needed).",
                    },
                    "reranker_model": {
                        "type": "string",
                        "description": "Provider-specific model name (e.g., 'rerank-2', 'rerank-multilingual-v3.0').",
                    },
                },
            },
        },
        {
            "name": "get_usage",
            "readOnly": True,
            "description": """Get current workspace usage and quota limits.

Returns memory, context, and member usage against effective limits (plan tier + addons).
Use this to check quota before bulk operations or to display usage in UIs.

Response includes:
- plan: Current plan tier name
- memories: {used, limit, percentage}
- contexts: {used, limit}
- members: {used, limit}
- mcp_calls_per_day: {limit}

No parameters required — uses the current workspace.""",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        # ====================================================================
        # Sleep Maintenance Observability (Issue #164)
        # ====================================================================
        {
            "name": "get_sleep_history",
            "readOnly": True,
            "description": """List recent Sleep Maintenance runs for a context.

Returns a summary of each run including status, timing, and counters
(memories processed, edges created, merges, promotions).

Use this to check what Sleep Maintenance has been doing and when it last ran.
Combine with get_sleep_report(report_id) for action-level detail.""",
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Target context UUID (from list_contexts).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of reports to return (default: 10, max: 50).",
                        "default": 10,
                    },
                },
            },
        },
        {
            "name": "get_sleep_report",
            "readOnly": True,
            "description": """Get detailed Sleep Maintenance report with all recorded actions.

Returns the full report (per-phase results, cost tracking) plus the
complete audit log of individual actions (edges created, merges,
importance changes, promotions, archives).

Each action includes:
- phase: Which phase took the action
- action_type: create_edge, merge, update_importance, promote, archive
- memory_id/target_id: Affected memories
- details: Action-specific data (old/new values, similarity scores, etc.)

Use get_sleep_history() first to find report_ids.""",
            "inputSchema": {
                "type": "object",
                "required": ["report_id"],
                "properties": {
                    "report_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Sleep report UUID (from get_sleep_history).",
                    },
                },
            },
        },
        {
            "name": "rollback_sleep_run",
            "description": """Rollback all actions from a completed Sleep Maintenance run.

Reverses each recorded action in order:
- create_edge → deletes the edge
- merge → restores the soft-deleted loser memory and re-embeds it
- update_importance → restores the previous importance value
- promote → reverts scope back to 'working'
- archive → restores the deleted memory and re-embeds it

Only works on reports with status 'completed'. After rollback, the
report is marked 'rolled_back' to prevent double rollback.

⚠️ This is a destructive operation — use with care.
Requires action recording (reports created before this feature have no actions to rollback).""",
            "inputSchema": {
                "type": "object",
                "required": ["report_id"],
                "properties": {
                    "report_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": "Sleep report UUID to rollback (from get_sleep_history).",
                    },
                },
            },
        },
        # ====================================================================
        # Resource Management Tools (Issue #46)
        # ====================================================================
        {
            "name": "setup_resource",
            "description": (
                "Create a new public context with an associated resource token in a single "
                "atomic operation. Use this to set up a resource ingestion pipeline.\n\n"
                "Returns context_id, resource_id, and a plaintext token. "
                "Save the token immediately — it is shown only once.\n\n"
                "Requires owner or admin role. Requires PRO plan.\n\n"
                "Example:\n"
                '  setup_resource(name="ec-products", resource_id="ec_products")\n'
                "  → context created, token issued, ready for ingest_events()"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["name", "resource_id"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Context name (lowercase alphanumeric, hyphens, underscores; "
                            "max 100 chars). Example: 'ec-products'."
                        ),
                    },
                    "resource_id": {
                        "type": "string",
                        "description": (
                            "Resource identifier (lowercase alphanumeric, hyphens, underscores; "
                            "max 255 chars). Must be unique in the workspace. "
                            "Example: 'ec_products'."
                        ),
                    },
                    "display_name": {
                        "type": "string",
                        "description": "Human-readable display name for the context.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Token description to identify its purpose.",
                    },
                    "quota_events_per_hour": {
                        "type": "integer",
                        "description": (
                            "Event ingestion quota per hour for the token (1-10000, default: 1000)."
                        ),
                    },
                },
            },
        },
        {
            "name": "ingest_events",
            "description": (
                "Batch ingest resource events (upsert/delete) into a resource. "
                "Maximum 100 events per call, 100KB max per event payload.\n\n"
                "Triggers incremental indexing for all contexts bound to this resource. "
                "Uses session authentication (not resource tokens).\n\n"
                "For bulk imports (10k+ records), use the CLI or SDK instead.\n\n"
                "Example:\n"
                '  ingest_events(resource_id="ec_products", events=[\n'
                '    {"op": "upsert", "doc_id": "PROD-1", "version": 1, '
                '"payload": {"name": "...", "price": 5980}},\n'
                '    {"op": "delete", "doc_id": "PROD-999"}\n'
                "  ])"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["resource_id", "events"],
                "properties": {
                    "resource_id": {
                        "type": "string",
                        "description": ("Resource identifier. Must belong to your workspace."),
                    },
                    "events": {
                        "type": "array",
                        "description": "List of events to ingest (max 100).",
                        "items": {
                            "type": "object",
                            "required": ["op", "doc_id"],
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["upsert", "delete"],
                                    "description": "Operation type.",
                                },
                                "doc_id": {
                                    "type": "string",
                                    "description": (
                                        "Document identifier (stable across versions)."
                                    ),
                                },
                                "version": {
                                    "type": "integer",
                                    "description": (
                                        "Document version (required for upsert, "
                                        "null for delete-all-versions)."
                                    ),
                                },
                                "payload": {
                                    "type": "object",
                                    "description": (
                                        "Document payload (required for upsert, "
                                        "null for delete). Max 100KB."
                                    ),
                                },
                                "idempotency_key": {
                                    "type": "string",
                                    "description": "Optional deduplication key.",
                                },
                                "importance": {
                                    "type": "number",
                                    "description": (
                                        "Memory importance score (0.0-1.0, default 0.6)."
                                    ),
                                },
                                "event_metadata": {
                                    "type": "object",
                                    "description": (
                                        "Optional metadata key-value pairs "
                                        "(source, tenant, correlation_id, etc.)."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        },
        {
            "name": "get_resource_impact",
            "readOnly": True,
            "description": (
                "Get resource stats: active token count, memory count, and current "
                "schema version. Use before creating/modifying a schema to understand "
                "the impact.\n\n"
                "Example:\n"
                '  get_resource_impact(resource_id="ec_products")\n'
                "  → {token_count: 2, memory_count: 500, current_schema_version: 3}"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["resource_id"],
                "properties": {
                    "resource_id": {
                        "type": "string",
                        "description": ("Resource identifier. Must belong to your workspace."),
                    },
                },
            },
        },
        {
            "name": "get_resource_schema",
            "readOnly": True,
            "description": (
                "Get the field schema definition for a resource. Returns field names, "
                "types, descriptions, and classification.\n\n"
                "Specify schema_version for a historical version, or omit for the latest.\n\n"
                "Example:\n"
                '  get_resource_schema(resource_id="ec_products")\n'
                '  → {schema_version: 3, field_definitions: [{name: "product_name", ...}]}'
            ),
            "inputSchema": {
                "type": "object",
                "required": ["resource_id"],
                "properties": {
                    "resource_id": {
                        "type": "string",
                        "description": ("Resource identifier. Must belong to your workspace."),
                    },
                    "schema_version": {
                        "type": "integer",
                        "description": ("Schema version to retrieve (default: latest)."),
                    },
                },
            },
        },
        {
            "name": "list_resource_tokens",
            "readOnly": True,
            "description": (
                "List resource tokens for your workspace. Optionally filter by resource_id. "
                "Returns token metadata (no plaintext tokens). "
                "Requires owner or admin role.\n\n"
                "Example:\n"
                '  list_resource_tokens(resource_id="ec_products")\n'
                "  → {tokens: [{id: 1, resource_id: ..., is_active: true, ...}], total: 3}"
            ),
            "inputSchema": {
                "type": "object",
                "required": [],
                "properties": {
                    "resource_id": {
                        "type": "string",
                        "description": (
                            "Filter by resource_id (optional). "
                            "If provided, must belong to your workspace."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": ("Number of tokens per page (1-100, default: 50)."),
                    },
                    "offset": {
                        "type": "integer",
                        "description": ("Starting offset for pagination (default: 0)."),
                    },
                    "include_revoked": {
                        "type": "boolean",
                        "description": ("Include revoked tokens in results (default: true)."),
                    },
                },
            },
        },
        # ====================================================================
        # Memory Broadlistening Analysis Tools (Issue #496)
        # ====================================================================
        {
            "name": "analyze_context",
            "description": (
                "Start a memory broadlistening analysis run on a context, "
                "or preview the cost when ``dry_run=true``. The analysis "
                "clusters memories into themes (kouchou-ai-style UMAP + "
                "KMeans + LLM labeling) and exposes them via "
                "``get_analysis`` / ``get_cluster`` / cluster-scoped recall.\n\n"
                "Requires the workspace owner role + Pro plan + an enabled "
                "OpenAI BYOK key + per-day quota available. v1 is daily=3 "
                "for Pro; addon ``extra_analysis_runs`` increases the limit. "
                "When ``dry_run=true``, the same gates apply but no row is "
                "created.\n\n"
                "Example:\n"
                '  analyze_context(context_id="...", dry_run=True)  # cost preview\n'
                '  analyze_context(context_id="...")                # 202 + run_id'
            ),
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID (must belong to your workspace).",
                    },
                    "from": {
                        "type": "string",
                        "description": "Optional ISO-8601 lower bound on memory.created_at.",
                    },
                    "to": {
                        "type": "string",
                        "description": "Optional ISO-8601 upper bound on memory.created_at.",
                    },
                    "types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of memory types to include.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of tags to filter on.",
                    },
                    "min_importance": {
                        "type": "number",
                        "description": "Optional importance floor (0.0–1.0).",
                    },
                    "query": {
                        "type": "string",
                        "description": "Reserved for v1.5 query-scoped runs (ignored in v1).",
                    },
                    "model_id": {
                        "type": "integer",
                        "description": (
                            "Optional ``llm_pricing.id`` override. v1 default = "
                            "openai gpt-5-nano (resolved server-side)."
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": (
                            "When true, return the cost estimate without "
                            "starting a run (default: false)."
                        ),
                    },
                },
            },
        },
        {
            "name": "get_analysis",
            "readOnly": True,
            "description": (
                "Fetch one analysis run by id, scoped to your workspace. "
                "Returns ``run_not_found`` for unknown ids OR runs in "
                "another workspace (existence is not leaked).\n\n"
                "Example:\n"
                '  get_analysis(run_id="...")\n'
                "  → {run_id, status, started_at, finished_at, "
                "cost_estimated_cents, cost_actual_cents, ...}"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["run_id"],
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Analysis run UUID (from analyze_context).",
                    },
                },
            },
        },
        {
            "name": "list_analyses",
            "readOnly": True,
            "description": (
                "List analysis runs for a context, sorted newest first. "
                "Cursor-paginated — pass the previous response's "
                "``next_cursor`` to fetch the next page. ``next_cursor=null`` "
                "marks the last page.\n\n"
                "Example:\n"
                '  list_analyses(context_id="...", limit=20)\n'
                '  → {items: [...], next_cursor: "2026-04-30T12:34:56"}'
            ),
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Page size (1-100, default 20).",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque pagination cursor from a previous response.",
                    },
                },
            },
        },
        {
            "name": "get_active_analysis",
            "readOnly": True,
            "description": (
                "Return the most recent ``status='succeeded'`` run for a "
                "context, or ``no_succeeded_run`` if the context has no "
                "completed analyses yet.\n\n"
                "Example:\n"
                '  get_active_analysis(context_id="...")\n'
                '  → {run_id, status: "succeeded", finished_at, ...}'
            ),
            "inputSchema": {
                "type": "object",
                "required": ["context_id"],
                "properties": {
                    "context_id": {
                        "type": "string",
                        "description": "Target context UUID.",
                    },
                },
            },
        },
        {
            "name": "get_cluster",
            "readOnly": True,
            "description": (
                "Drill-down view of one cluster within an analysis run. "
                "Returns the cluster label/description/count + the top "
                "representative memories (capped at 5) + a paginated list of "
                "all member memories (Layer 1 + 2: summary, tags, importance).\n\n"
                "Page size defaults to 50 (max 200). For semantic search "
                "scoped to this cluster, call ``recall`` with "
                '``filters={"analysis_cluster": {"run_id": ..., "cluster_index": ...}}``.\n\n'
                "Example:\n"
                '  get_cluster(run_id="...", cluster_index=3)\n'
                "  → {label, description, count, representatives: [...], "
                "memories: [...], next_cursor: ...}"
            ),
            "inputSchema": {
                "type": "object",
                "required": ["run_id", "cluster_index"],
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Analysis run UUID.",
                    },
                    "cluster_index": {
                        "type": "integer",
                        "description": (
                            "Zero-based ordinal of the cluster within the run "
                            "(stable across calls)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Memories per page (1-200, default 50).",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque pagination cursor from a previous response.",
                    },
                },
            },
        },
    ]


# ============================================================================
# Tool Execution Helpers (Issue #172: DRY reduction)
# ============================================================================


# ============================================================================
# Main Tool Execution Entry Point
# ============================================================================
